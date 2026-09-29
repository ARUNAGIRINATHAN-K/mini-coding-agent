"""Validated workspace tools and approval callback interface."""

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .context import IGNORED_PATH_NAMES, clip

ApprovalCallback = Callable[[str, dict], bool]


def terminal_approval(name, args):
    """Compatibility callback used by the plain terminal CLI."""
    try:
        answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


class ToolManager:
    def __init__(self, agent):
        self.agent = agent

    def build(self):
        tools = {
            "list_files": {"schema": {"path": "str='.'"}, "risky": False, "description": "List files in the workspace.", "run": self.list_files},
            "read_file": {"schema": {"path": "str", "start": "int=1", "end": "int=200"}, "risky": False, "description": "Read a UTF-8 file by line range.", "run": self.read_file},
            "search": {"schema": {"pattern": "str", "path": "str='.'"}, "risky": False, "description": "Search the workspace with rg or a simple fallback.", "run": self.search},
            "run_shell": {"schema": {"command": "str", "timeout": "int=20"}, "risky": True, "description": "Run a shell command in the repo root.", "run": self.run_shell},
            "write_file": {"schema": {"path": "str", "content": "str"}, "risky": True, "description": "Write a text file.", "run": self.write_file},
            "patch_file": {"schema": {"path": "str", "old_text": "str", "new_text": "str"}, "risky": True, "description": "Replace one exact text block in a file.", "run": self.patch_file},
        }
        if self.agent.depth < self.agent.max_depth:
            tools["delegate"] = {"schema": {"task": "str", "max_steps": "int=3"}, "risky": False, "description": "Ask a bounded read-only child agent to investigate.", "run": self.delegate}
        return tools

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.agent.root / path
        resolved = path.resolve()
        probe = resolved
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        for candidate in (probe, *probe.parents):
            try:
                if candidate.samefile(self.agent.root):
                    return resolved
            except OSError:
                continue
        raise ValueError(f"path escapes workspace: {raw_path}")

    def validate(self, name, args):
        args = args or {}
        if name == "list_files":
            if not self.path(args.get("path", ".")).is_dir():
                raise ValueError("path is not a directory")
        elif name == "read_file":
            if not self.path(args["path"]).is_file():
                raise ValueError("path is not a file")
            start, end = int(args.get("start", 1)), int(args.get("end", 200))
            if start < 1 or end < start:
                raise ValueError("invalid line range")
        elif name == "search":
            if not str(args.get("pattern", "")).strip():
                raise ValueError("pattern must not be empty")
            self.path(args.get("path", "."))
        elif name == "run_shell":
            if not str(args.get("command", "")).strip():
                raise ValueError("command must not be empty")
            timeout = int(args.get("timeout", 20))
            if timeout < 1 or timeout > 120:
                raise ValueError("timeout must be in [1, 120]")
        elif name == "write_file":
            path = self.path(args["path"])
            if path.exists() and path.is_dir():
                raise ValueError("path is a directory")
            if "content" not in args:
                raise ValueError("missing content")
        elif name == "patch_file":
            path = self.path(args["path"])
            if not path.is_file():
                raise ValueError("path is not a file")
            old_text = str(args.get("old_text", ""))
            if not old_text:
                raise ValueError("old_text must not be empty")
            if "new_text" not in args:
                raise ValueError("missing new_text")
            count = path.read_text(encoding="utf-8").count(old_text)
            if count != 1:
                raise ValueError(f"old_text must occur exactly once, found {count}")
        elif name == "delegate":
            if self.agent.depth >= self.agent.max_depth:
                raise ValueError("delegate depth exceeded")
            if not str(args.get("task", "")).strip():
                raise ValueError("task must not be empty")

    @staticmethod
    def example(name):
        return {
            "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
            "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
            "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
            "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
            "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
            "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
        }.get(name, "")

    def execute(self, name, args):
        tool = self.agent.tools.get(name)
        if tool is None:
            return f"error: unknown tool '{name}'"
        try:
            self.validate(name, args)
        except Exception as exc:  # noqa: BLE001
            message = f"error: invalid arguments for {name}: {exc}"
            example = self.example(name)
            return message + (f"\nexample: {example}" if example else "")
        if self.agent.repeated_tool_call(name, args):
            return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
        step = self.agent._tool_step
        self.agent.emit("tool_requested", tool_name=name, args=args, risky=tool["risky"], step=step)
        if tool["risky"] and not self.agent.approve(name, args):
            result = f"error: approval denied for {name}"
            self.agent.emit("tool_completed", tool_name=name, args=args, success=False, output_chars=len(result), step=step)
            return result
        try:
            result = clip(tool["run"](args))
        except Exception as exc:  # noqa: BLE001
            result = f"error: tool {name} failed: {exc}"
        self.agent.emit("tool_completed", tool_name=name, args=args, success=not result.startswith("error:"), output_chars=len(result), step=step)
        return result

    def list_files(self, args):
        path = self.path(args.get("path", "."))
        entries = [item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower())) if item.name not in IGNORED_PATH_NAMES]
        return "\n".join(("[D]" if entry.is_dir() else "[F]") + f" {entry.relative_to(self.agent.root)}" for entry in entries[:200]) or "(empty)"

    def read_file(self, args):
        path = self.path(args["path"])
        start, end = int(args.get("start", 1)), int(args.get("end", 200))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
        return f"# {path.relative_to(self.agent.root)}\n{body}"

    def search(self, args):
        pattern, path = str(args.get("pattern", "")).strip(), self.path(args.get("path", "."))
        if shutil.which("rg"):
            result = subprocess.run(["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)], cwd=self.agent.root, capture_output=True, text=True, check=False)
            return result.stdout.strip() or result.stderr.strip() or "(no matches)"
        matches = []
        files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(self.agent.root).parts)]
        for file_path in files:
            for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if pattern.lower() in line.lower():
                    matches.append(f"{file_path.relative_to(self.agent.root)}:{number}:{line}")
                    if len(matches) >= 200:
                        return "\n".join(matches)
        return "\n".join(matches) or "(no matches)"

    def run_shell(self, args):
        result = subprocess.run(str(args["command"]).strip(), cwd=self.agent.root, shell=True, capture_output=True, text=True, timeout=int(args.get("timeout", 20)), check=False)
        return "\n".join([f"exit_code: {result.returncode}", "stdout:", result.stdout.strip() or "(empty)", "stderr:", result.stderr.strip() or "(empty)"])

    def write_file(self, args):
        path, content = self.path(args["path"]), str(args["content"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.agent.invalidate_context([path.relative_to(self.agent.root)], "write_file")
        return f"wrote {path.relative_to(self.agent.root)} ({len(content)} chars)"

    def patch_file(self, args):
        path, old_text = self.path(args["path"]), str(args.get("old_text", ""))
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
        self.agent.invalidate_context([path.relative_to(self.agent.root)], "patch_file")
        return f"patched {path.relative_to(self.agent.root)}"

    def delegate(self, args):
        from .agent import MiniAgent

        task = str(args["task"]).strip()
        child = MiniAgent(
            model_client=self.agent.model_client, workspace=self.agent.workspace, session_store=self.agent.session_store,
            approval_policy="never", max_steps=int(args.get("max_steps", 3)), max_new_tokens=self.agent.max_new_tokens,
            depth=self.agent.depth + 1, max_depth=self.agent.max_depth, read_only=True,
        )
        child.session["memory"]["task"] = task
        child.session["memory"]["notes"] = [clip(self.agent.history_text(), 300)]
        return "delegate_result:\n" + child.ask(task)
