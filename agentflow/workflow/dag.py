"""DAG 构建与校验：拓扑排序 / 环检测 / 悬空节点。DESIGN.md §4.3。

边来源 = 显式 ``edges`` + 隐式边（params 引用 ``$.nodes.<upstream>.output``，§4.10.3）。
校验项：悬空节点、自环、环、JSONPath 合法性、无节点。
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from agentflow.workflow.parser import iter_node_refs, validate_params_refs
from agentflow.workflow.schema import WorkflowDef


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    topo_order: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def build_edges(wf: WorkflowDef) -> list[tuple[str, str]]:
    """显式边 ∪ 隐式边（params 引用上游 output 产生的依赖边）。"""
    edges: list[tuple[str, str]] = [(e.from_, e.to) for e in wf.edges]
    for upstream, node_id in iter_node_refs(wf):
        if (upstream, node_id) not in edges:
            edges.append((upstream, node_id))
    return edges


def topological_sort(nodes: set[str], edges: list[tuple[str, str]]) -> list[str]:
    """Kahn 算法；edges 需已剔除悬空端点。"""
    indeg: dict[str, int] = {n: 0 for n in nodes}
    adj: dict[str, list[str]] = defaultdict(list)
    for u, v in edges:
        if u not in nodes or v not in nodes:
            continue
        adj[u].append(v)
        indeg[v] += 1

    queue = deque(sorted(n for n in nodes if indeg[n] == 0))
    order: list[str] = []
    while queue:
        u = queue.popleft()
        order.append(u)
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    return order


def validate(wf: WorkflowDef) -> ValidationResult:
    res = ValidationResult()
    node_ids = set(wf.nodes)

    if not node_ids:
        res.errors.append("workflow 没有定义任何节点")
        return res

    # 1. 显式边悬空 / 自环
    for e in wf.edges:
        if e.from_ not in node_ids:
            res.errors.append(f"edge 引用不存在的 from 节点 '{e.from_}'")
        if e.to not in node_ids:
            res.errors.append(f"edge 引用不存在的 to 节点 '{e.to}'")
        if e.from_ == e.to and e.from_ in node_ids:
            res.errors.append(f"edge 自环 '{e.from_}'")

    # 2. params JSONPath 校验
    res.errors.extend(validate_params_refs(wf))

    # 3. 环检测（在剔除悬空端点后的图上做，避免悬空边误报成环）
    valid_edges = [(u, v) for (u, v) in build_edges(wf) if u in node_ids and v in node_ids]
    order = topological_sort(node_ids, valid_edges)
    if len(order) != len(node_ids):
        leftover = sorted(node_ids - set(order))
        res.errors.append(f"DAG 存在环，涉及节点: {leftover}")
    else:
        res.topo_order = order

    return res
