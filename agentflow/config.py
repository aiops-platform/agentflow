"""集中配置：本地 / 生产差异收敛在此（DESIGN.md §4.9）。

配置一律走环境变量（凭据不进代码、不进 YAML，DESIGN.md §4.11.7）。本地 SQLite、生产 Postgres 由
环境切换。各适配层（opencode/sandbox/观测）随 M1-M4 逐步接入。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent        # my-agent-cc 根目录
EXAMPLES_DIR = ROOT / "examples"
WORKDIR = Path(os.environ.get("AGENTFLOW_WORKDIR", str(ROOT / "workdir")))


@dataclass
class Settings:
    """运行时设置（首期仅编排相关，其余随里程碑补齐）。"""
    # 编排（M2）
    concurrency: int = int(os.environ.get("AGENTFLOW_CONCURRENCY", "4"))

    # opencode（M1）
    opencode_url: str = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4090")

    # 状态存储（M2）：本地 SQLite，生产 Postgres/Redis
    state_dsn: str = os.environ.get(
        "AGENTFLOW_STATE_DSN", f"sqlite:///{WORKDIR / 'state.db'}"
    )

    # 观测（M4）
    langfuse_url: str | None = os.environ.get("LANGFUSE_URL") or None
    langfuse_public_key: str | None = os.environ.get("LANGFUSE_PUBLIC_KEY") or None
    langfuse_secret_key: str | None = os.environ.get("LANGFUSE_SECRET_KEY") or None


settings = Settings()
