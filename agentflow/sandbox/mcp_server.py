"""opensandbox MCP server：run_python / run_shell / read_file / write_file（spike 3 验证 ✅）。

以 stdio 运行，由 opencode 经 ``opencode mcp add`` 拉起；tester / fix-implementer 挂载，
代码执行发生在沙箱内（非宿主机 shell）。
"""
from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from agentflow.sandbox.opensandbox import OpenSandboxAdapter

mcp = FastMCP("opensandbox")

_adapter: OpenSandboxAdapter | None = None
_lock = asyncio.Lock()


async def _get_adapter() -> OpenSandboxAdapter:
    global _adapter
    async with _lock:
        if _adapter is None:
            _adapter = await OpenSandboxAdapter().create()
    return _adapter


def _fmt(r) -> str:
    out = f"stdout:\n{r.stdout or '(none)'}\nresult:\n{r.result or '(none)'}"
    if r.error:
        out += f"\nerror:\n{r.error}"
    return out


@mcp.tool()
async def run_python(code: str) -> str:
    """在隔离沙箱中执行 Python 代码，返回 stdout 与最终表达式结果。"""
    adapter = await _get_adapter()
    return _fmt(await adapter.run_code(code, "python"))


@mcp.tool()
async def run_shell(command: str) -> str:
    """在隔离沙箱中执行 shell 命令，返回 stdout。"""
    adapter = await _get_adapter()
    return _fmt(await adapter.run_shell(command))


@mcp.tool()
async def read_file(path: str) -> str:
    """读取沙箱内文件内容。"""
    adapter = await _get_adapter()
    return await adapter.read_file(path)


@mcp.tool()
async def write_file(path: str, content: str) -> str:
    """写文件到沙箱（覆盖）。"""
    adapter = await _get_adapter()
    await adapter.write_file(path, content)
    return f"written: {path}"


if __name__ == "__main__":
    mcp.run()
