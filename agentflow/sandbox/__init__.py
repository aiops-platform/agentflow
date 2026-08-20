"""沙箱适配层（DESIGN.md §4.6）。

- ``adapter.py``：``Sandbox`` 接口 + ``SandboxResult``（防腐层）
- ``opensandbox.py``：``OpenSandboxAdapter``（本地 podman / 生产 K8s）
- ``mcp_server.py``：MCP server（run_python / run_shell / read_file / write_file）
"""
from agentflow.sandbox.adapter import Sandbox, SandboxResult  # noqa: F401
from agentflow.sandbox.opensandbox import OpenSandboxAdapter  # noqa: F401

__all__ = ["Sandbox", "SandboxResult", "OpenSandboxAdapter"]
