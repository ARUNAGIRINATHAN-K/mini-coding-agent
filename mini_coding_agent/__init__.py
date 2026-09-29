"""Reusable core and compatibility exports for Mini Coding Agent."""

from .agent import MiniAgent
from .cli import build_agent, build_arg_parser, build_welcome, main
from .context import RepositoryContextService, WorkspaceContext
from .events import AgentEvent, EventEmitter
from .model import FakeModelClient, OllamaModelClient
from .prompting import PrefixCacheStore, PromptComposer, PromptParts
from .sessions import SessionStore

__all__ = [
    "AgentEvent",
    "EventEmitter",
    "FakeModelClient",
    "MiniAgent",
    "OllamaModelClient",
    "PrefixCacheStore",
    "PromptComposer",
    "PromptParts",
    "RepositoryContextService",
    "SessionStore",
    "WorkspaceContext",
    "build_agent",
    "build_arg_parser",
    "build_welcome",
    "main",
]
