"""K8s MCP server。DESIGN.md §4.5 / SCENARIOS.md §2.4。

- 只读（infra-locator）：``describe_pod`` / ``get_events``
- 写（infra-remediator，写操作审批门禁 M4）：``scale`` / ``apply`` / ``exec``

实现：调用 ``kubectl`` CLI（子进程）。写操作用 ``K8S_READONLY=1`` 可强制只读（本地安全网）。

配置（环境变量）：:

  K8S_NAMESPACE  默认 ``order``
  K8S_READONLY   默认 false；true 时写工具直接拒绝
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP

from agentflow.tools.common import evidence, get_env

mcp = FastMCP("k8s")

K8S_NAMESPACE = get_env("K8S_NAMESPACE", "order")
K8S_READONLY = get_env("K8S_READONLY", False, cast=bool)


async def _default_kubectl(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "kubectl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(), err.decode()


# 模块级 runner，测试可替换
_runner = _default_kubectl


def _ns(namespace: str | None) -> str:
    return namespace or K8S_NAMESPACE


@mcp.tool()
async def describe_pod(pod: str, namespace: str | None = None) -> dict:
    """查 pod 状态（phase/restart/container/oom/驱逐），infra-locator 定位资源载体。"""
    code, out, err = await _runner("get", "pod", pod, "-n", _ns(namespace), "-o", "json")
    if code != 0:
        return evidence("error", 60, error=err.strip(), pod=pod)
    return evidence("ok", 60, pod=pod, namespace=_ns(namespace), pod_json=json.loads(out))


@mcp.tool()
async def get_events(namespace: str | None = None) -> dict:
    """查命名空间 K8s 事件（重启/驱逐/调度等）。"""
    code, out, err = await _runner("get", "events", "-n", _ns(namespace), "-o", "json")
    if code != 0:
        return evidence("error", 60, error=err.strip())
    return evidence("ok", 60, namespace=_ns(namespace), events=json.loads(out))


@mcp.tool()
async def scale(deployment: str, replicas: int, namespace: str | None = None) -> dict:
    """扩缩容 deployment（写操作，审批门禁 M4）。"""
    if K8S_READONLY:
        return evidence("error", 60, error="K8S_READONLY=1，写操作被拒绝")
    code, out, err = await _runner("scale", f"deploy/{deployment}", f"--replicas={replicas}",
                                   "-n", _ns(namespace))
    if code != 0:
        return evidence("error", 60, error=err.strip(), deployment=deployment)
    return evidence("ok", 60, deployment=deployment, replicas=replicas, output=out.strip())


@mcp.tool()
async def apply(manifest_yaml: str) -> dict:
    """apply K8s 清单（写操作，审批门禁 M4）。manifest_yaml 为 YAML 文本，经 stdin 提交。"""
    if K8S_READONLY:
        return evidence("error", 60, error="K8S_READONLY=1，写操作被拒绝")
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "apply", "-f", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(input=manifest_yaml.encode())
    if proc.returncode != 0:
        return evidence("error", 60, error=err.decode().strip())
    return evidence("ok", 60, output=out.decode().strip())


@mcp.tool()
async def exec_pod(pod: str, command: str, namespace: str | None = None) -> dict:
    """在 pod 内执行命令（止血：清盘/查盘等，写操作，审批门禁 M4）。"""
    if K8S_READONLY:
        return evidence("error", 60, error="K8S_READONLY=1，写操作被拒绝")
    code, out, err = await _runner("exec", pod, "-n", _ns(namespace), "--",
                                   *command.split())
    if code != 0:
        return evidence("error", 60, error=err.strip(), pod=pod)
    return evidence("ok", 60, pod=pod, output=out)


if __name__ == "__main__":
    mcp.run()
