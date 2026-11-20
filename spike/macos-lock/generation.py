"""First host drives one fixed SDK request. Replacement exercises the admission gate."""
import asyncio
import os
import sys
import time
from pathlib import Path

from probes import env_for, exclusive, identity, lock_path, read, save, state

BUDGET = 15
POLL = .01


def replacement(work, evidence):
    old = read(work / "tree.json")
    arrived = time.monotonic()
    initial = {k: state(v) for k, v in old.items()}
    save(evidence / "replacement-start.json", dict(t=arrived, host=identity(os.getpid()), states=initial))
    with lock_path(work).open("r+b") as stream:
        start = time.monotonic()
        deadline = start + BUDGET
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            # Diagnostic only. A live/dead PID never grants or withholds the gate.
            cli_check = time.monotonic()
            cli_before = state(old["cli"])
            before = time.monotonic()
            if before >= deadline:
                break
            if exclusive(stream.fileno()):
                acquired = time.monotonic()
                states = {k: state(v) for k, v in old.items()}
                checked = time.monotonic()
                # This marker stands for opening admission while still holding the gate.
                marker = time.monotonic()
                save(evidence / "gate.json", dict(outcome="acquired", start=start, attempts=attempts,
                     cli_check=cli_check, cli_before=cli_before, syscall_before=before, acquired=acquired,
                     states=states, checked=checked, marker=marker))
                return
            time.sleep(max(0, min(POLL, deadline - time.monotonic())))
        save(evidence / "gate.json", dict(outcome="timeout", start=start, attempts=attempts,
             end=time.monotonic(), states={k: state(v) for k, v in old.items()}, marker=None))


async def run(work, evidence, port, variant):
    os.environ.clear()
    os.environ.update(env_for(work, port), LOCK_SPIKE_WORK=str(work), LOCK_SPIKE_VARIANT=variant)
    os.chdir(work / "cwd")
    try:
        (work / "first").touch(exist_ok=False)
    except FileExistsError:
        if (work / "tree.json").exists():
            replacement(work, evidence)
        else:
            save(evidence / "premature-restart.json", dict(t=time.monotonic()))
        await asyncio.sleep(120)
        return

    save(work / "first-host.json", identity(os.getpid()))
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    options = ClaudeAgentOptions(cli_path=work / "cli-launcher", cwd=Path.cwd(), env=dict(os.environ),
                                 tools=["Bash"], allowed_tools=["Bash"], setting_sources=[], max_turns=2)
    async with asyncio.timeout(110), ClaudeSDKClient(options=options) as client:
        await client.query("[[S:tool-barrier/reap]]")
        async for _ in client.receive_response():
            pass
        save(work / "result.json", dict(t=time.monotonic()))
        # Background Bash has returned. Leave the SDK connection (and CLI) open.
        await asyncio.sleep(90)


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]))
