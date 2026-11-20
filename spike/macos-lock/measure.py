"""Observe the two lock strategies on exact fixture processes outside the service."""
import asyncio
import ctypes
import os
import platform
import shlex
import subprocess
import sys
import time
import uuid
from importlib.metadata import version
from pathlib import Path

import psutil

from probes import HERE, binary, descriptors, env_for, exclusive, identity, lock_path, read, save, state
from observe import NETWORK, command, domain_info, job_info, kill_exact, launchctl, plist, track_descendants
from provider import Provider

MAC = sys.platform == "darwin"


def tool_command(work, case):
    def probe(mode):
        return shlex.join([sys.executable, "-B", str(HERE / "probes.py"), mode, str(work)])

    if case == "foreground":
        action = "for ((i=0;i<1000;i++)); do test -f release && break; sleep .1; done"
    else:
        action = f"nohup {probe(case)} </dev/null >/dev/null 2>&1 & echo $! > background-pid"
    return f"{probe('bash')} || exit 31; {action}"


async def ready(test, seconds):
    async with asyncio.timeout(seconds):
        while not (result := test()):
            await asyncio.sleep(.02)
        return result


def linux_orphans():
    # Only the observer adopts orphaned fixture processes. No Linux service manager is involved.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "subreaper")


def reap_children():
    if not MAC:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass


def known_rows(work):
    rows = []
    if (work / "first-host.json").exists():
        rows.append(read(work / "first-host.json"))
    rows.extend(read(p)["identity"] for p in work.glob("owner-*.json"))
    if (work / "bash.json").exists():
        rows.extend(read(work / "bash.json")["tree"].values())
    if (work / "background.json").exists():
        rows.append(read(work / "background.json")["identity"])
    return {(r["pid"], r["birth"]): r for r in rows}


async def trial(work, evidence, variant, case):
    for path in [work / "home" / ".claude", work / "tmp", work / "cwd", work / "instance", work / "private", evidence]:
        path.mkdir(parents=True)
    work = work.resolve()
    mock = Provider(evidence / "mock.jsonl", command=tool_command(work, case))
    port = await mock.start()
    env = env_for(work, port)
    launcher = work / "cli-launcher"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-B", str(HERE / "launcher.py")]) + ' "$@"\n')
    launcher.chmod(0o700)
    args = [sys.executable, "-B", str(HERE / "generation.py"), str(work), str(evidence), str(port), variant]
    target = plain = None
    private = work / "private"
    try:
        if MAC:
            label = "io.netizen.spike.lock." + uuid.uuid4().hex
            domain = f"gui/{os.getuid()}"
            path = plist(work, private, label, [*NETWORK, *args], env)
            save(evidence / "fidelity.json", read(private / "fidelity.json"))
            assert domain_info(domain)["rc"] == 0
            launchctl("bootstrap", domain, str(path)).check_returncode()
            target = f"{domain}/{label}"
        else:
            with (private / "host.stdout").open("w") as out, (private / "host.stderr").open("w") as err:
                plain = subprocess.Popen(args, env=env, cwd=work / "cwd", stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=err)

        def tool_ready():
            if (work / "probe-error.json").exists():
                raise RuntimeError(read(work / "probe-error.json"))
            if case == "foreground" and (work / "result.json").exists():
                raise RuntimeError("tool finished before foreground receipt")
            if (evidence / "premature-restart.json").exists() or plain and plain.poll() is not None:
                raise RuntimeError("original host exited before receipt")
            bg = case != "foreground"
            return (work / "bash.json").exists() and (not bg or (
                (work / "background.json").exists() and (work / "result.json").exists()
                and any(r["tool_result"] and r["path"] == "/v1/messages" for r in mock.rows)))

        await ready(tool_ready, 75)
        receipt = read(work / "bash.json")
        tree = receipt["tree"]
        owner = receipt["owner"]
        path = lock_path(work)
        assert path.stat().st_ino == owner["inode"]
        fds = {"bash": receipt["lock_fds"]}
        for role in tree.keys() - {"bash"}:
            fds[role] = descriptors(tree[role], path)
        bg_info = None
        if case != "foreground":
            bg_info = read(work / "background.json")
            bg = tree["background"] = bg_info["identity"]
            assert bg["pid"] == int((work / "cwd" / "background-pid").read_text())
            if state(bg) == "live":
                await ready(lambda: state(bg) != "live" or psutil.Process(bg["pid"]).name() == "sleep", 4)
            if state(bg) == "live":
                fds["background"] = descriptors(bg, path)
        save(work / "tree.json", tree)
        initial = {k: state(v) for k, v in tree.items()}
        assert initial["host"] == initial["cli"] == "live" and not fds["host"]
        assert case != "foreground" or initial["bash"] == "live"
        service = job_info(target) if target else None
        if service:
            assert f"pid = {tree['host']['pid']}" in service["fields"]
        else:
            assert tree["host"]["pid"] == plain.pid
        with path.open("r+b") as stream:
            busy = not exclusive(stream.fileno())
        save(evidence / "before.json", dict(tree=tree, states=initial, owner=owner, fds=fds,
             busy=busy, service=service, background_before_exec=bg_info,
             tool_returned=case != "foreground", inspector="lsof" if MAC else "/proc/pid/fd"))

        t0 = time.monotonic()
        kill_exact(tree["host"])
        sent = time.monotonic()
        events = [dict(start=t0, end=t0, states=initial)]
        previous = initial
        previous_start = previous_end = t0
        gate = None
        while True:
            if plain:
                plain.poll()
            reap_children()
            start = time.monotonic()
            current = {k: state(v) for k, v in tree.items()}
            end = time.monotonic()
            if current != previous:
                events.append(dict(start=start, end=end, states=current,
                                   preceding_start=previous_start, preceding_end=previous_end))
            previous, previous_start, previous_end = current, start, end
            if MAC and gate is None and (evidence / "gate.json").exists():
                gate = read(evidence / "gate.json")
            if ((gate and start > (gate.get("marker") or gate["end"]) + 2 and start > t0 + 4)
                    or (not MAC and start > t0 + 2)):
                break
            if start > t0 + 40:
                raise TimeoutError("replacement failed to produce gate outcome")
            await asyncio.sleep(.01)
        final_fds = {}
        if "background" in tree and state(tree["background"]) == "live":
            final_fds["background"] = descriptors(tree["background"], path)
        save(evidence / "measurement.json", dict(variant=variant, case=case, kill_before=t0, kill_sent=sent,
             changes=events, end=end, final_states=current, final_fds=final_fds, gate=gate,
             post_kill_requests=[r for r in mock.rows if r["t"] >= t0]))
        return bool(fds["bash"] or fds.get("background") or (bg_info and bg_info["lock_fds"]))
    finally:
        tracked = known_rows(work)
        replacement = evidence / "replacement-start.json"
        if replacement.exists():
            row = read(replacement)["host"]
            tracked[row["pid"], row["birth"]] = row
        if plain and plain.poll() is None:
            row = identity(plain.pid)
            tracked[row["pid"], row["birth"]] = row
        # Acquire descendants only from already-verified fixture roots, before bootout.
        # All signalling below is limited to recorded PID + creation time.
        for row in list(tracked.values()):
            track_descendants(tracked, row)
        bootout = launchctl("bootout", target) if target else None
        (work / "cwd" / "release").touch()
        for row in reversed(list(tracked.values())):
            if state(row) == "live":
                try:
                    kill_exact(row)
                except (psutil.NoSuchProcess, ProcessLookupError):
                    pass
        if plain:
            plain.wait(timeout=5)

        def all_gone():
            reap_children()
            return all(state(r) in ("gone", "reused") for r in tracked.values())

        try:
            await ready(all_gone, 10)
        finally:
            await asyncio.wait_for(mock.runner.cleanup(), 5)
            final_job = job_info(target) if target else None
            save(evidence / "cleanup.json", dict(bootout=bootout.returncode if bootout else None,
                 final_job=final_job, mock_closed=True,
                 tracked=[dict(**r, final_state=state(r)) for r in tracked.values()]))
        assert not target or (bootout.returncode == 0 and final_job["rc"] != 0)


async def main(root, evidence):
    for path in [root / "home" / ".claude", root / "tmp", evidence]:
        path.mkdir(parents=True)
    assert sys.platform in ("darwin", "linux") and version("claude-agent-sdk") == "0.2.186"
    if not MAC:
        linux_orphans()
    versions = dict(os=platform.system(), release=platform.release(), arch=platform.machine(),
                    python=platform.python_version(), image=os.environ.get("ImageVersion"),
                    packages={k: version(k) for k in ("claude-agent-sdk", "aiohttp", "psutil")},
                    cli=command(*(NETWORK if MAC else []), str(binary()), "--version",
                                env=env_for(root, 9), check=True).stdout.strip(),
                    commit=os.environ.get("GITHUB_SHA"), run_id=os.environ.get("GITHUB_RUN_ID"))
    if MAC:
        versions.update(macos=command("/usr/bin/sw_vers", check=True).stdout.splitlines(),
                        sip=command("/usr/bin/csrutil", "status", check=True).stdout.strip(),
                        manager=launchctl("managername").stdout.strip())
    if os.environ.get("GITHUB_ACTIONS"):
        versions["packages"]["netizen-cli"] = version("netizen-cli")
    save(evidence / "environment.json", versions)
    inherited = False
    completed = []
    for variant in ("v1", "v2"):
        if variant == "v2" and not inherited:
            break
        for case in ("foreground", "nohup", "setsid"):
            for index in (1, 2):
                name = f"{variant}-{case}-{index}"
                print(name, flush=True)
                inherited_here = await trial(root / name, evidence / name, variant, case)
                inherited |= inherited_here
                completed.append(name)
    save(evidence / "summary.json", dict(completed=completed, v2_triggered=any(s.startswith("v2") for s in completed)))


if __name__ == "__main__":
    asyncio.run(main(*(Path(s).resolve() for s in sys.argv[1:])))
