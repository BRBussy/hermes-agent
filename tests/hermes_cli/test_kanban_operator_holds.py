import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_worker_identity import board, claim, spawn, stop


def create(conn, **kwargs):
    return kb.create_task(conn, title='Harmless hold fixture', body='Keep this exact scope',
                          execution_authority='Isolated hold acceptance', assignee='worker', **kwargs)


def test_initial_hold_survives_ticks(board):
    tid = create(board, initial_status='blocked')
    for _ in range(4):
        kb.recompute_ready(board)
        assert kb.get_task(board, tid).status == 'blocked'
        assert kb.claim_task(board, tid) is None
    assert kb.get_task(board, tid).body == 'Keep this exact scope'
    assert board.execute('SELECT count(*) FROM task_runs WHERE task_id = ?', (tid,)).fetchone()[0] == 0
    assert kb.unblock_task(board, tid)
    assert not kb.unblock_task(board, tid)
    assert kb.claim_task(board, tid)
    assert kb.claim_task(board, tid) is None


def test_missing_history_does_not_authorise_promotion(board):
    tid = create(board, initial_status='blocked')
    board.execute('DELETE FROM task_events WHERE task_id = ?', (tid,))
    board.commit()
    assert kb.recompute_ready(board) == 0
    assert kb.get_task(board, tid).status == 'blocked'


def test_dependency_wait_and_worker_hold_are_distinct(board):
    parent = create(board)
    child = create(board, parents=[parent])
    held = create(board, parents=[parent], initial_status='blocked')
    worker = kb.claim_task(board, parent)
    assert worker
    assert kb.block_task(board, parent, reason='Operator input', expected_run_id=worker.current_run_id)
    for _ in range(3):
        assert kb.recompute_ready(board) == 0
    assert kb.unblock_task(board, parent)
    assert kb.complete_task(board, parent)
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == 'ready'
    assert kb.get_task(board, held).status == 'blocked'


@pytest.mark.parametrize('operation', ['schedule', 'block', 'reclaim'])
def test_operator_transition_stops_running_process(board, operation):
    task = claim(board)
    proc = spawn(board, task)
    try:
        fn = getattr(kb, operation + '_task')
        assert fn(board, task.id, reason='Harmless operator interruption')
        assert proc.wait(timeout=3) is not None
        assert not kb.block_task(board, task.id, expected_run_id=task.current_run_id)
        assert not kb.heartbeat_worker(board, task.id, expected_run_id=task.current_run_id)
        current = kb.get_task(board, task.id)
        assert current.current_run_id is None
        if operation != 'reclaim':
            for _ in range(3):
                assert kb.recompute_ready(board) == 0
            assert kb.unblock_task(board, task.id)
            assert not kb.unblock_task(board, task.id)
        resumed = kb.claim_task(board, task.id)
        assert resumed and resumed.current_run_id != task.current_run_id
        assert kb.claim_task(board, task.id) is None
        assert not kb.block_task(board, task.id, expected_run_id=task.current_run_id)
    finally:
        stop(proc)


def test_reclaim_refuses_failed_termination(board, monkeypatch):
    task = claim(board)
    proc = spawn(board, task)
    try:
        monkeypatch.setattr(kb.time, 'sleep', lambda _: None)
        assert not kb.reclaim_task(board, task.id, signal_fn=lambda *_: None)
        current = kb.get_task(board, task.id)
        assert current.current_run_id == task.current_run_id
        assert current.claim_lock == task.claim_lock
        assert proc.poll() is None
    finally:
        stop(proc)


@pytest.mark.parametrize('operation', ['schedule', 'block', 'reclaim'])
def test_unverified_launch_cannot_be_released(board, operation):
    task = claim(board)
    assert not getattr(kb, operation + '_task')(board, task.id)
    assert kb.get_task(board, task.id).current_run_id == task.current_run_id


def test_cooperative_block_waits_for_worker_exit_before_resume(board):
    task = claim(board)
    proc = spawn(board, task)
    try:
        assert kb.block_task(board, task.id, expected_run_id=task.current_run_id)
        assert not kb.unblock_task(board, task.id)
        assert proc.poll() is None
    finally:
        stop(proc)
    assert kb.unblock_task(board, task.id)


@pytest.mark.parametrize('status', ['blocked', 'scheduled'])
def test_review_pause_resumes_review(board, status):
    tid = create(board)
    board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    board.commit()
    assert getattr(kb, 'block_task' if status == 'blocked' else 'schedule_task')(board, tid)
    assert kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).status == 'review'
    task = kb.claim_review_task(board, tid)
    assert task
    proc = spawn(board, task)
    try:
        assert getattr(kb, 'block_task' if status == 'blocked' else 'schedule_task')(board, tid)
        proc.wait(timeout=3)
        assert kb.unblock_task(board, tid)
        assert kb.get_task(board, tid).status == 'review'
    finally:
        stop(proc)


def test_repeated_operator_holds_do_not_trigger_triage(board):
    tid = create(board)
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 2):
        assert kb.block_task(board, tid, reason='Hold scope for operator approval')
        assert kb.get_task(board, tid).status == 'blocked'
        assert kb.unblock_task(board, tid)


def test_initial_hold_has_durable_provenance_after_reconnect(board):
    tid = create(board, initial_status='blocked')
    events = kb.list_events(board, tid)
    assert events
    assert any(e.kind == 'blocked' and e.payload['origin'] == 'initial_status' for e in events)
    path = board.execute('PRAGMA database_list').fetchone()[2]
    with kb.connect(Path(path)) as restarted:
        assert kb.recompute_ready(restarted) == 0
        assert kb.get_task(restarted, tid).status == 'blocked'


def test_gateway_dispatch_ticks_start_exactly_one_resumed_worker(board):
    tid = create(board, initial_status='blocked')
    processes = []
    def launch(task, *_args, **_kwargs):
        proc = spawn(board, task)
        processes.append(proc)
        return proc.pid
    try:
        for _ in range(3):
            result = kb.dispatch_once(board, spawn_fn=launch)
            assert not result.spawned
        assert not processes
        assert kb.unblock_task(board, tid)
        assert not kb.unblock_task(board, tid)
        for _ in range(3):
            kb.dispatch_once(board, spawn_fn=launch)
        assert len(processes) == 1
        assert len(kb.list_runs(board, tid)) == 1
        assert kb.get_task(board, tid).body == 'Keep this exact scope'
    finally:
        for proc in processes:
            stop(proc)


@pytest.mark.parametrize('operation', ['block', 'schedule', 'reclaim'])
def test_cli_interruption_and_resumption(board, operation):
    from hermes_cli import kanban as cli
    task = claim(board)
    proc = spawn(board, task)
    try:
        output = cli.run_slash(f'{operation} {task.id}')
        assert 'cannot' not in output.lower()
        proc.wait(timeout=3)
        if operation != 'reclaim':
            output = cli.run_slash(f'unblock {task.id}')
            assert 'cannot' not in output.lower()
        assert kb.claim_task(board, task.id)
        assert kb.claim_task(board, task.id) is None
    finally:
        stop(proc)


@pytest.mark.parametrize('status', ['blocked', 'scheduled', 'todo', 'ready', 'triage'])
@pytest.mark.parametrize('bulk', [False, True])
def test_dashboard_interrupts_before_status_write(board, status, bulk):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    app = FastAPI()
    app.include_router(router)
    task = claim(board)
    proc = spawn(board, task)
    try:
        with TestClient(app) as client:
            response = (client.post('/tasks/bulk', json={'ids': [task.id], 'status': status})
                        if bulk else client.patch('/tasks/' + task.id, json={'status': status}))
        assert response.status_code == 200, response.text
        assert kb.get_task(board, task.id).status == status, response.text
        proc.wait(timeout=3)
        assert not kb.block_task(board, task.id, expected_run_id=task.current_run_id)
    finally:
        stop(proc)


def test_stale_run_target_cannot_reclaim_replacement(board):
    first = claim(board)
    assert kb.block_task(board, first.id, expected_run_id=first.current_run_id)
    assert kb.unblock_task(board, first.id)
    second = kb.claim_task(board, first.id)
    proc = spawn(board, second)
    try:
        assert not kb.reclaim_task(board, first.id, expected_run_id=first.current_run_id)
        assert kb.get_task(board, first.id).current_run_id == second.current_run_id
        assert proc.poll() is None
    finally:
        stop(proc)


@pytest.mark.parametrize('mutate', ['host', 'identity', 'pid'])
def test_unobservable_identity_refuses_pause(board, mutate):
    task = claim(board)
    proc = spawn(board, task)
    try:
        row = board.execute('SELECT identity FROM worker_attempts WHERE run_id = ?', (task.current_run_id,)).fetchone()
        identity = json.loads(row[0])
        if mutate == 'host':
            identity['host'] = 'unobservable-fixture-host'
        elif mutate == 'pid':
            identity['pid'] += 1000000
        board.execute('UPDATE worker_attempts SET identity = ? WHERE run_id = ?',
                      (None if mutate == 'identity' else json.dumps(identity), task.current_run_id))
        board.commit()
        assert not kb.schedule_task(board, task.id)
        assert kb.get_task(board, task.id).current_run_id == task.current_run_id
        assert proc.poll() is None
    finally:
        stop(proc)


def test_cli_initial_block_is_not_an_admission_grant(board):
    from hermes_cli import kanban as cli
    result = json.loads(cli.run_slash('create "CLI held scope" --initial-status blocked --execution-authority "Fixture only" --json'))
    tid = result['id']
    for _ in range(3):
        kb.recompute_ready(board)
        assert kb.get_task(board, tid).status == 'blocked'
    assert len(kb.list_runs(board, tid)) == 0


@pytest.mark.parametrize('handler', ['_handle_block', '_handle_complete', '_handle_request_review', '_handle_heartbeat'])
def test_worker_tools_reject_late_lifecycle_writes(board, monkeypatch, handler):
    from tools import kanban_tools
    task = claim(board)
    proc = spawn(board, task)
    try:
        with monkeypatch.context() as worker_env:
            worker_env.setenv('HERMES_KANBAN_TASK', task.id)
            worker_env.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
            live = json.loads(kanban_tools._handle_heartbeat({}))
            assert live['ok'], live
        assert kb.schedule_task(board, task.id)
        proc.wait(timeout=3)
        assert kb.unblock_task(board, task.id)
        replacement = kb.claim_task(board, task.id)
        with monkeypatch.context() as worker_env:
            worker_env.setenv('HERMES_KANBAN_TASK', task.id)
            worker_env.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
            result = json.loads(getattr(kanban_tools, handler)({'reason': 'Late write', 'summary': 'Late write'}))
            assert not result.get('ok'), result
        assert kb.get_task(board, task.id).current_run_id == replacement.current_run_id
        assert kb.get_task(board, task.id).status == 'running'
    finally:
        stop(proc)


def test_typed_worker_dependency_wait_releases_after_parent_completion(board):
    worker = claim(board)
    parent = create(board)
    kb.link_tasks(board, parent_id=parent, child_id=worker.id)
    assert kb.block_task(board, worker.id, kind='dependency', expected_run_id=worker.current_run_id)
    assert kb.get_task(board, worker.id).status == 'todo'
    assert kb.recompute_ready(board) == 0
    assert kb.complete_task(board, parent)
    kb.recompute_ready(board)
    assert kb.get_task(board, worker.id).status == 'ready'


def test_worker_block_recurrence_still_escalates(board):
    tid = create(board)
    for i in range(kb.BLOCK_RECURRENCE_LIMIT):
        worker = kb.claim_task(board, tid)
        assert worker
        assert kb.block_task(board, tid, kind='capability', expected_run_id=worker.current_run_id)
        if i < kb.BLOCK_RECURRENCE_LIMIT - 1:
            assert kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).status == 'blocked'
    assert not kb.unblock_task(board, tid)


# Negative-case cleanup must signal the captured child after it is reparented.
@pytest.mark.live_system_guard_bypass
def test_operator_pause_stops_child_process_tree(board, tmp_path):
    import os
    import subprocess
    import sys
    import time
    import psutil

    task = claim(board)
    marker = tmp_path / 'child-pid'
    code = '''import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)
marker = Path(sys.argv[1])
staged = marker.with_suffix('.tmp')
staged.write_text(str(child.pid))
os.replace(staged, marker)
time.sleep(300)
'''
    proc = subprocess.Popen([sys.executable, '-c', code, str(marker)],
                            env={**os.environ, 'HERMES_KANBAN_TASK': task.id,
                                 'HERMES_KANBAN_RUN_ID': str(task.current_run_id),
                                 'HERMES_KANBAN_CLAIM_LOCK': task.claim_lock}, start_new_session=True)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        child = psutil.Process(int(marker.read_text()))
        assert child.is_running()
        kb._set_worker_pid(board, task.id, proc.pid)
        assert kb.schedule_task(board, task.id)
        proc.wait(timeout=3)
        try:
            assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
        run = kb.get_run(board, task.current_run_id)
        assert run.metadata['operator_termination']['descendants_exit_verified']
        assert run.metadata['operator_termination']['descendant_count'] >= 1
    finally:
        if child is not None:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
