# runtools

Lightweight job coordination, monitoring, and history for scripts and programs.

Wrap any program with mutual exclusion, queuing, approvals, and observability — without deploying
a full orchestration platform. No framework lock-in, no base classes to inherit, no decorators required.

## Quick Start

```bash
# Wrap a script with mutual exclusion and history
run job --mutual-exclusion ./my-task.sh

# See what's running
taro ps

# See history
taro history

# Stop a running job
taro stop my-task
```

## Installation

```bash
pip install runtoolsio
```

This installs all runtools components. You can also install selectively:

```bash
pip install runtoolsio[cli]   # Job wrapper CLI only (runcli + runjob + runcore)
pip install runtoolsio[ops]   # Ops CLI only (taro + runcore)
pip install runtoolsio[core]  # Core library only
pip install runtoolsio[job]   # Execution library only (runjob + runcore)
```

## Packages

| Package | Description | Depends on |
|---------|-------------|------------|
| [**runcore**](https://github.com/runtoolsio/runcore) | Contracts/protocols, monitoring/control, SQLite persistence | — |
| [**runjob**](https://github.com/runtoolsio/runjob) | Execution machinery (phases, instances, nodes) | runcore |
| [**taro**](https://github.com/runtoolsio/taro) | Ops CLI (`ps`, `history`, `listen`, `stop`, `wait`, `approve`, `resume`, `tail`, `stats`) | runcore |
| [**runcli**](https://github.com/runtoolsio/runcli) | Job wrapper CLI — wraps any program with coordination, monitoring, and history | runjob |

## How It Works

Jobs run inside **environments**. Jobs in the same environment can coordinate with each other
(mutual exclusion, queuing, dependencies). Each environment is implemented by a **node** that
manages instances, coordinates jobs, and dispatches events:

- **In-process** (`InProcessNode`) — direct in-memory callbacks, for embedding in applications
- **Local** (`LocalNode`) — Unix domain sockets, for jobs on the same machine
- **Distributed** (planned) — Redis, for jobs across machines

### Coordination Phases

Jobs are composed of **phases** that form a tree. The root phase lifecycle = job lifecycle.
Coordination phases can be layered around your execution phase:

- `MutualExclusion` — prevent concurrent execution
- `ExecutionQueue` — queue jobs when limit is reached
- `Checkpoint` / `Approval` — execution gates (manual or programmatic)
- `Dependency` / `Waiting` — inter-job dependencies

### Monitoring

Phase transitions produce lifecycle events, dispatched to observers and connectors.
The `taro` CLI connects to an environment and provides real-time and historical views.

## Documentation

Visit [runtools.io](https://runtools.io) for documentation.

## License

MIT
