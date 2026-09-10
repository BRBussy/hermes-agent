import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_worker_identity import board
from tests.hermes_cli.test_kanban_transfer import kanban_root


def create(conn):
    return kb.create_task(conn, title='Blocker identity fixture', body='Keep implementation, review and publication scope',
                          assignee='worker', execution_authority='Isolated recurrence experiment')


def report(conn, tid, identity='review-approval', reason='Approve implementation review', kind='needs_input'):
    task = kb.claim_task(conn, tid)
    assert task
    assert kb.block_task(conn, tid, blocker_id=identity, reason=reason, kind=kind,
                         expected_run_id=task.current_run_id)
    return kb.get_task(conn, tid)


def test_distinct_needs_input_and_interleaved_recurrence(board):
    tid = create(board)
    assert report(board, tid).block_recurrences == 1
    assert kb.unblock_task(board, tid)
    task = report(board, tid, 'publication-approval', 'Approve simulated publication')
    assert task.status == 'blocked' and task.block_recurrences == 1
    assert kb.unblock_task(board, tid)
    task = report(board, tid, reason='Implementation decision still outstanding', kind='capability')
    assert task.status == 'blocked' and task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    events = kb.list_events(board, tid)
    assert events and events[-1].kind == 'block_loop_detected'
    assert events[-1].payload['blocker_id'] == 'review-approval'
    assert not kb.unblock_task(board, tid)
    for _ in range(3):
        assert not kb.dispatch_once(board, spawn_fn=lambda *_: pytest.fail('Escalation dispatched')).spawned
    assert kb.get_task(board, tid).status == 'blocked'
    assert kb.get_task(board, tid).body == 'Keep implementation, review and publication scope'


def test_resolution_new_episode_and_restart(board):
    tid = create(board)
    report(board, tid)
    assert kb.unblock_task(board, tid)
    report(board, tid)
    path = board.execute('PRAGMA database_list').fetchone()[2]
    with kb.connect(Path(path)) as restarted:
        assert not kb.unblock_task(restarted, tid)
        assert kb.unblock_task(restarted, tid, resolved_blocker_id='review-approval', resolution='Operator approved the reviewed implementation')
    task = report(board, tid)
    issue = task.blocker_state['issues']['review-approval']
    assert issue['count'] == 1 and issue['episode'] == 2 and not issue['resolved']
    receipts = [e for e in kb.list_events(board, tid) if e.kind == 'blocker_resolved']
    assert len(receipts) == 1 and receipts[0].payload['recurrences'] == kb.BLOCK_RECURRENCE_LIMIT


@pytest.mark.parametrize('kind', [None, 'needs_input'])
def test_category_only_migration_retains_unknown_history(board, kind):
    tid = create(board)
    board.execute('UPDATE tasks SET block_kind=?, block_recurrences=1 WHERE id=?', (kind, tid))
    board.commit()
    task = report(board, tid, 'new-condition', kind=kind)
    assert task.block_recurrences == 1
    assert task.blocker_state['issues']['unspecified']['count'] == 1
    assert kb.unblock_task(board, tid)
    task = report(board, tid, None, kind=kind)
    assert task.block_recurrences == 2
    assert not kb.unblock_task(board, tid)


def test_resolution_validation_and_stale_worker_are_atomic(board):
    tid = create(board)
    task = report(board, tid)
    before = task.blocker_state
    with pytest.raises(ValueError):
        kb.unblock_task(board, tid, resolved_blocker_id='review-approval', resolution=' ')
    with pytest.raises(ValueError):
        kb.unblock_task(board, tid, resolved_blocker_id='wrong', resolution='Done')
    assert kb.get_task(board, tid).blocker_state == before
    assert kb.unblock_task(board, tid)
    current = kb.claim_task(board, tid)
    assert current
    assert not kb.block_task(board, tid, blocker_id='other', expected_run_id=-1)
    assert kb.get_task(board, tid).blocker_state == before


def test_operator_holds_preserve_identity_and_do_not_count(board):
    tid = create(board)
    task = report(board, tid)
    assert kb.unblock_task(board, tid)
    for _ in range(3):
        assert kb.block_task(board, tid, reason='Operator pause')
        assert kb.get_task(board, tid).blocker_state == task.blocker_state
        assert kb.unblock_task(board, tid)


def test_real_worker_tool_process_and_notification(board):
    from hermes_cli.kanban_notifications import notification_for_event
    tid = create(board)
    for attempt in range(2):
        if attempt:
            assert kb.unblock_task(board, tid)
        task = kb.claim_task(board, tid)
        assert task
        env = dict(os.environ, HERMES_KANBAN_TASK=tid, HERMES_KANBAN_RUN_ID=str(task.current_run_id),
                   HERMES_KANBAN_CLAIM_LOCK=task.claim_lock)
        code = "from tools.kanban_tools import _handle_block; print(_handle_block({'reason':'Grant fixture approval','kind':'needs_input','blocker_id':'fixture-approval'}))"
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload['status'] == 'blocked', payload
        assert payload['blocker_state']['issues']['fixture-approval']['count'] == attempt + 1
    events = kb.list_events(board, tid)
    assert events
    notice = notification_for_event(board, events[-1], 'default')
    assert 'fixture-approval' in notice['text']
    assert 'Grant fixture approval' in notice['text']
    assert 'record its blocker ID and resolution' in notice['text']
    assert 'triage' not in notice['text']


def test_cli_and_dashboard_serialise_identity(board):
    from hermes_cli.kanban import _task_to_dict
    tid = create(board)
    task = report(board, tid)
    assert _task_to_dict(task)['blocker_state'] == task.blocker_state
    from tools.kanban_tools import _task_summary_dict
    assert _task_summary_dict(kb, board, task)['blocker_state'] == task.blocker_state


def test_dashboard_recovery_requires_resolution(board):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router, _task_dict
    tid = create(board)
    report(board, tid)
    assert kb.unblock_task(board, tid)
    report(board, tid)
    assert _task_dict(kb.get_task(board, tid))['blocker_state']['active_id'] == 'review-approval'
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.patch('/tasks/' + tid, json={'status': 'ready'}).status_code == 409
        result = client.patch('/tasks/' + tid, json={'status': 'ready', 'resolved_blocker_id': 'review-approval',
                                                     'resolution': 'Operator approved fixture review'})
        assert result.status_code == 200, result.text
    assert kb.get_task(board, tid).status == 'ready'


@pytest.mark.parametrize('status, operation', [('ready', 'claim_task'), ('review', 'claim_review_task')])
def test_claim_rejects_escalation_after_alternative_status_write(board, status, operation):
    tid = create(board)
    report(board, tid)
    assert kb.unblock_task(board, tid)
    report(board, tid)
    board.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    board.commit()
    assert getattr(kb, operation)(board, tid) is None


def test_schema_upgrade_exposes_existing_counter(board):
    tid = create(board)
    board.execute('ALTER TABLE tasks DROP COLUMN blocker_state')
    board.execute('UPDATE tasks SET block_recurrences=1 WHERE id=?', (tid,))
    board.commit()
    path = board.execute('PRAGMA database_list').fetchone()[2]
    kb._INITIALIZED_PATHS.discard(str(Path(path).resolve()))
    with kb.connect(Path(path)) as restarted:
        task = kb.get_task(restarted, tid)
        assert task.blocker_state['issues']['unspecified']['count'] == 1
        assert report(restarted, tid, None).block_recurrences == 2


def test_cli_resolution_and_worker_show(board, capsys):
    from hermes_cli import kanban
    from types import SimpleNamespace
    from tools.kanban_tools import _handle_show
    tid = create(board)
    report(board, tid)
    visible = json.loads(_handle_show({'task_id': tid}))
    assert visible['task']['blocker_state']['active_id'] == 'review-approval'
    args = SimpleNamespace(task_ids=[tid], reason=None, resolved_blocker_id='review-approval', resolution='Reviewed and approved')
    assert kanban._cmd_unblock(args) == 0
    assert 'Unblocked' in capsys.readouterr().out
    assert kb.get_task(board, tid).blocker_state['issues']['review-approval']['resolved']


def test_completion_clears_counters_and_keeps_events(board):
    tid = create(board)
    report(board, tid)
    assert kb.complete_task(board, tid, summary='Fixture completed with operator authority')
    task = kb.get_task(board, tid)
    assert task.block_recurrences == 0 and not task.blocker_state['issues']
    assert any(e.kind == 'blocked' and e.payload['blocker_id'] == 'review-approval' for e in kb.list_events(board, tid))


def test_worker_cli_accepts_identity_option(board, monkeypatch, capsys):
    import argparse
    from hermes_cli import kanban
    tid = create(board)
    task = kb.claim_task(board, tid)
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers())
    args = parser.parse_args(['kanban', 'block', tid, 'Fixture approval required', '--blocker-id', 'cli-approval', '--kind', 'needs_input'])
    assert kanban.kanban_command(args) == 0
    assert kb.get_task(board, tid).blocker_state['active_id'] == 'cli-approval'
    assert 'cli-approval' in capsys.readouterr().out



def test_board_transfer_preserves_unresolved_episode(kanban_root, tmp_path):
    from hermes_cli import kanban_transfer as transfer
    kb.create_board('recurrence-source')
    with kb.connect_closing(board='recurrence-source') as conn:
        tid = create(conn)
        report(conn, tid)
        assert kb.unblock_task(conn, tid)
        task = report(conn, tid)
        state = task.blocker_state
    archive = transfer.export_board('recurrence-source', str(tmp_path / 'recurrence'))['archive']
    kanban_root('restored')
    imported = transfer.import_board(archive)
    with kb.connect_closing(board=imported['board']) as conn:
        assert kb.get_task(conn, tid).blocker_state == state
        assert not kb.unblock_task(conn, tid)
        assert kb.unblock_task(conn, tid, resolved_blocker_id='review-approval', resolution='Operator supplied approval after transfer')


def test_category_only_triage_card_recovers_with_resolution(board):
    tid = create(board)
    board.execute("UPDATE tasks SET status='triage', block_kind='needs_input', block_recurrences=2 WHERE id=?", (tid,))
    kb._append_event(board, tid, 'block_loop_detected', {'reason': 'Retained category-only blocker', 'recurrences': 2})
    board.commit()
    assert not kb.unblock_task(board, tid)
    assert kb.unblock_task(board, tid, resolved_blocker_id='unspecified', resolution='Operator reconciled the recorded blockers')
    assert report(board, tid, 'new-explicit-condition').block_recurrences == 1


def test_gateway_notifier_delivers_escalation_cause_and_action(board, monkeypatch):
    from tests.gateway.test_kanban_changes_requested_notifier import RecordingAdapter, _run_one_tick, _runner
    tid = create(board)
    kb.add_notify_sub(board, task_id=tid, platform='telegram', chat_id='isolated-fixture', delivery_mode='notify')
    report(board, tid)
    assert kb.unblock_task(board, tid)
    report(board, tid, reason='Implementation approval still required')
    adapter = RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert adapter.sent
    messages = [item['text'] for item in adapter.sent]
    assert any('held after repeated unresolved blocker' in text and 'review-approval' in text
               and 'record its blocker ID and resolution' in text for text in messages)
