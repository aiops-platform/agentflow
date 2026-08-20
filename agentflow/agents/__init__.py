"""可插拔职能智能体注册（DESIGN.md §4.2）。

- ``base.py``：``AgentSpec`` + agent.md frontmatter 解析
- ``registry.py``：``AgentRegistry``（扫描 agents/ 目录，加载 agent.md）
"""
from agentflow.agents.base import AgentSpec, parse_agent_md  # noqa: F401
from agentflow.agents.registry import AgentRegistry  # noqa: F401

__all__ = ["AgentSpec", "AgentRegistry", "parse_agent_md"]
