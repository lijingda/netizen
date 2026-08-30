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
)
from netizen.experience import (
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
        with self.assertRaisesRegex(InvalidInteraction, "0.147.0"):
            self.parse("/compact")

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
        self.assertNotIn("/compact", help_text)
        self.assertIn("/new：", help_text)
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
        with self.assertRaisesRegex(InvalidInteraction, "未知命令"):
            self.parse("/unknown")

    def test_argument_counts_are_strict(self) -> None:
        with self.assertRaisesRegex(InvalidInteraction, "快捷创建已下线"):
            self.parse("/new one two")
        with self.assertRaisesRegex(InvalidInteraction, "不接受参数"):
            self.parse("/status extra")
        with self.assertRaisesRegex(InvalidInteraction, "不接受参数"):
            self.parse("/config extra")
        with self.assertRaisesRegex(InvalidInteraction, "0.147.0"):
            self.parse("/compact extra")
        with self.assertRaisesRegex(InvalidInteraction, "不接受参数"):
            self.parse("/release extra", NativeCapability.RELEASE)
        with self.assertRaisesRegex(InvalidInteraction, "/sessions"):
            self.parse("/sessions unknown")
        with self.assertRaisesRegex(InvalidInteraction, "不接受参数"):
            self.parse("/archive extra")
        with self.assertRaisesRegex(InvalidInteraction, "/unarchive"):
            self.parse("/unarchive")
        with self.assertRaisesRegex(InvalidInteraction, "120"):
            self.parse("/rename " + "x" * 121)
