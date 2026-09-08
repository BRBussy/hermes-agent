from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_dispatch_passes_recorded_workspace_to_spawn(monkeypatch, tmp_path, lane):
    home = tmp_path / "profile"
    (home / "profiles/worker").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    task_id = kb.create_task(conn, title="Workspace context", assignee="worker",
                             workspace_kind="worktree", workspace_path=str(tmp_path / "anchor"))
    if lane == "review":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
    workspace = tmp_path / "resolved"
    workspace.mkdir()
    branch = "wt/resolved"
    monkeypatch.setattr(kb, "_resolve_worktree_workspace", lambda task, **kwargs: (workspace, branch))
    captured = []

    def spawn(task, path, **kwargs):
        recorded = kb.get_task(conn, task.id)
        captured.append((task.workspace_path, task.branch_name, path,
                         recorded.workspace_path, recorded.branch_name))
        return None

    kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
    assert captured
    assert captured == [(str(workspace), branch, str(workspace), str(workspace), branch)]
    conn.close()
