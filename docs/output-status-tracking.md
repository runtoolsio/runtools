# Output & Status Tracking

How jobs report progress and how that progress is captured, parsed, and exposed.

## 1. StatusTracker — the core concept

`StatusTracker` accumulates status updates from a running job. It tracks the current event,
ongoing operations with progress, warnings, and the final result.

```python
from runtools.runjob.track import StatusTracker

tracker = StatusTracker()

tracker.event("Downloading files")           # set current event
tracker.operation("download").update(3, 10)  # 3 of 10 done
tracker.warning("Disk almost full")
tracker.result("done")

print(tracker.to_status())  # done  (!Disk almost full)
```

| Method                    | Purpose                                            |
|---------------------------|----------------------------------------------------|
| `event(text)`             | Set the current event (replaces previous)          |
| `operation(name)`         | Get or create a named `OperationTracker`           |
| `warning(text)`           | Append a warning (all warnings are kept)           |
| `result(text)`            | Set the final result and deactivate all operations |
| `to_status()`             | Return an immutable `Status` snapshot              |

`OperationTracker.update(completed, total, unit)` updates progress. If the new `completed` value
is greater than the current one, it's treated as an absolute value; otherwise as an increment.

> **Package:** `runtools.runjob.track`

## 2. Status — the snapshot

`to_status()` returns a frozen `Status` dataclass. All fields are immutable.

```python
status = tracker.to_status()

status.last_event        # Event("Downloading files", <timestamp>)
status.operations[0]     # Operation(name="download", completed=3, total=10, ...)
status.warnings          # [Event("Disk almost full", <timestamp>)]
status.result            # Event("done", <timestamp>)
```

`Status.__str__()` formats a human-readable status line:

- Active operations shown as `[name completed/total (pct%)]`
- If no active operations, the last event text is shown
- If a result is set, it replaces everything
- Warnings are appended as `(!warning1, warning2)`

```python
str(status)  # 'done  (!Disk almost full)'
```

> **Package:** `runtools.runcore.status` — `Event`, `Operation`, `Status`

## 3. OutputLine — structured output

`OutputLine` is a frozen dataclass representing a single line of job output. It carries both
the raw text and optional structured fields.

```python
from runtools.runcore.output import OutputLine

line = OutputLine("some message", ordinal=1, fields={"event": "started", "completed": 5})
```

| Field      | Type              | Purpose                                         |
|------------|-------------------|-------------------------------------------------|
| `message`  | `str`             | Human-readable text                             |
| `ordinal`  | `int`             | Sequence number for ordering                    |
| `is_error` | `bool`            | Whether this is error output (default `False`)  |
| `source`   | `Optional[str]`   | Identifier of the output source                 |
| `fields`   | `Optional[dict]`  | Structured key-value data                       |

> **Package:** `runtools.runcore.output`

## 4. Connecting output to status — StatusTracker as observer

`StatusTracker` implements `OutputObserver`. When it receives an `OutputLine`, its built-in
`combined_output_handler` reads the `fields` dict and updates the tracker automatically:

```python
tracker = StatusTracker()
tracker.new_output(OutputLine("msg", ordinal=1, fields={"event": "upload", "completed": 5, "total": 10}))

print(tracker.to_status())  # [upload 5/10 (50%)]
```

The default handler (`combined_output_handler`) works as follows:
- If the line has `fields` — extract status from them (via `field_based_handler`)
- Otherwise — treat the entire `message` as an event

**Recognized field names:**

| Field       | Effect                                                           |
|-------------|------------------------------------------------------------------|
| `event`     | Sets the current event; also used as operation name if progress fields are present |
| `operation` | Explicit operation name (takes precedence over `event` for naming operations)      |
| `completed` | Completed count for the operation                                |
| `total`     | Total count for the operation                                    |
| `unit`      | Unit label for the operation (e.g., "files", "MB")               |
| `result`    | Sets the final result                                            |
| `timestamp` | Timestamp for the update (defaults to now)                       |

## 5. Parsing text into fields — KVParser + ParsingPreprocessor

If your job prints key-value pairs in its output, `KVParser` extracts them into fields
and `ParsingPreprocessor` wraps the parsers into a callable that transforms `OutputLine` objects.

```python
from runtools.runcore.util.parser import KVParser
from runtools.runjob.output import ParsingPreprocessor, OutputSink

preprocessor = ParsingPreprocessor([KVParser()])

line = OutputLine("event=[downloading] completed=[5] total=[10]", ordinal=1)
parsed = preprocessor(line)
# parsed.fields == {'event': 'downloading', 'completed': '5', 'total': '10'}
```

`KVParser` supports bracket-wrapped values (`key=[value]`, `key=<value>`, `key=(value)`) by default.
Standard `key=value` pairs (without brackets) also work.

If a line already has `fields` (e.g., from structured logging), `ParsingPreprocessor` passes it through unchanged.

**KVParser options:**

| Parameter          | Default | Purpose                                                  |
|--------------------|---------|----------------------------------------------------------|
| `prefix`           | `""`    | Prepend to all extracted keys                            |
| `field_split`      | `" "`   | Characters used as field delimiters                      |
| `value_split`      | `"="`   | Characters used as key-value delimiters                  |
| `include_brackets` | `True`  | Treat `[]`, `<>`, `()` as value wrappers                 |
| `exclude_keys`     | `()`    | Keys to skip                                             |
| `aliases`          | `None`  | Dict mapping extracted keys to standard names            |

> **Package:** `runtools.runcore.util.parser` — `KVParser`, `IndexParser`

## 6. OutputSink — wiring it together

`OutputSink` is the hub that connects parsing to observers. It accepts `OutputLine` objects,
runs them through an optional preprocessor, and forwards to all registered observers.

```python
sink = OutputSink(ParsingPreprocessor([KVParser()]))
tracker = StatusTracker()
sink.add_observer(tracker)

sink.new_output(OutputLine("event=[downloading] completed=[5]", ordinal=1))
print(tracker.to_status())  # downloading
```

The flow: **OutputLine → preprocessing (parsing) → observers (StatusTracker, OutputRouter, ...)**

## 7. In a job instance — auto-wiring

`instance.create()` sets up the full pipeline for you. It creates default `OutputSink`,
`OutputRouter`, and `StatusTracker`, then auto-wires the tracker as an output observer.

```python
from runtools.runjob import instance
from runtools.runjob.output import OutputSink, ParsingPreprocessor
from runtools.runcore.util.parser import KVParser

sink = OutputSink(ParsingPreprocessor([KVParser()]))
inst = instance.create(instance_id, env, root_phase, output_sink=sink)
inst.run()

# StatusTracker was auto-wired — status available via:
run = inst.to_run()
print(run.status)  # Status snapshot from the auto-created tracker
```

What `create()` does automatically:
1. Creates `OutputSink` if not provided (no parsing by default)
2. Creates `OutputRouter` with an in-memory tail buffer (last 50 lines)
3. Creates `StatusTracker`
4. Registers the tracker as an observer on the sink: `output_sink.add_observer(status_tracker)`

During `inst.run()`, log records from the root logger are also captured into the sink
(filtered to the current instance).

## 8. Structured logging — fields without parsing

Instead of printing key-value text for `KVParser` to parse, you can use Python's logging
with `extra` fields. The `OutputSink` log handler extracts extras automatically — no parser needed.

```python
import logging
log = logging.getLogger("my_job")

log.info("progress", extra={"event": "upload", "completed": 45, "total": 100})
# OutputSink's log handler extracts extras → OutputLine.fields automatically
# No KVParser needed — fields are already structured
```

This works because `OutputSink.capturing_log_handler()` creates a handler that:
1. Formats the log record into `OutputLine.message`
2. Extracts all non-built-in attributes from the `LogRecord` into `OutputLine.fields`

The `instance.create()` auto-wiring captures logs from the root logger during `inst.run()`.

## 9. runcli `--kv-filter`

The `run` CLI wraps all of the above into command-line flags:

```bash
# Enable KV parsing for status tracking
run --kv-filter ./my-script.sh

# Map custom output keys to recognized field names
run --kv-filter --kv-alias "count=completed" ./my-script.sh
```

`--kv-filter` (`-k`) creates an `OutputSink(ParsingPreprocessor([KVParser()]))`.
`--kv-alias` maps output keys to standard field names (e.g., your script prints `count=5`
but the tracker expects `completed`).

So if your script prints:

```
event=[downloading] count=[5] total=[10]
```

Then `run -k --kv-alias "count=completed" ./my-script.sh` will track it as:
`[downloading 5/10 (50%)]`

---

## Data flow summary

```
         ┌──────────────────────────────────────┐
         │            Job Output                │
         │  (stdout/stderr or logging.info())   │
         └─────────────────┬────────────────────┘
                           │
                           ▼
         ┌──────────────────────────────────────┐
         │            OutputSink                │
         │  ┌────────────────────────────────┐  │
         │  │ ParsingPreprocessor (optional) │  │
         │  │  └─ KVParser / IndexParser     │  │
         │  └────────────────────────────────┘  │
         └───────────┬──────────┬───────────────┘
                     │          │
         ┌───────────▼──┐  ┌───▼──────────────┐
         │StatusTracker │  │ OutputRouter     │
         │              │  │ (tail buffer,    │
         │ .to_status() │  │  file storage)   │
         └──────────────┘  └──────────────────┘
```

## Source files

| File | Package | Contains |
|------|---------|----------|
| `runjob/track.py`       | `runtools.runjob.track`       | `StatusTracker`, `OperationTracker`, output handlers |
| `runcore/status.py`     | `runtools.runcore.status`     | `Event`, `Operation`, `Status`                       |
| `runcore/output.py`     | `runtools.runcore.output`     | `OutputLine`, `OutputObserver`, `OutputLineFactory`  |
| `runjob/output.py`      | `runtools.runjob.output`      | `OutputSink`, `ParsingPreprocessor`, `OutputRouter`  |
| `runcore/util/parser.py`| `runtools.runcore.util.parser` | `KVParser`, `IndexParser`                           |
| `runjob/instance.py`    | `runtools.runjob.instance`    | `create()` factory with auto-wiring                  |
| `runcli/__init__.py`    | `runtools.runcli`             | `_build_output_sink()`, CLI integration              |
