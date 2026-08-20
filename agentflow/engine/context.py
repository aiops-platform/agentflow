"""WorkflowContext：运行期上下文的 JSONPath 取值 / 回写。DESIGN.md §4.10。

上下文按 node_id 命名空间分区，天然解决并行分支写冲突（各节点只写自己的 ``nodes.<id>``）：

.. code-block:: json
   {
     "meta":   { "run_id": "...", "workflow": "..." },
     "inputs": { "repo": "...", "bug_report": {...} },
     "nodes":  { "logs": { "status": "done", "output": {...}, "stdout": "..." } }
   }
"""
from __future__ import annotations

import re
from typing import Any

from agentflow.domain import FailedOutput

_MISSING = object()


def _resolve(data: dict[str, Any], ref: str) -> Any:
    """解析 ``$.a.b.c`` 形式的 JSONPath，缺失返回 _MISSING。"""
    if not ref.startswith("$."):
        return _MISSING
    cur: Any = data
    for part in ref[2:].split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


class WorkflowContext:
    def __init__(self, run_id: str, workflow_name: str, inputs: dict[str, Any] | None = None):
        self.data: dict[str, Any] = {
            "meta": {"run_id": run_id, "workflow": workflow_name},
            "inputs": inputs or {},
            "nodes": {},
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "WorkflowContext":
        """从落盘快照恢复（断点续跑用）。"""
        ctx = cls(
            (snapshot.get("meta") or {}).get("run_id", ""),
            (snapshot.get("meta") or {}).get("workflow", ""),
            snapshot.get("inputs") or {},
        )
        ctx.data = snapshot
        return ctx

    def get(self, ref: str) -> Any:
        """取 JSONPath 引用值；缺失返回 None。"""
        v = _resolve(self.data, ref)
        return None if v is _MISSING else v

    def has(self, ref: str) -> bool:
        return _resolve(self.data, ref) is not _MISSING

    def set_node(self, node_id: str, *, status: str, output: Any = None,
                 stdout: str = "", attempts: int = 0) -> None:
        self.data["nodes"][node_id] = {
            "status": status,
            "output": output,
            "stdout": stdout,
            "attempts": attempts,
        }

    def mark_failed(self, node_id: str, reason: str, stdout: str = "") -> None:
        """失败节点写固定 fallback 结构（DESIGN.md §4.10.5）。"""
        self.set_node(
            node_id, status="failed",
            output=FailedOutput(error_reason=reason).model_dump(), stdout=stdout,
        )

    def resolve_params(self, params: dict[str, str]) -> dict[str, Any]:
        """把节点的 params（JSONPath 引用）解析成具体值；缺失为 None。"""
        out: dict[str, Any] = {}
        for key, ref in params.items():
            out[key] = self.get(ref)
        return out

    def snapshot(self) -> dict[str, Any]:
        return self.data


# ── when 条件求值 ──

_COMPARE_RE = re.compile(r"^\s*(\$\.\S+)\s*(==|!=)\s*(.+?)\s*$")


def eval_when(condition: str, ctx: WorkflowContext) -> bool:
    """求值 ``when`` 条件边：``$.path``（真值）/ ``$.path == literal`` / ``$.path != literal``。"""
    if not condition:
        return True
    m = _COMPARE_RE.match(condition)
    if m:
        ref, op, rhs = m.group(1), m.group(2), m.group(3).strip()
        val = ctx.get(ref)
        return (val == _parse_literal(rhs)) if op == "==" else (val != _parse_literal(rhs))
    val = ctx.get(condition.strip())
    return bool(val)


def _parse_literal(s: str) -> Any:
    s = s.strip()
    if s == "true":
        return True
    if s == "false":
        return False
    if s in ("null", "None"):
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s
