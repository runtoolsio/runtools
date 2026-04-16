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
  on `OutputSink` that's set when the phase starts, so all threads sharing the sink get the correct source.

- Align `runcli` `JsonFormatter` keys with `OutputLine.serialize()` schema.
  `OutputLine` now uses plain top-level keys (`msg`, `ts`, `lvl`, `logger`, `thread`) with `_rt` for
  runtools-internal metadata. `runcli/log.py` `JsonFormatter` already uses plain keys but lacks `_rt`.
  Document reserved keys and collision policy (reserved keys = undefined behavior if user extras collide).

- Document `taro` pattern-matching behavior per command.
  Current behavior differs between commands such as `history`, `ps`, `wait`, and `output`.
