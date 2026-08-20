"""opensandbox 实现（spike 2/3 已验证 ✅）。DESIGN.md §4.6。

- 本地 podman / 生产 K8s 由 ``config.Settings``（SANDBOX_* 环境变量）切换，代码一致。
- ⚠️ code-interpreter 镜像必须传 ``entrypoint=["/opt/opensandbox/code-interpreter.sh"]``（spike 踩坑）。
- 沙箱生命周期 per-node：``create()`` 建、``destroy()`` 销毁，节点内多次执行复用同一沙箱。
"""
from __future__ import annotations

from typing import Any

from agentflow.config import settings
from agentflow.sandbox.adapter import SandboxResult

_LANG = {
    "python": "PYTHON", "py": "PYTHON",
    "bash": "BASH", "sh": "BASH",
    "java": "JAVA", "go": "GO", "js": "JAVASCRIPT", "ts": "TYPESCRIPT",
}


class OpenSandboxAdapter:
    def __init__(self, *, domain: str | None = None, image: str | None = None,
                 entrypoint: str | None = None, python_version: str = "3.11"):
        self.domain = domain or settings.sandbox_domain
        self.image = image or settings.sandbox_image
        self.entrypoint = entrypoint or settings.sandbox_entrypoint
        self.python_version = python_version
        self._sandbox: Any = None
        self._interpreter: Any = None

    @property
    def sandbox_id(self) -> str:
        return getattr(self._sandbox, "id", "") or ""

    async def create(self) -> "OpenSandboxAdapter":
        from code_interpreter import CodeInterpreter
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig

        self._sandbox = await Sandbox.create(
            self.image,
            connection_config=ConnectionConfig(domain=self.domain),
            entrypoint=self.entrypoint,
            env={"PYTHON_VERSION": self.python_version},
        )
        self._interpreter = await CodeInterpreter.create(sandbox=self._sandbox)
        return self

    async def _ensure(self):
        if self._interpreter is None:
            await self.create()
        return self._interpreter

    async def run_code(self, code: str, language: str = "python") -> SandboxResult:
        from code_interpreter import SupportedLanguage

        interp = await self._ensure()
        lang = getattr(SupportedLanguage, _LANG.get(language, "PYTHON"))
        return self._to_result(await interp.codes.run(code, language=lang))

    async def run_shell(self, command: str) -> SandboxResult:
        return await self.run_code(command, language="bash")

    async def read_file(self, path: str) -> str:
        interp = await self._ensure()
        return await interp.files.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        interp = await self._ensure()
        await interp.files.write_file(path, content)

    async def destroy(self) -> None:
        if self._sandbox is not None:
            for op in ("kill", "close"):
                try:
                    await getattr(self._sandbox, op)()
                except Exception:  # noqa: BLE001 —— 销毁尽力而为
                    pass
            self._sandbox = None
            self._interpreter = None

    @staticmethod
    def _to_result(execution: Any) -> SandboxResult:
        """opencode 的 Execution → 我们的 SandboxResult（防腐）。"""
        logs = getattr(execution, "logs", None)
        stdout = "".join(o.text for o in (logs.stdout if logs else []))
        result = "".join(o.text for o in (getattr(execution, "result", None) or []))
        error = str(execution.error) if getattr(execution, "error", None) else None
        return SandboxResult(stdout=stdout, result=result, error=error,
                             exit_code=getattr(execution, "exit_code", None))
