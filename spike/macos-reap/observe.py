"""Run on a disposable macOS runner. The observer and loopback provider stay outside the job."""
import asyncio
import json
import os
import platform
import plistlib
import signal
import subprocess
import sys
import time
import traceback
import uuid
from importlib.metadata import version
from pathlib import Path

import psutil
from host import binary, env_for, identity, save, state
from netizen_cli.cli_services import ServiceBinding, _service_definition
from provider import Provider

HERE = Path(__file__).resolve().parent
NETWORK = ["/usr/bin/sandbox-exec", "-p",
           '(version 1)(allow default)(deny network-outbound)'
           '(allow network-outbound (remote ip "localhost:*") (remote unix-socket))']


def command(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=10, **kwargs)


def launchctl(*args):
    return command("/bin/launchctl", *args)


def domain_info(domain):
    result = launchctl("print", domain)
    return dict(domain=domain, rc=result.returncode, diagnostic=result.stderr.strip(),
                header=result.stdout.splitlines()[:1])


def job_info(target):
    result = launchctl("print", target)
    fields = ("state", "pid", "runs", "last exit code", "last terminating signal", "active count")
    lines = [line.strip() for line in result.stdout.splitlines()
             if line.startswith("\t") and not line.startswith("\t\t")
             and line.strip().partition(" = ")[0] in fields]
    return dict(t=time.monotonic(), rc=result.returncode, fields=lines, diagnostic=result.stderr.strip())


def plist(work, evidence, label, arguments, env):
    binding = ServiceBinding(work / "instance", Path(sys.executable), Path(sys.prefix).resolve())
    source = _service_definition(work / "home", platform="darwin", binding=binding)
    original = plistlib.loads(source)
    replacements = dict(Label=label, ProgramArguments=arguments, EnvironmentVariables=env,
                        WorkingDirectory=str(work / "cwd"), StandardOutPath=str(evidence / "host.stdout"),
                        StandardErrorPath=str(evidence / "host.stderr"))
    path = evidence / f"{label}.plist"
    path.write_bytes(plistlib.dumps(original | replacements))
    (evidence / "netizen-original.plist").write_bytes(source)
    actual = plistlib.loads(path.read_bytes())
    assert original.keys() == actual.keys() and replacements.keys() <= original.keys()
    retained = original.keys() - replacements.keys()
    expected = {k: original[k] for k in retained}
    observed = {k: actual[k] for k in retained}
    assert {"KeepAlive", "ExitTimeOut", "ThrottleInterval", "Umask", "AbandonProcessGroup", "RunAtLoad"} <= retained
    # Canonical plist bytes distinguish an integer from a boolean and compare nested types too.
    assert plistlib.dumps(expected) == plistlib.dumps(observed)
    save(evidence / "fidelity.json", dict(replaced=sorted(replacements), expected=expected, observed=observed,
                                         canonical_plist_equal=True))
    return path


def starts(evidence):
    return [json.loads(path.read_text()) for path in sorted(evidence.glob("start-*.json"))]


def track_descendants(known, row):
    if state(row) != "live":
        return
    try:
        for child in psutil.Process(row["pid"]).children(recursive=True):
            try:
                info = identity(child.pid)
                if info["birth"] == child.create_time():
                    known[(child.pid, info["birth"])] = info
            except (psutil.NoSuchProcess, ProcessLookupError):
                pass
    except (psutil.NoSuchProcess, ProcessLookupError):
        pass


def kill_exact(row):
    proc = psutil.Process(row["pid"])
    if proc.create_time() != row["birth"]:
        raise RuntimeError("original PID was reused")
    proc.send_signal(signal.SIGKILL)


async def observe(name, root, evidence_root):
    work, evidence = root / name, evidence_root / name
    for path in (work / "home" / ".claude", work / "cwd", work / "tmp", evidence):
        path.mkdir(parents=True)
    mock = Provider(evidence / "mock.jsonl")
    port = await mock.start()
    label = "io.netizen.spike.reap." + uuid.uuid4().hex
    env = env_for(work, port)
    args = [*NETWORK, sys.executable, "-B", str(HERE / "host.py"), str(work), str(evidence), str(port)]
    tree_file = evidence / "tree.json"
    managed = name != "plain"
    target = plain = None
    tracked = {}
    def remember(row):
        tracked[(row["pid"], row["birth"])] = row

    try:
        if managed:
            path = plist(work, evidence, label, args, env)
            attempts = []
            for kind in ("gui", "user"):
                domain = f"{kind}/{os.getuid()}"
                attempt = domain_info(domain)
                attempts.append(attempt)
                if attempt["rc"] == 0:
                    boot = launchctl("bootstrap", domain, str(path))
                    attempt.update(bootstrap_rc=boot.returncode, bootstrap_error=boot.stderr.strip())
                    if not boot.returncode:
                        target = f"{domain}/{label}"
                        break
            save(evidence / "domain.json", dict(attempts=attempts, target=target))
            if not target:
                raise RuntimeError(f"bootstrap failed: {attempts}")
        else:
            with (evidence / "host.stdout").open("w") as out, (evidence / "host.stderr").open("w") as err:
                plain = subprocess.Popen(args, env=env, cwd=work / "cwd", stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=err)
            remember(identity(plain.pid))

        async with asyncio.timeout(80):
            while not tree_file.exists():
                generations = starts(evidence)
                if plain and plain.poll() is not None or len(generations) > 1:
                    raise RuntimeError("host exited before the Bash receipt")
                for gen in generations:
                    remember(gen["host"])
                    track_descendants(tracked, gen["host"])
                await asyncio.sleep(.1)

        tree = json.loads(tree_file.read_text())
        original, = starts(evidence)
        assert original["host"] == tree["host"]
        for row in tree.values():
            remember(row)
            assert state(row) == "live"
        track_descendants(tracked, tree["host"])
        assert any(r["path"] == "/v1/messages" and r["marker"] for r in mock.rows)
        job_states = []
        if target:
            job_states.append(job_info(target))
            assert f"pid = {tree['host']['pid']}" in job_states[0]["fields"], job_states[0]
        else:
            assert tree["host"]["pid"] == plain.pid

        states = {k: state(v) for k, v in tree.items()}
        assert set(states.values()) == {"live"}
        t0, w0 = time.monotonic(), time.time()
        kill_exact(tree["host"])
        signal_sent = time.monotonic()
        changes = [dict(t=t0, wall=w0, states=states)]
        release = None
        due = iter((.25, 1., 3., 10., 20., 25.))
        snapshot_at = next(due)
        with (evidence / "samples.jsonl").open("w", buffering=1) as samples:
            while True:
                if plain:
                    plain.poll()
                before, wall_before = time.monotonic(), time.time()
                current = {k: state(v) for k, v in tree.items()}
                sample = dict(t=before, wall=wall_before, end=time.monotonic(), wall_end=time.time(), states=current)
                print(json.dumps(sample), file=samples)
                if current != changes[-1]["states"]:
                    changes.append(sample)
                elapsed = before - t0
                if elapsed >= snapshot_at:
                    if target:
                        job_states.append(job_info(target))
                    snapshot_at = next(due, float("inf"))
                if elapsed >= 20 and release is None:
                    held_states = current.copy()
                    release = time.monotonic()
                    (work / "cwd" / "release-reap").touch()
                if release is not None and before >= release + 5:
                    break
                await asyncio.sleep(.04)
        end = time.monotonic()
        assert current["host"] != "live", "host did not exit after SIGKILL"
        generations = starts(evidence)
        restarts = [g for g in generations if g["marker"] != original["marker"]]
        later = [r for r in mock.rows if t0 <= r["t"] <= end]
        result = dict(domain=target.split("/")[0] if target else None, tree=tree, original=original,
                      kill_before=t0, kill_returned=signal_sent, kill_wall=w0, release=release, end=end,
                      held_states=held_states, final_states=current, changes=changes, restarts=restarts,
                      post_kill_requests=later, total_requests=len(mock.rows), launchctl_print=job_states)
        save(evidence / "measurement.json", result)
        return result
    finally:
        for gen in starts(evidence):
            remember(gen["host"])
        if tree_file.exists():
            for row in json.loads(tree_file.read_text()).values():
                remember(row)
        for row in list(tracked.values()):
            track_descendants(tracked, row)
        bootout = launchctl("bootout", target) if target else None
        (work / "cwd" / "release-reap").touch()
        for row in reversed(list(tracked.values())):
            if state(row) == "live":
                try:
                    kill_exact(row)
                except (ProcessLookupError, psutil.NoSuchProcess):
                    pass
        if plain:
            plain.wait(timeout=5)
        async with asyncio.timeout(10):
            while any(state(r) in ("zombie", "live") for r in tracked.values()):
                await asyncio.sleep(.05)
        final_job = job_info(target) if target else None
        await asyncio.wait_for(mock.runner.cleanup(), 5)
        save(evidence / "cleanup.json", dict(bootout_rc=bootout.returncode if bootout else None,
             final_job=final_job, mock_closed=True,
             tracked=[dict(**row, final_state=state(row)) for row in tracked.values()]))
        assert not target or (bootout.returncode == 0 and final_job["rc"] != 0)


async def main(root, evidence):
    root.mkdir(parents=True)
    evidence.mkdir(parents=True)
    for path in (root / "home" / ".claude", root / "tmp"):
        path.mkdir(parents=True)
    assert sys.platform == "darwin" and platform.machine() == "arm64" and sys.version_info[:2] == (3, 12)
    versions = dict(macos=command("/usr/bin/sw_vers", check=True).stdout.strip().splitlines(),
                    arch=platform.machine(), python=platform.python_version(),
                    packages={p: version(p) for p in ("claude-agent-sdk", "netizen-cli", "psutil", "aiohttp")},
                    cli=command(*NETWORK, str(binary()), "--version", env=env_for(root, 9), check=True).stdout.strip(),
                    launchctl_manager=launchctl("managername").stdout.strip(),
                    domains=[domain_info(f"{k}/{os.getuid()}") for k in ("gui", "user")],
                    sip=command("/usr/bin/csrutil", "status").stdout.strip(), network_profile=NETWORK[-1],
                    image=os.environ.get("ImageVersion"), commit=os.environ.get("GITHUB_SHA"),
                    run_id=os.environ.get("GITHUB_RUN_ID"))
    save(evidence / "environment.json", versions)
    print(json.dumps(versions, indent=2), flush=True)
    failures = []
    table = ["sample | domain | CLI at 20s | Bash at 20s | later requests | replacement starts | old processes live at replacement marker"]
    for name in ("launchd-1", "launchd-2", "plain"):
        try:
            result = await observe(name, root, evidence)
            live_at_restart = [",".join(k for k, v in gen["old_state"].items() if v == "live") or "none"
                               for gen in result["restarts"]]
            table.append(" | ".join((name, result["domain"] or "plain", result["held_states"]["cli"],
                                     result["held_states"]["bash"], str(len(result["post_kill_requests"])),
                                     str(len(result["restarts"])), "/".join(live_at_restart) or "n/a")))
        except Exception:
            failures.append(name)
            failure = traceback.format_exc()
            (evidence / name / "failure.txt").write_text(failure)
            print(failure, flush=True)
            table.append(name + " | measurement failed")
    print("\n".join(table), flush=True)
    (evidence / "summary.txt").write_text("\n".join(table) + "\n")
    if failures:
        raise RuntimeError(f"incomplete measurements: {failures}")


if __name__ == "__main__":
    asyncio.run(main(*(Path(s).resolve() for s in sys.argv[1:])))
