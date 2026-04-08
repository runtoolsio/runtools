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

- Support dual output representation for clean vs verbose rendering.
  Keep `OutputLine.message` as the logical/plain message used by default in TUI/history/output files.
  Add an optional secondary display field for richer rendered output from logging capture (for example a
  formatted prefix or alternate rendered text) without replacing the primary message.
  Use verbose mode in `taro` to show the richer representation when present.
  Goals:
  - keep normal output clean and consistent
  - avoid duplicating full formatted lines when only a prefix/extra context is needed
  - preserve room for richer diagnostics without coupling default output to external logger formatting

- Document `taro` pattern-matching behavior per command.
  Current behavior differs between commands such as `history`, `ps`, `wait`, and `of`.
