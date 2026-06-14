# 第三部分：UI 系统

## 1. UI 策略

保留服务端 Jinja，不做前端框架重写。使用统一的 Bootstrap 5 / Tabler 类组件体系，建立自己的 token 和组件，不直接复制整套模板。

目标：

- 新手一眼知道下一步；
- 减少渠道和 API Key 带来的认知负担；
- 所有任务、错误、额度和数据质量状态表达一致；
- 移动端能查看和处理关键任务；
- 页面代码不再各写一套样式。

## 2. 信息架构

### 主导航

```text
工作台
找客户
审核线索
客户 CRM
触达
分析
设置
```

管理员后台不出现在普通用户导航中。

### 二级结构

**找客户**

- 新建搜索
- 导入文件
- 任务记录
- 数据源

**触达**

- 待联系
- 邮件模板
- 跟进序列
- 送达率
- 抑制名单

**设置**

- 公司与产品
- 数据源
- 邮箱与 WhatsApp
- 团队
- 套餐与用量
- 询盘插件
- 安全

## 3. App Shell

```text
┌────────────────────────────────────────────┐
│ 顶栏：页面标题 / 全局搜索 / 任务 / 帮助 / 用户 │
├───────────┬────────────────────────────────┤
│ 左侧导航   │ 页面内容                        │
│ 240px     │ max-width 1440px               │
│           │                                │
└───────────┴────────────────────────────────┘
```

原则：

- 左侧导航最多 7 个一级项；
- 当前页面明确高亮；
- 顶栏显示正在运行的任务；
- 通知只显示需要行动的事项；
- 不长期占用右侧信息栏，详情使用 Drawer。

## 4. 设计 Token

```css
:root {
  --lf-space-1: 4px;
  --lf-space-2: 8px;
  --lf-space-3: 12px;
  --lf-space-4: 16px;
  --lf-space-5: 24px;
  --lf-space-6: 32px;
  --lf-space-7: 48px;

  --lf-radius-sm: 6px;
  --lf-radius-md: 10px;
  --lf-radius-lg: 14px;

  --lf-font-xs: 12px;
  --lf-font-sm: 14px;
  --lf-font-md: 16px;
  --lf-font-lg: 20px;
  --lf-font-xl: 28px;

  --lf-content-max: 1440px;
  --lf-sidebar-width: 240px;
}
```

颜色只通过语义 token 使用：

```text
primary
neutral
success
warning
danger
info
```

不要在模板中散落十六进制颜色。

## 5. 核心组件

必须先建立以下 Jinja macro/partial：

- `app_shell`
- `page_header`
- `breadcrumb`
- `button`
- `status_badge`
- `kpi_card`
- `empty_state`
- `alert`
- `toast`
- `modal`
- `confirm_dialog`
- `drawer`
- `filter_bar`
- `data_table`
- `pagination`
- `form_field`
- `secret_field`
- `channel_card`
- `job_progress`
- `usage_meter`
- `quality_score`
- `timeline`
- `skeleton`

目录：

```text
templates/components/
├─ _buttons.html
├─ _forms.html
├─ _badges.html
├─ _tables.html
├─ _jobs.html
├─ _channels.html
├─ _empty_states.html
└─ _dialogs.html
```

## 6. 状态语言

### 任务状态

| 内部状态 | 中文 | UI |
|---|---|---|
| queued | 等待中 | 中性 |
| running | 进行中 | 蓝色 + 进度 |
| succeeded | 已完成 | 绿色 |
| partial | 部分完成 | 黄色 |
| failed | 失败 | 红色 + 重试 |
| cancelled | 已取消 | 灰色 |

### 数据源状态

| 状态 | 含义 |
|---|---|
| ready | 已配置且最近检测正常 |
| setup_required | 需要配置 |
| limited | 可用但额度或能力受限 |
| degraded | 最近错误率高 |
| beta | 实验功能 |
| disabled | 已关闭 |

不要只用“免费/付费”表达渠道状态。

### 线索质量

拆成三个维度：

- 匹配度；
- 数据完整度；
- 联系方式可信度。

不要只显示一个神秘的“AI 分数”。

## 7. 关键页面规范

### 工作台

顶部：

- 有效线索；
- 待审核；
- 待跟进；
- 本月用量。

主体：

1. 今日行动；
2. 最近任务；
3. 漏斗简图；
4. 配置问题。

没有数据时显示引导动作，而不是空图表。

### 找客户

第一屏只显示：

- 产品画像摘要；
- 目标国家；
- 推荐的 3 个渠道；
- 预计用量；
- “开始查找”。

高级数据源折叠到“更多数据源”。

### 渠道卡片

每张卡片必须显示：

- 渠道名称；
- 适合找什么；
- 当前状态；
- 是否需要 Key；
- 成本/额度；
- 数据更新时间；
- 风险或限制；
- 配置/测试按钮。

第三方价格、条数等信息来自渠道 catalog，不写死在 HTML。

### 审核线索

桌面端表格，移动端卡片。

默认列：

- 公司；
- 国家；
- 匹配原因；
- 来源；
- 网站；
- 联系方式；
- 数据质量；
- 操作。

支持快速键：

- A 接受；
- X 忽略；
- M 合并；
- R 研究。

### CRM

首版提供 List 和 Kanban 两种视图，但共用状态与筛选逻辑。不要分别实现两套数据逻辑。

### 设置

使用左侧二级导航或顶部 tabs。秘密字段：

- 默认不可见；
- 显示“已配置 ····abcd”；
- 空提交不清除；
- 修改需明确确认；
- 提供“测试连接”。

## 8. 文案规范

- 面向用户说业务结果，不说实现细节；
- “Serper API”可在高级配置里出现，主流程说“网页搜索数据源”；
- 不使用无法验证的绝对宣传，如“无限量”“95%准确”“10倍回复率”；
- 错误信息包含：发生了什么、用户能做什么、是否会丢数据；
- 不把 Python 异常直接展示给用户；
- 危险操作用具体对象名确认。

示例：

错误：
> `KeyError: smtp_pass`

正确：
> 邮箱连接失败。请检查 SMTP 授权码，原有线索和草稿未受影响。

## 9. 响应式

- 1280px 以上：侧边栏 + 完整表格；
- 768–1279px：可折叠侧栏，次要列隐藏；
- 小于 768px：底部或抽屉导航，表格转卡片；
- 所有主要按钮最小点击区域 44px；
- 不允许横向滚动成为主要操作方式。

## 10. 可访问性

- 输入框有 label；
- 图标按钮有 aria-label；
- 不只靠颜色表达状态；
- 键盘可完成核心流程；
- Modal 能正确锁定焦点；
- 表单错误与字段关联；
- 对比度满足 WCAG AA；
- 动画尊重 `prefers-reduced-motion`。

## 11. UI 开发门禁

每个页面 PR 必须提供：

- 桌面截图；
- 375px 移动截图；
- 空状态；
- 加载状态；
- 错误状态；
- 权限不足状态；
- 与设计 token 的一致性；
- 不存在页面内大段重复 CSS。
