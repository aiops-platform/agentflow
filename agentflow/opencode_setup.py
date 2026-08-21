"""opencode 接线：把平台的数据源 MCP server + 职能 agent 注册/生成到 opencode。DESIGN.md §4.2/§4.5。

两件事：
1. 注册 MCP server（``opencode mcp add``）—— 让 query_logs / query_metrics / describe_pod 等工具可用。
2. 生成 opencode agent（``agents/*/agent.md`` → ``~/.config/opencode/agents/<name>.md``）—— 让每个职能
   agent 成为 opencode 的 subagent（system prompt 角色方法论 + permission 权限）。

用法：
  python -m agentflow opencode-setup [--dry-run] [--agents-dir DIR] [--output-dir DIR]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from agentflow.agents.base import parse_agent_md
from agentflow.config import AGENTS_DIR

# 数据源 MCP server → 模块（opencode mcp add <name> -- python -m <module>）
MCP_SERVERS: dict[str, str] = {
    "es-logs": "agentflow.tools.es_logs",
    "prometheus-metrics": "agentflow.tools.prometheus_metrics",
    "k8s": "agentflow.tools.k8s",
    "cmdb": "agentflow.tools.cmdb",
    "service-topology": "agentflow.tools.topology",
    "opensandbox": "agentflow.sandbox.mcp_server",
    "read-upstream": "agentflow.tools.read_upstream",
}

# opencode 内置工具全集（permission 按此展开）
OPENCODE_TOOLS = ["bash", "read", "edit", "glob", "grep", "webfetch",
                  "task", "todowrite", "websearch", "lsp", "skill"]

# MCP server → 暴露的工具名（用于按 agent 隔离 MCP 工具）
MCP_TOOLS: dict[str, list[str]] = {
    "es-logs": ["query_logs"],
    "prometheus-metrics": ["query_metrics"],
    "k8s": ["describe_pod", "get_events", "scale", "apply", "exec_pod"],
    "cmdb": ["get_ci"],
    "service-topology": ["get_service", "get_dependencies", "get_dependents", "get_path"],
    "opensandbox": ["run_python", "run_shell", "read_file", "write_file"],
    "read-upstream": ["read_upstream_output"],
}
ALL_MCP_TOOLS = sorted({t for tools in MCP_TOOLS.values() for t in tools})


def build_mcp_config(python: str | None = None) -> dict:
    """生成 opencode.jsonc 的 ``mcp`` 配置片段。"""
    py = python or sys.executable
    return {name: {"type": "local", "command": [py, "-m", mod]}
            for name, mod in MCP_SERVERS.items()}


def build_agent_md(name: str, our_md: str) -> str:
    """把我们的 agent.md 转成 opencode agent 文件（frontmatter mode=subagent + permission + 正文）。

    工具隔离：agent frontmatter ``tools`` 里的 MCP 工具名 → ``permission[tool]=allow``，
    其余 MCP 工具 → ``deny``（MCP 工具经 ``permission`` 的 additionalProperties 声明）。
    """
    body, fm = parse_agent_md(our_md)
    perm = fm.get("permission") or {}
    agent_tools = set((fm.get("tools") or {}).keys())

    lines = ["---", f"description: {name} 职能 agent（AIOps bug-fix）", "mode: subagent", "permission:"]
    for tool in OPENCODE_TOOLS:
        action = perm.get(tool, "allow" if tool == "read" else "deny")
        lines.append(f"  {tool}: {action}")
    for tool in ALL_MCP_TOOLS:
        action = "allow" if tool in agent_tools else "deny"
        lines.append(f"  {tool}: {action}")
    lines.append("---")
    lines.append("")
    lines.append(body.rstrip())
    return "\n".join(lines) + "\n"


def register_mcp_servers(python: str | None = None) -> list[str]:
    """注册所有 MCP server 到 opencode，返回执行的命令列表。

    - opencode 不在 PATH 时抛 RuntimeError（带明确提示），而非裸 FileNotFoundError。
    - 单个 server 注册失败（非零返回码）打印告警，不中断其余注册。
    """
    if shutil.which("opencode") is None:
        raise RuntimeError(
            "找不到 opencode 命令（不在 PATH 中）。请先安装 opencode 并确保可执行，例如：\n"
            "  export PATH=\"$HOME/.nvm/versions/node/v24.19.0/bin:$PATH\"\n"
            "（opencode 经 npm 安装，位于 nvm 的 bin 目录；安装见 aiops/agentflow-spike 仓库 README）"
        )
    py = python or sys.executable
    commands: list[str] = []
    for name, mod in MCP_SERVERS.items():
        commands.append(f"opencode mcp add {name} -- {py} -m {mod}")
        r = subprocess.run(["opencode", "mcp", "add", name, "--", py, "-m", mod],
                           check=False, capture_output=True, text=True)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            print(f"⚠️ 注册 MCP server '{name}' 失败: {msg}", file=sys.stderr)
    return commands


def generate_agents(agents_dir: str | Path | None = None,
                    output_dir: str | Path | None = None) -> list[Path]:
    """把 agents/<name>/agent.md 生成到 opencode agents 目录，返回生成的文件列表。"""
    src = Path(agents_dir or AGENTS_DIR)
    dst = Path(output_dir or Path.home() / ".config" / "opencode" / "agents")
    dst.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if src.is_dir():
        for d in sorted(src.iterdir()):
            md = d / "agent.md"
            if d.is_dir() and md.exists():
                out = dst / f"{d.name}.md"
                out.write_text(build_agent_md(d.name, md.read_text(encoding="utf-8")), encoding="utf-8")
                written.append(out)
    return written


def run_setup(apply_mcp: bool, agents_dir: str | None = None,
              output_dir: str | None = None) -> dict:
    """执行接线。``apply_mcp=False`` 为 dry-run（只规划不落盘/不注册）。返回计划 + 是否已执行。"""
    src = Path(agents_dir or AGENTS_DIR)
    dst = Path(output_dir or Path.home() / ".config" / "opencode" / "agents")
    planned_agents = [str(dst / f"{d.name}.md") for d in sorted(src.iterdir())
                      if d.is_dir() and (d / "agent.md").exists()]
    if apply_mcp:
        generate_agents(src, dst)
        mcp_commands = register_mcp_servers()
    else:
        mcp_commands = [f"opencode mcp add {name} -- python -m {mod}"
                        for name, mod in MCP_SERVERS.items()]
    return {"agents": planned_agents, "mcp_commands": mcp_commands, "applied": apply_mcp}
