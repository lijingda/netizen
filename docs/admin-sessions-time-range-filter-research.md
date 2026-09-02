# Admin Sessions 时间范围筛选研究与设计建议

日期：2026-09-02

## 研究范围

本文比较通用设计系统与监控/分析产品的官方时间范围选择模式，并将结论映射到 Netizen Admin 的 Sessions 和 Side Topics 筛选器。当前实现以本文的 P0 交互与时间边界为验收基线。

## 改造前实现与问题

改造前 Admin 把 `createdFrom` 和 `createdBefore` 暴露为两个独立文本框，要求管理员直接输入 ISO-8601。筛选栏使用自动适配网格，因此起止字段可能跨行分离。

这带来四类问题：

1. 暴露了 API 传输格式，而不是让用户选择时间。
2. 起点和终点缺少“同一个范围”的视觉与语义关联。
3. 没有说明时区、上下界是否包含，也没有就地校验。
4. 缺少常用范围、当前选择摘要和快速清除。

本地实现还有两项必须保留的事实：

- 数据库存储 UTC ISO-8601 时间，带 `+00:00` 偏移和微秒。
- 查询区间是 `[createdFrom, createdBefore)`：起点包含、终点不包含。

## 主流产品模式

### 通用设计系统

- [Ant Design DatePicker](https://ant.design/components/date-picker/) 将 RangePicker 作为一个复合字段，支持范围、预设、清除、时间精度和确认交互。
- [MUI Date Range Picker](https://mui.com/x/react-date-pickers/date-range-picker/) 把桌面和移动端呈现分开，并提供 [shortcuts](https://mui.com/x/react-date-pickers/shortcuts/)、[timezone](https://mui.com/x/react-date-pickers/timezone/)、[validation](https://mui.com/x/react-date-pickers/validation/)、[accessibility](https://mui.com/x/react-date-pickers/accessibility/) 与 [lifecycle](https://mui.com/x/react-date-pickers/lifecycle/) 的独立契约。
- [Carbon Date Picker](https://carbondesignsystem.com/components/date-picker/usage/) 的范围模式保留可见的开始和结束标签，同时用同一个日历完成选择；其 [accessibility guidance](https://carbondesignsystem.com/components/date-picker/accessibility/) 强调键盘、标签和错误反馈。

这些设计系统的共同点不是“只能显示一个输入框”，而是把开始、结束、日历、清除和校验组织成一个逻辑完整的 range field。

### 监控与分析产品

- [Grafana dashboard time range](https://grafana.com/docs/grafana/latest/visualizations/dashboards/use-dashboards/#set-dashboard-time-range) 折叠显示当前范围，展开后区分快捷相对时间和绝对时间，并让时区成为明确概念。
- [Kibana time filter](https://www.elastic.co/docs/explore-analyze/query-filter/filtering#time-filter) 提供常用时间、最近使用和自定义起止时间，确认后才更新结果。

这类产品最接近 Admin 的排查场景：用户往往先选择“最近 24 小时 / 7 天”，必要时再进入精确范围，而不是先手写两个时间戳。

### 可访问性基线

[WAI-ARIA Date Picker Dialog](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/examples/datepicker-dialog/) 给出的关键基线包括：有名称的对话框、完整键盘导航、Escape 关闭、关闭后把焦点还给触发器，以及清晰播报当前状态。

## 推荐方案

### 1. 收起态：一个“创建时间”复合筛选器

筛选栏只保留一个字段：

```text
创建时间
[ 日历  全部时间                                      ▾ ]
```

选中后显示人类可读摘要，而不是 ISO 字符串：

```text
[ 日历  最近 7 天 · 至 09-02 16:20 · UTC+08:00       × ▾ ]
```

自定义日期范围可显示为：

```text
[ 日历  2026-08-27 — 2026-09-02 · UTC+08:00          × ▾ ]
```

`×` 只在有时间条件时出现，用于清除时间条件；整个控件作为一个网格项，不允许起止字段分别换行。

### 2. 展开态：快捷范围 + 自定义范围

桌面端使用锚定在字段下方的弹层：

```text
┌──────────────────┬─────────────────────────────────────┐
│ 不限时间         │ 自定义范围                          │
│ 今天             │ 开始  [2026-08-27] [00:00]         │
│ 昨天             │ 结束  [2026-09-02] [23:59]         │
│ 最近 24 小时     │                                     │
│ 最近 7 天        │ 时区  Asia/Shanghai (UTC+08:00)     │
│ 最近 30 天       │                清除   取消   完成    │
└──────────────────┴─────────────────────────────────────┘
```

推荐默认预设：

- 不限时间
- 今天
- 昨天
- 最近 24 小时
- 最近 7 天
- 最近 30 天

“今天”是本地日历日；“最近 24 小时”是滚动窗口。名称必须保留这个差异，不能都写成模糊的“一天”。

### 3. 第一版的日期时间输入

当前 Admin 是无前端框架的原生 HTML/CSS/JavaScript。第一版建议在弹层内使用两个 `datetime-local` 输入，外加预设和统一摘要，不自行实现日历网格。

这样可以先解决信息架构、时区、校验和响应式问题，也不会为一个筛选器引入整套 UI 框架。若未来 Admin 引入统一组件库，可把弹层内部替换为双月日历；外部交互与 API 映射无需改变。

如果实际使用证明分钟级选择很少，可把自定义模式默认简化为日期范围，把“精确到时间”作为展开项；不要同时在筛选栏常驻四个日期/时间输入。

### 4. 确认模型

当前页面已有全局“筛选”按钮，因此弹层按钮应叫“完成”，不叫“应用筛选”：

- “完成”把弹层草稿写回创建时间字段，但不请求服务端。
- “取消”丢弃本次弹层修改。
- “清除”移除弹层草稿。
- 页面“筛选”一次性提交全部筛选条件。

这样避免连续编辑日期时反复查询，也避免出现两个都叫“应用”的按钮。选择预设后可以立即完成并关闭弹层，但仍由页面“筛选”发起请求。

### 5. 时区与后端映射

控件明确显示浏览器解析出的 IANA 时区和当前偏移，例如 `Asia/Shanghai (UTC+08:00)`。提交时统一转换为后端的 canonical UTC ISO-8601（固定六位微秒并使用 `+00:00`），继续使用现有参数名：

- `createdFrom`
- `createdBefore`

日期级自定义范围对用户应当“两端日期均包含”。例如管理员在 `Asia/Shanghai` 选择 `2026-08-27 — 2026-09-02`，前端应提交：

```text
createdFrom=2026-08-26T16:00:00.000000+00:00
createdBefore=2026-09-02T16:00:00.000000+00:00
```

也就是把结束日期转换为当地“下一日 00:00”的 exclusive upper bound，才能包含 9 月 2 日全天。

快捷范围中的“最近 24 小时 / 7 天 / 30 天”使用当前时刻作为 `createdBefore`，按持续时长计算 `createdFrom`；“今天 / 昨天”按所示时区的日历边界计算。

当前 SQLite 条件直接比较时间文本，因此不能把 JavaScript `toISOString()` 产生的 `Z` 字符串原样交给现有查询。后端参数形状无需变化，但实现时应先严格解析，再统一为与存储值相同的 canonical UTC 表示，避免继续按任意用户字符串做词典序比较。至少校验：

- 两个值都是带时区的有效 ISO-8601；
- `createdFrom < createdBefore`；
- 本地时间在 DST 切换处真实存在且不歧义；
- 标准化为 UTC 后再进入查询与 cursor fingerprint。

### 6. 响应式与可访问性

- 桌面端用双栏弹层；窄屏改为全宽模态或底部面板，内容纵向排列。
- 触发器使用真实 `button`，并暴露展开状态。
- 弹层使用有名称的 dialog/popover；Escape 等同取消，关闭后恢复触发器焦点。
- “开始”和“结束”保留可见标签，格式说明不能只依赖 placeholder。
- 错误落在对应输入下方，配合 `aria-invalid` 和 `aria-describedby`；无效范围时禁用“完成”。
- 键盘用户可完成打开、编辑、清除、取消和确认全流程。

## 建议的落地优先级

### P0

1. 合并为单个创建时间复合字段。
2. 增加预设、自定义起止、清除/取消/完成。
3. 明示浏览器时区并统一输出 UTC。
4. 正确映射 `[from, before)`，补前后端校验。
5. 同步应用到 Sessions 和 Side Topics，避免两个页面再次分叉。

### P1

1. 若实际需要，再加入最近使用范围。
2. 若 Admin 引入统一组件库，再升级为双月日历。
3. 结合整个筛选栏改版增加“重置全部”和已生效条件摘要。

## 非目标

- 不改变数据库的 UTC 存储格式。
- 不改变现有 API 参数名称或 keyset pagination 模型。
- 不为当前页面手写完整日历网格。
- 不把时区选择扩展成新的实例配置层；第一版只显示并使用浏览器时区。

## 方案验收点

- 未选择时显示“全部时间”，不改变当前默认查询结果。
- 用户无需看见或输入 ISO-8601。
- 起止值在视觉、语义和响应式布局中始终属于同一个字段。
- 预设与自定义范围都能稳定转换为 UTC `[from, before)` 查询。
- 选择同一天不会漏掉当天后续记录。
- 无效范围不能提交，并能明确指出错误位置。
- 桌面、窄屏和纯键盘操作均可完成筛选。
