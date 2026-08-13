"""File-based IPC backend for AutoCAD LT.

Protocol:
1. Python writes JSON command to C:/temp/autocad_mcp_cmd_{request_id}.json
2. Python types the fixed string "(c:mcp-dispatch)" + Enter
3. LISP reads cmd, dispatches via command map, writes result to
   C:/temp/autocad_mcp_result_{request_id}.json
4. Python polls for result file (100ms intervals, 10s timeout)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import structlog

from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult
from autocad_mcp.config import IPC_DIR, IPC_TIMEOUT, LISP_DIR

log = structlog.get_logger()

# IPC settings
POLL_INTERVAL = 0.1  # seconds
TIMEOUT = IPC_TIMEOUT  # seconds (configurable via AUTOCAD_MCP_IPC_TIMEOUT)
STALE_THRESHOLD = 60.0  # clean up files older than this
LEASE_FILENAME = "autocad_mcp_dispatch.lock"
LEASE_STALE_THRESHOLD = max(5.0, POLL_INTERVAL * 20)


def find_autocad_window() -> int | None:
    """Find the AutoCAD LT window handle by checking window titles."""
    if sys.platform != "win32":
        return None
    try:
        import win32gui

        windows: list[int] = []

        def callback(hwnd, result):
            if win32gui.IsWindowVisible(hwnd):
                text = win32gui.GetWindowText(hwnd).lower()
                if "autocad" in text and ("drawing" in text or ".dwg" in text):
                    result.append(hwnd)
            return True

        win32gui.EnumWindows(callback, windows)
        return windows[0] if windows else None
    except ImportError:
        return None


class FileIPCBackend(AutoCADBackend):
    """File-based IPC with AutoCAD LT via mcp_dispatch.lsp."""

    def __init__(self):
        self._session_id = uuid.uuid4().hex[:12]
        self._hwnd: int | None = None
        self._command_hwnd: int | None = None
        self._ipc_dir = Path(IPC_DIR)
        self._screenshot_provider = None
        self._lock = asyncio.Lock()  # Single in-flight command
        self._needs_recovery = False  # Set when a dispatch times out
        self._escape_targets: list[int] = []  # HWNDs used for ESC cancellation
        self._lease_token: str | None = None

    @property
    def name(self) -> str:
        return "file_ipc"

    @property
    def capabilities(self) -> BackendCapabilities:
        caps = BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=True,
            can_zoom=True,
            can_query_entities=True,
            can_file_operations=True,
            can_undo=True,
        )
        caps.operations = {
                "drawing": [
                    "create", "open", "info", "save", "save_as_dxf", "plot_pdf", "purge",
                    "get_variables", "undo", "redo",
                ],
                "entity": [
                    "create_line", "create_circle", "create_polyline", "create_rectangle",
                    "create_arc", "create_ellipse", "create_mtext", "create_hatch",
                    "list", "count", "get", "copy", "move", "rotate", "scale", "mirror",
                    "offset", "array", "fillet", "chamfer", "erase",
                ],
                "layer": [
                    "list", "create", "set_current", "set_properties", "freeze", "thaw",
                    "lock", "unlock",
                ],
                "block": [
                    "list", "insert", "insert_with_attributes", "get_attributes",
                    "update_attribute",
                ],
                "annotation": [
                    "create_text", "create_dimension_linear", "create_dimension_aligned",
                    "create_dimension_angular", "create_dimension_radius", "create_leader",
                ],
                "pid": [
                    "setup_layers", "insert_symbol", "list_symbols", "draw_process_line",
                    "connect_equipment", "add_flow_arrow", "add_equipment_tag",
                    "add_line_number", "insert_valve", "insert_instrument", "insert_pump",
                    "insert_tank",
                ],
                "view": ["zoom_extents", "zoom_window", "get_screenshot"],
                "system": ["execute_lisp"],
                "reference": [
                    "capture", "inspect", "create_workspace", "duplicate", "snapshot",
                    "clear_proposal", "reset",
                ],
        }
        return caps

    async def initialize(self) -> CommandResult:
        """Find AutoCAD window and verify dispatcher is loaded."""
        self._hwnd = find_autocad_window()
        if not self._hwnd:
            return CommandResult(ok=False, error="AutoCAD LT window not found")

        # Set up screenshot provider
        try:
            from autocad_mcp.screenshot import Win32ScreenshotProvider

            self._screenshot_provider = Win32ScreenshotProvider(self._hwnd)
        except Exception:
            pass

        # Find command-line child edit control for focus-free dispatch
        self._command_hwnd = self._find_command_line_hwnd()
        self._escape_targets = self._find_escape_targets()
        log.info("command_line_hwnd", hwnd=self._command_hwnd)

        # Ensure IPC directory exists
        self._ipc_dir.mkdir(parents=True, exist_ok=True)

        # Clean up stale IPC files
        self._cleanup_stale_files()

        # Verify the dispatcher is loaded; auto-load it if it isn't
        result = await self._dispatch("ping", {})
        if not result.ok:
            result = await self._auto_load_dispatcher()

        if not result.ok:
            lisp_path = str(LISP_DIR / "mcp_dispatch.lsp").replace("\\", "/")
            return CommandResult(
                ok=False,
                error=(
                    "AutoCAD LT detected but mcp_dispatch.lsp could not be loaded.\n"
                    f'In AutoCAD command line, type:\n  (load "{lisp_path}")\n'
                    "Or add lisp-code/ to trusted paths for auto-loading."
                ),
            )

        return CommandResult(ok=True, payload={"backend": "file_ipc", "hwnd": self._hwnd})

    async def _auto_load_dispatcher(self) -> CommandResult:
        """Load mcp_dispatch.lsp by typing (load ...) into AutoCAD, then re-ping.

        Recovers from the common "dispatcher not loaded" first-run failure
        without requiring manual intervention in the AutoCAD UI.
        """
        lisp_path = str(LISP_DIR / "mcp_dispatch.lsp").replace("\\", "/")
        load_cmd = f'(load "{lisp_path}")'
        log.info("auto_load_dispatcher", path=lisp_path)

        result = CommandResult(ok=False, error="dispatcher auto-load failed")
        for attempt in range(1, 4):
            self._send_esc(count=2)
            time.sleep(0.1)
            self._type_string(load_cmd)
            await asyncio.sleep(1.5)  # let AutoCAD finish loading
            result = await self._dispatch("ping", {})
            if result.ok:
                log.info("dispatcher_auto_loaded", attempt=attempt)
                return result

        return result

    async def status(self) -> CommandResult:
        info = {
            "backend": "file_ipc",
            "hwnd": self._hwnd,
            "ipc_dir": str(self._ipc_dir),
            "session_id": self._session_id,
            "lease_file": str(self._lease_path()),
            "lease_owned": self._lease_token is not None,
            "capabilities": {k: v for k, v in self.capabilities.__dict__.items()},
        }
        return CommandResult(ok=True, payload=info)

    # --- IPC dispatch ---

    async def _dispatch(self, command: str, params: dict) -> CommandResult:
        """Send a command via file IPC and wait for result."""
        async with self._lock:
            return await self._dispatch_unlocked(command, params)

    async def _dispatch_unlocked(self, command: str, params: dict) -> CommandResult:
        """Core IPC logic (must be called under _lock)."""
        started = time.monotonic()
        request_id = f"{self._session_id}_{uuid.uuid4().hex[:12]}"
        cmd_file = self._ipc_dir / f"autocad_mcp_cmd_{request_id}.json"
        result_file = self._ipc_dir / f"autocad_mcp_result_{request_id}.json"
        tmp_file = cmd_file.with_suffix(".tmp")
        lease = await self._acquire_dispatch_lease(request_id, command)
        if not lease["acquired"]:
            return CommandResult(
                ok=False,
                error="IPC dispatch lease timeout",
                details={
                    "phase": "lease_wait",
                    "session_id": self._session_id,
                    "request_id": request_id,
                    "command": command,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "timeout_seconds": TIMEOUT,
                    "lease_wait_seconds": lease["waited_seconds"],
                    "lease_reclaimed": lease["reclaimed"],
                },
            )

        result_seen = False
        malformed_results = 0
        recovery_sent = False
        try:
            # Strip None values — the simple LISP JSON parser can't handle null
            clean_params = {k: v for k, v in params.items() if v is not None}

            # Atomic write: write to .tmp, then rename
            payload = {
                "request_id": request_id,
                "command": command,
                "params": clean_params,
                "ts": time.time(),
            }
            try:
                tmp_file.write_text(json.dumps(payload), encoding="utf-8")
                tmp_file.rename(cmd_file)
            except OSError as exc:
                return CommandResult(
                    ok=False,
                    error=f"IPC command file write failed: {exc}",
                    details={
                        "phase": "command_write",
                        "session_id": self._session_id,
                        "request_id": request_id,
                        "command": command,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "lease_reclaimed": lease["reclaimed"],
                    },
                )

            # Type the fixed dispatch trigger
            if not self._type_dispatch_trigger():
                return CommandResult(
                    ok=False,
                    error="Could not trigger AutoCAD dispatcher",
                    details={
                        "phase": "dispatch_trigger",
                        "session_id": self._session_id,
                        "request_id": request_id,
                        "command": command,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "lease_reclaimed": lease["reclaimed"],
                    },
                )

            # Poll for result
            deadline = time.time() + TIMEOUT
            while time.time() < deadline:
                if not self._refresh_dispatch_lease():
                    return CommandResult(
                        ok=False,
                        error="IPC dispatch lease was lost",
                        details={
                            "phase": "recovery",
                            "session_id": self._session_id,
                            "request_id": request_id,
                            "command": command,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "timeout_seconds": TIMEOUT,
                            "result_seen": result_seen,
                            "malformed_results": malformed_results,
                        },
                    )
                if result_file.exists():
                    result_seen = True
                    try:
                        # AutoCAD LISP writes files in Windows-1252 encoding;
                        # try UTF-8 first (covers ASCII), fall back to cp1252
                        try:
                            text = result_file.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            text = result_file.read_text(encoding="cp1252")
                        data = json.loads(text)
                        # Verify request_id matches
                        if data.get("request_id") == request_id:
                            return CommandResult(
                                ok=data.get("ok", False),
                                payload=data.get("payload"),
                                error=data.get("error"),
                                details=data.get("details"),
                            )
                    except (json.JSONDecodeError, OSError):
                        malformed_results += 1  # File may be partially written, retry
                await asyncio.sleep(POLL_INTERVAL)

            # Timeout: clear any stuck command and flag recovery for the next
            # dispatch so the cascade of failures stops here.
            self._needs_recovery = True
            self._send_esc(count=3)
            recovery_sent = True
            log.warning("dispatch_timeout", request_id=request_id, command=command)
            return CommandResult(
                ok=False,
                error=f"Timeout waiting for result (request_id={request_id})",
                details={
                    "phase": "result_poll",
                    "session_id": self._session_id,
                    "request_id": request_id,
                    "command": command,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "timeout_seconds": TIMEOUT,
                    "result_seen": result_seen,
                    "malformed_results": malformed_results,
                    "recovery_sent": recovery_sent,
                    "lease_reclaimed": lease["reclaimed"],
                },
            )

        finally:
            # Cleanup
            for f in (cmd_file, result_file, tmp_file):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
            self._release_dispatch_lease()

    def _lease_path(self) -> Path:
        return self._ipc_dir / LEASE_FILENAME

    async def _acquire_dispatch_lease(self, request_id: str, command: str) -> dict[str, object]:
        """Acquire the shared AutoCAD dispatch lease without blocking the loop."""
        started = time.monotonic()
        reclaimed = False
        lease_path = self._lease_path()
        lease_data = {
            "token": uuid.uuid4().hex,
            "session_id": self._session_id,
            "pid": os.getpid(),
            "request_id": request_id,
            "command": command,
            "created_at": time.time(),
        }

        while True:
            try:
                fd = os.open(str(lease_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as lease_file:
                    json.dump(lease_data, lease_file)
                self._lease_token = str(lease_data["token"])
                return {
                    "acquired": True,
                    "waited_seconds": round(time.monotonic() - started, 3),
                    "reclaimed": reclaimed,
                }
            except FileExistsError:
                existing = self._read_dispatch_lease()
                if self._lease_is_stale(lease_path):
                    if self._reclaim_dispatch_lease(existing):
                        reclaimed = True
                        continue

                if time.monotonic() - started >= TIMEOUT:
                    return {
                        "acquired": False,
                        "waited_seconds": round(time.monotonic() - started, 3),
                        "reclaimed": reclaimed,
                    }
                await asyncio.sleep(POLL_INTERVAL)
            except OSError:
                return {
                    "acquired": False,
                    "waited_seconds": round(time.monotonic() - started, 3),
                    "reclaimed": reclaimed,
                }

    def _read_dispatch_lease(self) -> dict | None:
        try:
            return json.loads(self._lease_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _lease_is_stale(lease_path: Path) -> bool:
        try:
            return time.time() - lease_path.stat().st_mtime > LEASE_STALE_THRESHOLD
        except OSError:
            return False

    def _reclaim_dispatch_lease(self, existing: dict | None) -> bool:
        """Remove a stale lease and its abandoned session files."""
        lease_path = self._lease_path()
        try:
            lease_path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False

        if existing:
            self._cleanup_session_files(existing.get("session_id"), stale_only=False)
        return True

    def _refresh_dispatch_lease(self) -> bool:
        """Refresh the lease heartbeat if this request still owns it."""
        if not self._lease_token:
            return False
        current = self._read_dispatch_lease()
        if not current or current.get("token") != self._lease_token:
            return False
        try:
            now = time.time()
            os.utime(self._lease_path(), (now, now))
            return True
        except OSError:
            return False

    def _release_dispatch_lease(self) -> None:
        """Release only the lease created by this request."""
        if not self._lease_token:
            return
        try:
            current = self._read_dispatch_lease()
            if current and current.get("token") == self._lease_token:
                self._lease_path().unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self._lease_token = None

    def _find_command_line_hwnd(self) -> int | None:
        """Find AutoCAD's MDIClient child window for command routing."""
        if sys.platform != "win32" or not self._hwnd:
            return None
        try:
            import win32gui

            mdi_client: list[int] = []

            def cb(child_hwnd, _):
                if win32gui.GetClassName(child_hwnd) == "MDIClient":
                    mdi_client.append(child_hwnd)
                    return False  # stop enumeration
                return True

            win32gui.EnumChildWindows(self._hwnd, cb, None)
            return mdi_client[0] if mdi_client else None
        except Exception:
            return None

    def _find_escape_targets(self) -> list[int]:
        """Collect HWNDs that should receive ESC to cancel a stuck command.

        Sending ESC to MDIClient alone is not always enough to clear a hung
        command (e.g. the in-place TEXT editor). Target the main frame, the
        MDIClient, and any Edit/RichEdit child controls.
        """
        targets: list[int] = []
        if self._hwnd:
            targets.append(self._hwnd)
        if self._command_hwnd:
            targets.append(self._command_hwnd)
        if sys.platform == "win32" and self._hwnd:
            try:
                import win32gui

                def cb(child_hwnd, _):
                    cls = win32gui.GetClassName(child_hwnd)
                    if "EDIT" in cls.upper():
                        targets.append(child_hwnd)
                    return True

                win32gui.EnumChildWindows(self._hwnd, cb, None)
            except Exception:
                pass

        # de-duplicate while preserving order
        seen: set[int] = set()
        result: list[int] = []
        for t in targets:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    def _send_esc(self, count: int = 2):
        """Post ESC keystrokes to all escape targets to cancel pending commands."""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            WM_KEYDOWN = 0x0100
            WM_KEYUP = 0x0101
            VK_ESCAPE = 0x1B
            post = ctypes.windll.user32.PostMessageW
            targets = self._escape_targets or ([self._command_hwnd] if self._command_hwnd else ([self._hwnd] if self._hwnd else []))
            for _ in range(count):
                for t in targets:
                    if t:
                        post(t, WM_KEYDOWN, VK_ESCAPE, 0)
                        post(t, WM_KEYUP, VK_ESCAPE, 0)
            time.sleep(0.05)
        except Exception as e:
            log.error("send_esc_failed", error=str(e))

    def _type_string(self, s: str, cancel_first: bool = False):
        """Type an arbitrary string + Enter into AutoCAD's command line (focus-free)."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            WM_CHAR = 0x0102
            post = ctypes.windll.user32.PostMessageW
            target = self._command_hwnd or self._hwnd
            if cancel_first:
                self._send_esc()
                time.sleep(0.05)
            for ch in s:
                post(target, WM_CHAR, ord(ch), 0)
            # Enter = carriage return
            post(target, WM_CHAR, 0x0D, 0)
            time.sleep(0.05)
            return True
        except Exception as e:
            log.error("type_string_failed", error=str(e))
            return False

    def _type_dispatch_trigger(self):
        """Type '(c:mcp-dispatch)' + Enter via WM_CHAR to MDIClient — no focus steal.

        Only sends an ESC prefix when a previous dispatch timed out (recovery),
        so a healthy in-flight command is never cancelled by an unnecessary ESC.
        """
        triggered = self._type_string("(c:mcp-dispatch)", cancel_first=self._needs_recovery)
        self._needs_recovery = False
        return bool(triggered)

    def _cleanup_stale_files(self):
        """Remove old files from this backend session only."""
        self._cleanup_session_files(self._session_id, stale_only=True)

    def _cleanup_stale_commands(self):
        """Remove this session's abandoned command files."""
        self._cleanup_session_files(self._session_id, stale_only=False, kinds=("cmd",))

    def _cleanup_session_files(
        self,
        session_id: str | None,
        *,
        stale_only: bool,
        kinds: tuple[str, ...] = ("cmd", "result", "lisp"),
    ) -> None:
        """Clean files owned by one validated session ID."""
        if not session_id or not all(char.isalnum() or char in "-_" for char in session_id):
            return
        try:
            now = time.time()
            for kind in kinds:
                suffix = "lsp" if kind == "lisp" else "json"
                pattern = f"autocad_mcp_{kind}_{session_id}_*.{suffix}"
                for file_path in self._ipc_dir.glob(pattern):
                    if stale_only and now - file_path.stat().st_mtime <= STALE_THRESHOLD:
                        continue
                    file_path.unlink(missing_ok=True)
        except OSError:
            pass

    # --- Drawing management ---

    async def drawing_info(self) -> CommandResult:
        return await self._dispatch("drawing-info", {})

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        return await self._dispatch("drawing-save", {"path": path})

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-save-as-dxf", {"path": path})

    async def drawing_create(self, name: str | None = None) -> CommandResult:
        return await self._dispatch("drawing-create", {"name": name})

    async def drawing_purge(self) -> CommandResult:
        return await self._dispatch("drawing-purge", {})

    async def drawing_plot_pdf(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-plot-pdf", {"path": path})

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        if names:
            # Strip $ prefix for AutoCAD compatibility (ezdxf uses $ACADVER, AutoCAD uses ACADVER)
            clean_names = [n.lstrip("$") for n in names]
            names_str = ";".join(clean_names)
        else:
            names_str = ""
        return await self._dispatch("drawing-get-variables", {"names_str": names_str})

    async def drawing_open(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-open", {"path": path})

    # --- Reference workspaces ---

    async def reference_capture(self, mode="all", layers=None, window=None) -> CommandResult:
        return await self._dispatch("reference-capture", {
            "mode": mode,
            "layers_str": ";".join(layers or []),
            "x1": window[0] if window else None,
            "y1": window[1] if window else None,
            "x2": window[2] if window else None,
            "y2": window[3] if window else None,
        })

    async def reference_duplicate(self, handles, dx, dy, target_layer) -> CommandResult:
        return await self._dispatch("reference-duplicate", {
            "handles_str": ";".join(handles),
            "dx": dx,
            "dy": dy,
            "target_layer": target_layer,
        })

    async def reference_clear(self, handles) -> CommandResult:
        return await self._dispatch("reference-clear", {"handles_str": ";".join(handles)})

    async def reference_snapshot(self, window=None) -> CommandResult:
        if window:
            zoom = await self.zoom_window(*window)
            if not zoom.ok:
                return await self.get_screenshot()
        return await self.get_screenshot()

    # --- Undo / Redo ---

    async def undo(self) -> CommandResult:
        return await self._dispatch("undo", {})

    async def redo(self) -> CommandResult:
        return await self._dispatch("redo", {})

    # --- Freehand LISP execution ---

    async def execute_lisp(self, code: str) -> CommandResult:
        """Execute arbitrary AutoLISP code via temp file.

        File persists for session; cleaned up by _cleanup_stale_files().
        """
        request_id = f"{self._session_id}_{uuid.uuid4().hex[:12]}"
        code_file = self._ipc_dir / f"autocad_mcp_lisp_{request_id}.lsp"
        code_file.write_text(code, encoding="utf-8")
        return await self._dispatch("execute-lisp", {
            "code_file": str(code_file).replace("\\", "/")
        })

    # --- Entity operations ---

    async def create_line(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        return await self._dispatch("create-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer})

    async def create_circle(self, cx, cy, radius, layer=None) -> CommandResult:
        return await self._dispatch("create-circle", {"cx": cx, "cy": cy, "radius": radius, "layer": layer})

    async def create_polyline(self, points, closed=False, layer=None) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-polyline", {
            "points_str": pts_str, "closed": "1" if closed else "0", "layer": layer
        })

    async def create_rectangle(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        return await self._dispatch("create-rectangle", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer})

    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None) -> CommandResult:
        return await self._dispatch("create-arc", {"cx": cx, "cy": cy, "radius": radius, "start_angle": start_angle, "end_angle": end_angle, "layer": layer})

    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None) -> CommandResult:
        return await self._dispatch("create-ellipse", {"cx": cx, "cy": cy, "major_x": major_x, "major_y": major_y, "ratio": ratio, "layer": layer})

    async def create_mtext(self, x, y, width, text, height=2.5, layer=None) -> CommandResult:
        return await self._dispatch("create-mtext", {"x": x, "y": y, "width": width, "text": text, "height": height, "layer": layer})

    async def create_hatch(self, entity_id, pattern="ANSI31") -> CommandResult:
        return await self._dispatch("create-hatch", {"entity_id": entity_id, "pattern": pattern})

    async def entity_list(self, layer=None) -> CommandResult:
        return await self._dispatch("entity-list", {"layer": layer})

    async def entity_count(self, layer=None) -> CommandResult:
        return await self._dispatch("entity-count", {"layer": layer})

    async def entity_get(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-get", {"entity_id": entity_id})

    async def entity_erase(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-erase", {"entity_id": entity_id})

    async def entity_copy(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-copy", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_move(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-move", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_rotate(self, entity_id, cx, cy, angle) -> CommandResult:
        return await self._dispatch("entity-rotate", {"entity_id": entity_id, "cx": cx, "cy": cy, "angle": angle})

    async def entity_scale(self, entity_id, cx, cy, factor) -> CommandResult:
        return await self._dispatch("entity-scale", {"entity_id": entity_id, "cx": cx, "cy": cy, "factor": factor})

    async def entity_mirror(self, entity_id, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("entity-mirror", {"entity_id": entity_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def entity_offset(self, entity_id, distance) -> CommandResult:
        return await self._dispatch("entity-offset", {"entity_id": entity_id, "distance": distance})

    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist) -> CommandResult:
        return await self._dispatch("entity-array", {"entity_id": entity_id, "rows": rows, "cols": cols, "row_dist": row_dist, "col_dist": col_dist})

    async def entity_fillet(self, entity_id1, entity_id2, radius) -> CommandResult:
        return await self._dispatch("entity-fillet", {"id1": entity_id1, "id2": entity_id2, "radius": radius})

    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2) -> CommandResult:
        return await self._dispatch("entity-chamfer", {"id1": entity_id1, "id2": entity_id2, "dist1": dist1, "dist2": dist2})

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        return await self._dispatch("layer-list", {})

    async def layer_create(self, name, color="white", linetype="CONTINUOUS") -> CommandResult:
        return await self._dispatch("layer-create", {"name": name, "color": color, "linetype": linetype})

    async def layer_set_current(self, name) -> CommandResult:
        return await self._dispatch("layer-set-current", {"name": name})

    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None) -> CommandResult:
        return await self._dispatch("layer-set-properties", {"name": name, "color": color, "linetype": linetype, "lineweight": lineweight})

    async def layer_freeze(self, name) -> CommandResult:
        return await self._dispatch("layer-freeze", {"name": name})

    async def layer_thaw(self, name) -> CommandResult:
        return await self._dispatch("layer-thaw", {"name": name})

    async def layer_lock(self, name) -> CommandResult:
        return await self._dispatch("layer-lock", {"name": name})

    async def layer_unlock(self, name) -> CommandResult:
        return await self._dispatch("layer-unlock", {"name": name})

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        return await self._dispatch("block-list", {})

    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None) -> CommandResult:
        return await self._dispatch("block-insert", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "block_id": block_id})

    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("block-insert-with-attributes", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "attributes": attributes or {}})

    async def block_get_attributes(self, entity_id) -> CommandResult:
        return await self._dispatch("block-get-attributes", {"entity_id": entity_id})

    async def block_update_attribute(self, entity_id, tag, value) -> CommandResult:
        return await self._dispatch("block-update-attribute", {"entity_id": entity_id, "tag": tag, "value": value})

    async def block_define(self, name, entities) -> CommandResult:
        return await self._dispatch("block-define", {"name": name, "entities": entities})

    # --- Annotation ---

    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None) -> CommandResult:
        return await self._dispatch("create-text", {"x": x, "y": y, "text": text, "height": height, "rotation": rotation, "layer": layer})

    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y) -> CommandResult:
        return await self._dispatch("create-dimension-linear", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "dim_x": dim_x, "dim_y": dim_y})

    async def create_dimension_aligned(self, x1, y1, x2, y2, offset) -> CommandResult:
        return await self._dispatch("create-dimension-aligned", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "offset": offset})

    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("create-dimension-angular", {"cx": cx, "cy": cy, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def create_dimension_radius(self, cx, cy, radius, angle) -> CommandResult:
        return await self._dispatch("create-dimension-radius", {"cx": cx, "cy": cy, "radius": radius, "angle": angle})

    async def create_leader(self, points, text) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-leader", {"points_str": pts_str, "text": text})

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        return await self._dispatch("pid-setup-layers", {})

    async def pid_insert_symbol(self, category, symbol, x, y, scale=1.0, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-insert-symbol", {"category": category, "symbol": symbol, "x": x, "y": y, "scale": scale, "rotation": rotation})

    async def pid_list_symbols(self, category) -> CommandResult:
        return await self._dispatch("pid-list-symbols", {"category": category})

    async def pid_draw_process_line(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-draw-process-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_connect_equipment(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-connect-equipment", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_add_flow_arrow(self, x, y, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-add-flow-arrow", {"x": x, "y": y, "rotation": rotation})

    async def pid_add_equipment_tag(self, x, y, tag, description="") -> CommandResult:
        return await self._dispatch("pid-add-equipment-tag", {"x": x, "y": y, "tag": tag, "description": description})

    async def pid_add_line_number(self, x, y, line_num, spec) -> CommandResult:
        return await self._dispatch("pid-add-line-number", {"x": x, "y": y, "line_num": line_num, "spec": spec})

    async def pid_insert_valve(self, x, y, valve_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-valve", {"x": x, "y": y, "valve_type": valve_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_instrument(self, x, y, instrument_type, rotation=0.0, tag_id="", range_value="") -> CommandResult:
        return await self._dispatch("pid-insert-instrument", {"x": x, "y": y, "instrument_type": instrument_type, "rotation": rotation, "tag_id": tag_id, "range_value": range_value})

    async def pid_insert_pump(self, x, y, pump_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-pump", {"x": x, "y": y, "pump_type": pump_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_tank(self, x, y, tank_type, scale=1.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-tank", {"x": x, "y": y, "tank_type": tank_type, "scale": scale, "attributes": attributes or {}})

    # --- View ---

    async def zoom_extents(self) -> CommandResult:
        return await self._dispatch("zoom-extents", {})

    async def zoom_window(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("zoom-window", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def get_screenshot(self) -> CommandResult:
        if self._screenshot_provider:
            data = self._screenshot_provider.capture()
            if data:
                return CommandResult(ok=True, payload=data)
        return CommandResult(ok=False, error="Screenshot capture failed")
