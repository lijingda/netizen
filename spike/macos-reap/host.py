"""The measured host. The replacement records the old tree and never queries the mock."""
import asyncio
import json
import os
import sys
import time
import uuid
from importlib.metadata import distribution
from pathlib import Path

import psutil


def save(path, data):
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def identity(pid):
    proc = psutil.Process(pid)
    return dict(pid=pid, birth=proc.create_time(), ppid=proc.ppid(),
                pgid=os.getpgid(pid), sid=os.getsid(pid), name=proc.name())


def state(row):
    try:
        proc = psutil.Process(row["pid"])
        if proc.create_time() != row["birth"]:
            return "reused"
        return "zombie" if proc.status() == psutil.STATUS_ZOMBIE else "live"
    except (psutil.NoSuchProcess, ProcessLookupError):
        return "gone"


def env_for(work, port):
    home = work / "home"
    return dict(PATH=f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
                LANG="en_US.UTF-8", HOME=str(home), SHELL="/bin/bash", TMPDIR=str(work / "tmp"),
                CLAUDE_CONFIG_DIR=str(home / ".claude"), ANTHROPIC_API_KEY="fixture-invalid",
                ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", HTTP_PROXY="http://127.0.0.1:9",
                HTTPS_PROXY="http://127.0.0.1:9", NO_PROXY="127.0.0.1,localhost",
                DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1", DISABLE_AUTOUPDATER="1",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_FEATURE_FLAGS="1")


def binary():
    return distribution("claude-agent-sdk").locate_file("claude_agent_sdk/_bundled/claude").resolve(strict=True)


async def until(predicate, budget):
    async with asyncio.timeout(budget):
        while not (value := predicate()):
            await asyncio.sleep(.025)
        return value


async def host(work, evidence, port):
    os.environ.clear()
    os.environ.update(env_for(work, port))
    os.chdir(work / "cwd")
    start = dict(t=time.monotonic(), host=identity(os.getpid()), marker=uuid.uuid4().hex)
    tree_file = evidence / "tree.json"
    if tree_file.exists():
        start["old_state"] = {k: state(v) for k, v in json.loads(tree_file.read_text()).items()}
    save(evidence / f"start-{start['marker']}.json", start)
    try:
        (work / "first").touch(exist_ok=False)
    except FileExistsError:
        await asyncio.sleep(150)
        return

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    cli_binary = binary()
    options = ClaudeAgentOptions(cwd=Path.cwd(), cli_path=cli_binary, env=dict(os.environ),
                                 tools=["Bash"], allowed_tools=["Bash"], setting_sources=[],
                                 max_turns=2, stderr=lambda line: print(line, file=sys.stderr, flush=True))
    async def receipt():
        path = Path("starts-reap")
        await until(lambda: path.exists() and path.read_text().strip(), 65)
        bash = psutil.Process(int(path.read_text().splitlines()[0]))
        assert Path(bash.exe()).name == "bash", bash.name()
        lineage = []
        parent = bash.parent()
        while parent is not None and parent.pid != os.getpid():
            lineage.append(parent)
            parent = parent.parent()
        assert parent is not None, "Bash is not a descendant of this host"
        cli_proc, = [p for p in lineage if Path(p.exe()).resolve() == cli_binary]
        save(tree_file, dict(host=start["host"], cli=identity(cli_proc.pid), bash=identity(bash.pid)))

    async with asyncio.timeout(85), ClaudeSDKClient(options=options) as client:
        await client.query("[[S:tool-barrier/reap]]")
        async with asyncio.TaskGroup() as group:
            group.create_task(receipt())
            async for _ in client.receive_response():
                pass


if __name__ == "__main__":
    asyncio.run(host(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])))
