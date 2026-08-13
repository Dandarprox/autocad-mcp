"""Lazy backend singleton, _safe/_error/_json helpers, screenshot utility."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
from typing import Any

import structlog
from mcp.types import ImageContent, TextContent

from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.config import ONLY_TEXT_FEEDBACK, detect_backend
from autocad_mcp.contracts import ContractError

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lazy backend singleton
# ---------------------------------------------------------------------------

_backend: AutoCADBackend | None = None
_init_lock = asyncio.Lock()


async def get_backend() -> AutoCADBackend:
    """Return (and lazily initialize) the backend singleton.

    Uses an asyncio Lock to prevent concurrent initialization races
    when multiple MCP tool calls arrive simultaneously.
    """
    global _backend
    if _backend is not None:
        return _backend

    async with _init_lock:
        # Double-check after acquiring lock (another task may have initialized)
        if _backend is not None:
            return _backend

        backend_name = detect_backend()

        if backend_name == "file_ipc":
            from autocad_mcp.backends.file_ipc import FileIPCBackend

            backend = FileIPCBackend()
        else:
            from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

            backend = EzdxfBackend()

        result = await backend.initialize()
        if not result.ok:
            raise RuntimeError(f"Backend init failed: {result.error}")

        _backend = backend
        log.info("backend_initialized", backend=_backend.name)
        return _backend


# ---------------------------------------------------------------------------
# JSON serialization helper
# ---------------------------------------------------------------------------


def _json(data: Any) -> str:
    """Serialize to compact JSON string."""
    return json.dumps(data, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Error formatting with actionable hints
# ---------------------------------------------------------------------------


def _error(
    e: Exception,
    context: str = "",
    error_code: str | None = None,
    **metadata: Any,
) -> str:
    """Format an exception with an actionable hint."""
    msg = str(e)
    msg_lower = msg.lower()
    hint = "Unexpected error. Check AutoCAD is responsive and retry."

    if error_code:
        code = error_code
    elif isinstance(e, ContractError):
        code = e.code
    elif "window not found" in msg_lower or "no autocad" in msg_lower:
        code = "backend_error"
        hint = "AutoCAD LT is not running or no drawing is open. Start AutoCAD and open a .dwg file."
    elif "timeout" in msg_lower:
        code = "backend_error"
        hint = "Command timed out. AutoCAD may be in a modal dialog. Press ESC in AutoCAD and retry."
    elif "not supported" in msg_lower or "backend" in msg_lower:
        code = "backend_error"
        hint = "Operation not supported on current backend. Check system(operation='status') for capabilities."
    elif "dispatcher" in msg_lower or "mcp_dispatch" in msg_lower:
        code = "backend_error"
        hint = "mcp_dispatch.lsp not loaded. In AutoCAD command line, type: (load \"mcp_dispatch.lsp\")"
    else:
        code = "internal_error"
        hint = "Unexpected error. Check AutoCAD is responsive and retry."

    if code == "invalid_input":
        hint = "Check the operation's required fields and value ranges, then retry."
    elif code == "unsupported_operation":
        hint = "Inspect system(operation='status') or select a backend that supports this operation."

    response = {
        "ok": False,
        "error": f"[{context}] {msg}" if context else msg,
        "error_code": code,
        "hint": hint,
    }
    if isinstance(e, ContractError):
        response.update(e.details)
    response.update(metadata)
    return _json(response)


# ---------------------------------------------------------------------------
# _safe decorator for tool error handling
# ---------------------------------------------------------------------------


def _safe(tool_name: str):
    """Wrap an async tool handler with uniform error handling."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                op = kwargs.get("operation", "unknown")
                log.error("tool_error", tool=tool_name, operation=op, error=str(e))
                return _error(e, f"{tool_name}.{op}", tool=tool_name, operation=op)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------


def _result_dict(
    result: CommandResult,
    tool: str | None = None,
    operation: str | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    """Add stable context to backend failures without changing success data."""
    data = result.to_dict()
    if not result.ok:
        data.setdefault("error_code", "backend_error")
        if tool:
            data["tool"] = tool
        if operation:
            data["operation"] = operation
        if backend:
            data["backend"] = backend
    return data


def _format_result(
    result: CommandResult,
    include_screenshot: bool = False,
    screenshot_data: str | None = None,
    tool: str | None = None,
    operation: str | None = None,
    backend: str | None = None,
) -> list[TextContent | ImageContent] | str:
    """Format a CommandResult for MCP response.

    Returns a list with TextContent + optional ImageContent if screenshot requested,
    or a plain JSON string if no screenshot.
    """
    text = _json(_result_dict(result, tool, operation, backend))

    if not include_screenshot or ONLY_TEXT_FEEDBACK or not screenshot_data:
        return text

    return [
        TextContent(type="text", text=text),
        ImageContent(
            type="image",
            data=screenshot_data,
            mimeType="image/png",
        ),
    ]


async def add_screenshot_if_available(
    result: CommandResult,
    include_screenshot: bool = False,
    tool: str | None = None,
    operation: str | None = None,
) -> list[TextContent | ImageContent] | str:
    """Conditionally append a screenshot to the result."""
    if not include_screenshot or ONLY_TEXT_FEEDBACK:
        backend = await get_backend()
        return _json(_result_dict(result, tool, operation, backend.name))

    backend = await get_backend()
    screenshot_result = await backend.get_screenshot()

    if screenshot_result.ok and screenshot_result.payload:
        return _format_result(
            result,
            True,
            screenshot_result.payload,
            tool,
            operation,
            backend.name,
        )

    return _json(_result_dict(result, tool, operation, backend.name))
