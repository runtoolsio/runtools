# Claude Code Guidelines

## Allowed Commands
```toml
# Read-only exploration commands - auto-allow
allow = [
  "wc *",
  "find *",
  "ls *",
  "tree *",
  "du *",
  "file *",
  "head *",
  "tail *",
  "stat *",
  "awk *",
]
```

## Principles
- `Style`: Line length 120. Google-style docstrings.
- `Error handling`: Never catch bare Exception.

## Running Tests
Each subproject has its own virtual environment. To run tests:

```bash
# runjob tests (includes integration tests)
source runjob/venv/bin/activate && cd runjob && pytest

# runcore tests
source runcore/venv/bin/activate && cd runcore && pytest
```

## Packages

- **runcore** — Contracts/protocols, monitoring/control, SQLite persistence. For apps that watch/control jobs.
- **runjob** — Execution machinery (phases, instances, nodes). For apps that run jobs.
- **taro** — Ops CLI built on runcore (`ps`, `history`, `listen`, `stop`, `wait`, `approve`, `resume`, `tail`, `stats`).
- **runcli** — Job wrapper CLI built on runjob. Wraps any program with coordination, monitoring, and history.

### Dependencies
- runjob --> runcore
- taro --> runcore
- runcli --> runjob

## Key Abstractions

- **Job** — definition (name, parameters, phases). **JobInstance** — a live, running job. **JobRun** — immutable
  snapshot (frozen dataclass) of a completed or in-progress instance.
- **Phase** — a unit of execution with lifecycle. **PhaseDetail** — immutable snapshot via `phase.detail()`.
  **PhaseControl** — wrapper providing control API access to a live Phase.
- **Connector** — protocol for connecting to an environment (monitoring, control).
- **Node** — runtime implementation of an environment. Manages instances, coordinates jobs, dispatches events.
- **Environment** — groups jobs into separate environments; jobs can coordinate only within the same environment.
  Access varies by implementation: `InProcessNode` (in-process), `LocalNode` (local sockets),
  distributed (TBI - Redis).

## Phase System

- The root phase lifecycle = job lifecycle. When the root phase completes, the job completes.
- `run_child(child)` — always use instead of `child.run(ctx)` directly (wires observers, registers children).
