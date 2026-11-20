"""Invoked via the SDK's cli_path. Both paths use the original CLI argv and stdio."""
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path

from probes import binary, identity, lock_path, save


def main():
    work = Path(os.environ["LOCK_SPIKE_WORK"])
    fd = os.open(lock_path(work), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    variant = os.environ["LOCK_SPIKE_VARIANT"]
    owner = identity(os.getpid())
    save(work / f"owner-{owner['pid']}.json", dict(identity=owner, fd=fd, inode=os.fstat(fd).st_ino))
    argv = [str(binary()), *sys.argv[1:]]
    if variant == "v1":
        os.set_inheritable(fd, True)
        os.execv(argv[0], argv)
    assert variant == "v2"
    # A caught signal is reset to default by exec in the CLI. SIG_IGN would survive exec.
    signal.signal(signal.SIGTERM, lambda *_: None)
    child = subprocess.Popen(argv, close_fds=True)
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
