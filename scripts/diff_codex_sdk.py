#!/usr/bin/env python3
"""Build raw SDK comparison material without importing either target package.

Versions are explicit labels, also checked against wheel METADATA. Offline --*-dir
accepts an openai_codex package directory or its parent; --*-wheel uses local wheels.
Only the SDK wheel is downloaded, using the invoking Python's own pip settings.
"""
from __future__ import annotations

import argparse
import ast
from email.parser import Parser
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any
import zipfile

from netizen_cli.terminal_cleanup import _source_tree_fingerprint

NOTICE = "这是原始差异材料，不判断重要性；无差异不代表原生执行行为无变化。"
STRUCTURE_ERROR = "脚本不适用于此版本结构：本次升级需人工对比，并在本次升级中修正脚本"


class StructureError(ValueError):
    pass


class DownloadError(RuntimeError):
    pass


class OutputError(OSError):
    pass


def expression(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def assignments(body: list[ast.stmt]) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for node in body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values[node.target.id] = node.value
    return values


def string_list(node: ast.AST, known: dict[str, ast.AST], seen: frozenset[str] = frozenset()) -> list[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        result = []
        for item in node.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                result.append(item.value)
            elif isinstance(item, ast.Starred):
                result.extend(string_list(item.value, known, seen))
            else:
                raise StructureError("无法静态解析 __all__ 元素")
        return result
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return string_list(node.left, known, seen) + string_list(node.right, known, seen)
    if isinstance(node, ast.Name) and node.id in known and node.id not in seen:
        return string_list(known[node.id], known, seen | {node.id})
    raise StructureError("无法静态解析 __all__；禁止执行包代码")


def literal_values(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    return sorted({ast.unparse(item) for part in ast.walk(node)
                   if isinstance(part, ast.Subscript) and expression(part.value) in {"Literal", "typing.Literal"}
                   for item in (part.slice.elts if isinstance(part.slice, ast.Tuple) else [part.slice])})


def union_members(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Subscript) and expression(node.value) in {"Annotated", "typing.Annotated"}:
        if not isinstance(node.slice, ast.Tuple) or not node.slice.elts:
            raise StructureError("未知 Annotated 结构")
        return union_members(node.slice.elts[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return union_members(node.left) + union_members(node.right)
    if isinstance(node, ast.Subscript) and expression(node.value) in {"Union", "typing.Union"}:
        return [ast.unparse(item) for item in (node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice])]
    return [ast.unparse(node)]


def function_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    return {"async": isinstance(node, ast.AsyncFunctionDef), "parameters": ast.unparse(node.args),
            "returns": expression(node.returns), "decorators": [ast.unparse(x) for x in node.decorator_list]}


def class_shape(node: ast.ClassDef) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    methods: dict[str, Any] = {}
    for child in node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            name, annotation, default = child.target.id, child.annotation, child.value
        elif isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
            name, annotation, default = child.targets[0].id, None, child.value
        else:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.setdefault(child.name, []).append(function_shape(child))
            continue
        if name.startswith("_"):
            continue
        aliases = {}
        for part in (annotation, default):
            if part is not None:
                for item in ast.walk(part):
                    if isinstance(item, ast.Call) and expression(item.func) in {"Field", "pydantic.Field"}:
                        aliases.update({key.arg: expression(key.value) for key in item.keywords
                                        if key.arg in {"alias", "validation_alias", "serialization_alias"}})
        fields[name] = {"annotation": expression(annotation), "default": expression(default),
                        "aliases": aliases, "literal_values": literal_values(annotation)}
    roots = []
    for base in node.bases:
        if isinstance(base, ast.Subscript) and expression(base.value) in {"RootModel", "pydantic.RootModel"}:
            roots = union_members(base.slice)
    return {"bases": [ast.unparse(x) for x in node.bases], "keywords": [ast.unparse(x) for x in node.keywords],
            "decorators": [ast.unparse(x) for x in node.decorator_list],
            "fields": fields, "methods": methods, "root_union_members": roots,
            "enum_values": {name: value["default"] for name, value in fields.items()}
            if any(expression(base) in {"Enum", "IntEnum", "StrEnum", "enum.Enum", "enum.IntEnum", "enum.StrEnum"}
                   for base in node.bases) else {}}


def safe_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise StructureError("包目录不存在或为 symlink")
    paths = sorted(root.rglob("*"))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
        raise StructureError("包目录包含 symlink 或非普通文件")
    return [path for path in paths if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]


def exported_shape(trees: dict[str, ast.Module], module: str, name: str,
                   seen: frozenset[tuple[str, str]] = frozenset()) -> dict[str, Any]:
    """Follow explicit in-package imports without importing any target code."""
    if (module, name) in seen or module not in trees:
        raise StructureError(f"无法解析导出定义（模块缺失或循环）：{module}:{name}")
    seen = seen | {(module, name)}
    body = trees[module].body
    definitions = [node for node in body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == name]
    imports = [(node, alias) for node in body if isinstance(node, ast.ImportFrom)
               for alias in node.names if (alias.asname or alias.name) == name]
    value = assignments(body).get(name)
    if sum((bool(definitions), bool(imports), value is not None)) != 1:
        raise StructureError(f"无法唯一解析导出定义：{module}:{name}")
    if imports:
        if len(imports) != 1:
            raise StructureError(f"导出有多个 import：{module}:{name}")
        node, alias = imports[0]
        if node.level:
            package = module.split("/")[:-1]
            if node.level > len(package) + 1:
                raise StructureError(f"导出 import 越出包：{module}:{name}")
            parts = package[:len(package) + 1 - node.level] + (node.module or "").split(".")
        elif node.module and node.module.startswith("openai_codex."):
            parts = node.module.split(".")[1:]
        else:
            raise StructureError(f"导出来自无法静态解析的外部模块：{module}:{name}")
        path = "/".join(part for part in parts if part)
        candidates = [candidate for candidate in (path + ".py", path + "/__init__.py") if candidate in trees]
        if len(candidates) != 1:
            raise StructureError(f"导出 import 无唯一模块：{module}:{name}")
        return exported_shape(trees, candidates[0], alias.name, seen)
    origin = {"definition_module": module, "definition_name": name}
    if definitions:
        if all(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in definitions):
            return {**origin, "kind": "function", "overloads": [function_shape(node) for node in definitions]}
        if len(definitions) == 1 and isinstance(definitions[0], ast.ClassDef):
            return {**origin, "kind": "class", **class_shape(definitions[0])}
    elif isinstance(value, (ast.Constant, ast.Name, ast.Attribute, ast.Subscript, ast.BinOp)):
        return {**origin, "kind": "assignment", "value": expression(value),
                "union_members": union_members(value), "literal_values": literal_values(value)}
    elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        factory = exported_shape(trees, module, value.func.id, seen)
        if factory["kind"] == "function":
            return {**origin, "kind": "computed_assignment", "value": expression(value), "factory": factory}
    raise StructureError(f"无法静态解析导出定义：{module}:{name}")


def snapshot(directory: Path) -> dict[str, Any]:
    root = directory if directory.name == "openai_codex" else directory / "openai_codex"
    files = safe_files(root)
    trees: dict[str, ast.Module] = {}
    hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        source = path.read_bytes()
        hashes[relative] = hashlib.sha256(source).hexdigest()
        if path.suffix == ".py":
            try:
                trees[relative] = ast.parse(source, filename=relative)
            except (SyntaxError, ValueError) as error:
                raise StructureError(f"无法静态解析 {relative}: {error}") from error
    if "__init__.py" not in trees or "__all__" not in assignments(trees["__init__.py"].body):
        raise StructureError("缺少包入口或 __all__ 锚点")
    if "generated/v2_all.py" not in trees or "client.py" not in trees:
        raise StructureError("缺少 generated/v2_all.py 或 client.py 锚点")
    exports = {}
    api = {}
    models = {}
    aliases = {}
    for relative, tree in trees.items():
        values = assignments(tree.body)
        if "__all__" in values:
            # Dynamic post-assignment mutations are not a supported export shape.
            if sum(isinstance(node, ast.Name) and node.id == "__all__" and isinstance(node.ctx, (ast.Store, ast.Del))
                   for node in ast.walk(tree)) != 1:
                raise StructureError(f"{relative}: __all__ 不是唯一静态赋值")
            for node in ast.walk(tree):
                if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
                    raise StructureError(f"{relative}: 动态 __all__ 增量")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "__all__":
                    raise StructureError(f"{relative}: 动态 __all__ 修改")
            exports[relative] = sorted(set(string_list(values["__all__"], values)))
        generated = relative.startswith("generated/")
        public_module = all(not part.startswith("_") or part == "__init__.py" for part in relative.split("/"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                key = f"{relative}:{node.name}"
                if generated:
                    models[key] = class_shape(node)
                elif public_module:
                    api[key] = {"kind": "class", **class_shape(node)}
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_") and public_module and not generated:
                api.setdefault(f"{relative}:{node.name}", {"kind": "function", "overloads": []})["overloads"].append(function_shape(node))
        for name, value in values.items():
            if not name.startswith("_") and isinstance(value, (ast.Name, ast.Attribute, ast.Subscript, ast.BinOp)):
                aliases[f"{relative}:{name}"] = {"value": ast.unparse(value), "union_members": union_members(value),
                                                "literal_values": literal_values(value)}
    for module, names in exports.items():
        for name in names:
            api[f"{module}:{name}"] = exported_shape(trees, module, name)
    if not exports.get("__init__.py") or not api or not models:
        raise StructureError("公开导出、高层类型或生成模型类数为 0")
    client = trees["client.py"]
    sources = [(node, alias) for node in client.body if isinstance(node, ast.ImportFrom)
               for alias in node.names if alias.name == "NOTIFICATION_MODELS"]
    if len(sources) != 1:
        raise StructureError("client.py 未引用唯一通知注册表")
    source, imported = sources[0]
    local = imported.asname or imported.name
    if source.level != 1 or not source.module or not any(
        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == local
        and node.attr == "get" for node in ast.walk(client)
    ):
        raise StructureError("通知注册表来源或使用方式变化")
    registry_file = source.module.replace(".", "/") + ".py"
    if registry_file not in trees:
        raise StructureError("通知注册表模块缺失")
    if sum(isinstance(node, ast.Name) and node.id == imported.name and isinstance(node.ctx, (ast.Store, ast.Del))
           for node in ast.walk(trees[registry_file])) != 1:
        raise StructureError("通知注册表不是唯一静态赋值")
    registry = assignments(trees[registry_file].body).get(imported.name)
    if not isinstance(registry, ast.Dict) or not registry.keys:
        raise StructureError("通知注册表不是非空静态 dict")
    notifications = {}
    for key, value in zip(registry.keys, registry.values):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or not isinstance(value, (ast.Name, ast.Attribute)):
            raise StructureError("通知注册表包含动态条目")
        if key.value in notifications:
            raise StructureError("通知注册表包含重复方法")
        notifications[key.value] = ast.unparse(value)
    # Fail closed on dynamic mutation, which would invalidate this inventory.
    for node in ast.walk(trees[registry_file]):
        if isinstance(node, ast.Name) and node.id == imported.name and isinstance(node.ctx, ast.Load):
            raise StructureError("通知注册表存在额外读取/动态组合，需人工复核")
    error_pattern = re.compile(r"error|exception|reject|refus|denied|fail", re.I)
    errors = {key: value for key, value in {**api, **models, **aliases}.items()
              if error_pattern.search(key.split(":")[-1])
              or error_pattern.search(" ".join(value.get("bases", []) + value.get("root_union_members", [])
                                              + value.get("union_members", [])))}
    return {"exports": exports, "api": api, "models": models, "aliases": aliases,
            "notifications": notifications, "notification_source": registry_file, "errors": errors,
            "files": hashes, "source_fingerprint": _source_tree_fingerprint(root),
            "counts": {"public_definitions": len(api), "generated_classes": len(models), "notifications": len(notifications)}}


def mapping_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {"added": {key: after[key] for key in sorted(after.keys() - before.keys())},
            "removed": {key: before[key] for key in sorted(before.keys() - after.keys())},
            "changed": {key: {"before": before[key], "after": after[key]}
                        for key in sorted(before.keys() & after.keys()) if before[key] != after[key]}}


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result = {key: mapping_diff(before[key], after[key]) for key in
              ("exports", "api", "models", "aliases", "notifications", "errors", "files")}
    result["inventories"] = {"from": before, "to": after}
    return result


def extract_wheel(wheel: Path, destination: Path) -> None:
    """Never use extractall: reject traversal, links, duplicate paths and ZIP bombs."""
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(wheel) as archive:
        seen: set[str] = set()
        total = 0
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (not path.parts or path.is_absolute() or ".." in path.parts or "\\" in info.filename
                or ":" in info.filename or str(path) in seen
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise StructureError(f"不可信 wheel 路径或文件类型：{info.filename!r}")
            seen.add(str(path))
            total += info.file_size
            if total > 128 * 1024 * 1024 or len(seen) > 10000:
                raise StructureError("wheel 超出静态分析大小/文件数界限")
            target = destination.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(archive.read(info))


def acquire(version: str, wheel: Path | None, directory: Path | None, temporary: Path) -> Path:
    if directory:
        return directory.resolve()
    if wheel is None:
        download = temporary / "download"
        download.mkdir()
        command = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
                   "--ignore-requires-python", "--dest", str(download), f"openai-codex=={version}"]
        # pip may print private configured index URLs. Do not copy its stdout or
        # stderr into public diff material; retain the user's own pip config.
        advice = "检查自己的 pip 配置与网络，或改用 --from-wheel / --to-wheel"
        try:
            result = subprocess.run(command, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired as error:
            raise DownloadError(f"pip 下载超时（180 秒）；{advice}") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise DownloadError(f"pip 下载进程失败（{type(error).__name__}）；{advice}") from error
        if result.returncode:
            raise DownloadError(f"pip 下载失败（退出码 {result.returncode}）；{advice}")
        wheels = list(download.glob("*.whl"))
        if len(wheels) != 1:
            raise StructureError("下载未产生唯一 SDK wheel")
        wheel = wheels[0]
    extracted = temporary / "extracted"
    extract_wheel(wheel, extracted)
    metadata_paths = list(extracted.glob("*.dist-info/METADATA"))
    if len(metadata_paths) != 1:
        raise StructureError("wheel 缺少唯一 METADATA")
    metadata = Parser().parsestr(metadata_paths[0].read_text(encoding="utf-8"))
    if metadata.get("Name", "").replace("_", "-") != "openai-codex" or metadata.get("Version") != version:
        raise StructureError("wheel 名称/版本与请求不符")
    return extracted


def render_change(before: Any, after: Any) -> str:
    if isinstance(before, list) and isinstance(after, list) and all(isinstance(item, str) for item in before + after):
        return json.dumps({"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
                           "before": before, "after": after}, ensure_ascii=False, indent=2)
    if isinstance(before, dict) and isinstance(after, dict):
        return json.dumps(mapping_diff(before, after), ensure_ascii=False, indent=2)
    return json.dumps({"before": before, "after": after}, ensure_ascii=False, indent=2)


def markdown(result: dict[str, Any], from_version: str, to_version: str) -> str:
    lines = [NOTICE, "", f"# openai-codex {from_version} → {to_version}", "",
             "仅 AST 静态解析；未 import/执行目标包。目录输入版本为调用者提供的标签，wheel 另校验 METADATA。",
             "公开模块定义及 __all__ 导出链覆盖高层 API，导出按名称与定义模块记录；其余私有实现列文件哈希，须人工复核。", ""]
    for label, inventory in result["inventories"].items():
        lines += [f"- {label}: {json.dumps(inventory['counts'], ensure_ascii=False)}",
                  f"  源码包指纹：`{inventory['source_fingerprint']}`；通知注册表：`{inventory['notification_source']}`"]
    for key, title in (("exports", "公开导出（__all__）"), ("api", "公开类、方法、函数与签名"),
                       ("models", "生成模型：类、字段、基类、Literal/枚举、RootModel"),
                       ("aliases", "别名赋值与联合成员"), ("notifications", "通知方法注册表"),
                       ("errors", "错误类型：SDK 异常与生成错误/拒绝类型"), ("files", "整包逐文件内容哈希变化（含私有模块）")):
        lines += ["", f"## {title}", ""]
        changes = result[key]
        if not any(changes.values()):
            lines.append("未检测到静态差异；这不是原生行为结论。")
        for kind in ("added", "removed", "changed"):
            for name, value in changes[kind].items():
                lines += [f"### {kind}: `{name}`", "", "```json",
                          render_change(value["before"], value["after"]) if kind == "changed" else json.dumps(value, ensure_ascii=False, indent=2),
                          "```", ""]
    lines += ["## 整包文件清单", "", "| 文件 | from SHA-256 | to SHA-256 |", "| --- | --- | --- |"]
    old_files, new_files = (result["inventories"][side]["files"] for side in ("from", "to"))
    lines += [f"| `{name}` | {old_files.get(name, '—')} | {new_files.get(name, '—')} |" for name in sorted(old_files.keys() | new_files.keys())]
    # The rust-v prefix was verified with gh api for both 0.163.0 and 0.167.0
    # when implementing this tool. Offline analysis never queries the network.
    url = f"https://github.com/openai/codex/compare/rust-v{from_version}...rust-v{to_version}"
    lines += ["", "## 上游 App Server 行为追查", "",
              f"[上游源码比较]({url})。tag 命名 rust-v 已用 gh 核实 0.163.0/0.167.0；其他目标版本须另核实 tag。",
              "App Server 行为差异须按 docs/deployment.md 的「Netizen 边界 → 需追查的 App Server 请求/行为」对照表人工追查。",
              "逐个判断新增结构化错误和执行前校验能否证明未投递；本脚本不作该判断。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_version", required=True)
    parser.add_argument("--to", dest="to_version", required=True)
    for side in ("from", "to"):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(f"--{side}-wheel", type=Path)
        group.add_argument(f"--{side}-dir", type=Path)
    parser.add_argument("--output", type=Path, help="Markdown file (default: stdout)")
    parser.add_argument("--json", type=Path, help="also save structured material and both full static inventories")
    args = parser.parse_args(argv)
    for version in (args.from_version, args.to_version):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+-]*)", version):
            parser.error("版本必须是显式版本号")
    try:
        with tempfile.TemporaryDirectory(prefix="netizen-sdk-diff-") as raw:
            snapshots = []
            for side in ("from", "to"):
                temporary = Path(raw) / side
                temporary.mkdir()
                snapshots.append(snapshot(acquire(getattr(args, f"{side}_version"), getattr(args, f"{side}_wheel"),
                                                  getattr(args, f"{side}_dir"), temporary)))
            result = compare(*snapshots)
        result.update(notice=NOTICE, from_version=args.from_version, to_version=args.to_version)
        rendered = markdown(result, args.from_version, args.to_version)
        try:
            if args.output:
                args.output.write_text(rendered, encoding="utf-8")
            else:
                print(rendered, end="")
            if args.json:
                args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            raise OutputError(f"输出文件写入失败（{type(error).__name__}）；检查输出目录、权限与可用空间") from error
        return 0
    except StructureError as error:
        print(f"{STRUCTURE_ERROR}。{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except (DownloadError, OutputError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, SyntaxError, zipfile.BadZipFile) as error:
        print(f"静态对比失败（{type(error).__name__}）：{error}；检查输入文件、路径与权限后重试", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
