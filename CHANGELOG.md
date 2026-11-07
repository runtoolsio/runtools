# Changelog

All five packages (runcore, runjob, runcli, taro, runtoolsio) are released in lockstep during the beta.
**Breaking** marks changes that need action when upgrading.

## 0.3.0b5 — unreleased

- runcli: `run job` and `run env` without `-e` warn on stderr when other environments are registered — they still
  use `local`, so registering an environment never stops existing jobs, but a job meant for a shared environment and
  missing `-e` no longer runs there unnoticed. The `--env` help no longer mentions a default from config, which
  doesn't exist.
- taro: `taro env create NAME --dsn DSN --grant-profiles PREFIX` grants the existing roles `PREFIX_node`,
  `PREFIX_operator` and `PREFIX_viewer` access to the new postgres environment, in the transaction that creates it —
  a missing role fails the command and nothing is created. `taro env grant NAME --profiles PREFIX` does the same for
  an existing environment, run with its owner's login — another login is refused, as PostgreSQL would only warn and
  grant nothing. Both replace the guide's `grants.sql`.
- A refused database operation prints `Permission denied in environment 'billing': permission denied for table
  signals` instead of a traceback — in taro, `run job`, and the TUI's delete, which also no longer closes the
  dashboard. A login without access to the environment gets `permission denied for schema …` instead of
  `relation "schema_info" does not exist`. runcore: postgres raises `PermissionDeniedError` (the psycopg error is its
  cause).
- taro: `taro env prune` removes only ended runs — `--keep-days 0` no longer fails while a matched job is running, and
  `--keep-days N` no longer fails with a `TypeError` once runs older than N days exist.
- Docs: PostgreSQL guide — a tutorial of use cases, simplest first, then a reference; per-environment permissions.

## 0.3.0b4 — 2026-10-29

- runjob: stopping a job (`taro stop`, timeout, SIGTERM to `run job`) now also stops the processes it started, for
  jobs not running in a terminal's foreground — cron, systemd, services, and background jobs (`run job … &`,
  `nohup run job … &`). Before, only the program itself was stopped; a surviving child kept the output open and the
  run never ended. Such jobs now run in their own process group.
- Unchanged: jobs in a terminal's foreground still stop only the program itself. A background job brought back with
  `fg` stays in its own process group, so Ctrl+C reaches only `run job` and terminal input stops the job.
- runcli: `run job --approve PHASE_ID` runs the program only after `taro approve -p PHASE_ID` (waiting holds no queue
  slot or exclusion lock); `run job --depends-on PATTERN` (repeatable, globs) ends the job `UNSATISFIED` unless a
  matching job is running — a lost run doesn't count; `run job --await PATTERN` (repeatable, `--await-timeout`)
  waits until a matching run has ended, with any outcome, like `taro wait` — runs that already ended count, so name
  the run (`JOB@RUN_ID` with a shared run ID), or skip history with `--await-future-only` (runjob: `AwaitPhase`
  takes `search_past`, default `True`).
- Docs: PostgreSQL environment guide (`docs/postgres.md`) — what runtools creates, TLS setup, tested access profiles.

## 0.3.0b3 — 2026-10-27

- postgres: a DSN's `options` (e.g. `-c role=runtools_ops` for logins sharing a group role) now reach every pooled
  connection — before, they were replaced, so environments ended up owned by the individual login.
- `taro env list` masks DSN passwords; `taro env create`/`add` warn when the DSN holds one (prefer `~/.pgpass`).
- Docs: postgres security — access is PostgreSQL privileges, TLS with `verify-full`, one login vs. separate logins
  sharing a group role.

## 0.3.0b2 — 2026-10-27

- `taro env create NAME --dsn DSN` creates a postgres environment; `taro env add NAME --dsn DSN` joins one created
  from another machine without touching its stored config.
- **Breaking:** `create` refuses a store that already exists instead of re-seeding its config — use `taro env add`.
- New `s3` extra (`runtoolsio[s3]`) for S3 output storage, so finished runs' output is readable from any machine.
- taro: the dashboard and per-job screens line up their active and history columns (history gains `N`, `ENDED` moves
  after `TIME`) and hide low-priority columns instead of scrolling sideways in narrow panes.
- taro: `?` on the instance screen shows the panel's keys; terminals without truecolor get an ANSI theme drawn in
  the terminal's own palette; requires Textual 8.2.8.
- taro: options are accepted after instance patterns (`taro stop job@r1 -f -e prod`).

## 0.3.0b1 — 2026-10-24

First beta. Lost-run resolution on the `postgres` kind (`JobRun.is_lost`, `taro env mark-lost`, `taro env clean`),
the `JobInstance` contract split (`run()` is producer-side only), lockstep versions.

- Database schema may change during the beta without a migration: such a release says so here, and environments
  must then be recreated (history is lost). Migrations arrive with 1.0.
