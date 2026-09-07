"""Tests for Hermes MCP schemas, dispatch and profile memory operations."""

from __future__ import annotations

import inspect
import pytest
from typing import get_args

from agent.transports.hermes_tools_mcp_server import (
    _signature_from_schema,
)


class TestSignatureFromSchema:
    """Test the JSON Schema -> Python signature conversion."""

    def test_simple_required_string_param(self):
        """A required string param becomes str with no default."""
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        sig, annots = _signature_from_schema(schema)

        assert len(sig.parameters) == 1
        param = sig.parameters["query"]
        assert param.name == "query"
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert annots["query"] == str
        assert param.default is inspect.Parameter.empty



    def test_skip_private_params(self):
        """Params starting with '_' are excluded from the signature."""
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "_internal": {"type": "string"},
            },
            "required": ["query", "_internal"],
        }
        sig, annots = _signature_from_schema(schema)

        assert "_internal" not in sig.parameters
        assert "_internal" not in annots
        assert "query" in sig.parameters

    def test_all_json_types(self):
        """All JSON schema types map to correct Python types."""
        schema = {
            "type": "object",
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "a": {"type": "array"},
                "o": {"type": "object"},
            },
            "required": ["s", "i", "n", "b", "a", "o"],
        }
        sig, annots = _signature_from_schema(schema)

        assert annots["s"] == str
        assert annots["i"] == int
        assert annots["n"] == float
        assert annots["b"] == bool
        assert annots["a"] == list
        assert annots["o"] == dict








class TestModuleSurface:
    def test_module_imports_clean(self):
        from agent.transports import hermes_tools_mcp_server as m
        assert callable(m.main)
        assert callable(m._build_server)
        assert isinstance(m.EXPOSED_TOOLS, tuple)
        assert len(m.EXPOSED_TOOLS) > 0

    def test_exposed_tools_are_safe_subset(self):
        """We MUST NOT expose tools codex already has, because codex'
        own builtins are better-integrated with its sandbox + approvals.
        Specifically: no terminal/shell, no read_file/write_file, no
        patch — those are codex's built-in tools."""
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
        forbidden = {
            "terminal", "shell", "read_file", "write_file", "patch",
            "search_files", "process",
        }
        leaked = forbidden & set(EXPOSED_TOOLS)
        assert not leaked, (
            f"these tools must NOT be exposed via the codex callback "
            f"because codex has built-in equivalents: {leaked}"
        )






class TestMain:
    def test_main_returns_2_when_mcp_unavailable(self, monkeypatch):
        """When the mcp package isn't installed, main() should exit
        cleanly with code 2 and an install hint, not crash."""
        import agent.transports.hermes_tools_mcp_server as m

        def boom_build(*a, **kw):
            raise ImportError("mcp not installed")

        monkeypatch.setattr(m, "_build_server", boom_build)
        rc = m.main(["--verbose"])
        assert rc == 2

    def test_main_handles_keyboard_interrupt(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class FakeServer:
            def run(self):
                raise KeyboardInterrupt()

        monkeypatch.setattr(m, "_build_server", lambda: FakeServer())
        rc = m.main([])
        assert rc == 0

    def test_main_returns_1_on_runtime_error(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class CrashingServer:
            def run(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(m, "_build_server", lambda: CrashingServer())
        rc = m.main([])
        assert rc == 1


@pytest.mark.asyncio
class TestMemoryMCP:
    async def test_memory_round_trip_preserves_unrelated_facts(self, tmp_path, monkeypatch):
        import json
        from agent.transports.hermes_tools_mcp_server import _build_server

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        memories = tmp_path / "memories"
        memories.mkdir()
        path = memories / "USER.md"
        path.write_text("Unrelated preference: tea.\n")
        server = _build_server()
        steps = [
            ({"action": "add", "target": "user", "content": "Notebook label: maple."}, "maple"),
            ({"action": "replace", "target": "user", "old_text": "Notebook label:", "content": "Notebook label: birch."}, "birch"),
            ({"action": "remove", "target": "user", "old_text": "Notebook label:"}, None),
        ]
        for args, expected in steps:
            result = await server.call_tool("memory", args)
            data = json.loads(result.content[0].text)
            assert data["success"] is True, data
            assert not data.get("staged")
            content = path.read_text()
            assert "Unrelated preference: tea." in content
            if expected:
                assert expected in content
            else:
                assert "Notebook label:" not in content

    async def test_memory_rechecks_disabled_target(self, tmp_path, monkeypatch):
        import json
        from agent.transports.hermes_tools_mcp_server import _build_server
        from hermes_cli.config import load_config, save_config

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        server = _build_server()
        config = load_config()
        config.setdefault("memory", {})["user_profile_enabled"] = False
        save_config(config)
        result = await server.call_tool("memory", {"action": "add", "target": "user", "content": "Must not persist."})
        data = json.loads(result.content[0].text)
        assert data["success"] is False
        assert not (tmp_path / "memories" / "USER.md").exists()

    async def test_memory_honours_character_limit(self, tmp_path, monkeypatch):
        import json
        from agent.transports.hermes_tools_mcp_server import _build_server
        from hermes_cli.config import load_config, save_config

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config = load_config()
        config.setdefault("memory", {})["user_char_limit"] = 12
        save_config(config)
        server = _build_server()
        result = await server.call_tool("memory", {"action": "add", "target": "user", "content": "This entry exceeds the configured limit."})
        data = json.loads(result.content[0].text)
        assert data["success"] is False
        assert not (tmp_path / "memories" / "USER.md").exists()

    async def test_memory_stages_writes_when_approval_is_required(self, tmp_path, monkeypatch):
        import json
        from agent.transports.hermes_tools_mcp_server import _build_server
        from hermes_cli.config import load_config, save_config

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config = load_config()
        config.setdefault("memory", {})["write_approval"] = True
        save_config(config)
        server = _build_server()
        result = await server.call_tool("memory", {"action": "add", "target": "user", "content": "Pending preference."})
        data = json.loads(result.content[0].text)
        assert data["staged"] is True
        assert data["pending_id"]
        assert not (tmp_path / "memories" / "USER.md").exists()
