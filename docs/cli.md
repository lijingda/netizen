# Netizen CLI 安装与维护

本手册描述 [ADR 0076](adr/0076-separate-cli-installations-from-instance-data.md) 的普通
Python 包模型。该改造尚未发布到 PyPI；以下是新版本接口，不代表当前线上旧 Release
已经支持。发布、旧安装手工转换与真实平台验收必须分别执行，不自动发生。

## 安装与首次使用

使用 Python 3.11–3.14；后台服务支持 Linux systemd user 和 macOS GUI LaunchAgent。
Netizen 不下载 Python、不创建隐藏 venv。选择一个长期使用的环境：

```sh
python -m pip install netizen-cli
# 或明确选定普通环境：
uv pip install --python /absolute/path/to/python netizen-cli
# 或交给 uv 管理持久工具环境：
uv tool install netizen-cli
```

不要安装同名的第三方 `netizen` 发行包；本项目发行名为 `netizen-cli`，导入模块为
`netizen_cli`，console command 为 `netizen`。也可用 `/absolute/path/to/python -m netizen_cli`
明确选择安装。遵守所选 Python 的系统环境保护；多个环境可以装不同版本。

```sh
netizen setup --root /absolute/path/to/.netizen
netizen start --root /absolute/path/to/.netizen
netizen status --root /absolute/path/to/.netizen
```

root 选择顺序为 `--root`、`NETIZEN_ROOT`、有效账号的 `~/.netizen`。包安装只安装程序、
依赖和资源，不生成实例、注册服务或迁移数据库。setup 检查共享 Codex 登录、通过官方
飞书浏览器流程准备应用凭据并校验权限，初始化数据、注册服务，但不默认启动。
无 TTY 也可用浏览器完成；agent 应转交验证 URL 并保留同一进程，不索取 App Secret。
凭据留在 `<root>/lark-app/config.json` 的固定 netizen profile；租户审批、发布、安装、
可用范围和机器人入群仍需用户完成。首次可用 `--admin-port` 指定端口。
setup 在外部授权前记录“初始化中”，因此授权失败或取消后可以重试，也可用 remove
--purge 清理已生成的精确文件；不会把已有实例缺库误当作首次创建。
已有服务绑定时，setup 只报告 `already_registered` 和当前状态，保留绑定及自启设置，
不会替用户重新启用或启动服务；运行已有绑定请用同一 root 的 start。如果之前的注册
过程失败，应先检查 status；需要重新注册时，用 remove（不加 --purge）保留数据，
再从目标 Python 环境对同一 root 执行 setup，确认注册成功后显式 start。

start 等待该实例真正 ready，不把 manager 已加载当作成功。每次实际启动（包括系统
自动启动和崩溃重启）都校验环境和数据、按需迁移。数据格式较新、损坏、缺库或缺少
迁移路径时拒绝业务启动，不创建空库；迁移提交后的启动失败不恢复旧数据库。
schema 没变化不会重复迁移或无条件备份。status/help/version 不触发迁移。

## 实例、程序与服务绑定

一份 CLI 安装可以服务多个 root。实例保留配置、凭据、SQLite、日志和恢复材料，程序
与内置 Skills 属于 Python 安装。Project 工作目录不搬动，Codex 登录和用户状态保持
原生共享；默认不为每个实例配置独立 CODEX_HOME。

服务定义固定绝对 Python 入口、环境身份和 canonical root。终端换 venv 不改已有
服务。没有独立实例注册表：服务定义是受管实例清单，管理器实时状态与 lifetime lock／
ready 证据决定运行状态。当前用户权限之外、未知 supervisor 和任意手工进程不在自动
发现保证中。
服务定义按冻结格式校验：Linux 新注册使用 v2，修正 WorkingDirectory 的单路径写法；
macOS 仍为 v1，并保留已有 v1 定义的识别。校验不随当前模板的排版变化而改变；未知、
被修改或被系统管理器拒绝的定义不能猜测接管。Linux 接受父目录符号链接产生的同文件
路径别名，但仍拒绝服务文件本身的符号链接、不同文件和额外覆盖配置。

```sh
netizen stop --root /absolute/path/to/.netizen
netizen restart --root /absolute/path/to/.netizen
netizen logs --root /absolute/path/to/.netizen --lines 100
netizen doctor --root /absolute/path/to/.netizen
```

stop 只在确认退出后成功，并保留绑定；restart 使用绑定环境而非调用者环境。
doctor 为只读诊断，不以另一环境的业务代码迁移运行实例。命令支持 `--json` 结果，
失败报告当前阶段、已知状态、原因和后续建议；未知不能写成已成功。

## 程序更新

```sh
netizen update
```

update 只影响这个命令实际使用的 Python 安装及其关联受管实例，拒绝 --root，
NETIZEN_ROOT 也不缩小范围。普通 pip、uv pip 与 uv tool 采用经过验证的有限识别；
未知／冲突／不支持的安装在停服前报错，允许用户用自己的包工具手工维护。
不根据 PATH 随便调用另一 pip，不试升级来识别环境，不失败后自动换后端。

自动更新的范围小于安装运行的范围：第一版不接管 editable、直接 URL、本地源码、
共享 user-site、pipx 或项目管理器控制的安装。发现自定义包配置文件时提示手工维护，
不解析文件内容后猜测其影响；macOS 的 pip 检查包含 XDG 数据目录和传统／回退目录。
这不等于禁用自定义索引：`PIP_INDEX_URL` 等受支持的环境变量在预检和安装时一致继承，
不将其中的凭据写入更新计划；改变安装目标或不能可靠复现解析条件的选项仍拒绝。
uvx／uv tool run 的缓存环境不支持自更新。
仅 uv tool 的私有 receipt／布局适配限定已验收的 0.12.20；普通 uv pip 按公开查询和
预检能力检查，不一概锁定 uv 版本，也不承诺所有版本均可用。
INSTALLER 只提供默认方式线索；已确认自主管理的普通环境可以显式选择
`--via pip|uv-pip`（pip 必须已存在），不要求永远使用最初的工具。
`--via uv-tool` 同样必须证明工具归属，任何 override 都不能绕过环境归属检查。

1. 核对安装目标、维护方式和完整受影响服务集合。
2. pip／uv pip 可靠预检查确认无变化时直接报告，不停启；预检错误中止。
   uv tool 不模拟等价 dry-run，可能无实际包变化也重启一次。
3. 记录原运行集合，逐个停止并确认退出；macOS 已加载但无 PID 的 job 也先卸载，
   防止 KeepAlive 在替换包期间重新拉起。此类原本未运行的 job 不加入恢复集合。
4. 用对应包工具更新安装；协调器在被替换包外执行，不继续导入旧包代码。
5. 新进程验证安装后，只恢复原运行实例，各实例启动检查／迁移并等待 ready。
6. 报告包更新与逐实例恢复结果。原本停止的实例仍停止。

停止失败立即中止，不自动补偿本轮已经停止的实例；安装状态不明不盲目重启；部分
实例启动失败不回滚其他实例或数据库。报告中保留这些实际结果与修复建议。
预检查与安装不是包索引的原子事务；索引或外部环境在两者之间变化时，仍可能发生
无实际版本变化的停启，不承诺拦截所有外部竞争。
不保证与任意外部 pip／uv 写入或直接 supervisor 启动互斥；更新时不要另外启动关联
实例或改动安装。没有运行时版本轮询，也不强制拦截外部升级。
包替换协调器收到 SIGINT／SIGTERM／SIGHUP 时，先终止并回收自己创建的子进程，
仍持有维护锁；然后报告中断与未知状态，不继续恢复实例。它不扫描或杀死任意进程，
也不保证自身遭 SIGKILL、机器断电或子进程主动脱离进程组后的自动恢复。

手工维护顺序：停止所有使用该环境的实例 → 用自己的包管理器升级 → 显式启动。
绕过此顺序可能同时影响代码、依赖或资源；出错后可能需要修复安装，不保证重启就能解决。
不要从将被停止的 Netizen 服务进程上下文里执行 update，应使用独立终端。

## 移除实例、清理数据、切换环境

```sh
# 默认保留数据：
netizen remove --root /absolute/path/to/.netizen
# 确认清理显示的实例文件；-y 仅省去交互确认：
netizen remove --root /absolute/path/to/.netizen --purge -y
```

两者都先展示准确目录、服务和删除／保留范围，再停止并确认退出、移除服务及自启。
默认保留配置、凭据、数据库和日志；--purge 只删除经过校验的精确实例文件，不递归
删除整个 root／home，不删未知文件、Project 或共享 Codex 数据，也不删云端飞书应用。
清理不可自动恢复，应预先备份。非交互未给 -y 时不默认同意；-y 不能绕过归属与停服
检查。中断后按剩余状态报告，不承诺跨文件删除事务。
若中断已删除数据库或配置，后续清理可能因无法核实 Project 保护范围而拒绝继续；
不能把剩余文件视为已确认可删。保留剩余内容，对照上次删除报告及备份人工核实恢复／
清理范围，不要整目录删除或修改归属标记绕过检查；缺失数据不会自动重建。
归属标记、维护锁和已有 Admin 重启记录保留为安全凭据，防止迟到的维护进程重新操作
已移除实例；它们不是仍在运行的实例。数据库中的 Project 目录同样受保护，无法验证
保护范围时停止清理，不以猜测继续删除。刚发起的 Admin 重启会阻止冲突的 CLI 操作，
待其完成后再试；已中断的重启可由显式 start 恢复并留下真实恢复记录。

**实例控制按 root，程序更新按环境，环境切换必须显式。** B 可控制绑定 A 的实例，
但 start/restart 仍执行 A；B 的 update 不涉及 A。不提供 rebind，切换使用：

```sh
/env-a/bin/python -m netizen_cli remove --root /path/to/.netizen -y
/env-b/bin/python -m netizen_cli start --root /path/to/.netizen
```

切换时不要加 --purge。只有确认无服务绑定、无旧进程占用且保留数据完整，B 的 start
才注册到 B 并执行启动准入。停止的 A 服务仍有绑定；A 环境丢失、manager 查询失败
不能冒充无绑定。注册成功但启动失败会报告实际 B 绑定，不自动切回 A；迁移已提交则
保留新库。两条命令不是一个事务，中间存在未注册／停止状态，start 明确要求启动。

卸载程序交给原包管理器。先处理该环境全部关联服务（包括停止的），remove 或按上述
方式转移，再从准确环境卸载 netizen-cli。原生 pip／uv 卸载不会替 Netizen 停服或清理
服务；程序卸载不删除保留的实例数据、用户 Python 或共享 Codex 状态。

## Admin 与旧安装

Admin 只允许本实例管理与显式重启，不再安装或升级全局程序。重启使用绑定环境中
当前程序，可能触发该实例数据迁移，不承诺任务续跑；程序更新从外部 CLI 执行。

旧 release/current 安装由维护者手工转换：先确认并停止旧服务、备份实例数据，安装
选定 Python 环境中的 CLI，再按新数据归属与初始化证据校验注册服务，启动验证，最后
只清理已确认的旧程序文件。没有自动旧布局转换器；不要直接对旧目录运行 purge，
也不要通过删库或伪造空实例绕过校验。转换操作需另行指定目标和确认，开发不自动执行。
旧部署模块、模板和仅服务旧事务的测试已移除，历史代码可在 Git 历史中查阅。
旧 shell／Python 安装与更新入口只保留拒绝执行和迁移指引；历史 schema 夹具仍保留。

## 验证与发布状态

新增入口须覆盖临时目录、历史 SQLite、fake manager、包工具退出与报告、wheel/sdist
隔离安装，以及源码目录之外的运行。`make check` 是代码门禁；打包测试需要受支持的
构建 Python（可通过 NETIZEN_TEST_BUILD_PYTHON 指定）。Linux/macOS 的真实 service
manager、飞书 browser/ready、实际 SDK Skills 执行和中断恢复仍需要对应实机验收。
未完成实机项不能因 synthetic 测试通过就标为通过；不把已有 release 的证据转移到新 CLI。

2026-09-29 首轮本地实现验收：Linux 开发环境的 `make check` 通过，2613 项测试中 2608
通过、5 项为另一平台专属分支的条件跳过；编译、依赖检查和 SDK synthetic／只读 Skills
discovery 均通过。该次启用了临时环境中的原生 pip／uv 探针，并执行了 wheel、sdist
及源码目录外的隔离安装验证。独立复审发现的 Project／CODEX_HOME 删除保护、日志源和
延迟 Admin 重启冲突已修复。此记录不代表已完成上述实机验收、PyPI 配置或正式发布。

同日独立复审后的修正／删减验收：启用原生 pip／uv 隔离探针重新运行 `make check`，
2431 项测试中 2423 通过、8 项为平台分支条件跳过；编译、依赖检查和全部 SDK 门禁
通过，Skills discovery 不修改用户配置。数量变化包含删除旧部署事务专用测试，并非
删除历史 schema 夹具；当前迁移、发布链、服务控制与 CLI 覆盖保留并补充了回归用例。
真实临时子进程验证更新信号与锁释放顺序；离线 uvx 验证固定版本缓存不被自更新改变；
wheel／sdist 与源码外安装验证通过。交叉复查未发现旧实现残留执行引用。真实
systemd／launchd、新 CLI 的浏览器／完整 Runtime 与正式 PyPI 发布仍未在本轮验收。

同日 Herdr／Traex 复核后的小修验收：补齐 macOS pip 的 XDG 数据目录及回退目录检查，
用真实 pip 26.2.1 的配置发现实现对照验证（模拟 macOS 分支，非 macOS 实机验收）。
新增配置回归用例，强化已有绑定／自启设置不变的覆盖，并完善 setup／purge 恢复指引。
启用原生 pip／uv 探针的 `make check` 再次通过：2435 项中 2427 通过、8 项平台分支
条件跳过，编译、依赖检查和 SDK 门禁全部通过。本轮未操作现有实例或发布程序。

同日 Linux 服务路径小修：修正 WorkingDirectory 字段及合法父目录路径别名识别，
保留冻结 v1 校验并新增 Linux v2 定义。启用原生 pip／uv 探针的 `make check` 通过：
2442 项中 2431 通过、11 项条件跳过，编译、依赖和 SDK 门禁全部通过。原生 systemd
解析及隔离实机验证覆盖基本启停／移除、A/B 环境绑定、缺库拒绝和特殊字符工作目录；
测试服务均已清理。业务 Runtime 使用明确夹具，不代表完整飞书／SDK Runtime、
环境更新故障矩阵、迁移故障或 macOS 已完成验收，也不代表该候选已正式发布。
