from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


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
ADR_DIRECTORY = ROOT / "docs/adr"
ADR_RECIPROCAL_FIELDS = (("amends", "amended_by"), ("supersedes", "superseded_by"))
ADR_INDEX_STATUS_LABELS = {"proposed": "草案", "accepted": "有效", "superseded": "已被"}
ADR_INDEX_ROW = re.compile(r"^\| \[(?P<identifier>[0-9]{4})\]\((?P<target>[^)]+)\) \|")
ADR_INDEX_LINK = re.compile(r"\[(?P<identifier>[0-9]{4})\]\(")


def adr_paths() -> list[Path]:
    return sorted(ADR_DIRECTORY.glob("[0-9][0-9][0-9][0-9]-*.md"))


def adr_front_matter() -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    for path in adr_paths():
        _, header, _ = path.read_text(encoding="utf-8").split("---\n", 2)
        records[path.name[:4]] = dict(
            line.split(": ", 1) for line in header.splitlines() if line
        )
    return records


def adr_references(record: dict[str, str], field: str) -> set[str]:
    return set(re.findall(r"[0-9]{4}", record.get(field, "")))


def source_files() -> list[str]:
    """Inspect versioned and not-yet-committed public files, never ignored data."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return sorted({name for name in result.stdout.split("\0")
                   if name and (ROOT / name).is_file() and not (ROOT / name).is_symlink()})


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

    def test_adr_revision_links_are_reciprocal(self) -> None:
        records = adr_front_matter()
        violations: list[str] = []

        for identifier, record in records.items():
            status = record.get("status")
            if status not in ADR_INDEX_STATUS_LABELS:
                violations.append(f"{identifier}: unknown status {status!r}")
            if (status == "superseded") != bool(adr_references(record, "superseded_by")):
                violations.append(f"{identifier}: status {status!r} disagrees with superseded_by")
            for forward, backward in ADR_RECIPROCAL_FIELDS:
                # A draft declares its revisions only on itself until it is accepted.
                for target in sorted(adr_references(record, forward)):
                    if target not in records:
                        violations.append(f"{identifier}: {forward} unknown ADR {target}")
                    elif status != "proposed" and identifier not in adr_references(
                        records[target], backward
                    ):
                        violations.append(f"{identifier} {forward} {target}, {target} lacks {backward}")
                for source in sorted(adr_references(record, backward)):
                    if source not in records:
                        violations.append(f"{identifier}: {backward} unknown ADR {source}")
                    elif records[source].get("status") == "proposed":
                        violations.append(f"{identifier}: {backward} lists draft {source}")
                    elif identifier not in adr_references(records[source], forward):
                        violations.append(f"{identifier} {backward} {source}, {source} lacks {forward}")

        self.assertEqual([], violations)

    def test_adr_index_shows_each_status_and_incoming_revision(self) -> None:
        records = adr_front_matter()
        files = {path.name[:4]: path.name for path in adr_paths()}
        rows: dict[str, list[str]] = {}
        violations: list[str] = []

        index = (ADR_DIRECTORY / "README.md").read_text(encoding="utf-8")
        for line in index.splitlines():
            match = ADR_INDEX_ROW.match(line)
            if match is None:
                continue
            identifier = match.group("identifier")
            if identifier in rows:
                violations.append(f"{identifier}: listed more than once")
            if match.group("target") != files.get(identifier):
                violations.append(f"{identifier}: index links {match.group('target')}")
            rows[identifier] = line.split(" | ")

        for identifier, record in records.items():
            cells = rows.get(identifier)
            if cells is None:
                violations.append(f"{identifier}: missing from index")
                continue
            label = ADR_INDEX_STATUS_LABELS.get(record.get("status", ""), "")
            if len(cells) < 5 or not cells[3].startswith(label):
                violations.append(f"{identifier}: index status disagrees with {record.get('status')!r}")
            linked = set(ADR_INDEX_LINK.findall(" | ".join(cells[3:])))
            incoming = adr_references(record, "amended_by") | adr_references(record, "superseded_by")
            for source in sorted(incoming - linked):
                violations.append(f"{identifier}: index omits revision by {source}")

        self.assertEqual([], violations)

    def test_local_operator_profile_is_ignored_and_public_template_is_shipped(
        self,
    ) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        manifest = source_files()

        self.assertIn("/LOCAL_ENVIRONMENT.md", ignored)
        self.assertIn(".gitignore", manifest)
        self.assertIn("LOCAL_ENVIRONMENT.example.md", manifest)
        self.assertNotIn("LOCAL_ENVIRONMENT.md", manifest)
        self.assertFalse(any(name.startswith("docs/research/") for name in manifest))

    def test_publishable_source_has_no_machine_specific_coordinates(self) -> None:
        violations: list[str] = []

        for relative in source_files():
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
            *sorted((ROOT / "skills/netizen-user-guide/references").rglob("*.md")),
        )
        retired_forms = (
            re.compile(r"/new\s+<"),
            re.compile(r"/new\s+\["),
            re.compile(r"/new\s+(?:alias|none|test)\b", re.IGNORECASE),
        )

        for document in documents:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.relative_to(ROOT)):
                for pattern in retired_forms:
                    self.assertIsNone(pattern.search(text))


if __name__ == "__main__":
    unittest.main()
