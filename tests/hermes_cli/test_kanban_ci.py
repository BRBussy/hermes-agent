"""CI policies over retained worktrees, actual lifecycle calls and controlled GitHub reads."""

import json
import subprocess

import pytest

from hermes_cli import config, kanban_ci as ci, kanban_db as kb, kanban_admission as admission
from hermes_cli.kanban_recovery import observe_publication
from tests.hermes_cli.test_kanban_publication import fixture, remote, accept, publish_receipt
from tests.hermes_cli.test_kanban_worktree_retention import submit, git


@pytest.fixture
def repository_revision():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'venv').resolve().parent
    revisions = git(source, 'rev-list', '--reverse', 'HEAD').splitlines()
    assert len(revisions) >= 2
    return revisions[1]


@pytest.fixture
def policy(monkeypatch):
    policy = dict(approval='Fixture approved policy', branch_rules='Additional floor to fixture branch rules',
                  required_checks=[dict(kind='check_run', name='unit', app_id=42)], allow_no_ci=False)
    policies = {'brbussy/hermes-agent': {'main': policy}}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'kanban': {'repository_ci_policies': policies}})
    return policies, policy


@pytest.fixture
def handoff(fixture, remote, policy):
    conn, task, repo = fixture
    review = submit(conn, task, 'fixture-developer', tests_run=['Fixture reports retained-file verification'])
    assert accept(conn, task, review)
    publish_receipt(task, remote)
    remote['runs'] = [dict(id=1, name='unit', app={'id': 42}, head_sha=remote['head'],
                           status='completed', conclusion='success')]
    admission.authorise(conn, task.id, 'Fixture merge authority', publication_actions=['merge'])
    return conn, task, remote


def test_passing_preflight_is_retained_and_completion_uses_it(handoff):
    conn, task, remote = handoff
    result = ci.preflight(conn, task.id)
    assert result['ready']
    assert result['worker_verification']['state'] == 'reported'
    assert result['github_ci']['checks'][0]['state'] == 'successful'
    assert result['merge_authority']['reference'] == 'Fixture merge authority'
    assert result['scope'] == dict(repository='brbussy/hermes-agent', pr=7, head=remote['head'], target_branch='main')
    assert kb.get_task(conn, task.id).publication['merge_preflight'] == result
    assert any(e.kind == 'merge_preflight' and e.payload == result for e in kb.list_events(conn, task.id))
    publish_receipt(task, remote, state='MERGED')
    reviewer = submit(conn, task, 'fixture-publisher', tests_run=['Fixture post-publication check'])
    assert accept(conn, task, reviewer, outcome='approved', session='fixture-final-reviewer')
    assert kb.latest_run(conn, task.id).metadata['merge_evidence']['github_ci']['state'] == 'reported'
    assert kb.get_task(conn, task.id).publication['merge_evidence'] == kb.latest_run(conn, task.id).metadata['merge_evidence']


@pytest.mark.parametrize('state,raw', [('failed', 'failure'), ('failed', 'cancelled'), ('pending', 'in_progress'),
                                     ('skipped', 'skipped'), ('unavailable', 'neutral')])
def test_required_non_success_needs_decision(handoff, state, raw):
    conn, task, remote = handoff
    remote['runs'][0].update(conclusion=raw, status='in_progress' if raw == 'in_progress' else 'completed')
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and result['decision_points']
    assert result['github_ci']['checks'][0]['state'] == state
    assert kb.claim_task(conn, task.id) is None


@pytest.mark.parametrize('unavailable', [False, True])
def test_absent_and_unavailable_are_distinct(handoff, unavailable):
    conn, task, remote = handoff
    remote['runs'] = []
    remote['unavailable'] = unavailable
    result = ci.preflight(conn, task.id)
    assert not result['ready']
    assert result['github_ci']['state'] == ('unavailable' if unavailable else 'absent')
    assert result['worker_verification']['state'] == 'reported'


def test_approved_no_ci_reports_absence(handoff, policy):
    conn, task, remote = handoff
    policy[1].update(required_checks=[], allow_no_ci=True)
    remote['runs'] = []
    result = ci.preflight(conn, task.id)
    assert result['ready'] and result['github_ci']['state'] == 'absent'
    remote['unavailable'] = True
    assert not ci.preflight(conn, task.id)['ready']


@pytest.mark.parametrize('scope', ['repository', 'branch', 'invalid'])
def test_missing_and_invalid_policy_are_decisions(handoff, policy, scope):
    conn, task, remote = handoff
    if scope == 'repository':
        policy[0]['another/repository'] = policy[0].pop('brbussy/hermes-agent')
    elif scope == 'branch':
        remote['prs'][0]['baseRefName'] = 'release'
    else:
        policy[1]['approval'] = ''
    result = ci.preflight(conn, task.id)
    assert not result['ready']
    assert 'policy_' + ('invalid' if scope == 'invalid' else 'missing') in result['decision_points']


def test_different_repository_requirements(policy):
    policy[0]['another/repository'] = {'main': dict(policy[1], required_checks=[dict(kind='status', name='lint')])}
    assert ci.policy_for('another/repository', 'main')['value']['required_checks'] == [dict(kind='status', name='lint')]
    assert ci.policy_for('brbussy/hermes-agent', 'main')['value']['required_checks'][0]['name'] == 'unit'


def test_skipped_requires_explicit_policy(handoff, policy):
    conn, task, remote = handoff
    remote['runs'][0]['conclusion'] = 'skipped'
    assert not ci.preflight(conn, task.id)['ready']
    policy[1]['required_checks'][0]['allow_skipped'] = True
    assert ci.preflight(conn, task.id)['ready']


@pytest.mark.parametrize('key', ['repository', 'pr', 'head', 'target_branch'])
def test_stale_check_receipt_is_rejected(handoff, key):
    conn, task, remote = handoff
    observed = observe_publication(task)
    observed['pull_requests'][0]['ci'][key] = 'stale'
    result = ci.report(conn, kb.get_task(conn, task.id), observed)
    assert not result['ready'] and 'stale_check_receipt' in result['decision_points']


def test_wrong_run_head_and_app_cannot_satisfy_requirement(handoff):
    conn, task, remote = handoff
    remote['runs'][0]['head_sha'] = '0' * 40
    assert ci.preflight(conn, task.id)['github_ci']['state'] == 'unavailable'
    remote['runs'][0].update(head_sha=remote['head'], app={'id': 99})
    assert not ci.preflight(conn, task.id)['ready']


@pytest.mark.parametrize('change', ['head', 'base'])
def test_pr_change_during_preflight_is_rejected(handoff, monkeypatch, change):
    conn, task, remote = handoff
    from hermes_cli import kanban_recovery
    real = kanban_recovery.observe_publication
    count = 0
    def changing(task):
        nonlocal count
        count += 1
        result = real(task)
        if count == 2:
            if change == 'head':
                result['pull_requests'][0]['head'] = '0' * 40
            else:
                result['pull_requests'][0]['target_branch'] = 'release'
        return result
    monkeypatch.setattr(kanban_recovery, 'observe_publication', changing)
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'publication_changed_during_preflight' in result['decision_points']


def test_missing_worker_report_cannot_borrow_ci_success(handoff):
    conn, task, remote = handoff
    row = ci.latest_submission(conn, task.id)
    data = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id = ?', (row['run_id'],)).fetchone()[0])
    data.pop('tests_run')
    conn.execute('UPDATE task_runs SET metadata = ? WHERE id = ?', (json.dumps(data), row['run_id']))
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'worker_verification_absent' in result['decision_points']


def test_content_change_after_review_requires_fresh_review(handoff):
    conn, task, remote = handoff
    from pathlib import Path
    Path(task.workspace_path, 'new-content.txt').write_text('Unreviewed content\n')
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'fresh_exact_state_review_required' in result['decision_points']


@pytest.mark.parametrize('invalid', [None, 'head', 'target_branch', 'pr', 'repository', 'policy_digest', 'reason'])
def test_exception_is_scoped_and_preserves_failed_ci(handoff, invalid):
    conn, task, remote = handoff
    remote['runs'][0]['conclusion'] = 'failure'
    report = ci.preflight(conn, task.id)
    exception = dict(report['scope'], policy_digest=report['policy']['digest'], ci_digest=report['ci_digest'],
                     approval='Fixture exception approval', reason='Fixture approved known failure',
                     issues=report['decision_points'])
    if invalid:
        exception[invalid] = ''
    result = ci.preflight(conn, task.id, exception=exception)
    assert result['ready'] is (invalid is None)
    assert result['github_ci']['checks'][0]['state'] == 'failed'
    assert result['exception'] == exception


def test_worker_cannot_record_exception(handoff, monkeypatch):
    conn, task, remote = handoff
    monkeypatch.setenv('HERMES_KANBAN_TASK', task.id)
    with pytest.raises(PermissionError):
        ci.preflight(conn, task.id, exception={'approval': 'Forged'})


def test_queries_unavailable_are_retained(handoff, monkeypatch):
    conn, task, remote = handoff
    from hermes_cli import kanban_recovery
    def fail(task):
        raise subprocess.CalledProcessError(1, ['gh'], stderr='Private diagnostic')
    monkeypatch.setattr(kanban_recovery, 'observe_publication', fail)
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'publication_query_unavailable' in result['decision_points']
    assert 'Private diagnostic' not in json.dumps(result)
    assert kb.get_task(conn, task.id).publication['merge_preflight'] == result


def test_merge_without_preflight_cannot_complete(handoff):
    conn, task, remote = handoff
    publish_receipt(task, remote, state='MERGED')
    reviewer = submit(conn, task, 'fixture-publisher', tests_run=['Fixture report'])
    assert not accept(conn, task, reviewer, outcome='approved', session='fixture-reviewer')
    assert 'retained approved preflight' in kb.get_task(conn, task.id).last_failure_error


def test_latest_status_wins_and_pagination_is_nonempty(monkeypatch):
    calls = []
    def query(args, **kwargs):
        calls.append(args[-1])
        if '/check-runs?' in args[-1]:
            return {'check_runs': []}
        if 'page=2' in args[-1]:
            return [dict(id=101, context='unit', state='failure')]
        return [dict(id=n, context='unit', state='success') for n in range(1, 101)]
    monkeypatch.setattr(ci, '_json_command', query)
    result = ci.collect('fixture/repository', dict(number=1, head='a'*40, target_branch='main'))
    assert len(calls) == 3 and len(result['checks']) == 1
    assert result['checks'][0]['state'] == 'failed'


def test_cli_exposes_durable_decision(handoff):
    from hermes_cli import kanban as cli
    conn, task, remote = handoff
    output = cli.run_slash(f'merge-check {task.id}')
    result = json.loads(output)
    assert result['ready']
    assert kb.get_task(conn, task.id).publication['merge_evidence'] == result


def test_head_change_after_review_invalidates_worker_and_review(handoff):
    from tests.hermes_cli.test_kanban_worktree_retention import git
    conn, task, remote = handoff
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'venv').resolve().parent
    revisions = git(source, 'rev-list', '--reverse', 'HEAD').splitlines()
    assert len(revisions) >= 3
    changed_head = revisions[2]
    assert changed_head != remote['head']
    git(task.workspace_path, 'update-ref', 'HEAD', changed_head)
    git(task.workspace_path, 'read-tree', '-mu', 'HEAD')
    publish_receipt(task, remote)
    result = ci.preflight(conn, task.id)
    assert not result['ready']
    assert 'worker_verification_stale_head' in result['decision_points']
    assert 'fresh_exact_state_review_required' in result['decision_points']


def test_retarget_after_authority_requires_scoped_approval(handoff, policy):
    conn, task, remote = handoff
    policy[0]['brbussy/hermes-agent']['release'] = dict(policy[1])
    remote['prs'][0]['baseRefName'] = 'release'
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'merge_authority_scope_mismatch' in result['decision_points']


def test_duplicate_check_names_do_not_hide_a_failure(handoff):
    conn, task, remote = handoff
    remote['runs'].append(dict(remote['runs'][0], id=2, conclusion='failure'))
    remote['runs'].append(dict(remote['runs'][0], id=3))
    result = ci.preflight(conn, task.id)
    assert not result['ready']
    assert len(result['github_ci']['checks']) == 3


def test_profile_policy_loads_from_selected_home(tmp_path, monkeypatch):
    for profile, allow_no_ci in [('main', True), ('worker', False)]:
        home = tmp_path / profile
        home.mkdir()
        monkeypatch.setenv('HERMES_HOME', str(home))
        value = dict(approval='Fixture approval', branch_rules='Fixture branch rules', allow_no_ci=allow_no_ci,
                     required_checks=[] if allow_no_ci else [dict(kind='status', name='lint')])
        (home / 'config.yaml').write_text(json.dumps({'kanban': {'repository_ci_policies': {
            'fixture/repository': {'main': value}}}}))
        result = ci.policy_for('fixture/repository', 'main')
        assert result['state'] == 'approved' and result['value'] == value


@pytest.mark.parametrize('data', [[], {}, {'check_runs': None}, {'check_runs': ['invalid']}])
def test_malformed_query_cannot_report_absent(monkeypatch, data):
    monkeypatch.setattr(ci, '_json_command', lambda args, **kwargs: data)
    result = ci.collect('fixture/repository', dict(number=1, head='a'*40, target_branch='main'))
    assert result['state'] == 'unavailable'


def test_partial_query_failure_cannot_report_pass(monkeypatch):
    calls = []
    def query(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return {'check_runs': [dict(id=1, name='unit', app={'id': 42}, head_sha='a'*40,
                                       status='completed', conclusion='success')]}
        raise subprocess.TimeoutExpired(args, 30)
    monkeypatch.setattr(ci, '_json_command', query)
    assert ci.collect('fixture/repository', dict(number=1, head='a'*40, target_branch='main'))['state'] == 'unavailable'


def test_unavailable_query_keeps_exact_identity_and_exception(handoff):
    conn, task, remote = handoff
    remote['unavailable'] = True
    first = ci.preflight(conn, task.id)
    exception = dict(first['scope'], policy_digest=first['policy']['digest'], ci_digest=first['ci_digest'], approval='Fixture query exception',
                     reason='Fixture unavailable checks accepted for this head', issues=first['decision_points'])
    result = ci.preflight(conn, task.id, exception=exception)
    assert result['ready'] and result['github_ci']['state'] == 'unavailable'


def test_branch_enforcement_cannot_be_waived(handoff):
    conn, task, remote = handoff
    remote['prs'][0]['mergeStateStatus'] = 'BLOCKED'
    first = ci.preflight(conn, task.id)
    exception = dict(first['scope'], policy_digest=first['policy']['digest'], ci_digest=first['ci_digest'], approval='Fixture invalid bypass',
                     reason='Fixture bypass attempt', issues=first['decision_points'])
    result = ci.preflight(conn, task.id, exception=exception)
    assert not result['ready'] and 'exception_scope_invalid' in result['decision_points']


def test_exception_does_not_cover_a_new_check_run(handoff):
    conn, task, remote = handoff
    remote['runs'][0]['conclusion'] = 'failure'
    first = ci.preflight(conn, task.id)
    exception = dict(first['scope'], policy_digest=first['policy']['digest'], ci_digest=first['ci_digest'],
                     approval='Fixture failure exception', reason='Known fixture failure', issues=first['decision_points'])
    assert ci.preflight(conn, task.id, exception=exception)['ready']
    remote['runs'][0]['id'] += 1
    assert not ci.preflight(conn, task.id)['ready']


def test_concurrent_task_change_keeps_new_authority(handoff, monkeypatch):
    conn, task, remote = handoff
    from hermes_cli import kanban_recovery, kanban_publication
    real = kanban_recovery.observe_publication
    count = 0
    def change(task):
        nonlocal count
        count += 1
        observed = real(task)
        if count == 2:
            publication = dict(kb.get_task(conn, task.id).publication, authority='Changed fixture authority')
            with kb.write_txn(conn):
                kanban_publication.save(conn, task.id, publication)
        return observed
    monkeypatch.setattr(kanban_recovery, 'observe_publication', change)
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'task_changed_during_preflight' in result['decision_points']
    assert kb.get_task(conn, task.id).publication['authority'] == 'Changed fixture authority'
    assert any(event.kind == 'merge_preflight' and not event.payload['ready'] for event in kb.list_events(conn, task.id))


def test_merge_authority_is_separate_from_ci(handoff):
    conn, task, remote = handoff
    from hermes_cli.kanban_publication import save
    with kb.write_txn(conn):
        save(conn, task.id, dict(kb.get_task(conn, task.id).publication, actions=['verify']))
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'merge_authority_required' in result['decision_points']
    assert result['github_ci']['checks'][0]['state'] == 'successful'


def test_check_query_budget_is_shared_and_exhaustion_is_unavailable(monkeypatch):
    times = iter([0, 1, 31])
    monkeypatch.setattr(ci.time, 'monotonic', lambda: next(times))
    queries = []
    def query(args, **kwargs):
        queries.append(kwargs['timeout'])
        return {'check_runs': []}
    monkeypatch.setattr(ci, '_json_command', query)
    result = ci.collect('fixture/repository', dict(number=1, head='a'*40, target_branch='main'))
    assert queries == [29] and result['state'] == 'unavailable'


def test_requirements_changed_during_preflight_invalidate_decision(handoff, monkeypatch):
    conn, task, remote = handoff
    from hermes_cli import kanban_recovery
    real = kanban_recovery.observe_publication
    count = 0
    def change(task):
        nonlocal count
        count += 1
        result = real(task)
        if count == 2:
            with kb.write_txn(conn):
                conn.execute('UPDATE tasks SET body = ? WHERE id = ?', ('Changed fixture requirements', task.id))
        return result
    monkeypatch.setattr(kanban_recovery, 'observe_publication', change)
    result = ci.preflight(conn, task.id)
    assert not result['ready'] and 'task_changed_during_preflight' in result['decision_points']
    assert 'merge_preflight' not in kb.get_task(conn, task.id).publication
