# Reference Workspace Design

## Goal

Allow the model to use an existing AutoCAD drawing as a protected reference
while creating new work beside it, duplicating it for modification, or
overlaying a proposal without deleting or silently changing the source.

## Current Context

The MCP can create and modify entities, manage layers, inspect entities, and
capture screenshots. It does not have a persistent concept of a reference
region, a proposal area, or a safe relationship between the two.

The reference is already open in AutoCAD. Image ingestion is not part of this
feature.

## Design

### Reference Tool

Add a dedicated `reference` MCP tool with these operations:

- `capture`: capture a reference from all current entities, a named layer set,
  or an explicit window.
- `inspect`: return the active workspace, reference bounds, entity count, and
  proposal bounds.
- `create_workspace`: create a `side_by_side`, `duplicate_then_modify`, or
  `overlay` workspace.
- `duplicate`: copy the captured reference into the proposal area and return a
  source-to-copy handle map.
- `snapshot`: return an image of the active reference, proposal, workspace, or
  supplied model-space window.
- `clear_proposal`: remove only entities created or copied into the active
  proposal workspace.
- `reset`: forget the in-memory workspace without modifying drawing entities.

Each capture creates a workspace ID. The server keeps the active workspace in
memory for the current MCP process. After a process restart, the model must
capture the reference again rather than guessing from layer names or old
handles.

### Reference Boundaries

The capture operation supports three boundary modes:

- `all`: all current model-space entities.
- `layer`: entities on one or more named layers.
- `window`: entities inside two supplied corner points.

Capture returns the minimum and maximum coordinates, width, height, entity
handles, and source layers. Empty selections fail with `empty_reference`.

### Workspace Modes

`side_by_side` preserves the reference coordinates and places new work to the
right of its maximum X coordinate. The placement gap is configurable, with a
reasonable default when omitted. New geometry uses a reserved proposal layer.

`duplicate_then_modify` copies the captured entities to the proposal area and
returns a mapping from source handles to copied handles. Subsequent changes
must target the copied handles.

`overlay` keeps proposal geometry in the reference coordinate system but uses
a separate proposal layer. The source remains available for comparison,
freezing, or hiding.

The active workspace records the reference bounds, proposal origin, mode, gap,
proposal layer, source handles, and proposal handles.

### Snapshots

Snapshots are explicit only. They are not attached automatically after capture
or mutation operations.

The snapshot operation prefers a model-space crop and returns an MCP image
attachment with workspace ID, bounds, image source, and fallback status. The
`ezdxf` backend renders the requested bounds directly. The File IPC backend
zooms AutoCAD to the requested bounds and captures the viewport. If the bounds
cannot be resolved, it returns the current viewport as a fallback.

`AUTOCAD_MCP_ONLY_TEXT` continues to suppress image attachments while retaining
the snapshot metadata.

### Safety Rules

- New entities default to the active proposal layer when a workspace is active
  and no explicit layer is supplied.
- Captured reference handles are protected from erase, move, rotate, scale,
  mirror, offset, fillet, and chamfer by default.
- Mutating a protected handle returns `reference_protected`.
- Workspace-aware mutations carry the workspace ID. A stale or unknown ID
  returns `workspace_mismatch`.
- `clear_proposal` operates only on tracked proposal handles or the reserved
  proposal layer. It never erases the whole drawing.
- `reset` changes only server memory and does not erase or move entities.

Reserved layer names use the pattern `MCP-PROPOSAL-<workspace suffix>`. The
reference is not moved to a new layer by default, so capture does not alter
the original drawing.

### Backend Boundary

Add focused backend operations for reference workflows instead of implementing
bulk behavior through repeated generic entity calls:

- Reference extent and entity discovery.
- Reference duplication with a target layer and translation.
- Proposal cleanup by tracked handles or workspace layer.

The `ezdxf` backend performs these operations directly. The File IPC backend
uses new whitelisted AutoLISP commands. It does not rely on arbitrary
`execute_lisp` calls.

Existing generic tools remain usable. They gain optional workspace context so
the server can apply proposal-layer defaults and reference protection without
changing existing non-workspace calls.

### Errors and Status

Use explicit error codes:

- `reference_not_captured`
- `workspace_mismatch`
- `reference_protected`
- `invalid_reference_boundary`
- `empty_reference`
- `proposal_not_found`

`system(operation="status")` includes the active workspace ID and reference
summary when one exists.

## Verification

Keep verification lightweight:

- Headless checks for all three workspace modes.
- A snapshot check for model-space bounds and image metadata.
- A protection check for reference handles.
- A proposal cleanup check proving the reference remains.
- Static command-map coverage for new File IPC commands.
- Existing test suite and one screenshot smoke check.

## Out of Scope

- Reading screenshots or images as geometric references.
- Persisting workspace state across MCP server restarts.
- Automatic retries of drawing mutations.
- Full CAD constraint solving or semantic recognition of arbitrary diagrams.
- Replacing the existing consolidated tools.
