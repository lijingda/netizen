from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from netizen_cli.attachments import AttachmentInput, AttachmentStore
from netizen_cli.management.blocking_io import BlockingIOResultUnknown
from tests.admin import test_project_files_web as project_tests


class AttachmentFilesWebTest(unittest.IsolatedAsyncioTestCase):
    close_runner = project_tests.ProjectFilesWebTest.close_runner
    request = project_tests.ProjectFilesWebTest.request
    _request = project_tests.ProjectFilesWebTest._request
    assert_private_response = project_tests.ProjectFilesWebTest.assert_private_response

    async def asyncSetUp(self):
        await project_tests.ProjectFilesWebTest.asyncSetUp(self)
        self.attachments = AttachmentStore(self.fixture.project_root.resolve() / "attachments")
        self.management.attachments._store = self.attachments
        self.batch = self.attachments.prepare([AttachmentInput("hello.md", b"# Hello"),
                                                AttachmentInput("archive.zip", b"PK\0binary")])
        self.attachments.publish(self.batch)
        status, _, root = await self.get("root")
        self.assertEqual(status, 200, root)
        self.identity = root["identity"]

    def target(self, operation, **query):
        return f"/api/v1/attachment-files/{operation}?" + urlencode(query)

    async def get(self, operation, **query):
        status, headers, body = await self.request(self.target(operation, **query))
        return status, headers, json.loads(body)

    async def entry(self, index=0):
        status, _, data = await self.get("directory", identity=self.identity, path=self.batch.batch_id)
        self.assertEqual(status, 200, data)
        return data["entries"][index]

    async def grant(self, entry):
        status, _, data = await self.get("delete-options", identity=self.identity,
                                         path=entry["path"], version=entry["version"])
        self.assertEqual(status, 200, data)
        return data["action"]

    async def delete(self, action):
        body = {key: action[key] for key in ("csrfToken", "actionToken", "target")}
        status, headers, data = await self.request("/api/v1/attachment-files/delete", method="POST",
                                                  body=json.dumps(body).encode())
        return status, headers, json.loads(data)

    async def test_all_attachment_routes_require_authentication_and_no_arbitrary_root(self):
        for operation in ("root", "directory", "content", "image", "delete-options"):
            status, headers, _ = await self.request(self.target(operation), authenticated=False)
            self.assertEqual(status, 401)
            self.assert_private_response(headers)
        status, _, _ = await self.request("/api/v1/attachment-files/delete", authenticated=False,
                                          method="POST", body=b"{}")
        self.assertEqual(status, 401)
        status, _, _ = await self.get("root", cwd="/etc")
        self.assertEqual(status, 400)
        for operation in ("upload", "download", "delete-directory"):
            status, _, _ = await self.request(self.target(operation))
            self.assertEqual(status, 404)

    async def test_owned_listing_and_shared_inert_preview(self):
        status, headers, listing = await self.get("directory", identity=self.identity, path="")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["entries"]), 1)
        self.assert_private_response(headers)
        entry = await self.entry()
        status, _, content = await self.get("content", identity=self.identity, path=entry["path"])
        self.assertEqual(status, 200, content)
        self.assertEqual(content["markdownHtml"], "<h1>Hello</h1>\n")
        status, _, _ = await self.get("content", identity=self.identity,
                                     path=self.batch.batch_id + "/.manifest.json")
        self.assertEqual(status, 400)

    async def test_unsupported_preview_still_deletes_with_single_use_authorization(self):
        entry = await self.entry(1)
        status, _, _ = await self.get("content", identity=self.identity, path=entry["path"])
        self.assertEqual(status, 415)
        action = await self.grant(entry)
        status, headers, data = await self.delete(action)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["deleted"])
        self.assertFalse(self.batch.files[1].path.exists())
        self.assert_private_response(headers)
        status, _, data = await self.delete(action)
        self.assertEqual((status, data["code"]), (409, "stale_or_consumed"))

    async def test_csrf_and_target_substitution_do_not_delete(self):
        entry = await self.entry()
        action = await self.grant(entry)
        forged = {**action, "csrfToken": "invalid"}
        status, _, data = await self.delete(forged)
        self.assertEqual((status, data["code"]), (403, "csrf_rejected"))
        action = await self.grant(entry)
        forged = {**action, "target": {**action["target"], "targetId": self.batch.batch_id + "/02-archive.zip"}}
        status, _, _ = await self.delete(forged)
        self.assertEqual(status, 409)
        self.assertTrue(all(item.path.exists() for item in self.batch.files))

    async def test_empty_directory_cleanup_failure_does_not_report_delete_failure(self):
        status, _, _ = await self.delete(await self.grant(await self.entry(1)))
        self.assertEqual(status, 200)
        action = await self.grant(await self.entry())
        with patch("netizen_cli.attachments.os.rmdir", side_effect=OSError("cleanup failed")):
            status, _, result = await self.delete(action)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["removedBatch"], "browser must return to root when the manifest is gone")
        self.assertTrue(self.batch.files[0].path.parent.is_dir())
        status, _, listing = await self.get("directory", identity=self.identity, path="")
        self.assertEqual(status, 200, listing)
        self.assertEqual(listing["entries"], [])

    async def test_changed_file_and_changed_root_invalidate_old_authorization(self):
        entry = await self.entry()
        action = await self.grant(entry)
        self.batch.files[0].path.write_text("changed")
        status, _, data = await self.delete(action)
        self.assertEqual((status, data["code"]), (409, "file_changed"))
        action = await self.grant(await self.entry())
        self.attachments.retained.rename(self.attachments.root / "old-retained")
        self.attachments.retained.mkdir()
        status, _, data = await self.delete(action)
        self.assertEqual((status, data["code"]), (409, "root_changed"))

    async def test_delete_timeout_uses_unknown_outcome_and_consumes_grant(self):
        action = await self.grant(await self.entry())
        with patch.object(self.management.attachments, "delete", new=AsyncMock(
            side_effect=BlockingIOResultUnknown(deadline=asyncio.get_running_loop().time()))):
            status, _, data = await self.delete(action)
        self.assertEqual(status, 503, data)
        self.assertNotEqual(data["code"], "read_timeout")
        status, _, data = await self.delete(action)
        self.assertEqual((status, data["code"]), (409, "stale_or_consumed"))
