from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from lark_channel import OutboundCard, SendResult

from netizen_cli.cards import rename_binding_card
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.runtime.contracts import NativeThreadMetadata, ThreadLifecycleError
from tests.support.channel_cards import (
    _card_button_value, _elements, direct_button_event, direct_card_event,
)
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage


class RenameNameChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app, self.store = self.fixture.app, self.fixture.store
        self.runtime, self.channel = self.fixture.runtime, self.fixture.channel
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        await self.fixture.new()
        self.binding = self.store.active_binding(self.scope.key)
        self.runtime.materialize(self.binding.id, "native-name", cwd=self.fixture.project)
        self.binding = self.store.get(self.binding.id)
        self.runtime.thread_metadata_values["native-name"] = NativeThreadMetadata(
            "native-name", "旧名称", "已有上下文",
        )
        await self.app.handle_message(FakeMessage("/rename", message_id="om_open_rename"))
        self.card = self.channel.replies[-1][1]
        self.message_id = "om_rename"

    async def generate(self, card=None):
        await self.app.handle_card_action(direct_button_event(
            _card_button_value(card or self.card, "自动生成"), message_id=self.message_id,
        ))
        pending = self.app._rename_name_generations.get(self.message_id)
        return pending.task if pending is not None else None

    async def complete(self, task):
        await asyncio.wait_for(task, 2)
        return OutboundCard(card=self.channel.updates[-1][1])

    async def save(self, title):
        field = _elements(self.card.card, "input")[0]["name"]
        await self.app.handle_card_action(direct_card_event(
            self.channel, {field: title}, message_id=self.message_id,
        ))

    def test_generation_does_not_submit_required_input_and_loading_has_no_controls(self):
        form, = _elements(self.card.card, "form")
        self.assertEqual(_elements(form, "button")[0]["text"]["content"], "保存新名称")
        self.assertTrue(_elements(form, "input")[0]["required"])
        self.assertEqual(_card_button_value(self.card, "自动生成")["binding_id"],
                         f"binding:v1:{self.binding.id}")
        loading = rename_binding_card(
            scope=self.scope, binding_id=self.binding.id, short_id=self.binding.short_id,
            project_alias=self.binding.project_alias, generating=True,
        )
        self.assertIn("正在生成中", str(loading.card))
        self.assertEqual(_elements(loading.card, "form"), [])
        self.assertEqual(_elements(loading.card, "button"), [])

    async def test_candidate_can_be_edited_and_saved_without_automatic_write(self):
        result = await self.complete(await self.generate())
        self.assertEqual(len(self.channel.updates), 2)
        self.assertEqual([item[0] for item in self.channel.updates], [self.message_id] * 2)
        self.assertIn("正在生成中", str(self.channel.updates[0][1]))
        self.assertEqual(_elements(result.card, "input")[0]["default_value"], "自动生成的名称")
        self.assertEqual(self.runtime.generate_name_calls, [self.binding.id])
        self.assertEqual(self.runtime.rename_binding_calls, [])
        self.assertNotEqual(_card_button_value(result, "自动生成")["nonce"],
                            _card_button_value(self.card, "自动生成")["nonce"])
        await self.save("  我修改的名称  ")
        self.assertEqual(self.runtime.rename_binding_calls, [(self.binding.id, "我修改的名称")])
        self.assertIn("会话已重命名", str(self.channel.updates[-1][1]))

    async def test_failure_and_timeout_restore_empty_input_and_allow_retry(self):
        for failure in (RuntimeError("模型不可用"), TimeoutError()):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(self.runtime, "generate_name_exact", new=AsyncMock(side_effect=failure)):
                    result = await self.complete(await self.generate())
                self.assertEqual(_elements(result.card, "input")[0]["default_value"], "")
                self.assertIn("名称生成失败", str(result.card))
                self.assertEqual(self.runtime.rename_binding_calls, [])
                retried = await self.complete(await self.generate(result))
                self.assertEqual(_elements(retried.card, "input")[0]["default_value"], "自动生成的名称")

    async def test_generation_returns_promptly_deduplicates_and_does_not_hold_scope(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(binding_id):
            started.set()
            await release.wait()
            return "候选"

        with patch.object(self.runtime, "generate_name_exact", new=AsyncMock(side_effect=delayed)) as generate:
            pending = await asyncio.wait_for(self.generate(), 1)
            await asyncio.wait_for(started.wait(), 1)
            same = await asyncio.wait_for(self.generate(), 1)
            self.assertIs(same, pending)
            async with asyncio.timeout(1):
                async with self.fixture.management.scope_coordinator.hold(self.scope.key):
                    await self.app.handle_message(FakeMessage("/help", message_id="om_other_control"))
            self.assertEqual(generate.await_count, 1)
            self.assertIn("正在生成中", str(self.channel.updates[-1][1]))
            release.set()
            await self.complete(pending)

    async def test_saving_during_generation_prevents_late_result_redraw(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(binding_id):
            started.set()
            await release.wait()
            return "迟到候选"

        with patch.object(self.runtime, "generate_name_exact", new=AsyncMock(side_effect=delayed)):
            pending = await self.generate()
            await asyncio.wait_for(started.wait(), 1)
            await self.save("已保存")
            count = len(self.channel.updates)
            release.set()
            await self.complete(pending)
        self.assertEqual(len(self.channel.updates), count)
        self.assertIn("会话已重命名", str(self.channel.updates[-1][1]))
        self.assertNotIn("迟到候选", str(self.channel.updates))

    async def test_save_waits_for_inflight_card_update_before_final_receipt(self):
        for failure in (None, ThreadLifecycleError("暂时无法改名")):
            with self.subTest(failure=failure is not None):
                updating, release = asyncio.Event(), asyncio.Event()
                original = self.channel.update_card

                async def update(message_id, card):
                    if "正在生成中" in str(card):
                        updating.set()
                        await release.wait()
                    return await original(message_id, card)

                with patch.object(self.channel, "update_card", side_effect=update):
                    pending = await self.generate()
                    await asyncio.wait_for(updating.wait(), 1)
                    generation = self.app._rename_name_generations[self.message_id]
                    rename = self.runtime.rename_exact

                    async def save_name(binding_id, title):
                        if failure is not None:
                            raise failure
                        return await rename(binding_id, title)

                    with patch.object(self.runtime, "rename_exact", side_effect=save_name):
                        save = asyncio.create_task(self.save("已保存"))
                        try:
                            async with asyncio.timeout(1):
                                while not generation.invalidated:
                                    await asyncio.sleep(0)
                            self.assertFalse(save.done())
                        finally:
                            release.set()
                            await asyncio.wait_for(save, 1)
                            await self.complete(pending)
                expected = "会话已重命名" if failure is None else "暂时无法改名"
                self.assertIn(expected, str(self.channel.updates[-1][1]))
                self.assertNotIn("正在生成中", str(self.channel.updates[-1][1]))
                self.assertEqual(self.runtime.generate_name_calls, [])

    async def test_switched_target_never_fills_the_old_card_or_renames_new_binding(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(binding_id):
            self.assertEqual(binding_id, self.binding.id)
            started.set()
            await release.wait()
            return "旧会话候选"

        with patch.object(self.runtime, "generate_name_exact", new=AsyncMock(side_effect=delayed)):
            pending = await self.generate()
            await asyncio.wait_for(started.wait(), 1)
            await self.fixture.create_binding(self.scope)
            release.set()
            result = await self.complete(pending)
        self.assertEqual(_elements(result.card, "input")[0]["default_value"], "")
        self.assertNotIn("旧会话候选", str(result.card))
        self.assertEqual(self.runtime.rename_binding_calls, [])

    async def test_deleted_target_discards_candidate(self):
        async def delete_while_generating(binding_id):
            self.store.delete_binding(binding_id)
            return "已删除会话候选"

        with patch.object(self.runtime, "generate_name_exact", new=AsyncMock(side_effect=delete_while_generating)):
            result = await self.complete(await self.generate())
        self.assertEqual(_elements(result.card, "input")[0]["default_value"], "")
        self.assertIn("名称生成失败", str(result.card))
        self.assertNotIn("已删除会话候选", str(result.card))

    async def test_lifecycle_change_discards_candidate_before_card_update(self):
        async def archive_while_generating(binding_id):
            self.runtime.lifecycle_states[binding_id] = object()
            return "正在归档会话候选"

        with patch.object(self.runtime, "generate_name_exact", new=AsyncMock(side_effect=archive_while_generating)):
            result = await self.complete(await self.generate())
        self.assertEqual(_elements(result.card, "input")[0]["default_value"], "")
        self.assertIn("生成结果已失效", str(result.card))
        self.assertNotIn("正在归档会话候选", str(result.card))

    async def test_failed_loading_card_does_not_start_generation(self):
        self.channel.card_update_results = [SendResult(success=False)]
        await self.complete(await self.generate())
        self.assertEqual(self.runtime.generate_name_calls, [])
        self.assertIn("本次未生成名称", str(self.channel.replies[-1][1]))

    async def test_failed_result_card_does_not_repeat_generation(self):
        self.channel.card_update_results = [SendResult.ok(), SendResult(success=False)]
        await self.complete(await self.generate())
        self.assertEqual(self.runtime.generate_name_calls, [self.binding.id])
        self.assertIn("卡片更新失败", str(self.channel.replies[-1][1]))

    async def test_closing_cancels_waiting_generation_without_redraw(self):
        started = asyncio.Event()

        async def delayed(binding_id):
            started.set()
            await asyncio.Event().wait()

        with patch.object(self.runtime, "generate_name_exact", new=AsyncMock(side_effect=delayed)):
            pending = await self.generate()
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(self.app.close(), 1)
        self.assertTrue(pending.cancelled())
        self.assertEqual(len(self.channel.updates), 1)
        self.assertEqual(self.app._rename_name_generations, {})

    async def test_close_before_generation_starts_releases_inflight_reference(self):
        pending = await self.generate()
        await self.app.close()
        self.assertTrue(pending.cancelled())
        self.assertEqual(self.app._rename_name_generations, {})
        self.assertEqual(self.runtime.generate_name_calls, [])


if __name__ == "__main__":
    unittest.main()
