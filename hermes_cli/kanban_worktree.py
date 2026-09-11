"""Task worktree ownership shared by preparation and cleanup consumers."""

from contextlib import closing
from pathlib import Path
import re
import sqlite3
import subprocess


def is_retained(path):
    """Preserve named task trees and registered paths, including archived boards.

    Unreadable ownership records preserve the candidate. Git locks cover
    cleanup consumers operating outside the owning profile.
    """
    from hermes_cli import kanban_db as kb

    target = Path(path).expanduser().resolve()
    if re.fullmatch(r"t_[0-9a-f]+", target.name):
        return True
    try:
        databases = {kb.kanban_db_path(), kb.kanban_home() / "kanban.db"}
        databases.update(kb.boards_root().glob("*/kanban.db"))
        databases.update(kb.boards_root().glob("_archived/*/kanban.db"))
        for database in databases:
            if not database.exists():
                continue
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                rows = conn.execute(
                    "SELECT workspace_path FROM tasks WHERE workspace_kind = 'worktree' "
                    "AND workspace_path IS NOT NULL"
                ).fetchall()
                for row in rows:
                    registered = Path(row[0]).expanduser().resolve()
                    if registered == target or target in registered.parents:
                        return True
    except (OSError, sqlite3.Error):
        return True
    return False


def retain(task):
    """Keep existing locks and add a persistent Git lock to an admitted tree."""
    from hermes_cli import kanban_db as kb

    if getattr(task, 'workspace_set', None):
        from hermes_cli.kanban_workspace_set import members
        for member in members(task):
            retain(member)
        return
    path = Path(task.workspace_path)
    git_dir = kb._git_dir(path)
    if git_dir is None or not kb._is_linked_worktree_checkout(path):
        raise ValueError("Retained workspace is missing or unregistered. Reconcile its contents before resuming")
    if (git_dir / "locked").exists():
        return
    result = subprocess.run(
        ["git", "-C", str(path), "worktree", "lock", "--reason",
         f"kanban retained task {task.id}", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode and not (git_dir / "locked").exists():
        raise ValueError("Cannot protect the registered task worktree: " + result.stderr.strip())
