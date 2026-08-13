"""In-memory reference workspaces for safe side-by-side CAD work."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from autocad_mcp.contracts import ContractError

WorkspaceMode = Literal["side_by_side", "duplicate_then_modify", "overlay"]


class ReferenceError(ContractError):
    """A reference workspace request cannot be completed safely."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message, **details)
        self.code = code


@dataclass
class ReferenceWorkspace:
    workspace_id: str
    handles: list[str]
    bounds: dict[str, float]
    source_layers: list[str] = field(default_factory=list)
    mode: WorkspaceMode | None = None
    gap: float = 20.0
    proposal_layer: str = ""
    dx: float = 0.0
    dy: float = 0.0
    proposal_handles: list[str] = field(default_factory=list)

    @property
    def proposal_bounds(self) -> dict[str, float] | None:
        if self.mode is None:
            return None
        return _translate_bounds(self.bounds, self.dx, self.dy)

    def summary(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "mode": self.mode,
            "reference": {
                "bounds": self.bounds,
                "entity_count": len(self.handles),
                "layers": self.source_layers,
            },
            "proposal": {
                "bounds": self.proposal_bounds,
                "layer": self.proposal_layer or None,
                "entity_count": len(self.proposal_handles),
                "gap": self.gap,
            },
        }


_active: ReferenceWorkspace | None = None


def capture(payload: dict[str, Any]) -> ReferenceWorkspace:
    """Create a new active workspace from backend capture metadata."""
    global _active
    handles = [str(handle) for handle in payload.get("handles", [])]
    bounds = payload.get("bounds") or {}
    required = ("min_x", "min_y", "max_x", "max_y")
    if not handles or any(key not in bounds for key in required):
        raise ReferenceError("empty_reference", "Reference capture returned no usable entities")
    if bounds["max_x"] < bounds["min_x"] or bounds["max_y"] < bounds["min_y"]:
        raise ReferenceError("invalid_reference_boundary", "Reference bounds are invalid")

    workspace_id = f"ws_{uuid.uuid4().hex[:10]}"
    _active = ReferenceWorkspace(
        workspace_id=workspace_id,
        handles=handles,
        bounds={key: float(bounds[key]) for key in required},
        source_layers=[str(layer) for layer in payload.get("layers", [])],
        proposal_layer=f"MCP-PROPOSAL-{workspace_id[-6:].upper()}",
    )
    return _active


def active() -> ReferenceWorkspace | None:
    return _active


def require(workspace_id: str | None = None, *, require_mode: bool = False) -> ReferenceWorkspace:
    if _active is None:
        raise ReferenceError("reference_not_captured", "Capture a reference before using a workspace")
    if workspace_id and workspace_id != _active.workspace_id:
        raise ReferenceError(
            "workspace_mismatch",
            f"Workspace '{workspace_id}' is not active",
            workspace_id=workspace_id,
            active_workspace_id=_active.workspace_id,
        )
    if require_mode and _active.mode is None:
        raise ReferenceError("proposal_not_found", "Create a workspace before using proposal operations")
    return _active


def create_workspace(mode: WorkspaceMode, gap: float = 20.0) -> ReferenceWorkspace:
    workspace = require()
    if gap < 0:
        raise ReferenceError("invalid_reference_boundary", "Workspace gap cannot be negative")
    workspace.mode = mode
    workspace.gap = gap
    if mode == "overlay":
        workspace.dx = 0.0
        workspace.dy = 0.0
    else:
        workspace.dx = workspace.bounds["max_x"] + gap - workspace.bounds["min_x"]
        workspace.dy = 0.0
    workspace.proposal_handles.clear()
    return workspace


def bounds_for(target: str, window: list[float] | None = None) -> dict[str, float]:
    workspace = require(require_mode=True)
    if target == "reference":
        return dict(workspace.bounds)
    if target == "proposal":
        return dict(workspace.proposal_bounds or workspace.bounds)
    if target == "workspace":
        proposal = workspace.proposal_bounds or workspace.bounds
        return {
            "min_x": min(workspace.bounds["min_x"], proposal["min_x"]),
            "min_y": min(workspace.bounds["min_y"], proposal["min_y"]),
            "max_x": max(workspace.bounds["max_x"], proposal["max_x"]),
            "max_y": max(workspace.bounds["max_y"], proposal["max_y"]),
        }
    if target == "window" and window and len(window) == 4:
        x1, y1, x2, y2 = window
        return {
            "min_x": min(x1, x2),
            "min_y": min(y1, y2),
            "max_x": max(x1, x2),
            "max_y": max(y1, y2),
        }
    raise ReferenceError("invalid_reference_boundary", "Snapshot window or target is invalid")


def apply_entity_context(
    data: dict[str, Any],
    workspace_id: str | None,
    entity_id: str | None = None,
    *,
    mutates_entity: bool = False,
) -> dict[str, Any]:
    """Apply proposal defaults and protect captured source handles."""
    if not workspace_id:
        if _active is not None and mutates_entity:
            raise ReferenceError(
                "workspace_mismatch",
                "An active reference workspace requires workspace_id for mutations",
                active_workspace_id=_active.workspace_id,
            )
        return data
    workspace = require(workspace_id, require_mode=True)
    if mutates_entity and entity_id and entity_id in workspace.handles:
        raise ReferenceError(
            "reference_protected",
            f"Reference entity '{entity_id}' is protected",
            workspace_id=workspace.workspace_id,
            entity_id=entity_id,
        )
    if workspace.mode and "layer" not in data:
        data["layer"] = workspace.proposal_layer
    return data


def record_handles(workspace_id: str | None, payload: Any) -> None:
    if not workspace_id or not isinstance(payload, dict):
        return
    workspace = require(workspace_id, require_mode=True)
    handles: list[str] = []
    if payload.get("handle"):
        handles.append(str(payload["handle"]))
    handles.extend(str(handle) for handle in payload.get("handles", []))
    handles.extend(str(handle) for handle in payload.get("copied_handles", []))
    for handle in handles:
        if handle not in workspace.proposal_handles:
            workspace.proposal_handles.append(handle)


def reset() -> None:
    global _active
    _active = None


def _translate_bounds(bounds: dict[str, float], dx: float, dy: float) -> dict[str, float]:
    return {
        "min_x": bounds["min_x"] + dx,
        "min_y": bounds["min_y"] + dy,
        "max_x": bounds["max_x"] + dx,
        "max_y": bounds["max_y"] + dy,
    }
