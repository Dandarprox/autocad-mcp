"""Tests for the v3.2 robustness improvements (no AutoCAD needed).

Covers:
- stale command cleanup before dispatch
- recovery flag / ESC-only-on-recovery dispatch trigger
- auto-load dispatcher decision path
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend


class TestCleanupStaleCommands:
    def test_removes_leftover_command_files_keeps_results(self):
        backend = FileIPCBackend()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend._ipc_dir = Path(tmpdir)
            (Path(tmpdir) / "autocad_mcp_cmd_aaa.json").write_text("{}")
            (Path(tmpdir) / "autocad_mcp_cmd_bbb.json").write_text("{}")
            (Path(tmpdir) / "autocad_mcp_result_aaa.json").write_text("{}")
            (Path(tmpdir) / "autocad_mcp_lisp_aaa.lsp").write_text("(+ 1 2)")

            backend._cleanup_stale_commands()

            assert not (Path(tmpdir) / "autocad_mcp_cmd_aaa.json").exists()
            assert not (Path(tmpdir) / "autocad_mcp_cmd_bbb.json").exists()
            # result and lisp files must be preserved
            assert (Path(tmpdir) / "autocad_mcp_result_aaa.json").exists()
            assert (Path(tmpdir) / "autocad_mcp_lisp_aaa.lsp").exists()


class TestDispatchTriggerRecovery:
    def test_trigger_sends_esc_when_recovery_needed(self):
        backend = FileIPCBackend()
        backend._needs_recovery = True
        backend._type_string = MagicMock()

        backend._type_dispatch_trigger()

        backend._type_string.assert_called_once_with("(c:mcp-dispatch)", cancel_first=True)
        assert backend._needs_recovery is False

    def test_trigger_skips_esc_when_healthy(self):
        backend = FileIPCBackend()
        backend._needs_recovery = False
        backend._type_string = MagicMock()

        backend._type_dispatch_trigger()

        backend._type_string.assert_called_once_with("(c:mcp-dispatch)", cancel_first=False)
        assert backend._needs_recovery is False


class TestAutoLoadDispatcher:
    @pytest.mark.asyncio
    async def test_auto_load_retries_until_ping_ok(self):
        backend = FileIPCBackend()
        backend._send_esc = MagicMock()
        backend._type_string = MagicMock()

        calls = {"n": 0}

        async def fake_dispatch(command, params):
            calls["n"] += 1
            if calls["n"] < 3:
                return CommandResult(ok=False, error="timeout")
            return CommandResult(ok=True, payload="pong")

        backend._dispatch = fake_dispatch

        result = await backend._auto_load_dispatcher()

        assert result.ok is True
        assert calls["n"] == 3
        # load command must have been typed between each failed ping
        assert backend._type_string.call_count == 3

    @pytest.mark.asyncio
    async def test_auto_load_gives_up_after_three_attempts(self):
        backend = FileIPCBackend()
        backend._send_esc = MagicMock()
        backend._type_string = MagicMock()

        async def fake_dispatch(command, params):
            return CommandResult(ok=False, error="timeout")

        backend._dispatch = fake_dispatch

        result = await backend._auto_load_dispatcher()

        assert result.ok is False
        assert backend._type_string.call_count == 3
