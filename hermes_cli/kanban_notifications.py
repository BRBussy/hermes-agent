"""Delivery-time interpretations of immutable Kanban events.

Recorded state is a snapshot, not a guarantee about state after delivery.
Structured findings remain on the event and card.
"""

import json
import re

from agent.redact import redact_sensitive_text

NOTIFY_KINDS = (
    'progress_warning', 'recovery_required', 'completed', 'blocked', 'gave_up',
    'crashed', 'timed_out', 'status', 'archived', 'unblocked',
    'block_loop_detected', 'review_requested', 'changes_requested', 'publication_pending',
)
_SILENT_KINDS = {'archived', 'unblocked', 'status'}
_STATE_KINDS = set(NOTIFY_KINDS) | {
    'claimed', 'review_reopened', 'publication_authorised', 'recovery_reconciled',
    'reconciled', 'scheduled', 'reclaimed', 'promoted', 'promoted_manual',
    'workspace_disposal', 'descendant_invalidated', 'dependency_wait', 'runtime_failure',
}
_LOCAL_PATH_RE = re.compile(
    r'(?<![\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\s,;]+|[A-Za-z]:\\[^\s,;]+)'
)


def readable_reason(value, limit=240):
    """Extract prose before truncation and redact external-delivery details."""
    def prose(item, depth=0):
        if depth > 8:
            return ''
        if isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith(('{', '[', '```')):
                if stripped.startswith('```'):
                    stripped = '\n'.join(stripped.splitlines()[1:-1])
                try:
                    return prose(json.loads(stripped), depth + 1)
                except (ValueError, TypeError):
                    if stripped.startswith('[') and not stripped.startswith(('[{', '["', '[[')):
                        return item
                    return 'Structured details are available on the card'
            return item
        if isinstance(item, dict):
            for key in ('reason', 'summary', 'message', 'findings', 'error', 'description', 'detail', 'title'):
                result = prose(item.get(key), depth + 1)
                if result:
                    return result
            return 'Structured details are available on the card' if item else ''
        if isinstance(item, list):
            return '. '.join(filter(None, (prose(child, depth + 1) for child in item[:3])))
        return ''

    text = redact_sensitive_text(prose(value), force=True, redact_url_credentials=True)
    text = _LOCAL_PATH_RE.sub('[local path]', text)
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + '...'


def _phase(task, event, run):
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.kind == 'publication_pending':
        return 'publication handoff'
    if event.kind in {'changes_requested', 'completed'} and payload.get('reviewer'):
        return 'review'
    if run and run.step_key:
        return readable_reason(run.step_key, 60)
    if event.kind == 'review_requested':
        return 'review handoff'
    return 'attempt' if event.run_id else 'task transition'


def render_event(task, event, board, *, latest_run=None, later_state=False,
                 run=None, artifact_text='', snapshot_id=None, phase=None):
    if event.kind not in NOTIFY_KINDS or event.kind in _SILENT_KINDS:
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    reason = readable_reason(payload.get('reason') or payload.get('summary') or payload.get('error'))
    earlier_attempt = bool(event.run_id and latest_run and event.run_id != latest_run.id)
    historical = earlier_attempt or later_state
    labels = {
        'completed': ('information', 'done'),
        'blocked': ('needs decision', 'blocked'),
        'gave_up': ('needs decision', 'gave up after repeated spawn failures'),
        'crashed': ('needs decision', 'worker crashed'),
        'timed_out': ('needs decision', 'timed out'),
        'progress_warning': ('information', 'worker activity needs inspection'),
        'recovery_required': ('needs decision', 'recovery requires reconciliation'),
        'block_loop_detected': ('needs decision', 'routed to triage'),
        'review_requested': ('information', 'ready for review'),
        'changes_requested': ('needs decision', 'review requested changes'),
        'publication_pending': ('publication handoff', 'content accepted, publication pending'),
    }
    category, label = labels[event.kind]
    if historical:
        category = 'earlier-attempt history' if earlier_attempt else 'earlier-event history'
    identity = f'[{board or "default"}] Kanban {event.task_id}'
    attempt = f'run {event.run_id}' if event.run_id else 'run not recorded'
    phase = phase or _phase(task, event, run)
    text = f'{identity}: {label}\n{category.capitalize()} | {phase} | {attempt} | event {event.id}'
    if task:
        text += '\n' + readable_reason(task.title, 120)
    if reason:
        text += '\n' + reason
    if event.kind == 'progress_warning':
        text += f'\nRecorded quiet interval: {readable_reason(str(payload.get("quiet_seconds", "unknown")), 24)}s'
    if event.kind == 'timed_out':
        text += f'\nRecorded runtime limit: {readable_reason(str(payload.get("limit_seconds", "unknown")), 24)}s'
    provenance = []
    for key in ('reviewer', 'implementer'):
        if payload.get(key):
            provenance.append(f'{key} @{readable_reason(payload[key], 48)}')
    if provenance:
        text += '\n' + ', '.join(provenance)
    if task:
        current = task.status
        if task.publication:
            current += ', ' + str(task.publication.get('label') or 'publication state recorded on card')
        if task.current_run_id:
            current += f', active run {task.current_run_id}'
        text += f'\nCurrent recorded state: {readable_reason(current, 160)}'
    else:
        text += '\nCurrent state unavailable. Inspect the card before acting'
    if snapshot_id is not None:
        text += f' (observed through event {snapshot_id})'
    if event.kind == 'publication_pending' and not historical:
        text += '\nInspect scoped publication authority and retain fresh published-state review'
    if event.kind in {'crashed', 'timed_out', 'recovery_required', 'progress_warning'} and not historical:
        text += '\nInspect the attempt and recovery requirements'
    if artifact_text:
        text += '\n' + artifact_text
    text += '\nFull structured details remain on the card'
    return {'text': text, 'category': category, 'historical': historical,
            'event_id': event.id, 'event_kind': event.kind, 'run_id': event.run_id, 'snapshot_event_id': snapshot_id}


def notification_for_event(conn, event, board):
    """Read one consistent database snapshot before interpreting an event.

    Filesystem receipts are independently verified observations. Sending cannot
    lock task state across a platform request, so wording identifies its snapshot.
    """
    from hermes_cli import kanban_db as kb

    if event.kind not in NOTIFY_KINDS or event.kind in _SILENT_KINDS:
        return None
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute('BEGIN')
    try:
        task = kb.get_task(conn, event.task_id)
        latest_run = kb.latest_run(conn, event.task_id)
        run = kb.get_run(conn, event.run_id) if event.run_id else None
        events = kb.list_events(conn, event.task_id)
        claims = [item for item in events if item.run_id == event.run_id and item.kind == 'claimed']
        phase = None
        if claims and event.run_id:
            phase = 'review' if (claims[-1].payload or {}).get('source_status') == 'review' else 'development'
        state_events = [item for item in events if item.kind in _STATE_KINDS]
        preceding = [item for item in state_events if item.id < event.id]
        if preceding:
            previous = preceding[-1]
            if event.kind in {'blocked', 'changes_requested', 'progress_warning'} and (previous.kind, previous.run_id, previous.payload) == (event.kind, event.run_id, event.payload):
                return None
        later_state = any(item.id > event.id and item.kind != 'progress_warning'
                          and (item.kind, item.run_id, item.payload) != (event.kind, event.run_id, event.payload)
                          for item in state_events)
        snapshot_id = max((item.id for item in events), default=event.id)
        artifact_text = ''
        artifact_paths = []
        if task and event.kind in {'completed', 'recovery_required'}:
            try:
                from hermes_cli.kanban_disposal import inspect
                from hermes_cli.kanban_evidence import discover
                workspace = inspect(conn, task.id)
                evidence = discover(conn, task.id)
                retained = [item for item in evidence if item.get('status') == 'retained']
                from pathlib import Path
                declared = (event.payload or {}).get('artifacts')
                public_paths = {value for value in declared if isinstance(value, str)} if isinstance(declared, list) else set()
                for item in retained:
                    manifest = item['manifest']
                    if event.run_id and manifest.get('run_id') == event.run_id:
                        artifact_paths.extend(str(Path(item['path']).parent / artifact['storage_name'])
                                              for artifact in manifest['artifacts']
                                              if str(Path(item['path']).parent / artifact['storage_name']) in public_paths)
                partial = any(item.get('manifest', {}).get('provenance', {}).get('kind') == 'partial_recovery' for item in retained)
                unavailable = any(item.get('status') != 'retained' and not item.get('resolved') for item in evidence)
                artifact_text = f'Workspace observation: {workspace["state"]}. '
                if retained:
                    artifact_text += f'Evidence: {len(retained)} verified manifest(s)'
                    if partial:
                        artifact_text += ', includes partial recovery'
                    if unavailable:
                        artifact_text += ', additional evidence unavailable'
                else:
                    artifact_text += 'Evidence: unavailable' if evidence else 'Evidence: no registered receipt'
                artifact_text += '. These observations do not establish complete restoration'
            except (OSError, ValueError, TypeError, KeyError, NotImplementedError, AttributeError, ImportError):
                artifact_text = 'Workspace and evidence verification unavailable. Inspect retained receipts'
        notice = render_event(task, event, board, latest_run=latest_run, later_state=later_state,
                            run=run, artifact_text=artifact_text, snapshot_id=snapshot_id, phase=phase)
        if notice:
            notice['artifact_paths'] = artifact_paths if not notice['historical'] else []
        return notice
    finally:
        if own_transaction:
            conn.rollback()
