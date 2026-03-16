# Run Identity, Duplicates, and Retry Plan

## Goal

First-class duplicate handling and retry support with full observability. Every submission attempt
is visible in the runs table — no silent discards, no auxiliary tables.

UX goals:

- `job_id + run_id` stays readable and stable as the logical run key
- duplicate attempts are visible in the same runs view as normal executions
- `taro listen` / `taro dash` pick up duplicate events naturally
- retry/rerun is a caller-side concern, not framework magic


## Current State

The identity foundation is in place:

- `InstanceID` has first-class `ordinal`
- `job_id + run_id` is the logical run key
- persistence uniqueness is based on `(job_id, run_id, ordinal)`
- output files are separated per concrete execution


## Core Model

### 1. Single instance identity with first-class ordinal

`InstanceID`:

- `job_id`
- `run_id`
- `ordinal` (1-based, assigned by persistence)

`job_id + run_id` is the logical grouping key. `(job_id, run_id, ordinal)` is fully unique.


### 2. Every submission attempt is a run

There is no separate `duplicate_submissions` table. Every non-raising duplicate admission creates
a real row in the `runs` table with the next ordinal. The strategy only decides what happens
after creation:

- **Normal run**: CREATED → STARTED → ENDED
- **Duplicate (recorded)**: CREATED → ENDED (never started, stop reason visible)
- **Suppressed duplicate**: CREATED → ENDED (never started, hidden by default in UI)

This means:

- `started IS NULL AND ended IS NOT NULL` trivially filters "never ran" instances
- phase tree snapshot is stored on the duplicate row — diffs are observable
- timestamps are in the normal columns, no special lookup needed
- `taro listen` picks up lifecycle events for duplicates like any other run


### 3. Retries and reruns are just new ordinals

A retry or rerun creates another execution of the same logical run with a higher ordinal.
This is entirely a caller-side concern — the framework just creates instances.

```python
i = node.create_instance(..., on_duplicate=DuplicateStrategy.IGNORE)
if i.ordinal > 10:
    i.stop(StopReason.MAX_RETRIES)
else:
    i.run()
```


## Admission and Lifecycle

### 4. Duplicate handling at node admission time

Duplicate handling happens during `node.create_instance(...)`, not in `run()`.

- `run()` never mutates identity
- admission is where uniqueness and dedup belong
- callers receive the final admitted identity immediately


### 5. Persist before constructing the instance

Flow:

1. Resolve logical run + next ordinal
2. Initialize persistence row (CREATED)
3. Apply duplicate strategy (may end immediately)
4. Create the concrete `JobInstance`
5. Register / notify

No `activate()` step. Instance is only created after admission and persistence succeed.


## Duplicate Strategy

### 6. Strategy is an enum

```python
class DuplicateStrategy(Enum):
    RAISE = "raise"              # no instance created, raise DuplicateInstanceError
    DUPLICATE_ERROR = "duplicate" # create next ordinal, end immediately (visible in runs)
    SUPPRESS = "suppress"         # create next ordinal, end immediately (hidden by default)
    IGNORE = "ignore"             # create next ordinal, return to caller (caller decides)
```

Every non-raising strategy creates a real instance with the next ordinal.
The strategy only controls what happens after creation.

Two new `StopReason` values:

- `StopReason.DUPLICATE` — used by `DUPLICATE_ERROR` strategy
- `StopReason.SUPPRESSED` — used by `SUPPRESS` strategy

Both result in CREATED → ENDED with `started IS NULL`. The distinction allows UI to hide
suppressed runs by default while always showing duplicate errors.


### 7. Default is RAISE

Default behavior of `node.create_instance(...)` stays safe:

- reject duplicates
- raise `DuplicateInstanceError`
- no row created

This preserves the current contract.


### 8. DUPLICATE_ERROR — full observability

The duplicate instance is created and ended immediately. It appears in runs with:

- `created` timestamp
- `ended` timestamp (same or near-same)
- `started IS NULL`
- stop reason indicating duplicate
- phase tree snapshot of what was submitted

This is the recommended default for production jobs where you want to see every attempt.


### 9. SUPPRESS — quiet recording

Same as DUPLICATE_ERROR but the run is marked as suppressible. UI can hide these
by default while still allowing drill-down.


### 10. IGNORE — caller control

The instance is created with the next ordinal and returned to the caller without
starting or stopping it. The caller has full control:

```python
i = node.create_instance(..., on_duplicate=DuplicateStrategy.IGNORE)
if i.stop_reason:
    pass  # already stopped by something else
elif i.ordinal > max_retries:
    i.stop(StopReason.MAX_RETRIES)
else:
    i.run()
```


## Persistence API

### 11. Runs table replaces history + duplicates

Rename `history` table to `runs`. Drop `duplicate_submissions` table entirely.

The persistence API:

- `read_runs()` → `list[JobRun]`
- `iter_runs()` → `Iterator[JobRun]`

No `JobRunDetail`, no `HistoryEntries`, no `DuplicateSubmission`. Every run — normal,
duplicate, suppressed — is a `JobRun` with the same schema.

Filtering duplicates vs real executions:

- `started IS NOT NULL` → actually ran
- `started IS NULL AND ended IS NOT NULL` → created but never started (duplicate/suppressed)
- `ended IS NULL` → still in progress or init-only


### 12. Show ordinal only when useful

UI goal:

- when a logical run has only one execution, don't clutter
- when there are multiple ordinals, show the ordinal column

Suppressed runs hidden by default with option to show.


## Construction API

### 13. Remove OutputRouter

The output path should be simplified so node/instance wiring deals directly with:

- in-memory tail buffering
- output storages / writers
- output observers

Batching strategy remains writer-owned.


## Remaining Implementation

1. Rename `history` table to `runs`. Drop `duplicate_submissions` table. Bump schema version.
2. Implement `DuplicateStrategy` enum with RAISE / DUPLICATE_ERROR / SUPPRESS / IGNORE.
3. Update `node.create_instance()` to handle non-raising strategies (create row, apply strategy).
4. Move toward persistence-first admission, then instance construction.
5. Update CLI/TUI to filter/display duplicate runs naturally.
6. Clean up: remove `DuplicateSubmission`, `JobRunDetail`, `_fetch_duplicates_for_runs`,
   `record_duplicate_submission`, `read_duplicate_submissions`.
