# 第五部分：GitHub 可复用项目

原则：**借鉴成熟边界与组件，不整仓复制。**

## 1. 推荐直接使用

### Flask-WTF

用途：

- 全局 CSRF；
- 表单；
- 文件上传校验；
- 可选 reCAPTCHA。

用法：作为 Flask extension 初始化，默认保护浏览器写请求。

### RQ

用途：

- 采集任务；
- 补充任务；
- 邮件发送；
- 自动跟进；
- 定期雷达；
- 维护任务。

原因：相对 Celery 简单，适合当前 Python/Flask 和小团队。

### Alembic

用途：

- schema 版本；
- SQLite batch migration；
- 中期 PostgreSQL 迁移。

不要一次性把所有 SQL 改成 ORM；先管理迁移，再逐步迁移 Repository。

### Ruff

用途：

- lint；
- import 排序；
- format；
- CI 检查。

使用一个工具减少新手配置复杂度。

### pytest

用途：

- 单元、集成、租户隔离、迁移和路由测试。

### pre-commit

用途：

- 提交前运行 Ruff、基础文件检查、密钥检查。

## 2. 推荐借鉴 UI

### Tabler

适合：

- Dashboard；
- 表格；
- 卡片；
- 表单；
- Badge；
- 导航；
- 响应式布局。

使用方式：

- 选择组件和基础 CSS；
- 建立 LeadFlow 自己的 token 和 Jinja macro；
- 不直接复制整个 demo 项目；
- 不把业务模板绑死到 Tabler 的构建系统。

## 3. 推荐借鉴项目结构

### cookiecutter-flask

借鉴：

- application factory；
- Blueprint；
- config 分层；
- tests；
- assets；
- commands；
- extensions 初始化。

不要直接把现有项目强行套入模板，也不要一次性替换现有认证和数据库。

## 4. 可选工具

- `pip-audit`：Python 依赖漏洞；
- `detect-secrets` 或 `gitleaks`：密钥扫描；
- `Sentry SDK`：错误追踪；
- `structlog`：结构化日志；
- `Playwright`：关键 E2E；
- `factory_boy`：测试数据；
- `freezegun`：试用期、任务和时间测试；
- `responses` 或 `respx`：HTTP mock；
- `Fernet`：早期租户秘密字段加密。

每个工具必须先通过任务卡批准，不一次性全装。

## 5. 不建议当前引入

- 大型 SaaS boilerplate；
- React/Next.js 全栈模板；
- Kubernetes；
- 微服务脚手架；
- Kafka；
- Airflow；
- 完整 CRM 开源项目改造；
- 将多个 GPL 项目代码直接复制进商业 SaaS。

## 6. 复用审查清单

使用任何 GitHub 项目前检查：

- 许可证是否允许商业使用；
- 最近维护情况；
- 安全策略；
- 依赖数量；
- 能否局部使用；
- 是否与 Flask/Jinja 兼容；
- 能否测试；
- 替换成本；
- 是否会迫使全项目重写；
- 是否需要保留版权声明。

在 `THIRD_PARTY_NOTICES.md` 记录使用的项目、版本、许可证和用途。
