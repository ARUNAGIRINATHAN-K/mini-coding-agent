# Implementation Plan: Mini Coding Agent UI and Context Upgrade

## Objective

Evolve the current standalone, Ollama-backed coding agent into a shared agent
runtime with two user interfaces:

- a browser-based dashboard for visual monitoring and approvals
- an improved terminal interface for keyboard-first local use

The upgrade extends the project’s first two core capabilities: live repository
context and stable prompt construction/cache reuse. Ollama continues to run in
Docker and is accessed through its local API at `http://127.0.0.1:11434`.

## Target Architecture

```text
React dashboard ─┐
                 ├─ FastAPI service + WebSocket events ─ Agent core ─ Ollama
Textual terminal ┘                                      ├─ session store
                                                        └─ context/cache store
```

The agent core is the single source of truth for tools, path validation,
approval policy, sessions, prompting, repository context, and model calls.
Neither UI may directly execute model-requested tools.

## Recommended Stack

| Area | Technology | Reason |
| --- | --- | --- |
| Agent core and API | Python 3.10+, FastAPI, Uvicorn, Pydantic | Keeps the existing Python implementation and provides typed HTTP/WebSocket APIs. |
| Browser dashboard | React, TypeScript, Vite, CSS modules | A maintainable local dashboard with a small, standard frontend toolchain. |
| Terminal UI | Textual | Rich, cross-platform terminal UI while staying in Python. |
| Backend tests | pytest | Extends the existing test suite. |
| Browser tests | Playwright | Covers user-facing approval and run workflows. |
| Model runtime | Ollama Docker | Keeps model execution local and separately managed from the agent. |

## Phase 0 — Baseline and Design Contract

**Goal:** establish a safe baseline before restructuring the application.

### Work

- Run and record the existing test suite.
- Document the current CLI flags, slash commands, session JSON shape, tool
  names, approval modes, and Ollama request payload.
- Add a short architecture decision record describing the shared-core rule and
  loopback-only default for the web service.
- Define a versioned event schema for the agent lifecycle.

### Deliverables

- Baseline test result.
- `docs/architecture.md` and `docs/events.md`.
- Compatibility checklist for the existing CLI.

### Exit criteria

- Current tests pass unchanged.
- Existing CLI command lines and saved sessions have documented compatibility
  expectations.

## Phase 1 — Extract a Shared Agent Core

**Goal:** make the current logic reusable without changing behavior.

### Work

- Convert `mini_coding_agent.py` from a monolithic module into a package:

  ```text
  mini_coding_agent/
    agent.py
    context.py
    events.py
    model.py
    prompting.py
    sessions.py
    tools.py
    cli.py
  ```

- Preserve the `mini-coding-agent` entry point and existing command-line flags.
- Replace direct terminal input approval with an approval callback/interface.
- Emit typed lifecycle events, including `run_started`, `model_requested`,
  `tool_requested`, `approval_required`, `tool_completed`, `run_completed`,
  and `run_failed`.
- Keep current workspace boundary checks, tool validation, delegation limits,
  and approval defaults intact.

### Deliverables

- Importable shared core.
- Backward-compatible plain CLI.
- Unit tests for event order, approval callbacks, tool validation, and session
  persistence.

### Exit criteria

- All existing tests pass after the refactor.
- A plain CLI run performs the same agent loop and creates compatible sessions.

## Phase 2 — Extended Live Repository Context

**Goal:** replace the one-time workspace summary with a refreshable, inspectable
repository context service.

### Work

- Add a repository index that captures:
  - repository root, branch, status, recent commits, and diff summary
  - project instructions and documentation files
  - a filtered file tree, file sizes, modification times, and content hashes
  - ignored paths, including `.git`, `.mini-coding-agent`, build output, and
    configured ignore patterns
- Do not place full source contents in the default prompt. Include summaries
  and make full file contents available through guarded read tools.
- Add explicit context refresh and targeted invalidation after `write_file` or
  `patch_file` succeeds.
- Add context-budget accounting: included items, character/token estimate, and
  truncation reasons.
- Persist safe index metadata under `.mini-coding-agent/cache/`.

### Deliverables

- `RepositoryContextService` and versioned context-cache format.
- Context snapshot API for later UI use.
- Tests for ignore rules, file-change detection, refresh behavior, writes,
  symlink escapes, and context-budget limits.

### Exit criteria

- The agent can refresh context without restarting.
- A changed file invalidates affected metadata only.
- Internal state and files outside the workspace are never indexed or exposed.

## Phase 3 — Prompt Composition and Cache Reuse [Completed]

**Goal:** make prompt state explainable and reduce unnecessary recomputation.

### Work

- Model prompts as named parts:

  ```text
  SystemPrefix + RepositoryContext + SessionMemory + Transcript + CurrentRequest
  ```

- Compute and cache the stable prefix separately from changing user input.
- Use content-addressed cache keys containing prompt schema version, workspace
  identity, relevant context hashes, model configuration, and tool schema.
- Invalidate cache entries when repository context, tool definitions, or prompt
  schema changes.
- Preserve existing history reduction and repeated-read handling, while exposing
  prompt size and reduction decisions as events.
- Record diagnostics such as prefix hash, context age, estimated prompt size,
  and invalidation reason. Do not represent these diagnostics as evidence of a
  model-side cache hit unless Ollama exposes one.

### Deliverables

- `PromptComposer` and cache metadata store.
- Prompt/context diagnostics events.
- Tests for deterministic composition, cache reuse, targeted invalidation,
  changed tool schema, and prompt-size limits.

### Exit criteria

- Identical stable inputs produce an identical prefix hash.
- Writes, workspace changes, and configuration changes invalidate the correct
  cache entries.
- The full prompt remains within configured size limits.

## Phase 4 — Local API and Run Service

**Goal:** make agent runs available to both interfaces through one local
service.

### Work

- Add a FastAPI application, bound to `127.0.0.1` by default.
- Provide endpoints for health, configuration, session listing/loading,
  repository context, and safe file preview.
- Provide a WebSocket run channel that streams lifecycle events and accepts
  user requests, approval decisions, cancel requests, and context refreshes.
- Enforce one active run per session unless explicitly designed otherwise.
- Keep approval decisions server-side and deny risky actions when the client
  disconnects or times out.
- Add `mini-coding-agent serve` and configuration flags for listen host/port.

### Deliverables

- Local REST API and WebSocket protocol.
- OpenAPI documentation for REST endpoints.
- API/WebSocket tests for normal runs, approval allow/deny, disconnects,
  cancellation, malformed requests, and session resume.

### Exit criteria

- A local client can run an agent task and receive events in order.
- A risky tool cannot execute without an allowed approval decision under
  `--approval ask`.
- The service is not exposed to the network unless the user explicitly opts in.

## Phase 5 — Browser Dashboard

**Goal:** provide a clear, local visual interface for coding-agent work.

### Work

- Create a React/TypeScript application in `web/`.
- Build the dashboard around five views:
  - session list and resume/new-session controls
  - conversation and agent-event timeline
  - repository context tree and selected-context details
  - tool activity/output inspector
  - model, cache, session, and Docker/Ollama health status
- Add an approval dialog that shows the tool name, arguments, risk, and
  allow/deny choices before sending a decision.
- Add a prompt composer, cancel control, context refresh control, and clear
  empty/error/reconnect states.
- Keep the browser client free of filesystem access and model credentials.

### Deliverables

- `web/` application and local development/build scripts.
- Responsive dashboard usable at desktop widths.
- Component tests plus Playwright coverage for a complete approval flow.

### Exit criteria

- A user can start/resume a session, submit a request, review events, approve
  or deny risky work, inspect context/cache diagnostics, and view the final
  result entirely in the browser.

## Phase 6 — Improved Terminal Interface

**Goal:** offer a keyboard-first interface with feature parity for core flows.

### Work

- Build a Textual application backed by the same local API/event protocol.
- Provide panes for chat/events, workspace context, tool output, and status.
- Support keyboard commands for submit, approve, deny, cancel, refresh,
  session selection, memory display, and exit.
- Preserve `--plain` mode as a simple stdin/stdout interface for scripts,
  terminals without rich UI support, and compatibility.
- Ensure terminal approval prompts remain prominent and require an explicit
  choice.

### Deliverables

- `mini-coding-agent tui` command.
- Terminal UI integration tests and manual accessibility/keyboard checklist.

### Exit criteria

- The TUI can complete the same session and approval workflow as the dashboard.
- Existing plain terminal usage continues to work.

## Phase 7 — Packaging, Documentation, and Release Readiness

**Goal:** make the expanded project installable, testable, and understandable.

### Work

- Update package metadata with runtime, optional UI, and development
  dependencies.
- Add reproducible browser build instructions and application startup commands.
- Update the README with architecture, Docker Ollama setup, dashboard/TUI
  usage, approval/security behavior, session storage, troubleshooting, and
  screenshots.
- Add a release checklist covering tests, formatting/linting, dependency audit,
  Docker connectivity, and manual approval verification.
- Define an upgrade strategy for old session and cache formats.

### Deliverables

- Updated `README.md`, `EXAMPLE.md`, and troubleshooting guide.
- CI workflow for Python tests, frontend checks, and production frontend build.
- Versioned migration notes.

### Exit criteria

- A new user can start Ollama in Docker, launch either UI, run a safe task, and
  understand how approvals and local data storage work.
- CI passes for the supported Python and Node versions.

## Cross-Phase Security Requirements

- Keep all default services bound to loopback addresses.
- Never let the browser or TUI bypass the agent’s central path validation,
  approval checks, or tool validation.
- Treat `--approval auto` as a deliberate opt-in and surface its risk clearly
  in both UIs.
- Continue excluding agent state and version-control internals from ordinary
  file listings and context.
- Redact secrets from logs, diagnostics, and UI event payloads where possible.
- Do not add telemetry or remote persistence without explicit user consent.

## Suggested Delivery Order

Deliver Phases 0–3 first; they improve correctness and make the existing CLI a
stable foundation. Then deliver Phase 4 before either UI, followed by the
dashboard (Phase 5), terminal UI (Phase 6), and release documentation (Phase
7). Each phase is independently testable and should be merged only when its
exit criteria are met.
