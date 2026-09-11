from pathlib import Path
import subprocess
import tempfile
import concurrent.futures
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_admission as admission


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    conn = kb.connect(path)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def repository():
    candidate = Path(__file__).resolve().parents[2]
    source = (candidate / 'venv').resolve().parent
    with tempfile.TemporaryDirectory(prefix='admission-', dir=candidate.parent) as temporary:
        target = Path(temporary) / 'Projects/github.com/BRBussy/hermes-agent'
        target.parent.mkdir(parents=True)
        subprocess.run(['git', 'clone', '--shared', '--no-checkout', str(source), str(target)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(target), 'sparse-checkout', 'set', '--no-cone', 'pyproject.toml'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(target), 'checkout'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(target), 'remote', 'set-url', 'origin',
                        'https://github.com/BRBussy/hermes-agent.git'], check=True)
        base = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()
        yield target, base


def prepare(board, tid, repository, branch=None):
    path, base = repository
    return admission.prepare(board, tid, 'BRBussy/hermes-agent', str(path), base, branch or f'test/{tid}')


def test_dependency_completion_cannot_grant_authority(board):
    parent = kb.create_task(board, title='Parent')
    child = kb.create_task(board, title='Optional observation', parents=[parent], task_scope='repository')
    kb.complete_task(board, parent)
    kb.recompute_ready(board)
    assert kb.claim_task(board, child) is None
    assert kb.get_task(board, child).current_run_id is None


def test_scratch_research_with_explicit_authority_is_valid(board):
    tid = kb.create_task(board, title='Research', task_scope='scratch', execution_authority='Approved research')
    assert kb.claim_task(board, tid)
    assert kb.claim_task(board, tid) is None


def test_preparation_preserves_card_and_requires_separate_authority(board, repository):
    tid = kb.create_task(board, title='Retained title', body='Retained scope', task_scope='repository', idempotency_key='one')
    kb.add_comment(board, tid, author='operator', body='Retained note')
    before = kb.get_task(board, tid)
    task = prepare(board, tid, repository)
    assert (task.id, task.title, task.body, task.idempotency_key) == (before.id, before.title, before.body, before.idempotency_key)
    assert task.review_required and task.workspace_kind == 'worktree'
    assert kb.claim_task(board, tid) is None
    assert prepare(board, tid, repository).workspace_path == task.workspace_path
    admission.authorise(board, tid, 'Approved isolated repository fixture')
    kb.recompute_ready(board)
    assert kb.claim_task(board, tid)
    with pytest.raises(ValueError, match='inactive'):
        prepare(board, tid, repository)


def test_unprepared_repository_rejects_authorisation(board):
    tid = kb.create_task(board, title='Repository task', task_scope='repository')
    with pytest.raises(ValueError, match='preparation'):
        admission.authorise(board, tid, 'Approval')


def test_conflicting_preparation_and_origin_are_rejected(board, repository):
    tid = kb.create_task(board, title='Repository task', task_scope='repository')
    prepare(board, tid, repository)
    with pytest.raises(ValueError, match='conflicts'):
        prepare(board, tid, repository, branch='test/other')
    path, base = repository
    with pytest.raises(ValueError, match='origin'):
        admission.prepare(board, tid, 'Other/repository', str(path), base, 'test/admission')
    second = kb.create_task(board, title='Second task', task_scope='repository')
    with pytest.raises(ValueError, match='another card'):
        prepare(board, second, repository, branch=f'test/{tid}')


def test_concurrent_claims_start_one_attempt(board, repository):
    tid = kb.create_task(board, title='Repository task', body='Perform the authorised repository concurrency fixture', task_scope='repository')
    prepare(board, tid, repository)
    admission.authorise(board, tid, 'Concurrency fixture approval')
    kb.recompute_ready(board)
    path = Path(board.execute('PRAGMA database_list').fetchone()[2])
    def attempt(_):
        conn = kb.connect(path)
        try:
            return kb.claim_task(conn, tid) is not None
        finally:
            conn.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, range(2))) == 1


def test_worker_cannot_inherit_or_grant_authority(board, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'parent')
    child = kb.create_task(board, title='Follow-up')
    assert not kb.get_task(board, child).execution_authority
    with pytest.raises(PermissionError):
        admission.authorise(board, child, 'Parent approval')
    with pytest.raises(PermissionError):
        kb.create_task(board, title='Follow-up', execution_authority='Parent approval')


def test_explicit_repository_scope_does_not_depend_on_title(board):
    tid = kb.create_task(board, title='Research', task_scope='repository', execution_authority='Approval')
    assert kb.claim_task(board, tid) is None


@pytest.mark.parametrize('route', ['cli', 'dashboard', 'tool'])
def test_creation_routes_leave_unapproved_work_inert(board, route):
    import argparse
    import json
    from hermes_cli import kanban
    from tools import kanban_tools
    if route == 'cli':
        parser = argparse.ArgumentParser()
        kanban.build_parser(parser.add_subparsers())
        args = parser.parse_args(['kanban', 'create', 'Route fixture', '--assignee', 'worker', '--json'])
        assert kanban._cmd_create(args) == 0
    elif route == 'dashboard':
        from plugins.kanban.dashboard import plugin_api
        payload = plugin_api.CreateTaskBody(title='Route fixture', assignee='worker')
        plugin_api.create_task(payload, board='default')
    else:
        result = json.loads(kanban_tools._handle_create({'title': 'Route fixture', 'assignee': 'worker'}))
        assert 'error' not in result
    rows = board.execute("SELECT id FROM tasks WHERE title = 'Route fixture'").fetchall()
    assert len(rows) == 1
    tid = rows[0][0]
    kb.recompute_ready(board)
    assert kb.claim_task(board, tid) is None
    assert kb.get_task(board, tid).status == 'todo'
