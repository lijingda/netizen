from __future__ import annotations

import unittest

from netizen.domain import (
    ACTIVE_STATE_VALUES,
    ActiveState,
    ControlIntent,
    ControlName,
    FeishuScope,
    GoalOperationState,
    NativeCapability,
    PromptInput,
    SESSION_STOP_ACTION_STATES,
    ScopeKind,
    session_stop_available,
)
from netizen.experience import (
    COMMAND_SPECS,
    CommandGroup,
    InvalidInteraction,
    command_help,
    parse_message,
    side_command_help,
)


class ScopeTest(unittest.TestCase):
    def test_direct_group_and_topic_keys_are_distinct(self) -> None:
        direct = FeishuScope("cli:test", "oc_chat", ScopeKind.DIRECT)
        group = FeishuScope("cli:test", "oc_chat", ScopeKind.GROUP)
        topic = FeishuScope("cli:test", "oc_chat", ScopeKind.TOPIC, "omt:one")

        self.assertEqual(len({direct.key, group.key, topic.key}), 3)
        self.assertNotIn("cli:test", direct.key)
        self.assertNotIn("omt:one", topic.key)

    def test_topic_requires_topic_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires topic_id"):
            FeishuScope("cli_test", "oc_chat", ScopeKind.TOPIC)


class SessionActionPolicyTest(unittest.TestCase):
    def test_active_states_and_stop_actions_are_central(self) -> None:
        self.assertEqual(
            ACTIVE_STATE_VALUES,
            frozenset(state.value for state in ActiveState),
        )
        self.assertEqual(
            SESSION_STOP_ACTION_STATES,
            frozenset(
                {
                    ActiveState.RUNNING.value,
                    ActiveState.STOPPING.value,
                    ActiveState.OBSERVATION_UNAVAILABLE.value,
                    GoalOperationState.RUNNING.value,
                    GoalOperationState.PAUSING.value,
                }
            ),
        )

    def test_stop_availability_consumes_the_shared_status_set(self) -> None:
        for state in SESSION_STOP_ACTION_STATES:
            with self.subTest(state=state):
                self.assertTrue(session_stop_available(state))
        self.assertFalse(session_stop_available("goal-paused"))
        self.assertFalse(session_stop_available("externally-active-goal"))
        self.assertFalse(session_stop_available(None))


class ExperienceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = FeishuScope("cli_test", "oc_chat", ScopeKind.DIRECT)

    def parse(self, text: str, *capabilities: NativeCapability):
        return parse_message(
            scope=self.scope,
            message_id="om_1",
            sender_id="ou_user",
            text=text,
            available_capabilities=capabilities,
        )

    def test_plain_text_and_double_slash_are_prompts(self) -> None:
        plain = self.parse("hello")
        escaped = self.parse("//status")
        resolved_references = self.parse(
            "$code-review $test-triage inspect",
            NativeCapability.SKILLS,
        )

        self.assertIsInstance(plain, PromptInput)
        self.assertEqual(plain.text, "hello")
        self.assertIsInstance(escaped, PromptInput)
        self.assertEqual(escaped.text, "/status")
        self.assertIsInstance(resolved_references, PromptInput)
        self.assertEqual(
            resolved_references.text,
            "$code-review $test-triage inspect",
        )
        self.assertEqual(
            resolved_references.skill_names,
            ("code-review", "test-triage"),
        )
        with self.assertRaisesRegex(InvalidInteraction, "discovery 不可用"):
            self.parse("$code-review inspect")

    def test_supported_commands_are_typed_controls(self) -> None:
        new = self.parse("/new")
        settings = self.parse("/settings")
        config = self.parse("/config")
        sessions = self.parse("/threads")
        archived = self.parse("/sessions archived")
        rename = self.parse('/rename "Release review"')
        archive = self.parse("/archive")
        delete = self.parse("/delete")
        unarchive = self.parse("/unarchive abcdef12")

        self.assertIsInstance(new, ControlIntent)
        self.assertEqual(new.name, ControlName.NEW)
        self.assertEqual(new.arguments, ())
        self.assertEqual(settings.name, ControlName.SETTINGS)
        self.assertEqual(config.name, ControlName.CONFIG)
        self.assertEqual(sessions.name, ControlName.SESSIONS)
        self.assertEqual(archived.arguments, ("archived",))
        self.assertEqual(rename.arguments, ("Release review",))
        self.assertEqual(archive.name, ControlName.ARCHIVE)
        self.assertEqual(delete.name, ControlName.DELETE)
        self.assertEqual(unarchive.arguments, ("abcdef12",))

    def test_new_is_card_only_and_all_raw_tails_get_one_migration_result(
        self,
    ) -> None:
        for command in (
            "/new none",
            "/new alias",
            '/new "alias"',
            "/new one two",
            '/new "unterminated',
        ):
            with self.subTest(command=command), self.assertRaisesRegex(
                InvalidInteraction,
                "快捷创建已下线，请发送 /new 并在卡片中选择",
            ):
                self.parse(command)

        escaped = self.parse("//new alias")
        self.assertIsInstance(escaped, PromptInput)
        self.assertEqual(escaped.text, "/new alias")

    def test_model_shortcuts_are_routed_to_config_not_registered(self) -> None:
        for command in ("/model", "/effort high", "/fast"):
            with self.subTest(command=command), self.assertRaisesRegex(
                InvalidInteraction,
                "统一使用 /config",
            ):
                self.parse(command)

    def test_project_mistakes_explain_settings_without_registering_controls(self) -> None:
        for command in ("/project", "/projects", "/PROJECT demo", '/projects "demo"'):
            with self.subTest(command=command), self.assertRaises(
                InvalidInteraction
            ) as raised:
                self.parse(command)
            error = str(raised.exception)
            self.assertIn("/settings", error)
            self.assertIn("/new", error)
            self.assertIn("/help", error)
            self.assertIn("未执行", error)

        escaped = self.parse("//project demo")
        self.assertIsInstance(escaped, PromptInput)
        self.assertEqual(escaped.text, "/project demo")
        self.assertNotIn("/project", command_help())

    def test_empty_message_points_to_help(self) -> None:
        with self.assertRaises(InvalidInteraction) as raised:
            self.parse("  ")
        self.assertIn("/help", str(raised.exception))

    def test_unavailable_native_commands_fail_closed(self) -> None:
        for command in ("/goal ship it",):
            with self.subTest(command=command), self.assertRaisesRegex(
                InvalidInteraction,
                "兼容契约未通过",
            ):
                self.parse(command)
        for command in ("/plan ship it", "/apps"):
            with self.subTest(command=command), self.assertRaisesRegex(
                InvalidInteraction,
                "高层 SDK 缺少",
            ):
                self.parse(command)

    def test_compact_is_a_native_control(self) -> None:
        self.assertEqual(self.parse("/compact").name, ControlName.COMPACT)

    def test_goal_is_a_capability_gated_typed_control(self) -> None:
        goal = self.parse("/goal ship it", NativeCapability.GOAL)
        exact_goal = self.parse(
            '/goal preserve  "quoted text"   and spacing',
            NativeCapability.GOAL,
        )
        unmatched_quote_goal = self.parse(
            '/goal explain the "unterminated quote literally',
            NativeCapability.GOAL,
        )
        self.assertEqual(goal.name, ControlName.GOAL)
        self.assertEqual(goal.arguments, ("ship it",))
        self.assertEqual(
            exact_goal.arguments,
            ('preserve  "quoted text"   and spacing',),
        )
        self.assertEqual(
            unmatched_quote_goal.arguments,
            ('explain the "unterminated quote literally',),
        )
        help_text = command_help({NativeCapability.GOAL})
        self.assertIn("/goal", help_text)

    def test_side_is_capability_gated_and_preserves_free_form_first_prompt(self) -> None:
        with self.assertRaisesRegex(InvalidInteraction, "兼容契约未通过"):
            self.parse("/side investigate")

        empty = self.parse("/side", NativeCapability.SIDE)
        prompt = self.parse(
            '/side preserve  "quoted text"   and spacing',
            NativeCapability.SIDE,
        )
        unmatched = self.parse(
            '/side explain the "unterminated quote literally',
            NativeCapability.SIDE,
        )
        close = self.parse("/side close", NativeCapability.SIDE)

        self.assertEqual(empty.name, ControlName.SIDE)
        self.assertEqual(empty.arguments, ())
        self.assertEqual(
            prompt.arguments,
            ('preserve  "quoted text"   and spacing',),
        )
        self.assertEqual(
            unmatched.arguments,
            ('explain the "unterminated quote literally',),
        )
        self.assertEqual(close.arguments, ("close",))
        self.assertIn("/side", command_help({NativeCapability.SIDE}))
        self.assertNotIn("/side", command_help())

    def test_release_is_capability_gated_and_argument_free(self) -> None:
        with self.assertRaisesRegex(InvalidInteraction, "订阅释放契约未通过"):
            self.parse("/release")

        release = self.parse("/release", NativeCapability.RELEASE)
        self.assertEqual(release.name, ControlName.RELEASE)
        self.assertEqual(release.arguments, ())
        self.assertIn("/release", command_help({NativeCapability.RELEASE}))
        self.assertNotIn("/release", command_help())

        with self.assertRaisesRegex(InvalidInteraction, "不接受参数"):
            self.parse("/release now", NativeCapability.RELEASE)

    def test_side_help_only_lists_the_side_surface(self) -> None:
        group_help = side_command_help(requires_mention=True)
        direct_help = side_command_help(requires_mention=False)

        for command in ("/status", "/stop", "/side close", "/help"):
            self.assertIn(command, group_help)
        for unavailable in ("/new", "/config", "/goal", "/archive"):
            self.assertNotIn(unavailable, group_help)
        self.assertIn("需要 @机器人", group_help)
        self.assertIn("无需 @机器人", direct_help)

    def test_skills_command_is_not_registered(self) -> None:
        with self.assertRaisesRegex(InvalidInteraction, "未知命令"):
            self.parse("/skills", NativeCapability.SKILLS)
        self.assertNotIn(
            "/skills",
            command_help({NativeCapability.SKILLS}),
        )

    def test_goal_skill_references_remain_explicitly_unavailable(self) -> None:
        with self.assertRaisesRegex(InvalidInteraction, "尚未验证"):
            self.parse(
                "/goal $code-review ship it",
                NativeCapability.GOAL,
                NativeCapability.SKILLS,
            )

    def test_help_is_generated_from_the_registered_command_surface(self) -> None:
        help_text = command_help()
        self.assertIn("/config", help_text)
        self.assertIn("/compact", help_text)
        self.assertIn("`/new`", help_text)
        self.assertNotIn("/new [", help_text)
        self.assertIn("/rename [名称]", help_text)
        self.assertIn("/sessions [archived]", help_text)
        self.assertIn("/unarchive <会话短 ID>", help_text)
        self.assertIn("永久删除当前会话及其原生历史", help_text)
        self.assertIn("不保证前台工具进程退出", help_text)
        self.assertNotIn("/model", help_text)
        self.assertNotIn("/goal", help_text)
        self.assertNotIn("/skills", help_text)
        self.assertNotIn("/plan", help_text)

    def test_help_starts_with_session_and_task_steps(self) -> None:
        first_section = command_help().split("\n---\n", maxsplit=1)[0]
        self.assertLess(first_section.index("/new"), first_section.index("直接发送任务"))
        self.assertIn("/settings", first_section)
        self.assertIn("/sessions", first_section)

    def test_grouped_help_lists_each_available_command_once(self) -> None:
        for capabilities in (frozenset(), frozenset(NativeCapability)):
            with self.subTest(capabilities=capabilities):
                help_text = command_help(capabilities)
                command_lines = [
                    line for line in help_text.splitlines() if line.startswith("- `/")
                ]
                expected = [
                    spec
                    for spec in COMMAND_SPECS
                    if spec.intent is not None
                    and (spec.requires is None or spec.requires in capabilities)
                ]
                self.assertCountEqual(
                    [line.split("`", maxsplit=2)[1] for line in command_lines],
                    [spec.usage for spec in expected],
                )
                for group in CommandGroup:
                    section = help_text.split(f"### {group.value}\n", maxsplit=1)[1]
                    section = section.split("\n---\n", maxsplit=1)[0]
                    for spec in expected:
                        if spec.group is group:
                            self.assertIn(f"`{spec.usage}`", section)

    def test_host_only_commands_are_explicitly_rejected_and_hidden(self) -> None:
        for command in ("/copy", "/vim", "/theme", "/exit", "/quit"):
            with self.subTest(command=command), self.assertRaisesRegex(
                InvalidInteraction,
                "宿主",
            ):
                self.parse(command)
        help_text = command_help()
        self.assertNotIn("/copy", help_text)
        self.assertNotIn("/exit", help_text)

    def test_unknown_command_never_becomes_a_prompt(self) -> None:
        with self.assertRaisesRegex(InvalidInteraction, "未知命令") as raised:
            self.parse("/unknown")
        self.assertIn("/help", str(raised.exception))
        self.assertIn("未执行", str(raised.exception))

    def test_invalid_quotes_offer_correct_usage_and_help(self) -> None:
        for command, usage in (
            ('/rename "Release review', "/rename [名称]"),
            ('/resume "abcdef12', "/resume <会话短 ID>"),
            ('/threads "archived', "/sessions [archived]"),
            ("/status \\", "/status"),
        ):
            with self.subTest(command=command), self.assertRaises(
                InvalidInteraction
            ) as raised:
                self.parse(command)
            error = str(raised.exception)
            self.assertIn("命令格式错误", error)
            self.assertIn(f"用法：{usage}", error)
            self.assertIn("/help", error)
            self.assertNotIn("No closing quotation", error)
            self.assertNotIn("No escaped character", error)

        with self.assertRaises(InvalidInteraction) as raised:
            self.parse('/unknown "')
        self.assertIn("/help", str(raised.exception))

    def test_invalid_arguments_offer_usage_and_help(self) -> None:
        for command, usage, reason in (
            ("/status extra", "/status", "不接受参数"),
            ("/config extra", "/config", "不接受参数"),
            ("/compact extra", "/compact", "不接受参数"),
            ("/release extra", "/release", "不接受参数"),
            ("/archive extra", "/archive", "不接受参数"),
            ("/resume", "/resume <会话短 ID>", "参数不正确"),
            ("/sessions unknown", "/sessions [archived]", "参数不正确"),
            ("/threads unknown", "/sessions [archived]", "参数不正确"),
            ("/unarchive", "/unarchive <会话短 ID>", "参数不正确"),
            ('/rename ""', "/rename [名称]", "不能为空"),
            ("/rename " + "x" * 121, "/rename [名称]", "120"),
        ):
            with self.subTest(command=command), self.assertRaises(
                InvalidInteraction
            ) as raised:
                self.parse(command, NativeCapability.RELEASE)
            error = str(raised.exception)
            self.assertIn(reason, error)
            self.assertIn(f"用法：{usage}", error)
            self.assertIn("/help", error)
