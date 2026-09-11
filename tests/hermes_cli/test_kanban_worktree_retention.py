"""Retained repository contents across task transitions and cleanup consumers."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import sys
from pathlib import Path
import subprocess
import tempfile

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_admission as admission
from hermes_cli import kanban_worktree as retention
from hermes_cli.kanban_review import latest_submission


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def fixture(tmp_path, monkeypatch, request):
    try:
        repository_revision = request.getfixturevalue('repository_revision')
    except pytest.FixtureLookupError:
        repository_revision = 'HEAD'
    candidate = Path(__file__).resolve().parents[2]
    source = (candidate / 'venv').resolve().parent
    with tempfile.TemporaryDirectory(prefix='retention-', dir=candidate.parent) as temporary:
        root = Path(temporary)
        repo = root / 'Projects/github.com/BRBussy/hermes-agent'
        repo.parent.mkdir(parents=True)
        repo.mkdir()
        git(repo, 'init')
        objects = Path(git(source, 'rev-parse', '--path-format=absolute', '--git-path', 'objects'))
        if not objects.is_absolute():
            objects = source / objects
        (repo / '.git/objects/info/alternates').write_text(str(objects) + '\n')
        shallow = source / '.git/shallow'
        if shallow.exists():
            (repo / '.git/shallow').write_bytes(shallow.read_bytes())
        base = git(source, 'rev-parse', repository_revision)
        git(repo, 'update-ref', 'refs/heads/main', base)
        git(repo, 'update-ref', 'refs/remotes/origin/main', base)
        git(repo, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(repo, 'remote', 'add', 'origin', 'https://github.com/BRBussy/hermes-agent.git')
        tracked = git(source, 'ls-tree', '-r', '--name-only', base).splitlines()
        assert tracked
        seed = 'pyproject.toml' if 'pyproject.toml' in tracked else tracked[0]
        git(repo, 'sparse-checkout', 'set', '--no-cone', seed)
        git(repo, 'read-tree', '-mu', 'HEAD')
        git(repo, 'remote', 'set-url', 'origin', 'https://github.com/BRBussy/hermes-agent.git')
        with (repo / '.git/info/exclude').open('a') as stream:
            stream.write('\n.task-evidence/\n')
        path = root / 'board.db'
        monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
        conn = kb.connect(path)
        tid = kb.create_task(conn, title='Retain reviewed work', assignee='builder', task_scope='repository',
                             body='Preserve repository deliverables through independently reviewed task lifecycle transitions.')
        task = admission.prepare(conn, tid, 'BRBussy/hermes-agent', str(repo), git(repo, 'rev-parse', 'HEAD'), f'test/{tid}')
        worktree = Path(task.workspace_path)
        (worktree / '.task-evidence').mkdir()
        (worktree / '.task-evidence/report.txt').write_text('Retained ignored evidence\n')
        admission.authorise(conn, tid, 'Authorised isolated retention regression')
        kb.recompute_ready(conn)
        yield conn, task, repo
        conn.close()


def manifest(task):
    path = Path(task.workspace_path)
    files = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in path.rglob('*') if p.is_file() and p.name != '.git'}
    assert files and '.task-evidence/report.txt' in files
    result = {'id': task.id, 'repository': task.repository_identity, 'base': task.approved_base,
            'branch': git(path, 'branch', '--show-current'), 'path': str(path),
            'git_dir': git(path, 'rev-parse', '--absolute-git-dir'), 'files': files}
    print('RETENTION_MANIFEST ' + json.dumps(result, sort_keys=True))
    return result


def submit(conn, task, session, *, tests_run=None):
    run = kb.claim_task(conn, task.id)
    assert run
    assert kb.request_review(conn, task.id, summary='Ready for independent review', reviewer='reviewer',
                             metadata={'worker_session_id': session, 'tests_run': tests_run}, expected_run_id=run.current_run_id)
    review = kb.claim_review_task(conn, task.id)
    assert review
    return review


def approve(conn, task, review, session='review-final'):
    state = latest_submission(conn, task.id)['state']
    return kb.complete_task(conn, task.id, summary='Independently verified',
                            expected_run_id=review.current_run_id,
                            metadata={'worker_session_id': session, 'review_outcome': 'approved',
                                      'reviewer_checks': ['Read source and checked manifest'],
                                      'reviewed_state_id': state['id']})


def test_two_corrections_block_restart_complete_and_reopen(fixture):
    conn, task, repo = fixture
    expected = manifest(task)
    receipts = []
    for cycle in range(2):
        review = submit(conn, task, f'developer-{cycle}')
        receipts.append(latest_submission(conn, task.id)['state']['id'])
        assert manifest(task) == expected
        assert kb.request_changes(conn, task.id, reason=f'Correct case {cycle}', expected_run_id=review.current_run_id) == (True, 'builder')
        assert manifest(task) == expected
        (Path(task.workspace_path) / 'correction.txt').write_text(f'Correction {cycle}\n')
        expected = manifest(task)
        with kb.connect(Path(conn.execute('PRAGMA database_list').fetchone()[2])) as restarted:
            assert manifest(kb.get_task(restarted, task.id)) == expected
            assert restarted.execute('SELECT count(*) FROM task_runs WHERE task_id = ? AND ended_at IS NULL', (task.id,)).fetchone()[0] == 0
    assert receipts[0] != receipts[1]
    run = kb.claim_task(conn, task.id)
    assert kb.block_task(conn, task.id, reason='Await fixture input', expected_run_id=run.current_run_id)
    assert kb.unblock_task(conn, task.id)
    assert manifest(task) == expected
    review = submit(conn, task, 'developer-final')
    assert approve(conn, task, review)
    assert manifest(task) == expected
    from plugins.kanban.dashboard.plugin_api import _set_status_direct
    assert _set_status_direct(conn, task.id, 'ready')
    assert not kb.get_task(conn, task.id).execution_authority
    assert kb.claim_task(conn, task.id) is None
    admission.authorise(conn, task.id, 'Authorised same-card continuation')
    review = submit(conn, task, 'developer-reopened')
    assert approve(conn, task, review, 'review-reopened')
    assert manifest(task) == expected
    assert len(kb.list_runs(conn, task.id)) >= 9


def test_changed_contents_reject_stale_review(fixture):
    conn, task, repo = fixture
    review = submit(conn, task, 'developer')
    path = Path(task.workspace_path) / 'changed.txt'
    path.write_text('Changed after review submission\n')
    assert not approve(conn, task, review)
    path.unlink()
    assert approve(conn, task, review)


def test_registered_workspace_never_recreated(fixture):
    conn, task, repo = fixture
    path = Path(task.workspace_path)
    displaced = path.with_name(path.name + '-saved')
    path.rename(displaced)
    try:
        with pytest.raises(ValueError, match='persistent worktree'):
            kb._resolve_worktree_workspace(task)
        assert not path.exists()
        assert kb.claim_task(conn, task.id) is None
    finally:
        displaced.rename(path)
    admission.validate_repository(task)
    assert kb._resolve_worktree_workspace(task)[0] == path


def test_missing_contents_rejected_and_restored(fixture):
    conn, task, repo = fixture
    path = Path(task.workspace_path)
    tracked = [path / name for name in git(path, 'ls-files', '-z').split('\0') if name and (path / name).is_file()]
    assert tracked
    saved = {p: p.read_bytes() for p in tracked}
    for p in saved:
        p.unlink()
    try:
        with pytest.raises(ValueError, match='contents are missing'):
            admission.validate_repository(task)
    finally:
        for p, data in saved.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    admission.validate_repository(task)


@pytest.mark.parametrize('dirty', [False, True])
def test_all_cleanup_consumers_preserve_manifest(fixture, dirty, monkeypatch):
    conn, task, repo = fixture
    import cli
    monkeypatch.setattr(cli, "_repo_is_shallow", lambda path: False)
    from hermes_cli.worktree_gc import TreeRecord, reclaim_worktrees
    git(task.workspace_path, 'worktree', 'unlock', task.workspace_path)
    assert not (kb._git_dir(Path(task.workspace_path)) / 'locked').exists()
    if dirty:
        (Path(task.workspace_path) / 'unfinished.txt').write_text('Unpublished work\n')
    expected = manifest(task)
    assert bool(git(task.workspace_path, 'status', '--porcelain')) == dirty
    record = TreeRecord(Path(task.workspace_path).name, task.workspace_path, task.branch_name, 100, None, 'reap', 'Stale audit')
    review = submit(conn, task, 'developer-cleanup')
    assert approve(conn, task, review)
    child = kb.create_task(conn, title='Child', parents=[task.id], execution_authority='Isolated child')
    assert kb.complete_task(conn, child)
    assert kb.archive_task(conn, task.id)
    consumers = [
        lambda: kb._cleanup_workspace(conn, task.id),
        lambda: kb._try_cleanup_parent_workspaces(conn, child),
        lambda: kb._cleanup_worktree_workspace(task.id, task.workspace_path, task.branch_name),
        lambda: cli._cleanup_failed_worktree_add(str(repo), Path(task.workspace_path), task.branch_name),
        lambda: cli._cleanup_worktree({'path': task.workspace_path, 'branch': task.branch_name, 'repo_root': str(repo)}),
        lambda: cli._prune_stale_worktrees(str(repo), max_age_hours=0),
        lambda: reclaim_worktrees(str(repo), records=[record]),
    ]
    assert consumers
    for consumer in consumers:
        consumer()
        assert manifest(task) == expected
    from hermes_cli.kanban import _cmd_gc
    from argparse import Namespace
    assert _cmd_gc(Namespace(event_retention_days=0, log_retention_days=0)) == 0
    assert manifest(task) == expected
    assert kb.list_events(conn, task.id)
    with pytest.raises(ValueError, match='explicit disposal'):
        kb.delete_task(conn, task.id)
    with pytest.raises(ValueError, match='explicit disposal'):
        kb.delete_archived_task(conn, task.id)


def test_existing_lock_and_stale_reclaim_record(fixture):
    conn, task, repo = fixture
    import cli
    from hermes_cli.worktree_gc import TreeRecord, reclaim_worktrees
    path = Path(task.workspace_path)
    git(path, 'worktree', 'unlock', str(path))
    git(path, 'worktree', 'lock', '--reason', 'Operator retained evidence', str(path))
    before = (kb._git_dir(path) / 'locked').read_bytes()
    admission.prepare(conn, task.id, 'BRBussy/hermes-agent', str(repo), task.approved_base, task.branch_name)
    assert (kb._git_dir(path) / 'locked').read_bytes() == before
    assert cli._worktree_lock_is_live(str(repo), str(path)) == 'live'
    expected = manifest(task)
    record = TreeRecord(path.name, str(path), task.branch_name, 100, None, 'reap', 'Stale audit')
    assert reclaim_worktrees(str(repo), records=[record]) == [f'kept {path.name} (retained or locked)']
    assert manifest(task) == expected


def test_registered_custom_path_and_unreadable_ownership_are_retained(fixture):
    conn, task, repo = fixture
    custom = repo / '.worktrees/custom-path'
    kb._ensure_git_worktree(repo, custom, 'test/custom')
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET workspace_path = ? WHERE id = ?', (str(custom), task.id))
    assert retention.is_retained(custom)
    assert not retention.is_retained(repo / 'unowned')
    conn.execute('ALTER TABLE tasks RENAME TO unreadable_tasks')
    try:
        assert retention.is_retained(repo / 'unowned')
    finally:
        conn.execute('ALTER TABLE unreadable_tasks RENAME TO tasks')
    assert not retention.is_retained(repo / 'unowned')


def test_concurrent_claim_cleanup_and_live_transition(fixture):
    conn, task, repo = fixture
    expected = manifest(task)
    database = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    def attempt(_):
        with kb.connect(database) as other:
            kb._cleanup_workspace(other, task.id)
            return kb.claim_task(other, task.id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(attempt, range(4)))
    assert len([a for a in attempts if a]) == 1
    assert manifest(task) == expected
    assert not kb.request_review(conn, task.id, summary='Unowned transition')
    assert kb.get_task(conn, task.id).current_run_id == next(a for a in attempts if a).current_run_id
    assert manifest(task) == expected


def test_live_worker_keeps_workspace_and_prevents_second_attempt(fixture):
    conn, task, repo = fixture
    from hermes_cli import kanban_worker as worker
    run = kb.claim_task(conn, task.id)
    expected = manifest(task)
    env = dict(os.environ, HERMES_KANBAN_TASK=task.id, HERMES_KANBAN_RUN_ID=str(run.current_run_id),
               HERMES_KANBAN_CLAIM_LOCK=run.claim_lock)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], env=env, cwd=task.workspace_path)
    try:
        kb._set_worker_pid(conn, task.id, process.pid)
        assert worker.active_worker_exists(conn, task.id)
        assert kb.request_review(conn, task.id, summary='Ready', reviewer='reviewer',
                                 metadata={'worker_session_id': 'live-developer'}, expected_run_id=run.current_run_id)
        kb._cleanup_workspace(conn, task.id)
        assert kb.claim_review_task(conn, task.id) is None
        assert process.poll() is None
        assert manifest(task) == expected
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert not worker.active_worker_exists(conn, task.id)
    assert kb.claim_review_task(conn, task.id)
    assert manifest(task) == expected


def test_board_removal_preserves_registered_owner(fixture, monkeypatch):
    conn, task, repo = fixture
    original = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    board_dir = original.parent / 'named-board'
    board_dir.mkdir()
    target = board_dir / 'kanban.db'
    with kb.connect(target) as other:
        conn.backup(other)
    monkeypatch.setattr(kb, 'board_dir', lambda slug: board_dir)
    for archive in (True, False):
        with pytest.raises(ValueError, match='retained task ownership'):
            kb.remove_board('retention-fixture', archive=archive)
        assert target.is_file()
    assert manifest(task)


def test_cleanup_preserves_main_and_non_git_paths(fixture, tmp_path):
    conn, task, repo = fixture
    before = (repo / 'pyproject.toml').read_bytes()
    kb._cleanup_worktree_workspace(task.id, str(repo))
    assert (repo / 'pyproject.toml').read_bytes() == before
    plain = tmp_path / 'plain'
    plain.mkdir()
    (plain / 'evidence.txt').write_text('Retained data')
    kb._cleanup_worktree_workspace(task.id, str(plain))
    assert (plain / 'evidence.txt').read_text() == 'Retained data'


def test_cleanup_preserves_missing_remote_baseline(fixture):
    conn, task, repo = fixture
    expected = manifest(task)
    refs = git(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes').splitlines()
    assert refs
    for ref in refs:
        git(repo, 'update-ref', '-d', ref)
    assert not git(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes')
    kb._cleanup_worktree_workspace(task.id, task.workspace_path, task.branch_name)
    assert manifest(task) == expected


def test_disposal_refusal_is_actionable_in_cli_and_dashboard(fixture, capsys):
    from argparse import Namespace
    from fastapi import HTTPException
    from hermes_cli.kanban import _cmd_archive
    from plugins.kanban.dashboard.plugin_api import delete_task
    conn, task, repo = fixture
    expected = manifest(task)
    assert kb.archive_task(conn, task.id)
    assert _cmd_archive(Namespace(task_ids=[], purge_ids=[task.id])) == 1
    assert 'explicit disposal' in capsys.readouterr().err
    with pytest.raises(HTTPException) as rejected:
        delete_task(task.id, board=None)
    assert rejected.value.status_code == 409
    assert 'explicit disposal' in rejected.value.detail
    assert kb.get_task(conn, task.id)
    assert manifest(task) == expected


@pytest.mark.parametrize('reason', ['Operator retained evidence', 'kanban retained task t_foreign', 'foreign hermes pid=99999999'])
def test_session_exit_preserves_foreign_locks_without_board_visibility(fixture, reason):
    conn, task, repo = fixture
    import cli
    path = repo / '.worktrees/custom-other-profile'
    kb._ensure_git_worktree(repo, path, 'test/other-profile')
    git(path, 'worktree', 'lock', '--reason', reason, str(path))
    file = path / 'pyproject.toml'
    expected = file.read_bytes()
    assert expected
    assert not retention.is_retained(path)
    cli._cleanup_failed_worktree_add(str(repo), path, 'test/other-profile')
    assert file.read_bytes() == expected
    cli._cleanup_worktree({'path': str(path), 'branch': 'test/other-profile', 'repo_root': str(repo)})
    assert file.read_bytes() == expected
    assert (kb._git_dir(path) / 'locked').read_text().strip() == reason


def test_session_exit_can_release_its_own_non_task_worktree(fixture):
    conn, task, repo = fixture
    import cli
    path = repo / '.worktrees/session-fixture'
    kb._ensure_git_worktree(repo, path, 'test/session-fixture')
    git(path, 'worktree', 'lock', '--reason', f'hermes pid={os.getpid()}', str(path))
    assert not retention.is_retained(path)
    assert (path / 'pyproject.toml').is_file()
    cli._cleanup_worktree({'path': str(path), 'branch': 'test/session-fixture', 'repo_root': str(repo)})
    assert not path.exists()


def test_scratch_parent_cannot_erase_retained_repository_child(fixture, monkeypatch):
    conn, task, repo = fixture
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACES_ROOT', str(repo.parent))
    assert kb._is_managed_scratch_path(repo)
    parent = kb.create_task(conn, title='Scratch parent containing retained child',
                            workspace_kind='scratch', workspace_path=str(repo),
                            execution_authority='Isolated parent cleanup fixture')
    kb.link_tasks(conn, parent, task.id)
    expected = manifest(task)
    assert retention.is_retained(repo)
    assert kb.complete_task(conn, parent, summary='Parent complete')
    assert manifest(task) == expected
    assert kb.archive_task(conn, task.id)
    assert manifest(task) == expected
    kb._cleanup_workspace(conn, parent)
    assert manifest(task) == expected
