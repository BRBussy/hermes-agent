"""Isolated disposal acceptance using existing Git objects and private boards."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_admission as admission
from hermes_cli import kanban_disposal as disposal
from hermes_cli import kanban_evidence as evidence


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    candidate = Path(__file__).resolve().parents[2]
    source = (candidate / 'venv').resolve().parent
    with tempfile.TemporaryDirectory(prefix='disposal-', dir=candidate.parent) as temporary:
        root = Path(temporary)
        monkeypatch.setenv('HERMES_KANBAN_HOME', str(root / 'profile'))
        monkeypatch.setenv('HERMES_KANBAN_ATTACHMENTS_ROOT', str(root / 'attachments'))
        repo = root / 'repository'
        repo.mkdir()
        git(repo, 'init')
        objects = git(source, 'rev-parse', '--path-format=absolute', '--git-path', 'objects')
        (repo / '.git/objects/info/alternates').write_text(objects + '\n')
        head = git(source, 'rev-parse', 'HEAD')
        git(repo, 'update-ref', 'refs/heads/main', head)
        git(repo, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(repo, 'remote', 'add', 'origin', 'https://github.com/BRBussy/hermes-agent.git')
        git(repo, 'sparse-checkout', 'set', '--no-cone', 'pyproject.toml')
        git(repo, 'read-tree', '-mu', 'HEAD')
        (repo / '.git/info/exclude').write_text('.task-evidence/\n')
        remote = root / 'remote.git'
        git(root, 'clone', '--bare', str(repo), str(remote))
        original_git = disposal._git
        def fixture_git(path, *args):
            if args[0] == 'ls-remote':
                return original_git(path, *args[:3], str(remote), *args[4:])
            return original_git(path, *args)
        monkeypatch.setattr(disposal, '_git', fixture_git)
        database = root / 'board.db'
        monkeypatch.setenv('HERMES_KANBAN_DB', str(database))
        conn = kb.connect(database)
        tid = kb.create_task(conn, title='Explicit disposal fixture', assignee='builder', task_scope='repository')
        task = admission.prepare(conn, tid, 'BRBussy/hermes-agent', str(repo), head, 'test/' + tid)
        path = Path(task.workspace_path)
        (path / '.task-evidence').mkdir()
        (path / '.task-evidence/report.txt').write_text('Verified fixture evidence\n')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        task = kb.get_task(conn, tid)
        yield conn, task, repo, remote, head
        conn.close()


def retain(conn, task):
    with kb.write_txn(conn):
        evidence.preserve(conn, task.id, {'evidence_paths': ['.task-evidence']}, 'checkpoint')


def run(fixture, **overrides):
    conn, task, repo, remote, head = fixture
    args = dict(request_id='request-1', authority='Authorised isolated fixture disposal',
                expected_head=head, remote='origin', ref='refs/heads/main')
    args.update(overrides)
    return disposal.dispose(conn, task.id, **args)


def assert_refused(fixture, result):
    conn, task, repo, remote, head = fixture
    assert result['outcome'] in {'refused', 'failed'}, result
    assert Path(task.workspace_path, '.task-evidence/report.txt').read_text() == 'Verified fixture evidence\n'
    assert git(repo, 'rev-parse', task.branch_name) == head
    assert disposal.inspect(conn, task.id)['receipts']


def test_authorised_disposal_and_retry(fixture):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    result = run(fixture)
    assert result['outcome'] == 'removed', result
    assert not Path(task.workspace_path).exists()
    assert task.workspace_path not in git(repo, 'worktree', 'list', '--porcelain')
    assert git(repo, 'rev-parse', task.branch_name) == head
    assert Path(result['recovery']['workspace'], '.task-evidence/report.txt').is_file()
    assert Path(result['recovery']['git_dir'], 'index').is_file()
    assert run(fixture) == result
    assert disposal.inspect(conn, task.id)['state'] == 'removed'
    assert run(fixture, request_id='another')['outcome'] == 'refused'


@pytest.mark.parametrize('case', ['authority', 'ignored', 'dirty', 'untracked', 'lock', 'active', 'remote-absent', 'remote-failure', 'remote-stale', 'head', 'shallow', 'ambiguous', 'artifact-corrupt', 'symlink', 'child', 'wildcard'])
def test_refusals(fixture, case):
    conn, task, repo, remote, head = fixture
    path = Path(task.workspace_path)
    if case != 'ignored':
        retain(conn, task)
    args = {}
    if case == 'authority':
        args['authority'] = ''
    elif case == 'dirty':
        (path / 'pyproject.toml').write_text('changed\n')
    elif case == 'wildcard':
        (path / '*').write_text('Unclassified wildcard filename')
    elif case == 'untracked':
        (path / 'unclassified.txt').write_text('valuable\n')
    elif case == 'lock':
        git(repo, 'worktree', 'unlock', str(path))
        git(repo, 'worktree', 'lock', '--reason', 'User retention lock', str(path))
    elif case == 'active':
        conn.execute("UPDATE tasks SET status = 'running', claim_lock = 'active' WHERE id = ?", (task.id,))
    elif case == 'remote-absent':
        git(remote, 'update-ref', '-d', 'refs/heads/main')
    elif case == 'remote-stale':
        git(repo, 'update-ref', 'refs/remotes/origin/main', head)
        git(remote, 'update-ref', '-d', 'refs/heads/main')
    elif case == 'remote-failure':
        remote.rename(remote.with_name('inaccessible.git'))
    elif case == 'head':
        args['expected_head'] = '0' * 40
    elif case == 'shallow':
        (repo / '.git/shallow').write_text(head + '\n')
    elif case == 'ambiguous':
        git(repo, 'config', '--add', 'remote.origin.url', 'https://github.com/other/repo.git')
    elif case == 'artifact-corrupt':
        found = evidence.discover(conn, task.id)[0]
        blob = Path(found['path']).parent / found['manifest']['artifacts'][0]['storage_name']
        blob.write_text('corruption')
    elif case == 'symlink':
        (path / 'link').symlink_to('/tmp')
    elif case == 'child':
        child = kb.create_task(conn, title='Active child')
        kb.link_tasks(conn, task.id, child)
    assert_refused(fixture, run(fixture, **args))


def test_extra_remote_requires_explicit_selection(fixture):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    git(repo, 'remote', 'add', 'other', 'https://github.com/other/repo.git')
    assert_refused(fixture, run(fixture, remote='other'))
    assert run(fixture, request_id='right-remote')['outcome'] == 'removed'


def test_changed_evidence_during_checks(fixture, monkeypatch):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    original = disposal._remote
    calls = []
    def change(*args):
        calls.append(1)
        result = original(*args)
        if len(calls) == 2:
            Path(task.workspace_path, 'late.txt').write_text('late evidence')
        return result
    monkeypatch.setattr(disposal, '_remote', change)
    assert_refused(fixture, run(fixture))
    assert calls


def test_failure_after_first_rename_retains_bytes_and_stops_retry(fixture, monkeypatch):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    rename = os.rename
    calls = []
    def fail(source, destination):
        calls.append(str(source))
        if len(calls) == 2:
            raise OSError('Injected administration retirement failure')
        return rename(source, destination)
    monkeypatch.setattr(disposal.os, 'rename', fail)
    result = run(fixture)
    assert result['outcome'] == 'unknown', result
    assert Path(result['recovery']['workspace'], '.task-evidence/report.txt').is_file()
    assert git(repo, 'rev-parse', task.branch_name) == head
    assert run(fixture) == result
    assert len(calls) == 2


def test_interruption_preserves_intent(fixture, monkeypatch):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(disposal.os, 'rename', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(fixture)
    assert run(fixture)['outcome'] == 'unknown'
    assert disposal.inspect(conn, task.id)['receipts'][0]['outcome'] == 'unknown'
    assert Path(task.workspace_path).is_dir()


def test_open_descriptor_late_write_is_preserved(fixture, monkeypatch):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    rename = os.rename
    report = Path(task.workspace_path, '.task-evidence/report.txt')
    with report.open('a') as stream:
        def change(source, destination):
            rename(source, destination)
            if str(source) == task.workspace_path:
                stream.write('Late open-descriptor output\n')
                stream.flush()
        monkeypatch.setattr(disposal.os, 'rename', change)
        result = run(fixture)
    assert result['outcome'] == 'unknown', result
    assert 'Late open-descriptor output' in Path(result['recovery']['workspace'], '.task-evidence/report.txt').read_text()


@pytest.mark.parametrize('predicate', ['remote', 'ignored'])
def test_shared_predicates_fail_closed(fixture, predicate):
    import cli
    conn, task, repo, remote, head = fixture
    assert not git(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes')
    if predicate == 'remote':
        assert cli._worktree_has_unpushed_commits(task.workspace_path)
    else:
        assert cli._worktree_is_dirty(task.workspace_path)


def test_parent_completion_preserves_workspace(fixture):
    conn, task, repo, remote, head = fixture
    child = kb.create_task(conn, title='Completed child')
    kb.link_tasks(conn, task.id, child)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (child,))
    kb._try_cleanup_parent_workspaces(conn, child)
    assert Path(task.workspace_path, '.task-evidence/report.txt').is_file()


def test_dry_run_retains_and_records(fixture):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    result = run(fixture, dry_run=True)
    assert result['outcome'] == 'retained'
    assert Path(task.workspace_path).is_dir()
    assert disposal.inspect(conn, task.id)['receipts'][0]['outcome'] == 'retained'


def test_recreated_path_is_exposed(fixture):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    assert run(fixture)['outcome'] == 'removed'
    Path(task.workspace_path).mkdir()
    assert disposal.inspect(conn, task.id)['state'] == 'unexpected_replacement'


def test_first_rename_failure_keeps_branch(fixture, monkeypatch):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    def fail(*args):
        raise PermissionError('Injected rename refusal')
    monkeypatch.setattr(disposal.os, 'rename', fail)
    result = run(fixture)
    assert result['outcome'] == 'unknown'
    assert Path(task.workspace_path).is_dir()
    assert git(repo, 'rev-parse', task.branch_name) == head


def test_cli_parser_and_detail(fixture, capsys):
    from hermes_cli import kanban
    import argparse
    conn, task, repo, remote, head = fixture
    result = run(fixture, authority='')
    assert result['outcome'] == 'refused'
    args = argparse.Namespace(task_id=task.id, json=True, board=None, tenant=None)
    assert kanban._cmd_show(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['disposal']['receipts'][0]['outcome'] == 'refused'


def test_live_worker_on_terminal_card_is_refused(fixture):
    import sys
    import time
    from hermes_cli.kanban_worker import process_identity
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    conn.execute("UPDATE tasks SET status = 'backlog' WHERE id = ?", (task.id,))
    admission.authorise(conn, task.id, 'Authorised fixture attempt')
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task.id,))
    claimed = kb.claim_task(conn, task.id)
    assert claimed and claimed.current_run_id
    env = {**os.environ, 'HERMES_KANBAN_TASK': task.id, 'HERMES_KANBAN_RUN_ID': str(claimed.current_run_id),
           'HERMES_KANBAN_CLAIM_LOCK': claimed.claim_lock}
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], env=env)
    try:
        identity = process_identity(process.pid, task.id, claimed.current_run_id, claimed.claim_lock)
        assert identity
        conn.execute("INSERT OR REPLACE INTO worker_attempts (run_id, task_id, claim_lock, identity, startup_state, launched_at) VALUES (?, ?, ?, ?, 'initialised', ?)",
                     (claimed.current_run_id, task.id, claimed.claim_lock, json.dumps(identity), int(time.time())))
        conn.execute('UPDATE task_runs SET ended_at = ? WHERE id = ?', (int(time.time()), claimed.current_run_id))
        conn.execute("UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?", (task.id,))
        assert_refused(fixture, run(fixture))
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_database_claim_cannot_race_disposal(fixture, monkeypatch):
    import sqlite3
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    original = disposal._remote
    checked = []
    def race(*args):
        with sqlite3.connect(database, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute("UPDATE tasks SET claim_lock = 'racer' WHERE id = ?", (task.id,))
        checked.append(True)
        return original(*args)
    monkeypatch.setattr(disposal, '_remote', race)
    assert run(fixture)['outcome'] == 'removed'
    assert checked


def test_branch_reclaim_preserves_retirement_anchor(fixture):
    from hermes_cli.worktree_gc import reclaim_branches, BranchRecord
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    assert run(fixture)['outcome'] == 'removed'
    records = [BranchRecord(task.branch_name, 'delete', 'stale audit')]
    assert records
    assert 'kept' in reclaim_branches(str(repo), records=records)[0]
    assert git(repo, 'rev-parse', task.branch_name) == head


@pytest.mark.parametrize('consumer', ['session', 'failed-add'])
def test_session_removal_failure_keeps_branch(fixture, monkeypatch, consumer):
    import cli
    conn, task, repo, remote, head = fixture
    session = repo / '.worktrees/session-fixture'
    git(repo, 'worktree', 'add', '-b', 'session-fixture', str(session), head)
    git(repo, 'update-ref', 'refs/remotes/origin/main', head)
    actual_run = subprocess.run
    attempted = []
    def refuse(args, **kwargs):
        if args[:3] == ['git', 'worktree', 'remove']:
            attempted.append(args)
            return subprocess.CompletedProcess(args, 1, '', 'Injected removal failure')
        return actual_run(args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', refuse)
    if consumer == 'session':
        cli._cleanup_worktree({'path': str(session), 'branch': 'session-fixture', 'repo_root': str(repo)})
    else:
        cli._cleanup_failed_worktree_add(str(repo), session, 'session-fixture')
    assert attempted
    assert session.is_dir()
    assert git(repo, 'rev-parse', 'session-fixture') == head


@pytest.mark.parametrize('consumer', ['subagent', 'failed-add'])
def test_subagent_ignored_evidence_is_retained(fixture, consumer):
    from tools.subagent_worktree import finalize_subagent_worktree
    conn, task, repo, remote, head = fixture
    session = repo / '.worktrees/child-fixture'
    git(repo, 'worktree', 'add', '-b', 'child-fixture', str(session), head)
    (session / '.task-evidence').mkdir()
    report = session / '.task-evidence/report.txt'
    report.write_text('Child evidence')
    if consumer == 'subagent':
        result = finalize_subagent_worktree({'path': str(session), 'branch': 'child-fixture',
                                            'repo_root': str(repo), 'base_commit': head})
        assert result['dirty'] and not result['pruned']
    else:
        import cli
        cli._cleanup_failed_worktree_add(str(repo), session, 'child-fixture')
    assert report.read_text() == 'Child evidence'


def test_complete_recovery_restores_git_identity(fixture):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    result = run(fixture)
    assert result['outcome'] == 'removed'
    original_admin = Path(result['checks']['identity']['git_dir'])
    assert not original_admin.exists() and not Path(task.workspace_path).exists()
    os.rename(result['recovery']['git_dir'], original_admin)
    os.rename(result['recovery']['workspace'], task.workspace_path)
    admission.validate_repository(kb.get_task(conn, task.id))
    assert git(task.workspace_path, 'rev-parse', 'HEAD') == head
    assert Path(task.workspace_path, '.task-evidence/report.txt').read_text() == 'Verified fixture evidence\n'
    assert (original_admin / 'locked').read_text().strip() == 'kanban retained task ' + task.id


def test_web_removal_refuses_unlocked_task(fixture):
    from hermes_cli.web_git import worktree_remove
    conn, task, repo, remote, head = fixture
    git(repo, 'worktree', 'unlock', task.workspace_path)
    with pytest.raises(ValueError, match='Kanban disposal'):
        worktree_remove(str(repo), task.workspace_path, True)
    assert Path(task.workspace_path, '.task-evidence/report.txt').is_file()


@pytest.mark.parametrize('change', ['index', 'lock'])
def test_git_administration_change_during_checks(fixture, monkeypatch, change):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    original = disposal._remote
    calls = []
    def mutate(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 2:
            if change == 'index':
                blob = git(repo, 'rev-parse', head + ':pyproject.toml')
                git(task.workspace_path, 'update-index', '--cacheinfo', '100755,' + blob + ',pyproject.toml')
            else:
                (kb._git_dir(Path(task.workspace_path)) / 'locked').write_text('Concurrent user lock\n')
        return result
    monkeypatch.setattr(disposal, '_remote', mutate)
    assert_refused(fixture, run(fixture))
    assert len(calls) == 2


def test_preflight_io_failure_is_recorded(fixture, monkeypatch):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    def fail(*args):
        raise OSError('Injected inventory read failure')
    monkeypatch.setattr(disposal, '_inventory', fail)
    result = run(fixture)
    assert result['outcome'] == 'failed'
    assert_refused(fixture, result)


def test_result_write_failure_after_retirement_requires_reconciliation(fixture, monkeypatch):
    conn, task, repo, remote, head = fixture
    retain(conn, task)
    save = disposal._save
    def fail(directory, name, receipt):
        if name.endswith('-result.json'):
            raise OSError('Injected result publication failure')
        return save(directory, name, receipt)
    monkeypatch.setattr(disposal, '_save', fail)
    with pytest.raises(OSError, match='result publication'):
        run(fixture)
    assert not Path(task.workspace_path).exists()
    receipt = disposal.inspect(conn, task.id)['receipts'][0]
    assert receipt['outcome'] == 'unknown'
    assert Path(receipt['recovery']['workspace'], '.task-evidence/report.txt').is_file()
    assert git(repo, 'rev-parse', task.branch_name) == head
    assert run(fixture)['outcome'] == 'unknown'
