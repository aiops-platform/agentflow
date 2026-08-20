"""沙箱适配层接口（防腐层，DESIGN.md §4.6 / §4.12）。

接口按我们的需求定义，不按库的 API 定义：``run_code`` / ``run_shell`` / 文件读写 / 生命周期。
当前实现：``opensandbox.OpenSandboxAdapter``；可替换 E2B / Docker / Daytona。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class SandboxResult:
    stdout: str = ""
    result: str = ""
    error: str | None = None
    exit_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.exit_code is None or self.exit_code == 0)


@runtime_checkable
class Sandbox(Protocol):
    """隔离沙箱接口。生命周期 = per-node：节点开始创建、节点结束销毁（§4.6）。"""

    @property
    def sandbox_id(self) -> str: ...

    async def run_code(self, code: str, language: str = "python") -> SandboxResult: ...

    async def run_shell(self, command: str) -> SandboxResult: ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def destroy(self) -> None: ...
