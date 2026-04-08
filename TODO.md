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

- Structured output as the default model.
  Redesign `OutputLine` / JSONL around a canonical structured event shape instead of `fields` plus formatter-driven rendering.
  Normalize common envelope fields across logging integrations:
  - `ts`
  - `level`
  - `logger`
  - `msg`
  Keep application data flat at top level. Treat `rt_*` as tracking-only ingestion metadata rather than normal output fields.

- Render structured output in `taro`.
  Replace the current formatter/template-based verbose rendering with presentation derived from structured fields.
  Define a stable display policy:
  - normal mode shows `msg`
  - verbose mode composes richer output from `ts`, `level`, `logger`, and selected extra fields
  Keep tracking metadata out of normal output rendering.

- Improve `runcli` parsing into the same structured model.
  Add explicit parsing layers for external program output:
  - JSON line objects
  - `k=v` / logfmt
  - common text log patterns used in practice (including Java-style formats)
  Normalize recognized fields into the canonical output shape and fall back to plain `msg` on low-confidence input.

- Document `taro` pattern-matching behavior per command.
  Current behavior differs between commands such as `history`, `ps`, `wait`, and `of`.
