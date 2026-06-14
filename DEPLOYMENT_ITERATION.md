# 第七部分：上线与迭代

## 1. 上线前门槛

### 安全

- [ ] 无固定默认管理员密码
- [ ] 管理员首次创建走 CLI/环境变量
- [ ] CSRF 默认启用
- [ ] 公共接口已列出豁免原因
- [ ] Cookie Secure/HttpOnly/SameSite 正确
- [ ] 可信代理配置正确
- [ ] API Key 加密
- [ ] 任务和对象具备租户校验
- [ ] 点击追踪不是开放跳转
- [ ] 上传大小和类型受限
- [ ] 支付回调验签、验金额、幂等
- [ ] 密钥扫描通过

### 稳定性

- [ ] Web 进程无后台调度线程
- [ ] 任务持久化
- [ ] worker 可重启恢复
- [ ] 所有外部调用有超时
- [ ] 失败任务可重试或人工重跑
- [ ] 迁移版本化
- [ ] 备份可恢复
- [ ] 健康检查存在
- [ ] 磁盘空间告警存在

### 质量

- [ ] 核心测试通过
- [ ] 租户隔离测试通过
- [ ] staging smoke test
- [ ] 关键页面移动端可用
- [ ] 隐私、条款、退订流程可用
- [ ] 第三方数据源限制已明确展示

## 2. 推荐部署

### docker-compose（小规模）

```yaml
services:
  web:
    image: leadflow:${APP_VERSION}
    command: waitress-serve --call app:create_app
    env_file: .env.prod
    volumes:
      - leadflow_data:/data
    depends_on:
      - redis

  worker:
    image: leadflow:${APP_VERSION}
    command: rq worker collection enrichment outreach maintenance
    env_file: .env.prod
    volumes:
      - leadflow_data:/data
    depends_on:
      - redis

  scheduler:
    image: leadflow:${APP_VERSION}
    command: rqscheduler
    env_file: .env.prod
    volumes:
      - leadflow_data:/data
    depends_on:
      - redis

  redis:
    image: valkey/valkey:stable
    volumes:
      - redis_data:/data

volumes:
  leadflow_data:
  redis_data:
```

生产镜像要固定具体版本，不长期使用 `latest`/`stable` 漂移标签。

## 3. 数据备份

### 3-2-1

- 3 份数据；
- 2 种介质；
- 1 份异地。

### 备份内容

- admin 数据库；
- 所有租户数据库；
- 加密后的租户配置；
- 上传文件；
- 迁移版本；
- manifest；
- SHA-256 校验和；
- 应用版本。

### 恢复演练

每月至少一次：

1. 新建空环境；
2. 恢复备份；
3. 启动同版本应用；
4. 验证租户数量；
5. 验证线索数量；
6. 验证登录；
7. 验证一条任务；
8. 验证升级迁移；
9. 记录 RTO/RPO。

不能只看“备份文件存在”。

## 4. 监控指标

### 系统

- 请求量、5xx、P95 延迟；
- CPU、内存、磁盘；
- SQLite 锁错误；
- worker 存活；
- queue 长度；
- 最老任务等待时间；
- 备份成功与最后时间。

### 业务

- 新租户；
- 完成入驻；
- 启动搜索；
- 有效线索；
- 审核接受率；
- 邮件发送、失败、退订；
- 任务按 provider 成功率；
- 支付创建、回调和开通。

### 告警

- 5xx 突增；
- worker 全部离线；
- queue 堆积；
- 备份超过 26 小时未成功；
- 磁盘超过 80%；
- 某 provider 连续失败；
- 支付验签失败异常增加；
- 邮件退订/投诉异常。

## 5. 发布策略

### 版本号

- patch：修复，无 schema 破坏；
- minor：向后兼容功能；
- major：不兼容变化。

### 每次发布

1. 锁定提交；
2. CI；
3. 构建镜像；
4. 生成变更日志；
5. 数据备份；
6. staging 迁移；
7. smoke test；
8. 正式迁移；
9. 部署；
10. 观察 30–60 分钟关键指标；
11. 记录发布结果。

## 6. 回滚

应用与数据库分开考虑：

- 应用回滚：部署上一镜像；
- schema 向后兼容：先扩展、后迁移数据、最后收缩；
- 不在同一版本立即删除旧列；
- 不可逆迁移前生成专项备份；
- 队列任务包含代码版本，避免旧 worker 消费不兼容任务。

## 7. 迭代方式

每两周一个小周期：

### 第 1 周

- 查看漏斗和错误；
- 访谈 3–5 个用户；
- 选择一个最大阻塞；
- 实现并灰度。

### 第 2 周

- 观察使用；
- 修复；
- 更新文档；
- 决定保留、调整或移除。

不要以“本周新增多少页面/渠道”为指标。以激活、有效线索和稳定性为准。

## 8. 渠道治理

每个数据源有 scorecard：

```text
成功率
平均耗时
有效线索率
联系方式覆盖
单次成本
条款/合规风险
维护成本
最近验证时间
```

连续两周低于阈值的渠道：

- 标记 degraded；
- 默认不推荐；
- 修复或下线；
- 不让失败渠道影响主流程。

## 9. 正式版发布建议

不要在完成 P0 前公开销售为成熟 SaaS。可以先以：

- 邀请制；
- 5–10 个种子客户；
- 每个租户限量；
- 人工客服支持；
- 实验渠道明确标记；

进行验证。

达到以下目标后再扩大：

- 连续 30 天无严重租户隔离/数据丢失事故；
- 任务成功率达到设定阈值；
- 备份恢复演练通过；
- 核心流程有自动化测试；
- 至少 5 个真实用户重复使用核心闭环；
- 有清晰的渠道成本和毛利模型。
