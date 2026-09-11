"""Publication handoffs over real retained worktrees and isolated remote receipts."""

import json
from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_admission as admission
from hermes_cli import kanban_publication as publication
from hermes_cli.kanban_review import capture_state, latest_submission
from tests.hermes_cli.test_kanban_worktree_retention import fixture, git, submit


@pytest.fixture
def remote(monkeypatch, fixture):
    conn, task, repo = fixture
    state = {"head": None, "prs": []}
    real_git = admission._git
    real_run = subprocess.run

    def read_git(path, *args):
        if args[0] == "ls-remote":
            return f"{state['head']}\trefs/heads/{task.branch_name}" if state["head"] else ""
        return real_git(path, *args)

    def run(args, **kwargs):
        if args[:2] == ['gh', 'api']:
            if state.get('unavailable'):
                raise subprocess.CalledProcessError(1, args)
            data = {'check_runs': state.get('runs', [])} if '/check-runs?' in args[-1] else state.get('statuses', [])
            return subprocess.CompletedProcess(args, 0, json.dumps(data), '')
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(state["prs"]), "")
        return real_run(args, **kwargs)

    monkeypatch.setattr(admission, "_git", read_git)
    monkeypatch.setattr(subprocess, "run", run)
    return state


def acceptance(conn, task, *, outcome="accepted_pending_publication", session="reviewer-content"):
    return {"worker_session_id": session, "review_outcome": outcome,
            "reviewer_checks": ["Inspected all retained deliverables"],
            "reviewed_state_id": latest_submission(conn, task.id)["state"]["id"],
            "publication": {"deliverable": "pull_request"}}


def accept(conn, task, run, **kwargs):
    return kb.complete_task(conn, task.id, expected_run_id=run.current_run_id,
                            summary="Content accepted for publication",
                            metadata=acceptance(conn, task, **kwargs))


def publish_receipt(task, remote, *, state="OPEN"):
    head = git(task.workspace_path, "rev-parse", "HEAD")
    remote["head"] = head
    remote["prs"] = [{"number": 7, "url": "https://github.com/BRBussy/hermes-agent/pull/7",
                      "headRefOid": head, "baseRefName": "main", "isDraft": False, "mergeStateStatus": "CLEAN",
                      "state": state, "statusCheckRollup": []}]


def pending(fixture):
    conn, task, repo = fixture
    review = submit(conn, task, "developer-content")
    assert accept(conn, task, review)
    return conn, task, review


def test_accepted_content_is_unfinished_and_not_a_defect(fixture):
    conn, task, review = pending(fixture)
    saved = kb.get_task(conn, task.id)
    assert saved.status == "todo"
    assert saved.assignee == "builder"
    assert saved.execution_authority is None
    assert saved.publication["accepted_state"]["content_digest"]
    assert saved.publication["label"] == "Content accepted, publication awaiting authority"
    events = kb.list_events(conn, task.id)
    assert events and not any(e.kind in {"blocked", "changes_requested", "completed"} for e in events)
    assert kb.goal_run_status(conn, task.id, review.current_run_id) == "publication_pending"
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, task.id) is None
    assert Path(task.workspace_path, ".task-evidence/report.txt").read_text()


@pytest.mark.parametrize("invalid", ["session", "state", "checks", "run", "content"])
def test_pending_acceptance_rejects_invalid_review(fixture, invalid):
    conn, task, repo = fixture
    review = submit(conn, task, "developer-content")
    metadata = acceptance(conn, task)
    run_id = review.current_run_id
    if invalid == "session":
        metadata["worker_session_id"] = "developer-content"
    elif invalid == "state":
        metadata["reviewed_state_id"] = "stale"
    elif invalid == "checks":
        metadata["reviewer_checks"] = []
    elif invalid == "run":
        run_id += 100
    else:
        Path(task.workspace_path, "hook-output.txt").write_text("Modified content\n")
    with pytest.raises(ValueError):
        kb.complete_task(conn, task.id, summary="Invalid acceptance", metadata=metadata, expected_run_id=run_id)
    assert kb.get_task(conn, task.id).current_run_id == review.current_run_id
    assert kb.get_task(conn, task.id).publication is None


def test_publication_requires_fresh_developer_submission_and_review(fixture, remote):
    conn, task, review = pending(fixture)
    admission.authorise(conn, task.id, "Fixture authorises push and PR once",
                        publication_actions=["push", "pull_request"])
    worker = kb.claim_task(conn, task.id)
    assert worker
    assert kb.goal_run_status(conn, task.id, review.current_run_id) == "publication_pending"
    publish_receipt(task, remote)
    assert not accept(conn, task, worker, outcome="approved")
    assert kb.request_review(conn, task.id, summary="Published and worker checks passed",
                             metadata={"worker_session_id": "publisher", "tests_run": ["retained files"]},
                             expected_run_id=worker.current_run_id)
    final = kb.claim_review_task(conn, task.id)
    assert final and final.assignee == "reviewer"
    assert accept(conn, task, final, outcome="approved", session="final-reviewer")
    completed = kb.get_task(conn, task.id)
    assert completed.status == "done"
    assert completed.publication["phase"] == "verified"
    receipt = kb.latest_run(conn, task.id).metadata["publication_receipt"]
    assert receipt["pull_requests"][0]["github_checks_state"] == "absent"
    assert receipt["pull_requests"][0]["github_checks"] == []
    assert Path(task.workspace_path, ".task-evidence/report.txt").is_file()


@pytest.mark.parametrize("invalid", ["unpublished", "wrong_head", "closed", "dirty"])
def test_promised_deliverable_cannot_complete_without_matching_receipt(fixture, remote, invalid):
    conn, task, review = pending(fixture)
    admission.authorise(conn, task.id, "Fixture publication", publication_actions=["push", "pull_request"])
    publish_receipt(task, remote)
    if invalid == "unpublished":
        remote["prs"] = []
    elif invalid == "wrong_head":
        remote["head"] = "0" * 40
    elif invalid == "closed":
        remote["prs"][0]["state"] = "CLOSED"
    else:
        Path(task.workspace_path, "dirty.txt").write_text("Changed after acceptance\n")
        admission.authorise(conn, task.id, "Fixture correction only")
    final = submit(conn, task, "publisher")
    assert not accept(conn, task, final, outcome="approved", session="final-reviewer")
    assert kb.get_task(conn, task.id).status == "running"


def test_staging_preserves_content_identity_but_invalidates_exact_state(fixture):
    conn, task, repo = fixture
    path = Path(task.workspace_path, "content.txt")
    path.write_text("Accepted bytes\n")
    before = capture_state(task)
    git(task.workspace_path, "add", "--sparse", "content.txt")
    after = capture_state(task)
    assert before["id"] != after["id"]
    assert publication.same_content(before, after)
    path.write_text("Hook modified bytes\n")
    changed = capture_state(task)
    assert not publication.same_content(before, changed)


def test_changed_content_requires_fresh_acceptance_and_authority(fixture, remote):
    conn, task, review = pending(fixture)
    saved = kb.get_task(conn, task.id).publication["accepted_state"]
    hooks = Path(task.workspace_path).parent / "fixture-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'Hook modified bytes\\n' > hook-output.txt\n")
    hook.chmod(0o700)
    git(task.workspace_path, "-c", f"core.hooksPath={hooks}", "hook", "run", "pre-commit")
    with pytest.raises(ValueError, match="content changed"):
        admission.authorise(conn, task.id, "Fixture publish", publication_actions=["push"])
    admission.authorise(conn, task.id, "Fixture correction")
    kb.recompute_ready(conn)
    corrected = submit(conn, task, "developer-correction")
    assert accept(conn, task, corrected, session="reviewer-correction")
    assert kb.get_task(conn, task.id).publication["accepted_state"]["id"] != saved["id"]
    assert kb.get_task(conn, task.id).publication["authority"] is None


def test_changed_accepted_content_cannot_dispatch_for_publication(fixture, remote):
    conn, task, review = pending(fixture)
    admission.authorise(conn, task.id, "Fixture publish", publication_actions=["push"])
    Path(task.workspace_path, "changed.txt").write_text("Changed after authority\n")
    assert kb.claim_task(conn, task.id) is None
    observed = []
    kb.dispatch_once(conn, spawn_fn=lambda *a, **kw: observed.append(a), reconcile_orphans=False)
    assert observed == []
    assert kb.get_task(conn, task.id).current_run_id is None


def test_retry_reconciles_consumed_actions_and_same_workspace(fixture, remote):
    conn, task, review = pending(fixture)
    admission.authorise(conn, task.id, "Fixture publish once", publication_actions=["commit", "push", "pull_request"])
    worker = kb.claim_task(conn, task.id)
    assert worker
    publish_receipt(task, remote)
    kb._record_task_failure(conn, task.id, "Fixture interruption after PR", outcome="crashed",
                            release_claim=True, end_run=True)
    with pytest.raises(ValueError, match="Reconcile"):
        admission.authorise(conn, task.id, "Premature retry", publication_actions=["push"])
    from hermes_cli.kanban_recovery import reconcile
    observed = reconcile(conn, task.id)
    assert observed["pull_requests"][0]["number"] == 7
    admission.authorise(conn, task.id, "Continue only outstanding fixture actions",
                        publication_actions=["commit", "push", "pull_request"])
    restored = kb.get_task(conn, task.id)
    assert restored.publication["actions"] == []
    assert set(restored.publication["consumed_actions"]) == {"commit", "push", "pull_request"}
    assert restored.workspace_path == task.workspace_path
    assert "consumed_actions" in kb.build_worker_context(conn, task.id)


def test_later_authorised_merge_reopens_same_card_and_retains_reviews(fixture, remote, monkeypatch):
    from hermes_cli.kanban_ci import preflight
    from hermes_cli import config
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'kanban': {'repository_ci_policies': {
        'brbussy/hermes-agent': {'main': {'approval': 'Fixture policy', 'branch_rules': 'Fixture rules',
                                      'required_checks': [], 'allow_no_ci': True}}}}})
    conn, task, repo = fixture
    review = submit(conn, task, "developer", tests_run=["Fixture worker verification report"])
    assert accept(conn, task, review, outcome="approved", session="reviewer")
    history = [run.id for run in kb.list_runs(conn, task.id)]
    publish_receipt(task, remote)
    admission.authorise(conn, task.id, "Fixture authorises merge once", publication_actions=["merge"])
    reopened = kb.get_task(conn, task.id)
    assert reopened.status == "ready"
    assert reopened.publication["deliverable"] == "merge"
    assert reopened.workspace_path == task.workspace_path
    assert preflight(conn, task.id)["ready"]
    publish_receipt(task, remote, state="MERGED")
    remote["head"] = None
    final = submit(conn, task, "publisher", tests_run=["Fixture final worker report"])
    assert accept(conn, task, final, outcome="approved", session="merge-reviewer")
    assert set(history) < {run.id for run in kb.list_runs(conn, task.id)}


def test_empty_publication_actions_are_rejected(fixture, remote):
    conn, task, review = pending(fixture)
    with pytest.raises(ValueError, match="explicit publication actions"):
        admission.authorise(conn, task.id, "Empty fixture grant", publication_actions=[])
    assert kb.get_task(conn, task.id).publication["authority"] is None


def test_publication_permission_does_not_authorise_content_corrections(fixture, remote):
    conn, task, review = pending(fixture)
    admission.authorise(conn, task.id, "Fixture permits publication only", publication_actions=["push"])
    final = submit(conn, task, "publisher")
    assert kb.request_changes(conn, task.id, reason="Correct the verified content",
                              expected_run_id=final.current_run_id) == (True, "builder")
    assert kb.get_task(conn, task.id).publication["phase"] == "changes_required"
    assert kb.claim_task(conn, task.id) is None
    admission.authorise(conn, task.id, "Fixture explicitly permits the correction")
    assert kb.claim_task(conn, task.id)


def test_import_preserves_receipts_but_cannot_dispatch_source_workspace(fixture, remote, tmp_path):
    from hermes_cli.kanban_transfer import _relocate_imported_rows
    conn, task, review = pending(fixture)
    admission.authorise(conn, task.id, "Fixture publish", publication_actions=["push"])
    saved = kb.get_task(conn, task.id).publication
    with kb.connect(tmp_path / "imported.db") as copied:
        conn.backup(copied)
        _relocate_imported_rows(copied, "publication-import-fixture")
        imported = kb.get_task(copied, task.id)
        assert imported.publication == saved
        assert imported.workspace_path is None
        assert kb.claim_task(copied, task.id) is None
    assert kb.get_task(conn, task.id).publication == saved
    assert Path(task.workspace_path, ".task-evidence/report.txt").is_file()


def test_worker_cannot_authorise_publication(fixture, remote, monkeypatch):
    conn, task, review = pending(fixture)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    with pytest.raises(PermissionError):
        admission.authorise(conn, task.id, "Forged authority", publication_actions=["push"])


def test_tool_cli_and_dashboard_expose_same_handoff(fixture, remote, monkeypatch):
    from hermes_cli import kanban as cli
    from tools import kanban_tools
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router
    conn, task, repo = fixture
    review = submit(conn, task, "developer")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_SESSION_ID", "tool-reviewer")
    result = json.loads(kanban_tools._handle_complete({
        "summary": "Content accepted", "metadata": acceptance(conn, task),
    }))
    assert result["ok"] and result["status"] == "todo"
    shown = json.loads(kanban_tools._handle_show({}))
    assert shown["task"]["publication"]["phase"] == "accepted_pending_publication"
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    output = cli.run_slash(f'authorise {task.id} --authority "Fixture push only" --publication-action push')
    assert json.loads(output)["publication"]["actions"] == ["push"]
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    response = TestClient(app).get(f"/api/plugins/kanban/tasks/{task.id}")
    assert response.status_code == 200
    assert response.json()["task"]["publication"] == kb.get_task(conn, task.id).publication


def test_publication_goal_handoff_never_runs_completion_judge(monkeypatch):
    from hermes_cli import goals
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: pytest.fail("Pending publication must remain unfinished"))
    result = goals.run_kanban_goal_loop(
        task_id="t_fixture", goal_text="Publish reviewed content",
        run_turn=lambda prompt: pytest.fail("Reviewer handed off"),
        task_status_fn=lambda: "publication_pending",
        block_fn=lambda reason: pytest.fail("Content is accepted"), first_response="Content accepted",
    )
    assert result["outcome"] == "publication_pending"
    assert result["turns_used"] == 1


def test_interrupted_published_state_review_resumes_as_review(fixture, remote):
    from hermes_cli.kanban_recovery import reconcile
    conn, task, review = pending(fixture)
    admission.authorise(conn, task.id, "Fixture publish", publication_actions=["push", "pull_request"])
    publish_receipt(task, remote)
    final = submit(conn, task, "publisher")
    kb._record_task_failure(conn, task.id, "Fixture reviewer interruption", outcome="crashed",
                            release_claim=True, end_run=True)
    reconcile(conn, task.id)
    admission.authorise(conn, task.id, "Fixture resumes independent review only")
    resumed = kb.claim_review_task(conn, task.id)
    assert resumed and resumed.current_run_id != final.current_run_id
    assert resumed.publication["actions"] == []
    assert accept(conn, task, resumed, outcome="approved", session="resumed-reviewer")
