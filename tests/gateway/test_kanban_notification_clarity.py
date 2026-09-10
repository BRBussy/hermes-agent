"""Notification interpretation, current-state races and delivery retries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_notifications import notification_for_event, readable_reason, render_event
from tests.gateway.test_kanban_changes_requested_notifier import RecordingAdapter, _run_one_tick, _runner


@pytest.mark.parametrize('value', [
    {'verdict': 'BLOCK', 'findings': [{'message': 'Fix the missing test'}]},
    json.dumps({'verdict': 'BLOCK', 'findings': [{'message': 'Fix the missing test'}]}),
    '```json\n{"reason": "Fix the missing test"}\n```',
])
def test_structured_findings_are_readable(value):
    assert readable_reason(value) == 'Fix the missing test'


def test_long_findings_redaction_and_unknown_metadata():
    assert readable_reason('[P1] Fix the missing test') == '[P1] Fix the missing test'
    assert len(readable_reason({'findings': ['long finding ' * 100]})) <= 240
    assert '{' not in readable_reason('{invalid json')
    assert readable_reason({'unknown': {'private': 'value'}}) == 'Structured details are available on the card'
    assert '/home/alice' not in readable_reason('Inspect /home/alice/private.json')


def fixture_card(tmp_path, monkeypatch, mode='notify+wake'):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'notifications.db'))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='Notification fixture', assignee='worker', execution_authority='Isolated notification fixture',
            session_id='agent:main:telegram:thread:chat-1:topic-7')
        kb.add_notify_sub(conn, task_id=tid, platform='telegram', chat_id='chat-1',
            thread_id='topic-7', delivery_mode=mode)
    return tid


def event(conn, tid, kind, payload=None, run_id=None):
    kb._append_event(conn, tid, kind, payload, run_id=run_id)
    return kb.list_events(conn, tid)[-1]


def test_recovery_before_delivery_preserves_failure_as_history(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        failure = event(conn, tid, 'blocked', {'reason': json.dumps({'findings': ['Approval needed']})})
        assert kb.complete_task(conn, tid, summary='Recovered successfully')
    adapter = RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 2
    text = adapter.sent[0]['text']
    assert 'Earlier-event history' in text and 'Current recorded state: done' in text
    assert 'Approval needed' in text and '{' not in text
    assert f'event {failure.id}' in text and '[default]' in text
    assert len(adapter.handled) == 1
    assert 'Earlier-event history' in adapter.handled[0].text
    assert 'proof of operator receipt' in adapter.handled[0].text


def test_new_attempt_identity_is_not_current_assignee(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    ev = kb.Event(7, tid, 'crashed', {}, 1, run_id=11)
    view = render_event(task, ev, 'board-a', latest_run=SimpleNamespace(id=12))
    assert view['historical'] and 'Earlier-attempt history' in view['text']
    assert 'run 11' in view['text'] and '@worker' not in view['text']


def test_unchanged_findings_suppressed_but_changes_and_attempts_survive(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        first = event(conn, tid, 'blocked', {'reason': 'Approval required'})
        duplicate = event(conn, tid, 'blocked', {'reason': 'Approval required'})
        different = event(conn, tid, 'blocked', {'reason': 'Credentials required'})
        assert notification_for_event(conn, first, 'default')
        assert notification_for_event(conn, duplicate, 'default') is None
        assert notification_for_event(conn, different, 'default')
        event(conn, tid, 'unblocked')
        retry = event(conn, tid, 'blocked', {'reason': 'Credentials required'})
        assert notification_for_event(conn, retry, 'default')


def test_concurrent_update_between_deliveries_is_resolved_again(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        event(conn, tid, 'blocked', {'reason': 'First problem'})
        event(conn, tid, 'blocked', {'reason': 'Second problem'})
    class UpdatingAdapter(RecordingAdapter):
        async def send(self, chat_id, text, metadata=None):
            await super().send(chat_id, text, metadata)
            if len(self.sent) == 1:
                with kb.connect() as conn:
                    assert kb.complete_task(conn, tid, summary='Recovered during delivery')
    adapter = UpdatingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 2
    assert 'Current recorded state: done' in adapter.sent[1]['text']
    assert 'Earlier-event history' in adapter.sent[1]['text']
    with kb.connect() as conn:
        _, remaining = kb.unseen_events_for_sub(conn, task_id=tid, platform='telegram', chat_id='chat-1', thread_id='topic-7')
        assert any(ev.kind == 'completed' for ev in remaining)


def test_failed_delivery_retries_with_current_state(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        event(conn, tid, 'blocked', {'reason': 'Approval required'})
    adapter = RecordingAdapter(fail_send=True)
    runner = _runner(adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))
    assert not adapter.handled
    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, summary='Recovery completed')
    adapter.fail_send = False
    runner._running = True
    asyncio.run(_run_one_tick(monkeypatch, runner))
    assert 'Earlier-event history' in adapter.sent[1]['text']
    assert 'Current recorded state: done' in adapter.sent[1]['text']


def test_completion_does_not_promise_workspace_or_evidence(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, summary='Source recreated in /home/alice/worktree')
        ev = kb.list_events(conn, tid)[-1]
        notice = notification_for_event(conn, ev, 'default')
    assert 'Workspace observation: unavailable' in notice['text']
    assert 'no registered receipt' in notice['text']
    assert '/home/alice' not in notice['text']
    assert 'do not establish complete restoration' in notice['text']


def test_tui_consumes_shared_state_interpretation(tmp_path, monkeypatch):
    from tui_gateway.server import _collect_kanban_notifications
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='fixture-session')
        event(conn, tid, 'changes_requested', {'reason': {'findings': ['Fix missing assertion']}})
        assert kb.complete_task(conn, tid, summary='Recovered')
    notices = _collect_kanban_notifications({'session_key': 'fixture-session'})
    assert notices and 'Earlier-event history' in notices[0]
    assert 'Fix missing assertion' in notices[0]
    assert _collect_kanban_notifications({'session_key': 'fixture-session'}) == []


def test_silent_progress_and_missing_current_state():
    for kind in ('status', 'archived', 'unblocked'):
        assert render_event(None, kb.Event(1, 'fixture', kind, {}, 1), 'default') is None
    notice = render_event(None, kb.Event(2, 'fixture', 'blocked', {}, 1), 'default')
    assert 'Current state unavailable' in notice['text']


@pytest.mark.parametrize('board_query', ['&board=notification-fixture', ''])
def test_dashboard_stream_carries_interpretation_and_raw_evidence(tmp_path, monkeypatch, board_query):
    import importlib.util
    from pathlib import Path
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        raw = {'reason': {'findings': ['Missing assertion'], 'verdict': 'BLOCK'}}
        event(conn, tid, 'blocked', raw)
        assert kb.complete_task(conn, tid, summary='Recovered')
    path = Path(__file__).resolve().parents[2] / 'plugins/kanban/dashboard/plugin_api.py'
    spec = importlib.util.spec_from_file_location('notification_dashboard_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kb.create_board('notification-fixture')
    kb.set_current_board('notification-fixture')
    module._EVENT_POLL_SECONDS = 0.01
    monkeypatch.setattr(module, '_ws_upgrade_authorized', lambda ws: ws.query_params.get('token') == 'fixture-token')
    app = FastAPI()
    app.include_router(module.router, prefix='/kanban')
    with TestClient(app) as client:
        with client.websocket_connect('/kanban/events?token=fixture-token' + board_query) as socket:
            frame = socket.receive_json()
    failures = [item for item in frame['events'] if item['kind'] == 'blocked']
    assert failures
    assert failures[0]['notification']['text'].startswith('[notification-fixture]')
    assert failures[0]['payload'] == raw
    assert failures[0]['notification']['historical']
    assert 'Current recorded state: done' in failures[0]['notification']['text']


def test_verified_partial_recovery_and_disposal_observations(tmp_path, monkeypatch):
    from hermes_cli import kanban_evidence as evidence
    tid = fixture_card(tmp_path, monkeypatch)
    workspace = tmp_path / 'worktree'
    workspace.mkdir()
    source = workspace / 'report.txt'
    source.write_text('Retained test evidence')
    with kb.connect() as conn:
        conn.execute('UPDATE tasks SET workspace_path = ? WHERE id = ?', (str(workspace), tid))
        evidence.preserve(conn, tid, {'artifacts': [str(source)],
            'evidence_provenance': {'kind': 'partial_recovery'}}, 'completed')
        assert kb.complete_task(conn, tid, summary='Completed with recovered evidence')
        completion = [item for item in kb.list_events(conn, tid) if item.kind == 'completed'][-1]
        directory = evidence._directory(conn, tid)
        (directory / 'disposal-fixture.json').write_text(json.dumps({'request_id': 'fixture', 'outcome': 'removed'}))
        source.unlink()
        workspace.rmdir()
        event(conn, tid, 'workspace_disposal', {'outcome': 'removed'})
        notice = notification_for_event(conn, completion, 'default')
        assert notice['historical']
        assert 'Workspace observation: removed' in notice['text']
        assert 'includes partial recovery' in notice['text']
        assert 'verified manifest' in notice['text']
        workspace.mkdir()
        notice = notification_for_event(conn, completion, 'default')
        assert 'unexpected_replacement' in notice['text']
        records = evidence.discover(conn, tid)
        assert records
        manifest = records[0]['manifest']
        (directory / manifest['artifacts'][0]['storage_name']).write_text('corrupted')
        notice = notification_for_event(conn, completion, 'default')
        assert 'Evidence: unavailable' in notice['text']


def test_attempt_phase_comes_from_recorded_claim(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        ev = event(conn, tid, 'blocked', {'reason': 'Inspect output'}, run_id)
        text = notification_for_event(conn, ev, 'default')['text']
        assert f'development | run {run_id}' in text
        assert kb.complete_task(conn, tid, summary='Resolved')
        text = notification_for_event(conn, ev, 'default')['text']
        assert 'Earlier-event history' in text


def test_partial_batch_retry_does_not_repeat_delivered_failure(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch, 'notify')
    with kb.connect() as conn:
        event(conn, tid, 'blocked', {'reason': 'First finding'})
        event(conn, tid, 'blocked', {'reason': 'Second finding'})
    class SecondSendFails(RecordingAdapter):
        async def send(self, chat_id, text, metadata=None):
            await super().send(chat_id, text, metadata)
            if len(self.sent) == 2:
                raise RuntimeError('Second fixture delivery failed')
    adapter = SecondSendFails()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 3
    assert 'First finding' in adapter.sent[0]['text']
    assert 'Second finding' in adapter.sent[2]['text']


def test_current_same_attempt_duplicate_does_not_mark_failure_resolved(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        first = event(conn, tid, 'blocked', {'reason': 'Still needs approval'})
        event(conn, tid, 'blocked', {'reason': 'Still needs approval'})
        notice = notification_for_event(conn, first, 'default')
        assert not notice['historical']
        assert 'Needs decision' in notice['text']


def test_historical_completion_does_not_upload_paths(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, summary='Earlier completion')
        event(conn, tid, 'workspace_disposal', {'outcome': 'removed'})
    runner = _runner(RecordingAdapter())
    calls = []
    async def upload(**kwargs):
        calls.append(kwargs)
    runner._deliver_kanban_artifacts = upload
    asyncio.run(_run_one_tick(monkeypatch, runner))
    assert runner.adapters
    assert not calls


def test_progress_warning_does_not_wake_coordinator(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    with kb.connect() as conn:
        event(conn, tid, 'progress_warning', {'quiet_seconds': 3600})
    adapter = RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1
    assert 'Information' in adapter.sent[0]['text']
    assert not adapter.handled


def test_native_upload_uses_verified_declared_artifacts_only(tmp_path, monkeypatch):
    tid = fixture_card(tmp_path, monkeypatch)
    workspace = tmp_path / 'artifact-workspace'
    workspace.mkdir()
    (workspace / 'public.txt').write_text('Public fixture deliverable')
    (workspace / '.task-evidence').mkdir()
    (workspace / '.task-evidence/private.txt').write_text('Private fixture test log')
    with kb.connect() as conn:
        conn.execute('UPDATE tasks SET workspace_path = ? WHERE id = ?', (str(workspace), tid))
        assert kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, metadata={'artifacts': ['public.txt'], 'evidence_paths': ['.task-evidence']})
        completion = [item for item in kb.list_events(conn, tid) if item.kind == 'completed'][-1]
        notice = notification_for_event(conn, completion, 'default')
        assert len(notice['artifact_paths']) == 1
        from pathlib import Path
        public = Path(notice['artifact_paths'][0])
        assert public.read_text() == 'Public fixture deliverable'
        public.write_text('Corrupted fixture deliverable')
        assert notification_for_event(conn, completion, 'default')['artifact_paths'] == []
