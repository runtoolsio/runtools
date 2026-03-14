# Environment Design — Next Generation

Replaces the current config-file-based model with database-as-single-source-of-truth.

## Core Principle

The database is the single source of truth for an environment.
No separate config files for environment configuration — all config, permissions,
and run data live in the database itself.

## Local Environments

Each environment is a SQLite file in a known directory:

```
~/.runtools/
├── dev.db
├── staging.db
└── prod.db
```

Discovery: list `.db` files in the standard directory.

- `taro ps` — if one DB exists, use it; if multiple, prompt user to select
- `taro ps -e dev` — use `dev.db` directly
- No "default environment" concept — prompt when ambiguous

Environment configuration stored in a `config` table within each database.
Editable via:

```
taro env config --edit
```

Opens config in editor (dump to temp file, edit, validate, write back).

## Distributed Environments (PostgreSQL)

### Connection Config

A minimal config file provides connection details only (not environment config):

```ini
[db]
host = db.prod.internal
port = 5432
auth = /path/to/private_key
environments_hint = {"env1": "env_prod", "env2": "env_staging"}
```

- `auth` uses private key (asymmetric crypto)
- `environments_hint` is a non-authoritative performance shortcut used only
  when the user specifies an explicit environment (for example `-e env1`)
- the database remains the source of truth for discovery and validation
- the hint only narrows where to look first; it does not define the environment

### Environments as PostgreSQL Schemas

Each environment = one schema within a single database:

```
taro_db (database)
├── env_prod (schema)
│   ├── history
│   ├── config
│   ├── users
│   └── duplicates
├── env_staging (schema)
│   ├── history
│   ├── config
│   ├── users
│   └── duplicates
└── env_dev (schema)
    └── ...
```

Discovery via PostgreSQL's information schema:

```sql
SELECT schema_name FROM information_schema.schemata
WHERE schema_name LIKE 'env_%';
```

Only schemas the connected user has `USAGE` on are visible — PostgreSQL RBAC
handles environment-level access control natively.

## Security Model

### Local Environments

File permissions on `.db` files — same as current unix domain socket approach.
Sufficient for single-machine use.

### Distributed Environments — Signed Requests

Control actions (stop, approve, resume) use signed requests:

```
taro stop instance1
  -> create request {action: stop, target: instance1, user: u1, timestamp: T}
  -> sign with u1's private key
  -> send to environment
  -> env: SELECT public_key, permissions FROM users WHERE user_id = 'u1'
  -> verify signature
  -> check permissions
  -> execute or reject
```

Provides:

- **Authentication** — asymmetric signature proves identity
- **Authorization** — permissions stored in DB `users` table
- **Audit** — who did what, when (logged in DB)
- **Replay protection** — timestamp in signed payload
- **No shared secrets** — private keys never leave the user's machine

### User Management

Users and their public keys stored in the `users` table per environment:

```
taro env user add u1 --key ~/.ssh/id_ed25519.pub
taro env user grant u1 --action stop,approve
```

PostgreSQL schema-level RBAC for environment access, application-level
permissions for fine-grained action control.

## Improvement Suggestion

For the first PostgreSQL version, schema-based discovery is acceptable.

A likely future improvement is to introduce an explicit environment registry table in the database:

- bootstrap config still provides database connection details
- the registry becomes the authoritative discovery mechanism
- each registry row maps environment id to schema name and metadata
- `environments_hint` remains only a shortcut for explicit lookup

This would be cleaner than treating every visible schema as automatically an environment, especially once
environment metadata grows.

## Environment Lifecycle

```
taro env create prod                     # local: create prod.db
taro env create prod --distributed       # create schema env_prod in configured PG
taro env config --edit -e prod           # edit config
taro env list                            # list all discoverable environments
taro env user add u1 --key ...           # add user (distributed)
taro env delete staging                  # remove environment
```
