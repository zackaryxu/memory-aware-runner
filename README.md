# Memory-aware Runner

Queue foreground commands behind a shared concurrency limit and macOS memory-pressure gate. Python 3.10 or newer; no third-party runtime packages.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/memory-runner status
.venv/bin/memory-runner run --jobs 2 -- python3 your_job.py
```

Separate invocations share the default per-user pool. Use the same policy options for concurrent commands, or give unrelated workloads different `--pool` directories. Custom pool directories must be private (mode `700`). `run --help` lists configuration.

Normal pressure admits up to `--jobs`; warning pressure admits up to `--warning-jobs` (default one). Every admission uses a fresh pressure reading; `--poll` controls the interval between waiting attempts. Critical or unavailable readings hold new jobs. Rapid swap/compressor growth also holds admission until the growth subsides. High but stable swap alone does not hold it. Running commands are left alone.

Admission waits at most 300 seconds by default; `--wait-timeout 0` waits indefinitely. Admission timeout exits `75`, setup failure exits `2`, and a missing executable exits `127`. Once admitted, the command replaces the wrapper and retains its PID, input/output and exit status. Exited processes are removed from pool state on the next pool operation. Commands that daemonize or detach background work are outside this foreground-command model.

```sh
# Small simulated-pressure demonstration; no live camera, service or production work.
.venv/bin/python examples/demo.py

# Tests use temporary pools and simulated readings.
.venv/bin/python -m unittest discover -s tests -v
```

The demo's `--probe-file` is an explicit simulation input, not a live memory reading. Live probing requires macOS. Probe failures are reported as unavailable; they never silently become healthy readings. The tool neither frees arbitrary RAM nor kills other processes, and does not guarantee that workloads cannot exhaust memory.

To remove: stop any commands you started, remove the virtual environment, and delete the pool directory reported by `status` after all its jobs have exited. Pool state stores PIDs, process-start identities, short labels, tickets, timestamps, policy, pressure readings, and any simulated probe-file path. It does not store command arguments.
