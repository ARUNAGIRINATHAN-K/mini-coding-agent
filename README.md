# Mini-Coding-Agent

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Ollama](https://img.shields.io/badge/Ollama-Supported-black.svg)](https://ollama.com/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

A lightweight, standalone coding agent written in Python and powered locally by [Ollama](https://ollama.com/).

<a href="https://magazine.sebastianraschka.com/p/components-of-a-coding-agent">
  <img src="https://substack-post-media.s3.amazonaws.com/public/images/49b97718-57f4-4977-99c8-8ad5c4d32af3_1548x862.png" width="600px" alt="Components of a Coding Agent">
</a>

## Overview

Mini-Coding-Agent implements a complete local agent loop: repository context collection, stable prompt composition, schema-checked and permission-gated tool execution, session persistence, context compression, and bounded delegation.

- **Shared core:** [`mini_coding_agent/`](mini_coding_agent/)
- **Compatibility launcher:** [`mini_coding_agent.py`](mini_coding_agent.py)
- **CLI entry point:** `mini-coding-agent`
- **Tutorial:** [Components of a Coding Agent](https://magazine.sebastianraschka.com/p/components-of-a-coding-agent)

## Features

The agent is built around six components:

1. **Live repo context:** Maintains a refreshable, metadata-only repository index with a filtered file tree, instructions, git state, diff summary, cache diagnostics, and guarded on-demand file reads.
2. **Prompt shape and cache reuse:** Keeps a stable system prefix separate from dynamic inputs to improve local model caching.
3. **Structured tools, validation, and permissions:** Runs named, schema-checked tools constrained by path validation and explicit approval rules.
4. **Context reduction and output management:** Deduplicates repeated reads, caps tool output length, and compresses turn history to stay within token budgets.
5. **Transcripts, memory, and resumption:** Saves session history and distilled working memory so sessions can be resumed across runs.
6. **Delegation and bounded subagents:** Spawns scoped, read-only subagents for isolated subtasks, with depth limits.

<a href="https://magazine.sebastianraschka.com/p/components-of-a-coding-agent">
  <img alt="Six core components of a coding agent" src="https://sebastianraschka.com/images/github/mini-coding-agent/six-components.webp" width="600px">
</a>

### Repository context

The agent persists a versioned, metadata-only context index at
`.mini-coding-agent/cache/repository-context-v1.json`. It records filtered
paths, file sizes, modification times, hashes, documentation summaries, and
Git metadata; it does not cache source-file contents. Use `/context` in the
interactive CLI to refresh the index. Successful `write_file` and `patch_file`
operations refresh only the affected paths before the next model turn.

### Tools

All tools enforce workspace path containment to prevent directory traversal and symlink escapes.

| Tool | Parameters | Risky | Description |
| --- | --- | :---: | --- |
| `list_files` | `path="."` | No | Lists up to 200 workspace files and directories. |
| `read_file` | `path`, `start=1`, `end=200` | No | Reads lines from a UTF-8 text file, with line numbers. |
| `search` | `pattern`, `path="."` | No | Searches file contents using `ripgrep`, with a standard fallback. |
| `run_shell` | `command`, `timeout=20` | **Yes** | Runs a shell command from the workspace root. |
| `write_file` | `path`, `content` | **Yes** | Creates or overwrites a file, creating parent folders as needed. |
| `patch_file` | `path`, `old_text`, `new_text` | **Yes** | Replaces an exact matching text block in a file. |
| `delegate` | `task`, `max_steps=3` | No | Spawns a read-only subagent to investigate a subtask. |

## Installation

### Requirements

- Python 3.10 or higher (no external runtime dependencies for basic usage)
- Ollama, installed natively or run via Docker
- *(Optional)* [`uv`](https://github.com/astral-sh/uv) for environment and project management

### Clone the repository

```bash
git clone https://github.com/rasbt/mini-coding-agent.git
cd mini-coding-agent
```

### Set up Ollama

**Native**

1. Install Ollama from [ollama.com/download](https://ollama.com/download).
2. Start the server:
```bash
   ollama serve
```
3. Pull the default model:
```bash
   ollama pull qwen3.5:4b
```

**Docker**

Use the provided `docker-compose.yml`:

```bash
# Start the Ollama container in the background
docker compose up -d ollama

# Pull the default model into the container
docker compose exec ollama ollama pull qwen3.5:4b

# Verify the server is reachable
curl http://127.0.0.1:11434/api/tags
```

> **GPU acceleration:** For NVIDIA GPUs, configure the [NVIDIA Container Toolkit](https://github.com/ollama/ollama/blob/main/docs/docker.mdx) on your host.

## Quick Start

With `uv` (recommended):

```bash
uv run mini-coding-agent
```

With Python directly:

```bash
python mini_coding_agent.py
```

To run a single task and exit, pass it as a positional argument:

```bash
uv run mini-coding-agent "Refactor binary_search.py and add unit tests"
```

## Usage

### Approval modes

Risky tools (`run_shell`, `write_file`, `patch_file`) are governed by an approval policy set with `--approval`:

| Mode | Behavior |
| --- | --- |
| `ask` (default, recommended) | Prompts for confirmation in the terminal before each risky tool call. |
| `auto` | Runs risky tools without confirmation. Use only with trusted prompts and repositories. |
| `never` | Rejects all risky tool calls. |

```bash
uv run mini-coding-agent --approval auto
```

### Sessions

Sessions are stored in `.mini-coding-agent/sessions/` inside the target workspace.

```bash
# Resume the most recent session
uv run mini-coding-agent --resume latest

# Resume a specific session
uv run mini-coding-agent --resume 20260401-144025-2dd0aa
```

### Interactive commands

In REPL mode, slash commands run locally and are not sent to the model.

| Command | Action |
| --- | --- |
| `/help` | Shows available commands. |
| `/memory` | Prints distilled session memory (task, tracked files, notes). |
| `/session` | Shows the path to the current session JSON file. |
| `/reset` | Clears session history and memory without exiting the REPL. |
| `/context` | Refreshes the repository index and prints its identifier and file count. |
| `/exit`, `/quit` | Exits the session. |

### CLI reference

```bash
uv run mini-coding-agent --help
```

| Flag | Default | Description |
| --- | --- | --- |
| `prompt` | none | Optional positional task prompt for one-shot execution. |
| `--cwd` | `.` | Workspace directory to inspect and modify. |
| `--model` | `qwen3.5:4b` | Ollama model name. |
| `--host` | `http://127.0.0.1:11434` | Ollama server URL. |
| `--ollama-timeout` | `300` | Ollama HTTP request timeout, in seconds. |
| `--resume` | none | Resume a session by ID or `latest`. |
| `--approval` | `ask` | Approval mode for risky tools: `ask`, `auto`, or `never`. |
| `--max-steps` | `6` | Maximum model and tool steps per user request. |
| `--max-new-tokens` | `512` | Maximum tokens generated per model step. |
| `--temperature` | `0.2` | Sampling temperature. |
| `--top-p` | `0.9` | Nucleus sampling threshold. |

## Contributing

The project is evolving into a shared agent runtime that supports a web dashboard (React/FastAPI) and an interactive terminal UI (Textual). Before contributing, review:

- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md): multi-phase UI and context upgrade roadmap
- [docs/architecture.md](docs/architecture.md): architectural boundaries and compatibility contracts
- [docs/events.md](docs/events.md): agent lifecycle event schema (version 1.0)
- [docs/baseline.md](docs/baseline.md): test baseline and verification guidelines
- [EXAMPLE.md](EXAMPLE.md): example agent execution session

## License

Licensed under the [Apache License 2.0](LICENSE).
