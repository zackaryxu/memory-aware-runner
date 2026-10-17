"""A short workload used by the local demonstration."""

import json
import os
from pathlib import Path
import sys
import time


def record(path, kind):
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        row = {"kind": kind, "pid": os.getpid(), "time": time.time()}
        os.write(descriptor, (json.dumps(row) + "\n").encode())
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    path = Path(sys.argv[1])
    record(path, "start")
    time.sleep(0.3)
    record(path, "end")
