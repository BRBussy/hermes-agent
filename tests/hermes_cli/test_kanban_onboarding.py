"""Board preparation and repository-set boundaries on isolated account state."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_admission as admission
from hermes_cli import kanban_workspace_set as sets
from hermes_cli.kanban_review import capture_state
from hermes_cli.kanban_publication import same_content


@pytest.fixture(scope="module")
def repository_sources():
    candidate = Path(__file__).resolve().parents[2]
    source = (candidate / 'venv').resolve().parent
    tmp_path = Path(tempfile.mkdtemp(prefix='onboarding-repositories-', dir=candidate.parent))
    result = []
    for name in ('sdk', 'consumer'):
        root = tmp_path / 'Projects/github.com/Fixture' / name
        root.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(['git', 'clone', '--shared', '--no-checkout', str(source), str(root)], capture_output=True, text=True, timeout=90)
        assert clone.returncode == 0, clone.stderr
        subprocess.run(['git', '-C', str(root), 'sparse-checkout', 'set', '--no-cone', 'pyproject.toml'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(root), 'checkout'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(root), 'remote', 'set-url', 'origin', f'https://github.com/Fixture/{name}.git'], check=True)
        result.append({'repository': f'Fixture/{name}', 'path': str(root),
                       'base': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
                       'branch': f'codex/{name}'})
    yield result
    shutil.rmtree(tmp_path)


@pytest.fixture
def repos(repository_sources):
    suffix = uuid.uuid4().hex[:8]
    return [{**repo, 'branch': repo['branch'] + '-' + suffix} for repo in repository_sources]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile'))
    monkeypatch.setenv('HERMES_KANBAN_ROOT', str(tmp_path / 'profile'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    conn = kb.connect(board='default')
    yield conn
    conn.close()


@pytest.fixture
def api(board):
    source = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('onboarding_api_test', source / 'plugins/kanban/dashboard/plugin_api.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def manifest(repos):
    return {'repositories': repos, 'package_manager': 'yarn@4.9.2',
            'links': [{'consumer': 'Fixture/consumer', 'dependency': 'Fixture/sdk', 'package': '@fixture/sdk'}],
            'test_commands': ['node test.cjs']}


def prepared(board, repos):
    tid = kb.create_task(board, title='SDK change', body='Change SDK and consumer, test both and require independent review',
                         task_scope='repository', assignee='developer', idempotency_key='sdk-set-v1')
    return sets.prepare(board, tid, manifest(repos))


def test_duplicate_board_preserves_metadata(api, tmp_path):
    payload = {'slug': 'project', 'name': 'Project', 'default_workdir': str(tmp_path)}
    first = api.post('/boards', json=payload)
    assert first.status_code == 200
    assert api.post('/boards', json=payload).json()['board'] == first.json()['board']
    assert api.post('/boards', json={**payload, 'name': 'Other'}).status_code == 400
    assert kb.read_board_metadata('project')['name'] == 'Project'


def test_missing_and_unwritable_directories(api, tmp_path):
    assert api.post('/boards', json={'slug': 'absent', 'default_workdir': str(tmp_path / 'missing')}).status_code == 400
    directory = tmp_path / 'readonly'
    directory.mkdir(mode=0o500)
    try:
        assert api.post('/boards', json={'slug': 'readonly', 'default_workdir': str(directory)}).status_code == 400
    finally:
        directory.chmod(0o700)


def test_blank_board_and_incomplete_todo_remain_inert(api, board):
    assert api.post('/boards', json={'slug': 'blank'}).json()['board']['default_workspace_kind'] == 'scratch'
    response = api.post('/tasks', json={'title': 'Draft', 'assignee': 'worker'})
    assert response.status_code == 200, response.text
    tid = response.json()['task']['id']
    assert kb.get_task(board, tid).status == 'todo'
    kb.recompute_ready(board)
    assert kb.claim_task(board, tid) is None
    assert board.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0


def test_api_preserves_body_branch_base_review_and_inherited_model(api, board, repos):
    repo = repos[0]
    payload = dict(title='Complete draft', body='First paragraph\n\nSecond paragraph', assignee='worker',
                   workspace_kind='worktree', workspace_path=repo['path'], repository=repo['repository'],
                   approved_base=repo['base'], branch_name=repo['branch'], review_required=True, idempotency_key='complete-v1')
    response = api.post('/tasks', json=payload)
    assert response.status_code == 200, response.text
    task = response.json()['task']
    assert task['body'] == payload['body']
    assert task['branch_name'] == repo['branch'] and task['approved_base'] == repo['base']
    assert task['review_required'] is True and task['idempotency_key'] == payload['idempotency_key']
    assert task['model_override'] is None and task['reasoning_effort'] is None
    assert kb.claim_task(board, task['id']) is None


def test_failed_api_preparation_retains_identifiable_draft(api, board, repos):
    response = api.post('/tasks', json={'title': 'Recoverable', 'repository': repos[0]['repository'], 'idempotency_key': 'recover'})
    assert response.status_code == 400
    tid = response.json()['detail']['task_id']
    assert kb.get_task(board, tid).execution_authority is None
    repo = repos[0]
    response = api.post(f'/tasks/{tid}/prepare', json={'repository': repo['repository'], 'repository_path': repo['path'],
                                                    'base': repo['base'], 'branch': repo['branch']})
    assert response.status_code == 200, response.text
    assert response.json()['task']['id'] == tid


def test_set_preparation_is_repeatable_and_separates_cards(board, repos):
    task = prepared(board, repos)
    before = capture_state(task)
    assert len(before['repositories']) == 2
    assert sets.prepare(board, task.id, manifest(repos)).workspace_set == task.workspace_set
    assert kb.claim_task(board, task.id) is None
    second_id = kb.create_task(board, title='Concurrent task', body='Separate changes', task_scope='repository')
    with pytest.raises(ValueError, match='another card'):
        sets.prepare(board, second_id, manifest(repos))
    second_repos = [{**repo, 'branch': repo['branch'] + '-second'} for repo in repos]
    second = sets.prepare(board, second_id, manifest(second_repos))
    assert {m.workspace_path for m in sets.members(task)}.isdisjoint({m.workspace_path for m in sets.members(second)})
    assert capture_state(task) == before


def test_second_repository_change_invalidates_review_and_publication(board, repos):
    task = prepared(board, repos)
    before = capture_state(task)
    member = sets.members(task)[1]
    Path(member.workspace_path, 'change.txt').write_text('New consumer behaviour\n')
    after = capture_state(task)
    assert after['id'] != before['id']
    assert not same_content(before, after)
    assert before['repositories'][sets.members(task)[0].repository_identity] == after['repositories'][sets.members(task)[0].repository_identity]


def test_wrong_remote_and_empty_set_are_rejected(board, repos):
    tid = kb.create_task(board, title='Invalid scope', task_scope='repository')
    with pytest.raises(ValueError, match='at least one'):
        sets.prepare(board, tid, {'repositories': []})
    wrong = [{**repos[0], 'repository': 'Other/repository'}, repos[1]]
    with pytest.raises(ValueError, match='origin'):
        sets.prepare(board, tid, manifest(wrong))
    assert kb.get_task(board, tid).workspace_set is None


def test_interrupted_preparation_resumes_without_resetting_first_tree(board, repos):
    tid = kb.create_task(board, title='Interrupted set', task_scope='repository')
    ensure = kb._ensure_git_worktree
    def interrupt(root, target, branch, **kwargs):
        if root == Path(repos[1]['path']):
            raise OSError('Injected interruption')
        ensure(root, target, branch, **kwargs)
        (target / 'retained.txt').write_text('Preserve this edit\n')
    with patch.object(kb, '_ensure_git_worktree', interrupt), pytest.raises(OSError, match='interruption'):
        sets.prepare(board, tid, manifest(repos))
    task = kb.get_task(board, tid)
    assert task.workspace_set['state'] == 'preparing'
    assert kb.claim_task(board, tid) is None
    with pytest.raises(ValueError, match='incomplete'):
        admission.authorise(board, tid, 'Fixture authority')
    task = sets.prepare(board, tid, manifest(repos))
    assert Path(sets.members(task)[0].workspace_path, 'retained.txt').read_text() == 'Preserve this edit\n'


def test_sandbox_roots_require_current_claim(board, repos):
    task = prepared(board, repos)
    database = board.execute('PRAGMA database_list').fetchone()[2]
    env = {'HERMES_KANBAN_DB': database, 'HERMES_KANBAN_TASK': task.id}
    with pytest.raises(ValueError, match='current task claim'):
        sets.writable_roots(env)
    admission.authorise(board, task.id, 'Fixture execution authority')
    kb.recompute_ready(board)
    claimed = kb.claim_task(board, task.id)
    assert claimed
    env.update(HERMES_KANBAN_RUN_ID=str(claimed.current_run_id), HERMES_KANBAN_CLAIM_LOCK=claimed.claim_lock)
    assert sets.writable_roots(env) == [m.workspace_path for m in sets.members(task)]


def submit_set(board, task):
    admission.authorise(board, task.id, 'Isolated review fixture execution')
    kb.recompute_ready(board)
    developer = kb.claim_task(board, task.id)
    assert developer
    assert kb.request_review(board, task.id, reviewer='reviewer', summary='Review both repositories',
                             metadata={'worker_session_id': 'fixture-developer'}, expected_run_id=developer.current_run_id)
    reviewer = kb.claim_review_task(board, task.id)
    assert reviewer
    from hermes_cli.kanban_review import latest_submission
    state = latest_submission(board, task.id)['state']
    metadata = {'worker_session_id': 'fixture-reviewer', 'review_outcome': 'approved',
                'reviewed_state_id': state['id'], 'reviewer_checks': ['Checked integration'],
                'repository_checks': {identity: {'reviewed_state_id': receipt['id'], 'checks': ['Inspected member files']}
                                      for identity, receipt in state['repositories'].items()}}
    return reviewer, metadata


def test_independent_review_must_cover_each_member(board, repos):
    task = prepared(board, repos)
    reviewer, metadata = submit_set(board, task)
    partial = dict(metadata, repository_checks=dict(list(metadata['repository_checks'].items())[:1]))
    assert not kb.complete_task(board, task.id, summary='Incomplete coverage', metadata=partial,
                                expected_run_id=reviewer.current_run_id)
    assert kb.get_task(board, task.id).current_run_id == reviewer.current_run_id
    assert kb.complete_task(board, task.id, summary='Both members reviewed', metadata=metadata,
                            expected_run_id=reviewer.current_run_id)
    assert all(Path(member.workspace_path).exists() for member in sets.members(task))


def test_partial_publication_recovery_preserves_per_repository_actions(board, repos, monkeypatch):
    from hermes_cli import kanban_recovery as recovery
    task = prepared(board, repos)
    reviewer, metadata = submit_set(board, task)
    metadata.update(review_outcome='accepted_pending_publication', publication={'deliverable': 'branch'})
    assert kb.complete_task(board, task.id, summary='Content accepted for both members', metadata=metadata,
                            expected_run_id=reviewer.current_run_id)
    published = set()
    def observe(member):
        head = admission._git(member.workspace_path, 'rev-parse', 'HEAD')
        return {'repository': member.repository_identity.removeprefix('github.com/'), 'head': head,
                'remote_head': head if member.repository_identity in published else None,
                'retained_changes': False, 'pull_requests': [], 'workspace': member.workspace_path,
                'branch': member.branch_name}
    monkeypatch.setattr(recovery, 'observe_publication', observe)
    identities = [member.repository_identity for member in sets.members(task)]
    with pytest.raises(ValueError, match='every repository'):
        admission.authorise(board, task.id, 'Incomplete publication plan', publication_actions={identities[0]: ['push']})
    plan = {identity: ['commit', 'push'] for identity in identities}
    admission.authorise(board, task.id, 'Synthetic publication fixture authority', publication_actions=plan)
    worker = kb.claim_task(board, task.id)
    assert worker
    published.add(identities[0])
    kb._record_task_failure(board, task.id, 'Injected interruption between repository publications',
                            outcome='crashed', release_claim=True, end_run=True)
    result = recovery.reconcile(board, task.id)
    assert set(result['repositories']) == set(identities)
    recovered = kb.get_task(board, task.id)
    assert recovered.publication['repositories'][identities[0]]['actions'] == []
    assert recovered.publication['repositories'][identities[1]]['actions'] == ['push']
    assert all(Path(member.workspace_path).exists() for member in sets.members(recovered))
    admission.authorise(board, task.id, 'Resume only outstanding fixture publication',
                        publication_actions={identities[0]: ['verify'], identities[1]: ['push']})
    resumed = kb.claim_task(board, task.id)
    assert resumed
    assert resumed.publication['repositories'][identities[0]]['actions'] == ['verify']
    assert resumed.publication['repositories'][identities[1]]['actions'] == ['push']


def test_imported_workspace_set_requires_fresh_preparation(board, repos):
    from hermes_cli.kanban_transfer import _relocate_imported_rows
    task = prepared(board, repos)
    _relocate_imported_rows(board, 'default')
    imported = kb.get_task(board, task.id)
    assert imported.workspace_set is None
    assert imported.execution_authority is None and imported.repository_identity is None
    assert kb.claim_task(board, task.id) is None
    assert any(event.kind == 'workspace_set_imported' for event in kb.list_events(board, task.id))


def test_concurrent_idempotent_creation_keeps_one_card(board):
    from concurrent.futures import ThreadPoolExecutor
    database = board.execute('PRAGMA database_list').fetchone()[2]
    def create(_):
        conn = kb.connect(Path(database))
        try:
            return kb.create_task(conn, title='Same draft', idempotency_key='concurrent-draft')
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert len(set(ids)) == 1
    assert board.execute('SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?', ('concurrent-draft',)).fetchone()[0] == 1


def test_api_idempotency_cannot_change_or_authorise_retained_draft(api):
    payload = {'title': 'Retained draft', 'body': 'Original requirements', 'idempotency_key': 'api-retry'}
    first = api.post('/tasks', json=payload)
    assert first.status_code == 200
    tid = first.json()['task']['id']
    assert api.post('/tasks', json=payload).json()['task']['id'] == tid
    conflict = api.post('/tasks', json=dict(payload, body='Different requirements', execution_authority='Unrelated authority'))
    assert conflict.status_code == 409 and conflict.json()['detail']['task_id'] == tid
    task = api.get('/tasks/' + tid).json()['task']
    assert task['body'] == payload['body'] and task['execution_authority'] is None


def test_requirement_revision_preserves_identity_and_revokes_authority(board, api):
    tid = kb.create_task(board, title='Draft', body='First requirements', idempotency_key='revise-draft',
                         execution_authority='First scope approval')
    response = api.patch('/tasks/' + tid, json={'body': 'Complete revised requirements\nSecond line'})
    assert response.status_code == 200
    task = response.json()['task']
    assert task['idempotency_key'] == 'revise-draft' and task['execution_authority'] is None
    assert task['body'] == 'Complete revised requirements\nSecond line'
    assert kb.claim_task(board, tid) is None
    admission.authorise(board, tid, 'Revised scope approval')
    claimed = kb.claim_task(board, tid)
    assert claimed
    assert api.patch('/tasks/' + tid, json={'body': 'Unreviewed scope'}).status_code == 409
    assert kb.get_task(board, tid).body == task['body']


def test_prepared_repository_without_description_cannot_execute(board, repos):
    task = prepared(board, repos)
    kb.revise_task_requirements(board, task.id, body='')
    with pytest.raises(ValueError, match='complete task description'):
        admission.authorise(board, task.id, 'Approval cannot supply missing requirements')
    assert kb.claim_task(board, task.id) is None
    for invalid in [None, {}, 3]:
        malformed = manifest([{**repos[0], 'path': invalid}, repos[1]])
        with pytest.raises(ValueError, match='non-empty strings'):
            sets.specification(task.id, malformed)
