import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker as worker


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    (home / 'profiles/worker').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    conn = kb.connect(tmp_path / 'board.db')
    yield conn
    conn.close()


def claim(conn):
    tid = kb.create_task(conn, title='Harmless process fixture', execution_authority='Isolated process acceptance', assignee='worker')
    return kb.claim_task(conn, tid)


def spawn(conn, task, initialise=True):
    env = dict(os.environ, HERMES_KANBAN_TASK=task.id,
               HERMES_KANBAN_RUN_ID=str(task.current_run_id), HERMES_KANBAN_CLAIM_LOCK=task.claim_lock)
    code = 'import time; time.sleep(300)'
    proc = subprocess.Popen([sys.executable, '-c', code], env=env)
    kb._set_worker_pid(conn, task.id, proc.pid)
    if initialise:
        conn.execute("UPDATE worker_attempts SET startup_state = 'initialised', initialised_at = ? WHERE run_id = ?",
                     (int(time.time()), task.current_run_id))
        conn.commit()
    return proc


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
    proc.wait(timeout=10)


def test_identity_rejects_pid_reuse_namespace_and_unrelated_process(board):
    task = claim(board)
    proc = spawn(board, task)
    try:
        identity = worker.process_identity(proc.pid, task.id, task.current_run_id, task.claim_lock)
        assert identity and worker.identity_alive(identity)
        mutations = [dict(identity, start=identity['start'] + 1), dict(identity, run_id=-1),
                     dict(identity, namespace='pid:[unrelated]'), dict(identity, pid=os.getpid())]
        assert mutations
        for invalid in mutations:
            assert not worker.identity_alive(invalid)
        board.execute('UPDATE worker_attempts SET identity = ? WHERE run_id = ?',
                      (json.dumps(mutations[0]), task.current_run_id))
        board.commit()
        signals = []
        result = kb._terminate_reclaimed_worker(proc.pid, task.claim_lock, conn=board,
                                                signal_fn=lambda *args: signals.append(args))
        assert not signals and not result['termination_attempted']
        assert proc.poll() is None
    finally:
        stop(proc)


def test_missing_startup_blocks_without_signalling_unrelated_pid(board):
    task = claim(board)
    kb._set_worker_pid(board, task.id, os.getpid())
    board.execute('UPDATE worker_attempts SET launched_at = 0 WHERE run_id = ?', (task.current_run_id,))
    board.commit()
    assert kb.check_worker_startup(board) == [task.id]
    assert kb.get_task(board, task.id).status == 'blocked'
    assert board.execute('SELECT startup_state FROM worker_attempts').fetchone()[0] == 'failed-start'
    assert kb.check_worker_startup(board) == []


def test_quiet_initialised_worker_keeps_claim(board):
    task = claim(board)
    proc = spawn(board, task)
    try:
        board.execute('UPDATE tasks SET claim_expires = 1, last_heartbeat_at = 1 WHERE id = ?', (task.id,))
        board.commit()
        assert kb.release_stale_claims(board) == 0
        assert kb.get_task(board, task.id).claim_lock == task.claim_lock
        assert proc.poll() is None
    finally:
        stop(proc)


def test_delayed_startup_is_not_reclaimed_before_deadline(board):
    task = claim(board)
    proc = spawn(board, task, initialise=False)
    try:
        assert kb.check_worker_startup(board) == []
        assert kb.detect_crashed_workers(board) == []
        assert kb.get_task(board, task.id).status == 'running'
    finally:
        stop(proc)


def test_short_lived_dispatch_refused_before_claim(board):
    tid = kb.create_task(board, title='No launch', assignee='worker')
    with pytest.raises(RuntimeError, match='persistent'):
        kb.dispatch_once(board)
    assert kb.get_task(board, tid).current_run_id is None


def test_worker_context_cannot_own_dispatch(monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'nested')
    with pytest.raises(RuntimeError, match='persistent'):
        worker.validate_launch_namespace()


def test_real_worker_acknowledges_exact_run(board, tmp_path):
    task = claim(board)
    database = board.execute('PRAGMA database_list').fetchone()[2]
    receipt = tmp_path / 'acknowledged'
    code = '''import os, time
from pathlib import Path
from hermes_cli import kanban_db as kb
conn = kb.connect(Path(os.environ['HERMES_KANBAN_DB']))
for i in range(100):
    if conn.execute('SELECT 1 FROM worker_attempts WHERE run_id = ?', (int(os.environ['HERMES_KANBAN_RUN_ID']),)).fetchone():
        break
    time.sleep(.05)
kb.acknowledge_worker(conn, os.environ['HERMES_KANBAN_TASK'], int(os.environ['HERMES_KANBAN_RUN_ID']), os.environ['HERMES_KANBAN_CLAIM_LOCK'])
Path(os.environ['ACK_RECEIPT']).touch()
time.sleep(300)
'''
    env = dict(os.environ, HERMES_KANBAN_DB=database, HERMES_KANBAN_TASK=task.id,
               HERMES_KANBAN_RUN_ID=str(task.current_run_id), HERMES_KANBAN_CLAIM_LOCK=task.claim_lock,
               ACK_RECEIPT=str(receipt))
    proc = subprocess.Popen([sys.executable, '-c', code], env=env)
    try:
        kb._set_worker_pid(board, task.id, proc.pid)
        deadline = time.monotonic() + 15
        while not receipt.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(.05)
        assert receipt.exists()
        assert kb._worker_initialised(board, task.id)
        assert kb.check_worker_startup(board) == []
    finally:
        stop(proc)


def test_failed_start_can_recover_once(board, monkeypatch):
    task = claim(board)
    proc = spawn(board, task, initialise=False)
    try:
        board.execute('UPDATE worker_attempts SET launched_at = 0 WHERE run_id = ?', (task.current_run_id,))
        board.commit()
        assert kb.check_worker_startup(board) == [task.id]
        proc.wait(timeout=10)
        from hermes_cli.kanban_admission import authorise
        authorise(board, task.id, 'Approved isolated startup recovery')
        kb.unblock_task(board, task.id)
        claimed = kb.claim_task(board, task.id)
        assert claimed and claimed.current_run_id != task.current_run_id
        assert kb.claim_task(board, task.id) is None
    finally:
        stop(proc)


def test_closed_run_cannot_overlap_its_live_worker(board):
    task = claim(board)
    proc = spawn(board, task)
    try:
        kb._record_task_failure(board, task.id, 'Interrupted fixture', outcome='crashed', release_claim=True, end_run=True)
        assert kb.claim_task(board, task.id) is None
    finally:
        stop(proc)
    assert kb.claim_task(board, task.id)


def test_failing_spawn_callback_is_called_once(board):
    kb.create_task(board, title='Single spawn', assignee='worker', execution_authority='Fixture launch')
    calls = []
    def launch(task, workspace, *, board=None):
        calls.append(task.id)
        raise ValueError('Fixture post-launch error')
    kb.dispatch_once(board, spawn_fn=launch, max_spawn=1)
    assert len(calls) == 1
