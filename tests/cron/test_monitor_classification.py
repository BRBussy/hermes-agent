import json
from types import SimpleNamespace

import pytest

from cron.scripts import classify_items


@pytest.mark.parametrize("scores", [[], [{"score": 9}], [{"index": 0, "score": 11}],
    [{"index": 0, "score": True}], [{"index": True, "score": 9}],
    [{"index": 0, "score": 9}, {"index": 0, "score": 8}]])
def test_malformed_nonempty_batch_is_an_error(scores, tmp_path, monkeypatch, capsys):
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"title": "Outage"}, {"title": "Newsletter"}]))
    monkeypatch.setattr("sys.argv", ["classify_items", "--criteria", "Service outages", "--input-file", str(items)])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(scores)))]))
    assert classify_items.main() == 5
    assert "one valid score per item" in capsys.readouterr().err


def test_scoring_contract_and_threshold(tmp_path, monkeypatch, capsys):
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"title": "Outage"}, {"title": "Newsletter"}]))
    monkeypatch.setattr("sys.argv", ["classify_items", "--criteria", "Service outages", "--input-file", str(items), "--format", "json"])
    def classify(**kwargs):
        messages = kwargs["messages"]
        assert messages and messages[0]["role"] == "system"
        assert all(word in messages[0]["content"] for word in ["index", "score", "0", "10"])
        assert "Service outages" in messages[1]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([{"index": 0, "score": 9}, {"index": 1, "score": 1}])))])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", classify)
    assert classify_items.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output) == 1 and output[0]["item"]["title"] == "Outage"
