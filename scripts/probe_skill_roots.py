#!/usr/bin/env python3
"""Discover process-local built-in Skills with disposable, unauthenticated state.

This does not run a model or prove Turn/Side/subagent execution. Run under an
external process deadline as the SDK's stdio shutdown can outlive cancellation.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile

from openai_codex import AsyncCodex, CodexConfig
import openai_codex

from netizen.builtin_skills import BUILTIN_SKILL_NAMES, validate_builtin_skills
from netizen.sdk_gap_adapter import (
    AppServerSkillCatalog,
    AppServerSkillRoots,
    require_no_facade_migration,
)


async def probe(source: Path) -> dict[str, object]:
    require_no_facade_migration()
    roots = validate_builtin_skills(source)
    with tempfile.TemporaryDirectory(prefix="netizen-skill-roots-") as directory:
        # Native discovery returns physical paths (e.g. /private/var on macOS).
        root = Path(directory).resolve()
        home, codex_home, project = (root / name for name in ("home", "codex", "project"))
        for path in (home, codex_home, project):
            path.mkdir()
        config_file = codex_home / "config.toml"
        config_content = "# Native user configuration must remain unchanged.\n"
        config_file.write_text(config_content, encoding="utf-8")
        user_skill = codex_home / "skills" / "netizen-probe-user" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text(
            "---\nname: netizen-probe-user\ndescription: Disposable native user Skill.\n---\n",
            encoding="utf-8",
        )
        env = dict(os.environ, HOME=str(home), CODEX_HOME=str(codex_home))
        config = CodexConfig(env=env, cwd=str(project), experimental_api=False)
        async with _codex(config) as codex:
            control = AppServerSkillRoots(codex)
            await control.set_roots((roots,))
            snapshot = await AppServerSkillCatalog(codex).list(project)
            if snapshot.errors:
                raise RuntimeError(f"skill discovery failed: {snapshot.errors}")
            if not any(Path(skill.path) == user_skill for skill in snapshot.skills):
                raise RuntimeError("extra roots replaced native user Skill discovery")
            for name in BUILTIN_SKILL_NAMES:
                matches = [skill for skill in snapshot.skills if skill.name == name]
                expected = roots / name / "SKILL.md"
                if len(matches) != 1 or not matches[0].enabled or Path(matches[0].path) != expected:
                    raise RuntimeError(f"missing or ambiguous release Skill: {name}")
            # A second server shares the same home, but not the first server's roots.
            async with _codex(config) as other:
                other_snapshot = await AppServerSkillCatalog(other).list(project)
            if any(skill.name in BUILTIN_SKILL_NAMES for skill in other_snapshot.skills):
                raise RuntimeError("extra roots leaked to another App Server")
            await control.set_roots(())
            cleared = await AppServerSkillCatalog(codex).list(project)
            if any(skill.name in BUILTIN_SKILL_NAMES for skill in cleared.skills):
                raise RuntimeError("clearing extra roots did not clear discovery")
            if not any(Path(skill.path) == user_skill for skill in cleared.skills):
                raise RuntimeError("clearing extra roots removed native user Skill discovery")
        if config_file.read_text(encoding="utf-8") != config_content:
            raise RuntimeError("extra roots unexpectedly wrote user configuration")
        if any((codex_home / "skills" / name).exists() for name in BUILTIN_SKILL_NAMES):
            raise RuntimeError("extra roots unexpectedly installed a global Skill")
    return {
        "openai_codex_version": openai_codex.__version__,
        "skill_roots": str(roots),
        "discovered": list(BUILTIN_SKILL_NAMES),
        "process_local": True,
        "native_user_skills_preserved": True,
        "config_unchanged": True,
        "model_execution_verified": False,
    }


@asynccontextmanager
async def _codex(config: CodexConfig):
    codex = AsyncCodex(config)
    process = None
    try:
        async with codex:
            process = codex._client._sync._proc
            yield codex
    finally:
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, name, None)
            if stream is not None:
                stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()

    async def run() -> dict[str, object]:
        async with asyncio.timeout(args.timeout):
            return await probe(args.source_root)

    print(json.dumps(asyncio.run(run()), ensure_ascii=False))


if __name__ == "__main__":
    main()
