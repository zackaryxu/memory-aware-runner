# Memory-aware Runner

Memory-aware Runner is a small tool for queuing commands on a Mac. I built it to help manage heavier jobs, like data processing, without having to watch memory usage and start each one myself. You can choose/config how many jobs can run at once, and separate commands share the same queue. It checks memory pressure before starting the next job, waits when memory is tight, and leaves jobs that are already running alone.

## Install

Download the [source ZIP](https://github.com/zackaryxu/memory-aware-runner/releases/download/v0.1.0/memory-aware-runner-0.1.0-source.zip), extract it, and run these commands from its folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

## Use

```sh
.venv/bin/memory-runner status
.venv/bin/memory-runner run --jobs 2 -- python3 your_job.py
```

Use the same `--jobs` setting for commands sharing the queue. This is for commands that stay running in the foreground.

[License](LICENSE)
