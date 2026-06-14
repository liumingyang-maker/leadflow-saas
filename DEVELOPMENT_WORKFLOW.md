# 第六部分：开发流程

## 1. 任务流

```text
需求假设
  ↓
写任务卡
  ↓
确认边界与验收
  ↓
创建分支
  ↓
先补行为测试
  ↓
最小实现
  ↓
lint/test/security
  ↓
人工验收
  ↓
PR
  ↓
预发布
  ↓
小流量上线
  ↓
观察指标
  ↓
合并结论到文档
```

## 2. 任务卡模板

```markdown
# TASK P?-???

## 背景
为什么做，用户或系统遇到了什么问题。

## 目标
本任务完成后可观察到的行为。

## 非目标
明确不在本任务中做什么。

## 允许修改
- path/a.py
- path/b.py
- tests/...

## 禁止修改
- 支付
- 数据模型
- 现有 URL

## 实现约束
- 必须使用某 Service
- 必须保持兼容
- 不访问真实网络

## 验收标准
- [ ] ...
- [ ] ...

## 自动化测试
- ...

## 人工测试
- ...

## 数据与回滚
- 是否迁移
- 回滚方法

## 风险
- ...
```

## 3. 分支策略

保持简单：

- `main`：可部署；
- 每个任务一个短分支；
- PR 合并前 rebase/merge main；
- 禁止直接在 main 开发；
- 生产版本打 `v0.x.y` tag。

## 4. 本地命令

建议 Makefile：

```make
setup:
	python -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

dev:
	flask --app 'app:create_app' run --debug

worker:
	rq worker collection enrichment outreach maintenance --with-scheduler

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest -q

test-cov:
	pytest --cov=app --cov=core --cov-report=term-missing

check: lint test
```

Windows/WSL2 统一使用 WSL2 命令，避免 Windows 和 Linux 路径差异。

## 5. Definition of Done

任务只有全部满足才完成：

- 验收标准全部通过；
- 新行为有测试；
- 旧行为未回归；
- lint/test 通过；
- 没有密钥和真实客户数据；
- 文档已更新；
- UI 有所需状态和截图；
- 迁移已验证；
- 回滚方法明确；
- 生产配置变化已列出；
- 风险和已知限制已说明。

## 6. Code Review 清单

### 架构

- 路由是否过重？
- 业务规则是否进入 Service/Domain？
- 租户是否显式传递？
- 外部集成是否被封装？
- 是否引入新的全局状态？

### 安全

- 是否有 CSRF？
- 是否校验对象所有权？
- 是否可能泄漏密钥？
- 是否信任未验证 header？
- 是否存在开放跳转、路径穿越、SSRF、任意文件上传？
- webhook 是否验签和防重放？

### 数据

- 是否有迁移？
- 是否可重复执行？
- 是否可能丢数据？
- 是否影响旧租户？
- 是否有备份/恢复说明？

### 可运维

- 失败能否重试？
- 日志是否可定位？
- 是否有超时？
- 是否会阻塞 Web worker？
- 是否能健康检查？
- 是否能回滚？

## 7. AI 协作节奏

最安全的节奏：

1. 让 AI 只做审查和测试；
2. 再让 AI 做最小修复；
3. 运行测试；
4. 人工查看 diff；
5. 再进入下一个任务。

不要一次给 Claude/Codex：

> “按文档把整个项目重构完。”

应给：

> “执行 P0-001。只能修改任务允许的文件。先写失败测试，再实现。完成后停止。”

## 8. 决策记录 ADR

影响长期架构的决定写入：

```text
docs/adr/
├─ 0001-keep-flask-jinja.md
├─ 0002-per-tenant-sqlite-now.md
├─ 0003-rq-for-background-jobs.md
├─ 0004-tabler-component-reference.md
└─ 0005-encrypted-tenant-secrets.md
```

ADR 包含：

- 背景；
- 决策；
- 备选；
- 后果；
- 何时重新评估。

## 9. 发布流程

- PR 合并后构建不可变镜像；
- 自动测试镜像启动与迁移；
- 部署到 staging；
- 执行 smoke test；
- 数据备份；
- 小流量或单租户验证；
- 正式发布；
- 观察错误率、任务失败率和登录/支付；
- 异常立即回滚镜像，不现场手改容器。

## 10. 新手操作守则

- 每次改代码前创建 Git 分支；
- 每次让 AI 改之前先 `git status`；
- 修改后先看 `git diff --stat`，再看完整 diff；
- AI 修改文件超过预期时立即停止；
- 不把 `.env` 发给 AI 或提交 GitHub；
- 数据库操作前复制数据目录；
- 不直接在生产服务器编辑源代码；
- 不执行不理解的删除命令；
- 先在本地和 staging 验证。
