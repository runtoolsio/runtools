# PostgreSQL environments

A `postgres` environment keeps all of runtools' state in a PostgreSQL database, so jobs on many machines share one
history, coordinate with each other, and can be watched and controlled from anywhere. This page covers what runtools
puts in the database, how to set up access, and what each kind of user can do.

## What runtools creates

You provide a database (a dedicated one is recommended, see [Connection security](#connection-security)); each
environment you create becomes one schema in it:

```text
database "runtools_db"                   ← you create it
├── schema runtools_billing_cacfdbb4fbf4 ← environment "billing", created by `taro env create`
│   ├── runs          one row per job run: its state, timestamps and the node's heartbeat
│   ├── run_tags      tags of runs
│   ├── signals       commands waiting for a node: stop, approve, resume, …
│   ├── output_tail   the newest ~1000 output lines of running jobs (a cache, dropped after the run ends)
│   ├── config        environment settings: output storage, retention, tail size
│   └── schema_info   schema version
└── schema runtools_reporting_…          ← another environment
```

The environment name (`billing` in this guide) is yours to choose — `taro env create billing` picks it, and jobs and
`taro` select the environment by it (`run job -e billing …`, `taro ps -e billing`). Every machine must register the
environment under the same name: the schema is found by it.

The schema name is `runtools_` + the environment name (`-` becomes `_`) + a hash. List them with:

```sql
SELECT nspname FROM pg_namespace WHERE nspname LIKE 'runtools\_%' ORDER BY nspname;
```

Who touches what:

| Table | Written by | Read by |
|---|---|---|
| `runs`, `run_tags` | nodes (machines running jobs); operators via `mark-lost` and `prune` | everyone |
| `signals` | operators (`stop`, `approve`, …); nodes delete them once applied | nodes |
| `output_tail` | nodes; removed by `taro env clean` | everyone (`taro tail`) |
| `config` | admins (`taro env edit`) | everyone |

## Before you start: TLS

Every example uses `sslmode=verify-full`: the connection is encrypted and the client checks that it talks to the
right server. That needs the CA that signed the server's certificate on every machine, and the DSN's host must match
a name in that certificate:

```bash
# a company or self-signed CA — either name it in the DSN …
taro env add billing --dsn "postgresql://bob@db.internal/runtools_db?sslmode=verify-full&sslrootcert=/etc/ssl/company-ca.pem"
# … or install it once where libpq looks by default, and leave sslrootcert out
mkdir -p ~/.postgresql && cp company-ca.pem ~/.postgresql/root.crt

# a CA your OS already trusts (e.g. a managed cloud database)
taro env add billing --dsn "postgresql://bob@db.example.com/runtools_db?sslmode=verify-full&sslrootcert=system"
```

If it's missing, connecting fails with `root certificate file "…/.postgresql/root.crt" does not exist`; a host that
isn't in the certificate fails with `server certificate for "…" does not match host name`.

## Quick start: one login

Simplest setup — one login shared by every machine and person; everyone who has it can do everything.

```sql
-- as a PostgreSQL superuser
CREATE DATABASE runtools_db;
CREATE ROLE runtools_user LOGIN PASSWORD '…';
GRANT CREATE ON DATABASE runtools_db TO runtools_user;
```

On each machine, put the password in `~/.pgpass` (mode `600`) instead of the DSN:

```text
db.internal:5432:runtools_db:runtools_user:the-password
```

```bash
taro env create billing --dsn "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"   # once
taro env add billing --dsn "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"      # every other machine
```

- `create` provisions the environment in the database — its schema and default config — and registers it on this
  machine. It needs `CREATE` on the database and refuses a schema that already exists: the config is shared, so it is
  never re-seeded.
- `add` only registers an existing environment on this machine, leaving the database untouched; it fails if the
  schema doesn't exist, so a wrong name or DSN shows up at once.
- The registry is per user (`~/.config/runtools/environments.toml`): each account that runs `run job` or `taro`
  needs its own `add`, even where `create` ran under another account.

## Access profiles

To separate who can do what, give each person and machine their own login and put it in one of four profiles.
The profiles are ordinary PostgreSQL group roles with grants on the environment's tables — runtools itself has no
built-in roles or permission checks.

| Profile | For | Can | Cannot |
|---|---|---|---|
| **admin** | whoever owns the environment | everything below, plus create and delete environments and change their config (`taro env edit`) | — |
| **node** | machines that run jobs (`run job`) | record runs, publish output, apply commands | change config, delete the environment |
| **operator** | people managing jobs | view, plus `stop`, `approve`/`resume`, `mark-lost`, `clean`, `prune` | change config, delete the environment |
| **viewer** | people watching | `taro ps`, `dash`, `history`, `tail`, `env show` | anything that writes — all refused |

PostgreSQL grants are per table, not per runtools command: node and operator both write the `runs` and `signals`
tables, so an operator login could also write those tables with plain SQL. The firm boundaries are **viewer**
(read-only) and **admin** (config and deletion).

### 1. Create the profiles and logins

This recipe stands on its own. It needs a database for runtools — create one first if you skipped the quick start:

```sql
CREATE DATABASE runtools_db;
```

Then, as a PostgreSQL superuser connected to it:

```sql
-- Permission profiles (group roles nobody logs in as)
CREATE ROLE runtools_admin NOLOGIN;
CREATE ROLE runtools_node NOLOGIN;
CREATE ROLE runtools_operator NOLOGIN;
CREATE ROLE runtools_viewer NOLOGIN;
GRANT CREATE ON DATABASE runtools_db TO runtools_admin;   -- lets admins create environment schemas

-- One login per person or machine, each in one profile
CREATE ROLE stan LOGIN PASSWORD '…';      GRANT runtools_admin TO stan;
CREATE ROLE build01 LOGIN PASSWORD '…';   GRANT runtools_node TO build01;
CREATE ROLE alice LOGIN PASSWORD '…';     GRANT runtools_operator TO alice;
CREATE ROLE bob LOGIN PASSWORD '…';       GRANT runtools_viewer TO bob;
```

### 2. Create the environment as an admin

The admin's DSN switches the session to the `runtools_admin` role (`options=-c role=…`, URL-encoded), so the
environment is owned by the profile rather than one person — any admin can then manage it. `--grant-profiles runtools`
grants `runtools_node`, `runtools_operator` and `runtools_viewer` their access:

```bash
taro env create billing --grant-profiles runtools \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Druntools_admin"
```

- The grants are made in the transaction that creates the environment: a missing role fails the command, and nothing
  is created (`Roles not found: runtools_operator`).
- They come with the tables, so they always match the schema. When the environment is recreated — for example after
  a beta release that changes the schema — the same command restores them.
- runtools only grants; creating the roles and adding logins to them stays with you (step 1).

### 3. Join from every other machine

Each login joins with its own DSN; no role switch is needed, the profile's grants apply automatically:

```bash
taro env add billing --dsn "postgresql://build01@db.internal/runtools_db?sslmode=verify-full"   # on a node
taro env add billing --dsn "postgresql://bob@db.internal/runtools_db?sslmode=verify-full"       # on a viewer's laptop
```

A refused action reports PostgreSQL's error — `permission denied for table signals` (not an operator) or
`must be owner of schema …` (admins only).

### 4. Check it works

Run a small job on a node, then watch and stop it from the viewer's and the operator's machines:

```bash
# node (build01): prints the time every second until stopped
run job -e billing -j demo sh -c 'while true; do date; sleep 1; done'
```

```bash
# viewer (bob)
taro ps -e billing                 # demo   …   EXEC
taro tail -e billing demo          # the latest timestamps
taro stop -e billing -f demo       # refused: … permission denied for table signals
```

```bash
# operator (alice)
taro stop -e billing -f demo       # ✓ Stopped demo@…
taro history -e billing            # demo   …   STOPPED
```

### Per-environment permissions

The profiles above apply to every environment created with `--grant-profiles runtools`. To give one login different
rights in different environments — alice operates `billing` but only watches `reporting` — create a set of profiles
per environment:

```sql
-- as a PostgreSQL superuser; the same four roles for reporting_…
CREATE ROLE billing_admin NOLOGIN;
CREATE ROLE billing_node NOLOGIN;
CREATE ROLE billing_operator NOLOGIN;
CREATE ROLE billing_viewer NOLOGIN;
GRANT CREATE ON DATABASE runtools_db TO billing_admin, reporting_admin;

GRANT billing_admin, reporting_admin TO stan;
GRANT billing_node TO build01;
GRANT reporting_node TO build02;
GRANT billing_operator, reporting_viewer TO alice;
```

Create each environment as its own admin role, granting its own profiles:

```bash
taro env create billing --grant-profiles billing \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Dbilling_admin"
taro env create reporting --grant-profiles reporting \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Dreporting_admin"
```

Every login then joins with `taro env add` as in step 3, and gets in each environment only what its profiles there
allow: alice's `taro stop -e billing` works, `taro stop -e reporting` is refused, and `build01` can't run jobs in
`reporting` at all — it reports `relation "schema_info" does not exist`, as a schema without `USAGE` is invisible.

Each environment's schema is owned by its own admin role, so `billing_admin` can't change or delete `reporting`.
Still shared by the whole database: who can connect, `CREATE` on it (any environment's admin can create new
environments) and advisory locks (see [Connection security](#connection-security)). For full separation — different
teams or trust levels — give each environment its own database.

### Granting an existing environment

To grant profiles to an environment created without `--grant-profiles`, save this as `grants.sql` — the same grants,
for the current schema — and run it as a member of the environment's owner role (e.g. an admin):

```sql
-- Grants an environment's profile roles <prefix>_node, _operator and _viewer — what
-- `taro env create --grant-profiles` does at creation. Run as a member of the schema's owner role:
--   psql ... -v schema=runtools_billing_cacfdbb4fbf4 -v prefix=billing -f grants.sql
\set node :prefix '_node'
\set operator :prefix '_operator'
\set viewer :prefix '_viewer'
GRANT USAGE ON SCHEMA :"schema" TO :"node", :"operator", :"viewer";
GRANT SELECT ON ALL TABLES IN SCHEMA :"schema" TO :"node", :"operator", :"viewer";
-- Nodes: record runs, publish output, consume commands
GRANT INSERT, UPDATE ON :"schema".runs TO :"node";
GRANT INSERT ON :"schema".run_tags TO :"node";
GRANT INSERT, DELETE ON :"schema".output_tail TO :"node";
GRANT DELETE ON :"schema".signals TO :"node";
-- Operators: send commands (stop, approve), mark-lost, clean, prune
GRANT INSERT, DELETE ON :"schema".signals TO :"operator";
GRANT UPDATE, DELETE ON :"schema".runs TO :"operator";
GRANT DELETE ON :"schema".run_tags, :"schema".output_tail TO :"operator";
```

```bash
psql "postgresql://stan@db.internal/runtools_db?sslmode=verify-full" \
  -v schema=runtools_billing_cacfdbb4fbf4 -v prefix=billing -f grants.sql
```

## Job output

PostgreSQL holds only the live tail (`taro tail` while a job runs, and briefly after). Complete output lives in the
environment's output storage, which has its own access control: with S3 storage, nodes need write access to the
bucket and anyone running `taro output` on finished runs needs read access — see
[Output storage across machines](environment.md#output-storage-across-machines). A viewer without S3 credentials can
watch running jobs but not read finished runs' full output.

## Connection security

- **Passwords:** keep them in `~/.pgpass` (mode `600` — libpq ignores the file otherwise), not in the DSN. The DSN is
  stored as plain text in the environment registry; `taro env create`/`add` warn about a password in it, and
  `taro env list` masks it.
- **Encryption:** use `sslmode=verify-full`. It needs a CA the client trusts (`sslrootcert`) and a server
  certificate matching the DSN's host — otherwise connecting fails. `sslmode=require` encrypts but does not
  guarantee server identity verification (it skips the hostname check).
- **Who can connect at all:** login roles and `pg_hba.conf` — limit it to the machines that run or watch jobs.
- **A dedicated database (recommended, not required):** runtools works in a shared database — it creates and drops
  only its own `runtools_…` schemas. But job coordination uses PostgreSQL advisory locks, which are shared by the
  whole database, not per schema: any login that can connect could hold a lock key and block a mutex or queue. A
  dedicated database limits that to runtools logins, keeps `GRANT CREATE ON DATABASE` and beta schema resets away
  from other data, and costs nothing extra on an existing server (`CREATE DATABASE runtools_db`).
- **Privileges can come from elsewhere:** role memberships and the database's default privileges (`ALTER DEFAULT
  PRIVILEGES`) may give other roles access beyond what this page grants — check them on shared servers.
- **Pooling:** connect directly or through a session-mode pooler, not a transaction-mode one — coordination relies on
  session-level advisory locks.

## Who did what

runtools records no identities. Separate logins make attribution possible through PostgreSQL's own logging — for
example `log_line_prefix` with `%u` (the login), and a `log_statement` level that covers the operations you care
about. Logs show the original login even when a session switches roles (`session_user`).
