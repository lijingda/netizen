# Netizen engineering guardrails

This file contains only durable rules for contributors. Keep explanation,
design detail, compatibility findings, and procedures in their source docs.

## Project map

- [README.md](README.md): product overview, commands, setup, and current status.
- [CONTEXT.md](CONTEXT.md): domain vocabulary.
- [docs/design.md](docs/design.md): current architecture, runtime semantics,
  persistence, configuration, and failures.
- [ADR 0008](docs/adr/0008-use-python-sdk-for-native-turn-control.md) and
  [ADR 0009](docs/adr/0009-use-version-gated-experimental-terminal-cleanup.md):
  SDK integration decisions. [ADR 0010](docs/adr/0010-correct-stop-and-background-cleanup-semantics.md)
  corrects `/stop` and foreground-process semantics. [ADR 0014](docs/adr/0014-use-removable-sdk-gap-adapters.md)
  defines the removable Goal/Skills facade-gap boundary. [ADR 0016](docs/adr/0016-store-binding-turn-settings.md)
  defines persistent Binding-scoped Turn-setting intent. [ADR 0017](docs/adr/0017-manage-native-thread-lifecycle.md)
  defines current-Binding lifecycle management and the removable Thread Delete gap.
  [ADR 0018](docs/adr/0018-remove-skills-command.md) removes the dedicated
  `/skills` browser while preserving native Skill invocation. [ADR 0019](docs/adr/0019-keep-native-thread-delete-unavailable.md)
  is the superseded `0.144.4` Thread Delete compatibility record.
  [ADR 0020](docs/adr/0020-observe-active-turn-plans-with-a-pinned-read-only-adapter.md)
  defines the exact-gated, non-consuming active-Turn plan observer.
  [ADR 0021](docs/adr/0021-support-multi-turn-ephemeral-side-topics.md)
  defines ephemeral multi-turn Side Topics and their removable fixed-method
  adapter. [ADR 0022](docs/adr/0022-load-account-shell-environment-at-service-start.md)
  defines service-start environment parity with the account shell.
  [ADR 0023](docs/adr/0023-keep-service-environment-authoritative-for-tools.md)
  keeps that captured environment authoritative for tool subprocesses.
  [ADR 0024](docs/adr/0024-send-structured-turn-files-from-completion-cards.md)
  defines stateless, structured ordinary-Turn file delivery through Feishu
  completion cards. [ADR 0025](docs/adr/0025-use-turn-provenance-not-project-containment-for-files.md)
  keeps Project as a relative-path base rather than a file authorization
  boundary. [ADR 0026](docs/adr/0026-upgrade-and-requalify-python-sdk-0147.md)
  upgrades the exact Python SDK/bundled CLI pair and requalifies both
  fingerprint-gated compatibility adapters. [ADR 0027](docs/adr/0027-use-turn-diff-and-self-contained-file-cards.md)
  uses native Turn diff as the primary file source and makes new completion
  cards self-contained across service restarts. [ADR 0028](docs/adr/0028-release-idle-persistent-thread-subscriptions.md)
  releases idle ordinary-Thread connection subscriptions without deleting
  Bindings or native history. [ADR 0029](docs/adr/0029-project-current-message-provenance-into-prompts.md)
  projects exact current-message sender attribution into ordinary and Side
  Prompts while keeping Side completion origin separate. [ADR 0030](docs/adr/0030-require-resolved-current-sender-names.md)
  requires the Channel SDK to resolve each current Prompt sender name and
  fails closed with the Feishu member-read permission when it cannot, while
  limiting sender identity to the app-scoped Open ID. [ADR 0031](docs/adr/0031-add-in-process-admin-web-control-plane.md)
  accepts the default-on, single-administrator, in-process Admin Web
  control plane while preserving one Runtime and the existing Feishu controls.
  [ADR 0032](docs/adr/0032-use-single-user-product-root.md) fixes the single
  `~/.netizen` product root and its preserved-versus-deletable lifecycle boundary.
  [ADR 0033](docs/adr/0033-use-official-sdk-for-feishu-app-onboarding.md)
  defines the Lark-CLI-free, official-SDK Feishu/Lark Bot app onboarding flow.
  [ADR 0034](docs/adr/0034-support-macos-with-a-user-launchagent.md) adds the
  macOS user LaunchAgent while preserving the shared activation/rollback
  transaction and exact lifetime-lock/ready boundary.
  [ADR 0035](docs/adr/0035-gate-activation-on-effective-feishu-permissions.md)
  lets the official registration page create or select an app and gates every
  activation on the effective tenant permission contract.
  [ADR 0036](docs/adr/0036-archive-exact-idle-sessions-from-the-sessions-card.md)
  permits confirmed exact archive of eligible idle sessions from `/sessions`
  while keeping the `/archive` command current-only.
  [ADR 0037](docs/adr/0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md)
  requalifies native Thread Delete and defines its thin Adapter plus four-view
  failure reconciliation.
  [ADR 0038](docs/adr/0038-delete-exact-idle-sessions-from-the-sessions-card.md)
  permits two-stage exact deletion of eligible idle sessions from `/sessions`
  while keeping the `/delete` command current-only.
  [ADR 0039](docs/adr/0039-add-binding-scoped-mention-catch-up-context.md)
  adds Binding-scoped, still-mention-triggered catch-up context with a durable
  exact-message boundary. [ADR 0040](docs/adr/0040-make-new-card-only-and-show-all-projects.md)
  makes `/new` card-only and displays all enabled Projects in one dropdown.
  Superseded ADRs are historical context.
- [docs/deployment.md](docs/deployment.md): setup, verification, and release
  procedures.
- Public deployment uses zero-argument `./install.sh` / `./uninstall.sh`; use
  `./service.sh start|stop|restart|status` without sudo. A non-interactive
  installer prepares credential files and exits with exact completion steps;
  agents must default to `./install.sh </dev/null` when credentials may be
  absent. An agent may carry the interactive browser setup only when its runner
  can preserve the exact PTY/background process across user turns, surface
  intermediate output, and resume stdin. Otherwise use the non-interactive
  credential-file handoff. Follow the exact relay procedure in
  `docs/deployment.md`; never ask the user to paste an App Secret into chat.
- The product root is fixed at `~/.netizen` below the effective user's account
  home; do not use XDG overrides as profiles or as a second Netizen instance.
  The Linux systemd user unit and macOS LaunchAgent plist remain in their
  native discovery paths; `CODEX_HOME` remains the native Codex state override.
- The user service reloads the account's exported interactive-login-shell
  environment on every start through the bounded launcher in ADR 0022. ADR
  0023's sole service-owned Codex override disables tool login shells so they
  cannot replace that environment. Do not add a persistent Netizen environment
  file, install-time PATH snapshot, TTY emulation, or copied variable policy.
- Keep one shared release/configuration/credential/database/Skill activation
  transaction across platforms. Service backends own only service definition,
  manager state, stop confirmation, publish/start/status, and ready waiting.
  macOS support is a current-user LaunchAgent only: do not add a LaunchDaemon,
  root helper, second environment/config layer, or parsing of `launchctl print`
  text. Database/Skill rollback requires both an unloaded manager target and a
  released stable lifetime lock; the inherited lock FD must be CLOEXEC before
  any Codex child can start. Loaded is never a substitute for the private ready
  marker written after admission opens.
- The repository defines no default remote host, SSH alias, account, or
  deployment path. A maintainer may keep those checkout-specific values in the
  ignored root file `LOCAL_ENVIRONMENT.md`; read it when present, never copy
  its values into tracked files or release artifacts, and never treat it as
  runtime configuration. When it is absent, use an explicitly supplied target
  or ask the user instead of guessing.
- [netizen-user-guide](skills/netizen-user-guide/SKILL.md): release-managed
  Codex Skill and end-user Feishu usage manual.

## Principles

- Netizen is a Feishu/Lark Channel for Codex, not another agent runtime. The
  Channel SDK owns messaging concerns; the official `openai-codex` SDK owns
  native Threads, Turns, history, tools, configuration, and permissions.
- Keep one long-lived Python service, one `FeishuChannel`, one Channel database,
  and one shared `AsyncCodex`. ADR 0031 authorizes exactly one additional
  client protocol: an in-process Admin Web that must remain a
  management-only adapter over that same application/runtime boundary. Do not
  add another service, agent runtime, scheduler, history model, configuration
  layer, or permission system.
- Preserve the Scope/Binding-to-native-Thread semantics in `docs/design.md`.
  Running input steers the exact Turn; it is never queued, merged, or silently
  converted into another Turn. Unknown side effects fail closed.
- Different Bindings may run concurrently and share the same canonical Project
  cwd. Do not add a global semaphore, Project lock, workspace copy, per-Binding
  `CODEX_HOME`, or cross-Thread execution limit.
- Use the service user's standard Codex state. Channel SQLite stores only
  Scope/Binding/Project Registry metadata, schema version, and deduplication
  TTL keys, plus ADR 0016's optional Binding-scoped Turn-setting catalog IDs
  and revision, ADR 0039's Mention Context Mode/exact boundary/revision without
  message bodies, plus ADR 0021's Side Topic routes/tombstones without native
  Thread IDs. Never persist prompts, supplemental messages, responses, Turn history, card sessions,
  queues, resolved/effective/default Codex configuration, or other Codex-owned
  state. Admin login sessions, CSRF tokens, native metadata indexes, and audit
  records also stay out of Channel SQLite.
- Use exact-pinned official SDKs and public high-level APIs by default. The only
  approved reach-throughs are ADR 0009's isolated, version/fingerprint-gated
  terminal cleanup and ADR 0014's removable capability-specific Goal/Skills
  adapters, ADR 0021's fixed Side boundary adapter, ADR 0028's reusable fixed
  Thread unsubscribe adapter, ADR 0037's fixed Thread Delete adapter, plus ADR
  0020's version/fingerprint-gated, non-consuming plan observer. The Delete
  adapter is production-permitted only through ADR 0037's exact method and
  Runtime reconciliation contract. Do not parse
  CLI output, add a generic/private RPC gateway, patch SDK internals, copy
  protocol models, or signal arbitrary processes.
- ADR 0039's `lark-oapi` message-history reader is a narrow public, typed,
  read-only Channel port that reuses the same application credentials. It is
  not a second Channel, event subscription, generic OpenAPI gateway, or message
  store. Keep chat-main and thread-topic container semantics distinct and retain
  the documented live-history rollout gate.
- Treat ADR 0009 cleanup success only as a successful request for App Server's
  registered background terminals. It never attests that foreground tool
  processes exited; `/stop` must preserve ADR 0010's explicit warning. ADR
  0028's read-only background-terminal presence check exposes no process
  identity and never cleans or terminates a terminal.
- Treat compatibility workarounds as narrow, fail-closed code. ADR 0014
  adapters are gated by per-capability shape/synthetic/live harnesses rather
  than a runtime version allowlist; ADR 0037's delete adapter additionally
  requires its disposable live gate and four-view Runtime reconciliation. ADR
  0009 retains its exact
  version/fingerprint gate; ADR 0020 independently retains the same strict
  class of gate for read-only queue observation. Changing one requires updated
  probes, focused tests, an accepted ADR, and a removal trigger.
- Feishu application availability and chat membership are the Channel
  admission boundary. ADR 0031's single Instance Administrator
  is a separate, dedicated-credential authority for the Admin Web only; do not
  turn it into multi-user RBAC, a Netizen allowlist, or Project ACLs. Except for
  ADR 0023's non-login tool boundary, let native Codex configuration control
  model, tools, Skills, MCP, approval, sandboxing, and shell environment policy.
- Unsupported native capabilities remain explicit gaps. Only capabilities
  explicitly approved by an ADR may use a narrow SDK Gap Adapter; never simulate
  them with prompts or local state. Keep Pilot non-goals in `docs/design.md`
  unless the product scope is explicitly changed.

## Change discipline

- For non-trivial work, inspect context first; state acceptance criteria and
  material non-goals; make the smallest reviewable change with focused tests.
- Review the final diff for correctness, regressions, architectural fit,
  missing tests, unnecessary abstraction, and unrelated changes.
- Run the checks appropriate to the changed boundary. Runtime, Channel,
  persistence, SDK, and deployment changes must use the documented
  compatibility/release checks. Report exact results and remaining risks.
- Keep this file short: terminology belongs in `CONTEXT.md`, behavior in
  `docs/design.md`, decisions in ADRs, and procedures in deployment docs.
