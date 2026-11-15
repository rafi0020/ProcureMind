"""Tool registry.

A tool is an async function with a typed argument model. The registry turns that model into the JSON schema
the LLM sees, so the description the model reads and the validation the runtime applies cannot drift apart.

    @registry.tool("run_sql", "Run one read-only query.", level="read")
    async def run_sql(ctx: ToolContext, args: RunSqlArgs) -> ToolResult: ...

Levels:
    read            no effect outside the run; safe to run again after a crash
    write           changes something; never repeated automatically unless declared idempotent
    needs_approval  parked until an administrator approves, then run once
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.types import ToolSpec

Level = Literal["read", "write", "needs_approval"]
Signal = Literal["continue", "finish", "wait_user"]
CommitHook = Callable[[AsyncSession], Awaitable[None]]


class ToolError(Exception):
    """A failure the model should be told about so it can correct itself. The message is shown to the model."""


@dataclass
class ToolContext:
    run_id: UUID
    org_id: UUID
    user_id: UUID | None
    role: str
    step_idx: int


@dataclass
class ToolResult:
    """What a tool hands back.

    `summary` is always shown to the model. `data` is shown too unless it is large, in which case the model
    gets the summary and the `handle` it can pass to other tools. `commit` holds database writes that must
    happen together with recording the step: they are applied in the same transaction, so a crash can never
    leave the effect without the record or the record without the effect.
    """

    summary: str
    data: dict[str, Any] | None = None
    handle: str | None = None
    signal: Signal = "continue"
    commit: CommitHook | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[ToolContext, Any], Awaitable[ToolResult]]
    level: Level
    timeout_s: float
    roles: tuple[str, ...]
    idempotent: bool

    @property
    def spec(self) -> ToolSpec:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(self.name, self.description, schema)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def tool(
        self,
        name: str,
        description: str,
        *,
        level: Level = "read",
        timeout_s: float = 30,
        roles: tuple[str, ...] = ("clerk", "approver", "admin"),
        idempotent: bool | None = None,
    ):
        def register(fn):
            hints = [v for k, v in fn.__annotations__.items() if k not in ("ctx", "return")]
            if len(hints) != 1 or not (isinstance(hints[0], type) and issubclass(hints[0], BaseModel)):
                raise TypeError(f"tool {name} must take (ctx, args) where args is a pydantic model")
            self._tools[name] = Tool(
                name, description, hints[0], fn, level, timeout_s, roles,
                idempotent if idempotent is not None else level == "read",
            )  # fmt: skip
            return fn

        return register

    def get(self, name: str, role: str) -> Tool | None:
        """The tool, if it exists and this role may use it."""
        tool = self._tools.get(name)
        return tool if tool and role in tool.roles else None

    def specs(self, role: str) -> list[ToolSpec]:
        """Only tools the role may use are ever described to the model."""
        return [t.spec for t in self._tools.values() if role in t.roles]


@dataclass(frozen=True)
class AgentSpec:
    """Everything that defines an agent: the instructions and the tools. The runtime is generic over this."""

    system_prompt: str
    registry: ToolRegistry
    # Optional per-run facts placed in front of the question (never in the system prompt, which must stay
    # identical across runs so providers can cache it). Receives the organisation id.
    context: Callable[[UUID], Awaitable[str]] | None = None
