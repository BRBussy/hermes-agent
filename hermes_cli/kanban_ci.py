"""Repository CI policy and point-in-time merge decision receipts.

Policy supplements GitHub branch enforcement. Callers must perform a fresh
preflight and use a conditional exact-head merge without bypassing branch rules.
"""

import hashlib
import json
import os
import re
import subprocess
import time

from hermes_cli.kanban_review import capture_state, latest_submission


def _json_command(args, *, timeout=30):
    return json.loads(subprocess.run(args, check=True, capture_output=True,
                                     text=True, timeout=timeout).stdout)


def _pages(repository, head, endpoint, deadline, field=None):
    rows = []
    for page in range(1, 21):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('Check query budget exhausted')
        result = _json_command(['gh', 'api', '--method', 'GET',
                                f'repos/{repository}/commits/{head}/{endpoint}?per_page=100&page={page}'], timeout=remaining)
        if field and not isinstance(result, dict):
            raise ValueError('Malformed check response')
        items = result.get(field) if field else result
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError('Malformed check page')
        rows.extend(items)
        if len(items) < 100:
            return rows
    raise ValueError('Check pagination limit reached')


def collect(repository, pr):
    receipt = dict(repository=repository, pr=pr['number'], head=pr['head'],
                   target_branch=pr.get('target_branch'), observed_at=int(time.time()),
                   state='unavailable', checks=[])
    if not re.fullmatch(r'[0-9a-f]{40}', pr['head']):
        return receipt
    try:
        deadline = time.monotonic() + 30
        runs = _pages(repository, pr['head'], 'check-runs', deadline, 'check_runs')
        statuses = _pages(repository, pr['head'], 'statuses', deadline)
        latest = {}
        for kind, records in [('check_run', runs), ('status', statuses)]:
            for item in records:
                name = item.get('name' if kind == 'check_run' else 'context')
                identifier = item.get('id')
                if not isinstance(name, str) or not name or type(identifier) is not int:
                    raise ValueError('Incomplete check identity')
                app_id = (item.get('app') or {}).get('id') if kind == 'check_run' else None
                if kind == 'check_run' and (item.get('head_sha') != pr['head'] or type(app_id) is not int):
                    raise ValueError('Check run head or application mismatch')
                raw = item.get('conclusion') if item.get('status') == 'completed' else item.get('status')
                if kind == 'status':
                    raw = item.get('state')
                state = ('successful' if raw == 'success' else 'skipped' if raw == 'skipped' else
                         'pending' if raw in {'queued', 'in_progress', 'pending', 'waiting', 'requested'} else
                         'failed' if raw in {'failure', 'error', 'cancelled', 'timed_out', 'action_required', 'startup_failure', 'stale'} else
                         'unavailable')
                row = dict(kind=kind, name=name, app_id=app_id, id=identifier, state=state,
                           conclusion=raw, head=pr['head'])
                key = (kind, name, app_id, identifier if kind == 'check_run' else None)
                if key not in latest or identifier > latest[key]['id']:
                    latest[key] = row
        receipt.update(state='reported' if latest else 'absent', checks=list(latest.values()))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        receipt['error'] = 'Check query failed, was incomplete or returned inconsistent data'
    return receipt


def policy_for(repository, branch):
    from hermes_cli.config import load_config_readonly
    if not isinstance(branch, str) or not branch:
        return {'state': 'missing'}
    try:
        policies = load_config_readonly().get('kanban', {}).get('repository_ci_policies', {})
        policy = policies.get(repository, {}).get(branch)
        if policy is None:
            return {'state': 'missing'}
        policy = json.loads(json.dumps(policy))
        if not isinstance(policy, dict) or not all(isinstance(policy.get(key), str) and policy[key].strip()
                                                   for key in ('approval', 'branch_rules')):
            raise ValueError('Policy approval and branch-rule relationship are required')
        required = policy.get('required_checks')
        if not isinstance(required, list) or type(policy.get('allow_no_ci')) is not bool:
            raise ValueError('Explicit check requirements are required')
        if not required and not policy['allow_no_ci']:
            raise ValueError('An empty requirement set needs explicit no-CI approval')
        keys = []
        for check in required:
            if (not isinstance(check, dict) or check.get('kind') not in {'check_run', 'status'}
                    or not isinstance(check.get('name'), str) or not check['name'].strip()
                    or type(check.get('allow_skipped', False)) is not bool
                    or (check.get('app_id') is not None and
                        (check['kind'] != 'check_run' or type(check['app_id']) is not int))):
                raise ValueError('Invalid required check')
            keys.append(_check_key(check))
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate required check')
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        return dict(state='approved', digest=digest, value=policy)
    except (ValueError, TypeError, AttributeError):
        return {'state': 'invalid'}


def _check_key(check):
    return 'required:' + json.dumps([check['kind'], check['name'], check.get('app_id')], separators=(',', ':'))


def evaluate(policy, ci, scope, exception=None):
    ci_digest = hashlib.sha256(json.dumps(dict(state=ci['state'], checks=sorted(ci['checks'], key=lambda row: json.dumps(row, sort_keys=True))), sort_keys=True).encode()).hexdigest()
    issues = []
    required = []
    if policy['state'] != 'approved':
        issues.append('policy_' + policy['state'])
    if (any(ci.get(key) != value for key, value in scope.items())
            or any(row.get('head') != scope['head'] for row in ci['checks'])):
        issues.append('stale_check_receipt')
    if ci['state'] == 'unavailable':
        issues.append('ci_unavailable')
    if policy['state'] == 'approved':
        for check in policy['value']['required_checks']:
            matches = [row for row in ci['checks'] if row['kind'] == check['kind'] and row['name'] == check['name']
                       and (check.get('app_id') is None or row['app_id'] == check['app_id'])]
            allowed = {'successful', 'skipped'} if check.get('allow_skipped') else {'successful'}
            states = [row['state'] for row in matches] or ['absent']
            required.append(dict(check=check, states=states))
            if not matches or any(state not in allowed for state in states):
                issues.append(_check_key(check))
    waived = []
    if exception:
        permitted = {'ci_unavailable'} | {_check_key(row['check']) for row in required}
        if (isinstance(exception, dict) and policy['state'] == 'approved'
                and all(exception.get(key) == value for key, value in scope.items())
                and exception.get('policy_digest') == policy['digest']
                and exception.get('ci_digest') == ci_digest
                and all(isinstance(exception.get(key), str) and exception[key].strip() for key in ('approval', 'reason'))
                and isinstance(exception.get('issues'), list) and exception['issues']
                and all(isinstance(issue, str) and issue in permitted for issue in exception['issues'])):
            waived = [issue for issue in issues if issue in exception['issues']]
        else:
            issues.append('exception_scope_invalid')
    return dict(ci_digest=ci_digest, required_checks=required, issues=issues, waived=waived,
                decision_points=[issue for issue in issues if issue not in waived], exception=exception)


def publication_scope(task, observed):
    prs = observed['pull_requests']
    pr = prs[0] if len(prs) == 1 else {}
    return dict(repository=task.repository_identity.removeprefix('github.com/'), pr=pr.get('number'),
                head=observed['head'], target_branch=pr.get('target_branch'))


def report(conn, task, observed, publication=None):
    publication = publication if publication is not None else task.publication or {}
    prs = observed['pull_requests']
    pr = prs[0] if len(prs) == 1 else {}
    scope = publication_scope(task, observed)
    policy = policy_for(scope['repository'], scope['target_branch'])
    ci = pr.get('ci') or dict(scope, state='unavailable', checks=[])
    evaluation = evaluate(policy, ci, scope, publication.get('ci_exception'))
    submission = latest_submission(conn, task.id)
    metadata = {}
    if submission:
        row = conn.execute('SELECT metadata FROM task_runs WHERE id = ?', (submission['run_id'],)).fetchone()
        metadata = json.loads(row[0] or '{}')
    worker = dict(state='absent', run_id=submission['run_id'] if submission else None,
                  head=submission['state']['head'] if submission else None, tests_run=metadata.get('tests_run'))
    if submission:
        reports = worker['tests_run']
        reported = (isinstance(reports, (list, dict)) and bool(reports)
                    or isinstance(reports, str) and bool(reports.strip())
                    or type(reports) in {int, float} and reports > 0)
        worker['state'] = 'stale_head' if worker['head'] != scope['head'] else 'reported' if reported else 'absent'
    points = evaluation['decision_points']
    if worker['state'] != 'reported':
        points.append('worker_verification_' + worker['state'])
    current = capture_state(task)
    accepted = publication.get('accepted_state') or {}
    review = dict(state='current' if accepted.get('id') == current['id'] else 'stale',
                  run_id=publication.get('reviewer_run_id'), head=accepted.get('head'))
    if review['state'] != 'current':
        points.append('fresh_exact_state_review_required')
    authority = dict(reference=publication.get('authority'), merge='merge' in publication.get('actions', []))
    if not authority['reference'] or not authority['merge']:
        points.append('merge_authority_required')
    if authority['merge'] and publication.get('merge_scope') != scope:
        points.append('merge_authority_scope_mismatch')
    if not pr or pr.get('head') != scope['head'] or observed['retained_changes']:
        points.append('published_head_mismatch')
    if pr.get('state') == 'OPEN' and observed['remote_head'] != scope['head']:
        points.append('remote_head_mismatch')
    if pr.get('state') not in {'OPEN', 'MERGED'}:
        points.append('open_pr_required')
    if pr.get('state') == 'OPEN' and (pr.get('merge_state') not in {'CLEAN', 'UNSTABLE'} or pr.get('is_draft') is not False):
        points.append('github_merge_state_requires_decision')
    return dict(scope=scope, observed_at=observed['observed_at'], policy=policy, github_ci=ci,
                worker_verification=worker, independent_review=review, merge_authority=authority,
                github_merge_state=dict(state=pr.get('merge_state'), is_draft=pr.get('is_draft')),
                **evaluation, ready=not points)


def preflight(conn, task_id, *, exception=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_admission import _operator
    from hermes_cli.kanban_publication import save
    from hermes_cli.kanban_recovery import observe_publication
    kb._assert_not_delegated_child_mutation()
    if os.environ.get('HERMES_KANBAN_TASK') not in {None, task_id}:
        raise PermissionError('A worker can inspect only its own merge handoff')
    if exception is not None:
        _operator()
    task = kb.get_task(conn, task_id)
    if not task or not task.publication:
        raise ValueError('An accepted publication handoff is required')
    publication = dict(task.publication)
    if exception is not None:
        publication['ci_exception'] = exception
    before = capture_state(task)
    try:
        observed = observe_publication(task)
        refreshed = observe_publication(task)
        decision = report(conn, task, refreshed, publication)
        identity = lambda value: (value['head'], value['remote_head'], value['status_digest'],
                                  [(pr['number'], pr['head'], pr.get('target_branch'), pr['state'])
                                   for pr in value['pull_requests']])
        if identity(observed) != identity(refreshed):
            decision['decision_points'].append('publication_changed_during_preflight')
        if not refreshed['pull_requests'] or refreshed['pull_requests'][0]['state'] != 'OPEN':
            decision['decision_points'].append('open_pr_required')
    except (OSError, subprocess.SubprocessError, ValueError):
        unavailable = dict(head=before['head'], remote_head=None, retained_changes=True,
                           observed_at=int(time.time()), pull_requests=[])
        decision = report(conn, task, unavailable, publication)
        decision['decision_points'].append('publication_query_unavailable')
    if task.status in {'done', 'archived'}:
        decision['decision_points'].append('active_publication_required')
    if capture_state(task)['id'] != publication['accepted_state']['id']:
        decision['decision_points'].append('reviewed_state_changed_during_preflight')
    decision['ready'] = not decision['decision_points']
    if decision.get('policy') != policy_for(decision['scope']['repository'], decision['scope'].get('target_branch')):
        decision['decision_points'].append('policy_changed_during_preflight')
        decision['ready'] = False
    with kb.write_txn(conn):
        current = kb.get_task(conn, task.id)
        if (not current or current.publication != task.publication
                or current.current_run_id != task.current_run_id or current.status != task.status
                or any(getattr(current, key) != getattr(task, key) for key in (
                    'title', 'body', 'repository_identity', 'workspace_path', 'workspace_kind',
                    'branch_name', 'approved_base', 'review_required', 'execution_authority'))):
            decision['decision_points'].append('task_changed_during_preflight')
            decision['ready'] = False
        else:
            publication['merge_evidence'] = decision
            publication['merge_preflight'] = decision
            save(conn, task.id, publication)
        kb._append_event(conn, task.id, 'merge_preflight', decision)
    return decision
