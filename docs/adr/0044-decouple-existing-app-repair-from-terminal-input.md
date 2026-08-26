---
status: accepted
date: 2026-08-26
amends: 0033, 0035
related: 0041
---

# 将已有飞书应用补权与终端输入解耦

ADR 0033/0035 用 stdin 是否连接 TTY 同时判断安装器能否提示凭据和能否运行官方
`register_app`。但 exact-App 修复只通过 callback 把验证 URL/二维码写到 stderr，在最多
660 秒内等待飞书页面确认，并在成功后把凭据 JSON 写入父进程私有 stdout pipe；它不读取
stdin。TTY 因而是手工输入能力的边界，不是官方浏览器补权能力的边界。

## 决定

1. 安装开始时已有完整 App ID/Secret，且有效 tenant scope 校验发现缺失项时，无论 stdin
   是否为 TTY，候选 release 都只对 exact App ID 运行一次官方 `register_app` 修复流程。
   验证 URL 和二维码继续进入 stderr，Secret 继续只由父安装器捕获并原子写入受保护文件。
2. 修复完成后只重新查询一次有效 tenant scope。全部授权后才能准备 host 和进入 activation；
   二次查询仍缺失、查询不可验证、流程失败或 660 秒超时都在旧服务停止和 `current` 切换前
   退出。安装器不循环 device flow，也不调用 `scope.apply`。
3. 本轮刚完成首次凭据初始化或 App Binding 重置时仍不立即打开第二次修复流程。无 TTY 且
   凭据不完整的首次安装继续生成受保护的配置/凭据骨架并退出；安装方式菜单、手工 App ID/
   Secret、linger 和旧 system service 等真正需要输入的步骤仍由 TTY 门禁。
4. device flow 的页面确认只完成官方应用配置请求，不冒充租户管理员审批、应用版本发布、
   租户安装、可用范围或机器人入群。二次有效权限查询是唯一 activation 判据；外部步骤尚未
   生效时，用户完成后重跑同一安装入口。

## 后果

Agent/CI 对已有完整凭据的升级不再因缺少 TTY 而跳过官方修复，但可能在原本立即失败的位置
有界等待最多 660 秒。能保留进程并转交 stderr 的调用方可以把 URL/二维码交给用户，而不需要
可写 stdin；无人观察的调用最终超时且不影响旧服务。官方修复成功返回的新 Secret 仍是持久
配置意图，即使后续有效权限查询尚未通过；这延续 ADR 0035 的两阶段凭据/activation 语义。
