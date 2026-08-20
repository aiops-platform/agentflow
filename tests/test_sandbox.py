"""M3 opensandbox 适配器测试（用 fake interpreter，免真实 opensandbox server）。"""
from __future__ import annotations

import asyncio

from agentflow.sandbox import OpenSandboxAdapter, SandboxResult
from agentflow.sandbox.opensandbox import OpenSandboxAdapter as Adapter


# ── fake opensandbox 对象 ──

class _Text:
    def __init__(self, text): self.text = text


class _Logs:
    def __init__(self, stdout): self.stdout = stdout


class FakeExecution:
    def __init__(self, stdout="", result="", error=None, exit_code=0):
        self.logs = _Logs([_Text(stdout)])
        self.result = [_Text(result)] if result else []
        self.error = error
        self.exit_code = exit_code


class _Codes:
    def __init__(self, parent): self.parent = parent

    async def run(self, code, language=None):
        self.parent.last_code = code
        self.parent.last_lang = language
        return self.parent.execution


class _Files:
    def __init__(self, parent): self.parent = parent

    async def read_file(self, path): return "file-content"

    async def write_file(self, path, content): self.parent.last_write = (path, content)


class FakeInterpreter:
    def __init__(self):
        self.execution = FakeExecution(stdout="hello\n", result="42")
        self.codes = _Codes(self)
        self.files = _Files(self)
        self.last_code = None
        self.last_lang = None
        self.last_write = None


def _adapter() -> Adapter:
    a = Adapter(domain="127.0.0.1:8080", image="img", entrypoint="entry")
    a._interpreter = FakeInterpreter()  # 绕过 create()
    return a


# ── 测试 ──

def test_to_result():
    r = Adapter._to_result(FakeExecution(stdout="out", result="res", error="boom", exit_code=1))
    assert r.stdout == "out" and r.result == "res" and r.error == "boom" and r.exit_code == 1
    assert not r.ok
    ok = Adapter._to_result(FakeExecution(stdout="o", result="r", error=None, exit_code=0))
    assert ok.ok
    print("  ✓ test_to_result")


async def test_adapter_delegation():
    a = _adapter()
    r = await a.run_code("print(1)", "python")
    assert r.stdout == "hello\n" and r.result == "42"
    assert a._interpreter.last_code == "print(1)" and a._interpreter.last_lang == "python"

    # run_shell 走 bash 语言
    await a.run_shell("ls")
    assert a._interpreter.last_lang == "bash"

    assert await a.read_file("/tmp/x") == "file-content"
    await a.write_file("/tmp/y", "data")
    assert a._interpreter.last_write == ("/tmp/y", "data")
    print("  ✓ test_adapter_delegation")


async def test_mcp_server_tools():
    import agentflow.sandbox.mcp_server as ms

    adapter = _adapter()
    ms._get_adapter = lambda: asyncio.sleep(0, result=adapter)  # type: ignore[assignment]

    out = await ms.run_python("1+1")
    assert "stdout:" in out and "hello" in out and "42" in out
    assert await ms.read_file("/x") == "file-content"
    assert await ms.write_file("/x", "c") == "written: /x"
    print("  ✓ test_mcp_server_tools")


async def main() -> None:
    test_to_result()
    await test_adapter_delegation()
    await test_mcp_server_tools()
    print("\nALL M3 SANDBOX TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
