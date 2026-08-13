"""Lightweight reference workspace checks for the headless backend."""

import base64

import pytest

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
from autocad_mcp.reference import (
    ReferenceError,
    apply_entity_context,
    capture,
    create_workspace,
    reset,
)


@pytest.fixture
async def backend():
    backend = EzdxfBackend()
    result = await backend.initialize()
    assert result.ok
    return backend


@pytest.fixture(autouse=True)
def clear_workspace():
    reset()
    yield
    reset()


async def test_capture_and_side_by_side_workspace(backend):
    await backend.create_rectangle(0, 0, 20, 10)
    result = await backend.reference_capture()
    assert result.ok

    workspace = capture(result.payload)
    workspace = create_workspace("side_by_side", gap=5)

    assert workspace.proposal_bounds == {
        "min_x": 25.0,
        "min_y": 0.0,
        "max_x": 45.0,
        "max_y": 10.0,
    }


async def test_duplicate_and_clear_leave_reference(backend):
    source = await backend.create_line(0, 0, 10, 0)
    captured = await backend.reference_capture()
    workspace = capture(captured.payload)
    create_workspace("duplicate_then_modify", gap=5)

    duplicate = await backend.reference_duplicate(
        workspace.handles,
        workspace.dx,
        workspace.dy,
        workspace.proposal_layer,
    )
    assert duplicate.ok
    copied = duplicate.payload["copied_handles"]
    assert copied

    cleared = await backend.reference_clear(copied)
    assert cleared.ok
    assert (await backend.entity_count()).payload["count"] == 1
    assert source.payload["handle"] in (await backend.entity_list()).payload["entities"][0].values()


async def test_reference_snapshot_contains_png(backend):
    await backend.create_circle(10, 10, 5)
    captured = await backend.reference_capture()
    workspace = capture(captured.payload)
    create_workspace("overlay")

    result = await backend.reference_snapshot([
        workspace.bounds["min_x"],
        workspace.bounds["min_y"],
        workspace.bounds["max_x"],
        workspace.bounds["max_y"],
    ])
    assert result.ok
    assert base64.b64decode(result.payload)[:4] == b"\x89PNG"


async def test_reference_handles_are_protected(backend):
    created = await backend.create_circle(0, 0, 2)
    captured = await backend.reference_capture()
    workspace = capture(captured.payload)
    create_workspace("overlay")

    with pytest.raises(ReferenceError, match="protected"):
        apply_entity_context(
            {"entity_id": created.payload["handle"]},
            workspace.workspace_id,
            created.payload["handle"],
            mutates_entity=True,
        )


def test_active_workspace_requires_context_for_mutations():
    capture({
        "handles": ["A"],
        "layers": ["0"],
        "bounds": {"min_x": 0, "min_y": 0, "max_x": 1, "max_y": 1},
    })
    create_workspace("overlay")

    with pytest.raises(ReferenceError, match="requires workspace_id"):
        apply_entity_context({}, None, mutates_entity=True)
