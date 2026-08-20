"""Spike 2: opensandbox 本地(podman)跑通 — 创建沙箱 → 跑 Python → 取输出 → 销毁。

前置: opensandbox-server 已跑在 127.0.0.1:8080 (走 podman 的 /var/run/docker.sock)
"""
import asyncio

from code_interpreter import CodeInterpreter, SupportedLanguage
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

SANDBOX_DOMAIN = "127.0.0.1:8080"
IMAGE = "opensandbox/code-interpreter:v1.0.2"


async def main() -> None:
    config = ConnectionConfig(domain=SANDBOX_DOMAIN)
    print(f"[1] 创建沙箱: {IMAGE} @ {SANDBOX_DOMAIN}", flush=True)
    sandbox = await Sandbox.create(
        IMAGE,
        connection_config=config,
        entrypoint=["/opt/opensandbox/code-interpreter.sh"],
        env={"PYTHON_VERSION": "3.11"},
    )
    print("[2] 沙箱就绪")

    async with sandbox:
        interpreter = await CodeInterpreter.create(sandbox=sandbox)
        code = (
            "import sys\n"
            "print('python', sys.version.split()[0])\n"
            "result = 6 * 7\n"
            "result\n"
        )
        print(f"[3] 运行代码:\n{code}")
        r = await interpreter.codes.run(code, language=SupportedLanguage.PYTHON)

        print("[4] exit_code:", r.exit_code)
        if r.logs and r.logs.stdout:
            print("[4] stdout:", [o.text for o in r.logs.stdout])
        if r.result:
            print("[5] result:", [o.text for o in r.result])
        if r.error:
            print("[5] error:", r.error)

        assert r.result and r.result[0].text.strip() == "42", "结果不符合预期 (期望 42)"
        print("SPIKE 2 PASS ✅  沙箱执行 + 结果读取正常")

    print("[6] 已退出 async with (沙箱生命周期结束)")


if __name__ == "__main__":
    asyncio.run(main())
