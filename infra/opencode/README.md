# opencode 运行时（LLM 驱动）

opencode 是驱动 agent 的 LLM 运行时，每个 workflow 节点 = 一个 opencode session（经 `agentflow/opencode/server_adapter.py` 的 HTTP+SSE 直连）。

## 启动 opencode serve

```bash
cd <后端 repo 根>
set -a; source .env; set +a   # 加载 DEEPSEEK_API_KEY（opencode 不自动读 .env）
opencode serve --hostname 127.0.0.1 --port 4090
```

## 注册 MCP + 生成 agent

```bash
python -m agentflow opencode-setup --apply
# 1) 注册 7 个 MCP server：es-logs / prometheus-metrics / k8s / cmdb / service-topology / opensandbox / read-upstream
# 2) 生成 15 个 agent 到 ~/.config/opencode/agents/<name>.md（成为 opencode subagent）
```

## 模型配置

- 默认模型：`~/.config/opencode/opencode.json` 的 `model` 字段（当前 `deepseek/deepseek-v4-flash`）。
- API key：项目根 `.env` 的 `DEEPSEEK_API_KEY`（已 gitignore）。
- deepseek 是 opencode 内置 provider，baseURL `https://api.deepseek.com`。
