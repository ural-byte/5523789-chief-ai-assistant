from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(ok|needs_clarification|error)$")
    data: dict = Field(default_factory=dict)
    user_message: str = ""
    buttons: list = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    presentation: Literal["model", "canonical", "grounded"] = "model"


@dataclass(frozen=True)
class ToolContext:
    owner_id: int
    chat_id: int
    operation_id: UUID
    reference_at: datetime
    timezone: str
    idempotency_key: str
    source_update: dict
    lease: tuple | None = None


class ToolHandler(Protocol):
    arguments: type[BaseModel]
    description: str

    async def prepare(self, ctx: ToolContext, arguments: BaseModel) -> Any: ...

    def apply(
        self, session, ctx: ToolContext, arguments: BaseModel, prepared: Any
    ) -> ToolResult: ...


class Registry:
    allowed_names = {
        "create_task",
        "prepare_meeting",
        "save_memory",
        "search_memory",
        "search_document",
        "prepare_data_deletion",
        "prepare_memory_resolution",
    }

    def __init__(self):
        self.tools: dict[str, ToolHandler] = {}
        self.jobs: dict[str, Any] = {}
        self.callback = None
        self.upload = None

    def register(self, name: str, handler: ToolHandler):
        if name not in self.allowed_names or name in self.tools:
            raise ValueError("unsupported or duplicate tool")
        self.tools[name] = handler

    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.arguments.model_json_schema(),
                },
            }
            for name, tool in self.tools.items()
        ]


registry = Registry()
