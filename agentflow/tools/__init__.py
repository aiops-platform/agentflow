"""数据源 MCP 工具层（DESIGN.md §4.5）。

统一以 MCP server 接入数据源，opencode 原生支持 MCP，每个 agent 在 frontmatter 挂载自己的
MCP 工具集。本包每个数据源一个文件：

- ``es_logs.py``            —— Elasticsearch 日志查询（``query_logs``），log-analyst / trace-analyst
- ``prometheus_metrics.py`` —— Prometheus 指标查询（``query_metrics``），metrics-analyst
- （后续）k8s.py / cmdb.py / topology.py —— infra-locator / triage / root-cause

统一约定：证据类返回带 ``query_status``（ok/empty/error）+ ``collected_at`` + ``ttl_seconds``，
见 :mod:`agentflow.tools.common`。
"""
