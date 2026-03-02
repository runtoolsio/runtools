# Pre-Beta Release Evaluation

Date: March 2, 2026
Scope: `runcore`, `runjob`, `runcli`, `taro` (first pre-beta release review)

## Overall Verdict: Not yet — but close

The core runtime design is solid. The abstractions (phases, lifecycle, observers, persistence, coordination) are
well-thought-out and mature enough to lock down. What blocks release is mostly **packaging/plumbing** — not
architecture. Estimated 2-3 focused days to clear the blockers.

---

## Cross-Cutting Blockers

These affect multiple packages and must be fixed first.

### 1. Broken dependency metadata (RELEASE BLOCKER)

Isolated `pip install` will fail with `ModuleNotFoundError`:

- `runjob/pyproject.toml` — `runtools-runcore` is **commented out**
- `runcli/pyproject.toml` — declares only `rich-argparse`, missing both `runcore` and `runjob`
- `taro/pyproject.toml` — `runtools-runcore` is **commented out**

### 2. Python version floor mismatch (RELEASE BLOCKER)

`runjob` declares `>=3.10` but uses:

- `typing.override` (3.12+) in `instance.py`, `node.py`, `server.py`
- `ExceptionGroup` handling (3.11+) in `instance.py`

Fix: bump to `>=3.12` or add `typing_extensions` fallback.

### 3. No CLI entry points (HIGH)

Neither `runcli` nor `taro` define `[project.scripts]` in `pyproject.toml`. Users must use
`python -m runtools.runcli` instead of `run job ...`.

### 4. Zero tests in runcli and taro (HIGH)

Both `test/` directories are empty. At minimum need smoke tests for CLI parsing and basic integration.

### 5. Thread exception leak in runjob tests (MEDIUM)

`test_exec_queue.py` triggers unhandled thread exception warnings from stop/cancellation in `instance.py`.
Should be contained or fixed.

---

## Package-by-Package Assessment

### runcore — Ready (with minor polish)

**Strengths:**

- Clean domain model — `RunLifecycle`, `PhaseRun`, `JobRun` are frozen dataclasses, well-designed
- Strong separation: lifecycle, persistence, querying, RPC are all cleanly isolated
- SQLite persistence with two-phase INSERT/UPDATE design and schema versioning (v1)
- Observer pattern with priority ordering — elegant
- Criteria/filtering API (`JobRunCriteria`, `MetadataCriterion`, etc.) is powerful and serializable
- 70 tests passing, good coverage of core paths

**Issues to address:**

| Priority | Issue | Detail |
|----------|-------|--------|
| HIGH | No public `__init__.py` exports | Users must know internal module names to import anything. Should export core types: `connect`, `JobRun`, `InstanceID`, `JobRunCriteria`, `TerminationStatus`, etc. |
| HIGH | `Watcher` class is nested and untyped | Returned from `connector.watcher()` but has no public type — opaque to callers and IDEs |
| MEDIUM | `iid()` function name | Cryptic. Should be `parse_instance_id()` or similar |
| MEDIUM | `get_run()` swallows `PersistenceDisabledError` | Returns `None` — ambiguous ("not found" vs "persistence off") |
| LOW | `InstanceObservableNotifications` | Verbose. Consider `InstanceObservations` |
| LOW | `InstanceID` as Sequence | Adds complexity; a simple `NamedTuple` or frozen dataclass would be clearer |

**Test gaps:** No integration tests for full connector lifecycle, observer priority ordering, event
serialization, or concurrent persistence access.

---

### runjob — Ready (with API clarity work)

**Strengths:**

- Phase system is the crown jewel — clean lifecycle (CREATED -> RUNNING -> ENDED), immutable snapshots via
  `snap()`, visitor pattern
- Coordination phases cover all major patterns: checkpoint, approval, mutex, dependency, queue
- Decorator composition (`@job`, `@phase`, `@timeout`, `@checkpoint`, `@approval`, `@mutex`, `@queue`) is
  ergonomic
- Method chaining: `inst.activate().notify_created()` is clean
- 105 tests passing with good behavioral coverage

**Issues to address:**

| Priority | Issue | Detail |
|----------|-------|--------|
| HIGH | `_JobInstance` exported with underscore | The only concrete `JobInstance` is named private but used publicly. Rename to `JobInstance` or provide factory |
| HIGH | Minimal `__init__.py` exports | Same problem as runcore — no clear public API surface |
| HIGH | Phase context type `C` is too abstract | Users get no IDE support; need Protocol-based typing for `OutputContext`, `JobInstanceContext` |
| MEDIUM | Naming inconsistencies | `MutualExclusionPhase` (verbose — `MutexPhase`?), `ExecutionQueue` (missing `Phase` suffix), `TimeoutExtension` vs `*Phase` |
| MEDIUM | Thread safety in `find_phase_control()` | Iterates `_children` without lock, but RPC server calls from other threads |
| MEDIUM | Observer errors silently logged as faults | No alerting mechanism — users may not realize observers are failing |
| LOW | Five different `run()` methods | `run()` on phase, instance, managed instance, `run_in_new_thread()`, `run_child()` — could confuse |
| LOW | No async/await support | Threading only. Limits async framework integration |

**Test gaps:** No concurrency stress tests, no deeply-nested phase composition tests, no ProcessPhase queue
overflow tests.

---

### taro — Functional, needs testing

**Strengths:**

- 14 CLI commands covering the full ops workflow (ps, history, live, stop, wait, approve, resume, tail, stats,
  dash, clean, env, of, listen)
- TUI architecture is sound — event-driven with proper `call_from_thread()` bridging, no direct widget mutation
  from background threads
- `LinkedTable` (cursor wrapping between active/history tables) is a nice UX touch
- Instance detail screen with phase tree + output filtering by phase subtree
- Clean integration with runcore — uses only public connector/criteria APIs

**Issues to address:**

| Priority | Issue | Detail |
|----------|-------|--------|
| HIGH | Zero tests | Empty test directory |
| HIGH | Pattern matching inconsistency | `ps`/`history` use PARTIAL (substring), `wait` uses FN_MATCH (glob), `of` uses strict `job@run`. Not documented per-command |
| MEDIUM | No TUI footer with keybindings | Users must discover Esc/q works. Should show `Esc Quit  ↑↓ Navigate  Enter Select` |
| MEDIUM | No debouncing in TUI event updates | Every event triggers full widget refresh — may flicker under high event rates |
| MEDIUM | Observer lifecycle in TUI | Manual unsubscribe on unmount — no context manager safety net |
| LOW | 9 date filter flags on `history`/`stats` | `--today`, `--yesterday`, `--week`, `--fortnight`, `--three-weeks`, `--four-weeks`, `--month`, `--days-back`, `--from/--to`. Works but verbose |
| LOW | No `--columns` customization | Fixed column sets in ps/history/live |

---

### runcli — Needs packaging fixes

**Strengths:**

- Clean vertical slice architecture — 857 lines total, each module single-responsibility
- Comprehensive CLI options covering all runjob coordination features
- Smart rerun suffix logic for duplicate instance handling
- Proper signal handling (SIGTERM, custom timeout signal)
- Phase tree construction follows correct composition order (mutex -> queue -> checkpoint -> extensions)

**Issues to address:**

| Priority | Issue | Detail |
|----------|-------|--------|
| BLOCKER | Missing dependencies in pyproject.toml | Only declares `rich-argparse` — missing `runcore` and `runjob` |
| BLOCKER | No `[project.scripts]` entry point | Can't run as `run job ...` |
| HIGH | Zero tests | Empty test directory |
| HIGH | `-p/--grok-pattern` declared but not implemented | Flag silently accepts but ignores patterns. Remove or guard with error |
| MEDIUM | `ACTION_SERVICE` constant unused | Forward declaration with no implementation — confusing |
| LOW | Config loaded but unused for job behavior | Only affects logging setup |

---

## API Stability Assessment

### Lock down now (mature, should not change)

| Abstraction | Package | Why stable |
|-------------|---------|-----------|
| Phase lifecycle (CREATED -> RUNNING -> ENDED) | runcore/runjob | Core model, frozen dataclasses |
| `TerminationStatus` enum | runcore | Well-defined, covers all cases |
| `JobRun` / `PhaseRun` snapshots | runcore | Immutable, serializable |
| Observer priority protocol | runcore | Documented, enforced by convention |
| SQLite schema v1 | runcore | Versioned, migration-ready |
| Coordination phases | runjob | Checkpoint, approval, mutex, queue — all proven |
| `@job` / `@phase` decorators | runjob | Clean, ergonomic |
| CLI command names | taro | `ps`, `history`, `stop`, `wait`, `dash` etc. are intuitive |

### Expect to evolve (document as unstable)

| Abstraction | Package | Why |
|-------------|---------|-----|
| Watcher API | runcore | Not widely used, no public type |
| OutputBackend interface | runcore | New backends may require changes |
| Plugin system | runcore | Immature |
| Status tracking handlers | runjob | Handler protocol not formalized |
| Phase context type `C` | runjob | Needs Protocol definitions |
| TUI widget internals | taro | Layout and CSS will evolve |
| runcli config format | runcli | Only logging today, will grow |

---

## Recommended Release Gates

In priority order:

1. **Fix dependency metadata** in all `pyproject.toml` files (~30 min)
2. **Bump `requires-python`** to `>=3.12` in runjob (or add compatibility shims) (~15 min)
3. **Add `[project.scripts]`** entry points for runcli and taro (~15 min)
4. **Remove or guard `-p/--grok-pattern`** in runcli (~15 min)
5. **Add public API exports** to `__init__.py` in runcore and runjob (~2-4 hours)
6. **Add smoke tests** for runcli (CLI parsing, basic job execution) and taro (command smoke tests) (~4-8 hours)
7. **Fix thread exception leak** in runjob's exec queue tests (~1-2 hours)
8. **Document pattern matching behavior** per taro command (~1 hour)

Items 1-4 are mechanical fixes. Item 5 is the most impactful design decision — it defines the public contract.
Items 6-8 are quality gates.

---

## Validation Summary

- `runcore`: 70 passed
- `runjob`: 105 passed, 1 warning (thread exception)
- `runcli`: CLI help smoke check passed
- `taro`: CLI help smoke check passed (`PYTHONPATH=src`)
