"""Repository state receipts for required same-card Kanban review."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


def capture_state(task):
    """Hash tracked and non-ignored untracked files, index, HEAD and requirements.

    Ignored build outputs are outside the review contract. Symlinks and nested
    repositories fail closed. Deliverables must be regular repository files.
    This is a completion-time snapshot, not a filesystem lock against other
    processes changing files after completion.
    """
    if getattr(task, 'workspace_set', None):
        from hermes_cli.kanban_workspace_set import members
        repositories = members(task)
        states = {member.repository_identity: capture_state(member) for member in repositories}
        if states != {member.repository_identity: capture_state(member) for member in repositories}:
            raise ValueError('Workspace set changed during review capture')
        state = dict(next(iter(states.values())))
        state['repositories'] = states
        state['workspace_set'] = task.workspace_set
        state['id'] = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return state
    if task.repository_identity:
        from hermes_cli.kanban_admission import validate_repository
        validate_repository(task)
    root = Path(task.workspace_path or "").resolve(strict=True)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True,
            timeout=30,
        ).stdout

    if task.workspace_kind == "scratch" or not task.workspace_path:
        raise ValueError("Required review needs a persistent repository workspace")
    if Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise ValueError("Review workspace must be the repository root")

    def snapshot():
        names = sorted(set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")) - {b""})
        if not names:
            raise ValueError("Review file set is empty")
        files = []
        for raw in names:
            name = os.fsdecode(raw)
            path = root / name
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root):
                raise ValueError(f"Review does not support symbolic links: {name}")
            if not path.exists():
                files.append([name, "deleted"])
                continue
            mode = path.stat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"Review requires regular files: {name}")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            files.append([name, stat.S_IMODE(mode), digest.hexdigest()])
        changed_names = set(git("diff", "--name-only", "-z", "HEAD").split(b"\0"))
        changed_names.update(git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"))
        return {
            "workspace": str(root),
            "head": git("rev-parse", "HEAD").decode().strip(),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD").decode().strip(),
            "index": hashlib.sha256(git("ls-files", "--stage", "-z")).hexdigest(),
            "file_count": len(files),
            "all_files_digest": hashlib.sha256(json.dumps(files, ensure_ascii=True).encode()).hexdigest(),
            "content_digest": hashlib.sha256(json.dumps(
                [record for record in files if record[1] != "deleted"], ensure_ascii=True,
            ).encode()).hexdigest(),
            "files": [record for record in files if os.fsencode(record[0]) in changed_names],
            "requirements": [task.title, task.body],
        }

    state = snapshot()
    if state != snapshot():
        raise ValueError("Repository changed while capturing review state")
    state["id"] = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return state


def latest_submission(conn, task_id):
    row = conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id = ? "
        "AND outcome = 'review_requested' ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row is None:
        return None
    metadata = json.loads(row["metadata"] or "{}")
    state = metadata.get("review_state")
    if not isinstance(state, dict) or not state.get("id"):
        return None
    return {"run_id": row["id"], "session_id": metadata.get("worker_session_id"), "state": state}


def completion_rejection(conn, task, metadata, expected_run_id, *, outcome="approved"):
    from hermes_cli.kanban_db import _retry_status_for_run

    if (task.status != "running" or expected_run_id is None
            or task.current_run_id != expected_run_id
            or _retry_status_for_run(conn, task.id, expected_run_id) != "review"):
        return "Required review can only complete from the current independent reviewer run"
    submission = latest_submission(conn, task.id)
    metadata = metadata or {}
    if not submission or submission["run_id"] == expected_run_id:
        return "A developer review submission is required"
    session = metadata.get("worker_session_id")
    if not session or not submission["session_id"] or session == submission["session_id"]:
        return "Developer and reviewer must have distinct recorded session identities"
    if metadata.get("review_outcome") != outcome or not metadata.get("reviewer_checks"):
        return "Reviewer acceptance and independent reviewer_checks are required"
    if metadata.get("reviewed_state_id") != submission["state"]["id"]:
        return "Reviewer must accept the submitted review state ID"
    try:
        current = capture_state(task)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return f"Cannot verify reviewed state: {exc}"
    if getattr(task, 'workspace_set', None):
        checks = metadata.get('repository_checks')
        required = current['repositories']
        if not isinstance(checks, dict) or set(checks) != set(required):
            return 'Independent review requires checks for every repository in the workspace set'
        for identity, state in required.items():
            receipt = checks[identity]
            if not isinstance(receipt, dict) or receipt.get('reviewed_state_id') != state['id'] or not receipt.get('checks'):
                return 'Each repository requires its exact reviewed state and independent checks'
    if current != submission["state"]:
        return "Deliverables or requirements changed. Request changes and resubmit for fresh review"
    return None
