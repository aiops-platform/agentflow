"""Workflow 定义与解析（DESIGN.md §4.1 / §4.3）。

- ``schema.py``：workflow YAML 的 Pydantic schema
- ``parser.py``：YAML → WorkflowDef + JSONPath 引用校验
- ``dag.py``：DAG 构建、拓扑排序、环/悬空节点检测
"""
