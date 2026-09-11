"""Persistent repository membership for one Kanban assignment."""

from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3
import threading


def members(task):
    if not task.workspace_set:
        return [task]
    manifest = task.workspace_set
    if manifest.get('state') != 'prepared' or not manifest.get('repositories'):
        raise ValueError('Workspace preparation is incomplete. Resume preparation on the same card')
    primary = manifest['repositories'][0]
    if (task.workspace_path, task.repository_identity, task.approved_base, task.branch_name) != (
            primary['workspace_path'], primary['repository_identity'], primary['base'], primary['branch']):
        raise ValueError('Primary workspace and workspace set differ')
    return [replace(task, workspace_set=None, repository_identity=item['repository_identity'],
                    workspace_path=item['workspace_path'], approved_base=item['base'],
                    branch_name=item['branch'], workspace_kind='worktree', review_required=True)
            for item in manifest['repositories']]


def specification(task_id, manifest):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_admission import _git
    from hermes_cli.banner import _canonical_github_remote
    if not isinstance(manifest, dict) or set(manifest) - {'repositories', 'links', 'package_manager', 'test_commands'}:
        raise ValueError('Workspace set needs repositories, links, package_manager and test_commands')
    rows = manifest.get('repositories')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Workspace set requires at least one repository')
    repositories = []
    identities = set()
    roots = set()
    for item in rows:
        if not isinstance(item, dict) or set(item) != {'repository', 'path', 'base', 'branch'}:
            raise ValueError('Each repository needs exactly repository, path, base and branch')
        if any(not isinstance(value, str) or not value.strip() for value in item.values()):
            raise ValueError('Repository preparation fields must be non-empty strings')
        repo, base, branch = item['repository'], item['base'], item['branch']
        if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('Repository must be organisation/name')
        if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base):
            raise ValueError('Base must be an exact lowercase commit SHA')
        root = kb._checked_worktree_path(Path(item['path']))
        identity = 'github.com/' + repo.lower()
        if root in roots or identity in identities:
            raise ValueError('A repository may occur only once in the workspace set')
        if kb._git_toplevel(root) != root or not (root / '.git').is_dir():
            raise ValueError('Prepare the primary repository checkout first')
        if _canonical_github_remote(_git(root, 'remote', 'get-url', 'origin')) != identity:
            raise ValueError('Repository origin differs from the selected identity')
        if _git(root, 'rev-parse', base + '^{commit}') != base:
            raise ValueError('Approved base is unavailable')
        _git(root, 'check-ref-format', 'refs/heads/' + branch)
        if not branch or branch.startswith('-'):
            raise ValueError('Invalid task branch')
        identities.add(identity)
        roots.add(root)
        repositories.append(dict(item, path=str(root), repository_identity=identity,
                                 workspace_path=str(root / '.worktrees' / task_id)))
    links = manifest.get('links', [])
    if not isinstance(links, list):
        raise ValueError('Links must be a list')
    names = {item['repository'] for item in repositories}
    for link in links:
        if (not isinstance(link, dict) or set(link) != {'consumer', 'dependency', 'package'}
                or link['consumer'] not in names or link['dependency'] not in names
                or link['consumer'] == link['dependency'] or not link['package']):
            raise ValueError('Each link needs a distinct member consumer, member dependency and package name')
    commands = manifest.get('test_commands', [])
    if not isinstance(commands, list) or any(not isinstance(cmd, str) or not cmd.strip() for cmd in commands):
        raise ValueError('Test commands must be non-empty strings')
    manager = manifest.get('package_manager')
    if len(rows) > 1 and (not commands or not isinstance(manager, str) or not re.fullmatch(r'[a-z][a-z0-9-]*@[0-9]+\.[0-9]+\.[0-9]+', manager)):
        raise ValueError('Multi-repository work requires an exact package-manager version and integration test commands')
    return {'repositories': repositories, 'links': links, 'package_manager': manager, 'test_commands': commands}


def prepare(conn, task_id, manifest):
    from hermes_cli import kanban_db as kb
    from hermes_cli import auth
    from hermes_cli.kanban_admission import _operator, validate_repository
    from hermes_cli.kanban_evidence import _directory
    from hermes_cli.kanban_worker import active_worker_exists
    from hermes_cli.kanban_worktree import retain
    _operator()
    task = kb.get_task(conn, task_id)
    if not task:
        raise ValueError('Task does not exist')
    directory = _directory(conn, task_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = kb._checked_worktree_path(directory / 'workspace-preparation.lock')
    with auth._file_lock(lock, threading.local(), 120, 'Workspace preparation is busy'):
        wanted = specification(task_id, manifest)
        with kb.write_txn(conn):
            task = kb.get_task(conn, task_id)
            if (task.status in {'running', 'done', 'archived'} or task.current_run_id or task.claim_lock
                    or active_worker_exists(conn, task_id)):
                raise ValueError('Preparation requires an inactive unfinished card')
            if task.workspace_set:
                saved = {key: value for key, value in task.workspace_set.items() if key != 'state'}
                if saved != wanted:
                    raise ValueError('Preparation conflicts with the retained workspace set')
                if task.workspace_set.get('state') == 'prepared':
                    validate_repository(task)
                    return task
            elif task.repository_identity:
                primary = wanted['repositories'][0]
                if (task.repository_identity, task.workspace_path, task.approved_base, task.branch_name) != (
                        primary['repository_identity'], primary['workspace_path'], primary['base'], primary['branch']):
                    raise ValueError('The first member must preserve the existing repository, worktree, base and branch')
            for item in wanted['repositories']:
                if conn.execute('SELECT 1 FROM tasks WHERE id != ? AND (workspace_path = ? OR '
                                '(repository_identity = ? AND branch_name = ?))',
                                (task_id, item['workspace_path'], item['repository_identity'], item['branch'])).fetchone():
                    raise ValueError('Workspace or branch belongs to another card')
                for row in conn.execute('SELECT workspace_set FROM tasks WHERE id != ? AND workspace_set IS NOT NULL', (task_id,)):
                    others = json.loads(row[0])['repositories']
                    if any(other['workspace_path'] == item['workspace_path'] or
                           (other['repository_identity'], other['branch']) == (item['repository_identity'], item['branch']) for other in others):
                        raise ValueError('Workspace or branch belongs to another card')
            pending = dict(wanted, state='preparing')
            primary = wanted['repositories'][0]
            conn.execute("UPDATE tasks SET workspace_set = ?, execution_authority = NULL, task_scope = 'repository', "
                         "workspace_kind = 'worktree', repository_identity = ?, workspace_path = ?, approved_base = ?, "
                         "branch_name = ?, review_required = 1 WHERE id = ?",
                         (json.dumps(pending), primary['repository_identity'], primary['workspace_path'], primary['base'], primary['branch'], task_id))
            kb._append_event(conn, task_id, 'workspace_preparation_started', pending)
        for item in wanted['repositories']:
            root = Path(item['path'])
            target = Path(item['workspace_path'])
            kb._ensure_git_worktree(root, target, item['branch'], base=item['base'])
            member = replace(task, workspace_set=None, workspace_kind='worktree', workspace_path=str(target),
                             repository_identity=item['repository_identity'], approved_base=item['base'],
                             branch_name=item['branch'], review_required=True)
            validate_repository(member)
            retain(member)
        with kb.write_txn(conn):
            current = kb.get_task(conn, task_id)
            if (current.current_run_id or current.claim_lock or current.workspace_set != pending
                    or current.status in {'running', 'done', 'archived'}):
                raise ValueError('Card changed during preparation. Reconcile the retained workspaces')
            primary = wanted['repositories'][0]
            prepared = dict(wanted, state='prepared')
            conn.execute("UPDATE tasks SET workspace_set = ?, repository_identity = ?, approved_base = ?, "
                         "workspace_kind = 'worktree', workspace_path = ?, branch_name = ?, review_required = 1 WHERE id = ?",
                         (json.dumps(prepared), primary['repository_identity'], primary['base'],
                          primary['workspace_path'], primary['branch'], task_id))
            kb._append_event(conn, task_id, 'workspace_set_prepared', prepared)
        return kb.get_task(conn, task_id)


def writable_roots(environment):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_admission import validate_repository
    database = environment.get('HERMES_KANBAN_DB')
    if not database or not Path(database).is_file():
        return []
    conn = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        task = kb.get_task(conn, environment['HERMES_KANBAN_TASK'])
        if not task or not task.workspace_set:
            return []
        if (str(task.current_run_id) != environment.get('HERMES_KANBAN_RUN_ID')
                or not task.claim_lock or task.claim_lock != environment.get('HERMES_KANBAN_CLAIM_LOCK')):
            raise ValueError('Workspace access requires the current task claim')
        validate_repository(task)
        return [member.workspace_path for member in members(task)]
    finally:
        conn.close()


def authorise_publication(conn, task, authority, plan):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_review import capture_state
    from hermes_cli.kanban_publication import same_content, consumed_actions, save
    from hermes_cli.kanban_recovery import observe_publication
    publication = task.publication
    if not publication or publication['phase'] not in {'accepted_pending_publication', 'publication_authorised'}:
        raise ValueError('Independent content acceptance is required before publication')
    states = capture_state(task)
    if not same_content(publication['accepted_state'], states):
        raise ValueError('Workspace set changed after content acceptance')
    repositories = members(task)
    if not isinstance(plan, dict) or set(plan) != {member.repository_identity for member in repositories}:
        raise ValueError('Publication authority must specify actions for every repository')
    scopes = {}
    for member in repositories:
        actions = plan[member.repository_identity]
        if (not isinstance(actions, list) or not actions
                or not set(actions) <= {'commit', 'push', 'pull_request', 'verify'}):
            raise ValueError('Select commit, push, pull_request or verify per repository')
        observed = observe_publication(member)
        consumed = consumed_actions(observed)
        scopes[member.repository_identity] = {'authority': authority, 'actions': sorted(set(actions) - set(consumed)),
                                              'consumed_actions': consumed, 'reconciliation': observed}
    if capture_state(task) != states:
        raise ValueError('Workspace set changed during publication reconciliation')
    publication = dict(publication, phase='publication_authorised', authority=authority,
                       repositories=scopes, actions=sorted({action for scope in scopes.values() for action in scope['actions']}))
    save(conn, task.id, publication)
    conn.execute('UPDATE tasks SET status = ?, assignee = ? WHERE id = ?',
                 (kb._landing_status_after_parents(conn, task.id), publication['implementer'], task.id))
    kb._append_event(conn, task.id, 'publication_authorised', publication)


def verify_publication(conn, task, metadata):
    from hermes_cli.kanban_review import capture_state
    from hermes_cli.kanban_publication import verify_delivery
    publication = task.publication
    scopes = publication.get('repositories', {})
    states = capture_state(task)
    repositories = members(task)
    if set(scopes) != {member.repository_identity for member in repositories}:
        return 'Publication requires authority for every repository'
    receipts = {}
    for member in repositories:
        identity = member.repository_identity
        member.publication = dict(publication, **scopes[identity],
                                  accepted_state=publication['accepted_state']['repositories'][identity])
        member_metadata = {}
        rejection = verify_delivery(conn, member, member_metadata)
        if rejection:
            return identity + ': ' + rejection
        receipts[identity] = member_metadata['publication_receipt']
    if capture_state(task) != states:
        return 'Workspace set changed during publication verification'
    metadata['publication_receipt'] = {'repositories': receipts}
    return None


def reconcile(conn, task):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_recovery import latest_failure, observe_publication
    from hermes_cli.kanban_review import capture_state
    from hermes_cli.kanban_publication import consumed_actions, save
    failed = latest_failure(conn, task.id)
    if not failed:
        raise ValueError('Card has no failed attempt to reconcile')
    before = capture_state(task)
    observed = {}
    for member in members(task):
        receipt = observe_publication(member)
        if receipt['remote_head'] and receipt['remote_head'] != receipt['head']:
            raise ValueError(member.repository_identity + ': remote branch differs from retained HEAD')
        consumed_actions(receipt)
        observed[member.repository_identity] = receipt
    result = {'failed_run_id': failed[0], 'repositories': observed}
    with kb.write_txn(conn):
        current = kb.get_task(conn, task.id)
        if (current.current_run_id or current.claim_lock or current.workspace_set != task.workspace_set
                or latest_failure(conn, task.id)[0] != failed[0] or capture_state(current) != before):
            raise ValueError('Workspace set or attempt changed during reconciliation')
        if current.publication:
            scopes = dict(current.publication.get('repositories', {}))
            for identity, receipt in observed.items():
                consumed = consumed_actions(receipt)
                scope = scopes.get(identity, {})
                scopes[identity] = dict(scope, reconciliation=receipt, consumed_actions=consumed,
                                       actions=sorted(set(scope.get('actions', [])) - set(consumed)))
            save(conn, task.id, dict(current.publication, repositories=scopes, reconciliation=result,
                                    actions=sorted({action for scope in scopes.values() for action in scope['actions']})))
        kb._append_event(conn, task.id, 'recovery_reconciled', result)
    return result
