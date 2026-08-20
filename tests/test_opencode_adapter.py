"""M1 opencode 适配器测试（独立脚本，无需真实 opencode serve）。

用 FakeAdapter 注入脚本化的 SSE 事件流 + 同步返回，验证 run_node 的事件映射、工具调用捕获、
token/cost、兜底与 session 清理。可 ``python tests/test_opencode_adapter.py`` 直接跑。
"""
from __future__ import annotations

import asyncio
from typing import Any

from agentflow.opencode import NodeEvent, NodeEventType, OpenCodeAdapter
from agentflow.opencode.server_adapter import _final_text


class FakeAdapter(OpenCodeAdapter):
    """注入脚本化 SSE 事件 + 同步返回，绕开真实 HTTP。"""

    def __init__(self, sse_events: list[dict], sync_msg: dict | None = None):
        super().__init__("http://fake")
        self._sse = sse_events
        self._sync = sync_msg or {
            "parts": [{"type": "text", "text": "42"}],
            "info": {"tokens": {"input": 100, "output": 50, "total": 150}},
        }
        self.deleted: str | None = None
        self.created_title: str | None = None
        self.sent_prompt: str | None = None

    async def create_session(self, title: str | None = None, agent: str | None = None) -> str:
        self.created_title = title
        self.created_agent = agent
        return "ses_test"

    async def send_message(self, session_id: str, prompt: str) -> dict[str, Any]:
        self.sent_prompt = prompt
        return self._sync

    async def delete_session(self, session_id: str) -> None:
        self.deleted = session_id

    async def _sse_events(self, session_id: str | None):
        for ev in self._sse:
            yield ev


def _sse(parts: list[dict]) -> list[dict]:
    """把 part 列表包成带 sessionID 的 message.part.updated 事件序列。"""
    return [
        {"type": "message.part.updated",
         "properties": {"sessionID": "ses_test", "part": p}}
        for p in parts
    ]


async def collect(adapter: OpenCodeAdapter, prompt: str = "hello") -> list[NodeEvent]:
    out: list[NodeEvent] = []
    async for ev in adapter.run_node("test-agent", prompt):
        out.append(ev)
    return out


def test_map_event():
    a = OpenCodeAdapter("http://fake")
    cases = [
        ({"type": "message.part.updated", "properties": {"part": {"type": "text", "text": "hi"}}},
         NodeEventType.TEXT, "hi"),
        ({"type": "message.part.updated", "properties": {"part": {"type": "reasoning", "text": "r"}}},
         NodeEventType.REASONING, "r"),
        ({"type": "message.part.updated",
          "properties": {"part": {"type": "tool", "tool": "run_python", "state": {"input": {"code": "x"}}}}},
         NodeEventType.TOOL_CALL, None),
        ({"type": "message.part.updated",
          "properties": {"part": {"type": "step-finish", "tokens": {"total": 9, "input": 4, "output": 5}, "cost": 0.0}}},
         NodeEventType.STEP_FINISH, None),
        ({"type": "session.idle", "properties": {"sessionID": "s"}}, NodeEventType.DONE, None),
        ({"type": "permission.asked", "properties": {"x": 1}}, NodeEventType.PERMISSION_ASKED, None),
    ]
    for ev, expect_type, expect_text in cases:
        mapped = a._map_event(ev, "s")
        assert mapped is not None and mapped.type is expect_type, f"{ev} -> {mapped}"
        if expect_text is not None:
            assert mapped.text == expect_text
    # 工具调用字段
    tool_ev = cases[2][0]
    mapped = a._map_event(tool_ev, "s")
    assert mapped.tool.name == "run_python" and mapped.tool.input == {"code": "x"}
    # step-finish token 映射
    sf = a._map_event(cases[3][0], "s")
    assert sf.tokens.total == 9 and sf.tokens.input == 4 and sf.tokens.output == 5
    # 无关事件 -> None
    assert a._map_event({"type": "session.updated", "properties": {}}, "s") is None
    print("  ✓ test_map_event")


async def test_run_node_full():
    sse = _sse([
        {"type": "text", "text": "42"},
        {"type": "tool", "tool": "run_python", "state": {"input": {"code": "import hashlib"}}},
        {"type": "step-finish", "tokens": {"input": 100, "output": 50, "total": 150}, "cost": 0.01},
    ]) + [{"type": "session.idle", "properties": {"sessionID": "ses_test"}}]

    a = FakeAdapter(sse)
    evs = await collect(a)

    types = [e.type for e in evs]
    assert types[0] is NodeEventType.SESSION_CREATED, types
    assert NodeEventType.TEXT in types
    assert NodeEventType.TOOL_CALL in types
    assert NodeEventType.STEP_FINISH in types
    assert types[-1] is NodeEventType.DONE, types

    # 工具调用从 SSE 捕获（同步返回无工具调用，正是 adapter 存在的意义）
    tool_ev = next(e for e in evs if e.type is NodeEventType.TOOL_CALL)
    assert tool_ev.tool.name == "run_python"
    assert tool_ev.tool.input == {"code": "import hashlib"}

    sf = next(e for e in evs if e.type is NodeEventType.STEP_FINISH)
    assert sf.tokens.total == 150 and sf.cost == 0.01

    assert a.created_title == "test-agent"
    assert a.created_agent == "test-agent"
    assert a.sent_prompt == "hello"
    assert a.deleted == "ses_test"
    print("  ✓ test_run_node_full")


async def test_run_node_fallback_from_sync():
    # SSE 只给 idle（无 text/step-finish）→ 应从同步返回兜底 text + token
    sse = [{"type": "session.idle", "properties": {"sessionID": "ses_test"}}]
    a = FakeAdapter(sse)
    evs = await collect(a)
    types = [e.type for e in evs]
    assert NodeEventType.TEXT in types and NodeEventType.STEP_FINISH in types
    text = next(e for e in evs if e.type is NodeEventType.TEXT)
    assert text.text == "42"
    sf = next(e for e in evs if e.type is NodeEventType.STEP_FINISH)
    assert sf.tokens.total == 150
    assert a.deleted == "ses_test"
    print("  ✓ test_run_node_fallback_from_sync")


async def test_run_node_sse_error():
    # SSE 流异常 → error 事件 + 兜底 + 清理
    class ErrAdapter(FakeAdapter):
        async def _sse_events(self, session_id):
            raise RuntimeError("connection reset")
            yield  # pragma: no cover

    a = ErrAdapter([], sync_msg={"parts": [], "info": {}})
    evs = await collect(a)
    types = [e.type for e in evs]
    assert NodeEventType.ERROR in types
    assert a.deleted == "ses_test"
    print("  ✓ test_run_node_sse_error")


def test_final_text():
    assert _final_text({"parts": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "a b"
    assert _final_text({"parts": [{"type": "tool"}]}) == ""
    print("  ✓ test_final_text")


async def main() -> None:
    test_map_event()
    await test_run_node_full()
    await test_run_node_fallback_from_sync()
    await test_run_node_sse_error()
    test_final_text()
    print("\nALL M1 ADAPTER TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
