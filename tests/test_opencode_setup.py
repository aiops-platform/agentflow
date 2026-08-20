"""opencode 接线测试：agent.md → opencode agent 转换 + MCP 配置生成。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from agentflow.config import AGENTS_DIR
from agentflow.opencode_setup import (MCP_SERVERS, build_agent_md, build_mcp_config,
                                      generate_agents)


def test_build_mcp_config():
    cfg = build_mcp_config(python="/usr/bin/python3")
    assert set(cfg) == set(MCP_SERVERS)
    assert len(cfg) == 6
    assert cfg["es-logs"]["type"] == "local"
    assert cfg["es-logs"]["command"] == ["/usr/bin/python3", "-m", "agentflow.tools.es_logs"]
    print("  ✓ test_build_mcp_config")


def test_build_agent_md():
    our_md = """---
permission:
  edit: deny
  bash: deny
tools:
  query_logs: {}
---
# 你是 log-analyst

查日志平台。
"""
    out = build_agent_md("log-analyst", our_md)
    assert "mode: subagent" in out
    assert "bash: deny" in out and "edit: deny" in out
    assert "read: allow" in out          # 默认只读允许
    assert "webfetch: deny" in out       # 其余默认拒绝
    assert "# 你是 log-analyst" in out   # 正文保留
    assert "query_logs" not in out       # MCP 工具走全局注册，不写进 permission
    print("  ✓ test_build_agent_md")


def test_generate_agents():
    with tempfile.TemporaryDirectory() as d:
        files = generate_agents(AGENTS_DIR, d)
        assert len(files) == 15
        triage = Path(d) / "triage.md"
        content = triage.read_text(encoding="utf-8")
        assert "mode: subagent" in content
        assert "bash: deny" in content
        # fix-implementer 应 edit: allow
        assert "edit: allow" in (Path(d) / "fix-implementer.md").read_text(encoding="utf-8")
    print("  ✓ test_generate_agents")


def main() -> None:
    test_build_mcp_config()
    test_build_agent_md()
    test_generate_agents()
    print("\nALL OPENCODE-SETUP TESTS PASS ✅")


if __name__ == "__main__":
    main()
