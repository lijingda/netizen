from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netizen.lark_app import (
    LarkAppConfigError,
    encode_lark_app,
    load_lark_app,
    parse_lark_app,
)


class LarkAppCredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.json"
        self.secret = "private-app-secret"
        self.path.write_bytes(encode_lark_app("cli_test", self.secret))
        self.path.chmod(0o600)

    def test_fixed_profile_ignores_cli_current_selection(self) -> None:
        document = json.loads(self.path.read_bytes())
        document["apps"].insert(0, {
            "name": "other", "appId": "cli_other", "appSecret": "other-secret",
            "brand": "feishu",
        })
        document["currentApp"] = "other"
        self.path.write_text(json.dumps(document))
        credentials = load_lark_app(self.path)
        self.assertEqual(credentials.app_id, "cli_test")
        self.assertEqual(credentials.app_secret, self.secret)
        self.assertNotIn(self.secret, repr(credentials))

    def test_incomplete_profile_can_only_be_read_for_installation(self) -> None:
        for app_id in ("", "cli_existing"):
            with self.subTest(app_id=app_id):
                content = encode_lark_app(app_id, "")
                self.assertEqual(parse_lark_app(content, allow_incomplete=True).app_id, app_id)
                with self.assertRaises(LarkAppConfigError):
                    parse_lark_app(content)

    def test_invalid_profile_does_not_disclose_credential_material(self) -> None:
        original = json.loads(self.path.read_bytes())
        profile = original["apps"][0]
        invalid = [
            b'{"apps": ["' + self.secret.encode(),
            b'{"apps":[],"apps":[]}',
            json.dumps({"apps": [profile, profile]}).encode(),
            json.dumps({"apps": [{**profile, "name": "other"}]}).encode(),
            json.dumps({"apps": [{**profile, "brand": "lark"}]}).encode(),
            json.dumps({"apps": [{**profile, "appId": self.secret}]}).encode(),
            json.dumps({"apps": [{**profile, "appSecret": {"source": "file", "id": self.secret}}]}).encode(),
            json.dumps({"apps": [{**profile, "appSecret": self.secret + "\n"}]}).encode(),
            self.secret.encode() * 6000,
        ]
        for index, content in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(LarkAppConfigError) as caught:
                parse_lark_app(content)
            self.assertNotIn(self.secret, str(caught.exception))

    def test_missing_relative_symlink_and_unsafe_permissions_are_rejected(self) -> None:
        for path in (self.path.parent / "missing", Path("relative.json")):
            with self.subTest(path=path), self.assertRaises(LarkAppConfigError):
                load_lark_app(path)
        link = self.path.parent / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(LarkAppConfigError):
            load_lark_app(link)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(LarkAppConfigError, "0600"):
            load_lark_app(self.path)

    def test_non_regular_or_other_account_file_is_rejected(self) -> None:
        for metadata in (
            SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.geteuid() + 1),
            SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_uid=os.geteuid()),
        ):
            with patch("netizen.lark_app.os.fstat", return_value=metadata):
                with self.assertRaises(LarkAppConfigError):
                    load_lark_app(self.path)

    def test_atomic_credential_replacement_is_observed_on_next_load(self) -> None:
        replacement = self.path.with_name("replacement.json")
        replacement.write_bytes(encode_lark_app("cli_rebound", "new-secret"))
        replacement.chmod(0o600)
        os.replace(replacement, self.path)
        credentials = load_lark_app(self.path)
        self.assertEqual((credentials.app_id, credentials.app_secret), ("cli_rebound", "new-secret"))


if __name__ == "__main__":
    unittest.main()
