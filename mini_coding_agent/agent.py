"""The reusable agent loop, response parser, and event integration."""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .context import RepositoryContextService, clip, now
from .events import EventEmitter
from .prompting import DEFAULT_MAX_PROMPT_CHARS, PromptComposer
from .tools import ToolManager, terminal_approval


class MiniAgent:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
        read_only=False,
        approval_callback=None,
        event_callback=None,
        max_prompt_chars=DEFAULT_MAX_PROMPT_CHARS,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.max_prompt_chars = max_prompt_chars
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.approval_callback = approval_callback or terminal_approval
        self.events = EventEmitter(event_callback)
        self._run_id = None
        self._tool_step = 0
        self.session = session or {
            "id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": {"task": "", "files": [], "notes": []},
        }
        self.tool_manager = ToolManager(self)
        self.tools = self.tool_manager.build()
        self.composer = PromptComposer(max_prompt_chars=self.max_prompt_chars)
        self.context_service = RepositoryContextService(self.root, workspace=self.workspace)
        self.prefix = self.build_prefix()
        self.session_path = self.session_store.save(self.session)

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(model_client=model_client, workspace=workspace, session_store=session_store, session=session_store.load(session_id), **kwargs)

    def subscribe(self, callback):
        self.events.subscribe(callback)

    def emit(self, event_type, **data):
        return self.events.emit(event_type, session_id=self.session["id"], run_id=self._run_id, **data)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        """Compatibility method retained for integrations using the old class."""
        return self.tool_manager.build()

    def build_prefix(self):
        prefix = self.composer.build_prefix(
            self.workspace,
            self.tools,
            self.context_service.prompt_summary(),
            context_id=self.context_service.context_id(),
        )
        diag = self.composer.last_diagnostics
        self.emit(
            "prompt_cached",
            cache_key=diag.get("cache_key"),
            local_prefix_cache_hit=diag.get("local_prefix_cache_hit"),
            prefix_hash=diag.get("prefix_hash"),
            context_id=diag.get("context_id"),
            estimated_chars=diag.get("estimated_chars"),
        )
        return prefix

    def context_snapshot(self):
        """Return an inspectable, content-free repository-context snapshot."""
        return self.context_service.snapshot()

    def refresh_context(self, paths=None, reason="manual_refresh"):
        """Refresh context without restarting the agent and rebuild its prefix."""
        snapshot = self.context_service.refresh(paths)
        self.prefix = self.build_prefix()
        self.emit(
            "context_ready",
            context_id=snapshot["context_id"],
            summary=self.context_service.prompt_summary(),
            estimated_chars=snapshot["budget"]["estimated_characters"],
            refreshed=True,
        )
        return snapshot

    def invalidate_context(self, paths, reason):
        if isinstance(paths, (str, Path)):
            paths = [paths]
        snapshot = self.context_service.invalidate(paths, reason)
        self.composer.invalidate_cache(reason=f"context_invalidated:{reason}")
        self.prefix = self.build_prefix()
        self.emit("context_invalidated", paths=[str(path) for path in paths], reason=reason)
        self.emit(
            "context_ready",
            context_id=snapshot["context_id"],
            summary=self.context_service.prompt_summary(),
            estimated_chars=snapshot["budget"]["estimated_characters"],
            refreshed=True,
        )
        return snapshot

    def memory_text(self):
        return self.composer.memory_text(self.session)

    def history_text(self):
        return self.composer.history_text(self.session)

    def prompt(self, user_message):
        return self.composer.prompt(self.prefix, self.session, user_message)

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    def note_tool(self, name, args, result):
        memory = self.session["memory"]
        path = args.get("path")
        if name in {"read_file", "write_file", "patch_file"} and path:
            self.remember(memory["files"], str(path), 8)
        self.remember(memory["notes"], f"{name}: {clip(str(result).replace(chr(10), ' '), 220)}", 5)

    def ask(self, user_message):
        memory = self.session["memory"]
        if not memory["task"]:
            memory["task"] = clip(user_message.strip(), 300)
        self._run_id = uuid.uuid4().hex
        self.emit("run_started", user_message=user_message, max_steps=self.max_steps)
        self.record({"role": "user", "content": user_message, "created_at": now()})
        tool_steps, attempts = 0, 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)
        try:
            while tool_steps < self.max_steps and attempts < max_attempts:
                attempts += 1
                full_prompt, diagnostics = self.composer.compose_with_diagnostics(
                    self.prefix,
                    self.session,
                    user_message,
                    context_id=self.context_service.context_id(),
                    tools=self.tools,
                    workspace=self.workspace,
                    repository_summary=self.context_service.prompt_summary(),
                )
                self.emit("prompt_composed", **diagnostics)
                self.emit(
                    "model_requested",
                    model=getattr(self.model_client, "model", None),
                    host=getattr(self.model_client, "host", None),
                    max_new_tokens=self.max_new_tokens,
                    attempt=attempts,
                )
                try:
                    raw = self.model_client.complete(full_prompt, self.max_new_tokens)
                except Exception as exc:
                    self.emit("run_failed", code="model_request_failed", message=str(exc), tool_steps=tool_steps, attempts=attempts)
                    raise
                kind, payload = self.parse(raw)
                if kind == "tool":
                    tool_steps += 1
                    self._tool_step = tool_steps
                    name, args = payload.get("name", ""), payload.get("args", {})
                    result = self.run_tool(name, args)
                    self.record({"role": "tool", "name": name, "args": args, "content": result, "created_at": now()})
                    self.note_tool(name, args, result)
                    continue
                if kind == "retry":
                    self.record({"role": "assistant", "content": payload, "created_at": now()})
                    continue
                final = (payload or raw).strip()
                self.record({"role": "assistant", "content": final, "created_at": now()})
                self.remember(memory["notes"], clip(final, 220), 5)
                self.emit("run_completed", final_answer=final, tool_steps=tool_steps, attempts=attempts)
                return final
            if attempts >= max_attempts and tool_steps < self.max_steps:
                final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            else:
                final = "Stopped after reaching the step limit without a final answer."
            self.record({"role": "assistant", "content": final, "created_at": now()})
            self.emit("run_completed", final_answer=final, tool_steps=tool_steps, attempts=attempts)
            return final
        finally:
            self._run_id = None
            self._tool_step = 0

    def repeated_tool_call(self, name, args):
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        return len(tool_events) >= 2 and all(item["name"] == name and item["args"] == args for item in tool_events[-2:])

    def run_tool(self, name, args):
        return self.tool_manager.execute(name, args)

    def tool_example(self, name):
        return self.tool_manager.example(name)

    def validate_tool(self, name, args):
        return self.tool_manager.validate(name, args)

    def approve(self, name, args):
        if self.read_only or self.approval_policy == "never":
            return False
        if self.approval_policy == "auto":
            return True
        request_id = uuid.uuid4().hex
        self.emit("approval_required", tool_name=name, args=args, request_id=request_id, risk="risky")
        allowed = bool(self.approval_callback(name, args))
        self.emit(
            "approval_resolved",
            tool_name=name,
            request_id=request_id,
            decision="allow" if allowed else "deny",
            source="callback",
        )
        return allowed

    def path_is_within_root(self, resolved):
        probe = resolved
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        for candidate in (probe, *probe.parents):
            try:
                if candidate.samefile(self.root):
                    return True
            except OSError:
                continue
        return False

    def path(self, raw_path):
        return self.tool_manager.path(raw_path)

    # Compatibility methods for callers that invoked old concrete tool methods.
    # They retain validation but intentionally do not run approval, matching the
    # old split between individual tool implementations and ``run_tool``.
    def _direct_tool(self, name, args, implementation):
        self.validate_tool(name, args)
        return implementation(args)

    def tool_list_files(self, args): return self._direct_tool("list_files", args, self.tool_manager.list_files)
    def tool_read_file(self, args): return self._direct_tool("read_file", args, self.tool_manager.read_file)
    def tool_search(self, args): return self._direct_tool("search", args, self.tool_manager.search)
    def tool_run_shell(self, args): return self._direct_tool("run_shell", args, self.tool_manager.run_shell)
    def tool_write_file(self, args): return self._direct_tool("write_file", args, self.tool_manager.write_file)
    def tool_patch_file(self, args): return self._direct_tool("patch_file", args, self.tool_manager.patch_file)
    def tool_delegate(self, args): return self._direct_tool("delegate", args, self.tool_manager.delegate)

    @staticmethod
    def parse(raw):
        raw = str(raw)
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            try:
                payload = json.loads(MiniAgent.extract(raw, "tool"))
            except Exception:  # noqa: BLE001
                return "retry", MiniAgent.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", MiniAgent.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", MiniAgent.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", MiniAgent.retry_notice()
            return "tool", payload
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = MiniAgent.parse_xml_tool(raw)
            return ("tool", payload) if payload is not None else ("retry", MiniAgent.retry_notice())
        if "<final>" in raw:
            final = MiniAgent.extract(raw, "final").strip()
            return ("final", final) if final else ("retry", MiniAgent.retry_notice("model returned an empty <final> answer"))
        raw = raw.strip()
        return ("final", raw) if raw else ("retry", MiniAgent.retry_notice("model returned an empty response"))

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice" + (f": {problem}" if problem else ": model returned malformed tool output")
        return f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. For multi-line files, prefer <tool name=\"write_file\" path=\"file.py\"><content>...</content></tool>."

    @staticmethod
    def parse_xml_tool(raw):
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.DOTALL)
        if not match:
            return None
        attrs = MiniAgent.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None
        body, args = match.group("body"), dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
            if f"<{key}>" in body:
                args[key] = MiniAgent.extract_raw(body, key)
        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        return {"name": name, "args": args}

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r'''([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')''', text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text, tag):
        start_tag, end_tag = f"<{tag}>", f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        return text[start:].strip() if end == -1 else text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag, end_tag = f"<{tag}>", f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        return text[start:] if end == -1 else text[start:end]

    def reset(self):
        self.session["history"] = []
        self.session["memory"] = {"task": "", "files": [], "notes": []}
        self.session_path = self.session_store.save(self.session)
