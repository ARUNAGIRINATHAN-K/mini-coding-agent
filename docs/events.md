# Agent Lifecycle Event Contract

**Schema version:** `1.0`

## Purpose and scope

This document defines the event contract emitted by the shared agent core for
the future browser dashboard and terminal UI. Phase 1 emits `run_started`,
`model_requested`, `tool_requested`, `approval_required`,
`approval_resolved`, `tool_completed`, `run_completed`, and `run_failed`.
Phase 2 also emits `context_ready` and `context_invalidated`. The remaining
event types are reserved for later phases.

Events describe observable agent work. They do not authorize tool execution;
the core remains responsible for validation and approval.

## Envelope

Every event must use this JSON envelope:

```json
{
  "version": "1.0",
  "event_id": "unique event identifier",
  "run_id": "unique run identifier",
  "session_id": "persisted session identifier",
  "sequence": 1,
  "type": "run_started",
  "timestamp": "2026-09-29T00:00:00+00:00",
  "data": {}
}
```

| Field | Requirement |
| --- | --- |
| `version` | Required schema version. Consumers must reject unsupported major versions. |
| `event_id` | Required unique identifier for deduplication. |
| `run_id` | Required identifier for one submitted user request and its tool/model loop. |
| `session_id` | Required persisted session ID. |
| `sequence` | Required positive integer, strictly increasing within a run. |
| `type` | Required event type listed below. |
| `timestamp` | Required ISO 8601 UTC timestamp. |
| `data` | Required JSON object whose shape depends on `type`. |

Unknown fields are allowed and must be ignored by older consumers. Event `data`
must not include secrets or unredacted credentials.

## Event types

| Type | Required `data` fields | Meaning |
| --- | --- | --- |
| `run_started` | `user_message`, `max_steps` | A user request has been accepted. |
| `context_ready` | `context_id`, `summary`, `estimated_chars`, `refreshed` | Repository context is available for the run. |
| `prompt_ready` | `prefix_hash`, `estimated_chars`, `history_reduced` | Prompt parts have been assembled. |
| `model_requested` | `model`, `host`, `max_new_tokens`, `attempt` | A model request is about to be sent. |
| `model_responded` | `attempt`, `response_chars`, `parsed_kind` | A model response was received and parsed. |
| `tool_requested` | `tool_name`, `args`, `risky`, `step` | The model requested a validated tool. |
| `approval_required` | `tool_name`, `args`, `request_id`, `risk` | A risky tool awaits an allow/deny decision. |
| `approval_resolved` | `tool_name`, `request_id`, `decision`, `source` | An approval decision was applied. |
| `tool_completed` | `tool_name`, `args`, `success`, `output_chars`, `step` | Tool execution ended; output may be separately fetched or safely clipped. |
| `context_invalidated` | `paths`, `reason` | A successful write or refresh invalidated context data. |
| `run_completed` | `final_answer`, `tool_steps`, `attempts` | The run reached a final answer. |
| `run_failed` | `code`, `message`, `tool_steps`, `attempts` | The run stopped because of a recoverable or terminal error. |
| `run_cancelled` | `reason`, `tool_steps`, `attempts` | The user or service cancelled the run. |

## Lifecycle and ordering

A normal run uses this order:

```text
run_started
  -> context_ready
  -> prompt_ready
  -> model_requested
  -> model_responded
  -> [tool_requested -> approval_required -> approval_resolved]?
  -> tool_completed
  -> (prompt_ready -> model_requested -> model_responded -> ...)*
  -> run_completed
```

`approval_required` and `approval_resolved` occur only for risky tools under
an approval policy that asks the user. A denied approval is followed by a
`tool_completed` event with `success: false`; it is a tool result fed back to
the agent, not an implicit approval retry.

Every run has exactly one terminal event: `run_completed`, `run_failed`, or
`run_cancelled`. No event may follow a terminal event for the same `run_id`.

## Approval decisions

The service assigns the `request_id` in `approval_required`. A UI returns a
decision that matches that ID and contains only `allow` or `deny`. The core
must deny a request when the UI disconnects, the decision times out, the ID is
unknown, or the session/run does not match. A UI cannot request a tool directly
through this contract.

## Versioning rules

- Additive fields and new optional event types are minor-compatible changes.
- Removing or changing the meaning/type of an existing field requires a new
  major `version`.
- Producers must emit one schema version per run.
- Consumers must display a clear compatibility error for unsupported major
  versions rather than silently processing events.
