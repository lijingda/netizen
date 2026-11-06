# Netizen engineering guardrails

Keep durable contributor rules here. Behavior, procedures, and compatibility
evidence belong in the linked source documents; read the relevant sections and
their cited ADRs before changing that boundary.

## Task navigation

- Start with [README](README.md), [CONTRIBUTING](docs/CONTRIBUTING.md), [overview](docs/design.md#工程概览),
  and [vocabulary](CONTEXT.md). For usage consult [the skill](skills/netizen-user-guide/SKILL.md) and
  [user guide](skills/netizen-user-guide/references/user-guide.md); they are not implementation guides.
- Runtime: [identity](docs/design.md#scopebinding-与会话配置), [admission](docs/design.md#锁与输入准入),
  [observation](docs/design.md#普通-turn-终态与观测恢复), [failures](docs/design.md#失败语义), [SDK adapters](docs/design.md#sdk-适配边界).
  [Goal](docs/design.md#goal-物理-turn-与逻辑终态) retains exact physical-Turn admission across rollover;
  Ordinary/Side/Goal steer and verified ordinary-resume rejections preserve separate cancellation and Goal receipt-identity protections.
  [Side](docs/design.md#side-运行与关闭), [subscriptions](docs/design.md#普通会话订阅), and
  [lifecycle/forks](docs/design.md#普通会话生命周期) preserve exact native results and Binding-local restore uncertainty;
  [removal](docs/design.md#原生归档与删除) and background naming retain their separate failure/cleanup boundaries.
- [Missing-current creation](docs/design.md#聊天默认配置与自动创建) uses chat defaults and the original message Scope;
  keep `/new`, input admission and Project deletion independent of those rules.
- [Collaboration](docs/design.md#普通会话协作): native Thread IDs, instance tool ranges and narrow ExternalMessage start/join;
  preserve human/card/scheduled exact-Turn admission and [directory/read/wait pagination](docs/design.md#工具与可访问目录).
- [Input preparation](docs/design.md#消息输入准备) owns provenance, mention catch-up, quotes and images.
  [Attachments](docs/design.md#输入文件附件): complete publication, exact local ownership and manual deletion independent
  of native acceptance; no receipt locks or inferred attachment history. [Questions](docs/design.md#原生问题卡片与回答)
  use target-specific Binding/Side admission; preserve exact identity, context, display and file-evidence contracts.
- [Presentation](docs/design.md#回复与活动展示): stable partial answers retain Goal rollover fragments.
  [Progress settings](docs/design.md#过程卡展示设置): independent bounded windows, preserved segments, self-contained pagination;
  resolve missing intent from instance fallback only at execution capture, preserve explicit historical values, never persist the fallback.
  [Commands/cards](docs/design.md#飞书命令与控制卡片) own controls, forms and callbacks.
- [SQLite](docs/design.md#channel-数据库与结构校验), [credentials](docs/design.md#飞书应用凭据), and
  [native environment](docs/design.md#原生环境与能力配置) own data, schema, App source and configuration boundaries.
- [Admin](docs/design.md#管理查询与操作): Feishu administrator identities and [runtime polling](docs/design.md#运行态投影与轮询).
  [Project browsing](docs/design.md#project-文件查看): current-directory identity, bounded descriptor-relative reads, inert previews,
  no download endpoint; keep it separate from exact-Turn Files. [Project deletion](docs/design.md#project-删除与交接)
  preserves exact inventory, tombstones and lifecycle boundaries.
- [Worktrees](docs/design.md#受管-worktree-与实际目录): independent ownership, actual cwd and optional cascade;
  Git owns live filesystem facts; manage only resources created by the shared service, never external App/CLI worktrees.
- [Schedules](docs/design.md#定时任务) own plans, dispatch, ordinary topics, MCP management and saved-plan manual runs.
  [Existing-Binding inputs](docs/design.md#原会话目标) retain ordinary start/steer admission/feedback, input-receipt Runs,
  and lifecycle suspension independent of manual enablement.
- [CLI](docs/cli.md) and [deployment](docs/deployment.md): environment-scoped packages replace per-instance releases.
  Routine Agent installation starts with README and setup output; use [relay guidance](docs/deployment.md#agent-驱动首次安装)
  for alternatives/troubleshooting. Read relevant acceptance gates before deployment changes and
  [multi-instance/Skills gates](docs/deployment.md#多实例与内置-skills-验收) with separate platform/live status before rollout.
- [Runtime Agent guidance](docs/design.md#运行环境提示与使用指南的边界) stays separate from user guidance;
  follow its description/body boundary and size budgets before adding execution hints.
- [ADR index](docs/adr/README.md) owns status/amendment chains and [admission](docs/adr/README.md#何时写-adr).
  Feature behavior belongs in design.md; add root navigation only for new durable contributor guardrails.

## Architecture

- Netizen is a Feishu/Lark Channel for Codex. Per instance, keep one long-lived Python
  service, one `FeishuChannel`, one Channel database, and one shared
  `AsyncCodex`. The Channel SDK owns messaging; the official `openai-codex`
  SDK owns native Threads, Turns, history, tools, configuration, and permissions.
- Follow the [Netizen/Codex responsibility boundary](docs/design.md#netizen-与-codex-的职责边界):
  Netizen owns SDK requests/results and its resources; Codex owns native execution.
- The in-process Admin Web is a management-only adapter over that same
  application/runtime boundary (ADR 0031). It manages existing sessions but does
  not create ordinary Bindings; interactive session creation belongs to Feishu.
  The one-shot Admin deployment process
  for explicit instance restarts is the sole deployment exception
  (ADR 0057/0059/0076); Admin cannot upgrade the shared program. The sole in-process Scheduler shares that boundary; Admin
  may maintain Scheduled Plans and request manual runs of their saved versions,
  but cannot submit arbitrary immediate Prompts (ADR 0061/0068).
  Do not add another runtime, long-lived service, scheduler, history model, or
  configuration layer.
- Preserve exact Scope/Binding-to-native-Thread identity. Existing running input
  steers the exact Turn; it is never queued or converted into another Turn.
  Only the verified session-tool entry uses native ExternalMessage start/join
  (ADR 0081); it does not change ordinary input admission.
  Unknown side effects fail closed at the documented operation-specific scope.
- Different Bindings may run concurrently in the same actual working directory.
  Project identity/default cwd and the selected managed Worktree are separate;
  materialized Thread cwd comes from Codex, never an implicit Project fallback.
  Do not add a global semaphore, Project lock, workspace copy, per-Binding
  `CODEX_HOME`, or cross-Thread execution limit.
- Channel SQLite owns only the documented Scope/Binding/Project metadata,
  schema version, deduplication TTL keys, explicit Binding choices/revisions,
  and Side routes/tombstones. ADR 0061/0070 narrowly add current Scheduled Plan
  instructions, minimal dispatch/Run metadata, exact initial or accepted physical Turn references,
  and bounded management-request deduplication.
  ADR 0073 also permits chat default rules, explicit creation settings and ordering
  revisions, independent of Project lifecycle. ADR 0085 adds managed Worktree
  identity/creation receipts, Project registration generation, and Binding working-cwd
  choices/associations; do not persist Git state or duplicate native cwd authority.
  Never store other prompts,
  message bodies, responses, Turn history/activity, card sessions, effective
  Codex configuration, or Admin sessions/tokens/indexes/audit records.
  Side routes never store native Thread IDs.
- Use exact-pinned official SDKs and public high-level APIs. Approved narrow
  adapters are terminal cleanup (ADR 0009), Goal/Skills (0014), Side boundary
  (0021), Thread unsubscribe (0028), Thread Delete (0037), and non-consuming
  Activity observation (0020/0052), process-local Skill roots (0074), and read-only
  account rate limits ([0077](docs/adr/0077-read-account-rate-limits-on-demand.md)),
  and fixed read-only history pages ([0083](docs/adr/0083-page-native-session-history-through-a-narrow-read-adapter.md)).
  Do not add a generic/private RPC gateway,
  parse CLI output, patch SDK internals, copy protocol models, or signal
  arbitrary processes. New gaps require an accepted ADR and a removal trigger.
  The narrow Feishu Admin OAuth/ticket HTTP exception and its SDK replacement
  trigger are defined in [ADR 0080](docs/adr/0080-authenticate-admins-with-feishu-identities.md).
- Preserve each adapter's documented gate: cleanup and Activity retain exact
  version/fingerprint checks; the other adapters use capability shape,
  synthetic, and live harnesses. Delete also requires disposable live coverage
  and exact native-response handling (ADR 0078), without catalog reconciliation.
  Model upgrades do not replace these gates.
- Message-history access remains the narrow, public, typed, read-only
  `lark-oapi` port using the same app credentials (ADR 0039). Preserve chat-main
  versus thread-topic semantics, inert historical context, and its rollout gate.
- Display is best effort and never changes native execution. Keep Reply Cards
  within the closed Activity/Partial Answer/Result/Files set; Goal management cards are separate; never expose reasoning or raw
  tool arguments/output. Files use exact Turn evidence, not workspace scans.
  Partial Answers use the existing observation/consumption chain independently of
  Progress Card, never signal completion, and never create database answer history.
  Preserve Goal's four-proof completion and exact final-Turn Result/Files handoff contract,
  including confirmed explicit clear while the current Turn completes (ADR 0084).
- Every materialized persisted non-ephemeral Thread retains archive/delete
  controls. Delegate shutdown to App Server after reserving exact lifecycle
  intent and releasing Binding/Scope locks; do not pre-interrupt, cleanup, or
  wait for idle. Preserve bounded observation/reconciliation and Binding-local
  unknown states (ADR 0037/0049); never fabricate a terminal.
- Feishu app availability and chat membership govern Channel admission, with optional
  instance group access under [ADR 0092](docs/adr/0092-restrict-execution-to-enabled-group-chats-on-demand.md).
  Direct chats and their topics remain unaffected. Instance Administrators are separate,
  equal Admin Web authorities (in Feishu they only add the ADR 0092 `/enable` group command),
  not multi-role RBAC or Project ACLs. Native Codex controls model, tools,
  Skills, MCP, sandboxing, and environment policy except the documented Binding
  Model/Effort/Speed intent, non-login tool boundary, and dedicated temporary
  Scheduler/session collaboration MCP server entries (ADR 0061/0081). These public process overrides must not
  write user configuration, replace user MCP entries, or override developer/base
  instructions. Fresh Threads use the public SDK's `auto_review` default;
  persistent forks retain native inherited permissions without client overrides.
- Unsupported native capabilities remain explicit gaps. Keep product non-goals
  in [design.md](docs/design.md#目标与边界); do not simulate them with prompts or
  local state.

## Deployment boundaries

- Select one canonical root by `--root` > `NETIZEN_ROOT` > the effective user's
  `~/.netizen`; derive exact systemd user unit/LaunchAgent and maintenance job
  names from that root (ADR 0074). Keep standard shared Codex state. No registry,
  cross-instance manager, XDG profiles,
  LaunchDaemon, root helper, persistent Netizen environment file, or PATH snapshot.
  Reload the exported interactive-login-shell environment at each start; Codex
  tool shells must not replace it (ADR 0022/0023). Reassert the captured root
  after shell reload, validate derived paths before writes, and retain it for tools.
- The fixed `netizen` profile in `<root>/lark-app/config.json` is the sole
  application credential source (ADR 0066). Share its documented Lark CLI file
  format without adding a CLI installation/runtime dependency. Preserve exact-App
  repair versus whole-file deletion for rebinding and atomic credential writes.
- Programs belong to the selected Python environment; instance roots contain data,
  not release/venv/current trees. Explicit setup initializes new data, every actual
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
  recursively remove an instance root. No legacy-layout or global-Skill migration.
- Bind each Admin independently. Only an absent port permits bounded allocation;
  retain the actual listener and atomically save its port before readiness. An
  explicit/persisted port never drifts. `/admin` reports this instance's live URLs;
  Cookie names and the visible root identify the instance without a shared gateway.
- The maintainer chooses formal release timing; nothing auto-releases on main or tag
  pushes. `netizen update` uses only its selected environment, rejects --root, records
  the running set, confirms stop before package replacement, validates with a new
  process, then restores only that set. Stop failure aborts without compensation;
  unknown installation state never triggers blind restart. No runtime version guard,
  separate instance registry, cross-instance rollback, or hidden package-manager fallback.
  Service definitions are the inventory; finite package-layout adapters must fail closed.
- Control is root-scoped, update is environment-scoped. Existing bindings never follow
  the calling venv. Explicit switching is remove (retain data) then target-environment
  start; no rebind command. Removal displays exact scope; -y skips confirmation only.
  --purge deletes only verified instance-owned files, never Projects, Worktree contents,
  or a whole root.
- Install the ordinary netizen-cli package with the user's package manager, then run
  netizen setup and start. The package install does not configure or start services.
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
- Review the final diff and report checks, results, and material verification gaps.
