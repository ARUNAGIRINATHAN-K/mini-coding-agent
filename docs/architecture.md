# Architecture and Compatibility Contract

**Status:** Accepted for Phase 0

## Current architecture

The project is a single-process Python command-line application with an
importable shared core. The `mini_coding_agent/` package separates the agent
loop, workspace context, prompts, tools, sessions, model client, events, and
plain CLI. `mini_coding_agent.py` remains a thin compatibility launcher for
direct script execution.

```text
CLI input -> MiniAgent -> OllamaModelClient -> Ollama /api/generate
                 |
                 +-> validated workspace tools
                 +-> approval prompt for risky tools
                 +-> .mini-coding-agent/sessions/<session-id>.json
```

Ollama can run directly on the host or in Docker. The current default endpoint
is `http://127.0.0.1:11434`.

## Architecture decisions for the UI upgrade

### Shared-core rule

The browser dashboard and terminal UI must use one shared agent core. Tools,
path containment, approval policy, session persistence, prompt composition,
repository context, and Ollama calls belong to that core. A UI may display or
request an action, but it must not reimplement, bypass, or directly invoke a
model-requested tool.

This prevents behavioral drift between interfaces and keeps risky operations
behind one approval and validation boundary.

### Local service default

The future web service must bind to `127.0.0.1` by default. It must not bind to
all network interfaces unless the user explicitly supplies an opt-in listen
address. Dashboard access is local by default; no remote authentication,
telemetry, or external persistence is implied by this plan.

### Event boundary

The shared core publishes versioned lifecycle events. Both interfaces will
consume the same event contract defined in [events.md](events.md).

## Existing CLI compatibility checklist

The following behavior must remain available through the plain CLI after the
Phase 1 refactor and later UI work.

| Area | Compatibility requirement |
| --- | --- |
| Entrypoint | `mini-coding-agent` continues to invoke the application. Running `python mini_coding_agent.py` remains supported while the compatibility module exists. |
| Interactive mode | Starting with no positional prompt opens the prompt loop and prints the welcome screen. |
| One-shot mode | Positional words are joined with spaces and submitted as one request. |
| `--cwd` | Continues to select the workspace, defaulting to `.`. |
| `--model` | Continues to default to `qwen3.5:4b`. |
| `--host` | Continues to default to `http://127.0.0.1:11434`. |
| `--ollama-timeout` | Continues to default to 300 seconds. |
| `--resume` | Accepts a session ID or `latest`; omitted starts a new session. |
| `--approval` | Continues to accept exactly `ask`, `auto`, and `never`, defaulting to `ask`. |
| Generation limits | `--max-steps` defaults to 6 and `--max-new-tokens` defaults to 512. |
| Sampling controls | `--temperature` defaults to 0.2 and `--top-p` defaults to 0.9. |
| Slash commands | `/help`, `/memory`, `/session`, `/reset`, `/context`, `/exit`, and `/quit` keep their documented meanings. `/quit` remains an alias for `/exit`. |
| Session location | Sessions remain at `<workspace-root>/.mini-coding-agent/sessions/<session-id>.json`. |
| Session fields | Existing `id`, `created_at`, `workspace_root`, `history`, and `memory` fields remain readable. New fields must be additive or be migrated explicitly. |
| Tool safety | Workspace path containment, validation, approval behavior, output clipping, and delegation bounds may not be weakened. |

## Current CLI flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `prompt` | none | Optional positional words joined into a one-shot task. |
| `--cwd` | `.` | Workspace directory to inspect and modify. |
| `--model` | `qwen3.5:4b` | Ollama model name. |
| `--host` | `http://127.0.0.1:11434` | Ollama server URL. |
| `--ollama-timeout` | `300` | Ollama request timeout in seconds. |
| `--resume` | none | A session ID or `latest`. |
| `--approval` | `ask` | Risky-action policy: `ask`, `auto`, or `never`. |
| `--max-steps` | `6` | Maximum valid tool/model iterations per request. |
| `--max-new-tokens` | `512` | Maximum generated tokens per model call. |
| `--temperature` | `0.2` | Sampling temperature. |
| `--top-p` | `0.9` | Nucleus sampling value. |

## Current slash commands

| Command | Current behavior |
| --- | --- |
| `/help` | Prints the interactive-command help text. |
| `/memory` | Prints distilled memory: task, tracked files, and notes. |
| `/session` | Prints the active saved-session path. |
| `/reset` | Clears history and memory in the current session and saves it. |
| `/context` | Refreshes the repository index and prints its context ID and indexed file count. |
| `/exit` | Exits the interactive loop. |
| `/quit` | Alias for `/exit`. |

## Current tool contract

| Tool | Arguments | Risky | Current behavior |
| --- | --- | --- |
| `list_files` | `path` (default `.`) | No | Lists up to 200 non-internal entries. |
| `read_file` | `path`, `start` (default 1), `end` (default 200) | No | Reads a UTF-8 file with line numbers. |
| `search` | `pattern`, `path` (default `.`) | No | Uses `rg` when available, otherwise a text-search fallback. |
| `run_shell` | `command`, `timeout` (default 20; 1–120) | Yes | Runs a shell command from the repository root. |
| `write_file` | `path`, `content` | Yes | Creates parent directories and writes UTF-8 text. |
| `patch_file` | `path`, `old_text`, `new_text` | Yes | Replaces exactly one matching text block. |
| `delegate` | `task`, `max_steps` (default 3) | No | Starts one bounded, read-only child agent; unavailable beyond the maximum depth. |

All paths resolve within the workspace root. Path traversal and resolvable
symlink escapes are rejected. The agent rejects a third consecutive identical
tool call. Tool output is clipped to 4,000 characters.

## Current approval modes

| Mode | Behavior |
| --- | --- |
| `ask` | Prompts the local terminal for every risky tool; only `y` or `yes` allows it. EOF denies the action. |
| `auto` | Allows risky tools automatically. Use only for trusted prompts and workspaces. |
| `never` | Denies risky tools. |

A read-only delegated agent also denies risky tools regardless of policy.

## Current persisted session shape

New sessions are saved as JSON under the session location described above.
The current top-level shape is:

```json
{
  "id": "YYYYMMDD-HHMMSS-abcdef",
  "created_at": "2026-09-29T00:00:00+00:00",
  "workspace_root": "absolute workspace path",
  "history": [],
  "memory": {
    "task": "",
    "files": [],
    "notes": []
  }
}
```

History entries are append-only records with `role`, `content`, and
`created_at`. Tool entries also contain `name` and `args`:

```json
{
  "role": "tool",
  "name": "read_file",
  "args": {"path": "README.md", "start": 1, "end": 20},
  "content": "# README.md\\n   1: ...",
  "created_at": "2026-09-29T00:00:00+00:00"
}
```

The format currently has no explicit schema version. Future work must either
continue accepting this shape or add a documented migration before changing or
renaming fields.

## Current Ollama request contract

For each model turn, the client sends an HTTP `POST` to
`<host>/api/generate` with `Content-Type: application/json`:

```json
{
  "model": "qwen3.5:4b",
  "prompt": "assembled agent prompt",
  "stream": false,
  "raw": false,
  "think": false,
  "options": {
    "num_predict": 512,
    "temperature": 0.2,
    "top_p": 0.9
  }
}
```

The values shown are defaults. The client expects a JSON response whose
`response` field is the model text; an `error` field becomes a runtime error.
