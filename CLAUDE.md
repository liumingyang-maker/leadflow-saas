# CLAUDE.md

Claude Code 在本仓库工作时：

1. 首先完整阅读并遵守根目录 `AGENTS.md`；
2. 再阅读 `README_START_HERE.md`、`PRODUCT_SCOPE.md`、`ARCHITECTURE.md`、`UI_SYSTEM.md`；
3. 只执行 `TASKS.md` 中一个明确任务 ID；
4. 开始修改前先给出涉及文件、测试计划、风险和不修改范围；
5. 不得继续向 `web/app.py` 添加新业务；
6. 不得在没有回归测试时修改认证、租户隔离、支付、任务或迁移；
7. 完成后按 `AGENTS.md` 的完成报告格式输出；
8. 发现任务需要跨越既定范围时，停止扩大修改，并把后续工作写成新的任务建议。

`AGENTS.md` 与本文件冲突时，以 `AGENTS.md` 为准。
