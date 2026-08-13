# MCP Foundation Design

## Goal

Make the existing AutoCAD MCP easier for an AI agent to use without changing
the eight public tool names or requiring users to change their MCP client
configuration.

This is the foundation for two later improvements:

- Better parity between the `ezdxf` and File IPC backends.
- More useful AutoCAD IPC diagnostics and recovery.

## Current Context

The server exposes eight consolidated tools. Each tool accepts a free-form
`operation` string and, for most operations, a free-form `data` dictionary.
This keeps the API small, but invalid operation names and malformed payloads
are discovered late and errors do not have a uniform shape.

Both backends already expose coarse capability flags. They do not expose the
specific operations supported by each backend.

The current test suite passes (`128 passed`). The implementation should not
turn this personal-use project into a heavily formalized software product.

## Design

### Public API

Keep calls in their current form:

```text
entity(operation="create_circle", data={"cx": 0, "cy": 0, "radius": 5})
```

Each tool's `operation` parameter becomes a `Literal` containing the valid
operation names. This lets MCP clients discover valid operation names and
reject unknown names during argument validation.

The `data` parameter remains a dictionary for compatibility. Its contents are
validated at runtime using operation-specific Pydantic models.

### Contract Registry

Add a small contract module that contains:

- Pydantic models for operation payloads.
- Common validators for positive dimensions, non-empty strings, valid points,
  and required paths.
- A registry mapping a tool and operation to its payload model.
- Capability metadata identifying the backend operation required by each
  contract.

Validation returns normalized values to the existing dispatch functions. The
registry is the single place to add a new operation's input rules.

The first version should cover the most commonly used operations and the
representative edge cases. It does not need a separate model for every
optional field if the existing backend already handles that field safely.

### Capabilities

Extend `BackendCapabilities` with an operation map grouped by tool. Keep all
existing boolean capability fields so current status consumers continue to
work.

Example shape:

```json
{
  "entity": ["create_line", "create_circle", "list", "get"],
  "drawing": ["create", "open", "save"],
  "view": ["get_screenshot"]
}
```

`system(operation="status")` includes this map. Before dispatching an
operation, the server checks the selected backend's map. Unsupported requests
fail immediately with a useful error instead of reaching a backend default
method and producing a vague failure.

The map also becomes the source of truth for future backend-parity changes.

### Result and Error Contract

Keep successful responses unchanged:

```json
{"ok": true, "payload": {}}
```

Keep the existing human-readable `error` string for compatibility. Add
optional fields to failures:

```json
{
  "ok": false,
  "error": "Operation is not supported",
  "error_code": "unsupported_operation",
  "tool": "entity",
  "operation": "offset",
  "backend": "ezdxf",
  "hint": "Use the file_ipc backend or inspect system(status)."
}
```

Use these error codes for the foundation:

- `invalid_operation`
- `invalid_input`
- `unsupported_operation`
- `backend_error`
- `internal_error`

Unknown operations and decorator-caught exceptions must include `ok: false`.
Field validation errors should identify the failing fields in the human-readable
message or an additional details field.

### Data Flow

```text
MCP request
  -> Literal operation validation
  -> operation contract validation
  -> backend capability check
  -> backend method
  -> uniform result/error formatting
```

The backend interfaces remain unchanged in this milestone. This keeps the
foundation isolated from AutoCAD-specific behavior.

## Verification

Use lightweight verification appropriate for personal use:

- Add a few contract tests covering a valid payload, a missing required field,
  and an invalid numeric or point value.
- Add one unsupported-operation check for each backend.
- Add one status check proving the operation map is returned.
- Run the existing suite as a regression check.
- Run one short headless smoke flow that creates an entity, queries it, and
  saves a DXF.

Do not add an exhaustive operation-by-operation test matrix, formal backward
compatibility harness, or test framework refactor.

## Out of Scope

- Adding new AutoCAD commands.
- Changing the File IPC file protocol.
- Replacing the eight consolidated tools with many tools.
- Implementing undo/redo or other backend parity work.
- Reworking the LISP dispatcher.
- Introducing a nested error object that would change the existing response
  shape.

## Follow-up Milestones

### Backend Parity

For each desired operation, implement the backend method, update its operation
map, and add only the smoke coverage needed to confirm the behavior.

### IPC Reliability

Use the same error metadata to report timeout stage, request ID, dispatcher
state, and recovery hints without changing the MCP tool contract.
