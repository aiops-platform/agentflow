"""fix-implementer 可选声明（DESIGN.md §4.2 spec.py）。

- input_view = "full"：需要完整 FixPlan（changes/impact/test_requirements），不裁剪 details。
- requires_sandbox = True：改代码 + 自测需在 opensandbox 沙箱内执行。
"""
input_view = "full"
requires_sandbox = True
