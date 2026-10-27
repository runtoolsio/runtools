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
- **`postgres`** — PostgreSQL storage + DB-polling instance directory + advisory locks. Runs and observes
  jobs across machines; remote control (stop/phase ops) works via the signals mailbox.

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

### Output storage across machines

The default output storage (`file`) writes a run's complete output to the disk of the machine that ran it. On a
`postgres` environment, other machines then see only the live tail — while the run is going and briefly after.
To read finished runs' output from any machine, store it in S3 (or an S3-compatible service) instead — install
`runtoolsio[s3]` on every machine and set it with `taro env edit`:

```toml
[[output.storages]]
type = "s3"
bucket = "my-bucket"
prefix = "runtools/work"     # optional key prefix
region = "eu-west-1"         # optional; default AWS chain otherwise
# endpoint_url = "..."       # S3-compatible services (R2, MinIO)
# profile = "..."            # named AWS profile
```

Credentials come from the standard AWS chain (environment, profile, instance role) on each machine.

### Schema compatibility (beta)

Each database records its schema version. During the beta, a release may change the schema without a migration:
opening an older database then fails with `IncompatibleSchemaError`, and the environment must be recreated
(`taro env delete`, then create it again) — its run history is lost. Such releases say so
in their notes. Migrations arrive with 1.0.

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

PostgreSQL storage + DB-polling instance directory + advisory locks. Nodes run jobs whose state reaches
remote connectors through the run-state persister (coalesced snapshot writes, observed by polling — no
direct node contact); job coordination (mutex, queue) works across machines via session advisory locks.
Remote control (`stop`/phase operations such as `approve`) works via the signals mailbox: the command is
written to the database and applied by the owning node within its poll interval (~1s); the applied
operation is recorded in the run's history (`control_requests`). A pending command does not outlive its
target — runs die with their node, so a command posted around an owning-node crash is never applied (the
run is reported lost, see below, and the pending command is eventually swept from the mailbox). Crashed nodes are
detected via heartbeats: a run whose node stops attesting liveness for 45 s is reported as lost by consumers (never
auto-terminated) — see [Resolving lost runs](#resolving-lost-runs). Access is controlled by PostgreSQL roles, not
file permissions — see [Security (postgres)](#security-postgres).
The environment `location` must be a direct DSN (session advisory locks are incompatible
with transaction-mode poolers). Live output of another node's instances is readable as a bounded
tail (roughly the newest `tail_cap` lines — the cap is amortized, so briefly up to about twice
that may be retained between prune cycles; roughly 1–2 s behind live; `tail_cap: 0` disables
tail publishing); complete output is available from the configured output storages once the run ends.
Follow-style tailing (`taro tail -f`) works: output is polled from the tail only while someone follows it.

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
taro env create dev                                # local: default DB path
taro env create staging --path ~/data/staging.db   # local: custom DB path
taro env create work --dsn postgresql://runtools@db.internal/runtools   # postgres
```

Provisions the store — a SQLite database, or the environment's schema in PostgreSQL — seeds the default config, and
adds an entry to the registry. A store that already exists is refused rather than re-seeded, since other machines may
share its config.

For postgres, keep the password out of the DSN (the registry stores it as plain text) and put it in `~/.pgpass`,
which libpq reads by itself. Point the DSN at PostgreSQL or a session-mode pooler, not a transaction-mode one
(coordination uses session advisory locks), and use a database you can reset — see
[Schema compatibility](#schema-compatibility-beta).

### Adding an existing environment

```bash
taro env add work --dsn postgresql://runtools@db.internal/runtools
```

Registers an environment whose store already exists — the way every other machine joins a postgres environment.
Use the same name it was created with; its stored config is left untouched.

### Security (postgres)

A `local` environment is protected by file permissions on its database and sockets. A `postgres` environment is
protected by PostgreSQL alone — runtools adds no permissions of its own: any role with full operator access to the
environment's schema (`USAGE` on it plus privileges on its tables and sequences) can watch, stop and approve jobs,
mark runs lost, delete history and edit the config.

- **Who can connect:** login roles and `pg_hba.conf`. Keep the password in `~/.pgpass` (mode 600 — libpq ignores
  it otherwise), not in the DSN: the registry stores the DSN as plain text, so `taro env create`/`add` warn about a
  password there, and `taro env list` masks it.
- **Who can operate:** `USAGE` on the environment's schema (`runtools_<name>_<hash>`) plus privileges on its tables
  and sequences. Deleting the environment drops the schema, which needs ownership. A new schema is private to the
  role that created it — unless role memberships or the database's default privileges grant others access.
- **In transit:** use `sslmode=verify-full` in the DSN. It needs a CA the client trusts (`sslrootcert`) and a
  server certificate matching the DSN's host — otherwise connecting fails. `sslmode=require` encrypts but does not
  guarantee server identity verification (it skips the hostname check).
- **Coordination:** advisory locks are shared by the whole database, not per schema — any role that can connect can
  hold a key and block a mutex or queue. Use a dedicated database.

#### One login, or one per person

- **Shared login** — simplest: everyone who has it is an operator; revoking one person means changing it for all.
- **Separate logins sharing a group role** — each person or machine logs in as itself but acts as the group, so the
  group owns the environment and every member can operate it; one login is revoked alone. They also make
  attribution possible: with server logging configured for it (e.g. `log_line_prefix` with `%u`, and a
  `log_statement` level covering the operations), PostgreSQL logs show which login did what — runtools itself
  records no identities:

  ```sql
  CREATE ROLE runtools_ops NOLOGIN;
  GRANT CREATE ON DATABASE runtools TO runtools_ops;   -- to create environment schemas
  CREATE ROLE alice LOGIN;
  GRANT runtools_ops TO alice;                          -- likewise for each person or machine
  ```

  ```bash
  # options=-c role=... switches each session to the group role (URL-encoded in the DSN)
  taro env create work --dsn "postgresql://alice@db.internal/runtools?sslmode=verify-full&options=-c%20role%3Druntools_ops"
  taro env add work --dsn "postgresql://bob@db.internal/runtools?sslmode=verify-full&options=-c%20role%3Druntools_ops"
  ```

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

### Resolving lost runs

A run whose node stopped heartbeating (crash, `kill -9`, lost network) is **lost**: still unfinished in the
database, but nobody is attesting it. Lost runs are never resolved automatically — a partitioned node may recover and
finish the run truthfully. They stay listed everywhere (`taro ps`, dashboards) with a `LOST <age>` badge and a dimmed
row, and `taro stop` on a lost run points here instead of sending a command nobody would apply.

```bash
taro env mark-lost                      # preview all lost runs, confirm, mark them LOST
taro env mark-lost "backup@*" -e prod   # only matching runs
taro env mark-lost -f                   # skip confirmation
```

Marking writes the terminal status `LOST` (outcome: fault) with the last heartbeat as an estimated end time. It is
race-safe: a run whose node attests again before the mark is skipped, and a node that recovers after the mark cannot
overwrite it. Lost runs are found from the database, so this works on every kind — on `local`, a dead node's runs
are not visible in the live view at all.

### Cleaning stale transport state

```bash
taro env clean                          # stale socket dirs + orphan signal/tail rows
taro env clean -e dev -f                # skip confirmation
```

Removes state left behind by dead processes: stale component directories (`local` kind) and the pending-command and
output-tail rows of runs that have ended or no longer exist. Rows of lost runs are kept until `mark-lost` resolves
them; run history is never touched.

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
