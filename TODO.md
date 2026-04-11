# TODO

## Release-Critical

- Add smoke/integration tests for `runcli` and `taro`.
  Cover at least: `run` job execution, `taro history --no-pager`, `taro stats`, `taro of`, and one live-command path.

- Reduce `taro` dependence on live connector sockets for read-only commands.
  Use direct DB/output access for non-interactive read-only paths such as `history --no-pager`, `stats`, `inspect`, and `of`.

- Add clear public API exports in `runcore` and `runjob`.
  Avoid forcing users to import from internal modules for core types and entry points.

- Fix the thread exception leak in `runjob` cancellation/exec-queue tests.

## Important

- Fix output source (`_current_phase`) not propagating to child threads.
  `ContextVar` doesn't auto-propagate to threads spawned by phases. Output from `ThreadPoolExecutor` workers
  in `FunctionPhase` gets `source=None`. Consider replacing the context var approach with a `default_source`
  on `OutputSink` that's set when the phase starts, so all threads sharing the sink get the correct source.

- Define and document reserved-key / collision handling across structured logging and output.
  Cover all related paths consistently:
  - `OutputLine.serialize()` and stored JSONL envelope keys
  - `runcli` `JsonFormatter`
  - dict/JSON ingestion in `StdLogOutputLink` and `OutputParser`
  Decide a single policy for collisions with canonical keys (`msg/ts/lvl/logger/...` or underscored storage variants):
  reject, drop, rename, or namespace conflicting user fields.

- Document `taro` pattern-matching behavior per command.
  Current behavior differs between commands such as `history`, `ps`, `wait`, and `of`.
