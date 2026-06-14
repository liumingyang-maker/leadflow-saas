# LeadFlow SaaS 重构文档包

> 适用仓库：`liumingyang-maker/leadflow-saas`
> 审查日期：2026-06-14
> 目标：在不推倒现有产品的前提下，把项目改造成新手可维护、可测试、可上线、适合 Codex / Claude Code 持续开发的结构。

## 一句话结论

当前项目已经具备“可演示、可试用”的产品雏形，但功能增长速度超过了工程结构承载能力。现在不应继续大量加渠道，而应先完成：

1. 安全止血；
2. 建立测试基线；
3. 拆分 `web/app.py`；
4. 把后台任务移出 Web 进程；
5. 建立统一 UI 系统；
6. 用任务卡约束 AI 每次只改一个边界。

## 推荐阅读顺序

1. `REPO_AUDIT.md`：现状、风险、保留/重构/冻结清单
2. `PRODUCT_SCOPE.md`：产品范围与版本边界
3. `ARCHITECTURE.md`：渐进式目标架构
4. `UI_SYSTEM.md`：页面结构、组件、视觉规范
5. `AGENTS.md`：Codex / Claude Code 必须遵守的规则
6. `TASKS.md`：按顺序可直接执行的任务清单
7. `DEVELOPMENT_WORKFLOW.md`：分支、测试、PR、发布流程
8. `DEPLOYMENT_ITERATION.md`：上线、监控、备份与迭代

## 不要先做的事情

- 不要先重写成 React、Next.js、FastAPI 或微服务。
- 不要一次性把 SQLite 全换成 PostgreSQL。
- 不要让 AI 执行“整体优化整个项目”。
- 不要继续添加更多爬虫渠道。
- 不要边拆架构边大改 UI、支付和业务规则。
- 不要删除旧路由，先兼容迁移。

## 第一阶段完成标准

当以下条件全部满足，才继续大规模做新功能：

- 默认管理员密码已取消；
- 所有写操作具有 CSRF 防护或明确的公开 API 签名机制；
- 至少有 25 个关键自动化测试；
- `web/app.py` 不再直接承载主要业务模块；
- 采集任务、自动跟进、竞品监控和备份不再由 Web 进程内线程执行；
- 每个任务都记录 `tenant_id`，查询任务状态时校验租户；
- CI 能自动执行 lint、test 和安全检查；
- 生产环境可一键回滚到上一镜像。

## 建议新目录

```text
leadflow-saas/
├─ app/
│  ├─ __init__.py
│  ├─ config.py
│  ├─ extensions.py
│  ├─ security.py
│  ├─ blueprints/
│  │  ├─ auth/
│  │  ├─ onboarding/
│  │  ├─ leads/
│  │  ├─ collection/
│  │  ├─ outreach/
│  │  ├─ radar/
│  │  ├─ billing/
│  │  ├─ settings/
│  │  ├─ inbound/
│  │  └─ admin/
│  ├─ domain/
│  │  ├─ tenants/
│  │  ├─ leads/
│  │  ├─ collection/
│  │  ├─ outreach/
│  │  └─ billing/
│  ├─ repositories/
│  ├─ integrations/
│  ├─ jobs/
│  ├─ templates/
│  └─ static/
├─ migrations/
├─ tests/
├─ docs/
├─ scripts/
├─ AGENTS.md
├─ CLAUDE.md
├─ TASKS.md
├─ pyproject.toml
├─ Makefile
└─ docker-compose.yml
```

这是目标结构，不要求一次完成。严格按 `TASKS.md` 渐进迁移。
