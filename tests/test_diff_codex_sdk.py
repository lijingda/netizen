from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import diff_codex_sdk as diff


class CodexSdkDiffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old = self.package("old")
        self.new = self.package("new")

    def package(self, name: str) -> Path:
        root = self.root / name / "openai_codex"
        (root / "generated").mkdir(parents=True)
        (root / "__init__.py").write_text('raise RuntimeError("TARGET CODE MUST NOT EXECUTE")\n'
                                           'from .api import Client\n__all__ = ["Client"]\n')
        (root / "api.py").write_text('class Client:\n    def run(self, value: str = "old") -> str: ...\n'
                                    'def helper(value: int = 1) -> int: ...\n')
        (root / "client.py").write_text('from .generated.notification_registry import NOTIFICATION_MODELS\n'
                                       'def decode(method):\n    return NOTIFICATION_MODELS.get(method)\n')
        (root / "errors.py").write_text('class SDKError(Exception): ...\n')
        (root / "generated/v2_all.py").write_text(
            'class Item(BaseModel):\n    value: Annotated[str, Field(alias="oldAlias")] = "old"\n'
            '    kind: Literal["old", "shared"]\n'
            'class Choice(Enum):\n    old = "old"\n'
            'class Envelope(RootModel[Item | str]):\n    root: Item | str\n'
            'class Removed(BaseModel): ...\n'
            'Alias = Item\n')
        (root / "generated/notification_registry.py").write_text('NOTIFICATION_MODELS = {"item/old": Item}\n')
        (root / "_private.py").write_text('secret_shape = 1\n')
        return root

    def test_export_signature_model_alias_union_literal_enum_and_error_changes(self) -> None:
        (self.new / "__init__.py").write_text('from .api import Client, added as NewExport\n__all__ = ["Client", "NewExport"]\n')
        (self.new / "api.py").write_text('class Client:\n    async def run(self, value: int = 2, *, flag: bool = True) -> int: ...\n'
                                        '    @property\n    def status(self) -> str: ...\n'
                                        'def added() -> None: ...\n')
        (self.new / "generated/v2_all.py").write_text(
            'class Item(NewBase):\n    value: Annotated[int, Field(alias="newAlias")] = 2\n'
            '    extra: str | None = None\n    kind: Literal["shared", "new"]\n'
            'class Choice(Enum):\n    new = "new"\n'
            'class ModelProviderError(BaseModel):\n    reason: str\n'
            'class Envelope(RootModel[Item | ModelProviderError]):\n    root: Item | ModelProviderError\n'
            'Alias = ModelProviderError\n')
        (self.new / "errors.py").write_text('class SDKError(Exception):\n    def __init__(self, code: int): ...\n'
                                           'class NewRejected(SDKError): ...\n')
        (self.new / "generated/notification_registry.py").write_text('NOTIFICATION_MODELS = {"item/new": Item}\n')
        (self.new / "_private.py").write_text('secret_shape = 2\n')
        result = diff.compare(diff.snapshot(self.old), diff.snapshot(self.new))
        self.assertEqual(result["exports"]["changed"]["__init__.py"]["after"], ["Client", "NewExport"])
        self.assertIn("api.py:helper", result["api"]["removed"])
        self.assertIn("api.py:added", result["api"]["added"])
        client = result["api"]["changed"]["api.py:Client"]["after"]
        self.assertTrue(client["methods"]["run"][0]["async"])
        self.assertIn("flag:bool=True", client["methods"]["run"][0]["parameters"].replace(" ", ""))
        self.assertEqual(client["methods"]["run"][0]["returns"], "int")
        self.assertEqual(client["methods"]["status"][0]["decorators"], ["property"])
        model = result["models"]["changed"]["generated/v2_all.py:Item"]["after"]
        self.assertEqual(model["fields"]["value"]["aliases"], {"alias": "'newAlias'"})
        self.assertEqual(model["fields"]["value"]["default"], "2")
        self.assertEqual(model["fields"]["kind"]["literal_values"], ["'new'", "'shared'"])
        self.assertEqual(model["bases"], ["NewBase"])
        self.assertIn("extra", model["fields"])
        self.assertIn("generated/v2_all.py:Removed", result["models"]["removed"])
        self.assertEqual(result["models"]["changed"]["generated/v2_all.py:Choice"]["after"]["enum_values"], {"new": "'new'"})
        self.assertEqual(result["models"]["changed"]["generated/v2_all.py:Envelope"]["after"]["root_union_members"], ["Item", "ModelProviderError"])
        self.assertEqual(result["aliases"]["changed"]["generated/v2_all.py:Alias"]["after"]["value"], "ModelProviderError")
        self.assertEqual(set(result["notifications"]["added"]), {"item/new"})
        self.assertEqual(set(result["notifications"]["removed"]), {"item/old"})
        self.assertIn("generated/v2_all.py:ModelProviderError", result["errors"]["added"])
        self.assertIn("generated/v2_all.py:Envelope", result["errors"]["added"])
        self.assertIn("errors.py:SDKError", result["errors"]["changed"])
        self.assertIn("errors.py:NewRejected", result["errors"]["added"])
        self.assertIn("_private.py", result["files"]["changed"])
        self.assertNotEqual(result["inventories"]["from"]["source_fingerprint"], result["inventories"]["to"]["source_fingerprint"])
        rendered = diff.markdown(result, "1.0", "2.0")
        self.assertTrue(rendered.startswith(diff.NOTICE))
        self.assertIn("错误类型", rendered)
        self.assertIn("rust-v1.0...rust-v2.0", rendered)

    def test_class_to_alias_is_reported_as_both_removal_and_assignment(self) -> None:
        path = self.new / "generated/v2_all.py"
        path.write_text(path.read_text().replace("class Removed(BaseModel): ...", "Removed = Item"))
        result = diff.compare(diff.snapshot(self.old), diff.snapshot(self.new))
        self.assertIn("generated/v2_all.py:Removed", result["models"]["removed"])
        self.assertEqual(result["aliases"]["added"]["generated/v2_all.py:Removed"]["value"], "Item")

    def test_class_decorators_and_all_method_signatures_are_reported(self) -> None:
        for root, frozen, annotation in ((self.old, False, "str"), (self.new, True, "int")):
            with (root / "api.py").open("a") as stream:
                stream.write(f'@dataclass(frozen={frozen}, kw_only=True)\nclass Context:\n')
                for name in ("__aenter__", "__aexit__", "__enter__", "__iter__", "__post_init__", "_helper"):
                    stream.write(f'    def {name}(self, value: {annotation}) -> {annotation}: ...\n')
        change = diff.compare(diff.snapshot(self.old), diff.snapshot(self.new))["api"]["changed"]["api.py:Context"]
        self.assertEqual(change["after"]["decorators"], ["dataclass(frozen=True, kw_only=True)"])
        for method in change["after"]["methods"].values():
            self.assertEqual(method[0]["parameters"], "self, value: int")
        self.assertEqual(len(change["after"]["methods"]), 6)

    def test_failure_and_failed_types_are_in_error_inventory(self) -> None:
        with (self.new / "generated/v2_all.py").open("a") as stream:
            stream.write('class ImageGenerationFailure(BaseModel): ...\nclass OperationFailed(BaseModel): ...\n')
        errors = diff.compare(diff.snapshot(self.old), diff.snapshot(self.new))["errors"]["added"]
        self.assertIn("generated/v2_all.py:ImageGenerationFailure", errors)
        self.assertIn("generated/v2_all.py:OperationFailed", errors)

    def test_computed_version_export_records_local_factory_without_executing_it(self) -> None:
        path = self.old / "__init__.py"
        path.write_text(path.read_text().replace('["Client"]', '["Client", "__version__"]') +
                        'from ._version import __version__\n')
        version = self.old / "_version.py"
        version.write_text('def package_version() -> str:\n    raise RuntimeError("must not execute")\n'
                           '__version__ = package_version()\n')
        value = diff.snapshot(self.old)["api"]["__init__.py:__version__"]
        self.assertEqual(value["value"], "package_version()")
        self.assertEqual(value["factory"]["definition_module"], "_version.py")
        self.assertEqual(value["factory"]["overloads"][0]["returns"], "str")
        version.write_text('__version__ = unknown_factory()\n')
        with self.assertRaises(diff.StructureError):
            diff.snapshot(self.old)

    def test_private_definitions_follow_export_chains_and_fail_closed(self) -> None:
        for root in (self.old, self.new):
            with (root / "__init__.py").open("a") as stream:
                stream.write('from .api import ExternalMessage, Sandbox\n')
            path = root / "__init__.py"
            path.write_text(path.read_text().replace('["Client"]', '["Client", "ExternalMessage", "Sandbox"]'))
            with (root / "api.py").open("a") as stream:
                stream.write('from ._inputs import Message as ExternalMessage\nfrom ._sandbox import Sandbox\n')
            (root / "_inputs.py").write_text('class Message:\n    text: str\n')
            (root / "_sandbox.py").write_text('class Sandbox(Enum):\n    read_only = "read-only"\n')
        with (self.new / "_inputs.py").open("a") as stream:
            stream.write('    sender: str\n')
        with (self.new / "_sandbox.py").open("a") as stream:
            stream.write('    workspace_write = "workspace-write"\n')
        result = diff.compare(diff.snapshot(self.old), diff.snapshot(self.new))
        message = result["api"]["changed"]["__init__.py:ExternalMessage"]["after"]
        self.assertEqual(message["definition_module"], "_inputs.py")
        self.assertIn("sender", message["fields"])
        self.assertIsNone(message["fields"]["sender"]["default"])
        self.assertIn("workspace_write", result["api"]["changed"]["__init__.py:Sandbox"]["after"]["enum_values"])
        for source in ('from .missing import ExternalMessage',
                       'from .api import ExternalMessage', 'ExternalMessage = unknown()'):
            with self.subTest(source=source):
                (self.new / "_inputs.py").write_text(source + '\n')
                with self.assertRaises(diff.StructureError):
                    diff.snapshot(self.new)

    def test_missing_or_unexpected_anchors_fail_closed(self) -> None:
        changes = {
            "__init__.py": ('', '__all__ = compute_exports()', '__all__ = []', '__all__ = ["Client"]\n__all__ = ["Other"]', '__all__ = ["Client"]\n__all__.append("X")'),
            "generated/v2_all.py": ('', 'def not_a_class(): ...', 'class Invalid syntax'),
            "generated/notification_registry.py": ('', 'NOTIFICATION_MODELS = {}', 'NOTIFICATION_MODELS = compute()',
                'NOTIFICATION_MODELS = {**other}', 'NOTIFICATION_MODELS = {"x": Item}\nNOTIFICATION_MODELS["y"] = Item'),
            "client.py": ('', 'from .generated.notification_registry import NOTIFICATION_MODELS'),
        }
        for filename, sources in changes.items():
            path = self.new / filename
            original = path.read_text()
            for source in sources:
                with self.subTest(filename=filename, source=source), self.assertRaises(diff.StructureError):
                    path.write_text(source)
                    diff.snapshot(self.new)
            path.write_text(original)
        (self.new / "generated/v2_all.py").unlink()
        with self.assertRaises(diff.StructureError):
            diff.snapshot(self.new)

    def test_cli_offline_never_downloads_or_executes_targets_and_failure_is_explicit(self) -> None:
        output, structured = self.root / "diff.md", self.root / "diff.json"
        argv = ["--from", "1.0", "--to", "2.0", "--from-dir", str(self.old), "--to-dir", str(self.new),
                "--output", str(output), "--json", str(structured)]
        with patch.object(diff.subprocess, "run", side_effect=AssertionError("offline must not spawn")):
            self.assertEqual(diff.main(argv), 0)
        self.assertTrue(output.read_text().startswith(diff.NOTICE))
        self.assertEqual(json.loads(structured.read_text())["models"], {"added": {}, "removed": {}, "changed": {}})
        (self.new / "__init__.py").unlink()
        with redirect_stderr(io.StringIO()) as stderr, redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(diff.main(argv[:-4]), 1)
        self.assertIn(diff.STRUCTURE_ERROR, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_operational_errors_are_distinct_and_hide_pip_output(self) -> None:
        argv = ["--from", "1.0", "--to", "2.0"]
        secret = "PRIVATE_INDEX_SENTINEL"
        for failure, expected in ((subprocess.CompletedProcess([], 1, secret, secret), "pip 下载失败"),
                                  (subprocess.TimeoutExpired("pip", 180, output=secret, stderr=secret), "pip 下载超时")):
            with self.subTest(expected=expected), redirect_stderr(io.StringIO()) as stderr:
                with patch.object(diff.subprocess, "run", **({"side_effect": failure} if isinstance(failure, Exception)
                                                             else {"return_value": failure})):
                    self.assertEqual(diff.main(argv), 1)
                self.assertIn(expected, stderr.getvalue())
                self.assertIn("--from-wheel", stderr.getvalue())
                self.assertNotIn(diff.STRUCTURE_ERROR, stderr.getvalue())
                self.assertNotIn(secret, stderr.getvalue())
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(diff.main(argv + ["--from-dir", str(self.old), "--to-dir", str(self.new),
                                              "--output", str(self.root)]), 1)
        self.assertIn("输出文件写入失败", stderr.getvalue())
        self.assertIn("权限", stderr.getvalue())
        self.assertNotIn(diff.STRUCTURE_ERROR, stderr.getvalue())

    def wheel(self, filename="fixture.whl", version="1.0") -> Path:
        wheel = self.root / filename
        with zipfile.ZipFile(wheel, "w") as archive:
            for path in self.old.rglob("*.py"):
                archive.write(path, path.relative_to(self.old.parent))
            archive.writestr(f"openai_codex-{version}.dist-info/METADATA", f"Name: openai-codex\nVersion: {version}\n")
        return wheel

    def test_offline_wheel_and_metadata_validation(self) -> None:
        temporary = self.root / "wheel-input"
        temporary.mkdir()
        package = diff.acquire("1.0", self.wheel(), None, temporary)
        self.assertGreater(diff.snapshot(package)["counts"]["generated_classes"], 0)
        mismatch = self.root / "mismatch"
        mismatch.mkdir()
        with self.assertRaises(diff.StructureError):
            diff.acquire("2.0", self.root / "fixture.whl", None, mismatch)

    def test_wheel_rejects_traversal_symlink_and_duplicate_paths(self) -> None:
        for index, name in enumerate(("../escaped.py", "/absolute.py", "x\\escape.py", "C:/escape.py", "link")):
            wheel = self.root / f"unsafe-{index}.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                info = zipfile.ZipInfo(name)
                if name == "link":
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"target")
            with self.assertRaises(diff.StructureError):
                diff.extract_wheel(wheel, self.root / f"extracted-{index}")
        self.assertFalse((self.root / "escaped.py").exists())
        wheel = self.root / "duplicate.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("x.py", "")
            archive.writestr("./x.py", "")
        with self.assertRaises(diff.StructureError):
            diff.extract_wheel(wheel, self.root / "duplicate")

    def test_directory_symlinks_fail_without_following(self) -> None:
        (self.new / "foreign.py").symlink_to(self.old / "api.py")
        with self.assertRaises(diff.StructureError):
            diff.snapshot(self.new)

    def test_fingerprint_matches_existing_adapter_without_importing_target(self) -> None:
        from netizen_cli.terminal_cleanup import _source_tree_fingerprint
        (self.old / "generated.py").write_text("sibling = True\n")
        self.assertEqual(diff.snapshot(self.old)["source_fingerprint"], _source_tree_fingerprint(self.old))

    def test_download_uses_own_pip_and_only_sdk_wheel(self) -> None:
        temporary = self.root / "download-input"
        temporary.mkdir()
        wheel = self.wheel()

        def fake_download(command, **kwargs):
            self.assertEqual(command[1:6], ["-m", "pip", "download", "--no-deps", "--only-binary=:all:"])
            self.assertIn("--ignore-requires-python", command)
            self.assertIn("--dest", command)
            self.assertEqual(command[-1], "openai-codex==1.0")
            self.assertNotIn("env", kwargs)
            self.assertNotIn("--index-url", command)
            (Path(command[-2]) / "downloaded.whl").write_bytes(wheel.read_bytes())
            return type("Result", (), {"returncode": 0})()

        with patch.object(diff.subprocess, "run", side_effect=fake_download):
            self.assertGreater(diff.snapshot(diff.acquire("1.0", None, None, temporary))["counts"]["notifications"], 0)


if __name__ == "__main__":
    unittest.main()
