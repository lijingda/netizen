-- Representative related metadata, with both scheduled target types and receipts.
INSERT INTO scopes VALUES ('scope', 'app', 'chat', 'group', NULL, 'binding', '2026-09-28');
INSERT INTO projects VALUES ('work', '/workspace', 1, 0, 2, '2026-09-27', '2026-09-28');
INSERT INTO bindings (
    binding_id, scope_key, project_alias, native_thread_id, creator_id, created_at, activated_at
) VALUES ('binding', 'scope', 'work', 'native-thread', 'user', '2026-09-27', '2026-09-28');
INSERT INTO dedup_keys VALUES ('message', 2000000000);
INSERT INTO side_topics VALUES (
    'side', 'app', 'chat', 'side-topic', 'side-root', 'source-message', 'binding',
    'user', 1, 'closed', '2026-09-27', '2026-09-28'
);
INSERT INTO schedule_plans (
    plan_id, revision, name, instructions, project_alias, app_id, chat_id,
    enabled, created_at, updated_at, source, session_settings_json, target_kind, target_binding_id
) VALUES (
    'plan-topic', 1, 'Topic plan', 'Saved plan instructions', 'work', 'app', 'chat',
    0, 1, 2, 'admin',
    '{"turn_settings":null,"reaction_pulse_enabled":false,"progress_card_enabled":true,"completion_mention_enabled":true,"message_context_mode":"current-only"}',
    'new_topic', NULL
), (
    'plan-binding', 2, 'Binding plan', 'Saved binding instructions', 'work', 'app', 'chat',
    0, 1, 2, 'admin', NULL, 'binding', 'binding'
);
INSERT INTO schedule_runs (
    run_id, plan_id, plan_revision, due_at, project_alias, app_id, chat_id, phase, barrier,
    root_uuid, seed_uuid, binding_id, initial_turn_id, created_at, updated_at, target_kind, disposition
) VALUES (
    'run-topic', 'plan-topic', 1, 5, 'work', 'app', 'chat', 'handed_off', 'released',
    'root-uuid-1', 'seed-uuid-1', 'binding', 'turn-1', 5, 6, 'new_topic', NULL
), (
    'run-binding', 'plan-binding', 2, 8, 'work', 'app', 'chat', 'released', 'released',
    'root-uuid-2', 'seed-uuid-2', 'binding', 'turn-2', 8, 9, 'binding', 'steered'
);
INSERT INTO schedule_requests VALUES ('request', 'run_now', 'digest', 'plan-binding', 2, 2000000000, 'run-binding');
INSERT INTO session_defaults VALUES (
    'default-chat', 'app', 'chat', 'chat', NULL, 'work',
    '{"turn_settings":{"model_id":"model","effort_id":"high","service_tier_id":"priority"},"reaction_pulse_enabled":true,"progress_card_enabled":true,"completion_mention_enabled":true,"message_context_mode":"catch-up"}',
    3, NULL
), (
    'default-group', 'app', 'group_name', NULL, 'Work', 'work',
    '{"turn_settings":null,"reaction_pulse_enabled":false,"progress_card_enabled":true,"completion_mention_enabled":true,"message_context_mode":"current-only"}',
    1, 0
);
INSERT INTO session_defaults_order VALUES ('app', 4);
