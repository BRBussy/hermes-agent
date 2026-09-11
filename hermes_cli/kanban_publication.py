"""Same-card publication authority and exact-content review handoffs."""

import json
import subprocess

from hermes_cli.kanban_review import capture_state, completion_rejection, latest_submission


def same_content(accepted, current):
    return all(accepted.get(key) == current.get(key) for key in (
        "workspace", "branch", "requirements", "content_digest",
    )) and bool(accepted.get("content_digest"))


def save(conn, task_id, publication):
    publication = dict(publication, label={
        "accepted_pending_publication": "Content accepted, publication awaiting authority",
        "publication_authorised": "Content accepted, publication authorised",
        "reviewing_publication": "Publication awaiting independent review",
        "changes_required": "Content requires fresh review",
        "verified": "Publication independently verified",
    }[publication["phase"]])
    if (publication['phase'] == 'publication_authorised' and 'merge' in publication.get('actions', [])
            and set(publication['actions']) <= {'merge', 'verify'}
            and not publication.get('merge_evidence', {}).get('ready')):
        publication['label'] = 'Merge awaiting CI, verification or authority decision'
    conn.execute("UPDATE tasks SET publication = ? WHERE id = ?",
                 (json.dumps(publication), task_id))


def accept_pending(conn, task, summary, metadata, run_id):
    from hermes_cli import kanban_db as kb
    if not task or not task.review_required or not task.repository_identity:
        raise ValueError("Publication acceptance requires a prepared repository and independent review")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Publication acceptance requires a review summary")
    rejection = completion_rejection(
        conn, task, metadata, run_id, outcome="accepted_pending_publication",
    )
    if rejection:
        raise ValueError(rejection)
    requested = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1", (task.id,),
    ).fetchone()
    provenance = json.loads(requested[0])
    if not provenance.get("implementer"):
        raise ValueError("Publication needs the recorded developer identity")
    requested_publication = metadata.get("publication")
    if not isinstance(requested_publication, dict):
        raise ValueError("publication must describe the promised deliverable")
    deliverable = requested_publication.get("deliverable")
    if deliverable not in {"branch", "pull_request", "merge"}:
        raise ValueError("publication.deliverable must be branch, pull_request or merge")
    publication = {
        "phase": "accepted_pending_publication", "deliverable": deliverable,
        "accepted_state": latest_submission(conn, task.id)["state"],
        "reviewer_run_id": run_id, "implementer": provenance["implementer"],
        "reviewer": task.assignee, "authority": None, "actions": [],
    }
    from hermes_cli.kanban_evidence import preserve
    metadata = preserve(conn, task.id, metadata, "review_requested")
    conn.execute(
        "UPDATE tasks SET status = 'todo', assignee = ?, execution_authority = NULL, "
        "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, last_failure_error = NULL "
        "WHERE id = ?", (publication["implementer"], task.id),
    )
    save(conn, task.id, publication)
    kb._end_run(conn, task.id, outcome="publication_pending", status="todo",
                summary=summary, metadata=metadata)
    kb._append_event(conn, task.id, "publication_pending", {
        "summary": summary, "deliverable": deliverable,
        "reviewed_state_id": publication["accepted_state"]["id"],
        "implementer": publication["implementer"], "reviewer": task.assignee,
    }, run_id=run_id)
    return True


def dispatch_rejection(task):
    publication = task.publication
    if not publication or publication["phase"] in {"changes_required", "verified", "reviewing_publication"}:
        return None
    if not publication.get("authority"):
        return "Content accepted. Record scoped publication authority before dispatch"
    if ('merge' in publication.get('actions', [])
            and set(publication['actions']) <= {'merge', 'verify'}
            and not publication.get('merge_evidence', {}).get('ready')):
        return 'Merge requires a fresh merge-check decision resolving CI, worker verification and authority'
    try:
        if not same_content(publication["accepted_state"], capture_state(task)):
            return "Accepted content changed. Authorise correction and request fresh content review"
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return f"Cannot verify accepted content: {exc}"
    return None


def consumed_actions(observed):
    consumed = []
    if not observed["retained_changes"]:
        consumed.append("commit")
    if observed["remote_head"] == observed["head"]:
        consumed.append("push")
    if observed["pull_requests"]:
        pr = observed["pull_requests"][0]
        if pr["head"] != observed["head"]:
            raise ValueError("Existing PR head differs. Reconcile publication before authorising")
        if pr["state"] == "CLOSED":
            raise ValueError("Closed PR requires explicit operator reconciliation")
        consumed.append("pull_request")
        if "push" not in consumed:
            consumed.append("push")
        if pr["state"] == "MERGED":
            consumed.append("merge")
    return consumed


def authorise(conn, task, authority, actions):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_recovery import observe_publication
    if not actions or set(actions) - {"commit", "push", "pull_request", "merge", "verify"}:
        raise ValueError("Select explicit publication actions: commit, push, pull_request, merge or verify")
    publication = dict(task.publication or {})
    if task.status == "done":
        if conn.execute(
            "WITH RECURSIVE descendants(id) AS (SELECT child_id FROM task_links WHERE parent_id = ? "
            "UNION SELECT l.child_id FROM task_links l JOIN descendants d ON l.parent_id = d.id) "
            "SELECT 1 FROM tasks JOIN descendants USING (id) WHERE status = 'running' LIMIT 1",
            (task.id,),
        ).fetchone():
            raise ValueError("Pause dependent workers before reopening publication")
        submission = latest_submission(conn, task.id)
        completed = kb.latest_run(conn, task.id)
        if (not submission or not completed or completed.outcome != "completed"
                or (completed.metadata or {}).get("review_outcome") != "approved"):
            raise ValueError("Later publication requires recorded independent completion")
        requested = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1", (task.id,),
        ).fetchone()
        provenance = json.loads(requested[0])
        publication = dict(
            phase="accepted_pending_publication", accepted_state=submission["state"],
            implementer=provenance["implementer"], reviewer=completed.profile,
            reviewer_run_id=completed.id,
            deliverable="merge" if "merge" in actions else "pull_request" if "pull_request" in actions else "branch",
        )
    if not publication or publication["phase"] == "changes_required":
        raise ValueError("Independent content acceptance is required before publication")
    if "merge" in actions:
        publication["deliverable"] = "merge"
    elif "pull_request" in actions and publication["deliverable"] == "branch":
        publication["deliverable"] = "pull_request"
    current = capture_state(task)
    if not same_content(publication["accepted_state"], current):
        raise ValueError("Accepted content changed. Authorise correction and request fresh review")
    observed = observe_publication(task)
    consumed = consumed_actions(observed)
    if capture_state(task) != current:
        raise ValueError("Workspace changed during publication reconciliation")
    publication.update(phase="publication_authorised", authority=authority,
                       actions=sorted(set(actions) - set(consumed)),
                       consumed_actions=consumed, reconciliation=observed)
    from hermes_cli.kanban_ci import report, publication_scope
    if 'merge' in publication['actions']:
        publication['merge_scope'] = publication_scope(task, observed)
    publication['merge_evidence'] = report(conn, task, observed, publication)
    if 'merge' in publication['actions']:
        publication.pop('merge_preflight', None)
    save(conn, task.id, publication)
    conn.execute(
        "UPDATE tasks SET status = ?, assignee = ?, completed_at = NULL, result = NULL WHERE id = ?",
        (kb._landing_status_after_parents(conn, task.id), publication["implementer"], task.id),
    )
    kb._append_event(conn, task.id, "publication_authorised", publication)
    if task.status == "done":
        kb.invalidate_descendants_for_parent_reopen(conn, task.id, author="publication authorisation")


def verify_delivery(conn, task, metadata):
    from hermes_cli.kanban_recovery import observe_publication
    publication = task.publication
    if not publication:
        return None
    if not publication.get("authority"):
        return "Publication authority and reconciliation are required"
    current = capture_state(task)
    if not same_content(publication["accepted_state"], current):
        return "Publication content changed. Accept the fresh content pending publication and reconcile authority"
    observed = observe_publication(task)
    if observed["retained_changes"] or (
        publication["deliverable"] != "merge" and observed["remote_head"] != current["head"]
    ):
        return "Promised publication requires a clean workspace and matching remote branch HEAD"
    prs = observed["pull_requests"]
    if publication["deliverable"] in {"pull_request", "merge"}:
        if not prs or prs[0]["head"] != current["head"]:
            return "Promised PR requires a matching published HEAD receipt"
        wanted = "MERGED" if publication["deliverable"] == "merge" else "OPEN"
        if prs[0]["state"] != wanted:
            return f"Promised deliverable requires PR state {wanted}"
    if capture_state(task) != current:
        return "Workspace changed during publication verification"
    from hermes_cli.kanban_ci import report
    metadata['merge_evidence'] = report(conn, task, observed)
    if publication['deliverable'] == 'merge':
        preflight = publication.get('merge_preflight') or {}
        evidence = metadata['merge_evidence']
        if (not preflight.get('ready') or preflight.get('scope') != evidence['scope']
                or preflight.get('policy') != evidence['policy']
                or not preflight.get('merge_authority', {}).get('reference')
                or preflight.get('merge_authority', {}).get('merge') is not True):
            return 'Promised merge requires a retained approved preflight for this exact PR, head, branch and policy'
        unresolved = [point for point in evidence['decision_points'] if point != 'merge_authority_required']
        if unresolved:
            return 'Merged publication has unresolved evidence: ' + ', '.join(unresolved)
    metadata["publication_receipt"] = observed
    return None
