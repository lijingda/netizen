"""Inspect only fixture identities and the stable per-instance lock inode."""
import fcntl
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "macos-reap"))
from host import binary, env_for as env_for, identity, save, state  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def lock_path(work):
    return work / "instance" / "agent-generation.lock"


def exclusive(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def descriptors(row, path):
    assert state(row) == "live", row
    pid = row["pid"]
    found = []
    if sys.platform == "darwin":
        # A PID-only listing also proves lsof saw this process in the negative case.
        data = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-F", "pfn"],
                              capture_output=True, text=True, timeout=8, check=True).stdout.splitlines()
        assert f"p{pid}" in data
        fd = None
        numeric = False
        for line in data:
            if line.startswith("f"):
                fd = line[1:]
                numeric |= fd.isdigit()
            if line == f"n{path}" and fd and fd.isdigit():
                found.append(int(fd))
        assert numeric, "incomplete lsof listing"
    else:
        for entry in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if os.path.samefile(entry, path):
                    found.append(int(entry.name))
            except FileNotFoundError:
                continue
    assert state(row) == "live", row
    return sorted(found)


def record():
    # Called synchronously by the tool shell before its final foreground/background command.
    work = Path(sys.argv[2])
    if sys.argv[1] == "bash":
        bash = identity(os.getppid())
        assert bash["name"] == "bash", bash
        host = read(work / "first-host.json")
        chain = []
        for process in psutil.Process(bash["pid"]).parents():
            if process.pid == host["pid"] and process.create_time() == host["birth"]:
                break
            chain.append(process)
        else:
            raise RuntimeError("tool shell is outside the measured host")
        real = binary()
        cli, = [p for p in chain if Path(p.exe()).resolve() == real]
        tree = dict(cli=identity(cli.pid), bash=bash, host=host)
        owner_pid = cli.pid
        if os.environ["LOCK_SPIKE_VARIANT"] == "v2":
            owner_pid = cli.ppid()
            tree["wrapper"] = identity(owner_pid)
        owner = read(work / f"owner-{owner_pid}.json")
        assert owner["identity"]["birth"] == psutil.Process(owner_pid).create_time()
        save(work / "bash.json", dict(tree=tree, owner=owner, lock_fds=descriptors(bash, lock_path(work))))
    else:
        assert sys.argv[1] in ("nohup", "setsid")
        if sys.argv[1] == "setsid":
            os.setsid()
        background = identity(os.getpid())
        save(work / "background.json", dict(identity=background, lock_fds=descriptors(background, lock_path(work))))
        os.execv("/bin/sleep", ["sleep", "90"])


if __name__ == "__main__":
    try:
        record()
    except Exception as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        save(Path(sys.argv[2]) / "probe-error.json", dict(kind=type(exc).__name__, mode=sys.argv[1],
             file=Path(frame.filename).name, line=frame.lineno, errno=getattr(exc, "errno", None)))
        raise
