# Environment

An environment isolates job instances from each other. Each environment has its own database, transport, and output
backends.

In practical terms, an environment is a named place where jobs coordinate, connectors observe/control them, and run
history is stored. Most users only need two ideas:

- `local` is the built-in environment for zero-config local use
- additional environments can be added in `environments.toml`

## Structure

An environment consists of:

- **Database** — persistent state: configuration, run history, future permissions. The environment is centered on its
  database, which is the authoritative source of truth.
- **Transport** — inter-component communication: RPC between nodes and connectors, event dispatching. Currently Unix
  domain sockets; future options include Redis, gRPC, etc.
- **Output backends** — output routing/storage for instance stdout/stderr and structured output. Configured from the
  database via `output.storages`.

The database is the source of truth. Transport and output backend configuration are stored in the database alongside
everything else.

The database is not optional. Every environment has a database, though the concrete implementation may be persistent or
transient/in-memory depending on the environment type.

## Built-in Local

The `local` environment is always available. It does not require a registry entry and cannot be created, deleted, or
redefined.

- Its DB path is deterministic: `~/.local/share/runtools/local.db`
- The DB is auto-created on first use by runjob/runcli
- The name `local` is reserved — it cannot appear in the registry
- It participates in environment resolution like any other environment (if its DB exists)

## Environment Entry

An `EnvironmentEntry` describes how to reach an environment's database. It carries the environment ID, the database
driver, and an optional path:

```python
entry = EnvironmentEntry(id="dev", driver="sqlite", path="~/data/dev.db")
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
driver = "sqlite"

[environments.staging]
driver = "sqlite"
path = "~/data/staging.db"
```

| Field    | Type | Default    | Description                                                                                                      |
|----------|------|------------|------------------------------------------------------------------------------------------------------------------|
| `driver` | str  | `"sqlite"` | Database driver. Only `"sqlite"` is currently supported. Future: `"postgres"`.                                   |
| `path`   | str? | none       | Path to the SQLite DB file. Supports `~` expansion. When omitted, defaults to `~/.local/share/runtools/<id>.db`. |

The `driver` field describes the storage backend mechanism (how to connect to the database), not the environment's
conceptual type.

The name `local` is reserved and cannot appear in the registry. If it does, registry loading fails with an error.

## Database

The environment database holds everything:

- **Config table** — authoritative runtime configuration (retention, output, transport settings)
- **Runs table** — job run history
- Future: permissions, audit log

### Configuration

Runtime configuration is stored in a `config` table (schema version 5). Each setting is a separate key with a
JSON-encoded value.

A fresh environment has an **empty config table** — all settings use defaults. Only non-default values are stored. All
config models are frozen (immutable after loading).

| Setting                           | Description                                                                    | Default              |
|-----------------------------------|--------------------------------------------------------------------------------|----------------------|
| `layout.root_dir`                 | Root directory for local transport socket directories; local environments only | system default       |
| `output.default_tail_buffer_size` | Default in-memory tail buffer size in bytes                                    | 2 MiB                |
| `output.storages`                 | Output storage backends (file, etc.)                                           | file storage enabled |
| `retention.max_runs_per_job`      | Max finished runs to keep per job                                              | 500                  |
| `retention.max_runs_per_env`      | Max finished runs to keep per environment                                      | 10000                |

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

## Environment Types

### Local

A local environment runs on a single machine. Transport uses Unix domain sockets. Database is a SQLite file. This is the
standard type.

### In-Process (testing)

An in-process environment exists only within a single process. It uses in-memory SQLite and memory-based locks with no
transport layer. Used for testing.

It still has a database, but that database is transient/in-memory rather than file-backed.

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
with node.connect(no_output_storage=True, tail_buffer_size=4096) as env:
    inst = env.create_instance("my_job", root_phase=my_phase)
    inst.run()
```

### Managing environments programmatically

```python
from runtools.runcore.env import (
    lookup, load_env_config, save_env_config,
    create_local_environment, delete_local_environment,
    available_environments, EnvironmentEntry,
)

# Create
create_local_environment("dev")
create_local_environment("staging", db_path="/data/staging.db")

# Read/modify config (use model_copy — config is frozen)
entry = lookup("dev")
cfg = load_env_config(entry)
cfg_modified = cfg.model_copy(update={"retention": RetentionConfig(max_runs_per_job=100)})
save_env_config(entry, cfg_modified)

# List available
for entry in available_environments():
    print(f"{entry.id}: {entry.driver} @ {entry.db_path()}")

# Programmatic entry (no registry needed)
custom = EnvironmentEntry(id="custom", driver="sqlite", path="/tmp/custom.db")

# Delete
delete_local_environment("dev")
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
