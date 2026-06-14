# 第二部分：技术架构

## 1. 迁移原则

- 保留 Python、Flask、Jinja；
- 先模块化，后决定是否换数据库或前端；
- 保持旧 URL 和旧数据兼容；
- 每次迁移一个 Blueprint；
- 先写行为测试，再搬实现；
- 新代码不得继续添加到 `web/app.py`；
- 旧代码只允许修复 P0 问题，业务新增写入新结构。

## 2. 目标分层

```text
HTTP / CLI / Job
       ↓
Blueprint / Command Handler
       ↓
Application Service
       ↓
Domain Rules
       ↓
Repository / Integration
       ↓
SQLite/PostgreSQL / External API / SMTP
```

### Blueprint

负责：

- 参数解析；
- 鉴权和权限；
- 调 Service；
- HTTP 状态码；
- 渲染模板或 JSON。

禁止：

- 大段业务流程；
- 直接创建第三方 SDK；
- 直接拼 SQL；
- 创建后台线程；
- 直接读取全局租户路径。

### Service

负责：

- 用例编排；
- 事务边界；
- 套餐与额度规则；
- 调用 Repository 和 Integration；
- 输出明确 DTO。

Service 不依赖 `request`、`session`、`flash`、`g`。

### Domain

负责：

- 状态转换；
- 去重规则；
- 评分规则；
- 套餐权限；
- 任务状态机；
- 邮件序列停止条件。

### Repository

负责：

- 数据读写；
- 租户隔离；
- 数据映射；
- 事务；
- 迁移后的兼容层。

每个租户数据方法必须显式接受 `tenant_id`，不得依赖隐式全局变量。

### Integration

负责：

- 第三方 API；
- HTTP 超时和重试；
- 认证；
- 响应标准化；
- 错误分类；
- 调用量记录。

Integration 不写业务数据库。

### Job

负责：

- 从队列取任务；
- 建立应用上下文；
- 调用 Service；
- 更新进度；
- 捕获异常；
- 重试和死信。

Job 不复制 Web 路由中的业务代码。

---

## 3. 目标目录

```text
app/
├─ __init__.py                 # create_app
├─ config.py                   # Development/Test/Production
├─ extensions.py               # csrf, limiter, login, db, cache
├─ security.py                 # headers, tenant guard, proxy handling
├─ errors.py
├─ logging.py
├─ blueprints/
│  ├─ auth/
│  │  ├─ routes.py
│  │  ├─ forms.py
│  │  └─ templates/auth/
│  ├─ onboarding/
│  ├─ leads/
│  ├─ collection/
│  ├─ outreach/
│  ├─ inbound/
│  ├─ radar/
│  ├─ billing/
│  ├─ settings/
│  └─ admin/
├─ application/
│  ├─ dto.py
│  ├─ services/
│  └─ policies/
├─ domain/
│  ├─ tenants/
│  ├─ leads/
│  ├─ collection/
│  ├─ outreach/
│  ├─ jobs/
│  └─ billing/
├─ repositories/
│  ├─ tenant_repository.py
│  ├─ lead_repository.py
│  ├─ job_repository.py
│  └─ order_repository.py
├─ integrations/
│  ├─ base.py
│  ├─ registry.py
│  ├─ serper/
│  ├─ google_maps/
│  ├─ apollo/
│  ├─ importyeti/
│  ├─ email/
│  └─ payment/
├─ jobs/
│  ├─ collection.py
│  ├─ followups.py
│  ├─ radar.py
│  └─ backup.py
├─ templates/
└─ static/
```

## 4. Flask 初始化

使用应用工厂：

```python
def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(resolve_config(config_name))

    init_extensions(app)
    register_blueprints(app)
    register_error_handlers(app)
    register_security_hooks(app)
    register_cli(app)

    return app
```

扩展对象在模块级创建但不绑定：

```python
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)

def init_extensions(app: Flask) -> None:
    csrf.init_app(app)
    limiter.init_app(app)
```

## 5. 配置

### 配置分层

```python
class BaseConfig:
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024

class ProductionConfig(BaseConfig):
    SESSION_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"

class TestConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
```

### 必需环境变量

```text
APP_ENV
SECRET_KEY
DATA_DIR
SITE_URL
ADMIN_BOOTSTRAP_EMAIL
ADMIN_BOOTSTRAP_PASSWORD
FERNET_KEY
DATABASE_URL                # 切 PostgreSQL 时使用
REDIS_URL                   # 使用 RQ 时使用
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASS
```

生产启动执行配置校验，关键变量缺失时直接失败，而不是使用弱默认值。

## 6. 多租户设计

### 近期方案：保留每租户 SQLite

优点：

- 不破坏现有数据；
- 易备份与搬迁；
- 对早期少量客户足够。

必须补强：

- 所有数据访问通过 Repository；
- 路径由 `TenantStorage` 统一生成；
- 严格验证 tenant ID 格式；
- 不允许用户输入直接参与文件路径；
- 每次任务记录租户；
- 自动化测试验证 A 租户不能读取 B 租户数据；
- 备份包含 manifest 和校验和。

### 中期方案：PostgreSQL

满足以下任一条件再迁移：

- 付费租户持续超过 20–50；
- 需要多个 Web 实例；
- SQLite 写锁或备份开始影响业务；
- 需要复杂团队权限、搜索或分析；
- 单租户数据明显增大。

迁移方式：

- 新表全部包含 `tenant_id`；
- Repository 默认强制 tenant 条件；
- 可使用 PostgreSQL Row Level Security 作为第二道保护；
- 双写/校验/切读，避免一次性停机迁移。

## 7. 数据迁移

### 立即引入迁移目录

```text
migrations/
├─ 0001_baseline.py
├─ 0002_jobs_table.py
├─ 0003_tracking_target_url.py
└─ 0004_encrypted_secrets.py
```

每个迁移定义：

- `upgrade(connection)`；
- `downgrade(connection)`，不安全时明确标注；
- `verify(connection)`；
- 迁移前备份要求；
- 对旧数据的默认值；
- 可重复执行保证。

推荐逐步引入 SQLAlchemy + Alembic，但不要为了使用 ORM 一次性重写所有查询。先让 Alembic 管迁移，再逐个 Repository 迁移。

## 8. 任务系统

### 推荐：RQ + Redis/Valkey

队列：

```text
collection    外部采集，耗时较长
enrichment    邮箱与公司补充
outreach      邮件发送和后续跟进
maintenance   雷达、清理、备份
```

任务模型：

```text
id
tenant_id
type
status
progress
input_json
result_json
error_code
error_message
created_at
started_at
finished_at
attempt
idempotency_key
```

状态：

```text
queued → running → succeeded
                 ↘ failed → queued(retry)
queued/running → cancelled
```

### 无 Redis 的过渡方案

建立 `jobs` 数据表和单独 `worker.py`：

- Web 只插入 queued 任务；
- worker 通过原子更新领取任务；
- 使用 `locked_at`、`locked_by` 和超时回收；
- scheduler 单独进程创建到期任务；
- 禁止在 Web 进程内启动线程。

## 9. 安全架构

### 会话与权限

- 用户、管理员使用不同 Blueprint 和权限模型；
- 管理员 session 可使用独立 cookie 名或独立子域；
- 管理操作写审计日志；
- 敏感操作二次确认；
- 账号暂停/过期在每次受保护请求检查；
- 退出登录使用 POST。

### CSRF

默认保护所有浏览器会话写操作。仅豁免：

- 支付 webhook；
- 邮件服务 webhook；
- 独立站询盘 API。

豁免接口必须具备至少一种：

- HMAC 签名；
- 随机 token；
- 时间戳与防重放；
- 幂等键；
- 来源域名白名单；
- 持久化限流。

### 公共接口

- 限制 Content-Type；
- 限制请求大小；
- 严格字段白名单；
- honeypot；
- 每租户和每 IP 限流；
- 幂等键；
- 日志中脱敏；
- 不把内部错误返回给调用方。

### 密钥

敏感字段定义统一 schema：

```python
SECRET_FIELDS = {
    "smtp_pass",
    "serper_api_key",
    "deepseek_api_key",
    "anthropic_api_key",
    "apollo_api_key",
    "hunter_api_key",
}
```

存储前加密，展示时只显示尾四位，更新时空值代表保持不变。

## 10. 外部集成规范

统一错误：

```python
class IntegrationError(Exception): ...
class IntegrationAuthError(IntegrationError): ...
class IntegrationQuotaError(IntegrationError): ...
class IntegrationRateLimitError(IntegrationError): ...
class IntegrationTimeoutError(IntegrationError): ...
class IntegrationResponseError(IntegrationError): ...
```

每次调用记录：

- provider；
- operation；
- tenant_id；
- duration_ms；
- status；
- request_count；
- result_count；
- error_type；
- retry_count；
- cost estimate。

禁止把完整请求、邮箱正文、密码和 API Key写入日志。

## 11. 测试架构

```text
tests/
├─ unit/
│  ├─ domain/
│  ├─ services/
│  └─ integrations/
├─ integration/
│  ├─ repositories/
│  ├─ auth/
│  ├─ tenant_isolation/
│  └─ migrations/
├─ contract/
│  └─ collectors/
├─ e2e/
│  └─ core_flow/
├─ fixtures/
└─ conftest.py
```

最低测试：

- 注册、验证、登录、退出；
- 试用过期和暂停；
- 租户隔离；
- CSRF；
- 任务权限；
- 上传限制；
- 线索去重；
- 状态转换；
- 退订；
- 支付回调签名与幂等；
- 任务重试；
- 备份恢复；
- 每个采集器的标准响应 contract。

外部 API 测试使用固定 fixture，不访问真实网络。

## 12. 可观测性

必须增加：

- JSON 结构化日志；
- request_id、tenant_id、job_id；
- `/health/live`；
- `/health/ready`；
- 错误追踪；
- 任务成功率与耗时；
- 第三方渠道错误率；
- 邮件发送/退订/投诉指标；
- 磁盘和备份状态。

## 13. 部署拓扑

### 开发

```text
web + worker + scheduler + redis
SQLite data volume
Mail/API mocks
```

### 小规模生产

```text
Nginx/Caddy
   ↓
Web container
Worker container
Scheduler container
Redis/Valkey
Persistent data volume
Object storage backup
```

Web、worker、scheduler 使用同一个代码镜像，不同启动命令。

### 扩展后

```text
Load balancer
Web replicas
Worker pools by queue
Managed PostgreSQL
Managed Redis
Object storage
Central logs/metrics
```
