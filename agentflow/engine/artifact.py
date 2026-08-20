"""per-attempt 工作区：文件 / 二进制 artifact 落盘路径。DESIGN.md §4.11.3。

- run 级共享 ``workdir/<run_id>/``；节点每次尝试用子目录 ``<node_id>/<attempt>/``，
  避免 retry 时旧 diff / 临时文件污染新一次执行。
- 结构化 output 只存相对路径 / URI 引用，大文件不落 StateStore（Postgres）。
"""
from __future__ import annotations

from pathlib import Path

from agentflow.config import WORKDIR


class ArtifactPaths:
    def __init__(self, run_id: str, root: Path | None = None):
        self.run_id = run_id
        self.root = root or (WORKDIR / run_id)

    def run_dir(self) -> Path:
        return self.root

    def node_dir(self, node_id: str, attempt: int = 0) -> Path:
        return self.root / node_id / f"attempt_{attempt}"

    def ensure_node(self, node_id: str, attempt: int = 0) -> Path:
        d = self.node_dir(node_id, attempt)
        d.mkdir(parents=True, exist_ok=True)
        return d
