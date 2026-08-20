"""AgentRegistry：扫描 ``agents/`` 目录，加载 agent.md，结合领域 output schema。DESIGN.md §4.2。

新增 agent = 新增目录（含 agent.md）。YAML 里 ``agent: triage`` 即插拔。
"""
from __future__ import annotations

from pathlib import Path

from agentflow.agents.base import AgentSpec, parse_agent_md
from agentflow.domain import AGENT_OUTPUT_SCHEMAS


class AgentRegistry:
    def __init__(self, agents_dir: str | Path):
        self.agents_dir = Path(agents_dir)
        self._specs: dict[str, AgentSpec] = {}

    def load(self) -> "AgentRegistry":
        if not self.agents_dir.is_dir():
            return self
        for d in sorted(self.agents_dir.iterdir()):
            if not d.is_dir():
                continue
            spec = self._load_one(d)
            if spec is not None:
                self._specs[spec.name] = spec
        return self

    def _load_one(self, d: Path) -> AgentSpec | None:
        md = d / "agent.md"
        system_prompt, fm = parse_agent_md(md.read_text(encoding="utf-8")) if md.exists() else ("", {})

        tools = fm.get("tools") or {}
        if isinstance(tools, dict):
            tools = list(tools.keys())

        return AgentSpec(
            name=d.name,
            description=fm.get("description") or "",
            system_prompt=system_prompt,
            output_schema=AGENT_OUTPUT_SCHEMAS.get(d.name),
            tools=tools,
            model=fm.get("model"),
            permissions=fm.get("permission") or fm.get("permissions") or {},
        )

    def get(self, name: str) -> AgentSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)
