"""Lost-run lifecycle end to end on a throwaway postgres: crash → LOST → stop hint → mark-lost → clean.

Release check for the `postgres` kind (transport doc, point 8). Runs a real node process, kills it with SIGKILL,
and drives the `taro` CLI through the whole resolution path. Isolated from the user's setup: a disposable Docker
container and temporary XDG directories, so no registered environment is touched.

Run with the workspace dev venv (all packages installed, postgres extra included):

    .venv/bin/python runtools/scripts/e2e_lost_run.py

Takes about a minute — a run counts as lost only after 45 s without a heartbeat.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENV_ID = 'e2e'
BIN = Path(sys.executable).parent
NODE_SCRIPT = f'''
from runtools.runjob import node
from runtools.runjob.program import ProgramPhase

with node.connect({ENV_ID!r}) as env:
    inst = env.create_instance('e2e_job', 'r1', ProgramPhase('work', 'sh', '-c', 'echo started; sleep 3600'))
    print(inst.id, flush=True)
    inst.run()
'''
results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {detail}" if detail and not ok else ''), flush=True)


def taro(*args):
    """Options go before patterns: everything after an instance pattern would be read as a pattern."""
    words = 2 if args[0] == 'env' else 1
    argv = [BIN / 'taro', *args[:words], '-e', ENV_ID, *args[words:]]
    out = subprocess.run(argv, env=os.environ, capture_output=True, text=True, timeout=60)
    return out.stdout + out.stderr


def ps_containing(text):
    out = taro('ps')
    return out if text in out else None


def wait_for(fn, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := fn():
            return value
        time.sleep(2)
    return None


def start_postgres():
    """Start the throwaway postgres; returns the container id at once, so the caller can always remove it."""
    return subprocess.check_output(['docker', 'run', '-d', '--rm', '-e', 'POSTGRES_PASSWORD=pw',
                                    '-p', '127.0.0.1::5432', 'postgres:16-alpine'], text=True).strip()


def wait_for_postgres(container):
    """Wait until the container accepts connections; returns its DSN."""
    port = subprocess.check_output(['docker', 'port', container, '5432'], text=True).strip().rsplit(':', 1)[1]

    def ready():
        return subprocess.run(['docker', 'exec', container, 'pg_isready', '-q'], capture_output=True).returncode == 0

    if not wait_for(ready, 60):
        raise RuntimeError("postgres did not start")
    time.sleep(1)
    return f"postgresql://postgres:pw@127.0.0.1:{port}/postgres"


def kill_process_group(pgid):
    """Kill what is left of a process group — the job's workload outlives a SIGKILLed node."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def child_pids(pid):
    """Direct children of ``pid`` — without a terminal each job runs in its own process group, led by such a child."""
    out = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def main():
    xdg = Path(tempfile.mkdtemp(prefix='runtools-e2e-'))
    os.environ.update({var: str(xdg / var.lower())
                       for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')})
    container = node_proc = None
    job_groups = []
    try:
        container = start_postgres()
        dsn = wait_for_postgres(container)
        from runtools.runcore.env import (
            EnvironmentEntry, EnvironmentKind, PostgresEnvironmentConfig, create_environment,
        )
        create_environment(EnvironmentEntry(id=ENV_ID, kind=EnvironmentKind.POSTGRES, location=dsn),
                           PostgresEnvironmentConfig())

        # Own process group: the crash kills only the node, cleanup kills the workload it leaves behind
        node_proc = subprocess.Popen([sys.executable, '-c', NODE_SCRIPT], stdout=subprocess.PIPE, text=True,
                                     start_new_session=True)
        instance_id = node_proc.stdout.readline().strip()
        check("node started the job", bool(instance_id), instance_id)

        ps = wait_for(lambda: ps_containing('e2e_job'), 30)
        check("ps lists the running instance, not lost", ps is not None and 'LOST' not in ps, ps)

        tail = wait_for(lambda: (lambda o: o if 'started' in o else None)(taro('tail', instance_id)), 30)
        check("tail serves the job's output through the db", tail is not None, taro('tail', instance_id))

        job_groups = child_pids(node_proc.pid)  # recorded while the node is alive, to clean up after the crash
        os.kill(node_proc.pid, signal.SIGKILL)
        node_proc.wait()
        crashed_at = time.monotonic()

        out = taro('stop', '-f', instance_id)
        check("stop before staleness sends a signal nobody will apply", 'Stopped' in out, out)

        ps = wait_for(lambda: ps_containing('LOST'), 90)
        check(f"ps shows LOST after the crash ({time.monotonic() - crashed_at:.0f}s)", ps is not None, taro('ps'))

        out = taro('stop', '-f', instance_id)
        check("stop on a lost instance prints the mark-lost hint", 'mark-lost' in out and 'Stopped' not in out, out)

        out = taro('env', 'clean', '-f')
        check("clean keeps a lost run's rows", 'No orphan' in out, out)

        out = taro('env', 'mark-lost', '-f')
        check("mark-lost marks the run", f'Marked lost {instance_id}' in out, out)

        hist = taro('history', '--no-pager')
        check("history shows the run ended LOST", 'LOST' in hist and 'e2e_job' in hist, hist)

        ps = taro('ps')
        check("ps no longer lists it", 'e2e_job' not in ps, ps)

        out = taro('env', 'clean', '-f')
        check("clean removes its signal and tail rows", 'Removed 1 signals and 1 output tails' in out, out)

        out = taro('env', 'clean', '-f')
        check("a second clean finds nothing", 'No orphan' in out, out)
    finally:
        for pgid in (node_proc.pid, *job_groups) if node_proc else ():
            kill_process_group(pgid)
        if container:
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
        shutil.rmtree(xdg, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
