# Netizen engineering guardrails

Netizen is a Feishu/Lark Channel for Codex. Per instance, keep one long-lived Python
service, one `FeishuChannel`, one Channel database, and one shared
`AsyncCodex`. The Channel SDK owns messaging; the official `openai-codex`
SDK owns native Threads, Turns, history, tools, configuration, and permissions.

Follow the [Netizen/Codex responsibility boundary](docs/design.md#netizen-与-codex-的职责边界):
Netizen owns SDK requests/results and its resources; Codex owns native execution.
Do not add another runtime, long-lived service, scheduler, history model, or
configuration layer.

Keep durable contributor rules here. Behavior, procedures and compatibility
evidence belong in linked sources; read relevant sections and cited ADRs before
changing those boundaries. Keep feature behavior in design.md; add root navigation
only for new durable contributor guardrails.
The usage guides are not implementation guides.

## Task navigation

### Getting started

- Start with: [README](README.md), [CONTRIBUTING](docs/CONTRIBUTING.md), [overview](docs/design.md#工程概览), [vocabulary](CONTEXT.md).
- Usage: [user skill](skills/netizen-user-guide/SKILL.md) and [user guide](skills/netizen-user-guide/references/user-guide.md).
- Decisions: [status/amendments](docs/adr/README.md), [ADR criteria](docs/adr/README.md#何时写-adr).

### Runtime and sessions

- [Identity](docs/design.md#scopebinding-与会话配置): Scope, Binding and Thread relationships, plus session configuration.
- [Admission](docs/design.md#锁与输入准入): locks and which Turn may accept an input.
- [Observation](docs/design.md#普通-turn-终态与观测恢复): ordinary Turn terminals and bounded observation recovery.
- [Failures](docs/design.md#失败语义): rejected inputs, uncertain effects and operation-specific recovery.
- [SDK adapters](docs/design.md#sdk-适配边界): approved adapters, compatibility gates and removal conditions.
- [Goal](docs/design.md#goal-物理-turn-与逻辑终态): physical Turns, logical completion and rollover.
- [Side](docs/design.md#side-运行与关闭): temporary-topic execution, admission and closure.
- [Subscriptions](docs/design.md#普通会话订阅): ordinary Thread subscription retention and release.
- [Lifecycle/forks](docs/design.md#普通会话生命周期): naming, activation, restoration and persistent forks.
- [Archive/delete](docs/design.md#原生归档与删除): native results, failure handling and cleanup.
- [Auto-creation](docs/design.md#聊天默认配置与自动创建): chat defaults and creation when no current Binding exists.
- [Collaboration](docs/design.md#普通会话协作): native Thread identity, access ranges and ExternalMessage delivery.
- [Directory/history](docs/design.md#工具与可访问目录): session discovery, read/wait tools and cursor pagination.

### Inputs and replies

- [Input preparation](docs/design.md#消息输入准备): provenance, mention catch-up, quotes and images.
- [Attachments](docs/design.md#输入文件附件): input file publication, exact ownership and manual deletion.
- [Questions](docs/design.md#原生问题卡片与回答): native question cards, answer identity and Binding/Side admission.
- [Presentation](docs/design.md#回复与活动展示): Activity, partial answers, Result and Files.
- [Progress settings](docs/design.md#过程卡展示设置): bounded windows, segments, pagination and instance fallback settings.
- [Commands/cards](docs/design.md#飞书命令与控制卡片): slash controls, configuration forms and callbacks.
- [Runtime guidance](docs/design.md#运行环境提示与使用指南的边界): execution hints, user guidance separation and size budgets.

### Management, data and operations

- [SQLite](docs/design.md#channel-数据库与结构校验): what may be stored, and schema validation.
- [Credentials](docs/design.md#飞书应用凭据): application credential source, App identity and repair.
- [Native environment](docs/design.md#原生环境与能力配置): Codex configuration, capabilities and built-in Skill registration.
- [Admin](docs/design.md#管理查询与操作): management operations and administrator identities.
- [Runtime polling](docs/design.md#运行态投影与轮询): displayed runtime state and bounded status polling.
- [Project browsing](docs/design.md#project-文件查看): current-directory file trees, bounded reads and previews.
- [Project deletion](docs/design.md#project-删除与交接): inventory, tombstones and native lifecycle handoffs.
- [Worktrees](docs/design.md#受管-worktree-与实际目录): resource ownership, actual working directories and optional deletion cascades.
- [Schedules](docs/design.md#定时任务): plans, dispatch, ordinary topics, MCP and saved-plan manual runs.
- [Existing-Binding inputs](docs/design.md#原会话目标): ordinary input delivery, Run receipts and lifecycle suspension.
- Operations: [CLI](docs/cli.md) and [deployment](docs/deployment.md).
- [Setup relay](docs/deployment.md#agent-驱动首次安装): Agent installation alternatives, verification URLs and troubleshooting.
- [Multi-instance/Skills gates](docs/deployment.md#多实例与内置-skills-验收): platform acceptance and live Skills validation before rollout.

## Architecture

- The in-process Admin Web is a management-only adapter over the shared
  application/runtime boundary (ADR 0031). It manages existing sessions but does
  not create ordinary Bindings; interactive session creation belongs to Feishu.
  The one-shot Admin deployment process for explicit instance restarts is the sole
  deployment exception (ADR 0057/0059/0076). Admin cannot upgrade the shared program.
- The sole in-process Scheduler uses the shared application/runtime boundary. Admin
  may maintain Scheduled Plans and request manual runs of their saved versions,
  but cannot submit arbitrary immediate Prompts (ADR 0061/0068).
  A scheduled plan suspended because its target session's lifecycle changed keeps
  its manual enablement unchanged.
- Project browsing must use current-directory identity, bounded descriptor-relative
  reads and inert previews, remain separate from exact-Turn Files, and expose no
  download endpoint.
- Preserve exact Scope/Binding-to-native-Thread identity. Existing running input
  steers the exact Turn; it is never queued or converted into another Turn.
  Only the verified session-tool entry uses native ExternalMessage start/join
  (ADR 0081); it does not change ordinary input admission. Human messages,
  question-card answers and scheduled inputs retain their exact-Turn admission.
  Collaboration tools use native Thread IDs and respect this instance's configured
  access ranges.
  Goal rollover never redirects a prepared input to another physical Turn.
  Unknown side effects fail closed at the documented operation-specific scope.
  Question answers use the admission rules of the Binding or Side that the original
  question card targets.
- Keep `/new`, input admission and Project deletion independent of automatic
  session creation.
- Different Bindings may run concurrently in the same actual working directory.
  Keep Project identity, its default cwd and the selected managed Worktree separate.
  A materialized Thread gets its cwd from Codex, never an implicit Project fallback.
  Do not add a global semaphore, Project lock, workspace copy, per-Binding
  `CODEX_HOME`, or cross-Thread execution limit.
- A worktree is not owned by any one session. Keep deletion cascades optional.
  Manage only worktrees created by this instance's shared service, never external
  App/CLI worktrees.
- Channel SQLite may store only the metadata and narrow exceptions explicitly listed
  in the [current data contract](docs/design.md#channel-数据库与结构校验).
  Do not persist Git state or duplicate native cwd authority. Never store prompts
  beyond those exceptions, message bodies, responses, Turn history/activity, card
  sessions, effective Codex configuration, or Admin sessions/tokens/indexes/audit records.
  Side routes never store native Thread IDs.
- Use exact-pinned official SDKs and public high-level APIs. Only the approved
  [narrow adapters](docs/design.md#sdk-适配边界) may cross that boundary.
  Do not add a generic/private RPC gateway, parse CLI output, patch SDK internals,
  copy protocol models, or signal arbitrary processes. New gaps require an accepted
  ADR and a removal trigger. The separate narrow Feishu Admin OAuth/ticket HTTP
  exception and its SDK replacement trigger remain defined in
  [ADR 0080](docs/adr/0080-authenticate-admins-with-feishu-identities.md).
- Preserve each adapter's documented gate: cleanup and Activity retain exact
  version/fingerprint checks; the other adapters use capability shape,
  synthetic, and live harnesses. Delete also requires disposable live coverage
  and exact native-response handling (ADR 0078), without catalog reconciliation.
  Model upgrades do not replace these gates.
- Every materialized persisted non-ephemeral Thread retains archive/delete
  controls. Delegate shutdown to App Server after reserving exact lifecycle
  intent and releasing Binding/Scope locks; do not pre-interrupt, cleanup, or
  wait for idle. Preserve bounded observation/reconciliation and Binding-local
  unknown states (ADR 0037/0049); never fabricate a terminal.
- Feishu app availability and chat membership govern Channel admission, with optional
  instance group access under [ADR 0092](docs/adr/0092-restrict-execution-to-enabled-group-chats-on-demand.md).
  Direct chats and their topics remain unaffected. Instance Administrators have
  separate, equal Admin Web authority. In Feishu they only add the ADR 0092 `/enable`
  group command; this does not introduce multi-role RBAC or Project ACLs.
- Native Codex controls model, tools, Skills, MCP, sandboxing, and environment
  policy except the documented Binding
  Model/Effort/Speed intent, non-login tool boundary, and dedicated temporary
  Scheduler/session collaboration MCP server entries (ADR 0061/0081).
  These public process overrides must not write user configuration, replace user
  MCP entries, or override developer/base instructions. Fresh Threads use the public SDK's `auto_review` default;
  persistent forks retain native inherited permissions without client overrides.
- Message-history access remains the narrow, public, typed, read-only
  `lark-oapi` port using the same app credentials (ADR 0039). Preserve chat-main
  versus thread-topic semantics, inert historical context, and its rollout gate.
- Validate exact local ownership of input attachments. Keep manual deletion
  independent of whether native execution accepted the input. Do not add attachment
  receipt locks or infer attachment history.
- Display is best effort and never changes native execution. Keep Reply Cards
  within the closed Activity/Partial Answer/Result/Files set. Goal management cards
  are separate. Never expose reasoning or raw tool arguments/output. Files use exact
  Turn evidence, not workspace scans.
  Partial Answers use the existing observation/consumption chain independently of
  Progress Card, never signal completion, and never create database answer history.
  Preserve Goal's four-proof completion and exact final-Turn Result/Files handoff contract,
  including confirmed explicit clear while the current Turn completes (ADR 0084).
- Resolve a missing progress setting from the instance fallback only when capturing
  settings for execution; never persist the resolved fallback into a Binding,
  default rule or plan. Preserve all historical progress settings as explicit choices.
- Unsupported native capabilities remain explicit gaps. Keep product non-goals
  in [design.md](docs/design.md#目标与边界); do not simulate them with prompts or
  local state.
- Keep runtime Agent guidance separate from user guidance. Follow its documented
  description/body boundary and size budgets before adding execution hints.

## Deployment boundaries

- Select one canonical root by `--root` > `NETIZEN_ROOT` > the effective user's
  `~/.netizen`; derive exact systemd user unit/LaunchAgent and maintenance job
  names from that root (ADR 0074). Keep standard shared Codex state. Do not add a registry,
  cross-instance manager, XDG profiles, LaunchDaemon, root helper, persistent
  Netizen environment file, or PATH snapshot.
  Reload the exported interactive-login-shell environment at each start; Codex
  tool shells must not replace it (ADR 0022/0023). Reassert the captured root
  after shell reload, validate derived paths before writes, and retain it for tools.
- The fixed `netizen` profile in `<root>/lark-app/config.json` is the sole
  application credential source (ADR 0066). Share its documented Lark CLI file
  format without adding a CLI installation/runtime dependency. Preserve exact-App
  repair versus whole-file deletion for rebinding and atomic credential writes.
- Programs belong to the selected Python environment; instance roots contain data,
  not release/venv/current trees. Explicit setup initializes new data. Every actual
  service start validates and migrates under the lifetime lock before Runtime admission
  (ADR 0076). Preserve v14 baseline and frozen migration steps/fixtures. Missing data
  never means a new instance. SQL failures before commit roll back; failures after
  commit retain the new database, never automatically restore an old snapshot.
  Restore CLOEXEC before Codex children start. Loaded/active is never a substitute
  for the private ready marker.
- Built-in Skills belong to the physical installed package. Register their root on
  the same initialized App Server before catalog/Thread access; fail startup if
  registration fails. Never install, snapshot, roll back, or uninstall global user
  Skills/Codex state. Preserve root ownership markers and unrelated content; never
  recursively remove an instance root. Do not add legacy-layout or global-Skill migration.
- Bind each Admin independently. Only an absent port permits bounded allocation;
  retain the actual listener and atomically save its port before readiness. An
  explicit/persisted port never drifts. `/admin` reports this instance's live URLs;
  Cookie names and the visible root identify the instance without a shared gateway.
- The maintainer chooses formal release timing; nothing auto-releases on main or tag
  pushes. `netizen update` uses only its selected environment, rejects --root, records
  the running set, confirms stop before package replacement, validates with a new
  process, then restores only that set. Stop failure aborts without compensation.
  Unknown installation state never triggers blind restart. Do not add a runtime
  version guard, separate instance registry, cross-instance rollback, or hidden
  package-manager fallback.
  Service definitions are the inventory; finite package-layout adapters must fail closed.
- Control is root-scoped, update is environment-scoped. Existing bindings never follow
  the calling venv. Explicit switching is remove (retain data) then target-environment
  start; do not add a rebind command. Removal displays exact scope; `-y` skips confirmation only.
  `--purge` deletes only verified instance-owned files, never Projects, Worktree contents,
  or a whole root.
- For routine Agent installation, start with README and setup output. Install the
  ordinary netizen-cli package with the user's package manager, then run netizen
  setup and start. The package install does not configure or start services.
  Public setup can perform initial browser setup and exact-App repair without a TTY
  (ADR 0062); Admin restart never performs browser authorization.
  Relay any verification URL to the user while retaining the same setup
  process and reading its stderr progress; credentials are saved privately.
  Never request an App Secret in chat. Other interaction modes are documented
  in the deployment handoff procedure.
  Package update and per-instance ready results are separate evidence; do not report
  a zero package-manager exit as proof every instance recovered. Native uninstall is
  not intercepted; remove/transfer all service bindings before uninstalling the package.
- No default remote target is defined. For operations, read ignored
  `LOCAL_ENVIRONMENT.md` when present; otherwise use an explicit target. Never
  copy its coordinates into tracked files/artifacts or treat it as runtime config.

## Verification

- Scale planning, tests, and review to the change. Preserve behavior coverage;
  avoid tests that lock documentation wording or source layout.
  For asynchronous progress, prefer bounded condition waits or explicit synchronization
  over fixed sleeps to avoid flaky tests under slow CI scheduling.
- For every Codex SDK or bundled App Server upgrade, complete the
  [SDK upgrade review](docs/deployment.md#codex-sdk-升级审查): compare the full API and
  behavior changes, reassess adapters and workarounds, classify new capabilities,
  and report migration decisions and evidence beyond automated test results.
- Use `make check` for the repository gate; it includes ruff and mypy. `CodexRuntime`
  and `ChannelApplication` are one-way layer chains enforced by mypy; follow the
  [static checks](docs/CONTRIBUTING.md#静态检查与类型债务) rules and never grow the typing
  debt list. Run affected live phases only under
  the [documented triggers](docs/deployment.md#代码门禁与按需实时兼容性验证).
  Formal Releases reuse successful CI for the exact main commit; package changes
  require isolated wheel/sdist validation. Synthetic tests do not replace platform/live
  acceptance; historical release-installer evidence does not certify the new CLI.
- Read the relevant acceptance gates before deployment changes. Before rollout, read
  the multi-instance/Skills gates and report platform and live validation separately.
- Review the final diff and report checks, results, and material verification gaps.
