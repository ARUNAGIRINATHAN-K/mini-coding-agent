"""Workspace discovery and small formatting helpers."""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000
IGNORED_PATH_NAMES = {".git", ".mini-coding-agent", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}
BUILD_OUTPUT_NAMES = {"build", "dist", "node_modules", ".next", ".nuxt", "coverage", ".coverage", "htmlcov"}
CONTEXT_CACHE_VERSION = 1
DEFAULT_CONTEXT_CHAR_BUDGET = 6000


def now():
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


class WorkspaceContext:
    """The stable, lightweight repository facts placed in the agent prefix."""

    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs

    @classmethod
    def build(cls, cwd):
        cwd = Path(cwd).resolve()

        def git(args, fallback=""):
            try:
                result = subprocess.run(
                    ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=5
                )
                return result.stdout.strip() or fallback
            except Exception:
                return fallback

        repo_root = Path(git(["rev-parse", "--show-toplevel"], str(cwd))).resolve()
        docs = {}
        for base in (repo_root, cwd):
            for name in DOC_NAMES:
                path = base / name
                if not path.exists():
                    continue
                key = str(path.relative_to(repo_root))
                if key not in docs:
                    docs[key] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)
        return cls(
            cwd=str(cwd), repo_root=str(repo_root), branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main").removeprefix("origin/"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
        )

    def text(self):
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        return "\n".join([
            "Workspace:", f"- cwd: {self.cwd}", f"- repo_root: {self.repo_root}",
            f"- branch: {self.branch}", f"- default_branch: {self.default_branch}",
            "- status:", self.status, "- recent_commits:", commits, "- project_docs:", docs,
        ])


class RepositoryContextService:
    """Refreshable, workspace-contained repository metadata index.

    The cache deliberately stores metadata only: relative paths, sizes, mtimes,
    hashes, and git summaries. Source file contents never enter the cache or
    default prompt; guarded read tools remain the source of full contents.
    """

    def __init__(self, root, workspace=None, char_budget=DEFAULT_CONTEXT_CHAR_BUDGET):
        self.root = Path(root).resolve()
        self.workspace = workspace
        self.char_budget = char_budget
        self.cache_path = self.root / ".mini-coding-agent" / "cache" / "repository-context-v1.json"
        self.files = {}
        self.documents = {}
        self.git = {}
        self.generation = 0
        self.ignore_patterns = []
        self.last_budget = {}
        self._load_cache()
        self.refresh()

    def _load_cache(self):
        """Load only compatible metadata; a refresh always verifies it later."""
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if cached.get("version") != CONTEXT_CACHE_VERSION or cached.get("root") != str(self.root):
            return
        self.files = cached.get("files", {})
        self.documents = cached.get("documents", {})
        self.git = cached.get("git", {})
        self.generation = int(cached.get("generation", 0))

    def _persist(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CONTEXT_CACHE_VERSION,
            "root": str(self.root),
            "generated_at": now(),
            "generation": self.generation,
            "git": self.git,
            "files": self.files,
            "documents": {
                path: {key: value for key, value in metadata.items() if key != "summary"}
                for path, metadata in self.documents.items()
            },
        }
        self.cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _git(self, args, fallback=""):
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, text=True, check=True, timeout=5
            )
            return result.stdout.strip() or fallback
        except Exception:
            return fallback

    def _git_snapshot(self):
        default_branch = self._git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main")
        unstaged_diff = self._git(["diff", "--stat"])
        staged_diff = self._git(["diff", "--cached", "--stat"])
        diff_summary = "\n".join(
            part for part in (
                f"unstaged:\n{unstaged_diff}" if unstaged_diff else "",
                f"staged:\n{staged_diff}" if staged_diff else "",
            ) if part
        ) or "(no diff)"
        return {
            "repo_root": str(self.root),
            "branch": self._git(["branch", "--show-current"], "-") or "-",
            "default_branch": default_branch.removeprefix("origin/"),
            "status": clip(self._git(["status", "--short"], "clean") or "clean", 1500),
            "recent_commits": [line for line in self._git(["log", "--oneline", "-5"]).splitlines() if line],
            "diff_summary": clip(diff_summary, 1500),
        }

    def _configured_patterns(self):
        patterns = []
        for name in (".gitignore", ".contextignore"):
            path = self.root / name
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                pattern = line.strip()
                if pattern and not pattern.startswith("#"):
                    patterns.append(pattern)
        return patterns

    @staticmethod
    def _pattern_matches(relative, pattern, is_dir):
        negated = pattern.startswith("!")
        pattern = pattern[1:] if negated else pattern
        if not pattern:
            return False
        directory_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if directory_only and not is_dir:
            return False
        relative = relative.as_posix()
        anchored = pattern.startswith("/")
        pattern = pattern.lstrip("/")
        if not pattern:
            return False
        if anchored or "/" in pattern:
            return Path(relative).match(pattern)
        return any(Path(part).match(pattern) for part in relative.split("/"))

    def _ignored(self, relative, is_dir):
        if any(part in IGNORED_PATH_NAMES or part in BUILD_OUTPUT_NAMES for part in relative.parts):
            return True
        ignored = False
        for pattern in self.ignore_patterns:
            if self._pattern_matches(relative, pattern, is_dir):
                ignored = not pattern.startswith("!")
        return ignored

    def _inside_root(self, path):
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def _iter_files(self):
        for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
            directory_path = Path(directory)
            for name in list(dirnames):
                path = directory_path / name
                relative = path.relative_to(self.root)
                if path.is_symlink() or self._ignored(relative, is_dir=True) or not self._inside_root(path):
                    dirnames.remove(name)
            for name in filenames:
                path = directory_path / name
                relative = path.relative_to(self.root)
                if path.is_symlink() or self._ignored(relative, is_dir=False) or not self._inside_root(path):
                    continue
                yield path

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()

    def _file_metadata(self, path):
        relative = path.relative_to(self.root).as_posix()
        stat = path.stat()
        return {
            "path": relative,
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "sha256": self._sha256(path),
        }

    def _document_metadata(self, path, metadata):
        if path.name not in DOC_NAMES and path.name != ".contextignore":
            return None
        summary_lines = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                summary_lines.append(line)
            if len(" ".join(summary_lines)) >= 400:
                break
        return {**metadata, "summary": clip(" ".join(summary_lines), 400)}

    def _index_path(self, path):
        relative = path.relative_to(self.root).as_posix()
        if path.is_symlink() or not path.is_file() or not self._inside_root(path) or self._ignored(Path(relative), is_dir=False):
            self.files.pop(relative, None)
            self.documents.pop(relative, None)
            return
        metadata = self._file_metadata(path)
        self.files[relative] = metadata
        document = self._document_metadata(path, metadata)
        if document:
            self.documents[relative] = document
        else:
            self.documents.pop(relative, None)

    def refresh(self, paths=None):
        """Refresh all metadata or only the affected paths after a write."""
        self.ignore_patterns = self._configured_patterns()
        if paths is None:
            self.files, self.documents = {}, {}
            for path in self._iter_files():
                self._index_path(path)
        else:
            if isinstance(paths, (str, Path)):
                paths = [paths]
            normalized = []
            for raw_path in paths:
                path = Path(raw_path)
                path = path if path.is_absolute() else self.root / path
                if self._inside_root(path):
                    normalized.append(path)
            if any(path.name in {".gitignore", ".contextignore"} for path in normalized):
                return self.refresh()
            for path in normalized:
                relative = path.relative_to(self.root).as_posix()
                if path.is_dir():
                    for nested in list(self.files):
                        if nested == relative or nested.startswith(relative + "/"):
                            self.files.pop(nested, None)
                            self.documents.pop(nested, None)
                    if path.exists() and not self._ignored(Path(relative), is_dir=True):
                        for nested in self._iter_files():
                            if nested == path or path in nested.parents:
                                self._index_path(nested)
                else:
                    self._index_path(path)
        self.git = self._git_snapshot()
        self.generation += 1
        self._persist()
        return self.snapshot()

    def invalidate(self, paths, reason="changed"):
        if isinstance(paths, (str, Path)):
            paths = [paths]
        paths = [str(path) for path in paths]
        snapshot = self.refresh(paths)
        snapshot["invalidation"] = {"paths": paths, "reason": reason}
        return snapshot

    def context_id(self):
        digest = hashlib.sha256()
        digest.update(str(self.generation).encode("utf-8"))
        for path, metadata in sorted(self.files.items()):
            digest.update(path.encode("utf-8"))
            digest.update(metadata["sha256"].encode("ascii"))
        return digest.hexdigest()[:16]

    def prompt_summary(self, char_budget=None):
        budget = char_budget or self.char_budget
        git = self.git
        lines = [
            "Workspace:",
            f"- cwd: {self.workspace.cwd if self.workspace else self.root}",
            f"- repo_root: {git.get('repo_root', self.root)}",
            f"- branch: {git.get('branch', '-')}",
            f"- default_branch: {git.get('default_branch', 'origin/main')}",
            "- status:", git.get("status", "clean"),
            "- recent_commits:",
            "\n".join(f"- {line}" for line in git.get("recent_commits", [])) or "- none",
            "- diff_summary:", git.get("diff_summary", "(no diff)"),
            "- project_documents:",
        ]
        for path in sorted(self.documents):
            document = self.documents[path]
            lines.append(f"- {document['path']} ({document['size']} bytes): {document['summary']}")
        if not self.documents:
            lines.append("- none")
        lines.append("- file_tree:")
        base = "\n".join(lines)
        included_files = 0
        truncation_reasons = []
        for path in sorted(self.files):
            metadata = self.files[path]
            line = f"- [F] {metadata['path']} ({metadata['size']} bytes)"
            if len(base) + len(line) + 1 > budget:
                truncation_reasons.append("file tree truncated by context character budget")
                break
            lines.append(line)
            base += "\n" + line
            included_files += 1
        if not self.files:
            lines.append("- none")
        if included_files < len(self.files) and not truncation_reasons:
            truncation_reasons.append("file tree truncated")
        summary = "\n".join(lines)
        if len(summary) > budget:
            summary = clip(summary, budget)
            truncation_reasons.append("repository metadata exceeded context character budget")
        self.last_budget = {
            "character_budget": budget,
            "estimated_characters": len(summary),
            "estimated_tokens": (len(summary) + 3) // 4,
            "included_file_items": included_files,
            "total_file_items": len(self.files),
            "included_document_items": len(self.documents),
            "truncation_reasons": truncation_reasons,
        }
        return summary

    def snapshot(self):
        self.prompt_summary()
        return {
            "version": CONTEXT_CACHE_VERSION,
            "context_id": self.context_id(),
            "generation": self.generation,
            "root": str(self.root),
            "git": self.git.copy(),
            "ignore_patterns": list(self.ignore_patterns),
            "files": [self.files[path].copy() for path in sorted(self.files)],
            "documents": [self.documents[path].copy() for path in sorted(self.documents)],
            "budget": self.last_budget.copy(),
        }
