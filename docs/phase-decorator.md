# `@phase` Decorator

Define phases as plain functions. When called inside a running phase's `_run()`, a child phase
is created automatically and the result is returned.

```python
from runtools.runjob.phase import BasePhase, phase

@phase
def fetch(url):
    return requests.get(url).json()

@phase
def transform(data):
    return [x * 10 for x in data]

class MyPipeline(BasePhase):
    TYPE = "PIPELINE"
    def __init__(self):
        super().__init__("pipeline", self.TYPE)

    def _run(self, ctx):
        data = fetch("http://example.com")
        return transform(data["items"])

    def _stop_running(self, reason):
        pass
```

## Decorator Variants

```python
@phase                          # type auto-derived: fetch_data -> FETCH_DATA
@phase("CUSTOM")                # explicit type
@phase(phase_type="CUSTOM")     # keyword form
```

## Nesting

`@phase` functions can call other `@phase` functions. Children are registered at each level:

```python
@phase
def inner():
    return "result"

@phase
def outer():
    return inner()    # inner becomes a child of outer
```

## Exception Handling

Exceptions from `@phase` functions propagate with their **original type**. The parent can catch
them normally:

```python
@phase
def risky_step():
    raise ValueError("bad input")

class MyPipeline(BasePhase):
    def _run(self, ctx):
        try:
            risky_step()
        except ValueError:
            return use_fallback()     # parent handles error, completes as COMPLETED
```

The child gets **ERROR** status. Since the parent caught the exception and returned normally,
the parent completes as **COMPLETED** — it had the chance to handle the error and chose to continue.

If the parent does NOT catch the exception, it propagates up and the parent also gets **ERROR**.

## Stopping

When a phase is stopped (via `stop()`), dynamically created `@phase` children don't exist yet,
so they can't be stopped directly. Instead, `run_child()` checks for the stop signal before
running each new child. This means:

- Any `@phase` call on a stopped parent raises `PhaseTerminated` immediately
- The currently running `@phase` function runs to completion (it's non-interruptible)
- No new `@phase` functions will start after the stop

```
pipeline.stop() called
  fetch(url)          # already running — runs to completion
  transform(data)     # run_child raises PhaseTerminated — never starts
```

## `PhaseTerminated` and `ChildPhaseTerminated`

`PhaseTerminated` is a phase-internal signal — a phase raises it to tell its own `run()` "I'm
done with this status." The `@phase` wrapper bridges this: if a child terminated non-successfully
(via `PhaseTerminated` or pre-termination), the wrapper raises `ChildPhaseTerminated` so the
parent can see and handle it.

```python
from runtools.runjob.phase import ChildPhaseTerminated, PhaseTerminated

@phase
def fetch_primary(url):
    raise PhaseTerminated(TerminationStatus.FAILED, "primary down")

@phase
def fetch_fallback(url):
    return {"data": [1, 2, 3]}

class MyPipeline(BasePhase):
    def _run(self, ctx):
        try:
            data = fetch_primary(url)
        except ChildPhaseTerminated:        # catches child failure only
            data = fetch_fallback(url)      # fallback — parent continues
        # PhaseTerminated from stop/timeout is NOT caught — propagates correctly
        return transform(data)
```

`ChildPhaseTerminated` extends `PhaseTerminated`, so the catching hierarchy is:
- `except ChildPhaseTerminated` — only child failures
- `except PhaseTerminated` — both child failures and own stop/timeout

If unhandled, `ChildPhaseTerminated` propagates to the parent's `run()` where `except
PhaseTerminated` catches it — the parent terminates with the child's status.

### Prefer regular exceptions

For most cases, regular exceptions are simpler and avoid coupling to library types:

```python
@phase
def fetch_data(url):
    raise ValueError("not found")    # child gets ERROR status

def _run(self, ctx):
    try:
        fetch_data(url)
    except ValueError:
        return use_fallback()         # parent completes as COMPLETED
```

Use `PhaseTerminated` in `@phase` functions only when you need a specific termination status
(e.g., FAILED vs ERROR) or for cooperative stopping.

### Future: FAILED vs ERROR without library coupling

Planned — a `failures` parameter on the decorator:

```python
@phase(failures=[ValueError, HTTPError])
def fetch_data(url):
    if not_found:
        raise ValueError("not found")    # -> FAILED (declared as expected failure)
    return data                           # IOError -> ERROR (not declared)
```

The function raises plain domain exceptions. The decorator knows which ones are expected failures.
Zero coupling to library types.
