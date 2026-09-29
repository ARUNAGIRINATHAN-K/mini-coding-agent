"""Prompt construction and transcript reduction."""

import json

from .context import MAX_HISTORY, clip


class PromptComposer:
    def build_prefix(self, workspace, tools, repository_summary=None):
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
            "Rules:\n" + rules, "Tools:\n" + "\n".join(tool_lines),
            "Valid response examples:\n" + examples, repository_summary or workspace.text(),
        ])

    @staticmethod
    def memory_text(session):
        memory = session["memory"]
        notes = "\n".join(f"- {note}" for note in memory["notes"]) or "- none"
        return "\n".join([
            "Memory:", f"- task: {memory['task'] or '-'}", f"- files: {', '.join(memory['files']) or '-'}",
            "- notes:", notes,
        ])

    @staticmethod
    def history_text(session):
        history = session["history"]
        if not history:
            return "- empty"
        lines, seen_reads = [], set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] in ("write_file", "patch_file"):
                seen_reads.discard(str(item["args"].get("path", "")))
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], 900 if recent else 180))
            else:
                lines.append(f"[{item['role']}] {clip(item['content'], 900 if recent else 220)}")
        return clip("\n".join(lines), MAX_HISTORY)

    def prompt(self, prefix, session, user_message):
        return "\n\n".join([
            prefix, self.memory_text(session), "Transcript:\n" + self.history_text(session),
            "Current user request:\n" + user_message,
        ])
