# Environment

An environment isolates job instances from each other. Each environment has its own database, live-instance
directory, and output backends.

In practical terms, an environment is a named place where jobs coordinate, connectors observe/control them, and run
history is stored. Most users only need two ideas:

- `local` is the built-in environment for zero-config local use
- additional environments can be added in `environments.toml`

## Structure

An environment is selected by its **kind** — a curated backend bundle naming a supported combination of storage,
live transport, and coordination (not independent knobs):

- **`local`** — SQLite storage + unix-socket instance directory + file locks. The zero-config default.
- **`postgres`** — PostgreSQL storage + DB-polling instance directory. Observe-only for now: connectors work,
  running a node raises until the producer slice lands.

Each kind composes:

- **Database** — persistent state: configuration, run history, future permissions. The environment is centered on its
  database, which is the authoritative source of truth.
- **Instance directory** — the consumer's view of live instances (how connectors find, observe, and control them).
  Chosen by the kind: unix sockets for `local`, DB polling for `postgres`.
- **Output backends** — output routing/storage for instance stdout/stderr and structured output. Configured from the
  database via `output.storages`, independent of the kind.

The database is the source of truth. Output backend and kind-specific configuration are stored in the database
alongside everything else.

The database is not optional. Every environment has a database, though the concrete implementation may be persistent or
transient/in-memory (in-process environments).

## Built-in Local

The `local` environment is always available. It does not require a registry entry and cannot be created, deleted, or
redefined.

- Its DB path is deterministic: `~/.local/share/runtools/local.db`
- The DB is auto-created on first use by runjob/runcli
- The name `local` is reserved — it cannot appear in the registry
- It participates in environment resolution like any other environment (if its DB exists)

## Environment Entry

An `EnvironmentEntry` describes how to reach an environment's database. It carries the environment ID, the kind,
and an optional location:

```python
entry = EnvironmentEntry(id="dev", kind=EnvironmentKind.LOCAL, location="~/data/dev.db")
```

Entries come from three sources:

- **Built-in local** — `lookup("local")` returns the built-in entry with a deterministic path
- **Registry** — `lookup("dev")` reads the entry from `environments.toml`
- **Programmatic** — construct `EnvironmentEntry(...)` directly

## Registry

A TOML file that maps user-created environment IDs to their databases. Only for environments beyond the built-in local.

### Location

`$XDG_CONFIG_HOME/runtools/environments.toml` (defaults to `~/.config/runtools/environments.toml`).

Single file — no directory scanning, no layered merging.

### Format

```toml
[environments.dev]
kind = "local"

[environments.staging]
kind = "local"
location = "~/data/staging.db"

[environments.prod]
kind = "postgres"
location = "postgresql://runtools@db.internal/runtools"
```

| Field      | Type | Default   | Description                                                                                                             |
|------------|------|-----------|--------------------------------------------------------------------------------------------------------------------------|
| `kind`     | str  | `"local"` | Environment kind: `"local"` or `"postgres"`.                                                                             |
| `location` | str? | none      | Kind-specific location: SQLite DB path (`~` expansion, defaults to `~/.local/share/runtools/<id>.db`) or postgres DSN. |

The name `local` is reserved and cannot appear in the registry. If it does, registry loading fails with an error.

## Database

The environment database holds everything:

- **Config table** — authoritative runtime configuration (retention, output, kind-specific settings)
- **Runs table** — job run history
- Future: permissions, audit log

### Configuration

Runtime configuration is stored in a `config` table. Each setting is a separate key with a JSON-encoded value.

The config model is per-kind (`LocalEnvironmentConfig` / `PostgresEnvironmentConfig`), chosen by the entry's `kind`
at load — the config itself stores neither `id` nor `kind`. All config models are frozen (immutable after loading).

| Setting                           | Kind  | Description                                                                        | Default              |
|-----------------------------------|-------|-------------------------------------------------------------------------------------|----------------------|
| `root_dir`                        | local | Transport root: holds component dirs, socket files, liveness locks                | system default       |
| `output.default_tail_buffer_size` | all   | Default in-memory tail buffer size in bytes                                        | 2 MiB                |
| `output.storages`                 | all   | Output storage backends (file, etc.)                                               | file storage enabled |
| `retention.keep_days`             | all   | Default retention for `env prune`: keep finished runs newer than N days            | none                 |
| `plugins`                         | all   | Plugin name → config mapping                                                       | empty                |

### Save semantics

`save_env_config()` performs a **full replace** (DELETE all + INSERT non-defaults). Reverting a setting to its default
removes the key from the database. No orphaned values.

## Environment Resolution

### Available environments

**Available environments** = built-in local (if its DB exists) + registered environments.

This set is used consistently for:

- `taro ps --all` — queries all available environments
- Implicit resolution (when no `-e` flag is given)

### runjob / runcli — "bootstrap local"

No env_id always means `local`. The built-in local DB is auto-created on first use:

- No `-e` → use `local`
- `-e local` → use `local`
- `-e <id>` → use that environment (must be registered)

Adding or removing other environments never affects default execution.

### taro — interactive resolution

Resolves across the full set of available environments:

- `-e <id>` → use that environment
- No `-e`, one available → auto-select it
- No `-e`, multiple available → interactive prompt (TTY) or error with "use -e/--env" (non-TTY)
- No `-e`, none available → error: "create one with `taro env create`"

### No configurable default

There is no "default environment" setting. The rule is deterministic: runjob/runcli default to `local`, taro resolves
from available environments.

## Kinds

The entry's `kind` selects the backend bundle — storage, live transport, and coordination together. Supported
combinations only; there are no independent storage/transport knobs.

### `local`

SQLite storage + process boundary over Unix domain sockets + file locks. The standard kind for durable environments
on a single machine: socket files, component dirs, and liveness `flock`s live under a per-environment directory
(overridable via the `root_dir` config setting). This is what the built-in `local` environment uses and what
`taro env create` writes.

### `postgres`

PostgreSQL storage + DB-polling instance directory. Connectors observe active runs by polling the environment
database rather than contacting producing nodes. **Observe-only for now** — `node.connect` raises
`NotImplementedError` until the producer slice (node access point + advisory locks) lands.

### In-process (not a kind)

No transport boundary — node and clients live in the same Python process, no connector boundary, in-memory locks.
Used for tests and embedded scenarios.

In-process environments are not registered, not loaded from a DB, and not selectable by kind. They are constructed
via the dedicated factory with an in-memory SQLite database; nothing is persisted.

```python
from runtools.runjob.node import in_process

with in_process() as env:
    inst = env.create_instance(...)
```

## Lifecycle

### Creating

```bash
taro env create dev                                # default DB path
taro env create staging --path ~/data/staging.db   # custom DB path
```

Creates the SQLite database (initializes schema) and adds an entry to the registry.

The built-in local environment is auto-created by runjob/runcli on first use. It cannot be created via
`taro env create`.

### Listing

```bash
taro env list
```

Shows all available environments: built-in local (if its DB exists, marked as `(built-in)`) and registered environments.

### Configuring

```bash
taro env show                           # show config (auto-selects if one env)
taro env show -e dev                    # specific env

taro env edit                           # edit in $EDITOR (TOML presentation)
taro env edit -e dev
```

The edit command dumps current config as TOML, opens `$EDITOR`, validates the result, and saves. Invalid TOML or schema
violations are reported without saving.

### Deleting

```bash
taro env delete staging                 # prompts for confirmation
taro env delete staging -f              # skip confirmation
```

Deletes the DB file and removes the registry entry. The built-in local cannot be deleted.

### Cleaning stale transport state

```bash
taro env clean                          # remove dead process socket dirs
taro env clean -e dev
```

### Pruning run history

```bash
taro env prune "*" --keep-days 0              # wipe all history
taro env prune "*" --keep-days 7              # keep last week
taro env prune "test_*" --keep-days 0         # wipe all test runs
taro env prune "backup" --keep-days 30 -e dev # prune old backup runs
```

Removes matching run records from the database and their output files. Both PATTERN and `--keep-days` are
required to prevent accidental data loss. Use `-f` to skip confirmation.

## Usage in Code

### Connecting (monitoring/control)

```python
from runtools.runcore import connector

# Built-in local (default)
with connector.connect() as conn:
    runs = conn.get_active_runs()

# By name
with connector.connect("dev") as conn:
    runs = conn.get_active_runs()

# By entry
from runtools.runcore.env import lookup

entry = lookup("dev")
with connector.connect(entry) as conn:
    runs = conn.get_active_runs()
```

### Running jobs

```python
from runtools.runjob import node

# Zero-config: built-in local, auto-creates DB if needed
with node.connect() as env:
    inst = env.create_instance("my_job", root_phase=my_phase)
    inst.run()

# Explicit environment
with node.connect("dev") as env:
    inst = env.create_instance("my_job", root_phase=my_phase)
    inst.run()

# With runtime overrides (not stored in config)
with node.connect(disable_output=("all",), tail_buffer_size=4096) as env:
    inst = env.create_instance("my_job", root_phase=my_phase)
    inst.run()
```

### Managing environments programmatically

```python
from runtools.runcore.env import (
    EnvironmentKind, LocalEnvironmentConfig, RetentionConfig,
    EnvironmentEntry, lookup, load_env_config, save_env_config,
    create_environment, delete_environment, available_environments,
)

# Create — supply the kind's config (defaults are the standard preset)
entry = EnvironmentEntry(id="dev", kind=EnvironmentKind.LOCAL)
create_environment(entry, LocalEnvironmentConfig())

# With a custom database location and socket root
entry = EnvironmentEntry(id="staging", kind=EnvironmentKind.LOCAL, location="/data/staging.db")
create_environment(entry, LocalEnvironmentConfig(root_dir="/tmp/staging-sockets"))

# Read/modify config — config is frozen, use model_copy(update={...})
entry = lookup("dev")
cfg = load_env_config(entry)
cfg_modified = cfg.model_copy(update={"retention": RetentionConfig(keep_days=30)})
save_env_config(entry, cfg_modified)

# List available
for entry in available_environments():
    print(f"{entry.id}: {entry.kind} @ {entry.location or '(default)'}")

# Programmatic entry (no registry needed)
custom = EnvironmentEntry(id="custom", kind=EnvironmentKind.LOCAL, location="/tmp/custom.db")

# Delete
delete_environment("dev")
```

## Implementation Notes

### Code structure

```python
class EnvironmentDatabase(ConfigStorage, RunStorage, ABC):
    """The environment's database — a single store implementing both interfaces."""

    @abstractmethod
    def open(self): ...

    @abstractmethod
    def close(self): ...
```

`ConfigStorage` and `RunStorage` are protocol boundaries over the same database, not separate stores. The
implementation (`SQLite`) is a single class with a flat method surface. Consumers that only need config (e.g.
`load_env_config`) or only runs (e.g. `PersistingObserver`) type-narrow via the protocol they depend on.

## Default Paths

| What                        | Path                                          |
|-----------------------------|-----------------------------------------------|
| Registry                    | `$XDG_CONFIG_HOME/runtools/environments.toml` |
| Database (no explicit path) | `$XDG_DATA_HOME/runtools/<id>.db`             |
| Transport sockets           | `/tmp/runtools_<user>/env/<id>/`              |
| Output files                | `$XDG_STATE_HOME/runtools/<id>/output/`       |
