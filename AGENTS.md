# Netizen engineering guardrails

Keep durable contributor rules here. Behavior, procedures, and compatibility
evidence belong in the linked source documents; read the relevant sections and
their cited ADRs before changing that boundary.

## Task navigation

- Product introduction and first use: [README.md](README.md). Complete commands
  and user-visible behavior: [user guide](skills/netizen-user-guide/references/user-guide.md).
  Local development and contribution: [CONTRIBUTING.md](docs/CONTRIBUTING.md).
  For a system overview, see [engineering overview](docs/design.md#工程概览);
  domain vocabulary is in [CONTEXT.md](CONTEXT.md). The task-specific contracts
  and ADRs below remain the direct entry points for related changes.
- Runtime, Scope/Binding identity and concurrency:
  [identity](docs/design.md#scopebinding-与会话配置),
  [locks and admission](docs/design.md#锁与输入准入),
  [Turn observation](docs/design.md#普通-turn-终态与观测恢复), and
  [failure semantics](docs/design.md#失败语义).
  Lifecycle-specific rules are in [Goal](docs/design.md#goal-物理-turn-与逻辑终态),
  [Side](docs/design.md#side-运行与关闭),
  [subscriptions](docs/design.md#普通会话订阅), and
  [archive/delete](docs/design.md#原生归档与删除).
  SDK compatibility boundaries are in [adapters](docs/design.md#sdk-适配边界).
  Goal input retains exact physical-Turn admission across rollover
  ([ADR 0069](docs/adr/0069-steer-the-current-physical-goal-turn.md)).
  Background Thread naming has its own failure and cleanup boundary in
  [ADR 0067](docs/adr/0067-name-threads-with-private-ephemeral-forks.md).
- Message provenance, mention catch-up, quotes, and images:
  [input preparation](docs/design.md#消息输入准备).
  Shared native question cards use target-specific Binding/Side input admission
  ([ADR 0071](docs/adr/0071-answer-native-questions-through-binding-input.md),
  [ADR 0072](docs/adr/0072-share-question-interaction-across-binding-and-side.md)).
  Reply Cards, Activity, completion mentions, and Files:
  [presentation](docs/design.md#回复与活动展示).
  Controls and form callbacks: [commands and cards](docs/design.md#飞书命令与控制卡片).
  Preserve the referenced
  ADRs' exact identity, context, display, and file-evidence contracts.
- SQLite and current schema policy: [data](docs/design.md#channel-数据库与结构校验).
  App credentials: [credential source](docs/design.md#飞书应用凭据).
  Service environment and native settings: [configuration](docs/design.md#原生环境与能力配置).
  Admin Web: [queries and actions](docs/design.md#管理查询与操作), including
  [runtime polling](docs/design.md#运行态投影与轮询).
  Project deletion uses the exact inventory, tombstone and lifecycle boundaries
  in [the current contract](docs/design.md#project-删除与交接) and
  [ADR 0060](docs/adr/0060-delete-projects-with-exact-session-inventory.md).
- Scheduled Plans, dispatch, ordinary topic Threads, and the dedicated MCP
  management entry: [scheduled tasks](docs/design.md#定时任务) and
  [ADR 0061](docs/adr/0061-schedule-ordinary-threads-in-feishu-topics.md).
  Manual runs of saved plans follow [ADR 0068](docs/adr/0068-run-saved-scheduled-plans-manually.md).
  Existing-Binding inputs follow [ADR 0070](docs/adr/0070-schedule-inputs-in-existing-bindings.md):
  ordinary start/steer admission and feedback, input-receipt Run semantics,
  and lifecycle-derived suspension independent of manual enablement.
- Installation, release, permissions, and platform service management:
  [deployment](docs/deployment.md). Routine Agent installation starts with the
  README command and follows installer output; consult the relay procedure for
  alternate setup or troubleshooting. Read the relevant acceptance gates
  before changing deployment behavior.
- End-user usage consultation:
  [netizen-user-guide](skills/netizen-user-guide/SKILL.md). Its reference manual
  explains the product; it is not an engineering implementation guide.
- Decisions: [ADRs](docs/adr/). Add a root navigation reference only when a new
  decision creates a durable guardrail contributors need before related work.

## Architecture

- Netizen is a Feishu/Lark Channel for Codex. Keep one long-lived Python
  service, one `FeishuChannel`, one Channel database, and one shared
  `AsyncCodex`. The Channel SDK owns messaging; the official `openai-codex`
  SDK owns native Threads, Turns, history, tools, configuration, and permissions.
- The in-process Admin Web is a management-only adapter over that same
  application/runtime boundary (ADR 0031). The one-shot Admin deployment process
  for upgrades and explicit restarts is the sole deployment exception
  (ADR 0057/0059). The sole in-process Scheduler shares that boundary; Admin
  may maintain Scheduled Plans and request manual runs of their saved versions,
  but cannot submit arbitrary immediate Prompts (ADR 0061/0068).
  Do not add another runtime, long-lived service, scheduler, history model, or
  configuration layer.
- Preserve exact Scope/Binding-to-native-Thread identity. Running input steers
  the exact Turn; it is never queued, merged, or converted into another Turn.
  Unknown side effects fail closed at the documented operation-specific scope.
- Different Bindings may run concurrently in the same canonical Project cwd.
  Do not add a global semaphore, Project lock, workspace copy, per-Binding
  `CODEX_HOME`, or cross-Thread execution limit.
- Channel SQLite owns only the documented Scope/Binding/Project metadata,
  schema version, deduplication TTL keys, explicit Binding choices/revisions,
  and Side routes/tombstones. ADR 0061/0070 narrowly add current Scheduled Plan
  instructions, minimal dispatch/Run metadata, exact initial or accepted physical Turn references,
  and bounded management-request deduplication. Never store other prompts,
  message bodies, responses, Turn history/activity, card sessions, effective
  Codex configuration, or Admin sessions/tokens/indexes/audit records.
  Side routes never store native Thread IDs.
- Use exact-pinned official SDKs and public high-level APIs. Approved narrow
  adapters are terminal cleanup (ADR 0009), Goal/Skills (0014), Side boundary
  (0021), Thread unsubscribe (0028), Thread Delete (0037), and non-consuming
  Activity observation (0020/0052). Do not add a generic/private RPC gateway,
  parse CLI output, patch SDK internals, copy protocol models, or signal
  arbitrary processes. New gaps require an accepted ADR and a removal trigger.
- Preserve each adapter's documented gate: cleanup and Activity retain exact
  version/fingerprint checks; the other adapters use capability shape,
  synthetic, and live harnesses. Delete also requires disposable live coverage
  and Runtime four-view reconciliation. Model upgrades do not replace these gates.
- Message-history access remains the narrow, public, typed, read-only
  `lark-oapi` port using the same app credentials (ADR 0039). Preserve chat-main
  versus thread-topic semantics, inert historical context, and its rollout gate.
- Display is best effort and never changes native execution. Keep Reply Cards
  within the closed Goal/Activity/Result/Files set; never expose reasoning or raw
  tool arguments/output. Files use exact Turn evidence, not workspace scans.
  Preserve Goal's four-proof completion and exact final-Turn handoff contract.
- Every materialized persisted non-ephemeral Thread retains archive/delete
  controls. Delegate shutdown to App Server after reserving exact lifecycle
  intent and releasing Binding/Scope locks; do not pre-interrupt, cleanup, or
  wait for idle. Preserve bounded observation/reconciliation and Binding-local
  unknown states (ADR 0037/0049); never fabricate a terminal.
- Feishu app availability and chat membership govern Channel admission. The
  single Instance Administrator is separate Admin Web authority, not multi-user
  RBAC, a Netizen allowlist, or Project ACLs. Native Codex controls model, tools,
  Skills, MCP, sandboxing, and environment policy except the documented Binding
  Model/Effort/Speed intent, non-login tool boundary, and dedicated temporary
  Scheduler MCP server entry (ADR 0061). This public process override must not
  write user configuration, replace user MCP entries, or override developer/base
  instructions. New Threads use the public SDK's `auto_review` default;
  Ask/Custom approval is not inherited.
- Unsupported native capabilities remain explicit gaps. Keep product non-goals
  in [design.md](docs/design.md#目标与边界); do not simulate them with prompts or
  local state.

## Deployment boundaries

- Use the effective user's fixed `~/.netizen` root, native systemd user unit or
  current-user LaunchAgent, and standard Codex state. No XDG profiles,
  LaunchDaemon, root helper, persistent Netizen environment file, or PATH snapshot.
  Reload the exported interactive-login-shell environment at each start; Codex
  tool shells must not replace it (ADR 0022/0023).
- The fixed `netizen` profile in `~/.netizen/lark-app/config.json` is the sole
  application credential source (ADR 0066). Share its documented Lark CLI file
  format without adding a CLI installation/runtime dependency. Preserve exact-App
  repair versus whole-file deletion for rebinding and atomic credential writes.
- Published Release and Source Install share one activation/rollback transaction.
  Keep database/Skill rollback gated by both unloaded manager target and released
  lifetime lock; restore CLOEXEC before Codex children start. Loaded/active is
  never a substitute for the private ready marker (ADR 0034).
- The maintainer chooses formal release timing; `scripts/release.py` executes
  the chain (ADR 0050). Nothing auto-releases on main or tag pushes. Admin upgrades
  target an exact immutable official Release through the shared installer/lock;
  Admin restarts use the exact installed service script without installation. Both
  share that lock and keep bounded results in deployment state, never SQLite
  (ADR 0057/0059).
- Use the official `install.sh` for Published Releases and `./dev-install.sh`
  for the exact workspace. Agents download the official installer to a file;
  run it with `</dev/null` and follow its output. Public installation can perform
  initial browser setup and exact-App repair without a TTY (ADR 0062); Admin
  upgrades still return `requires_action` without browser authorization.
  Relay any verification URL to the user while retaining the same installer
  process and reading its stderr progress; credentials are saved privately.
  Never request an App Secret in chat. Other interaction modes are documented
  in the deployment handoff procedure.
  A successful official installer exit completes routine upgrade verification;
  expand checks only for an ambiguous result, changed boundary, or user request.
- No default remote target is defined. For operations, read ignored
  `LOCAL_ENVIRONMENT.md` when present; otherwise use an explicit target. Never
  copy its coordinates into tracked files/artifacts or treat it as runtime config.

## Verification

- Scale planning, tests, and review to the change. Preserve behavior coverage;
  avoid tests that lock documentation wording or source layout.
- Use `make check` for the repository gate. Run affected live phases only under
  the [documented triggers](docs/deployment.md#代码门禁与按需实时兼容性验证).
  Formal Releases reuse successful CI for the exact main commit; routine
  Published Release upgrades use installer success rather than repeated acceptance.
- Review the final diff and report checks, results, and material verification gaps.
