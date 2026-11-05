# PostgreSQL environments

Use a PostgreSQL environment when jobs on different machines need to share history, coordinate, or be controlled
from one place. You create the environment once, then join it from each machine that runs or watches jobs.

Start with the task you need:

| I want to… | Start here |
|---|---|
| Try runtools across two machines | [One shared login](#try-it-across-two-machines) |
| Let someone watch without changing anything | [Add a viewer](#let-someone-watch-jobs) |
| Let a teammate stop and approve jobs | [Add an operator](#let-a-teammate-operate-jobs) |
| Give a job server its own credentials | [Add a node login](#give-a-job-server-its-own-login) |
| Give people different access to billing and reporting | [Per-environment permissions](#give-teams-different-access-to-environments) |

The first recipe gets you running. The next three build on it. The team recipe is an alternative for new environments.
You don't need to read the whole page before starting.

## Before you start

You'll need PostgreSQL, `run` and `taro` installed with PostgreSQL support, and someone who can create database roles.
The SQL examples run in `psql`. Your database administrator can run them for you.

Replace `db.internal` with your database host. We use `runtools_db` for the database and `billing` for the environment;
you can choose other names. Every machine must use the same environment name because runtools uses it to find the
right schema.

### TLS

The examples use `sslmode=verify-full`, which encrypts the connection and checks the server's identity. Ask your
administrator for the server's CA certificate and install it on each machine:

```bash
mkdir -p ~/.postgresql
cp company-ca.pem ~/.postgresql/root.crt
```

The hostname in the connection string must match the server certificate. If you're using Amazon RDS or need another
way to configure TLS, see [Connection security](#connection-security).

### Passwords

Keep passwords in `~/.pgpass`, not in connection strings. Runtools saves connection strings in its local registry.
Each machine needs a line for the login it uses:

```text
db.internal:5432:runtools_db:runtools_user:the-password
```

Protect the file:

```bash
chmod 600 ~/.pgpass
```

In later recipes, replace `runtools_user` with that person's or machine's login. The `\password` commands below prompt
for a password without putting the plaintext password in your SQL history.

## Try it across two machines

This is the simplest setup for a trusted team. Everyone shares one login and can manage the environment.

### Create the database and login

Ask your PostgreSQL administrator to run:

```sql
CREATE DATABASE runtools_db;
CREATE ROLE runtools_user LOGIN;
GRANT CREATE ON DATABASE runtools_db TO runtools_user;
\password runtools_user
```

Put that password in `~/.pgpass` on both machines, as shown above.

### Create once, join elsewhere

On the first machine:

```bash
taro env create billing --dsn "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"
```

On the second machine:

```bash
taro env add billing --dsn "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"
```

`create` creates the schema and default settings, and registers the environment locally. `add` only registers it;
it doesn't change the shared settings. Each OS account has its own registry, so another account on the same machine
also needs to run `add`.

### Run, watch, and stop a job

On the first machine, start a job that prints the time every second:

```bash
run job -e billing -j demo sh -c 'while true; do date; sleep 1; done'
```

On the second machine:

```bash
taro ps -e billing
taro tail -e billing demo
taro stop -e billing -f demo
```

Once the job finishes, check its history:

```bash
taro history -e billing
```

You should see `demo` with a `STOPPED` outcome. Stopping is a request to the node, so completion may take a moment.

Always use `-e billing` for jobs meant for this environment. Without `-e`, `run job` uses the built-in `local`
environment, even when `billing` is registered. It warns in that case, but still runs locally. Interactive `taro`
asks you to choose when several environments are available; in scripts it requires `-e`.

## Let someone watch jobs

Suppose Bob needs to see jobs and their output, but shouldn't stop jobs or change settings.
This recipe assumes `billing` already exists. Its owner sets up the permissions; Bob only needs his own login.

### Set up the environment's profiles once

A profile is just a PostgreSQL group role. Runtools uses three conventional names for node, operator, and viewer
access. Ask your database administrator to create the set for billing, unless it already exists:

```sql
CREATE ROLE billing_node NOLOGIN;
CREATE ROLE billing_operator NOLOGIN;
CREATE ROLE billing_viewer NOLOGIN;
```

For an existing environment, its owner applies the [existing-environment grants](#granting-an-existing-environment)
once, using prefix `billing`. If you're setting up a new environment instead, create these roles first and use:

```bash
taro env create billing --grant-profiles billing \
  --dsn "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full"
```

Choose the route that fits your setup. Don't run `create` again for an existing environment.
The flag grants access to the three roles; it doesn't create roles or logins. Provisioning and grants happen in one
transaction, so a missing role fails the command without leaving a new environment behind.

### Add Bob

The database administrator runs:

```sql
CREATE ROLE bob LOGIN;
GRANT billing_viewer TO bob;
\password bob
```

If Bob already has a login, only the `GRANT` is needed.

On Bob's machine, configure the CA and add this line to his private `~/.pgpass`:

```text
db.internal:5432:runtools_db:bob:his-password
```

Then Bob joins:

```bash
taro env add billing --dsn "postgresql://bob@db.internal/runtools_db?sslmode=verify-full"
```

### Check Bob's access

With a demo job running, Bob can watch it:

```bash
taro ps -e billing
taro tail -e billing demo
taro history -e billing
```

This command should be refused with a PostgreSQL permission error for `signals`:

```bash
taro stop -e billing -f demo
```

These restrictions assume Bob has no broader access through other role memberships or grants. For complete output
stored outside PostgreSQL, see [Job output](#job-output).

## Let a teammate operate jobs

Suppose Alice needs to stop and approve jobs, resolve lost runs, and clean up history. Use the `billing_operator`
profile from the previous recipe. Its grants must already have been applied to billing.

The database administrator runs:

```sql
CREATE ROLE alice LOGIN;
GRANT billing_operator TO alice;
\password alice
```

For an existing login, run only the `GRANT`. Alice adds her password to `~/.pgpass`, configures the CA, and joins:

```bash
taro env add billing --dsn "postgresql://alice@db.internal/runtools_db?sslmode=verify-full"
```

With a demo job running, Alice can stop it:

```bash
taro stop -e billing -f demo
```

After it finishes, `taro history -e billing` shows its outcome. Alice can also use `approve`, `resume`, `env mark-lost`,
`env clean`, and `env prune`. She cannot edit environment settings or delete the environment itself.

## Give a job server its own login

Use the `billing_node` profile for a server that runs jobs. This avoids sharing the environment owner's credentials.
As above, the profile and its grants must already exist.

The database administrator runs:

```sql
CREATE ROLE build01 LOGIN;
GRANT billing_node TO build01;
\password build01
```

On the server, configure the CA and `~/.pgpass` for `build01`, then register the environment under the OS account
that runs the jobs:

```bash
taro env add billing --dsn "postgresql://build01@db.internal/runtools_db?sslmode=verify-full"
run job -e billing -j node-check echo hello
```

Check `taro history -e billing` for a completed `node-check` run. The node can record runs, publish output, and apply
commands. It cannot edit environment settings or drop the environment.

## Give teams different access to environments

Use a separate profile set for each environment when people need different access. In this example, Alice operates
billing but only watches reporting. Stan administers both.

This is a recipe for two new environments. Use an existing `runtools_db`, or ask your administrator to create it:

```sql
CREATE DATABASE runtools_db;
```

The database administrator creates the profiles:

```sql
CREATE ROLE billing_admin NOLOGIN;
CREATE ROLE billing_node NOLOGIN;
CREATE ROLE billing_operator NOLOGIN;
CREATE ROLE billing_viewer NOLOGIN;

CREATE ROLE reporting_admin NOLOGIN;
CREATE ROLE reporting_node NOLOGIN;
CREATE ROLE reporting_operator NOLOGIN;
CREATE ROLE reporting_viewer NOLOGIN;

GRANT CREATE ON DATABASE runtools_db TO billing_admin, reporting_admin;
```

For new logins, run:

```sql
CREATE ROLE stan LOGIN;
CREATE ROLE alice LOGIN;
\password stan
\password alice
```

Skip those statements for logins that already exist. Assign membership:

```sql
GRANT billing_admin, reporting_admin TO stan;
GRANT billing_operator, reporting_viewer TO alice;
```

Permissions add up. If Alice already belongs to a broader operator or admin role, adding viewer membership doesn't
remove that access. Review her existing memberships before relying on this separation.

After configuring his CA and `~/.pgpass`, Stan creates the environments. The connection strings switch to the matching
admin role so each schema belongs to a group, rather than to Stan personally:

```bash
taro env create billing --grant-profiles billing \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Dbilling_admin"
taro env create reporting --grant-profiles reporting \
  --dsn "postgresql://stan@db.internal/runtools_db?sslmode=verify-full&options=-c%20role%3Dreporting_admin"
```

Alice configures her CA and `~/.pgpass`, then joins both:

```bash
taro env add billing --dsn "postgresql://alice@db.internal/runtools_db?sslmode=verify-full"
taro env add reporting --dsn "postgresql://alice@db.internal/runtools_db?sslmode=verify-full"
```

She can view both environments. With a demo job running in each, `taro stop -e billing -f demo` is allowed and
`taro stop -e reporting -f demo` is refused. Add job-server logins to each environment's node profile using the
[node recipe](#give-a-job-server-its-own-login).

For existing environments, apply the [manual grants](#granting-an-existing-environment) for each prefix instead of
creating them again. Grants don't change schema ownership; creating separate admin profiles doesn't transfer
existing environments to them.

## Reference

You can stop here if your setup works. The rest explains the database layout and the less common setup options.

### What runtools creates

Each environment is one schema inside your PostgreSQL database:

```text
database "runtools_db"
├── schema runtools_billing_cacfdbb4fbf4
│   ├── runs          run state, history, and heartbeats
│   ├── run_tags      tags attached to runs
│   ├── signals       pending commands such as stop and approve
│   ├── output_tail   recent output, retained briefly after completion
│   ├── config        output storage, retention, and other settings
│   └── schema_info   schema version
└── schema runtools_reporting_…
```

`create` provisions a schema and registers it. `add` registers an existing one without changing its settings.
Registration is per OS user, in `~/.config/runtools/environments.toml` by default.

### Permissions at a glance

Runtools has no separate user database. A login gets access through membership in a PostgreSQL role that has the
appropriate grants:

```text
bob → billing_viewer → read access to billing's tables
alice → billing_operator → read and operational write access to billing's tables
```

| Profile | Typical use | Access |
|---|---|---|
| admin / schema owner | Environment administration | All environment operations, including config changes and schema deletion |
| node | Job servers | Read data, record runs and tags, publish output, consume commands |
| operator | People managing jobs | Read data, send commands, resolve lost runs, clean transport rows, prune history |
| viewer | Monitoring | Read data; cannot write through these grants |

These are table permissions, not restrictions on individual CLI commands. A role allowed to update `runs` can also
update it through SQL. Node and operator permissions overlap, but neither profile grants config writes or schema
ownership. Each environment's admin owns its schema; `--grant-profiles` configures only node, operator, and viewer.

If a login's commands fail with `relation "schema_info" does not exist`, it may lack `USAGE` on the environment's
schema — check that its profile was granted on that environment.

When a beta release requires recreating an environment, pass the same `--grant-profiles` prefix again to restore its
grants. Keep the PostgreSQL roles and memberships unless you also intend to change who has access.

### Job output

PostgreSQL holds the recent tail, not complete output. Full output lives in the configured output backend.
For S3, nodes need bucket write access, and people reading finished runs with `taro output` need read access.
PostgreSQL viewer membership doesn't provide those credentials. See
[Output storage across machines](environment.md#output-storage-across-machines).

### Connection security

A dedicated database is recommended. Environments use separate schemas, but advisory locks and database-level
permissions are shared. Separate databases give teams a stronger boundary than separate schemas alone.

Allow the required logins and machines in `pg_hba.conf`, or the equivalent settings on your managed service.
Connect directly or through a session-mode pooler. Transaction-mode pooling doesn't support the session advisory
locks used for coordination.

For Amazon RDS, you can install its CA bundle on each client:

```bash
mkdir -p ~/.postgresql
curl -fSs -o ~/.postgresql/root.crt https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
```

You can also choose the trust source in the connection string:

```text
…/runtools_db?sslmode=verify-full&sslrootcert=/etc/ssl/company-ca.pem
…/runtools_db?sslmode=verify-full&sslrootcert=system
```

Use `system` only when your libpq version supports it and the issuing CA is in that system trust store.
A missing CA file or a hostname that doesn't match the certificate causes the connection to fail.

For a TLS connection using SCRAM password authentication, `sslmode=require&channel_binding=require` is an alternative
that doesn't require distributing a CA. It requires channel binding and fails if it is unavailable. It doesn't check
the certificate chain or hostname; `verify-full` remains the recommendation. An MD5-stored password isn't enough:
the administrator must set `password_encryption = 'scram-sha-256'` before resetting the password with `\password`,
and the server must use SCRAM authentication.

### Who did what

Runtools doesn't record the identity of the person issuing each command. Separate logins allow attribution through
PostgreSQL logging when configured, for example using `%u` in `log_line_prefix` and a suitable `log_statement` setting.

### Granting an existing environment

`--grant-profiles` grants at creation. To grant profiles to an environment created without it, run these grants as
the environment owner, or a member of its role. They need the environment's schema name — `runtools_` + the
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
psql "postgresql://runtools_user@db.internal/runtools_db?sslmode=verify-full" \
  -v schema=runtools_billing_cacfdbb4fbf4 -v prefix=billing -f grants.sql
```
