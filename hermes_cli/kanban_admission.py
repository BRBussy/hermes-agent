"""Repository preparation and execution authority for Kanban cards."""
from pathlib import Path
import os
import re
import subprocess

from hermes_cli.banner import _canonical_github_remote


def _git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True,
                                   stderr=subprocess.PIPE, timeout=30).strip()


def _operator():
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise PermissionError('A worker cannot authorise or prepare its own follow-up tasks')


def validate_repository(task):
    from hermes_cli import kanban_db as kb
    if not all((task.repository_identity, task.approved_base, task.workspace_path,
                task.branch_name, task.review_required, task.workspace_kind == 'worktree')):
        raise ValueError('Repository work requires preparation with an exact base, worktree, branch and review')
    path = kb._checked_worktree_path(Path(task.workspace_path))
    if kb._git_toplevel(path) != path or not kb._is_linked_worktree_checkout(path):
        raise ValueError('Repository task requires its registered persistent worktree')
    if kb._git_current_branch(path) != task.branch_name:
        raise ValueError('Prepared branch differs from the checkout')
    origin = _canonical_github_remote(_git(path, 'remote', 'get-url', 'origin'))
    if origin != task.repository_identity:
        raise ValueError('Prepared repository identity differs from origin')
    subprocess.run(['git', '-C', str(path), 'merge-base', '--is-ancestor', task.approved_base, 'HEAD'],
                   check=True, capture_output=True, timeout=30)


def rejection(task):
    if not task.execution_authority:
        return 'Explicit execution authority is required'
    if task.task_scope == 'repository' or task.workspace_kind == 'worktree' or task.project_id:
        try:
            validate_repository(task)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return str(exc)
    return None


def authorise(conn, task_id, authority):
    from hermes_cli import kanban_db as kb
    _operator()
    if not isinstance(authority, str) or not authority.strip():
        raise ValueError('Record the task-specific execution authority')
    with kb.write_txn(conn):
        from hermes_cli.kanban_worker import active_worker_exists
        if active_worker_exists(conn, task_id):
            raise ValueError('A worker still owns this card')
        task = kb.get_task(conn, task_id)
        if not task or task.status in {'running', 'done', 'archived'} or task.claim_lock:
            raise ValueError('Authorisation requires an inactive unfinished card')
        unresolved = conn.execute(
            "SELECT 1 FROM worker_attempts WHERE task_id = ? AND identity IS NULL "
            "AND startup_state = 'failed-start' LIMIT 1", (task_id,),
        ).fetchone()
        if unresolved:
            raise ValueError('Unverified startup requires process ownership reconciliation')
        from hermes_cli.kanban_recovery import required
        if required(conn, task):
            raise ValueError('Reconcile the failed attempt before authorising recovery')
        if task.task_scope == 'repository' or task.workspace_kind == 'worktree' or task.project_id:
            validate_repository(task)
        conn.execute('UPDATE tasks SET execution_authority = ? WHERE id = ?', (authority.strip(), task_id))
        kb._append_event(conn, task_id, 'execution_authorised', {'authority': authority.strip()})
    return kb.get_task(conn, task_id)


def prepare(conn, task_id, repository, repository_path, base, branch):
    from hermes_cli import kanban_db as kb
    _operator()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository must be organisation/name')
    if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', base):
        raise ValueError('Approved base must be an exact commit SHA')
    root = kb._checked_worktree_path(Path(repository_path))
    identity = 'github.com/' + repository.lower()
    if _canonical_github_remote(_git(root, 'remote', 'get-url', 'origin')) != identity:
        raise ValueError('Repository origin does not match the requested identity')
    if _git(root, 'rev-parse', base + '^{commit}') != base.lower():
        raise ValueError('Approved base is unavailable')
    with kb.write_txn(conn):
        from hermes_cli.kanban_worker import active_worker_exists
        if active_worker_exists(conn, task_id):
            raise ValueError('A worker still owns this card')
        task = kb.get_task(conn, task_id)
        if not task or task.status in {'running', 'done', 'archived'} or task.claim_lock or task.current_run_id:
            raise ValueError('Preparation requires an inactive unfinished card')
        target = root / '.worktrees' / task_id
        if task.repository_identity:
            if (task.repository_identity, task.approved_base, task.branch_name, task.workspace_path) != (
                    identity, base.lower(), branch, str(target)):
                raise ValueError('Preparation conflicts with the recorded repository, base, branch or workspace')
            validate_repository(task)
            return task
        if conn.execute('SELECT 1 FROM tasks WHERE id != ? AND (workspace_path = ? OR '
                        '(repository_identity = ? AND branch_name = ?))',
                        (task_id, str(target), identity, branch)).fetchone():
            raise ValueError('Workspace or branch belongs to another card')
        kb._ensure_git_worktree(root, target, branch, base=base.lower())
        task.workspace_kind = 'worktree'
        task.workspace_path = str(target)
        task.branch_name = branch
        task.repository_identity = identity
        task.approved_base = base.lower()
        task.review_required = True
        validate_repository(task)
        conn.execute("UPDATE tasks SET task_scope = 'repository', repository_identity = ?, approved_base = ?, "
                     "workspace_kind = 'worktree', workspace_path = ?, branch_name = ?, review_required = 1, "
                     "execution_authority = NULL WHERE id = ?",
                     (identity, base.lower(), str(target), branch, task_id))
        kb._append_event(conn, task_id, 'repository_prepared',
                         {'repository': identity, 'base': base.lower(), 'workspace': str(target), 'branch': branch})
    return kb.get_task(conn, task_id)
