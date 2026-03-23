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

- Add `OutputLine.status_type` derived property.
  Returns `'operation'`, `'event'`, or `'result'` based on parsed fields, `None` otherwise.
  Enables output views (tail, TUI panels) to filter out status-tracking lines from user-visible output.

- Document `taro` pattern-matching behavior per command.
  Current behavior differs between commands such as `history`, `ps`, `wait`, and `of`.
