"""Lightweight smoke tests for the agent-facing request contracts."""

import pytest

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
from autocad_mcp.contracts import (
    ContractError,
    UnsupportedOperationError,
    ensure_supported,
    validate_operation,
)


def test_circle_contract_normalizes_defaults():
    payload = validate_operation(
        "entity",
        "create_circle",
        {"cx": 1, "cy": 2, "radius": 3},
    )

    assert payload["radius"] == 3.0


def test_contract_rejects_missing_required_value():
    with pytest.raises(ContractError, match="radius"):
        validate_operation("entity", "create_circle", {"cx": 1, "cy": 2})


def test_contract_rejects_invalid_points():
    with pytest.raises(ContractError, match="at least x and y"):
        validate_operation("entity", "create_polyline", {"points": [[0, 0], [1]]})


@pytest.mark.asyncio
async def test_capability_map_rejects_headless_only_operation():
    backend = EzdxfBackend()
    await backend.initialize()

    with pytest.raises(UnsupportedOperationError, match="offset"):
        ensure_supported(backend, "entity", "offset")


@pytest.mark.asyncio
async def test_status_capabilities_include_operations():
    backend = EzdxfBackend()
    await backend.initialize()
    result = await backend.status()

    assert result.ok
    assert "create_circle" in result.payload["capabilities"]["operations"]["entity"]
