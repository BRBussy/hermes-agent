"""Explicit task workspace retirement with durable recovery and decision receipts."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_evidence as evidence


def _git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True,
                            text=True, timeout=30, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    if result.returncode:
        raise ValueError('Git verification failed: ' + args[0])
    return result.stdout.strip()


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(directory, name, receipt):
    evidence._write(directory / name, evidence._json(receipt))
    _sync(directory)


def inspect(conn, task_id):
    """Expose receipts and distinguish an unavailable board path from retirement."""
    task = kb.get_task(conn, task_id)
    directory = evidence._directory(conn, task_id)
    receipts = []
    for path in sorted(directory.glob('disposal-*.json')):
        try:
            receipt = json.loads(evidence._read(path))
            receipt['receipt_path'] = str(path)
            if receipt.get('outcome') == 'pending':
                receipt['outcome'] = 'unknown'
                receipt['reason'] = 'Interrupted request requires reconciliation. Do not repeat filesystem actions'
            receipts.append(receipt)
        except (OSError, ValueError):
            receipts.append({'outcome': 'unknown', 'receipt_path': str(path), 'reason': 'Receipt unavailable'})
    finished = {r.get('request_id') for r in receipts if not r.get('receipt_path', '').endswith('-intent.json')}
    receipts = [r for r in receipts if not (r.get('receipt_path', '').endswith('-intent.json') and r.get('request_id') in finished)]
    present = bool(task and task.workspace_path and Path(task.workspace_path).is_dir())
    removed = any(r.get('outcome') == 'removed' for r in receipts)
    return {'workspace_path': task.workspace_path if task else None, 'path_present': present,
            'state': ('unexpected_replacement' if removed else 'present') if present else ('removed' if removed else 'unavailable'),
            'receipts': receipts}


def _inventory(path):
    records = {}
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            source = Path(root) / name
            relative = str(source.relative_to(path))
            if relative == '.git':
                continue
            info = source.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError('Unclassified symbolic link or special file requires preservation review')
            if stat.S_ISREG(info.st_mode):
                records[relative] = hashlib.sha256(evidence._read(source)).hexdigest()
    if not records:
        raise ValueError('Workspace inventory must contain files')
    return records


def _remote(task, remote, ref, expected_head):
    path = Path(task.workspace_path)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', remote or '') or remote.startswith('-'):
        raise ValueError('Select one explicit remote')
    if not ref or not ref.startswith('refs/heads/'):
        raise ValueError('Select a full remote branch reference')
    _git(path, 'check-ref-format', ref)
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', expected_head or ''):
        raise ValueError('Specify the approved exact workspace HEAD')
    if _git(path, 'rev-parse', 'HEAD') != expected_head:
        raise ValueError('Workspace HEAD differs from disposal authority')
    if _git(path, 'rev-parse', '--is-shallow-repository') != 'false':
        raise ValueError('Shallow recovery history is unverifiable')
    from hermes_cli.banner import _canonical_github_remote
    urls = _git(path, 'remote', 'get-url', '--all', remote).splitlines()
    if len(urls) != 1 or _canonical_github_remote(urls[0]) != task.repository_identity:
        raise ValueError('Recovery remote is ambiguous or differs from the prepared repository')
    rows = _git(path, 'ls-remote', '--exit-code', '--refs', remote, ref).splitlines()
    if rows != [expected_head + '\t' + ref]:
        raise ValueError('Remote branch does not affirm the approved exact HEAD')
    return {'remote': remote, 'ref': ref, 'head': expected_head,
            'repository_identity': task.repository_identity, 'verified_at_ns': time.time_ns(),
            'method': 'ls-remote exact branch HEAD'}


def _inactive(conn, task):
    from hermes_cli.kanban_worker import active_worker_exists
    if task.status not in {'done', 'archived', 'cancelled', 'failed'} or task.claim_lock or active_worker_exists(conn, task.id):
        raise ValueError('Disposal requires a terminal card without an active owning attempt')
    if conn.execute('SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL LIMIT 1', (task.id,)).fetchone():
        raise ValueError('An unfinished attempt owns this workspace')
    if conn.execute("SELECT 1 FROM worker_attempts WHERE task_id = ? AND identity IS NULL AND startup_state IN ('starting', 'failed-start') LIMIT 1", (task.id,)).fetchone():
        raise ValueError('Unverified worker startup requires reconciliation')
    if conn.execute("SELECT 1 FROM task_links l JOIN tasks t ON t.id = l.child_id WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') LIMIT 1", (task.id,)).fetchone():
        raise ValueError('Active children still require the workspace')
    if conn.execute('SELECT 1 FROM tasks WHERE id != ? AND workspace_path = ? LIMIT 1', (task.id, task.workspace_path)).fetchone():
        raise ValueError('Another card owns this workspace')


def _artifacts(conn, task, inventory):
    evidence.require_retained(conn, task.id)
    receipts = evidence.discover(conn, task.id)
    retained = {}
    references = []
    for receipt in receipts:
        manifest = receipt.get('manifest', {})
        if receipt['status'] != 'retained' or manifest.get('workspace') != task.workspace_path or manifest.get('task_id') != task.id:
            continue
        references.append(receipt['path'])
        for record in manifest['artifacts']:
            retained.setdefault(record['source'], set()).add(record['sha256'])
    if not references:
        raise ValueError('Disposal requires verified retained task artifacts')
    tracked = set(_git(task.workspace_path, 'ls-files', '-z').split('\0'))
    tracked.discard('')
    if not tracked:
        raise ValueError('Tracked inventory must contain files')
    for path, digest in inventory.items():
        if path not in tracked and digest not in retained.get(path, set()):
            raise ValueError('Untracked or ignored content lacks matching retained evidence')
    for path, digests in retained.items():
        if path in inventory and inventory[path] not in digests:
            raise ValueError('Required artifact changed after retention')
    return references


def dispose(conn, task_id, *, request_id, authority, expected_head, remote, ref, dry_run=False):
    """Retire a workspace into private recovery storage without unlinking its bytes.

    The database write transaction blocks claims and the repository lock serialises
    preparation. Atomic renames preserve late writes through open descriptors.
    A durable intent precedes both renames. Interrupted requests require operator
    reconciliation and cannot resume destructive actions by retrying an identifier.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request_id or ''):
        raise ValueError('A safe, unique request identifier is required')
    if not re.fullmatch(r't_[0-9a-f]+', task_id or '') or kb.get_task(conn, task_id) is None:
        raise ValueError('Disposal requires an existing task identity')
    if kb.get_task(conn, task_id).workspace_set:
        raise ValueError('Workspace sets require a recovery plan covering every member before disposal')
    directory = evidence._directory(conn, task_id)
    kb._checked_worktree_path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    _sync(directory.parent)
    final = directory / f'disposal-{request_id}-result.json'
    intent = directory / f'disposal-{request_id}-intent.json'
    with kb.write_txn(conn):
        if final.exists():
            return json.loads(evidence._read(final))
        if intent.exists():
            return {'request_id': request_id, 'outcome': 'unknown', 'reason': 'Interrupted disposal requires reconciliation', 'receipt_path': str(intent)}
        task = kb.get_task(conn, task_id)
        receipt = {'schema': 1, 'request_id': request_id, 'task_id': task_id,
                   'authority': authority, 'created_at_ns': time.time_ns(), 'outcome': 'refused',
                   'workspace': task.workspace_path if task else None,
                   'run_id': task.current_run_id if task else None, 'dry_run': dry_run, 'checks': {}}
        started = False
        try:
            from hermes_cli.kanban_admission import _operator, validate_repository
            from hermes_cli import auth
            _operator()
            if not isinstance(authority, str) or not authority.strip():
                raise ValueError('Explicit task-specific disposal authority is required')
            if not task:
                raise ValueError('Task does not exist')
            if any(r.get('outcome') in {'unknown', 'removed'} for r in inspect(conn, task_id)['receipts']):
                raise ValueError('Existing disposal requires reconciliation before another request')
            _inactive(conn, task)
            validate_repository(task)
            path = kb._checked_worktree_path(Path(task.workspace_path))
            common = kb._git_common_dir(path)
            admin = kb._git_dir(path)
            if not common or not admin or admin.parent != common / 'worktrees':
                raise ValueError('Linked worktree administration identity is unverifiable')
            lock_path = kb._checked_worktree_path(common / 'hermes-worktrees.lock')
            with auth._file_lock(lock_path, threading.local(), 120, 'Timed out locking task disposal'):
                validate_repository(task)
                _inactive(conn, task)
                lock = admin / 'locked'
                if evidence._read(lock).decode().strip() != f'kanban retained task {task.id}':
                    raise ValueError('User lock must be preserved')
                if _git(path, 'status', '--porcelain', '--untracked-files=no'):
                    raise ValueError('Tracked changes require preservation review')
                admin_inventory = _inventory(admin)
                receipt['checks']['remote'] = _remote(task, remote, ref, expected_head)
                inventory = _inventory(path)
                receipt['checks']['artifacts'] = _artifacts(conn, task, inventory)
                receipt['checks']['files'] = inventory
                receipt['checks']['identity'] = {'repository': task.repository_identity, 'branch': task.branch_name,
                                                'base': task.approved_base, 'git_dir': str(admin)}
                if dry_run:
                    receipt['outcome'] = 'retained'
                    receipt['reason'] = 'Preservation checks passed. Dry run retains the workspace'
                    receipt['finished_at_ns'] = time.time_ns()
                    _save(directory, final.name, receipt)
                    kb._append_event(conn, task_id, 'workspace_disposal', receipt)
                    return receipt
                recovery_root = kb._checked_worktree_path(common / 'hermes-disposal-recovery')
                recovery_root.mkdir(mode=0o700, exist_ok=True)
                _sync(common)
                if recovery_root.stat().st_uid != os.getuid() or stat.S_IMODE(recovery_root.stat().st_mode) != 0o700:
                    raise ValueError('Recovery storage must be private and owned by the executing account')
                recovery = recovery_root / (task.id + '-' + request_id)
                recovery.mkdir(mode=0o700)
                _sync(recovery_root)
                if path.stat().st_dev != recovery.stat().st_dev:
                    raise ValueError('Atomic recovery requires the same filesystem')
                if directory.resolve().is_relative_to(path) or recovery.resolve().is_relative_to(path):
                    raise ValueError('Recovery and receipt storage must be outside the workspace')
                receipt['recovery'] = {'workspace': str(recovery / 'workspace'), 'git_dir': str(recovery / 'git-admin'),
                                       'common_dir': str(common), 'branch': task.branch_name, 'head': expected_head,
                                       'storage_policy': 'Complete bytes retained. Disk reclamation requires separate authority'}
                receipt['outcome'] = 'pending'
                _save(directory, intent.name, receipt)
                receipt['checks']['remote'] = _remote(task, remote, ref, expected_head)
                if _inventory(path) != inventory or evidence._read(lock).decode().strip() != f'kanban retained task {task.id}':
                    raise ValueError('Workspace or lock changed during disposal checks')
                validate_repository(task)
                _inactive(conn, task)
                if _inventory(admin) != admin_inventory:
                    raise ValueError('Git administration changed during disposal checks')
                started = True
                os.rename(path, recovery / 'workspace')
                _sync(path.parent)
                _sync(recovery)
                os.rename(admin, recovery / 'git-admin')
                _sync(admin.parent)
                _sync(recovery)
                if (path.exists() or admin.exists() or _inventory(recovery / 'workspace') != inventory
                        or _inventory(recovery / 'git-admin') != admin_inventory
                        or evidence._read(recovery / 'git-admin' / 'locked').decode().strip() != f'kanban retained task {task.id}'
                        or _git(common, 'rev-parse', 'refs/heads/' + task.branch_name) != expected_head):
                    raise RuntimeError('Workspace changed during retirement. Recovery bytes retained')
                receipt['outcome'] = 'removed'
                receipt['reason'] = 'Task workspace and registration retired. Complete recovery bytes and branch retained'
                conn.execute('UPDATE tasks SET execution_authority = NULL WHERE id = ?', (task.id,))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            receipt['outcome'] = 'unknown' if started else ('failed' if isinstance(exc, OSError) else 'refused')
            receipt['reason'] = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        except BaseException:
            conn.rollback()
            raise
        receipt['finished_at_ns'] = time.time_ns()
        _save(directory, final.name, receipt)
        if task:
            kb._append_event(conn, task_id, 'workspace_disposal', receipt)
        return receipt


def recovery_branches(repo_root):
    """Return recovery branch anchors, or None when ownership is unverifiable."""
    try:
        common = kb._git_common_dir(Path(repo_root))
        if common is None:
            return None
        directory = kb._checked_worktree_path(common / 'hermes-disposal-recovery')
        branches = set()
        for recovery in directory.iterdir() if directory.exists() else ():
            head = evidence._read(recovery / 'git-admin' / 'HEAD').decode().strip()
            if not head.startswith('ref: refs/heads/'):
                return None
            branches.add(head.removeprefix('ref: refs/heads/'))
        return branches
    except (OSError, ValueError):
        return None
