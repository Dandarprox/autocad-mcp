"""Small, shared input contracts for the consolidated MCP tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ContractError(ValueError):
    """A request failed validation before reaching a backend."""

    code = "invalid_input"

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


class UnsupportedOperationError(ContractError):
    """The selected backend does not implement an operation."""

    code = "unsupported_operation"


class Payload(BaseModel):
    """Base payload that tolerates future optional fields."""

    model_config = ConfigDict(extra="allow")


class EmptyPayload(Payload):
    pass


class PathPayload(Payload):
    path: str = Field(min_length=1)


class LinePayload(Payload):
    x1: float
    y1: float
    x2: float
    y2: float


class CirclePayload(Payload):
    cx: float
    cy: float
    radius: float = Field(gt=0)


class RectanglePayload(LinePayload):
    pass


class PolylinePayload(Payload):
    points: list[list[float]] = Field(min_length=2)

    @field_validator("points")
    @classmethod
    def points_are_2d(cls, points: list[list[float]]) -> list[list[float]]:
        if any(len(point) < 2 for point in points):
            raise ValueError("each point must contain at least x and y")
        return points


class ArcPayload(CirclePayload):
    start_angle: float
    end_angle: float


class EllipsePayload(Payload):
    cx: float
    cy: float
    major_x: float
    major_y: float
    ratio: float = Field(gt=0, le=1)


class MTextPayload(Payload):
    x: float
    y: float
    width: float = Field(gt=0)
    text: str = Field(min_length=1)


class EntityIDPayload(Payload):
    entity_id: str = Field(min_length=1)


class MovePayload(EntityIDPayload):
    dx: float
    dy: float


class RotatePayload(EntityIDPayload):
    cx: float
    cy: float
    angle: float


class ScalePayload(EntityIDPayload):
    cx: float
    cy: float
    factor: float = Field(gt=0)


class OffsetPayload(EntityIDPayload):
    distance: float = Field(gt=0)


class MirrorPayload(EntityIDPayload):
    x1: float
    y1: float
    x2: float
    y2: float


class ArrayPayload(EntityIDPayload):
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)
    row_dist: float
    col_dist: float


class FilletPayload(Payload):
    id1: str = Field(min_length=1)
    id2: str = Field(min_length=1)
    radius: float = Field(gt=0)


class ChamferPayload(Payload):
    id1: str = Field(min_length=1)
    id2: str = Field(min_length=1)
    dist1: float = Field(gt=0)
    dist2: float = Field(gt=0)


class LayerNamePayload(Payload):
    name: str = Field(min_length=1)


class LayerCreatePayload(LayerNamePayload):
    pass


class LayerPropertiesPayload(LayerNamePayload):
    pass


class BlockInsertPayload(Payload):
    name: str = Field(min_length=1)
    x: float
    y: float
    scale: float = Field(default=1.0, gt=0)
    rotation: float = 0.0


class BlockAttributesPayload(BlockInsertPayload):
    attributes: dict[str, str] | None = None


class BlockEntityPayload(EntityIDPayload):
    pass


class AttributePayload(BlockEntityPayload):
    tag: str = Field(min_length=1)
    value: str


class TextPayload(Payload):
    x: float
    y: float
    text: str = Field(min_length=1)


class LinearDimensionPayload(Payload):
    x1: float
    y1: float
    x2: float
    y2: float
    dim_x: float
    dim_y: float


class AlignedDimensionPayload(Payload):
    x1: float
    y1: float
    x2: float
    y2: float
    offset: float


class AngularDimensionPayload(Payload):
    cx: float
    cy: float
    x1: float
    y1: float
    x2: float
    y2: float


class RadiusDimensionPayload(Payload):
    cx: float
    cy: float
    radius: float = Field(gt=0)
    angle: float


class LeaderPayload(Payload):
    points: list[list[float]] = Field(min_length=2)
    text: str = Field(min_length=1)

    @field_validator("points")
    @classmethod
    def points_are_2d(cls, points: list[list[float]]) -> list[list[float]]:
        if any(len(point) < 2 for point in points):
            raise ValueError("each point must contain at least x and y")
        return points


class WindowPayload(Payload):
    x1: float
    y1: float
    x2: float
    y2: float


class CodePayload(Payload):
    code: str = Field(min_length=1)


DrawingOperation = Literal[
    "create", "open", "info", "save", "save_as_dxf", "plot_pdf", "purge",
    "get_variables", "undo", "redo",
]
EntityOperation = Literal[
    "create_line", "create_circle", "create_polyline", "create_rectangle",
    "create_arc", "create_ellipse", "create_mtext", "create_hatch", "list",
    "count", "get", "copy", "move", "rotate", "scale", "mirror", "offset",
    "array", "fillet", "chamfer", "erase",
]
LayerOperation = Literal[
    "list", "create", "set_current", "set_properties", "freeze", "thaw",
    "lock", "unlock",
]
BlockOperation = Literal[
    "list", "insert", "insert_with_attributes", "get_attributes",
    "update_attribute", "define",
]
AnnotationOperation = Literal[
    "create_text", "create_dimension_linear", "create_dimension_aligned",
    "create_dimension_angular", "create_dimension_radius", "create_leader",
]
PIDOperation = Literal[
    "setup_layers", "insert_symbol", "list_symbols", "draw_process_line",
    "connect_equipment", "add_flow_arrow", "add_equipment_tag", "add_line_number",
    "insert_valve", "insert_instrument", "insert_pump", "insert_tank",
]
ViewOperation = Literal["zoom_extents", "zoom_window", "get_screenshot"]
SystemOperation = Literal[
    "status", "health", "get_backend", "runtime", "init", "execute_lisp",
]


CONTRACTS: dict[str, dict[str, type[Payload]]] = {
    "drawing": {
        "open": PathPayload,
        "save_as_dxf": PathPayload,
        "plot_pdf": PathPayload,
    },
    "entity": {
        "create_line": LinePayload,
        "create_circle": CirclePayload,
        "create_polyline": PolylinePayload,
        "create_rectangle": RectanglePayload,
        "create_arc": ArcPayload,
        "create_ellipse": EllipsePayload,
        "create_mtext": MTextPayload,
        "create_hatch": EntityIDPayload,
        "get": EntityIDPayload,
        "copy": MovePayload,
        "move": MovePayload,
        "rotate": RotatePayload,
        "scale": ScalePayload,
        "mirror": MirrorPayload,
        "offset": OffsetPayload,
        "array": ArrayPayload,
        "fillet": FilletPayload,
        "chamfer": ChamferPayload,
        "erase": EntityIDPayload,
    },
    "layer": {
        "create": LayerCreatePayload,
        "set_current": LayerNamePayload,
        "set_properties": LayerPropertiesPayload,
        "freeze": LayerNamePayload,
        "thaw": LayerNamePayload,
        "lock": LayerNamePayload,
        "unlock": LayerNamePayload,
    },
    "block": {
        "insert": BlockInsertPayload,
        "insert_with_attributes": BlockAttributesPayload,
        "get_attributes": BlockEntityPayload,
        "update_attribute": AttributePayload,
    },
    "annotation": {
        "create_text": TextPayload,
        "create_dimension_linear": LinearDimensionPayload,
        "create_dimension_aligned": AlignedDimensionPayload,
        "create_dimension_angular": AngularDimensionPayload,
        "create_dimension_radius": RadiusDimensionPayload,
        "create_leader": LeaderPayload,
    },
    "view": {"zoom_window": WindowPayload},
    "system": {"execute_lisp": CodePayload},
}


def validate_operation(tool: str, operation: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate and normalize an operation payload when a contract exists."""
    model = CONTRACTS.get(tool, {}).get(operation, Payload)
    try:
        return model.model_validate(data or {}).model_dump()
    except ValidationError as exc:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ContractError(messages) from exc


def ensure_supported(backend: Any, tool: str, operation: str) -> None:
    """Fail early when a backend does not advertise an operation."""
    if not backend.capabilities.supports(tool, operation):
        supported = backend.capabilities.operations.get(tool, [])
        raise UnsupportedOperationError(
            f"Operation '{operation}' is not supported on the {backend.name} backend",
            tool=tool,
            operation=operation,
            backend=backend.name,
            supported=supported,
        )
