"""Workflow 定义 schema（Pydantic v2）。DESIGN.md §4.1。

YAML 是唯一事实来源。``nodes``（agent / params / approve / retry / timeout /
on_schema_error / on_failure / idempotency_key）+ ``edges``（from / to / when 条件边）。

参数引用约定（DESIGN.md §4.10）：``$.inputs.*`` 全局入参、``$.meta.*`` 运行元数据、
``$.nodes.<id>.output.*`` 上游结构化输出。params 里声明 ``$.nodes.<upstream>.output``
即产生隐式 DAG 依赖边（§4.10.3）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class InputDef(BaseModel):
    """workflow 级入参定义。"""
    type: str = "string"            # string / object / int / ...
    required: bool = False
    default: Any = None


class RetryDef(BaseModel):
    """infra 失败重试（仅对 infra 失败生效，logic 失败走 on_failure，DESIGN.md §4.11.1）。"""
    max: int = 3
    backoff_seconds: float = 1.0


class NodeDef(BaseModel):
    """单个节点（= 一个 opencode session）。"""
    agent: str                                        # 职能 agent 名，对应 agents/<name>/
    params: dict[str, str] = Field(default_factory=dict)   # JSONPath 引用，声明即产生依赖边
    approve: str | None = None                        # 审批触发条件（如 high-risk / write）
    retry: RetryDef | None = None                     # infra 失败重试
    timeout: int | None = None                        # 节点超时（秒）
    on_schema_error: Literal["fail", "retry", "coerce"] = "fail"
    on_failure: Literal["abort", "continue"] = "abort"
    idempotency_key: str | None = None                # 有副作用节点幂等键
    input_view: Literal["summary", "full"] = "summary"  # 输入裁剪：summary 去 details（默认）/ full 全量

    @model_validator(mode="before")
    @classmethod
    def _coerce_retry(cls, data: Any) -> Any:
        # 允许 retry: 3 简写为 retry: {max: 3}
        if isinstance(data, dict) and isinstance(data.get("retry"), int):
            data = dict(data)
            data["retry"] = {"max": data["retry"]}
        return data


class EdgeDef(BaseModel):
    """DAG 边；``when`` 为条件边（JSONPath 布尔表达式，见 DESIGN.md §2.5）。"""
    model_config = {"populate_by_name": True}

    from_: str = Field(alias="from")
    to: str
    when: str | None = None


class WorkflowDef(BaseModel):
    """一份 workflow 的完整定义。"""
    name: str
    inputs: dict[str, InputDef] = Field(default_factory=dict)
    nodes: dict[str, NodeDef]
    edges: list[EdgeDef] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_inputs(cls, data: Any) -> Any:
        # 允许 inputs 简写：{ repo: {type: string, required: true} } 或 { repo: {} }
        if isinstance(data, dict) and isinstance(data.get("inputs"), dict):
            normalized: dict[str, Any] = {}
            for key, val in data["inputs"].items():
                if val is None:
                    normalized[key] = {}
                elif isinstance(val, dict):
                    normalized[key] = val
                else:
                    normalized[key] = {"type": str(val)}
            data = dict(data)
            data["inputs"] = normalized
        return data
