# opensandbox 沙箱（基础设施）

opensandbox 是代码执行沙箱，`fix-implementer` / `tester` 通过 MCP 工具 `run_python` / `run_shell` 在沙箱内跑代码（隔离，不落宿主机）。

## 启动 opensandbox-server

```bash
# 前置：podman machine 已启动（podman 提供 docker 兼容 socket）
OPENSANDBOX_INSECURE_SERVER=YES opensandbox-server --config infra/opensandbox/sandbox.toml
# 监听 127.0.0.1:8080（SANDBOX_DOMAIN 默认值）
```

## 所需镜像（podman 需先拉取）

| 镜像 | 用途 |
|---|---|
| `opensandbox/code-interpreter:v1.0.2` | 沙箱主镜像 |
| `opensandbox/execd:v1.0.21` | 沙箱执行环境 |
| `opensandbox/egress:v1.1.4` | 出站代理 |

## 关键坑

- `Sandbox.create` 的 `entrypoint` 必须是 **list**（`["/opt/opensandbox/code-interpreter.sh"]`），不是字符串——否则 `POST /v1/sandboxes` 返回 422，agent 会静默降级宿主机执行。
- 沙箱生命周期 **per-node**：节点开始创建、结束销毁，节点内多次执行复用同一沙箱。
- podman 拉 `docker.io` 镜像被墙时需配镜像源（见 `spike/README.md` 的坑 1）。
