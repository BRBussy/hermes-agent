import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_recovery as recovery
from hermes_cli import kanban_admission as admission
from tests.hermes_cli.test_kanban_worker_identity import claim, spawn, stop
from tests.hermes_cli.test_kanban_task_admission import board, repository, prepare


def test_durable_exit_status_and_clean_exit_classification(board):
    task = claim(board)
    proc = spawn(board, task)
    observer = threading.Thread(target=kb._observe_worker_exit,
                                args=(proc, board.execute('PRAGMA database_list').fetchone()[2], task.current_run_id))
    observer.start()
    proc.terminate()
    observer.join(timeout=10)
    assert not observer.is_alive()
    evidence = recovery.diagnostics(board, task.id)
    assert evidence and evidence[0]['exit_kind'] == 'signaled'
    assert evidence[0]['exit_code'] == 15
    board.execute('UPDATE tasks SET started_at = 1 WHERE id = ?', (task.id,))
    board.commit()
    assert kb.detect_crashed_workers(board) == [task.id]
    assert recovery.diagnostics(board, task.id)[0]['outcome'] == 'crashed'


def test_stall_warning_is_once_per_gap_and_never_kills(board):
    task = claim(board)
    proc = spawn(board, task)
    try:
        board.execute('UPDATE worker_attempts SET initialised_at = 1, launched_at = 1 WHERE run_id = ?', (task.current_run_id,))
        board.commit()
        assert recovery.warn_stalled(board) == [task.id]
        assert recovery.warn_stalled(board) == []
        assert kb.detect_stale_running(board, stale_timeout_seconds=1) == []
        assert proc.poll() is None
        assert kb.heartbeat_worker(board, task.id, expected_run_id=task.current_run_id)
        assert recovery.warn_stalled(board) == []
        assert recovery.diagnostics(board, task.id)[0]['last_activity_at']
    finally:
        stop(proc)


def failed_repository(board, repository):
    tid = kb.create_task(board, title='Retain work', task_scope='repository')
    task = prepare(board, tid, repository)
    admission.authorise(board, tid, 'Isolated implementation approval')
    kb.recompute_ready(board)
    claimed = kb.claim_task(board, tid)
    assert claimed
    change = Path(task.workspace_path) / 'retained.txt'
    change.write_text('Retained implementation\n')
    kb._record_task_failure(board, tid, 'Fixture process exit', outcome='crashed', release_claim=True, end_run=True)
    assert not kb.get_task(board, tid).execution_authority
    return tid, change


def remote_receipts(monkeypatch, task, prs):
    actual_run = subprocess.run
    calls = []
    head = admission._git(task.workspace_path, 'rev-parse', 'HEAD')
    def run(argv, **kwargs):
        if argv[0] == 'gh':
            calls.append(argv)
            data = {'check_runs': []} if '/check-runs?' in argv[-1] else [] if '/statuses?' in argv[-1] else prs(head)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data))
        if 'ls-remote' in argv:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=head + '\trefs/heads/' + task.branch_name + '\n')
        return actual_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    return calls


def test_recovery_preserves_changes_and_does_not_repeat_publication(board, repository, monkeypatch):
    tid, change = failed_repository(board, repository)
    task = kb.get_task(board, tid)
    assert recovery.required(board, task)
    with pytest.raises(ValueError, match='Reconcile'):
        admission.authorise(board, tid, 'Resume')
    calls = remote_receipts(monkeypatch, task, lambda head: [
        {'number': 17, 'url': 'https://github.com/BRBussy/hermes-agent/pull/17', 'headRefOid': head, 'state': 'OPEN'}])
    observed = recovery.reconcile(board, tid)
    assert observed['pull_requests'][0]['number'] == 17
    assert observed['retained_changes']
    assert calls
    assert any('ls-remote' in argv for argv in calls)
    assert any(argv[:3] == ['gh', 'pr', 'list'] for argv in calls)
    assert observed['pull_requests'][0]['ci']['state'] == 'absent'
    assert all('push' not in argv and 'commit' not in argv and 'create' not in argv for argv in calls)
    admission.authorise(board, tid, 'Resume verification using existing publication receipts')
    kb.recompute_ready(board)
    assert kb.claim_task(board, tid)
    assert kb.claim_task(board, tid) is None
    assert change.read_text() == 'Retained implementation\n'
    context = kb.build_worker_context(board, tid)
    assert 'pull/17' in context and 'Do not repeat them' in context


def test_unknown_remote_outcome_stays_inert(board, repository, monkeypatch):
    tid, change = failed_repository(board, repository)
    task = kb.get_task(board, tid)
    remote_receipts(monkeypatch, task, lambda head: [])
    actual = admission._git
    def fail(path, *args):
        if 'ls-remote' in args:
            raise subprocess.CalledProcessError(128, ['git', 'ls-remote'])
        return actual(path, *args)
    monkeypatch.setattr(admission, '_git', fail)
    with pytest.raises(subprocess.CalledProcessError):
        recovery.reconcile(board, tid)
    assert recovery.required(board, task)
    assert kb.claim_task(board, tid) is None
    assert change.exists()


def test_reviewer_phase_is_retained_on_failure(board, repository):
    tid = kb.create_task(board, title='Review handoff', task_scope='repository')
    prepare(board, tid, repository)
    admission.authorise(board, tid, 'Fixture review approval')
    board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    board.commit()
    claimed = kb.claim_review_task(board, tid)
    assert claimed
    assert kb._retry_status_for_run(board, tid) == 'review'
    kb._record_task_failure(board, tid, 'Reviewer exited', outcome='crashed', release_claim=True, end_run=True)
    assert kb.get_task(board, tid).status == 'review'
    assert not kb.get_task(board, tid).execution_authority


def test_transport_loss_is_distinct_from_process_death(board, monkeypatch):
    import io
    from types import SimpleNamespace
    from agent.transports.codex_app_server import CodexAppServerClient
    task = claim(board)
    proc = spawn(board, task)
    try:
        monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
        client = object.__new__(CodexAppServerClient)
        client._proc = SimpleNamespace(stdout=io.BytesIO(b''))
        client._closed = False
        client._stderr_lock = threading.Lock()
        client._stderr_lines = []
        client._read_stdout()
        observed = recovery.diagnostics(board, task.id)[0]
        assert observed['runtime_failure'] == 'transport_lost'
        assert observed['exit_kind'] is None
        assert proc.poll() is None
    finally:
        stop(proc)
