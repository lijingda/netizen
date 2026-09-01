# Netizen engineering guardrails

This file contains only durable rules for contributors. Keep explanation,
design detail, compatibility findings, and procedures in their source docs.

## Project map

Treat the Project map as curated contributor navigation, not a complete ADR
index. Reference a new ADR here only when it creates a durable guardrail that
contributors must know before starting related work.

- [README.md](README.md): product overview, commands, setup, and current status.
- [CONTEXT.md](CONTEXT.md): domain vocabulary.
- [docs/design.md](docs/design.md): current architecture, runtime semantics,
  persistence, configuration, and failures.
- [docs/adr/](docs/adr/): accepted architecture decisions and superseded
  historical context; start from the decisions cited by the relevant guardrails
  below.
- [docs/deployment.md](docs/deployment.md): setup, verification, and release
  procedures.
- Formal releases follow ADR 0050: the maintainer decides timing, then
  `scripts/release.py` executes the whole chain; nothing auto-releases on
  main or tag pushes.
- Public Published Release deployment uses the zero-argument official
  `install.sh`; a repository checkout's zero-argument `./install.sh` downloads
  the latest stable official installer, while `./dev-install.sh` installs the
  exact current workspace and runs the full local gate. Use zero-argument
  `./uninstall.sh` and `./service.sh start|stop|restart|status` without sudo.
  A non-interactive installer with incomplete credentials prepares credential
  files and exits with exact completion steps; agents must download the official
  installer to a file and run `sh install.sh </dev/null` when credentials may be
  absent. Do not pipe an Agent install through `curl | sh`. With complete existing
  credentials, exact-App permission repair is the sole browser exception: an
  agent may relay it when its runner can preserve the exact process across user
  turns and surface intermediate stderr; it does not require a PTY or writable
  stdin. First-time browser setup still requires preserving the exact PTY/process,
  surfacing intermediate output, and resuming stdin; otherwise use the
  credential-file handoff. Follow the exact relay procedure in
  `docs/deployment.md`; never ask the user to paste an App Secret into chat.
- A zero exit from the downloaded official installer is the authoritative
  success signal for a routine Published Release upgrade. It already completes
  Host Validation and the activation/rollback transaction; when the service was
  active it also waits for the private ready marker, while an intentionally
  stopped service remains stopped. After that success, do not repeat database
  integrity, journal, cross-host Admin Web, live-Thread, or manual Feishu
  acceptance checks. Expand verification only when the installer is nonzero or
  its result is ambiguous, the changed boundary requires requalification, or
  the user explicitly requests it. Service-manager `active` alone remains
  weaker than installer success.
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
  transaction across Published Release and Source Install modes and across
  platforms. Service backends own only service definition,
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
  message bodies, ADR 0046's two Binding-scoped Task Feedback booleans/revision,
  plus ADR 0021's Side Topic routes/tombstones without native Thread IDs. Never
  persist prompts, supplemental messages, responses, Turn history/activity
  projections, reply-card identities/sessions, queues, resolved/effective/
  default Codex configuration, or other Codex-owned state. Admin login sessions,
  CSRF tokens, native metadata indexes, and audit records also stay out of
  Channel SQLite.
- ADR 0051 keeps lifecycle reactions for accepted ordinary/Side Turns,
  successful steers, and terminal outcomes always on as best-effort display.
  Reaction Pulse and Progress Card are independent Binding-scoped opt-ins that
  default off; Reaction Pulse controls only the periodic `THINKING` display.
  ADR 0047 renders Goal, Activity, Result, and Files as
  one closed set of typed Reply Card modules under a single best-effort
  presenter; it is not a plugin system. Goal always contributes its control
  module, while Progress Card controls Activity for ordinary Turns and Goals.
  With no Goal, Activity, or files, preserve rich/static terminal text. Never
  expose reasoning, raw tool/command output, tool arguments, elapsed time,
  percentage, or ETA. Display failures never alter native execution.
- ADR 0047 auto-clears only a four-proof complete Goal whose exact final Turn
  completed. Hold its Runtime slot through the bounded terminal display handoff;
  paused/blocked/limited/unknown Goals never auto-clear. Goal controls require
  exact process-local message ownership plus the strongest SDK-visible native
  fingerprint; that fingerprint is not globally unique. Concurrent external
  mutation of the same Thread Goal is unsupported because the pinned SDK's
  thread-scoped clear has no expected-generation CAS.
- ADR 0048 keeps the Side root card and ephemeral lifecycle unchanged, freezes
  Parent Model/Effort/Speed plus both Task Feedback choices at Side creation,
  and renders each Side Turn through the ordinary Activity/Result/Files reply
  path. Parent changes do not propagate, and Goal remains unsupported in Side.
- Use exact-pinned official SDKs and public high-level APIs by default. The only
  approved reach-throughs are ADR 0009's isolated, version/fingerprint-gated
  terminal cleanup and ADR 0014's removable capability-specific Goal/Skills
  adapters, ADR 0021's fixed Side boundary adapter, ADR 0028's reusable fixed
  Thread unsubscribe adapter, ADR 0037's fixed Thread Delete adapter, plus ADR
  0020/0052's version/fingerprint-gated, non-consuming Activity observer. The Delete
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
- ADR 0049 treats `completed`, `interrupted`, and `failed` as terminal for the
  exact Ordinary Turn while preserving its Native Thread. A recoverable
  unverified view gets one short bounded resume/read attempt; if it still
  cannot be verified, keep the exact identity reserved, stop periodic I/O, and
  expose Binding-local `turn-observation-unavailable`. Exact
  `active/inProgress` returns to ordinary unbounded polling; manual recheck is
  the same bounded attempt. Never fabricate a terminal.
- Every materialized persisted non-ephemeral Thread retains archive/delete
  controls regardless of local Turn, Goal, Compaction, or observation state.
  Reserve only the exact Binding lifecycle intent, release Binding/Scope locks,
  and delegate directly to App Server `thread/archive` or `thread/delete`;
  never duplicate App Server shutdown with local interrupt, cleanup, terminal
  waiting, or idle proof. On a non-cancellation response error, reconcile the
  native catalog once without retrying the mutation. Cancellation and an
  inconclusive reconciliation remain Binding-local lifecycle unknown; archived
  and active delete share ADR 0037's native cascade primitive.
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
  ADR 0016's optional Binding-scoped Model/Effort/Speed intent and ADR 0023's
  non-login tool boundary, let native Codex configuration control model, tools,
  Skills, MCP, sandboxing, and shell environment policy. New Threads use the
  public SDK's `auto_review` default because Ask/Custom approval cannot be
  inherited; do not claim that native approval configuration is fully preserved.
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
