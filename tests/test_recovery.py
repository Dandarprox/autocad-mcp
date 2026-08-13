"""Tests for the v3.2 robustness improvements (no AutoCAD needed).

Covers:
- stale command cleanup before dispatch
- recovery flag / ESC-only-on-recovery dispatch trigger
- auto-load dispatcher decision path
"""

import tempfile
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends import file_ipc
from autocad_mcp.backends.file_ipc import FileIPCBackend


class TestCleanupStaleCommands:
    def test_removes_leftover_command_files_keeps_results(self):
        backend = FileIPCBackend()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend._ipc_dir = Path(tmpdir)
            session = backend._session_id
            (Path(tmpdir) / f"autocad_mcp_cmd_{session}_aaa.json").write_text("{}")
            (Path(tmpdir) / "autocad_mcp_cmd_other_bbb.json").write_text("{}")
            (Path(tmpdir) / f"autocad_mcp_result_{session}_aaa.json").write_text("{}")
            (Path(tmpdir) / f"autocad_mcp_lisp_{session}_aaa.lsp").write_text("(+ 1 2)")

            backend._cleanup_stale_commands()

            assert not (Path(tmpdir) / f"autocad_mcp_cmd_{session}_aaa.json").exists()
            assert (Path(tmpdir) / "autocad_mcp_cmd_other_bbb.json").exists()
            assert (Path(tmpdir) / f"autocad_mcp_result_{session}_aaa.json").exists()
            assert (Path(tmpdir) / f"autocad_mcp_lisp_{session}_aaa.lsp").exists()


class TestDispatchLease:
    @pytest.mark.asyncio
    async def test_lease_serializes_processes_and_checks_ownership(self):
        first = FileIPCBackend()
        second = FileIPCBackend()
        with tempfile.TemporaryDirectory() as tmpdir:
            first._ipc_dir = Path(tmpdir)
            second._ipc_dir = Path(tmpdir)

            acquired = await first._acquire_dispatch_lease("first_request", "ping")
            assert acquired["acquired"] is True
            assert second._read_dispatch_lease()["session_id"] == first._session_id

            second._release_dispatch_lease()
            assert first._lease_path().exists()

            first._release_dispatch_lease()
            acquired = await second._acquire_dispatch_lease("second_request", "ping")
            assert acquired["acquired"] is True
            second._release_dispatch_lease()

    @pytest.mark.asyncio
    async def test_stale_lease_reclaims_only_abandoned_session(self):
        backend = FileIPCBackend()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend._ipc_dir = Path(tmpdir)
            dead_session = "dead_session"
            lease_path = backend._lease_path()
            lease_path.write_text(json.dumps({"session_id": dead_session}), encoding="utf-8")
            old_time = time.time() - file_ipc.LEASE_STALE_THRESHOLD - 1
            os.utime(lease_path, (old_time, old_time))
            abandoned = Path(tmpdir) / f"autocad_mcp_cmd_{dead_session}_old.json"
            abandoned.write_text("{}")
            unrelated = Path(tmpdir) / "autocad_mcp_cmd_other_live.json"
            unrelated.write_text("{}")

            acquired = await backend._acquire_dispatch_lease("new_request", "ping")

            assert acquired["acquired"] is True
            assert acquired["reclaimed"] is True
            assert not abandoned.exists()
            assert unrelated.exists()
            backend._release_dispatch_lease()

    @pytest.mark.asyncio
    async def test_result_timeout_includes_phase_diagnostics(self, monkeypatch):
        backend = FileIPCBackend()
        backend._type_dispatch_trigger = lambda: True
        backend._send_esc = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend._ipc_dir = Path(tmpdir)
            monkeypatch.setattr(file_ipc, "TIMEOUT", 0.01)
            monkeypatch.setattr(file_ipc, "POLL_INTERVAL", 0.001)

            result = await backend._dispatch_unlocked("ping", {})

            assert result.ok is False
            assert result.details["phase"] == "result_poll"
            assert result.details["request_id"].startswith(backend._session_id)
            assert result.details["recovery_sent"] is True

    @pytest.mark.asyncio
    async def test_lease_wait_timeout_is_distinct(self, monkeypatch):
        backend = FileIPCBackend()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend._ipc_dir = Path(tmpdir)
            backend._lease_path().write_text(json.dumps({"session_id": "live"}), encoding="utf-8")
            monkeypatch.setattr(file_ipc, "TIMEOUT", 0.01)
            monkeypatch.setattr(file_ipc, "POLL_INTERVAL", 0.001)

            result = await backend._dispatch_unlocked("ping", {})

            assert result.ok is False
            assert result.details["phase"] == "lease_wait"
            assert result.details["lease_wait_seconds"] >= 0


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
