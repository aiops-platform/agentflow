"""CLI 入口：``python -m agentflow validate|run|list``。DESIGN.md §4.9 / §九。

- ``validate``（M0）：静态校验（schema / 环 / 悬空节点 / JSONPath）+ 拓扑序。
- ``run``（M2）：本地运行 workflow（默认接真实 opencode serve；SQLite 持久化支持断点续跑）。
- ``list``（M2）：列出可插拔 agent。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from agentflow.agents import AgentRegistry
from agentflow.config import AGENTS_DIR, WORKDIR
from agentflow.engine import DAGExecutor
from agentflow.engine.state import SqliteStore
from agentflow.opencode import OpenCodeAdapter
from agentflow.workflow.dag import validate
from agentflow.workflow.parser import WorkflowParseError, parse


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        wf = parse(args.workflow)
    except ValidationError as e:
        print(f"❌ schema 校验失败: {e}", file=sys.stderr)
        return 1
    except WorkflowParseError as e:
        print(f"❌ 解析失败: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    res = validate(wf)
    print(f"workflow: {wf.name}")
    print(f"nodes ({len(wf.nodes)}): {', '.join(wf.nodes)}")
    print(f"edges: {len(wf.edges)} 条显式边")

    if res.errors:
        print("\n❌ 校验失败:")
        for e in res.errors:
            print(f"  - {e}")
        return 1

    print("\n✅ 校验通过")
    if res.topo_order:
        print("拓扑序:", " → ".join(res.topo_order))
    for w in res.warnings:
        print(f"⚠️  {w}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        wf = parse(args.workflow)
    except (ValidationError, WorkflowParseError, FileNotFoundError) as e:
        print(f"❌ 解析失败: {e}", file=sys.stderr)
        return 1

    res = validate(wf)
    if res.errors:
        print("❌ workflow 校验未通过，先修再跑:", file=sys.stderr)
        for e in res.errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    inputs: dict = {}
    if args.trigger:
        inputs = json.loads(Path(args.trigger).read_text(encoding="utf-8"))

    registry = AgentRegistry(AGENTS_DIR).load()
    runtime = OpenCodeAdapter()
    store = SqliteStore(WORKDIR / "state.db")
    executor = DAGExecutor(runtime, store=store, registry=registry)
    try:
        result = asyncio.run(executor.run(wf, inputs=inputs,
                                          run_id=args.resume, resume=bool(args.resume)))
    finally:
        asyncio.run(runtime.aclose())
        store.close()

    print(f"\nrun_id: {result.run_id}  status: {result.status}")
    for nid, nr in result.nodes.items():
        marker = {"done": "✅", "failed": "❌", "skipped": "⏭️", "cancelled": "🚫"}.get(nr.status, "?")
        extra = f" (tokens={nr.tokens}, cost={nr.cost})" if nr.tokens else ""
        print(f"  {marker} {nid}: {nr.status}{extra}")
        if nr.error:
            print(f"       error: {nr.error}")
    return 0 if result.ok else 1


def _cmd_list(args: argparse.Namespace) -> int:
    registry = AgentRegistry(AGENTS_DIR).load()
    print(f"可插拔 agent（{len(registry)} 个）:")
    for name in registry.names():
        spec = registry.get(name)
        tools = f"  tools={spec.tools}" if spec.tools else ""
        print(f"  - {name}: {spec.description}{tools}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow", description="AIOps Bug Fix 工作流平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="静态校验 workflow.yaml（环/悬空/JSONPath）")
    p_validate.add_argument("workflow", help="workflow YAML 路径")

    p_run = sub.add_parser("run", help="运行 workflow（需 opencode serve）")
    p_run.add_argument("workflow", help="workflow YAML 路径")
    p_run.add_argument("--trigger", help="触发入参 JSON（ticket 等）")
    p_run.add_argument("--resume", help="断点续跑：从该 run_id 恢复（跳过 done，重跑 failed）")

    sub.add_parser("list", help="列出可插拔 agent")

    args = parser.parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "list":
        return _cmd_list(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
