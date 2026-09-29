"""Prompt construction, named part composition, and stable prefix caching."""

import hashlib
import json
from dataclasses import dataclass

from .context import MAX_HISTORY, clip, now

PROMPT_SCHEMA_VERSION = "1.0"
DEFAULT_MAX_PROMPT_CHARS = 32000


@dataclass(frozen=True)
class PromptParts:
    """Named components of the prompt assembly."""

    system_prefix: str
    repository_context: str
    session_memory: str
    transcript: str
    current_request: str

    def to_dict(self):
        return {
            "system_prefix": self.system_prefix,
            "repository_context": self.repository_context,
            "session_memory": self.session_memory,
            "transcript": self.transcript,
            "current_request": self.current_request,
        }

    @property
    def prefix(self) -> str:
        return "\n\n".join([self.system_prefix, self.repository_context])

    @property
    def full_prompt(self) -> str:
        return "\n\n".join([
            self.system_prefix,
            self.repository_context,
            self.session_memory,
            "Transcript:\n" + self.transcript,
            "Current user request:\n" + self.current_request,
        ])


@dataclass
class PrefixCacheEntry:
    """Cache metadata entry for a stable prefix."""

    cache_key: str
    prefix: str
    system_prefix: str
    repository_context: str
    schema_version: str
    workspace_root: str
    context_id: str
    tool_schema_hash: str
    model_config_hash: str
    created_at: str
    hits: int = 0

    def to_dict(self):
        return {
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "workspace_root": self.workspace_root,
            "context_id": self.context_id,
            "tool_schema_hash": self.tool_schema_hash,
            "model_config_hash": self.model_config_hash,
            "created_at": self.created_at,
            "hits": self.hits,
            "prefix_hash": hashlib.sha256(self.prefix.encode("utf-8")).hexdigest(),
        }


class PrefixCacheStore:
    """In-memory metadata store for content-addressed prefix cache entries."""

    def __init__(self):
        self._entries: dict[str, PrefixCacheEntry] = {}
        self.last_invalidation_reason: str | None = None

    @staticmethod
    def compute_cache_key(
        schema_version: str,
        workspace_root: str,
        context_id: str,
        tool_schema_hash: str,
        model_config_hash: str = "",
    ) -> str:
        raw_key = f"{schema_version}:{workspace_root}:{context_id}:{tool_schema_hash}:{model_config_hash}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def get(self, cache_key: str) -> PrefixCacheEntry | None:
        entry = self._entries.get(cache_key)
        if entry:
            entry.hits += 1
        return entry

    def put(self, entry: PrefixCacheEntry) -> None:
        self._entries[entry.cache_key] = entry

    def invalidate(self, reason: str = "manual_invalidation") -> int:
        count = len(self._entries)
        self._entries.clear()
        self.last_invalidation_reason = reason
        return count

    def get_metadata(self) -> dict:
        return {
            "total_entries": len(self._entries),
            "last_invalidation_reason": self.last_invalidation_reason,
            "keys": list(self._entries.keys()),
        }


def compute_tool_schema_hash(tools: dict) -> str:
    """Compute a deterministic hash of tool definitions and schemas."""
    simplified = {}
    for name, tool in sorted(tools.items()):
        simplified[name] = {
            "description": tool.get("description", ""),
            "risky": bool(tool.get("risky", False)),
            "schema": tool.get("schema", {}),
        }
    serialized = json.dumps(simplified, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def build_system_prefix(tools: dict) -> str:
    """Build system rules, tool declarations, and response examples."""
    tool_lines = []
    for name, tool in tools.items():
        fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        risk = "approval required" if tool["risky"] else "safe"
        tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
    examples = "\n".join([  # noqa: FLY002
        '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
        '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
        '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
        "<final>Done.</final>",
    ])
    rules = "\n".join([  # noqa: FLY002
        "- Use tools instead of guessing about the workspace.",
        "- Return exactly one <tool>...</tool> or one <final>...</final>.",
        "- Tool calls must look like:", '  <tool>{"name":"tool_name","args":{...}}</tool>',
        "- For write_file and patch_file with multi-line text, prefer XML style:",
        '  <tool name="write_file" path="file.py"><content>...</content></tool>',
        "- Final answers must look like:", "  <final>your answer</final>",
        "- Never invent tool results.", "- Keep answers concise and concrete.",
        "- If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.",
        "- Before writing tests for existing code, read the implementation first.",
        "- When writing tests, match the current implementation unless the user explicitly asked you to change the code.",
        "- New files should be complete and runnable, including obvious imports.",
        "- Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.",
        "- Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, or delegate with args={}.",
    ])
    return "\n\n".join([
        "You are Mini-Coding-Agent, a small local coding agent running through Ollama.",
        "Rules:\n" + rules,
        "Tools:\n" + "\n".join(tool_lines),
        "Valid response examples:\n" + examples,
    ])


class PromptComposer:
    """Assembles prompt parts, handles cache reuse, and emits diagnostics."""

    def __init__(
        self,
        schema_version: str = PROMPT_SCHEMA_VERSION,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    ):
        self.schema_version = schema_version
        self.max_prompt_chars = max_prompt_chars
        self.cache_store = PrefixCacheStore()
        self.last_diagnostics: dict = {}

    def invalidate_cache(self, reason: str = "manual_invalidation") -> int:
        return self.cache_store.invalidate(reason)

    def build_prefix(
        self,
        workspace,
        tools,
        repository_summary=None,
        context_id=None,
        model_config=None,
    ) -> str:
        workspace_root = str(getattr(workspace, "repo_root", getattr(workspace, "cwd", ".")))
        repo_context_text = repository_summary or (workspace.text() if hasattr(workspace, "text") else "")
        if not context_id:
            context_id = hashlib.sha256(repo_context_text.encode("utf-8")).hexdigest()[:16]

        tool_hash = compute_tool_schema_hash(tools)
        model_hash = hashlib.sha256(json.dumps(model_config or {}, sort_keys=True).encode("utf-8")).hexdigest()[:16]

        cache_key = self.cache_store.compute_cache_key(
            schema_version=self.schema_version,
            workspace_root=workspace_root,
            context_id=context_id,
            tool_schema_hash=tool_hash,
            model_config_hash=model_hash,
        )

        cached_entry = self.cache_store.get(cache_key)
        if cached_entry:
            prefix = cached_entry.prefix
            cache_hit = True
            invalidation_reason = None
        else:
            system_prefix = build_system_prefix(tools)
            repository_context = repo_context_text
            prefix = "\n\n".join([system_prefix, repository_context])
            entry = PrefixCacheEntry(
                cache_key=cache_key,
                prefix=prefix,
                system_prefix=system_prefix,
                repository_context=repository_context,
                schema_version=self.schema_version,
                workspace_root=workspace_root,
                context_id=context_id,
                tool_schema_hash=tool_hash,
                model_config_hash=model_hash,
                created_at=now(),
            )
            self.cache_store.put(entry)
            cache_hit = False
            invalidation_reason = self.cache_store.last_invalidation_reason

        prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        self.last_diagnostics = {
            "cache_key": cache_key,
            "local_prefix_cache_hit": cache_hit,
            "prefix_hash": prefix_hash,
            "schema_version": self.schema_version,
            "workspace_root": workspace_root,
            "context_id": context_id,
            "tool_schema_hash": tool_hash,
            "invalidation_reason": invalidation_reason,
            "estimated_chars": len(prefix),
            "estimated_tokens": (len(prefix) + 3) // 4,
        }
        return prefix

    @staticmethod
    def memory_text(session) -> str:
        memory = session["memory"]
        notes = "\n".join(f"- {note}" for note in memory["notes"]) or "- none"
        return "\n".join([
            "Memory:", f"- task: {memory['task'] or '-'}", f"- files: {', '.join(memory['files']) or '-'}",
            "- notes:", notes,
        ])

    @staticmethod
    def history_text(session) -> str:
        text, _ = PromptComposer.history_text_with_reduction(session)
        return text

    @staticmethod
    def history_text_with_reduction(session) -> tuple[str, dict]:
        history = session.get("history", [])
        if not history:
            return "- empty", {"total_items": 0, "skipped_reads": 0, "truncated_chars": 0}
        lines, seen_reads = [], set()
        recent_start = max(0, len(history) - 6)
        skipped_reads = 0
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] in ("write_file", "patch_file"):
                seen_reads.discard(str(item["args"].get("path", "")))
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    skipped_reads += 1
                    continue
                seen_reads.add(path)
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], 900 if recent else 180))
            else:
                lines.append(f"[{item['role']}] {clip(item['content'], 900 if recent else 220)}")
        raw_text = "\n".join(lines)
        clipped_text = clip(raw_text, MAX_HISTORY)
        truncated_chars = max(0, len(raw_text) - len(clipped_text))
        reduction_stats = {
            "total_items": len(history),
            "skipped_reads": skipped_reads,
            "truncated_chars": truncated_chars,
        }
        return clipped_text, reduction_stats

    def compose_parts(
        self,
        prefix: str,
        session: dict,
        user_message: str,
        tools: dict | None = None,
        workspace=None,
        repository_summary: str | None = None,
    ) -> PromptParts:
        if tools:
            sys_prefix = build_system_prefix(tools)
            if prefix and sys_prefix in prefix:
                repo_ctx = prefix.split(sys_prefix, 1)[1].strip()
            elif repository_summary:
                repo_ctx = repository_summary
            else:
                repo_ctx = workspace.text() if workspace and hasattr(workspace, "text") else ""
        else:
            if "\n\nWorkspace:" in prefix:
                parts = prefix.split("\n\nWorkspace:", 1)
                sys_prefix = parts[0]
                repo_ctx = "Workspace:" + parts[1]
            else:
                sys_prefix = prefix
                repo_ctx = ""

        session_mem = self.memory_text(session)
        transcript = self.history_text(session)
        return PromptParts(
            system_prefix=sys_prefix,
            repository_context=repo_ctx,
            session_memory=session_mem,
            transcript=transcript,
            current_request=user_message,
        )

    def prompt(self, prefix: str, session: dict, user_message: str) -> str:
        full_prompt, _ = self.compose_with_diagnostics(prefix, session, user_message)
        return full_prompt

    def compose_with_diagnostics(
        self,
        prefix: str,
        session: dict,
        user_message: str,
        context_id: str | None = None,
        tools: dict | None = None,
        workspace=None,
        repository_summary: str | None = None,
    ) -> tuple[str, dict]:
        parts = self.compose_parts(prefix, session, user_message, tools, workspace, repository_summary)
        full_prompt = parts.full_prompt
        _, reduction_stats = self.history_text_with_reduction(session)

        within_budget = True
        truncated = False
        if len(full_prompt) > self.max_prompt_chars:
            within_budget = False
            full_prompt = full_prompt[:self.max_prompt_chars]
            truncated = True

        prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        parts_sizes = {
            "system_prefix": len(parts.system_prefix),
            "repository_context": len(parts.repository_context),
            "session_memory": len(parts.session_memory),
            "transcript": len(parts.transcript),
            "current_request": len(parts.current_request),
        }

        diagnostics = {
            "prefix_hash": prefix_hash,
            "cache_key": self.last_diagnostics.get("cache_key", ""),
            "local_prefix_cache_hit": self.last_diagnostics.get("local_prefix_cache_hit", False),
            "context_id": context_id or self.last_diagnostics.get("context_id", ""),
            "estimated_chars": len(full_prompt),
            "estimated_tokens": (len(full_prompt) + 3) // 4,
            "max_prompt_chars": self.max_prompt_chars,
            "within_budget": within_budget,
            "truncated": truncated,
            "parts_sizes": parts_sizes,
            "history_reduction": reduction_stats,
            "invalidation_reason": self.last_diagnostics.get("invalidation_reason"),
        }
        return full_prompt, diagnostics

