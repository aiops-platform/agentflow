"""Spike 3: 包装 opensandbox 的 MCP server，向 opencode agent 暴露 run_python 工具。

以 stdio 方式运行，由 opencode 通过 `opencode mcp add` 拉起。
"""
import asyncio

from fastmcp import FastMCP

mcp = FastMCP("opensandbox")

LOG_FILE = "/tmp/mcp-run-python.log"
_sandbox = None
_interpreter = None
_lock = asyncio.Lock()


def _log(msg: str) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


async def _get_interpreter():
    """懒加载一个常驻沙箱（复用，避免每次调用都建容器）。"""
    global _sandbox, _interpreter
    async with _lock:
        if _interpreter is None:
            from opensandbox import Sandbox
            from opensandbox.config import ConnectionConfig
            from code_interpreter import CodeInterpreter

            config = ConnectionConfig(domain="127.0.0.1:8080")
            _sandbox = await Sandbox.create(
                "opensandbox/code-interpreter:v1.0.2",
                connection_config=config,
                entrypoint=["/opt/opensandbox/code-interpreter.sh"],
                env={"PYTHON_VERSION": "3.11"},
            )
            _interpreter = await CodeInterpreter.create(sandbox=_sandbox)
    return _interpreter


@mcp.tool()
async def run_python(code: str) -> str:
    """在隔离沙箱中执行 Python 代码，返回 stdout 与最终表达式结果。

    用于需要真实运行代码验证的场景（计算、数据处理、调用库等）。
    """
    from code_interpreter import SupportedLanguage

    _log(f"CALL run_python code={code!r}")
    interp = await _get_interpreter()
    r = await interp.codes.run(code, language=SupportedLanguage.PYTHON)
    stdout = "".join(o.text for o in (r.logs.stdout if r.logs else []))
    result = "".join(o.text for o in (r.result if r.result else []))
    err = str(r.error) if r.error else ""
    _log(f"RESULT stdout={stdout!r} result={result!r} err={err!r}")
    out = f"stdout:\n{stdout or '(none)'}\nresult:\n{result or '(none)'}"
    if err:
        out += f"\nerror:\n{err}"
    return out


if __name__ == "__main__":
    mcp.run()
