# Session Orchestration Design

## Goal

ModelConsole should support many independent agent sessions behind a thin UI client.
The first UI is the TUI, but the backend must also support a browser UI or other
automation clients without moving orchestration state into the UI.

The target model is:

- Each session owns its private conversation transcript.
- Entry-point and orchestrator sessions coordinate other sessions through compact
  state summaries, not by reading their full transcripts.
- Worker sessions can create their own subagent sessions.
- UI clients render state and send commands through stable APIs.
- Policy, execution, provider adapters, event streaming, and session storage stay
  separated enough to become separate processes later.

## Architecture

The MVP should run in one Python process, but the module and API boundaries should
be treated as microservice boundaries.

```text
UI clients
  +-- TUI
  +-- Browser UI
  +-- CLI automation
        |
        v
Gateway API
  |
  +-- Session Service
  +-- Event Service
  +-- Job Service
  +-- Orchestrator Service
  +-- Compression Service
  +-- Provider Adapter Service
  +-- Policy Service
  +-- Executor Service
```

The TUI must not be the authoritative owner of transcripts or running jobs. It is
a thin client for input, session selection, and event rendering.

## Service Responsibilities

### Gateway API

The gateway owns HTTP routing, request validation, response encoding, and API
compatibility. It should not contain orchestration decisions or provider-specific
stream parsing.

Existing `mcon.server.app` should be reduced toward this role over time.

### Session Service

The session service owns:

- session creation and metadata updates
- parent, child, and root session relationships
- private transcript storage
- compact state storage
- session status transitions
- session tree queries

Only the owning provider run should read a session's private transcript. Parent,
entry-point, and sibling sessions should consume only compact state and explicit
events.

### Event Service

The event service owns append-only event logs and pub/sub fan-out to UI clients.
All state changes that matter to a UI or orchestrator should be emitted as
events.

Events must be durable before they are published so reconnecting clients can
replay missed events.

### Job Service

The job service owns running process lifecycle:

- one running provider job per session by default
- job start and completion
- interrupt and cancellation
- server shutdown cleanup
- timeout handling

The current fire-and-forget TUI worker model should be replaced with server-side
job ownership.

### Orchestrator Service

The orchestrator service owns task decomposition and coordination. It should
never inspect worker private transcripts. It builds its prompt from:

- the active user request
- root session goal
- session tree metadata
- compact state for child sessions
- recent explicit events
- open blockers and approvals

It dispatches work by appending messages to worker sessions and starting runs.

### Compression Service

The compression service updates the compact state for a session. It runs after a
provider job completes and may also be called explicitly.

Compact state should be short, structured, and stable enough for orchestration:

```text
@session sess_worker_abc
state=running
goal="Implement isolated session APIs"
done=["SessionStore skeleton added", "event schema drafted"]
risk=["interrupt endpoint not wired"]
need=[]
next="connect provider adapter to JobService"
artifacts=["packages/mcon/src/mcon/server/sessions.py"]
```

### Provider Adapter Service

The provider adapter service hides Codex CLI, Claude Code, or future provider
differences. It converts provider-specific streams into ModelConsole events and
hands process ownership to the job service.

For Codex, the adapter should start:

```text
codex exec --json --sandbox <mode> --cd <cwd> -
```

The adapter should append assistant output to the owning session transcript only
after the run finishes successfully enough to produce usable assistant text.

### Policy Service

The policy service owns command, file, credential, and subject resolution. It
also produces compact policy summaries for prompts.

Prompt summaries are advisory. Enforcement must remain in the execution path.

### Executor Service

The executor service owns sandboxed command execution and session filesystem
application. Chat/provider `workspace-write` must not be treated as equivalent
to mcon executor enforcement until provider tools are routed through the executor
boundary.

The executor service must also be responsible for file-argument mediation for
commands that can read or write arbitrary paths. A command being allowed by
command policy is not enough to let it access paths outside the file policy.
For example, an allowed `git diff` must not be able to read `/etc/passwd` through
`git diff --no-index /etc/passwd /workspace/README.md`.

The sandbox runtime should avoid broad host binds such as all of `/etc` whenever
possible. If a runtime file is required, bind only the minimal file or directory
needed for that command and treat those paths as runtime dependencies, not as
agent-readable data.

## Data Model

### Session

```python
@dataclass
class Session:
    session_id: str
    root_session_id: str
    parent_session_id: str | None
    kind: Literal[
        "entrypoint",
        "orchestrator",
        "worker",
        "subagent",
        "reviewer",
    ]
    subject: str
    provider: str
    cwd: str
    sandbox: Literal["read-only", "workspace-write"]
    status: Literal[
        "idle",
        "running",
        "blocked",
        "waiting_approval",
        "complete",
        "failed",
        "cancelled",
    ]
    goal: str
    created_at: str
    updated_at: str
```

### Message

```python
@dataclass
class Message:
    message_id: str
    session_id: str
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    created_at: str
    metadata: dict[str, object]
```

Messages are private to their session unless deliberately copied into compact
state or explicit artifacts.

### Event

```python
@dataclass
class SessionEvent:
    event_id: str
    root_session_id: str
    session_id: str
    parent_session_id: str | None
    type: str
    created_at: str
    payload: dict[str, object]
```

Important event types:

```text
session.created
session.updated
session.started
session.completed
session.failed
session.cancelled
message.appended
assistant.delta
stderr.line
tool.started
tool.completed
tool.failed
approval.requested
approval.resolved
compressed_state.updated
artifact.created
```

### Job

```python
@dataclass
class Job:
    job_id: str
    session_id: str
    provider: str
    status: Literal["running", "completed", "failed", "cancelled"]
    started_at: str
    completed_at: str | None
```

The process object itself should stay in memory inside `JobService`, not in
persistent storage.

## State Storage Layout

Use JSON and JSONL for the MVP because they are easy to inspect and migrate.
Move to SQLite or Postgres only when locking, querying, or multi-process
coordination requires it.

Do not store session state in the workspace by default. Transcripts, compact
state, and event logs can contain user prompts, model output, paths, command
output, and credential names. They must not be accidentally committed with the
target repository.

The default state root should be outside `/workspace`:

```text
MCON_STATE_ROOT=/mcon/state
```

For local development outside the container, use a repository-ignored path only
when explicitly configured:

```text
MCON_STATE_ROOT=.mcon/state
```

If `.mcon/` is supported for local development, it must be added to `.gitignore`
before any implementation writes session data there.

```text
/mcon/state/
  sessions/
    sess_entry_001/
      session.json
      messages.jsonl
      events.jsonl
      compressed.txt
    sess_worker_abc/
      session.json
      messages.jsonl
      events.jsonl
      compressed.txt
```

Writes should be atomic per file where practical:

- append JSONL event
- fsync or flush before publish
- write compact state via temp file and rename

Use a session-level lock for metadata and transcript writes. Do not use a single
global lock for all sessions except during initial MVP simplification.

Session IDs must be generated by the server. They must not be used directly as
filesystem paths without validation. Use a fixed prefix and a restricted
character set such as `sess_[A-Za-z0-9_-]+`.

## API Design

All UI clients should use the same API.

```text
POST /api/sessions
GET  /api/sessions
GET  /api/sessions/{session_id}
GET  /api/sessions/{session_id}/tree

POST /api/sessions/{session_id}/messages
POST /api/sessions/{session_id}/runs
POST /api/sessions/{session_id}/interrupt
POST /api/sessions/{session_id}/compact

GET  /api/events/stream?root_session_id=...
GET  /api/sessions/{session_id}/events

POST /api/policy/explain-command
POST /api/policy/explain-file
POST /api/executor/run
```

Every endpoint that takes a session ID must verify that the target session
belongs to the caller's root session or authorized scope. The first MVP may be a
single-user local server, but the API shape should not rely on clients being
honest about parentage.

### Create Session

```http
POST /api/sessions
```

```json
{
  "kind": "worker",
  "parent_session_id": "sess_orchestrator_001",
  "root_session_id": "sess_entry_001",
  "subject": "mcon.agent.coder",
  "provider": "codex",
  "cwd": "/workspace",
  "sandbox": "read-only",
  "goal": "Implement session isolation"
}
```

If `root_session_id` is omitted, the new session becomes its own root.

### Append Message

```http
POST /api/sessions/{session_id}/messages
```

```json
{
  "role": "user",
  "content": "Proceed with the next implementation step.",
  "metadata": {
    "source": "tui"
  }
}
```

This endpoint appends a message only. It does not start a provider run.

### Start Run

```http
POST /api/sessions/{session_id}/runs
```

```json
{
  "mode": "normal"
}
```

Response:

```json
{
  "job_id": "job_123",
  "session_id": "sess_worker_abc",
  "status": "running"
}
```

The server should reject a second run for a session that is already running
unless an explicit queueing mode is added.

### Interrupt Run

```http
POST /api/sessions/{session_id}/interrupt
```

This endpoint terminates the active job for the session. It should emit
`session.cancelled` and leave the transcript in a valid state.

### Event Stream

MVP can use NDJSON:

```http
GET /api/events/stream?root_session_id=sess_entry_001
```

Example events:

```json
{"type":"session.started","session_id":"sess_worker_abc","payload":{"job_id":"job_123"}}
{"type":"assistant.delta","session_id":"sess_worker_abc","payload":{"text":"..."}}
{"type":"compressed_state.updated","session_id":"sess_worker_abc","payload":{"state":"@session sess_worker_abc\nstate=idle\n..."}}
{"type":"session.completed","session_id":"sess_worker_abc","payload":{"returncode":0}}
```

Browser support can be layered as SSE or WebSocket over the same event service.

The event stream needs reconnect and flow-control semantics before it is used by
more than one UI:

- clients should pass `after_event_id` or equivalent replay cursor
- the server should cap replay windows or paginate historical reads
- slow clients should not block provider jobs
- disconnected clients should not keep provider processes alive by accident
- event payloads should avoid embedding full private transcripts

## Local API Security

The current server is a local development control plane, but browser UI support
changes the threat model. A website in a user's browser may be able to reach
localhost endpoints unless the server defends against it.

Before enabling browser clients, add:

- an explicit local auth token or session cookie
- Origin and Host validation
- CSRF protection for state-changing browser requests
- CORS disabled by default
- request body size limits
- per-root-session authorization checks

TUI-only development can run with a trusted local mode, but that mode should be
explicit in configuration and visible in `/status`.

## Prompt Construction

Provider prompts differ by session kind.

### Worker Session Prompt

Worker prompts use that session's private transcript plus policy context for the
session subject and cwd. They do not include sibling transcripts.

### Orchestrator Session Prompt

Orchestrator prompts use compact state only:

```text
You are the ModelConsole orchestrator.
Coordinate child sessions using compact state only.
Do not infer private transcript details that are absent from compact state.

Root goal:
...

Child sessions:
@session sess_worker_abc
state=running
...

Latest user request:
...
```

If more detail is needed, the orchestrator asks the worker session to compact or
report a specific fact through a message. It does not read the private transcript
directly.

## TUI Changes

Remove authoritative transcript state from the TUI:

- remove long-term use of `TuiState.messages`
- replace direct `/api/chat/stream` calls with session APIs
- render events from `GET /api/events/stream`
- keep only active session, input buffer, and display preferences locally

Commands:

```text
/sessions
/tree
/new worker "goal..."
/session sess_worker_abc
/send sess_worker_abc "message..."
/run sess_worker_abc
/interrupt sess_worker_abc
/compact sess_worker_abc
/status
```

Plain input appends a user message to the active session and starts a run unless
the session is already running.

The TUI should not allow multiple in-flight runs for one logical conversation
session. It can still accept input while a run is active, but queued input should
remain pending until the current run completes. If the user wants parallel work,
the TUI should create or select a separate session explicitly.

## PR 3 Remediation Plan

The review comments on PR 3 map to two layers: immediate fixes that should land
before merging the PR, and structural fixes covered by this session
orchestration design.

### 1. Close the sandbox file-policy bypass

Immediate PR fix:

- Remove the unconditional broad `/etc` bind from the executor sandbox, or
  replace it with a minimal runtime allowlist.
- Add tests showing that an allowed command cannot read outside workspace policy,
  including `git diff --no-index /etc/passwd /workspace/README.md` or an
  equivalent fixture path.
- Add command-specific file argument checks for commands that can read arbitrary
  paths, starting with `git diff --no-index`.

Design follow-up:

- Move command argument path extraction into Policy Service.
- Make Executor Service enforce both command permission and file permission for
  path-bearing commands.
- Record denied path accesses as events so UI clients can explain why a command
  was blocked.

### 2. Reject chat `workspace-write` until executor integration exists

Immediate PR fix:

- Change `/api/chat/stream` to reject `sandbox=workspace-write` with a clear
  error, or force chat runs to `read-only`.
- Keep `/exec` as the only write-capable path because it goes through
  mcon-executor policy and sandbox enforcement.
- Update README to say chat write mode is intentionally disabled until provider
  tools are mediated by mcon.

Design follow-up:

- Provider Adapter Service should expose tool requests as structured events.
- Executor Service should be the only component that applies workspace writes.
- `workspace-write` can be re-enabled only when provider tools are routed through
  Executor Service and are subject to the same command/file policy checks.

### 3. Add server-owned job lifecycle and cancellation

Immediate PR fix:

- Wrap provider process streaming in `try/finally`.
- On write failure, client disconnect, timeout, or server shutdown, terminate
  the provider process and kill it after a short grace period.
- Avoid daemon-only TUI worker ownership for long-running provider work.

Design follow-up:

- Introduce Job Service before adding more providers.
- Add `/api/sessions/{session_id}/interrupt`.
- Emit `session.cancelled`, `session.failed`, and `session.completed` events from
  the server-side job owner, not from the UI.

### 4. Preserve conversation order through session ownership

Immediate PR fix:

- Serialize chat runs per TUI session, or mark each parallel run as an
  independent session.
- Do not append assistant responses into one transcript in completion order when
  prompts were built from different snapshots.
- Add tests for two queued messages where the second response completes first.

Design follow-up:

- Move transcript ownership to Session Service.
- Let the TUI queue input against the active session.
- Use separate worker sessions for parallel work and let Orchestrator Service
  coordinate them via compact state.

## Migration Plan

0. Patch PR 3 blockers: close the file-policy bypass, reject chat
   `workspace-write`, add provider process cleanup, and serialize per-session
   chat runs.
1. Add `schemas.py`, `events.py`, and `sessions.py`.
2. Add session creation, message append, event append, and event read APIs.
3. Add `jobs.py` with provider process ownership and interrupt support.
4. Move Codex streaming into `server.providers.codex`.
5. Add `/api/sessions/{id}/runs` and `/interrupt`.
6. Keep `/api/chat/stream` as a compatibility shim that creates or reuses a
   session internally.
7. Update the TUI to use session APIs and event streams.
8. Add `compression.py` and compact state updates after runs.
9. Add `orchestrator.py` for compact-state-only coordination.
10. Add browser-facing SSE or WebSocket once the event service is stable.

## Safety and Review Concerns

- Do not allow chat `workspace-write` to imply mcon executor enforcement.
- Do not treat command allow as file access allow; path-bearing command
  arguments must also satisfy file policy.
- Do not broadly bind host runtime directories into the sandbox if they contain
  readable data outside the workspace policy.
- Do not leak worker private transcripts to entry-point or sibling sessions.
- Do not store session state under the target workspace unless the path is
  explicitly configured and ignored by Git.
- Do not let cancelled provider processes survive after client disconnects or
  server shutdown.
- Do not append assistant messages from failed partial streams without marking
  the run status and preserving enough event context for debugging.
- Do not publish events before durable append succeeds.
- Do not add cross-session locks that serialize all workers unless it is a
  clearly documented MVP limitation.
- Do not let UI clients forge session parentage outside their root.
- Do not enable browser clients without local API protections against cross-site
  requests to localhost.

## Initial Test Coverage

The first implementation should add tests for:

- session creation with root and parent IDs
- message isolation between sessions
- event append and replay order
- compact state read and write
- rejecting concurrent runs for the same session
- interrupting a running job
- orchestrator prompt construction without private transcripts
- compatibility behavior for the existing `/api/chat/stream`
- path-bearing command denial for files outside policy
- chat `workspace-write` rejection until executor integration exists
- provider process cleanup on stream disconnect
- per-session queue ordering when responses complete out of order
