# PostgreSQL environments

A PostgreSQL environment lets jobs on many machines share one history: run a job on one machine, then watch, stop
or approve it from any other. The page has two parts:

- **[Tutorial](#tutorial)** — use cases from the simplest to the more involved; each builds on the previous one.
- **[Reference](#reference)** — how it works, all the options, and troubleshooting.

## Tutorial

You need a PostgreSQL server, runtools with the `postgres` extra on each machine, and someone who can create roles
in PostgreSQL — the SQL below runs in `psql` as a database administrator. The examples use the host `db.internal`,
the database `runtools_db` and an environment named `billing`; use your own names.

### 1. Share jobs between two machines

Everyone uses one PostgreSQL login. The administrator creates it with a database:

```sql
CREATE DATABASE runtools_db;
CREATE ROLE runtools_user LOGIN;
GRANT CREATE ON DATABASE runtools_db TO runtools_user;
\password runtools_user
```

On both machines, add the password to `~/.pgpass` and make the file private (`chmod 600 ~/.pgpass`):

```text
db.internal:5432:runtools_db:runtools_user:the-password
```

Create the environment on the first machine, then join it from the second:

```bash
taro env create billing --dsn "postgresql://runtools_user@db.internal/runtools_db"   # first machine
taro env add billing --dsn "postgresql://runtools_user@db.internal/runtools_db"      # second machine
```

Run a job on the first machine:

```bash
run job -e billing -j demo sh -c 'while true; do date; sleep 1; done'
```

Watch and stop it from the second:

```bash
taro ps -e billing            # demo is running
taro tail -e billing demo     # its latest output
taro stop -e billing demo     # stops it on the first machine
taro history -e billing       # demo ended STOPPED
```

Both machines now see the same jobs. Use `-e billing` in these commands to select the shared environment.

The connection isn't verified yet — [use case 3](#3-secure-the-connection) fixes that.

### 2. Run jobs from cron or a service

Servers usually run jobs under a service account. Each OS account keeps its own list of environments, so register
`billing` as that account, with the password in that account's `~/.pgpass`:

```bash
sudo -iu jobs     # become the service account
taro env add billing --dsn "postgresql://runtools_user@db.internal/runtools_db"
```

Then always pass `-e`:

```text
0 2 * * *  run job -e billing -j nightly-report /opt/jobs/report.sh
```

Without `-e`, the job runs in `local` and never shows up in `billing`; `run job` prints a warning, but cron may not
show it to anyone.

### 3. Secure the connection

So far the connection is encrypted only if the server offers it, and nothing checks that the server is the real one.
`sslmode=verify-full` does both. It needs, on each machine, the CA certificate that signed the server's certificate —
ask your administrator for it:

```bash
mkdir -p ~/.postgresql
cp company-ca.pem ~/.postgresql/root.crt
```

On Amazon RDS, use AWS's bundle instead — see [TLS](#tls).

Then add `?sslmode=verify-full` to the DSN. For an environment already registered, edit its `location` in
`~/.config/runtools/environments.toml`:

```toml
[environments.billing]
kind = "postgres"
location = "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"
```

Don't use `taro env delete` to re-register: it drops the shared environment for everyone. New registrations simply
include `?sslmode=verify-full` from the start, as in the rest of this tutorial.

### 4. Let someone only watch

So far everyone shares one login and can do everything. To give Bob read-only access, he gets his own login in a
**profile** — a PostgreSQL group role standing for a permission level. runtools uses three:

| Profile | For |
|---|---|
| `runtools_viewer` | watching jobs and output |
| `runtools_operator` | also stopping, approving and cleaning up jobs |
| `runtools_node` | machines that run jobs |

The administrator creates them once:

```sql
CREATE ROLE runtools_node NOLOGIN;
CREATE ROLE runtools_operator NOLOGIN;
CREATE ROLE runtools_viewer NOLOGIN;
```

The profiles then need access to each environment:

- **An existing environment**, like `billing`: grant it once, on the machine that created it — only the login that
  created an environment can grant access to it:

  ```bash
  taro env grant billing --profiles runtools
  ```

- **A new environment**: grant it as you create it:

  ```bash
  taro env create reporting --grant-profiles runtools --dsn "…"
  ```

Now Bob's login — the administrator runs:

```sql
CREATE ROLE bob LOGIN;
GRANT runtools_viewer TO bob;
\password bob
```

Bob puts his password in `~/.pgpass`, installs the CA, and joins with his own login:

```bash
taro env add billing --dsn "postgresql://bob@db.internal/runtools_db?sslmode=verify-full"
```

He can watch (`taro ps`, `taro tail`, `taro history`), but `taro stop -e billing demo` is refused with
`permission denied for table signals`.

He can watch every environment the `runtools_*` profiles have access to — here `billing` and `reporting`.
To limit him to one, see [use case 7](#7-separate-teams).

### 5. Let a teammate stop jobs

Alice should also stop and approve jobs. With the profiles in place, it's one grant:

```sql
CREATE ROLE alice LOGIN;
GRANT runtools_operator TO alice;
\password alice
```

She joins like Bob, with her own login, and her `taro stop` works.

### 6. Give a job server its own login

Servers that only run jobs don't need the shared login. Give each one a login in the node profile:

```sql
CREATE ROLE build01 LOGIN;
GRANT runtools_node TO build01;
\password build01
```

On the server, register `billing` with that login, as the account that runs the jobs:

```bash
sudo -iu jobs
taro env add billing --dsn "postgresql://build01@db.internal/runtools_db?sslmode=verify-full"
```

If that account already registered `billing` with the shared login ([use case 2](#2-run-jobs-from-cron-or-a-service)),
`add` refuses it as already registered — change the login in its `location` instead, as in
[use case 3](#3-secure-the-connection).

The server can run jobs, but can't change the environment's settings or delete it. Once every person and server has
its own login, nobody needs the shared `runtools_user` login except whoever administers the environment.

### 7. Separate teams

The `runtools_*` profiles give the same rights in every environment. If Alice should operate `billing` but only watch
`reporting`, give each environment its own set of profiles, named after it:

```sql
CREATE ROLE billing_node NOLOGIN;     CREATE ROLE reporting_node NOLOGIN;
CREATE ROLE billing_operator NOLOGIN; CREATE ROLE reporting_operator NOLOGIN;
CREATE ROLE billing_viewer NOLOGIN;   CREATE ROLE reporting_viewer NOLOGIN;
```

Give each set access to its own environment, as in use case 4:

- **Existing environments**, like `billing` and `reporting`:

  ```bash
  taro env grant billing --profiles billing
  taro env grant reporting --profiles reporting
  ```

- **A new environment**: create it with `--grant-profiles`. Its roles (`payments_node`, `payments_operator`,
  `payments_viewer`) must exist first, created like the ones above — otherwise the command fails and creates nothing:

  ```bash
  taro env create payments --grant-profiles payments --dsn "…"
  ```

Then move Alice. Permissions add up, so she must also leave the shared operator profile — otherwise it still lets her
stop jobs in `reporting`:

```sql
REVOKE runtools_operator FROM alice;
GRANT billing_operator, reporting_viewer TO alice;
```

Now her `taro stop -e billing …` works and `taro stop -e reporting …` is refused. To also give each environment its
own administrators, see [Admin profiles](#admin-profiles).

## Reference

### What runtools creates

You provide the database; each environment becomes one schema in it:

```text
database "runtools_db"
├── schema runtools_billing_cacfdbb4fbf4   ← environment "billing"
│   ├── runs          one row per job run: state, timestamps, the node's heartbeat
│   ├── run_tags      tags of runs
│   ├── signals       commands waiting for a node: stop, approve, resume, …
│   ├── output_tail   the newest ~1000 output lines of running jobs (a cache, dropped after the run ends)
│   ├── config        environment settings: output storage, retention, tail size
│   └── schema_info   schema version
└── schema runtools_reporting_…            ← environment "reporting"
```

The schema name is `runtools_` + the environment name (`-` becomes `_`) + a hash. List them with:

```sql
SELECT nspname FROM pg_namespace WHERE nspname LIKE 'runtools\_%' ORDER BY nspname;
```

### Registering environments

- `taro env create` provisions the schema and default settings, and registers the environment on this machine. It
  needs `CREATE` on the database and refuses a schema that already exists — the settings are shared, so they are
  never re-seeded.
- `taro env add` only registers an existing environment; it fails if the schema doesn't exist, so a wrong name or
  DSN shows up at once.
- The registry is per OS account: `~/.config/runtools/environments.toml`. Every account that runs `run job` or `taro`
  needs its own `add`.
- `taro env delete` on a postgres environment drops its schema — for everyone, not only on this machine.
- Every machine must use the same environment name: runtools finds the schema by it.

**Without `-e`**, commands use the built-in `local` environment. Once another environment is registered, `run job`
still runs in `local` but warns, and `taro` asks which one in a terminal and requires `-e` in scripts.

### Profiles and permissions

runtools has no users or permission checks of its own — access is plain PostgreSQL privileges in two links:

```text
login    ──member of──▶  profile (group role)  ──has access to──▶  environment (schema)
alice                    runtools_operator                         billing, reporting
```

A login can act in an environment only when both links exist. You make the first with `GRANT <profile> TO <login>`;
runtools makes the second (`taro env grant`, or `--grant-profiles` on create).

| Profile | For | Can | Cannot |
|---|---|---|---|
| admin (schema owner) | whoever administers the environment | everything below, plus change settings (`taro env edit`) and delete the environment | — |
| node | machines that run jobs | record runs, publish output, apply commands | change settings, delete the environment |
| operator | people managing jobs | view, plus `stop`, `approve`/`resume`, `mark-lost`, `clean`, `prune` | change settings, delete the environment |
| viewer | people watching | `ps`, `dash`, `history`, `tail`, `env show` | anything that writes |

- Grants are per table, not per command: node and operator both write `runs` and `signals`, so an operator could also
  write those tables with plain SQL. The firm boundaries are viewer (read-only) and admin (settings and deletion).
- Permissions add up: a login also keeps whatever its other role memberships, and the database's default privileges
  (`ALTER DEFAULT PRIVILEGES`), give it.
- After a beta release that recreates environments, create them again with the same `--grant-profiles` prefix. Roles
  and memberships stay as they are.

### Granting profiles

runtools grants the existing roles `PREFIX_node`, `PREFIX_operator` and `PREFIX_viewer` access to an environment; it
never creates roles or logins.

- `taro env create NAME --grant-profiles PREFIX` grants in the same transaction that creates the environment.
- `taro env grant NAME --profiles PREFIX` grants on an existing environment. Run it with the login that created the
  environment, or a member of its [admin role](#admin-profiles). Any other login is refused, because PostgreSQL
  would only warn and grant nothing. Repeating it is harmless.

Either way, a missing role fails the command, and nothing is created or granted.
The exact grants are `_PROFILE_GRANTS` in
[`runcore/db/postgres.py`](https://github.com/runtoolsio/runcore/blob/main/src/runtools/runcore/db/postgres.py).

#### Admin profiles

The environment belongs to the login that created it. To let a group administer it instead, create an admin role,
allow it to create environments, and switch to it in the creating DSN (`options=-c role=…`, URL-encoded):

```sql
CREATE ROLE billing_admin NOLOGIN;
GRANT CREATE ON DATABASE runtools_db TO billing_admin;
GRANT billing_admin TO stan;
```

```bash
taro env create billing --grant-profiles billing \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Dbilling_admin"
```

Each environment's schema is owned by its admin role, so `billing_admin` can't change or delete `reporting`. Ownership
doesn't move later: an existing environment stays with whoever created it.

### TLS

`sslmode=verify-full` encrypts the connection and checks the server's certificate and hostname. The client needs the
CA that signed the server's certificate, and the DSN's host must be a name in that certificate.

```bash
# a company or self-signed CA: where libpq looks by default
mkdir -p ~/.postgresql && cp company-ca.pem ~/.postgresql/root.crt

# Amazon RDS: its CA isn't in OS trust stores — install AWS's bundle
curl -fsS -o ~/.postgresql/root.crt https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
```

Or name the CA in the DSN:

```text
…/runtools_db?sslmode=verify-full&sslrootcert=/etc/ssl/company-ca.pem   # a CA file elsewhere
…/runtools_db?sslmode=verify-full&sslrootcert=system                    # a CA the OS trusts (libpq 16+)
```

A missing CA fails with `root certificate file "…/.postgresql/root.crt" does not exist`; a host that isn't in the
certificate fails with `server certificate for "…" does not match host name`.

**Without a CA file:** for password logins using SCRAM, `?sslmode=require&channel_binding=require` ties the login to
the TLS connection, so a server in the middle can't complete it. It fails if channel binding is unavailable —
including for a password stored as `md5`; run `SET password_encryption = 'scram-sha-256';` before `\password` to be
sure. It doesn't verify the certificate or hostname: `verify-full` remains the recommendation.

### Passwords

Keep passwords in `~/.pgpass` (`host:port:database:login:password`, mode `600` — libpq ignores it otherwise), never
in the DSN: the DSN is stored as plain text in the registry. `taro env create`/`add` warn about a password in the DSN,
and `taro env list` masks it. Set passwords with `\password`, which sends them hashed, so they don't end up in SQL
history or server logs.

### Connection security

- **A dedicated database** is recommended, not required. runtools creates and drops only its own `runtools_…`
  schemas, but advisory locks, which coordinate jobs, are shared by the whole database: any login that can connect
  could hold a lock key and block a mutex or queue. A dedicated database limits that to runtools logins and keeps
  `CREATE` on the database and beta schema resets away from other data. For a hard boundary between teams, give each
  its own database.
- **Who can connect:** login roles and `pg_hba.conf` (or your managed service's equivalent) — limit it to the
  machines that run or watch jobs.
- **Pooling:** connect directly or through a session-mode pooler, not a transaction-mode one — coordination relies on
  session-level advisory locks.

### Job output

PostgreSQL holds only the live tail (`taro tail` while a job runs, and briefly after). Complete output lives in the
environment's output storage, which has its own access control: with S3, nodes need write access to the bucket and
anyone running `taro output` on finished runs needs read access — see
[Output storage across machines](environment.md#output-storage-across-machines). A viewer without S3 credentials can
watch running jobs but not read finished runs' full output.

### Who did what

runtools records no identities. Separate logins make attribution possible through PostgreSQL's own logging — for
example `log_line_prefix` with `%u` (the login), and a `log_statement` level that covers the operations you care
about. Logs show the original login even when a session switches roles (`session_user`).

### Troubleshooting

| Error | Cause |
|---|---|
| `permission denied for table signals` | the login isn't an operator in this environment |
| `must be owner of schema …` | only the environment's admin can do this |
| `relation "schema_info" does not exist` | the login may lack `USAGE` on the schema — check its profile was granted on this environment |
| `No environment 'billing' at that location` (`env add`) | wrong environment name or DSN, or the environment wasn't created yet |
| `Roles not found: …` (`env create`, `env grant`) | create the profile roles first; nothing was created or granted |
| `Not the owner of environment …` (`env grant`) | run it with the login that created the environment, or a member of its admin role |
| `root certificate file … does not exist` | install the CA — see [TLS](#tls) |
| `server certificate for "…" does not match host name` | use the host name the certificate was issued for |
