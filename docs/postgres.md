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

You name the environment when you create it — `billing` in this guide:

```bash
taro env create billing …            # creates the environment named "billing"
run job -e billing -j backup …       # runs a job in it
taro ps -e billing                   # shows its running jobs
```

Every machine must use the same name: runtools finds the environment's schema in the database by it.

Who touches what:

| Table | Written by | Read by |
|---|---|---|
| `runs`, `run_tags` | nodes (machines running jobs); operators via `mark-lost` and `prune` | everyone |
| `signals` | operators (`stop`, `approve`, …); nodes delete them once applied | nodes |
| `output_tail` | nodes; removed by `taro env clean` | everyone (`taro tail`) |
| `config` | admins (`taro env edit`) | everyone |

### Creating and joining an environment

An environment is created once and then joined from every other machine:

- `taro env create` provisions the environment in the database — its schema and default config — and registers it on
  this machine. It needs `CREATE` on the database and refuses a schema that already exists: the config is shared, so
  it is never re-seeded.
- `taro env add` only registers an existing environment on this machine, leaving the database untouched; it fails if
  the schema doesn't exist, so a wrong name or DSN shows up at once.
- The registry is per user (`~/.config/runtools/environments.toml`): each account that runs `run job` or `taro`
  needs its own `add`, even where `create` ran under another account.

## Before you start

### Choose a setup

Pick one of the two setups below; both create the same `billing` environment:

| Setup | Logins | Who can do what |
|---|---|---|
| [A. One shared login](#a-one-shared-login) | one, used by every machine and person | everyone can do everything |
| [B. Access profiles](#b-access-profiles) | one per person and machine | each login only what its profile allows |

### TLS

Every example uses `sslmode=verify-full`: the connection is encrypted and the client checks that it talks to the
right server. Each machine needs the CA that signed the server's certificate, and the DSN's host must be a name in
that certificate. Make the CA available once per machine:

```bash
# a company or self-signed CA: install it where libpq looks by default
mkdir -p ~/.postgresql && cp company-ca.pem ~/.postgresql/root.crt

# Amazon RDS: its CA isn't in OS trust stores — install AWS's bundle the same way
curl -so ~/.postgresql/root.crt https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
```

The examples' DSNs end in `?sslmode=verify-full`, which uses that file. Alternatively name the CA in the DSN:

```text
…/runtools_db?sslmode=verify-full&sslrootcert=/etc/ssl/company-ca.pem   # a CA file elsewhere
…/runtools_db?sslmode=verify-full&sslrootcert=system                    # a CA your OS already trusts
```

A missing CA fails with `root certificate file "…/.postgresql/root.crt" does not exist`; a host that isn't in the
certificate fails with `server certificate for "…" does not match host name`.

### Passwords

Keep each login's password in `~/.pgpass` on the machines that use it — not in the DSN, which is stored as plain text
in the environment registry. One `host:port:database:login:password` line per login, and the file must be private
(libpq ignores it otherwise):

```text
db.internal:5432:runtools_db:runtools_user:the-password
```

```bash
chmod 600 ~/.pgpass
```

## A. One shared login

One login shared by every machine and person; everyone who has it can do everything.

```sql
-- as a PostgreSQL superuser, in psql
CREATE DATABASE runtools_db;
CREATE ROLE runtools_user LOGIN;
GRANT CREATE ON DATABASE runtools_db TO runtools_user;
\password runtools_user
```

`\password` prompts for the password and sends it hashed, so it never appears in SQL history or server logs. Put it in
`~/.pgpass` on each machine ([Passwords](#passwords)), then:

```bash
taro env create billing --dsn "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"   # once
taro env add billing --dsn "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"      # every other machine
```

## B. Access profiles

A login for each person and machine, each in one of four profiles.

### How it works

runtools has no users or permission checks of its own — access is plain PostgreSQL privileges, in three layers:

```text
logins                  profiles (group roles)             environments (schemas)
stan     ──member of──▶  runtools_admin      ──owns──────▶  billing, reporting
build01  ──member of──▶  runtools_node       ──granted───▶  billing, reporting
alice    ──member of──▶  runtools_operator   ──granted───▶  billing, reporting
bob      ──member of──▶  runtools_viewer     ──granted───▶  billing, reporting
```

- **Login** — a PostgreSQL role someone connects as: one per person or machine.
- **Profile** — a group role nobody logs in as, standing for a permission level: admin, node, operator or viewer. A
  login has a profile's rights by being a member of it.
- **Grant** — a profile's access to one environment's tables, made by `taro env create --grant-profiles`. The admin
  profile needs none: it creates the environment and owns it.

A login can act in an environment only when both links exist — it is a member of a profile, and that profile has
access to the environment. You make the first link with SQL (step 1); runtools makes the second (step 3). So a new
person needs only step 1, and a new environment only step 3.

The four profiles of a set share a prefix: `runtools_admin`, `runtools_node`, … One set for all environments gives
everyone the same rights everywhere; a set per environment (`billing_*`, `reporting_*`) lets a login have different
rights in each — see [Per-environment permissions](#per-environment-permissions).

### What each profile can do

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

As a PostgreSQL superuser, in psql:

```sql
CREATE DATABASE runtools_db;

-- Profiles: group roles nobody logs in as
CREATE ROLE runtools_admin NOLOGIN;
CREATE ROLE runtools_node NOLOGIN;
CREATE ROLE runtools_operator NOLOGIN;
CREATE ROLE runtools_viewer NOLOGIN;
GRANT CREATE ON DATABASE runtools_db TO runtools_admin;   -- lets admins create environments

-- Logins: one per person or machine, each in one profile
CREATE ROLE stan LOGIN;      GRANT runtools_admin TO stan;
CREATE ROLE build01 LOGIN;   GRANT runtools_node TO build01;
CREATE ROLE alice LOGIN;     GRANT runtools_operator TO alice;
CREATE ROLE bob LOGIN;       GRANT runtools_viewer TO bob;

-- Passwords: psql prompts and sends them hashed, so they never appear in SQL history or server logs
\password stan
\password build01
\password alice
\password bob
```

### 2. Store each login's password on its machine

Each person or machine keeps only its own login in `~/.pgpass` ([Passwords](#passwords)):

| Machine | Line to add |
|---|---|
| stan's laptop | `db.internal:5432:runtools_db:stan:…` |
| build01 | `db.internal:5432:runtools_db:build01:…` |
| alice's laptop | `db.internal:5432:runtools_db:alice:…` |
| bob's laptop | `db.internal:5432:runtools_db:bob:…` |

### 3. Create the environment as an admin

On stan's laptop. The DSN switches the session to the `runtools_admin` role (`options=-c role=…`, URL-encoded), so
the environment is owned by the profile rather than one person — any admin can then manage it. `--grant-profiles
runtools` grants `runtools_node`, `runtools_operator` and `runtools_viewer` their access:

```bash
taro env create billing --grant-profiles runtools \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Druntools_admin"
```

- The grants are made in the transaction that creates the environment: a missing role fails the command, and nothing
  is created (`Roles not found: runtools_operator`).
- They come with the tables, so they always match the schema. When the environment is recreated — for example after
  a beta release that changes the schema — the same command restores them.
- runtools only grants; creating the roles and adding logins to them stays with you (step 1).

### 4. Join from every other machine

Each login joins with its own DSN; no role switch is needed, the profile's grants apply automatically:

```bash
taro env add billing --dsn "postgresql://build01@db.internal/runtools_db?sslmode=verify-full"   # on build01
taro env add billing --dsn "postgresql://alice@db.internal/runtools_db?sslmode=verify-full"     # on alice's laptop
taro env add billing --dsn "postgresql://bob@db.internal/runtools_db?sslmode=verify-full"       # on bob's laptop
```

A refused action reports PostgreSQL's error — `permission denied for table signals` (not an operator) or
`must be owner of schema …` (admins only).

### 5. Check it works

Run a small job on the node, then watch and stop it from the viewer's and the operator's laptops:

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
rights in different environments — alice operates `billing` but only watches `reporting` — use a set of profiles per
environment instead. Use this instead of step 1's role and login SQL — as a PostgreSQL superuser, in psql:

```sql
CREATE DATABASE runtools_db;

-- One set of profiles per environment
CREATE ROLE billing_admin NOLOGIN;      CREATE ROLE reporting_admin NOLOGIN;
CREATE ROLE billing_node NOLOGIN;       CREATE ROLE reporting_node NOLOGIN;
CREATE ROLE billing_operator NOLOGIN;   CREATE ROLE reporting_operator NOLOGIN;
CREATE ROLE billing_viewer NOLOGIN;     CREATE ROLE reporting_viewer NOLOGIN;
GRANT CREATE ON DATABASE runtools_db TO billing_admin, reporting_admin;

-- Logins: build02 is a second node, running reporting's jobs
CREATE ROLE stan LOGIN;      GRANT billing_admin, reporting_admin TO stan;
CREATE ROLE build01 LOGIN;   GRANT billing_node TO build01;
CREATE ROLE build02 LOGIN;   GRANT reporting_node TO build02;
CREATE ROLE alice LOGIN;     GRANT billing_operator, reporting_viewer TO alice;
CREATE ROLE bob LOGIN;       GRANT billing_viewer, reporting_viewer TO bob;
-- then \password for each login, as in step 1
```

Create each environment as its own admin role, granting its own profiles:

```bash
taro env create billing --grant-profiles billing \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Dbilling_admin"
taro env create reporting --grant-profiles reporting \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Dreporting_admin"
```

Every login then joins each environment with `taro env add`, as in step 4, and gets in each only what its profiles
there allow: alice's `taro stop -e billing` works, `taro stop -e reporting` is refused, and `build01` can't run jobs in
`reporting` at all — it reports `relation "schema_info" does not exist`, as a schema without `USAGE` is invisible.

Each environment's schema is owned by its own admin role, so `billing_admin` can't change or delete `reporting`.
Still shared by the whole database: who can connect, `CREATE` on it (any environment's admin can create new
environments) and advisory locks (see [Connection security](#connection-security)). For full separation — different
teams or trust levels — give each environment its own database.

## Job output

PostgreSQL holds only the live tail (`taro tail` while a job runs, and briefly after). Complete output lives in the
environment's output storage, which has its own access control: with S3 storage, nodes need write access to the
bucket and anyone running `taro output` on finished runs needs read access — see
[Output storage across machines](environment.md#output-storage-across-machines). A viewer without S3 credentials can
watch running jobs but not read finished runs' full output.

## Connection security

- **Passwords:** in `~/.pgpass`, never in the DSN ([Passwords](#passwords)). `taro env create`/`add` warn about a
  password in the DSN, and `taro env list` masks it.
- **Encryption:** `sslmode=verify-full` ([TLS](#tls)). `sslmode=require` encrypts but skips the hostname check, so it
  doesn't guarantee you reach the right server.
- **Who can connect at all:** login roles and `pg_hba.conf` — limit it to the machines that run or watch jobs.
- **A dedicated database (recommended, not required):** runtools creates and drops only its own `runtools_…` schemas,
  so a shared database works. But advisory locks, which coordinate jobs, are shared by the whole database: any login
  that can connect could hold a lock key and block a mutex or queue. A dedicated database limits that to runtools
  logins and keeps `CREATE` on the database and beta schema resets away from other data.
- **Privileges can come from elsewhere:** role memberships and the database's default privileges (`ALTER DEFAULT
  PRIVILEGES`) may give other roles access beyond what this page grants — check them on shared servers.
- **Pooling:** connect directly or through a session-mode pooler, not a transaction-mode one — coordination relies on
  session-level advisory locks.

## Who did what

runtools records no identities. Separate logins make attribution possible through PostgreSQL's own logging — for
example `log_line_prefix` with `%u` (the login), and a `log_statement` level that covers the operations you care
about. Logs show the original login even when a session switches roles (`session_user`).

## Appendix: granting an existing environment

`--grant-profiles` grants at creation. To grant profiles to an environment created without it, run these grants as a
member of the environment's owner role (e.g. an admin). They need the environment's schema name — `runtools_` + the
environment name (`-` becomes `_`) + a hash; list them with:

```sql
SELECT nspname FROM pg_namespace WHERE nspname LIKE 'runtools\_%' ORDER BY nspname;
```

Save as `grants.sql` — the same grants `--grant-profiles` makes, for the current schema:

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
