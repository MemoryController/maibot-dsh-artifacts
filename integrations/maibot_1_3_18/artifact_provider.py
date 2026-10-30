"""Dedicated Host adapter. This module is installed into src/agent explicitly."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from src.core.tooling import (
    ToolAvailabilityContext,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)

from .artifact_handoff import HandoffBroker, HandoffConfig, HandoffError

TOOL_NAME = "agent_artifact_handoff"


class HostFiles:
    """Use Host APIs with a broker-bound real chat, never plugin scope or admin."""

    def __init__(self) -> None:
        self._create_lock = asyncio.Lock()

    async def require_chat(self, chat_id: str) -> None:
        from src.files.service import get_file_service

        service = get_file_service()
        service.require_enabled()
        service.require_session(chat_id)

    async def create(self, chat_id: str, name: str, source: Path) -> dict[str, Any]:
        from src.files.service import disk_call, get_file_service

        service = get_file_service()
        # FileService.create takes bytes. Limit memory to one bounded snapshot at a
        # time per Agent, and reject a stricter live Host limit before allocating.
        async with self._create_lock:
            service.require_enabled()
            service.require_session(chat_id)
            size = await disk_call(lambda: source.stat().st_size)
            if size > service.store.config.max_file_mib * 1024**2:
                raise ValueError("File exceeds live Host size limit")
            content = await disk_call(source.read_bytes)
            asset = await service.create(chat_id, name, content)
            return service.store.public(asset)

    async def send(self, file_ref: str, chat_id: str) -> dict[str, Any]:
        from src.files.service import get_file_service

        return await get_file_service().send(file_ref, chat_id)


def create_broker(service: Any, config: Any) -> HandoffBroker | None:
    if not config.artifact_handoff_enabled:
        return None
    return HandoffBroker(
        service.root / "artifact-handoff",
        service.agent_id,
        HandoffConfig(
            workspace=config.artifact_workspace,
            max_file_bytes=config.artifact_max_file_bytes,
            max_batch_files=config.artifact_max_batch_files,
            ttl_seconds=config.artifact_ttl_seconds,
            allow_send=config.artifact_allow_send,
        ),
        HostFiles(),
    )


async def task_prompt(agent: Any, record: dict[str, Any]) -> str:
    """Only queue records marked when issued by Host can receive a grant."""
    if agent.artifacts is None or not record.get("artifact_task_id"):
        return ""
    try:
        grant = await agent.artifacts.grant(record["artifact_task_id"])
    except HandoffError:
        return "[Host 文件交接授权不可用或已过期；请原聊天创建新任务，勿猜测凭证。]"
    return (
        "[Host 文件交接授权：仅用于本条任务；不要回显 token。完成后直接调用 "
        + TOOL_NAME
        + "，无需 agent_talk/主聊天 Planner 再次选工具。只接受 outbox_prefix 下已写完的文件；"
        "一个任务只接受一个批次，使用固定 request_id，重复请求只查询原结果；unknown 禁止重做。]\n"
        + json.dumps(grant, ensure_ascii=False)
    )


class ArtifactToolProvider:
    provider_name = "agent_artifacts"
    provider_type = "builtin"

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def list_tools(self, context: ToolAvailabilityContext | None = None) -> list[ToolSpec]:
        if self.agent.artifacts is None or context is None or context.session_id != self.agent.service.agent_id:
            return []
        return [
            ToolSpec(
                name=TOOL_NAME,
                provider_name=self.provider_name,
                provider_type=self.provider_type,
                description=(
                    "用本任务的 Host 文件交接凭证将 outbox 文件直接登记到发起任务的原聊天。"
                    "不接受聊天ID。相同request_id和参数重复调用只返回原结果，unknown不得换ID重试。"
                ),
                parameters_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "task_id": {"type": "string"},
                        "token": {"type": "string"},
                        "request_id": {
                            "type": "string",
                            "description": "本任务固定幂等标识，1到128位字母数字下划线横线",
                        },
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "send": {"type": "boolean", "default": False},
                    },
                    "required": ["task_id", "token", "request_id", "paths"],
                },
                metadata={"visibility": "visible"},
            )
        ]

    async def invoke(
        self, invocation: ToolInvocation, context: ToolExecutionContext | None = None
    ) -> ToolExecutionResult:
        try:
            if invocation.tool_name != TOOL_NAME or self.agent.artifacts is None or context is None:
                raise PermissionError("Artifact tool unavailable")
            result = await self.agent.artifacts.handoff(invocation.arguments, context.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not expose tokens, SQL parameters, signed URLs or Host paths.
            result = {"success": False, "error": {"code": "handoff_unavailable"}}
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=bool(result.get("success")),
            content=json.dumps(result, ensure_ascii=False),
            structured_content=result,
        )

    async def close(self) -> None:
        pass
