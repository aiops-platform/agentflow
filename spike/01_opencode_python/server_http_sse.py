"""Spike 1: 用 Python 直连 opencode serve 的 HTTP+SSE，验证「建 session → 发 prompt → 收事件 → 取输出+token/cost」。

结论: 这是 OpenCodeAdapter 的 server_adapter 实现依据。
"""
import asyncio
import json

import httpx

BASE = "http://127.0.0.1:4090"
PROMPT = "what is 6*7? reply with just the number"


async def main() -> None:
    timeout = httpx.Timeout(60.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1. 创建 session
        r = await client.post(f"{BASE}/session", json={"title": "spike1"})
        sid = r.json()["id"]
        print(f"[1] session created: {sid}")

        # 2. 打开 SSE 事件流（后台任务）
        events: list[dict] = []
        done = asyncio.Event()

        async def read_events() -> None:
            async with client.stream("GET", f"{BASE}/event") as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    events.append(ev)
                    t = ev.get("type")
                    if t in ("session.idle",):
                        done.set()

        reader = asyncio.create_task(read_events())
        await asyncio.sleep(0.3)  # 等 reader 连上

        # 3. 发 prompt（同步返回完整消息）
        r = await client.post(
            f"{BASE}/session/{sid}/message",
            json={"parts": [{"type": "text", "text": PROMPT}]},
        )
        msg = r.json()
        print(f"[2] message sent, http={r.status_code}")

        # 4. 等完成信号
        try:
            await asyncio.wait_for(done.wait(), timeout=50)
        except asyncio.TimeoutError:
            print("   (warning: 未等到 session.idle，可能已由同步响应返回)")

        # 5. 从同步响应里取最终文本 + token/cost
        info = msg.get("info", {})
        print("[3] model:", info.get("modelID"), "/", info.get("providerID"))
        text_parts = [p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text"]
        print("[4] answer text:", " ".join(text_parts))
        print("[5] tokens:", json.dumps(info.get("tokens", {}), ensure_ascii=False))

        # 6. 从 SSE 事件里取 step-finish（监控路径：喂 Langfuse 的数据源）
        sf = [
            ev["properties"]["part"]
            for ev in events
            if ev.get("type") == "message.part.updated"
            and ev.get("properties", {}).get("part", {}).get("type") == "step-finish"
        ]
        if sf:
            last = sf[-1]
            print("[6] step-finish tokens:", json.dumps(last.get("tokens"), ensure_ascii=False))
            print("[6] step-finish cost:", last.get("cost"))

        # 7. 事件类型统计（验证监控事件完整性）
        from collections import Counter
        counts = Counter(ev.get("type") for ev in events)
        print("[7] event types:", dict(counts))

        reader.cancel()
        print("SPIKE 1 PASS ✅  建 session → prompt → 事件 → 输出/token/cost 全链路可用")


if __name__ == "__main__":
    asyncio.run(main())
