"""opencode HTTP+SSE 直连适配器（主实现，spike 1/3/4 已验证 ✅）。

opencode serve 的 HTTP API：:

  POST   /session               建会话（返回 {"id": ...}）
  POST   /session/:id/message   发 prompt（body {"parts":[{"type":"text","text":...}]}）
  GET    /event                 SSE 事件流（全局，需按 properties.sessionID 过滤）
  POST   /session/:id/abort     中止（节点超时用）
  DELETE /session/:id           删除会话

⚠️ 关键发现（spike 实测，DESIGN.md §4.4）：同步 ``POST /message`` 返回里**没有工具调用 part**
（只有 step-start / reasoning / text / step-finish），要观测工具调用必须消费 ``GET /event`` 的
SSE 流（``message.part.updated``）。故 ``run_node`` 先挂 SSE 监听再发 prompt。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agentflow.config import settings
from agentflow.opencode.events import NodeEvent, NodeEventType, TokenUsage, ToolCall


class OpenCodeAdapter:
    """opencode 单节点运行时（每个节点 = 一个 session）。"""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 300.0):
        self.base_url = (base_url or settings.opencode_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=5.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── HTTP 基础操作 ──

    async def create_session(self, title: str | None = None, agent: str | None = None) -> str:
        body: dict[str, Any] = {"title": title or "agentflow"}
        if agent:
            body["agent"] = agent   # 指定 opencode subagent（system prompt + permission 生效）
        r = await self._client.post(f"{self.base_url}/session", json=body)
        r.raise_for_status()
        return r.json()["id"]

    async def send_message(self, session_id: str, prompt: str) -> dict[str, Any]:
        r = await self._client.post(
            f"{self.base_url}/session/{session_id}/message",
            json={"parts": [{"type": "text", "text": prompt}]},
        )
        r.raise_for_status()
        return r.json()

    async def delete_session(self, session_id: str) -> None:
        try:
            await self._client.delete(f"{self.base_url}/session/{session_id}")
        except httpx.HTTPError:
            pass  # 删除失败不致命

    async def abort(self, session_id: str) -> None:
        try:
            await self._client.post(f"{self.base_url}/session/{session_id}/abort")
        except httpx.HTTPError:
            pass

    # ── SSE ──

    async def _sse_events(self, session_id: str | None) -> AsyncIterator[dict[str, Any]]:
        """原始 SSE 事件流；按 ``properties.sessionID`` 过滤（事件未带则不过滤）。"""
        async with self._client.stream("GET", f"{self.base_url}/event") as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                sid = self._event_session_id(ev)
                if session_id and sid and sid != session_id:
                    continue
                yield ev

    @staticmethod
    def _event_session_id(ev: dict[str, Any]) -> str | None:
        props = ev.get("properties") or {}
        return props.get("sessionID") or props.get("session_id")

    # ── 事件映射（opencode 原始事件 → NodeEvent）──

    def _map_event(self, ev: dict[str, Any], session_id: str | None) -> NodeEvent | None:
        t = ev.get("type")
        props = ev.get("properties") or {}
        common: dict[str, Any] = {"session_id": session_id, "raw": ev}

        if t == "message.part.updated":
            part = props.get("part") or {}
            pt = part.get("type")
            if pt == "text":
                return NodeEvent(type=NodeEventType.TEXT, text=part.get("text"), **common)
            if pt == "reasoning":
                return NodeEvent(type=NodeEventType.REASONING, text=part.get("text"), **common)
            if pt == "tool":
                state = part.get("state") or {}
                tool = ToolCall(
                    name=part.get("tool") or "",
                    input=state.get("input") or {},
                    output=state.get("output"),
                )
                return NodeEvent(type=NodeEventType.TOOL_CALL, tool=tool, **common)
            if pt == "step-finish":
                return NodeEvent(
                    type=NodeEventType.STEP_FINISH,
                    tokens=TokenUsage.from_dict(part.get("tokens")),
                    cost=part.get("cost"),
                    **common,
                )
        elif t == "session.idle":
            return NodeEvent(type=NodeEventType.DONE, **common)
        elif t == "permission.asked":
            return NodeEvent(type=NodeEventType.PERMISSION_ASKED, permission=props, **common)
        return None

    # ── 主入口 ──

    async def _drain(self, session_id: str | None, queue: asyncio.Queue) -> None:
        """把 SSE 事件推入队列；流结束/异常时推哨兵（("end", None) / ("error", msg)）。"""
        try:
            async for ev in self._sse_events(session_id):
                await queue.put(("event", ev))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 流异常转成 error 哨兵，不让后台任务悄悄死掉
            await queue.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            await queue.put(("end", None))

    async def run_node(
        self,
        agent: str,
        prompt: str,
        tools: list[str] | None = None,
        idle_timeout: float = 120.0,
    ) -> AsyncIterator[NodeEvent]:
        """运行单个节点：建 session（指定 subagent）→ 挂 SSE → 发 prompt → 消费事件 → 删 session。

        ``agent`` 对应 opencode 的 subagent 名（``opencode-setup`` 已生成）；建 session 时传给
        ``POST /session`` 的 ``agent`` 字段，使该节点以对应 subagent 的 system prompt + permission 运行。
        ``tools`` 参数当前仅记录（MCP 工具经 ``opencode mcp add`` 全局挂载）。
        """
        session_id = await self.create_session(title=agent, agent=agent)
        yield NodeEvent(type=NodeEventType.SESSION_CREATED, session_id=session_id)

        queue: asyncio.Queue = asyncio.Queue()
        reader = asyncio.create_task(self._drain(session_id, queue))
        saw_text = False
        saw_step_finish = False
        sync_msg: dict[str, Any] = {}

        try:
            await asyncio.sleep(0.3)  # 等 SSE reader 连上（对齐 spike 1）
            try:
                sync_msg = await self.send_message(session_id, prompt)
            except httpx.HTTPError as e:
                yield NodeEvent(type=NodeEventType.ERROR, session_id=session_id, error=str(e))
                return

            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    yield NodeEvent(
                        type=NodeEventType.ERROR, session_id=session_id,
                        error=f"等待 session.idle 超时（>{idle_timeout}s）",
                    )
                    break

                if kind == "event":
                    mapped = self._map_event(payload, session_id)
                    if mapped is None:
                        continue
                    if mapped.type is NodeEventType.TEXT:
                        # 跳过用户 prompt 的回显（SSE 会把用户消息 part 也当 text 事件发出来）
                        if mapped.text and mapped.text.strip() == prompt.strip():
                            continue
                        saw_text = True
                    elif mapped.type is NodeEventType.STEP_FINISH:
                        saw_step_finish = True
                    yield mapped
                    if mapped.type is NodeEventType.DONE:
                        break
                elif kind == "error":
                    yield NodeEvent(type=NodeEventType.ERROR, session_id=session_id, error=payload)
                    break
                else:  # "end"：流结束但未收到 session.idle
                    break

            # 兜底：SSE 缺文本/step-finish 时，从同步返回补（同步返回无工具调用，但含最终文本+token）
            if not saw_text:
                text = _final_text(sync_msg)
                if text:
                    yield NodeEvent(type=NodeEventType.TEXT, session_id=session_id, text=text)
            if not saw_step_finish:
                info = sync_msg.get("info") or {}
                tokens = TokenUsage.from_dict(info.get("tokens"))
                if tokens.total:
                    yield NodeEvent(type=NodeEventType.STEP_FINISH, session_id=session_id,
                                    tokens=tokens, cost=info.get("cost"))
        finally:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
            await self.delete_session(session_id)


def _final_text(msg: dict[str, Any]) -> str:
    return " ".join(
        p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text"
    ).strip()
