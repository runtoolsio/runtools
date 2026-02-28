# AGENTS.md

> **Scope:** This file applies to all runtoolsio projects (runcore, runjob, runcli, taro, ...).

## Project Overview

Runtools is a mono-repo for a Python job execution and coordination framework. It consists of four packages
with separate virtual environments.

## Packages

- **runcore** — Contracts/protocols, monitoring/control, SQLite persistence. For apps that watch/control jobs.
- **runjob** — Execution machinery (phases, instances, nodes). For apps that run jobs.
- **taro** — Ops CLI + TUI built on runcore (`ps`, `history`, `listen`, `stop`, `wait`, `approve`, `resume`, `tail`, `stats`, `dash`).
- **runcli** — Job wrapper CLI built on runjob. Wraps any program with coordination, monitoring, and history.

### Dependencies

- runjob --> runcore
- taro --> runcore
- runcli --> runjob

## Build and Test Commands

Each subproject has its own virtual environment. To run tests:

```bash
# runjob tests (includes integration tests)
source runjob/venv/bin/activate && cd runjob && pytest

# runcore tests
source runcore/venv/bin/activate && cd runcore && pytest
```

## Coding Standards

- Check existing project patterns before introducing new ones — learn the style
- Be Pythonic (iterators/generators, comprehensions, context managers, multi-assignment/unpacking, etc.)
- Remember The Zen of Python (explicit is better than implicit, etc.)
- Be aware of features in the newest Python versions (up to 3.13)
- Clean and readable code — KISS
- Do not overengineer — YAGNI
- Be SOLID where applicable (OOP parts)
- Duck typing has still its merit
- Line length: 120
- Google-style docstrings — always specify types, comment `why` not `what`
- Do not insert obvious inline comments
- Avoid broad `Exception`; allow it only at process/observer boundaries with explicit handling
- Test behaviour not implementation — pytest with BDD-ish style

## Project Stage

Pre-beta — backward compatibility is not required.

## Key Abstractions

- **Job** — definition (name, parameters, phases). **JobInstance** — a live, running job. **JobRun** — immutable
  snapshot (frozen dataclass) of a completed or in-progress instance.
- **Phase** — a unit of execution with lifecycle. **PhaseRun** — immutable snapshot via `phase.snap()`.
  **PhaseControl** — wrapper providing control API access to a live Phase.
- **Connector** — protocol for connecting to an environment (monitoring, control).
- **Node** — runtime implementation of an environment. Manages instances, coordinates jobs, dispatches events.
- **Environment** — groups jobs into separate environments; jobs can coordinate only within the same environment.
  Access varies by implementation: `InProcessNode` (in-process), `LocalNode` (local sockets),
  distributed (TBI - Redis). Public entry points: `connect(env_id)` (config lookup with local fallback)
  and `create(env_config)` (explicit config). Type-specific factories (`_local`, `_in_process`) are private —
  the `EnvironmentConfig` Pydantic model is the single source of truth for defaults.

## TUI (`taro/tui/`)

Textual-based terminal UI. See `taro/docs/tui.md` for full architecture.

- **`taro dash`** — Live dashboard (active + history tables). `DashboardApp` → `DashboardScreen`.
- **`taro history`** — Interactive history table (default), `--no-pager` for plain text output.
- **Instance detail** — `InstanceScreen` with header, phase tree, phase detail, output panel.
- **Selector** — `select_instance()` modal for action commands.
- Shared table helpers in `tui/selector.py`: `add_columns`, `build_cells`, `row_key`, `LinkedTable`.

## Phase System

- The root phase lifecycle = job lifecycle. When the root phase completes, the job completes.
- `run_child(child)` — always use instead of `child.run(ctx)` directly (wires observers, registers children).
