"""支持 ``python -m agentflow ...``（DESIGN.md §4.9）。"""
from agentflow.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
