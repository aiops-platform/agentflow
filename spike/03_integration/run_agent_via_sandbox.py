"""Spike 3: 驱动 opencode agent 通过 MCP 工具调用 opensandbox 沙箱。

简化版: 只用同步 POST /message，从返回的 parts 里看工具调用 + 最终答案。
"""
import asyncio
import json

import httpx

BASE = "http://127.0.0.1:4090"
PROMPT = "请使用 run_python 工具执行 `import hashlib; print(hashlib.sha256(b'opencode-spike').hexdigest())`，然后把输出的哈希值原样告诉我。"


async def main() -> None:
    timeout = httpx.Timeout(240.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{BASE}/session", json={"title": "spike3"})
        sid = r.json()["id"]
        print(f"[1] session: {sid}", flush=True)

        print("[2] sending prompt (agent 会调用 run_python，含沙箱创建)...", flush=True)
        r = await client.post(
            f"{BASE}/session/{sid}/message",
            json={"parts": [{"type": "text", "text": PROMPT}]},
        )
        msg = r.json()

        # 打印所有 parts（含类型 + 内容摘要），看工具调用链路
        print("[3] message parts:", flush=True)
        for p in msg.get("parts", []):
            t = p.get("type")
            if t == "text":
                print(f"    [{t}] {p.get('text','')[:200]}", flush=True)
            elif t == "tool":
                print(f"    [{t}] name={p.get('tool')} input={json.dumps(p.get('state',{}).get('input',{}),ensure_ascii=False)[:200]}", flush=True)
            else:
                brief = json.dumps(p, ensure_ascii=False)[:200]
                print(f"    [{t}] {brief}", flush=True)

        texts = [p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text"]
        joined = " ".join(texts)
        print(f"[4] final text: {joined}", flush=True)

        # 找工具调用
        tools = [p for p in msg.get("parts", []) if p.get("type") == "tool"]
        print(f"[5] tool calls: {len(tools)}", flush=True)
        for p in tools:
            print(f"    tool={p.get('tool')} state.keys={list((p.get('state') or {}).keys())}", flush=True)

        ok = len(tools) > 0
        print("SPIKE 3 PASS ✅  agent 经 MCP 调用了 run_python 工具" if ok else "SPIKE 3 ? 未发现工具调用（可能模型直接作答）")


if __name__ == "__main__":
    asyncio.run(main())
