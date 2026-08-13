# IPC Session Lease Design

## Goal

Make the File IPC backend safe when multiple MCP server processes share the
same IPC directory, and make timeout failures useful enough to diagnose.

## Current Problem

Each backend instance uses a process-local asyncio lock, but separate MCP
processes do not coordinate. Before every dispatch, the backend deletes every
`autocad_mcp_cmd_*.json` file in the shared directory. One process can
therefore delete another process's active command.

If a process exits after writing a command, AutoCAD can later process that
abandoned command. Current timeout errors identify the request ID but do not
show whether the failure happened while waiting for the lease, triggering
AutoCAD, or polling for a result.

## Design

### Session-Owned Files

Each `FileIPCBackend` creates a random session ID at initialization. Request
IDs include the session ID and a random request suffix. This keeps command,
result, temporary, and LISP filenames unique and allows cleanup to identify
ownership.

The backend cleans only files belonging to its own session during normal
operation. It never removes another live session's command or result files.

### Cross-Process Dispatch Lease

All File IPC backends coordinate through one atomic lease file in the IPC
directory, such as `autocad_mcp_dispatch.lock`.

The lease lifecycle is:

1. Attempt exclusive file creation.
2. Write session ID, process ID, request ID, command, and acquisition time.
3. Refresh the lease timestamp while waiting for AutoCAD's result.
4. Release the lease only if it still belongs to the current request.
5. If the lease heartbeat is stale, reclaim it and clean only the abandoned
   session named in its metadata.

The lease covers command-file creation, dispatch triggering, result polling,
and cleanup. This serializes AutoCAD access across processes, which matches
AutoCAD's single command-line execution model.

If a process crashes, its lease eventually becomes stale. A later process can
reclaim the lease and remove abandoned files from that session. A current
process refreshes the lease during result polling, so a long-running command
is not mistaken for a crashed process.

The AutoLISP dispatcher does not change its command-file glob or command
payload contract.

### Diagnostics

Extend failed command results with optional structured details while keeping
the existing `ok`, `error`, and `payload` fields.

Diagnostics include:

- `phase`: `lease_wait`, `command_write`, `dispatch_trigger`, `result_poll`, or
  `recovery`.
- `session_id`.
- `request_id`.
- `command`.
- `elapsed_seconds` and configured timeout values.
- Whether the result file was observed.
- Whether malformed result files were observed.
- Whether recovery ESC was sent.
- Whether a stale lease was reclaimed.

Lease-wait timeouts are distinct from AutoCAD result-poll timeouts. Mutating
commands are never retried automatically after a timeout.

`system(operation="status")` reports the session ID, lease path, and existing
IPC configuration.

## Verification

Keep tests lightweight:

- Confirm distinct sessions generate distinct request paths.
- Confirm cleanup removes only the current session's files.
- Confirm lease acquisition, release, stale reclaim, and ownership checks.
- Confirm lease-wait and result-poll timeout details differ.
- Run the existing test suite.
- Run a headless smoke check to confirm the `ezdxf` backend is unaffected.

## Out of Scope

- Changing the AutoLISP dispatcher protocol or glob.
- Automatic command retries.
- Multi-machine coordination.
- Replacing file IPC with sockets or named pipes.
- Adding a lock-file dependency.
