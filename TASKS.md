# 第四部分（AI 协作任务）+ 执行任务清单

> 规则：严格按依赖顺序。一个分支/PR 只执行一个任务。
> 优先级：P0 阻止上线；P1 完成可维护核心；P2 产品增强；P3 扩展。

---

# 阶段 0：冻结与基线

## P0-000 建立重构基线

**目标**：记录当前可见行为，避免重构过程中“修好了结构却改坏功能”。

**允许修改**

- `tests/`
- `pytest.ini` 或 `pyproject.toml`
- `requirements-dev.txt`
- `Makefile`
- `.github/workflows/ci.yml`

**实施**

- 建立测试 app fixture 和临时 DATA_DIR；
- 写 smoke tests：landing、login、register、health（暂可新增）；
- 捕获当前 URL map 快照；
- 建立 Ruff 和 pytest；
- CI 执行 lint/test。

**验收**

- [ ] 本地一条命令运行测试；
- [ ] 测试不访问真实网络；
- [ ] 测试不使用仓库真实数据；
- [ ] CI 可重复通过。

---

# 阶段 1：安全止血

## P0-001 删除固定管理员默认密码

**依赖**：P0-000

**目标**：生产不再自动创建 `admin@leads.com/admin123`。

**允许修改**

- `core/admin_db.py`
- 新 CLI 文件
- `config.py`
- `DEPLOY.md`
- 测试

**实施**

- 删除固定账号自动创建；
- 新增 `flask admin create` 或 `python -m scripts.create_admin`；
- 账号密码来自交互输入或环境变量；
- 密码最少 12 位；
- 已存在固定账号时给出迁移警告，不自动重置；
- 管理员首次登录要求改密码。

**验收**

- [ ] 空数据库启动不会生成弱口令；
- [ ] CLI 可创建管理员；
- [ ] 重复邮箱失败；
- [ ] 密码不出现在日志；
- [ ] 文档删除默认凭据。

## P0-002 全局 CSRF

**依赖**：P0-000

**目标**：浏览器会话下所有写操作默认防 CSRF。

**允许修改**

- app 初始化/extension
- templates/components
- 所有写表单模板
- JS 请求封装
- 测试

**实施**

- 引入 Flask-WTF `CSRFProtect`；
- 普通表单加入 token；
- fetch/AJAX 统一加入 header；
- 明确列出公开 API 豁免；
- 支付 webhook、inbound 等写替代保护说明。

**验收**

- [ ] 无 token POST 返回 400；
- [ ] 合法 token 成功；
- [ ] webhook 按设计豁免；
- [ ] GET 不改变关键状态；
- [ ] logout 改 POST。

## P0-003 任务租户隔离

**依赖**：P0-000

**目标**：任何租户不能读取另一租户任务状态。

**允许修改**

- 任务创建/查询相关代码
- 新 job repository
- 测试

**实施**

- 任务记录包含 `tenant_id`；
- 使用 UUID；
- 查询强制 tenant 条件；
- 不存在和无权访问统一返回 404；
- 日志包含 task_id/tenant_id，用户响应不泄露别的租户。

**验收**

- [ ] A 可读 A；
- [ ] B 读 A 为 404；
- [ ] 未登录为 401/重定向；
- [ ] 重启后任务记录仍存在。

## P0-004 修复点击追踪开放跳转

**依赖**：P0-000

**目标**：追踪端点不能把系统域名用作任意钓鱼跳板。

**实施**

- 创建 tracking 时保存目标 URL 或签名 URL；
- 点击端点仅使用服务端目标；
- 校验 http/https；
- 禁止 localhost、私有网络和非预期 scheme；
- 旧链接提供安全兼容策略。

**验收**

- [ ] 任意 `u` 参数不能控制跳转；
- [ ] 合法追踪链接跳转并计数；
- [ ] 非法目标跳安全页面；
- [ ] 有负向测试。

## P0-005 生产 Cookie、代理和账号状态守卫

**依赖**：P0-000

**目标**：会话策略一致，暂停/过期用户现有 session 也失效。

**实施**

- 统一 session 生命周期；
- production secure cookie；
- 可信代理才读取 forwarded header；
- 每个受保护请求验证租户 status/trial/plan；
- 统一 `tenant_required`；
- 删除重复 `login_required`。

**验收**

- [ ] 暂停后下一请求失效；
- [ ] 过期试用被拦截；
- [ ] 测试环境可用 HTTP；
- [ ] 伪造 X-Forwarded-For 不能绕过关键限流策略。

## P0-006 加密租户秘密字段

**依赖**：P0-000

**目标**：API Key 与 SMTP 密码不再明文落盘。

**实施**

- 建立 SecretStore；
- 支持旧明文读取并在保存时迁移；
- 新写入加密；
- UI 只显示尾四位；
- 支持 key rotation 设计；
- 备份包含密文，不包含解密 key。

**验收**

- [ ] 文件中找不到原始秘密；
- [ ] 重启后可解密；
- [ ] 空提交不清除；
- [ ] 错 key 明确失败；
- [ ] 日志不泄露。

## P0-007 公共 Inbound API 防滥用

**依赖**：P0-002、P0-005

**实施**

- token 轮换；
- 持久化限流或 Redis 限流；
- 可选 allowed origins；
- honeypot；
- 最大 body；
- 字段白名单；
- 幂等键；
- spam 标记；
- CORS 不反射凭据。

**验收**

- [ ] 重复请求不重复入库；
- [ ] 大请求被拒绝；
- [ ] token 无效不泄露租户；
- [ ] 限流重启后仍有效；
- [ ] 可正常从允许站点提交。

---

# 阶段 2：拆分应用入口

## P1-001 引入 Application Factory

**依赖**：全部 P0 安全任务至少 P0-001、002、005

**目标**：可创建 dev/test/prod 多实例 app。

**实施**

- 新建 `app/create_app`；
- 扩展延迟绑定；
- 保留旧 `web.app:app` 兼容导出一段时间；
- 启动命令改用 factory；
- 导入 app 不启动线程。

**验收**

- [ ] 可创建两个不同配置 app；
- [ ] 测试使用临时目录；
- [ ] import 无副作用；
- [ ] 旧 URL smoke test 通过。

## P1-002 拆 Auth Blueprint

**依赖**：P1-001

**迁移**

- login/register/logout；
- verify/resend；
- forgot/reset；
- form；
- auth templates。

**验收**

- [ ] URL 保持；
- [ ] rate limit；
- [ ] CSRF；
- [ ] session fixation 防护；
- [ ] 行为测试。

## P1-003 拆 Tenant/Onboarding Blueprint 与 Service

**依赖**：P1-001

**迁移**

- current tenant context；
- onboarding；
- product profile；
- tenant guard；
- config service。

## P1-004 拆 Leads/CRM Blueprint

**依赖**：P1-001

**迁移**

- 列表、详情、状态、活动、导入/导出；
- 建立 LeadService 和 LeadRepository；
- 状态机集中。

## P1-005 拆 Collection Blueprint

**依赖**：P1-003、P1-004

**迁移**

- collection 页面；
- 新建任务；
- 任务进度；
- 结果进入 review queue；
- 路由不直接 import 每个 collector。

## P1-006 拆 Outreach Blueprint

**依赖**：P1-004

**迁移**

- email template；
- 单封发送；
- sequence；
- tracking；
- unsubscribe；
- suppression；
- deliverability。

## P1-007 拆 Settings Blueprint

**依赖**：P1-003

**迁移**

- 公司、产品、渠道、邮件、用量；
- secret field；
- connection test。

## P1-008 拆 Admin Blueprint

**依赖**：P1-001、P0-001

**实施**

- 普通用户与管理员边界；
- 管理员审计日志；
- 危险操作确认；
- 禁止默认密码；
- 账户、套餐、任务和系统状态。

## P1-009 拆 Billing Blueprint

**依赖**：P1-001

**实施**

- order service；
- payment integration；
- webhook handler；
- idempotency；
- 支付专项测试。

## P1-010 清空旧 app.py 业务

**依赖**：P1-002 至 P1-009

**目标**

旧 `web/app.py` 只保留兼容导入，或删除并更新启动路径。

**验收**

- [ ] URL map 对比通过；
- [ ] 文件不再含业务路由；
- [ ] 完整回归通过。

---

# 阶段 3：任务与调度

## P1-020 建立持久化 Job 模型

**依赖**：P0-003

- jobs schema；
- JobRepository；
- status state machine；
- progress/event；
- idempotency。

## P1-021 迁移采集任务到 Worker

**依赖**：P1-020、P1-005

- Web enqueue；
- worker 运行；
- progress；
- retry；
- cancel；
- 每 provider 独立错误。

## P1-022 迁移自动跟进

**依赖**：P1-020、P1-006

- scheduler 只创建任务；
- worker 发信；
- 幂等；
- quota；
- suppression；
- stopped condition。

## P1-023 迁移竞品监控

**依赖**：P1-020

- 低优先级 maintenance queue；
- provider 失败隔离；
- 每租户配额；
- 可暂停。

## P1-024 迁移备份任务

**依赖**：P1-020

- 独立 scheduler；
- manifest/checksum；
- OSS；
- restore script；
- 告警。

## P1-025 删除 Web 进程线程和内存状态

**依赖**：P1-021 至 P1-024

- 删除 `_task_status`；
- 删除三个 scheduler thread；
- serve 可多进程；
- 无导入副作用。

---

# 阶段 4：数据与集成

## P1-030 建立版本迁移系统

- baseline；
- schema table/Alembic；
- old DB upgrade test；
- backup gate。

## P1-031 建立 Collector Contract

- request/result DTO；
- errors；
- registry；
- config validation；
- fixture contract tests。

## P1-032 迁移 Google Search

## P1-033 迁移 Google Maps

## P1-034 迁移 CSV/XLSX Import

## P1-035 迁移 Enrichment

## P1-036 渠道 Catalog

字段：

```text
key
name
status
recommended_for
requires_key
cost_model
capabilities
risk_level
last_verified_at
help_url
```

## P1-037 冻结不稳定渠道

- 默认关闭；
- 显示 beta/degraded；
- 不进推荐主流程；
- 为后续维护建立 scorecard。

---

# 阶段 5：UI 系统

## P1-040 引入 UI Token 与基础 Layout

- app shell；
- sidebar/topbar；
- CSS token；
- 响应式；
- 无业务改动。

## P1-041 建立 Jinja 组件

- buttons/forms/badges/tables/modal/toast/drawer；
- Story/demo page；
- accessibility。

## P1-042 重做工作台

- 今日行动；
- KPI；
- 最近任务；
- 配置问题；
- 空状态。

## P1-043 重做找客户流程

- 3 个推荐渠道；
- advanced；
- 预估用量；
- JobProgress；
- review queue。

## P1-044 重做线索审核

- quality dimensions；
- keyboard actions；
- merge；
- batch accept。

## P1-045 重做 CRM

- list/Kanban；
- shared filters；
- timeline；
- responsive。

## P1-046 重做设置

- 分组；
- secret field；
- connection test；
- channel health。

---

# 阶段 6：上线

## P1-050 Docker Compose 多进程拓扑

- web/worker/scheduler/redis；
- healthcheck；
- volumes；
- immutable version。

## P1-051 结构化日志与错误追踪

## P1-052 备份恢复演练

## P1-053 staging 与 smoke tests

## P1-054 发布和回滚脚本

## P1-055 首批种子租户灰度

---

# 进入新功能前的 Gate

全部满足：

- [ ] P0 完成；
- [ ] app factory；
- [ ] 认证和租户已拆；
- [ ] job 持久化；
- [ ] 无 Web 后台线程；
- [ ] migrations；
- [ ] 关键测试；
- [ ] UI token；
- [ ] staging；
- [ ] restore drill。

之后才能进入 P2：竞品雷达增强、团队协作、WhatsApp API、更多 BYOK 数据源。
