# Netizen engineering guardrails

Netizen is a Feishu/Lark Channel for native agents. Each instance has one long-lived
Python service, one `FeishuChannel`, one Channel database, and one backend fixed at setup.
Codex is the only supported backend. Its Bindings share one official `AsyncCodex` client.

The Channel SDK owns messaging. ADR 0107 permits three typed Feishu process-message
writes through the instance's pinned `lark-oapi` client. Netizen owns SDK requests,
result delivery and its resources. The selected agent owns native execution, history, tools, configuration
and permissions. Keep a single runtime per instance, with no second service,
scheduler, history model or configuration layer. See the
[responsibility boundary](docs/design.md#netizen-与原生-agent-的职责边界).

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

- [Backend contract](docs/design.md#后端契约): core native facts, capability modules and how a module is added (ADR 0111).
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
- [Project browsing](docs/design.md#project-文件查看): current-directory file trees, bounded reads and previews; [HEAD diffs](docs/design.md#project-未提交变更).
- [Project deletion](docs/design.md#project-删除与交接): inventory, tombstones and native lifecycle handoffs.
- [Worktrees](docs/design.md#受管-worktree-与实际目录): resource ownership, actual working directories and optional deletion cascades.
- [Schedules](docs/design.md#定时任务): plans, dispatch, ordinary topics, MCP and saved-plan manual runs.
- [Existing-Binding inputs](docs/design.md#原会话目标): ordinary input delivery, Run receipts and lifecycle suspension.
- Operations: [CLI](docs/cli.md) and [deployment](docs/deployment.md).
- [Setup relay](docs/deployment.md#agent-驱动首次安装): Agent installation alternatives, verification URLs and troubleshooting.
- [Multi-instance/Skills gates](docs/deployment.md#多实例与内置-skills-验收): platform acceptance and live Skills validation before rollout.

## Architecture

- Every backend meets the [backend contract](docs/design.md#后端契约) (ADR 0111): the
  core native facts plus only the capability modules it declares. Each capability item
  is either implemented through its module port or closed at every entry. Shared
  execution, eligibility and delivery logic never branches on backend identity or grows
  parallel paths; existing direct Codex calls move behind ports as ADR 0111 §6 describes.
  The shared presentation event vocabulary and its single delivery policy change only
  by ADR. Guarantees rest on native facts pinned by gates, never on measured timing.
  New modules are defined in Netizen terms so any backend, including Codex, can
  implement them.
- Delegate native-owned semantics (ADR 0112). First check host-owned state (owner
  gate, capability items, Scope/Binding identity, captured version, cursor, stop,
  lifecycle and unknown occupancy) and reject before writing if any fails. Then, for
  native-owned behavior such as busy handling, configuration validity or permissions,
  send the request and map the native response. Pre-reject only when a native response
  could not prove what happened.
- The in-process Admin Web is a management-only adapter over the shared
  application/runtime boundary (ADR 0031). It manages existing sessions but does
  not create ordinary Bindings; interactive session creation belongs to Feishu.
  The one-shot Admin deployment process for explicit instance restarts is the sole
  deployment exception (ADR 0057/0059/0076). Admin cannot upgrade the shared program.
  Batch archive and delete apply only to explicitly selected rows on the current
  page, sequentially through existing exact single-item endpoints (ADR 0100);
  arbitrary filter results, cross-page selection and server-side batches remain excluded.
- The sole in-process Scheduler uses the shared application/runtime boundary. Admin
  may maintain Scheduled Plans and request manual runs of their saved versions,
  but cannot submit arbitrary immediate Prompts (ADR 0061/0068).
  A scheduled plan suspended because its target session's lifecycle changed keeps
  its manual enablement unchanged.
- Project browsing must use current-directory identity, bounded descriptor-relative
  reads and inert previews, remain separate from exact-Turn Files, and expose no
  download endpoint. Answer-link directory roots (ADR 0098) follow the same read rules;
  Admin file viewing reaches whatever the service account can read, so do not add
  secret-path denylists. Its HEAD diff (ADR 0095) persists no Git state and runs only the
  isolated read-only Git commands with lazy fetch and transports disabled; viewing
  must never execute repository-configured programs.
- Preserve exact Scope/Binding-to-native-session identity. Never queue an input
  or convert it into a later Turn. On Codex, running input steers the exact Turn.
  Stop the exact current Turn.
  Only the verified session-tool entry uses native ExternalMessage start/join
  (ADR 0081); it does not change ordinary input admission. Human messages,
  question-card answers and scheduled inputs retain their exact-Turn admission.
  Collaboration tools use native Thread IDs and respect this instance's configured
  access ranges.
  Goal rollover never redirects a prepared input to another physical Turn.
  Unknown side effects fail closed at the documented operation-specific scope.
  Structured native provider rejections prove non-delivery and reject only that operation (ADR 0093).
  Question answers use the admission rules of the Binding or Side that the original
  question card targets.
- Keep `/new`, input admission and Project deletion independent of automatic
  session creation.
- Different Bindings may run concurrently in the same actual working directory.
  Keep Project identity, its default cwd and the selected managed Worktree separate.
  Resolve a materialized session's cwd from its native backend. Do not add a global
  semaphore, Project lock, workspace copy or cross-session execution limit. Codex
  Bindings share the standard `CODEX_HOME`.
- A worktree is not owned by any one session. Keep deletion cascades optional.
  Manage only worktrees created by this instance's shared service, never external
  App/CLI worktrees.
- Channel SQLite may store only the metadata and narrow exceptions explicitly listed
  in the [current data contract](docs/design.md#channel-数据库与结构校验), including the
  scheduled topic Run self-assessment enum (ADR 0102).
  Do not persist Git state or duplicate native cwd authority. Never store prompts
  beyond those exceptions, message bodies, responses, Turn history/activity, card
  sessions, effective agent configuration, or Admin sessions/tokens/indexes/audit records.
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
- Every materialized persisted non-ephemeral Codex Thread retains archive/delete
  controls. Delegate shutdown to App Server after reserving exact lifecycle
  intent and releasing Binding/Scope locks; do not pre-interrupt, cleanup, or
  wait for idle. Preserve bounded observation/reconciliation and Binding-local
  unknown states (ADR 0037/0049); never fabricate a terminal.
- Feishu app availability and chat membership govern Channel admission, with optional
  instance group access under [ADR 0092](docs/adr/0092-restrict-execution-to-enabled-group-chats-on-demand.md).
  The group switch leaves direct chats and their topics unaffected. Instance
  Administrators have equal Admin Web authority. Their only additional Feishu command
  is `/enable` in groups. Keep multi-role RBAC and Project ACLs out of this boundary.
- On Codex instances, Codex controls model, tools, Skills, MCP, sandboxing, and
  environment policy except the documented Binding Model/Effort/Speed intent,
  non-login tool boundary, and dedicated temporary
  Scheduler/session collaboration MCP server entries (ADR 0061/0081) and the
  Thread-level entry for a scheduled new-topic Run (ADR 0102).
  These public overrides must not write user configuration, replace user
  MCP entries, or override developer/base instructions. Fresh Codex Threads use the public SDK's `auto_review` default;
  persistent forks retain native inherited permissions without client overrides.
- Message-history access remains the narrow, public, typed, read-only
  `lark-oapi` port using the same app credentials (ADR 0039). Preserve chat-main
  versus thread-topic semantics, inert historical context, and its rollout gate.
- Validate exact local ownership of input attachments. Keep manual deletion
  independent of whether native execution accepted the input. Do not add attachment
  receipt locks or infer attachment history.
- Display is best effort and never changes native execution. Keep Reply Cards
  within the closed Activity/Partial Answer/Result/Files set. Goal management cards
  are separate. Outside Reply Cards, ADR 0107 permits process messages carrying only
  the safe Activity projection. Never expose reasoning or raw tool arguments/output.
  Files use exact Turn evidence, not workspace scans.
  Partial Answers use the existing observation/consumption chain independently of
  Progress Card, never signal completion, and never create database answer history.
  Preserve Goal's four-proof completion and exact final-Turn Result/Files handoff contract,
  including confirmed explicit clear while the current Turn completes (ADR 0084).
- Resolve a missing progress setting from the instance fallback only when capturing
  settings for execution; never persist the resolved fallback into a Binding,
  default rule or plan. Preserve all historical progress settings as explicit choices.
- Preserve native capabilities. Users can disable them in native settings.
  A per-session override requires a concrete Feishu benefit.
  Weigh each capability against the Feishu interaction context: terminal-only interactions
  (for example `@` autocomplete) need not be reproduced, and conflicts with Netizen's own
  commands are decided explicitly. Gate unsupported operations at every entry and never simulate them with prompts or local state.
  See [product boundaries](docs/design.md#目标与边界).
- Keep runtime Agent guidance separate from user guidance. Follow its documented
  description/body boundary and size budgets before adding execution hints.

## Deployment boundaries

- Select one canonical root by `--root` > `NETIZEN_ROOT` > the effective user's
  `~/.netizen`; derive exact systemd user unit/LaunchAgent and maintenance job
  names from that root (ADR 0074). Do not add a registry,
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
  (ADR 0076). Validate the configured backend at startup.
  Preserve v14 baseline and frozen migration steps/fixtures. Missing data
  never means a new instance. SQL failures before commit roll back; failures after
  commit retain the new database, never automatically restore an old snapshot.
  Restore CLOEXEC before Codex children start. Loaded/active is never a substitute
  for the private ready marker.
- Built-in Skills belong to the physical installed package. On Codex, register their root on
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

## Design principles

Boundaries and examples are in the [design principles](docs/CONTRIBUTING.md#设计原则).

- Decide from current state, not from the event or scenario that produced it.
  Derive each output from independent state dimensions. Act only when the desired
  result differs from the last confirmed one, and treat an unknown result as
  different. Do not add "when X happens, also do Y" handlers.
- Keep failures local. A failed attempt affects only that attempt; the next one
  recomputes from fresh state, without retry timers or sticky failure counts.
  Degrade the smallest scope that contains the problem (attempt, Turn, session,
  instance) and notify once. Never resend a non-idempotent action whose result is
  unknown.
- Keep the normal path complete and send every anomaly through one generic
  degraded path. When review keeps finding holes in edge cases, simplify the rule
  instead of patching cases.
- Base decisions on facts the native agent publicly reports, pinned by gates, and
  compare like with like within one public view.
- Test invariants with ordering explorers and mutation checks rather than one
  test per scenario. Freeze decoded output before a behavior-preserving refactor.
- Write the degraded path and the blocking bar into the spec before implementation
  and review.

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
  Package changes require isolated wheel/sdist validation. Synthetic tests do not replace platform/live
  acceptance; historical release-installer evidence does not certify the new CLI.
- Formal releases classify the range with `scripts/release.py` (ADR 0103). Routine
  releases use exact-main CI, including native isolated and Admin browser smoke, and
  isolated wheel/sdist validation. Triggered manual areas require recorded evidence
  or explicit risk acceptance.
- Read the relevant acceptance gates before deployment changes. Before rollout, read
  the multi-instance/Skills gates and report platform and live validation separately.
- Review the final diff and report checks, results, and material verification gaps.
