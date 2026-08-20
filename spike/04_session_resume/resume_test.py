"""Spike 4: opencode session 能否在 server 崩溃重启后恢复上下文？

架构问题（DESIGN.md 4.11.2）：断点续跑时，crash 后是「重开新 session」还是「恢复原 session」？
本 spike 验证后者是否可行。

方法：
  1. 起 server A → 建 session → turn1 让模型记住一个独特口令
  2. SIGKILL server（模拟崩溃）
  3. 起 server B → 同一 session 上 turn2 问「口令是什么」
  4. 若回答里含口令 → 上下文跨重启保留；否则丢失
"""
import asyncio
import time

import httpx

PORT = 4091
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "ZEPHYR-7421"


def start_server():
    import subprocess
    p = subprocess.Popen(
        ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等就绪
    for _ in range(40):
        try:
            httpx.get(BASE + "/config", timeout=1)
            return p
        except Exception:
            time.sleep(0.5)
    return p


def final_text(msg: dict) -> str:
    return " ".join(p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text")


async def main() -> None:
    # 1. server A
    srv_a = start_server()
    print(f"[1] server A 已启动 (port {PORT})", flush=True)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
        # 2. 建 session + turn1
        r = await client.post(BASE + "/session", json={"title": "resume-test"})
        sid = r.json()["id"]
        print(f"[2] session: {sid}", flush=True)

        r = await client.post(
            BASE + f"/session/{sid}/message",
            json={"parts": [{"type": "text", "text": f"记住这个口令：{SECRET}。只回复'已记住'。"}]},
        )
        print(f"[3] turn1 回复: {final_text(r.json())}", flush=True)
        await asyncio.sleep(1)  # 等消息落盘

    # 3. SIGKILL server A（模拟崩溃）
    srv_a.kill()
    srv_a.wait()
    print("[4] server A 已 SIGKILL（模拟崩溃）", flush=True)
    time.sleep(2)

    # 4. server B（重启）
    srv_b = start_server()
    print("[5] server B 已重启", flush=True)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
        # 5. 会话还在吗？
        r = await client.get(BASE + "/session")
        sessions = r.json() if isinstance(r.json(), list) else []
        listed = any(s.get("id") == sid for s in sessions)
        print(f"[6] 重启后 session 仍在列表: {listed}", flush=True)

        # 6. 同一 session 继续问
        r = await client.post(
            BASE + f"/session/{sid}/message",
            json={"parts": [{"type": "text", "text": "我刚才让你记住的口令是什么？"}]},
        )
        t2 = final_text(r.json())
        print(f"[7] turn2 回复: {t2}", flush=True)

        retained = SECRET in t2
        print(f"[8] 上下文跨重启保留: {retained}", flush=True)

    srv_b.kill()
    srv_b.wait()

    verdict = "session resume 可行 ✅" if retained else "session resume 不可行 ❌（断点续跑应重开新 session）"
    print(f"SPIKE 4: {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
