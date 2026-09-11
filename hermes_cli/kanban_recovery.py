"""Attempt diagnosis and read-only publication reconciliation."""
import hashlib
import json
import subprocess
import time


def latest_failure(conn, task_id):
    return conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND outcome IN "
        "('crashed', 'spawn_failed', 'timed_out', 'stale', 'reclaimed') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def receipt(conn, task_id):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? "
                       "AND kind = 'recovery_reconciled' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def required(conn, task):
    if task.task_scope != 'repository' and task.workspace_kind != 'worktree':
        return False
    failure = latest_failure(conn, task.id)
    saved = receipt(conn, task.id)
    return bool(failure and (not saved or saved['failed_run_id'] != failure[0]))


def observe_publication(task):
    """Read local and remote identity without consuming publication authority."""
    from hermes_cli.kanban_admission import _git, validate_repository
    validate_repository(task)
    head = _git(task.workspace_path, 'rev-parse', 'HEAD')
    status = _git(task.workspace_path, 'status', '--porcelain=v1', '-z')
    remote = _git(task.workspace_path, 'ls-remote', '--heads', 'origin', 'refs/heads/' + task.branch_name)
    remote_lines = [line.split() for line in remote.splitlines() if line.strip()]
    if len(remote_lines) > 1 or any(len(parts) != 2 for parts in remote_lines):
        raise ValueError('Remote branch receipt is ambiguous')
    remote_head = remote_lines[0][0] if remote_lines else None
    repository = task.repository_identity.removeprefix('github.com/')
    result = subprocess.run(
        ['gh', 'pr', 'list', '--repo', repository, '--head', task.branch_name,
         '--state', 'all', '--json', 'number,url,headRefOid,baseRefName,state,isDraft,mergeStateStatus,statusCheckRollup'],
        check=True, capture_output=True, text=True, timeout=30,
    )
    prs = json.loads(result.stdout)
    if not isinstance(prs, list) or len(prs) > 1:
        raise ValueError('Pull request receipt is ambiguous')
    if any(not isinstance(pr, dict) or not all(pr.get(key) for key in ('number', 'url', 'headRefOid', 'state')) for pr in prs):
        raise ValueError('Pull request receipt is incomplete')
    from hermes_cli.kanban_ci import collect
    receipts = []
    for pr in prs:
        item = dict(number=pr['number'], url=pr['url'], head=pr['headRefOid'], state=pr['state'],
                    target_branch=pr.get('baseRefName'), merge_state=pr.get('mergeStateStatus'),
                    is_draft=pr.get('isDraft'), github_checks=pr.get('statusCheckRollup'),
                    github_checks_state='reported' if pr.get('statusCheckRollup') else
                    'absent' if pr.get('statusCheckRollup') == [] else 'unavailable')
        item['ci'] = collect(repository, item)
        receipts.append(item)
    return dict(repository=repository, observed_at=int(time.time()), head=head,
                    remote_head=remote_head, branch=task.branch_name, workspace=task.workspace_path,
                    retained_changes=bool(status), status_digest=hashlib.sha256(status.encode()).hexdigest(),
                    existing_commit=head != task.approved_base,
                    pull_requests=receipts)


def reconcile(conn, task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_admission import _operator, _git, validate_repository
    _operator()
    task = kb.get_task(conn, task_id)
    if not task or task.status in {'running', 'done', 'archived'} or task.claim_lock or task.current_run_id:
        raise ValueError('Recovery requires an inactive unfinished card')
    from hermes_cli.kanban_worker import active_worker_exists
    if active_worker_exists(conn, task_id):
        raise ValueError('A worker still owns this card')
    validate_repository(task)
    if task.workspace_set:
        from hermes_cli.kanban_workspace_set import reconcile as reconcile_set
        return reconcile_set(conn, task)
    failure = latest_failure(conn, task_id)
    if not failure:
        raise ValueError('Card has no failed attempt to reconcile')
    observed = observe_publication(task)
    head = observed['head']
    status = _git(task.workspace_path, 'status', '--porcelain=v1', '-z')
    if observed['remote_head'] and observed['remote_head'] != head:
        raise ValueError('Remote branch differs from the retained local commit')
    if observed['pull_requests'] and (observed['pull_requests'][0]['head'] != head or
                                     observed['pull_requests'][0]['state'] not in {'OPEN', 'MERGED'}):
        raise ValueError('Pull request state requires operator reconciliation')
    observed['failed_run_id'] = failure[0]
    with kb.write_txn(conn):
        current = kb.get_task(conn, task_id)
        if current.current_run_id or current.claim_lock or latest_failure(conn, task_id)[0] != failure[0]:
            raise ValueError('Attempt changed during reconciliation')
        if _git(task.workspace_path, 'rev-parse', 'HEAD') != head or _git(task.workspace_path, 'status', '--porcelain=v1', '-z') != status:
            raise ValueError('Workspace changed during reconciliation')
        kb._append_event(conn, task_id, 'recovery_reconciled', observed)
        if current.publication:
            from hermes_cli.kanban_publication import consumed_actions, save
            consumed = consumed_actions(observed)
            save(conn, task_id, dict(current.publication,
                                    actions=sorted(set(current.publication.get('actions', [])) - set(consumed)),
                                    consumed_actions=consumed, reconciliation=observed))
    return observed


def diagnostics(conn, task_id):
    rows = conn.execute(
        "SELECT a.run_id, a.startup_state, a.launched_at, a.initialised_at, a.last_activity_at, "
        "a.activity_kind, a.runtime_failure, a.exit_kind, a.exit_code, r.last_heartbeat_at, r.outcome, r.status "
        "FROM worker_attempts a JOIN task_runs r ON r.id = a.run_id WHERE a.task_id = ? ORDER BY a.run_id DESC LIMIT 10",
        (task_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def warn_stalled(conn):
    from hermes_cli import kanban_db as kb
    from hermes_cli.config import load_config_readonly
    threshold = (load_config_readonly() or {}).get('kanban', {}).get('worker_progress_warning_seconds', 3600)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold <= 0:
        raise ValueError('kanban.worker_progress_warning_seconds must be positive')
    now = int(time.time())
    rows = conn.execute(
        "SELECT a.*, t.id FROM worker_attempts a JOIN tasks t ON t.current_run_id = a.run_id "
        "WHERE t.status = 'running' AND a.startup_state = 'initialised'"
    ).fetchall()
    warned = []
    for row in rows:
        observed = row['last_activity_at'] or row['initialised_at'] or row['launched_at']
        if now - observed < threshold or (row['last_warning_at'] and row['last_warning_at'] >= observed):
            continue
        if not kb._worker_identity_alive(conn, row['id']):
            continue
        with kb.write_txn(conn):
            changed = conn.execute('UPDATE worker_attempts SET last_warning_at = ? WHERE run_id = ? '
                                   'AND last_activity_at IS ? AND last_warning_at IS ?',
                                   (now, row['run_id'], row['last_activity_at'], row['last_warning_at'])).rowcount
            if changed:
                kb._append_event(conn, row['id'], 'progress_warning',
                                 {'run_id': row['run_id'], 'quiet_seconds': now-observed,
                                  'reason': 'Verified worker is alive. Inspect progress before recovery.'}, run_id=row['run_id'])
                warned.append(row['id'])
    return warned


def record_transport_loss():
    import os
    from hermes_cli import kanban_db as kb
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    run_id = os.environ.get('HERMES_KANBAN_RUN_ID')
    if not task_id or not run_id:
        return
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            task = kb.get_task(conn, task_id)
            if not task or task.current_run_id != int(run_id) or task.status != 'running':
                return
            conn.execute("UPDATE worker_attempts SET runtime_failure = 'transport_lost' WHERE run_id = ?", (int(run_id),))
            kb._append_event(conn, task_id, 'runtime_failure', {'kind': 'transport_lost'}, run_id=int(run_id))
