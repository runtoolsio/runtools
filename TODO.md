# TODO

## Release-Critical

- Add smoke/integration tests for `runcli` and `taro`.
  Cover at least: `run` job execution, `taro history --no-pager`, `taro jobs`, `taro output`, and one live-command path.

- Reduce `taro` dependence on live connector sockets for read-only commands.
  Use direct DB/output access for non-interactive read-only paths such as `history --no-pager`, `output`, `inspect`.

- Add clear public API exports in `runcore` and `runjob`.
  Avoid forcing users to import from internal modules for core types and entry points.

- Fix the thread exception leak in `runjob` cancellation/exec-queue tests.

## Important

- Fix output source (`_current_phase`) not propagating to child threads.
  `ContextVar` doesn't auto-propagate to threads spawned by phases. Output from `ThreadPoolExecutor` workers
  in `FunctionPhase` gets `source=None`. Consider replacing the context var approach with a `default_source`
  on `OutputPipeline` that's set when the phase starts, so all threads sharing the sink get the correct source.

- Align `runcli` `JsonFormatter` keys with `OutputLine.serialize()` schema.
  `OutputLine` now uses plain top-level keys (`msg`, `ts`, `lvl`, `logger`, `thread`) with `_rt` for
  runtools-internal metadata. `runcli/log.py` `JsonFormatter` already uses plain keys but lacks `_rt`.
  Document reserved keys and collision policy (reserved keys = undefined behavior if user extras collide).

- Document `taro` pattern-matching behavior per command.
  Current behavior differs between commands such as `history`, `ps`, `wait`, and `output`.

## Output Format Plan

### Run snapshot (separate layer — NOT the output schema)

- **Split `Fault.reason` → `type` + `message`.**
  `from_exception` currently merges `f"{ClassName}: {exception}"` — lossy to re-split at
  export. Store type/message separately; update serialize/deserialize and the test in
  `runcore/src/runtools/runcore/test/job.py`. Map `category` → `runtools.fault.*`.
  Resolve the two stack-trace homes (`Fault.stack_trace` vs `TerminationInfo.stack_trace`).
  Note: output-stream exceptions stay free-text lines (`is_error`); runtools does not
  synthesize structure from arbitrary tracebacks.

### Deferred — export boundary (separate effort, not the on-disk schema)

- **OTel/ECS export adapters** (`to_otel()` / `to_ecs()`) + OTLP sink.
  Mapping tables in the plan are documentation-only until this exists.

- **Resource mapping.** Identity (`job_id`/`run_id`/`ordinal`/`node_id`) →
  `service.name` / `service.instance.id` + `runtools.*`, attached at Resource level
  (set once), NOT duplicated per log line.

- **Trace correlation.** Promote `trace_id`/`span_id` to first-class envelope fields and
  map to OTel `TraceId`/`SpanId`. Pass-through preserves them in `fields` today.

- **Exception export.** Native `type`/`message`/`stacktrace` → OTel `exception.*`;
  `category` → `runtools.fault.*`.
