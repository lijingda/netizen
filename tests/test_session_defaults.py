from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.chat_targets import ChatTargetError
from netizen_cli.defaults import DefaultConfigurationError
from netizen_cli.defaults.service import SessionDefaultsService
from netizen_cli.domain import MentionContextMode
from netizen_cli.model_settings import EffortOption, ModelCatalog, ModelOption
from netizen_cli.projects import ProjectRegistry
from netizen_cli.session_settings import BindingTurnSettings, SessionSettings
from tests.support.chat_targets import FakeChatTargetDirectory


class SessionDefaultsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.bindings = BindingStore(self.root / "channel.sqlite3")
        self.projects = ProjectRegistry(store=self.bindings, project_root=self.root, projects={"p": self.root})
        self.chats = SimpleNamespace(get_chat_info=AsyncMock(return_value=SimpleNamespace(name="支付 OnCall", chat_mode="topic")))
        self.chat_targets = FakeChatTargetDirectory(self.chats)
        self.catalog = ModelCatalog((ModelOption(
            "model", "native-model", "Model", "", True, "high", "default",
            (EffortOption("high", "High", "high"),), (),
        ),))
        self.runtime = SimpleNamespace(model_catalog=AsyncMock(return_value=self.catalog))
        self.service = self.new_service()

    def new_service(self, app_id="app"):
        return SessionDefaultsService(
            bindings=self.bindings, projects=self.projects, runtime=self.runtime,
            app_id=app_id, chat_info=self.chats,
            chat_target_validator=self.chat_targets.validate_target,
        )

    async def asyncTearDown(self):
        self.bindings.close()
        self.directory.cleanup()

    def save_request(self, **fields):
        return {"mode": "save", "kind": "chat", "chat_id": "oc_chat",
                "expected_revision": None, "project": "p", "session_settings": SessionSettings().to_dict(), **fields}

    async def save(self, **fields):
        return (await self.service.manage(self.save_request(**fields)))["rule"]

    async def group(self, keyword="oncall", **fields):
        return await self.save(kind="group_name", chat_id=None, keyword=keyword, **fields)

    async def listing(self, kind="group_name", **fields):
        return await self.service.manage({"mode": "list", "kind": kind, **fields})

    async def test_exact_precedes_ordered_group_rules_without_name_lookup(self):
        first = await self.group(" OnCALL ")
        second = await self.group("支付")
        self.assertEqual(first["keyword"], "OnCALL")
        self.assertEqual((await self.service.resolve("oc_chat", "group")).id, first["id"])
        listing = await self.listing()
        await self.service.manage({"mode": "reorder", "rule_ids": [second["id"], first["id"]], "order_revision": listing["order_revision"]})
        self.assertEqual((await self.service.resolve("oc_chat", "group")).id, second["id"])
        exact = await self.save()
        self.chats.get_chat_info.reset_mock()
        self.chats.get_chat_info.side_effect = RuntimeError("must not read the name")
        self.assertEqual((await self.service.resolve("oc_chat", "group")).id, exact["id"])
        self.chats.get_chat_info.assert_not_awaited()

    async def test_unconfigured_and_direct_chats_do_not_read_group_name(self):
        self.assertIsNone(await self.service.resolve("empty", "group"))
        await self.group()
        self.assertIsNone(await self.service.resolve("direct", "p2p"))
        self.chats.get_chat_info.assert_not_awaited()
        self.chats.get_chat_info.return_value = SimpleNamespace(chat_mode="p2p", name="oncall")
        self.assertIsNone(await self.service.resolve("unknown"))

    async def test_group_name_and_kind_failures_are_not_silent_misses(self):
        await self.group()
        for value, code in (
            (None, "chat_kind_unknown"),
            (SimpleNamespace(chat_mode="group", name=""), "chat_name_unavailable"),
        ):
            self.chats.get_chat_info.return_value = value
            with self.assertRaises(DefaultConfigurationError) as raised:
                await self.service.resolve("chat", "group")
            self.assertEqual(raised.exception.code, code)
        self.chats.get_chat_info.side_effect = OSError("offline")
        with self.assertRaises(DefaultConfigurationError) as raised:
            await self.service.resolve("chat", "group")
        self.assertEqual(raised.exception.code, "chat_unavailable")

    async def test_invalid_first_match_is_returned_for_validation_without_fallback(self):
        await self.group()
        self.projects.register(alias="other", path=str(self.root), create_directory=False)
        await self.group("支付", project="other")
        self.projects.set_enabled(alias="p", enabled=False, expected_revision=1)
        match = await self.service.resolve("chat", "group")
        self.assertEqual(match.project, "p")
        with self.assertRaises(DefaultConfigurationError) as raised:
            await self.service.validate(match)
        self.assertEqual(raised.exception.code, "project_unavailable")
        # Failed configuration stays visible and can be deleted without checking
        # Project availability or the native model catalog again.
        viewed = await self.service.manage({"mode": "view", "chat_id": "chat"})
        self.assertEqual(viewed["effective"]["id"], match.id)
        await self.service.manage({"mode": "delete", "id": match.id, "expected_revision": 1})
        self.assertEqual((await self.service.resolve("chat", "group")).project, "other")

    async def test_exact_creation_update_and_delete_use_cas(self):
        rule = await self.save()
        with self.assertRaises(DefaultConfigurationError) as raised:
            await self.save()
        self.assertEqual(raised.exception.code, "revision_conflict")
        revised = await self.save(id=rule["id"], expected_revision=1,
            session_settings=SessionSettings().merge({"progress_card_enabled": True}).to_dict())
        self.assertEqual(revised["revision"], 2)
        self.assertTrue(revised["session_settings"]["progress_card_enabled"])
        for mode in ("save", "delete"):
            with self.assertRaises(DefaultConfigurationError) as raised:
                request = self.save_request(id=rule["id"], expected_revision=1) if mode == "save" else {
                    "mode": "delete", "id": rule["id"], "expected_revision": 1}
                await self.service.manage(request)
            self.assertEqual(raised.exception.code, "revision_conflict")
        deleted = await self.service.manage({"mode": "delete", "id": rule["id"], "expected_revision": 2})
        self.assertEqual(deleted, {"deleted": rule["id"]})
        self.assertIsNone(await self.service.resolve("oc_chat"))

    async def test_exact_save_revalidates_same_target_before_mutation(self):
        rule = await self.save()
        self.assertEqual(self.chat_targets.calls, ["oc_chat"])
        self.chats.get_chat_info.assert_awaited_once_with("oc_chat")
        self.chat_targets.errors["oc_chat"] = ChatTargetError("chat_unavailable", "机器人无法访问目标聊天。")
        with self.assertRaises(DefaultConfigurationError) as caught:
            await self.save(id=rule["id"], expected_revision=1)
        self.assertEqual(caught.exception.code, "chat_unavailable")
        self.assertEqual(self.chat_targets.calls, ["oc_chat", "oc_chat"])
        self.assertEqual(self.bindings.defaults.get("app", rule["id"]).revision, 1)

    async def test_exact_save_requires_target_validator_but_group_rules_do_not(self):
        self.service._chat_target_validator = None
        with self.assertRaises(DefaultConfigurationError) as caught:
            await self.save()
        self.assertEqual(caught.exception.code, "chat_query_unavailable")
        self.chats.get_chat_info.assert_not_awaited()
        self.assertIsNone(self.bindings.defaults.exact("app", "oc_chat"))
        self.assertEqual((await self.group())["kind"], "group_name")

    async def test_order_cas_covers_add_delete_and_complete_inventory(self):
        first = await self.group("oncall")
        listing = await self.listing()
        second = await self.group("支付")
        with self.assertRaises(DefaultConfigurationError) as raised:
            await self.service.manage({"mode": "reorder", "rule_ids": [first["id"]], "order_revision": listing["order_revision"]})
        self.assertEqual(raised.exception.code, "revision_conflict")
        listing = await self.listing()
        for ids in ([first["id"]], [first["id"], first["id"]], [first["id"], "foreign"]):
            with self.assertRaises(DefaultConfigurationError):
                await self.service.manage({"mode": "reorder", "rule_ids": ids, "order_revision": listing["order_revision"]})
        await self.service.manage({"mode": "delete", "id": first["id"], "expected_revision": 1})
        remaining = await self.listing()
        self.assertEqual([(item["id"], item["position"]) for item in remaining["items"]], [(second["id"], 0)])
        self.assertGreater(remaining["order_revision"], listing["order_revision"])

    async def test_app_namespace_and_delete_exact_fallback(self):
        group = await self.group()
        exact = await self.save()
        foreign = self.new_service("other-app")
        self.assertIsNone(await foreign.resolve("oc_chat", "group"))
        self.assertEqual((await foreign.manage({"mode": "list", "kind": "chat"}))["items"], [])
        with self.assertRaises(DefaultConfigurationError) as raised:
            await foreign.manage({"mode": "delete", "id": exact["id"], "expected_revision": 1})
        self.assertEqual(raised.exception.code, "not_found")
        with self.assertRaises(DefaultConfigurationError):
            await foreign.manage({"mode": "reorder", "rule_ids": [group["id"]], "order_revision": 1})
        await self.service.manage({"mode": "delete", "id": exact["id"], "expected_revision": 1})
        self.assertEqual((await self.service.resolve("oc_chat", "group")).id, group["id"])

    async def test_card_mutations_cannot_change_another_chat_rule(self):
        rule = await self.save()
        with self.assertRaises(DefaultConfigurationError):
            await self.save(id=rule["id"], expected_revision=1, chat_id="oc_other")
        with self.assertRaises(DefaultConfigurationError):
            await self.service.manage({"mode": "delete", "kind": "chat", "chat_id": "oc_other",
                "id": rule["id"], "expected_revision": 1})
        self.assertEqual((await self.service.resolve("oc_chat")).to_dict(), rule)
        await self.service.manage({"mode": "delete", "kind": "chat", "chat_id": "oc_chat",
            "id": rule["id"], "expected_revision": 1})
        self.assertIsNone(await self.service.resolve("oc_chat"))

    async def test_project_deletion_does_not_delete_rules_or_pin_alias_revision(self):
        rule = await self.save(expected_project_revision=1)
        before = self.projects.preview_delete("p")
        reserved = self.projects.begin_delete(alias="p", expected_revision=1,
            expected_inventory_fingerprint=before.fingerprint)
        self.projects.finish_delete(alias="p", expected_revision=reserved.project.revision,
            expected_inventory_fingerprint=reserved.fingerprint)
        match = await self.service.resolve("oc_chat")
        self.assertEqual(match.to_dict(), rule)
        with self.assertRaises(DefaultConfigurationError):
            await self.service.validate(match)
        self.projects.register(alias="p", path=str(self.root), create_directory=False)
        await self.service.validate(match)
        with self.assertRaises(DefaultConfigurationError):
            await self.save(id=rule["id"], expected_revision=1, expected_project_revision=1)
        revised = await self.save(id=rule["id"], expected_revision=1)
        self.assertEqual(revised["project"], "p")

    async def test_group_capacity_keeps_complete_sort_inventory_bounded(self):
        for index in range(200):
            self.bindings.defaults.save(app_id="app", kind="group_name", chat_id=None,
                keyword=f"keyword-{index}", project="p", session_settings=SessionSettings(),
                rule_id=None, expected_revision=None)
        with self.assertRaises(DefaultConfigurationError) as raised:
            await self.group("overflow")
        self.assertEqual(raised.exception.code, "capacity")
        listing = await self.listing(limit=200)
        self.assertEqual(len(listing["items"]), 200)
        self.assertFalse(listing["has_more"])
        ids = [item["id"] for item in reversed(listing["items"])]
        await self.service.manage({"mode": "reorder", "rule_ids": ids, "order_revision": listing["order_revision"]})
        self.assertEqual([rule.id for rule in self.bindings.defaults.group_rules("app")], ids)

    async def test_explicit_model_validates_on_save_and_use_inheritance_skips_catalog(self):
        explicit = SessionSettings(turn_settings=BindingTurnSettings("model", "high", "default"))
        rule = await self.save(session_settings=explicit.to_dict())
        self.assertEqual(self.runtime.model_catalog.await_count, 1)
        match = await self.service.resolve("oc_chat")
        self.runtime.model_catalog.side_effect = RuntimeError("offline")
        with self.assertRaises(DefaultConfigurationError) as raised:
            await self.service.validate(match)
        self.assertEqual(raised.exception.code, "model_catalog_unavailable")
        viewed = await self.service.manage({"mode": "view", "chat_id": "oc_chat"})
        self.assertEqual(viewed["exact"], rule)
        self.runtime.model_catalog.reset_mock()
        await self.save(id=rule["id"], expected_revision=1)
        await self.service.validate(await self.service.resolve("oc_chat"))
        self.runtime.model_catalog.assert_not_awaited()

    async def test_invalid_model_and_direct_catch_up_rejected(self):
        invalid = SessionSettings(turn_settings=BindingTurnSettings("missing", "high", "default"))
        with self.assertRaises(DefaultConfigurationError) as raised:
            await self.save(session_settings=invalid.to_dict())
        self.assertEqual(raised.exception.code, "invalid_model_settings")
        self.chats.get_chat_info.return_value = SimpleNamespace(chat_mode="p2p")
        with self.assertRaisesRegex(DefaultConfigurationError, "单聊"):
            await self.save(session_settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP).to_dict())

    async def test_save_rechecks_project_after_model_validation_await(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def catalog():
            entered.set()
            await release.wait()
            return self.catalog

        self.runtime.model_catalog.side_effect = catalog
        explicit = SessionSettings(turn_settings=BindingTurnSettings("model", "high", "default"))
        pending = asyncio.create_task(self.save(expected_project_revision=1, session_settings=explicit.to_dict()))
        await entered.wait()
        self.projects.set_enabled(alias="p", enabled=False, expected_revision=1)
        release.set()
        with self.assertRaises(DefaultConfigurationError) as raised:
            await pending
        self.assertEqual(raised.exception.code, "project_unavailable")
        self.assertIsNone(await self.service.resolve("oc_chat"))

    async def test_view_shows_match_errors_without_losing_precise_saved_values(self):
        await self.group()
        self.chats.get_chat_info.return_value = SimpleNamespace(chat_mode="group", name=None)
        viewed = await self.service.manage({"mode": "view", "chat_id": "oc_chat"})
        self.assertIsNone(viewed["effective"])
        self.assertIn("群名称", viewed["match_error"])
        exact = await self.save()
        viewed = await self.service.manage({"mode": "view", "chat_id": "oc_chat"})
        self.assertEqual(viewed["exact"], exact)
        self.assertIsNone(viewed["match_error"])

    async def test_options_p2p_and_model_outage(self):
        options = await self.service.manage({"mode": "options"})
        self.assertTrue(options["context_mode_available"])
        self.assertEqual(options["models"][0]["id"], "model")
        self.assertEqual(options["session_settings"]["turn_settings"]["model_id"], "model")
        self.runtime.model_catalog.side_effect = RuntimeError("offline")
        self.chats.get_chat_info.return_value = SimpleNamespace(chat_mode="p2p")
        options = await self.service.manage({"mode": "options", "chat_id": "oc_chat"})
        self.assertFalse(options["context_mode_available"])
        self.assertEqual(options["models"], [])
        self.assertIsNotNone(options["model_catalog_error"])
        self.assertIsNone(options["session_settings"]["turn_settings"])

    async def test_metadata_persists_and_query_pages_preserve_order(self):
        for chat_id in ("first", "second", "third"):
            await self.save(chat_id=chat_id)
        await self.group()
        validate_channel_database(self.root / "channel.sqlite3")
        first = await self.listing("chat", limit=2)
        second = await self.listing("chat", limit=2, offset=2)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len({item["id"] for item in first["items"] + second["items"]}), 3)
        self.bindings.close()
        self.bindings = BindingStore(self.root / "channel.sqlite3")
        self.service = self.new_service()
        self.assertEqual((await self.service.resolve("unknown", "group")).keyword, "oncall")

    async def test_malformed_management_requests_fail_before_saving(self):
        requests = [
            self.save_request(expected_revision=True), self.save_request(keyword="mixed"),
            self.save_request(kind="group_name", chat_id=None, keyword="  "),
            self.save_request(session_settings={}), self.save_request(disabled=True),
            {"mode": "list", "kind": "all"}, {"mode": "list", "kind": "chat", "limit": 51},
            {"mode": "reorder", "rule_ids": [], "order_revision": True},
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(DefaultConfigurationError):
                await self.service.manage(request)
        self.assertEqual((await self.listing("chat"))["items"], [])

    async def test_parallel_exact_saves_have_one_winner(self):
        results = await asyncio.gather(self.save(), self.save(), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, DefaultConfigurationError) for result in results), 1)


class DefaultsSchemaTest(unittest.TestCase):
    def test_schema_damage_is_rejected_without_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.sqlite3"
            store = BindingStore(path)
            store.close()
            connection = sqlite3.connect(path)
            connection.execute("DROP INDEX session_defaults_chat")
            connection.close()
            before = path.read_bytes()
            for opener in (BindingStore, validate_channel_database):
                with self.assertRaisesRegex(RuntimeError, "defaults identity index"):
                    opener(path)
                self.assertEqual(path.read_bytes(), before)

    def test_previous_schema_is_rejected_without_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.sqlite3"
            store = BindingStore(path)
            store._connection.execute("UPDATE schema_version SET version = 13")
            store.close()
            before = path.read_bytes()
            for opener in (BindingStore, validate_channel_database):
                with self.assertRaisesRegex(RuntimeError, "unsupported.*schema"):
                    opener(path)
                self.assertEqual(path.read_bytes(), before)
