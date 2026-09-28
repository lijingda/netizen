-- Frozen schema v14, independent of current runtime creation code.
CREATE TABLE bindings (
                    binding_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES scopes(scope_key),
                    project_alias TEXT NOT NULL,
                    native_thread_id TEXT UNIQUE,
                    model_id TEXT,
                    effort_id TEXT,
                    service_tier_id TEXT,
                    settings_revision INTEGER NOT NULL DEFAULT 1
                        CHECK(settings_revision >= 1),
                    message_context_mode TEXT NOT NULL DEFAULT 'current-only'
                        CHECK(
                            message_context_mode IN ('current-only', 'catch-up')
                        ),
                    context_anchor_message_id TEXT,
                    context_anchor_create_time_ms INTEGER,
                    context_revision INTEGER NOT NULL DEFAULT 1
                        CHECK(
                            typeof(context_revision) = 'integer'
                            AND context_revision >= 1
                        ),
                    task_reactions_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(
                            typeof(task_reactions_enabled) = 'integer'
                            AND task_reactions_enabled IN (0, 1)
                        ),
                    progress_card_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(
                            typeof(progress_card_enabled) = 'integer'
                            AND progress_card_enabled IN (0, 1)
                        ),
                    completion_mention_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK(
                            typeof(completion_mention_enabled) = 'integer'
                            AND completion_mention_enabled IN (0, 1)
                        ),
                    feedback_revision INTEGER NOT NULL DEFAULT 1
                        CHECK(
                            typeof(feedback_revision) = 'integer'
                            AND feedback_revision >= 1
                        ),
                    creator_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    ever_activated INTEGER NOT NULL DEFAULT 1
                        CHECK(ever_activated IN (0, 1)),
                    CHECK(
                        (
                            model_id IS NULL
                            AND effort_id IS NULL
                            AND service_tier_id IS NULL
                        ) OR (
                            model_id IS NOT NULL
                            AND effort_id IS NOT NULL
                            AND service_tier_id IS NOT NULL
                        )
                    ),
                    CHECK(
                        (
                            message_context_mode = 'current-only'
                            AND context_anchor_message_id IS NULL
                            AND context_anchor_create_time_ms IS NULL
                        ) OR (
                            message_context_mode = 'catch-up'
                            AND context_anchor_message_id IS NOT NULL
                            AND length(context_anchor_message_id) > 0
                            AND context_anchor_create_time_ms IS NOT NULL
                            AND typeof(context_anchor_create_time_ms) = 'integer'
                            AND context_anchor_create_time_ms > 0
                        )
                    )
                );

CREATE TABLE dedup_keys (
                    dedup_key TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );

CREATE TABLE projects (
                        alias TEXT PRIMARY KEY,
                        cwd TEXT NOT NULL,
                        enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                        deleted INTEGER NOT NULL DEFAULT 0
                            CHECK(typeof(deleted) = 'integer' AND deleted IN (0, 1)),
                        revision INTEGER NOT NULL CHECK(revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

CREATE TABLE schedule_plans (
        plan_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision >= 1),
        name TEXT NOT NULL, instructions TEXT NOT NULL, project_alias TEXT NOT NULL,
        app_id TEXT NOT NULL, chat_id TEXT NOT NULL, schedule_json TEXT,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), next_due_at REAL,
        processed_through REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        source TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        session_settings_json TEXT,
        target_kind TEXT NOT NULL DEFAULT 'new_topic' CHECK(target_kind IN ('new_topic','binding')),
        target_binding_id TEXT,
        CHECK((target_kind = 'new_topic' AND target_binding_id IS NULL) OR
              (target_kind = 'binding' AND target_binding_id IS NOT NULL AND session_settings_json IS NULL)),
        CHECK(deleted = 0 OR (enabled = 0 AND instructions = '' AND schedule_json IS NULL))
    );

CREATE TABLE schedule_requests (
        request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_digest TEXT NOT NULL,
        plan_id TEXT NOT NULL, revision INTEGER NOT NULL, expires_at REAL NOT NULL,
        run_id TEXT
    );

CREATE TABLE schedule_runs (
        run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES schedule_plans(plan_id),
        plan_revision INTEGER NOT NULL, due_at REAL NOT NULL,
        project_alias TEXT NOT NULL, app_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('claimed','publishing_topic','binding_ready','starting_turn','handed_off','released')),
        barrier TEXT NOT NULL CHECK(barrier IN ('held','unknown','released')),
        root_uuid TEXT NOT NULL UNIQUE, seed_uuid TEXT NOT NULL UNIQUE,
        root_message_id TEXT, topic_id TEXT, origin_message_id TEXT,
        binding_id TEXT, initial_turn_id TEXT, error_code TEXT, delivery_state TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        missed_from REAL, missed_count INTEGER NOT NULL DEFAULT 0,
        binding_removed INTEGER NOT NULL DEFAULT 0 CHECK(binding_removed IN (0,1)),
        trigger_source TEXT NOT NULL DEFAULT 'scheduled' CHECK(trigger_source IN ('scheduled','manual')),
        target_kind TEXT NOT NULL DEFAULT 'new_topic' CHECK(target_kind IN ('new_topic','binding')),
        disposition TEXT CHECK(disposition IN ('started','steered')),
        CHECK(target_kind = 'new_topic' OR binding_id IS NOT NULL),
        CHECK(disposition IS NULL OR (target_kind = 'binding' AND initial_turn_id IS NOT NULL AND barrier = 'released'))
    );

CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE scopes (
                    scope_key TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    topic_id TEXT,
                    active_binding_id TEXT,
                    updated_at TEXT NOT NULL
                );

CREATE TABLE session_defaults (
        id TEXT PRIMARY KEY,
        app_id TEXT NOT NULL CHECK(length(app_id) > 0),
        kind TEXT NOT NULL CHECK(kind IN ('chat', 'group_name')),
        chat_id TEXT, keyword TEXT,
        project TEXT NOT NULL CHECK(length(project) > 0),
        session_settings_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision >= 1),
        position INTEGER,
        CHECK((kind = 'chat' AND chat_id IS NOT NULL AND length(chat_id) > 0
            AND keyword IS NULL AND position IS NULL) OR
            (kind = 'group_name' AND chat_id IS NULL AND keyword IS NOT NULL
            AND length(trim(keyword)) > 0 AND typeof(position) = 'integer' AND position >= 0))
    );

CREATE TABLE session_defaults_order (
        app_id TEXT PRIMARY KEY CHECK(length(app_id) > 0),
        revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision >= 1)
    );

CREATE TABLE side_topics (
                    side_id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    topic_id TEXT,
                    root_message_id TEXT,
                    source_message_id TEXT NOT NULL,
                    parent_binding_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    requires_mention INTEGER NOT NULL
                        CHECK(requires_mention IN (0, 1)),
                    state TEXT NOT NULL
                        CHECK(state IN ('creating', 'open', 'closed', 'expired', 'failed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(app_id, source_message_id)
                );

CREATE INDEX bindings_by_scope
                    ON bindings(scope_key, activated_at DESC, created_at DESC);

CREATE INDEX bindings_global_created
    ON bindings(created_at DESC, binding_id DESC)
    ;

CREATE INDEX bindings_project_created
    ON bindings(project_alias, created_at DESC, binding_id DESC)
    ;

CREATE INDEX bindings_scope_created
    ON bindings(scope_key, created_at DESC, binding_id DESC)
    ;

CREATE INDEX schedule_plans_due ON schedule_plans(app_id, enabled, next_due_at) WHERE deleted = 0;

CREATE INDEX schedule_plans_project ON schedule_plans(project_alias, deleted);

CREATE UNIQUE INDEX schedule_runs_barrier ON schedule_runs(plan_id) WHERE barrier != 'released';

CREATE UNIQUE INDEX schedule_runs_binding ON schedule_runs(binding_id) WHERE binding_id IS NOT NULL AND target_kind = 'new_topic';

CREATE UNIQUE INDEX schedule_runs_due ON schedule_runs(plan_id, due_at) WHERE trigger_source = 'scheduled';

CREATE INDEX schedule_runs_plan ON schedule_runs(plan_id, due_at DESC, run_id DESC);

CREATE UNIQUE INDEX schedule_runs_root ON schedule_runs(app_id, chat_id, root_message_id) WHERE root_message_id IS NOT NULL AND target_kind = 'new_topic';

CREATE UNIQUE INDEX schedule_runs_topic ON schedule_runs(app_id, chat_id, topic_id) WHERE topic_id IS NOT NULL AND target_kind = 'new_topic';

CREATE INDEX scopes_chat_topic_kind
    ON scopes(chat_id, topic_id, kind, scope_key)
    ;

CREATE INDEX scopes_kind_chat_topic
    ON scopes(kind, chat_id, topic_id, scope_key)
    ;

CREATE UNIQUE INDEX session_defaults_chat ON session_defaults(app_id, chat_id) WHERE kind = 'chat';

CREATE UNIQUE INDEX session_defaults_position ON session_defaults(app_id, position) WHERE kind = 'group_name';

CREATE UNIQUE INDEX side_topics_by_root
                    ON side_topics(app_id, chat_id, root_message_id)
                    WHERE root_message_id IS NOT NULL;

CREATE UNIQUE INDEX side_topics_by_topic
                    ON side_topics(app_id, chat_id, topic_id)
                    WHERE topic_id IS NOT NULL;

CREATE INDEX side_topics_chat_topic_created
    ON side_topics(chat_id, topic_id, created_at DESC, side_id DESC)
    ;

CREATE INDEX side_topics_global_created
    ON side_topics(created_at DESC, side_id DESC)
    ;

CREATE INDEX side_topics_parent_created
    ON side_topics(parent_binding_id, created_at DESC, side_id DESC)
    ;

CREATE INDEX side_topics_state_created
    ON side_topics(state, created_at DESC, side_id DESC)
    ;

CREATE TRIGGER bindings_context_scope_insert
    BEFORE INSERT ON bindings
    WHEN NEW.message_context_mode = 'catch-up'
         AND EXISTS (
             SELECT 1
             FROM scopes
             WHERE scope_key = NEW.scope_key
               AND kind = 'direct'
         )
    BEGIN
        SELECT RAISE(
            ABORT,
            'direct Binding cannot use catch-up context'
        );
    END;

CREATE TRIGGER bindings_context_scope_update
    BEFORE UPDATE OF scope_key, message_context_mode,
        context_anchor_message_id, context_anchor_create_time_ms
    ON bindings
    WHEN NEW.message_context_mode = 'catch-up'
         AND EXISTS (
             SELECT 1
             FROM scopes
             WHERE scope_key = NEW.scope_key
               AND kind = 'direct'
         )
    BEGIN
        SELECT RAISE(
            ABORT,
            'direct Binding cannot use catch-up context'
        );
    END;

CREATE TRIGGER bindings_context_shape_insert
    BEFORE INSERT ON bindings
    WHEN NOT (
        (
            NEW.message_context_mode = 'current-only'
            AND NEW.context_anchor_message_id IS NULL
            AND NEW.context_anchor_create_time_ms IS NULL
        ) OR (
            NEW.message_context_mode = 'catch-up'
            AND NEW.context_anchor_message_id IS NOT NULL
            AND length(NEW.context_anchor_message_id) > 0
            AND NEW.context_anchor_create_time_ms IS NOT NULL
            AND typeof(NEW.context_anchor_create_time_ms) = 'integer'
            AND NEW.context_anchor_create_time_ms > 0
        )
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'Binding context must match its mode'
        );
    END;

CREATE TRIGGER bindings_context_shape_update
    BEFORE UPDATE OF message_context_mode,
        context_anchor_message_id, context_anchor_create_time_ms
    ON bindings
    WHEN NOT (
        (
            NEW.message_context_mode = 'current-only'
            AND NEW.context_anchor_message_id IS NULL
            AND NEW.context_anchor_create_time_ms IS NULL
        ) OR (
            NEW.message_context_mode = 'catch-up'
            AND NEW.context_anchor_message_id IS NOT NULL
            AND length(NEW.context_anchor_message_id) > 0
            AND NEW.context_anchor_create_time_ms IS NOT NULL
            AND typeof(NEW.context_anchor_create_time_ms) = 'integer'
            AND NEW.context_anchor_create_time_ms > 0
        )
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'Binding context must match its mode'
        );
    END;

CREATE TRIGGER bindings_turn_settings_insert
                    BEFORE INSERT ON bindings
                    WHEN NOT (
                        (
                            NEW.model_id IS NULL
                            AND NEW.effort_id IS NULL
                            AND NEW.service_tier_id IS NULL
                        ) OR (
                            NEW.model_id IS NOT NULL
                            AND NEW.effort_id IS NOT NULL
                            AND NEW.service_tier_id IS NOT NULL
                            AND length(NEW.model_id) > 0
                            AND length(NEW.effort_id) > 0
                            AND length(NEW.service_tier_id) > 0
                        )
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'Binding Turn settings must be all NULL or all set'
                        );
                    END;

CREATE TRIGGER bindings_turn_settings_update
                    BEFORE UPDATE OF model_id, effort_id, service_tier_id ON bindings
                    WHEN NOT (
                        (
                            NEW.model_id IS NULL
                            AND NEW.effort_id IS NULL
                            AND NEW.service_tier_id IS NULL
                        ) OR (
                            NEW.model_id IS NOT NULL
                            AND NEW.effort_id IS NOT NULL
                            AND NEW.service_tier_id IS NOT NULL
                            AND length(NEW.model_id) > 0
                            AND length(NEW.effort_id) > 0
                            AND length(NEW.service_tier_id) > 0
                        )
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'Binding Turn settings must be all NULL or all set'
                        );
                    END;

CREATE TRIGGER scopes_activate_binding
                    AFTER UPDATE OF active_binding_id ON scopes
                    WHEN NEW.active_binding_id IS NOT NULL
                    BEGIN
                        UPDATE bindings
                        SET ever_activated = 1
                        WHERE binding_id = NEW.active_binding_id
                          AND scope_key = NEW.scope_key;
                    END;

CREATE TRIGGER scopes_context_kind_update
    BEFORE UPDATE OF kind ON scopes
    WHEN NEW.kind = 'direct'
         AND EXISTS (
             SELECT 1
             FROM bindings
             WHERE scope_key = NEW.scope_key
               AND message_context_mode = 'catch-up'
         )
    BEGIN
        SELECT RAISE(
            ABORT,
            'direct Scope cannot contain catch-up Binding'
        );
    END;

INSERT INTO schema_version VALUES (14);
