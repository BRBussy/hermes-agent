"""Accepted publication notifications preserve routing without defect wording."""

import asyncio

import pytest

from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_changes_requested_notifier import RecordingAdapter, _run_one_tick, _runner


@pytest.mark.parametrize("mode", ["notify", "wake", "notify+wake"])
def test_publication_acceptance_is_actionable_and_not_a_defect(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "publication.db"))
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Accepted fixture",
                                 session_id="agent:main:telegram:thread:chat-1:topic-7")
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1",
                          thread_id="topic-7", chat_type="thread", delivery_mode=mode,
                          delivery_metadata={"thread_id": "topic-7", "chat_type": "thread"})
        kb._append_event(conn, task_id, "publication_pending", {
            "summary": "Reviewed content is ready", "deliverable": "pull_request",
        })
    adapter = RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    texts = [item["text"] for item in adapter.sent] + [item.text for item in adapter.handled]
    assert texts
    assert all("content accepted" in text.lower() for text in texts)
    assert all("BLOCK" not in text and "implementation is not approved" not in text for text in texts)
    assert len(adapter.sent) == (mode != "wake")
    assert len(adapter.handled) == (mode != "notify")
    if adapter.handled:
        assert adapter.handled[0].source.thread_id == "topic-7"
        assert "scoped publication authority" in adapter.handled[0].text
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == (mode != "wake")
    assert len(adapter.handled) == (mode != "notify")
