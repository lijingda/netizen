from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts import netizen_installer as installer


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_IPV4 = re.compile(
    r"(?:^|(?<![0-9]))(?:"
    r"10(?:\.[0-9]{1,3}){3}|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2}|"
    r"192\.168(?:\.[0-9]{1,3}){2}"
    r")(?![0-9])"
)
ABSOLUTE_ACCOUNT_HOME = re.compile(
    r"/(?:Users|home)/(?P<account>[A-Za-z0-9._-]+)"
)
GENERIC_ACCOUNT_NAMES = frozenset({"user", "service-user", "your-user"})
INSTANCE_EXECUTION_RECORD = re.compile(
    r"(?:\b(?:MainPID|NRestarts)\s*=\s*[0-9]+\b)|"
    r"(?:\bPID\s+`?[0-9]{2,}`?\b)|"
    r"(?:\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b)|"
    r"(?:\breleases?/[0-9a-f]{32,64}\b)|"
    r"(?:\bnetizen-previous-[0-9]{8,}\b)|"
    r"(?:channel\.sqlite3\.pre-[0-9]{8,})"
)


class RepositoryHygieneTest(unittest.TestCase):
    def test_adr_numeric_identifiers_are_unique(self) -> None:
        by_identifier: dict[str, list[str]] = {}
        for path in sorted((ROOT / "docs/adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
            by_identifier.setdefault(path.name[:4], []).append(path.name)

        duplicates = {
            identifier: names
            for identifier, names in by_identifier.items()
            if len(names) > 1
        }
        self.assertEqual({}, duplicates)

    def test_local_operator_profile_is_ignored_and_public_template_is_shipped(
        self,
    ) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        manifest = installer.source_manifest(ROOT)
        template = (ROOT / "LOCAL_ENVIRONMENT.example.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("/LOCAL_ENVIRONMENT.md", ignored)
        self.assertIn(".gitignore", manifest)
        self.assertIn("LOCAL_ENVIRONMENT.example.md", manifest)
        self.assertNotIn("LOCAL_ENVIRONMENT.md", manifest)
        self.assertIn("A clean clone does not\nneed it", template)
        self.assertIn("Never place raw Feishu App Secrets", template)
        self.assertIn("Do not make installer", template)

    def test_publishable_source_has_no_machine_specific_coordinates(self) -> None:
        violations: list[str] = []

        for relative in installer.source_manifest(ROOT):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if PRIVATE_IPV4.search(line):
                    violations.append(f"{relative}:{line_number}: private IPv4 address")
                for match in ABSOLUTE_ACCOUNT_HOME.finditer(line):
                    account = match.group("account")
                    if account not in GENERIC_ACCOUNT_NAMES:
                        violations.append(
                            f"{relative}:{line_number}: concrete account home {account!r}"
                        )

        self.assertEqual([], violations)

    def test_public_guidance_has_no_instance_execution_records(self) -> None:
        violations: list[str] = []
        guidance = [
            ROOT / "AGENTS.md",
            ROOT / "CONTEXT.md",
            ROOT / "README.md",
            ROOT / "LOCAL_ENVIRONMENT.example.md",
            *(ROOT / "docs").rglob("*.md"),
        ]

        for path in guidance:
            relative = path.relative_to(ROOT)
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if INSTANCE_EXECUTION_RECORD.search(line):
                    violations.append(
                        f"{relative}:{line_number}: instance execution record"
                    )

        self.assertEqual([], violations)

    def test_end_user_guidance_exposes_only_card_based_new(self) -> None:
        documents = (
            ROOT / "README.md",
            ROOT / "skills/netizen-user-guide/references/user-guide.md",
        )
        retired_forms = (
            re.compile(r"/new\s+<"),
            re.compile(r"/new\s+\["),
            re.compile(r"/new\s+(?:alias|none|test)\b", re.IGNORECASE),
        )

        for document in documents:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.relative_to(ROOT)):
                self.assertIn("/new", text)
                for pattern in retired_forms:
                    self.assertIsNone(pattern.search(text))


if __name__ == "__main__":
    unittest.main()
