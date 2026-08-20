"""YAML → WorkflowDef 解析 + JSONPath 引用校验。DESIGN.md §4.1 / §4.10.3。

JSONPath 引用约定：
- 合法根：``$.inputs.*``（全局入参）、``$.meta.*``（运行元数据）、``$.nodes.<id>.output.*``（上游输出）
- 禁止 params 引用 ``$.nodes.*.stdout``（原始输出不进 prompt，必须走 summary/details，DESIGN.md §4.10.2）
- ``$.nodes.<upstream>.output`` 声明即产生隐式依赖边（供 dag 构建）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

import yaml

from agentflow.workflow.schema import WorkflowDef

_REF_RE = re.compile(r"^\$\.(inputs|meta|nodes)\.(.+)$")
_STDOUT_RE = re.compile(r"^\$\.nodes\.[^.]+\.stdout")


class WorkflowParseError(Exception):
    """YAML 结构级错误（顶层不是映射等）。"""


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise WorkflowParseError(f"{path}: 顶层必须是映射（应含 name/nodes/edges）")
    return data


def parse(path: str | Path) -> WorkflowDef:
    return WorkflowDef.model_validate(load_yaml(path))


def _ref_target(value: str) -> tuple[str, str] | None:
    """解析引用，返回 (root, rest)；非法返回 None。"""
    m = _REF_RE.match(value or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def iter_node_refs(wf: WorkflowDef) -> Iterator[tuple[str, str]]:
    """遍历所有节点 params，产出依赖边 ``(上游节点, 当前节点)``。

    节点 params 里引用 ``$.nodes.<upstream>.output`` 表示「当前节点依赖 upstream」，
    即声明一条 DAG 边 ``upstream → 当前节点``（DESIGN.md §4.10.3）。
    """
    for node_id, node in wf.nodes.items():
        for value in node.params.values():
            t = _ref_target(value)
            if t and t[0] == "nodes":
                upstream = t[1].split(".")[0]
                yield upstream, node_id


def validate_params_refs(wf: WorkflowDef) -> list[str]:
    """校验所有节点 params 的 JSONPath 合法性，返回错误列表。"""
    errors: list[str] = []
    for node_id, node in wf.nodes.items():
        for key, value in node.params.items():
            if value is None:
                continue
            if not isinstance(value, str) or not value.startswith("$"):
                errors.append(f"node '{node_id}' param '{key}' 不是合法 JSONPath: {value!r}")
                continue
            if _STDOUT_RE.match(value):
                errors.append(
                    f"node '{node_id}' param '{key}' 引用 stdout（禁止，需走 summary/details）: {value}"
                )
                continue
            t = _ref_target(value)
            if t is None:
                errors.append(f"node '{node_id}' param '{key}' JSONPath 非法: {value}")
                continue
            root, rest = t
            if root == "nodes":
                upstream = rest.split(".")[0]
                if upstream not in wf.nodes:
                    errors.append(f"node '{node_id}' 引用不存在的上游节点 '{upstream}': {value}")
                elif rest != upstream and not rest.startswith(upstream + ".output"):
                    errors.append(f"node '{node_id}' param '{key}' 只能引用 .output（stdout 禁止）: {value}")
    return errors
