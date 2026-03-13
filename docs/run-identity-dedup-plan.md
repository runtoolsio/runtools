# Run Identity, Duplicates, and Retry Plan

## Goal

Introduce first-class duplicate handling and retry support before the first public release, without relying on
awkward `run_id` suffixes.

The main UX goal is:

- keep the submitted `job_id + run_id` readable and stable as the logical run key
- show retries/re-runs cleanly in history
- track duplicate submissions without losing observability

## Current State

The identity foundation is now in place:

- `InstanceID` has first-class `ordinal`
- `job_id + run_id` is treated as the logical run key
- persistence uniqueness is based on `(job_id, run_id, ordinal)`
- exact matching/removal paths carry ordinal
- output files are separated per concrete execution

The remaining work is duplicate admission, retry/rerun orchestration, and history/CLI/TUI presentation.

## Core Model

### 1. Use a single instance identity with first-class ordinal

`InstanceID` should become:

- `job_id`
- `run_id`
- `ordinal`

This is the only fully unique identity of a concrete execution.

`job_id + run_id` remains the logical grouping key, but not a fully unique instance identity by itself.


### 2. Retries and reruns are new ordinals of the same logical run

A retry or rerun does **not** change `job_id + run_id`.

Instead, it creates another execution of the same logical run with a higher ordinal.

This gives cleaner history than mutating `run_id` to values like `T14:00-2`.


### 3. Duplicate submissions are tracked separately from executions

Duplicate submissions should not be silently discarded.

We will store duplicate submission events in a small auxiliary table, linked to the logical run, containing at least:

- logical run key (`job_id`, `run_id`)
- timestamp
- optional reason / source

This table is not a second history stream. It is auxiliary metadata attached to the main run history.


## Admission and Lifecycle

### 4. Duplicate handling stays at node admission time

Duplicate handling should happen during `node.create_instance(...)`, not in `run()`.

Reasons:

- `run()` should not mutate identity
- admission is where uniqueness and dedup belong
- callers should receive the final admitted identity immediately


### 5. Create/persist before constructing the final runnable instance

We want to move toward:

1. resolve logical run + ordinal
2. initialize persistence row
3. create the concrete `JobInstance`
4. register / notify it

This is cleaner than creating an instance first and later activating it.

`notify_created` should remain a separate concern.

Planned lifecycle change:

- remove `activate()` from the admission flow
- create the instance only after admission/persistence succeeds
- emit created notifications after successful registration


## Duplicate Policy

### 6. Default policy remains raise

Default behavior of `node.create_instance(...)` stays:

- reject duplicates
- raise `DuplicateInstanceError`

This preserves the current contract and keeps behavior safe by default.


### 7. Built-in policies should be exposed as constants / strategy objects

We do not need an enum as the main abstraction.

Preferred shape:

- built-in policy constants / objects such as `OnDuplicate.RAISE`, `OnDuplicate.IGNORE`, `OnDuplicate.SKIP`,
  `OnDuplicate.RERUN`
- room for custom policy objects later

The policy abstraction should stay simple and admission-focused.


### 8. Ignore/skip semantics

We do not need a fully silent ignore mode.

If a duplicate is ignored/skipped, we still want observability:

- duplicate event recorded
- main history remains understandable
- CLI can optionally surface duplicate counts/details


## Retry

### 9. Retry gets its own node-level entry point

Retry should not be collapsed into duplicate policy.

Retry is a higher-level concern and will get its own node method, with factory-based recreation support.

Duplicate handling and retry must work together, but they are separate mechanisms.


### 10. No `JobInstance` should be created before admission resolves the final instance identity

We should not create a concrete `JobInstance` until duplicate handling and persistence have resolved the final
logical run + ordinal.

This means:

- duplicate handling happens before instance construction
- retry/rerun does not clone `JobInstance`
- node resolves the final instance identity first
- persistence initializes the run
- only then is the concrete `JobInstance` created


## Construction API

### 11. Remove `OutputRouter` from the target design

The redesigned instance construction should not keep `OutputRouter` as a separate abstraction.

The output path should be simplified so node/instance wiring deals directly with:

- in-memory tail buffering
- output storages / writers
- output observers

Batching strategy should remain writer-owned, not centralized.


## Persistence and History UX

### 12. History view should answer "what happened?"

The history model is about what happened operationally, not only what executed successfully.

This is why:

- retries/re-runs should be visible as separate executions of the same logical run
- duplicate submissions should still be observable


### 13. Show ordinal only when useful

History UX goal:

- when a logical run has only one execution, do not clutter the UI
- when there are multiple executions, show the ordinal column so retries and reruns are immediately visible

Duplicates can be shown as a count, with drill-down/detail later if needed.

## Remaining Implementation Direction

1. Add an auxiliary duplicates table with logical run key + timestamp (+ optional reason/source).
2. Keep duplicate detection in `node.create_instance(...)`.
3. Move toward persistence-first admission, then instance construction.
4. Introduce a node-level `InstanceComponentProvider`-style object for recreatable runtime parts.
5. Add a dedicated node retry entry point that resolves the next ordinal first and only then creates the
   concrete `JobInstance`.
6. Surface repeated executions cleanly in history, CLI, and TUI, showing ordinal only when it adds value.
