# Changelog

All five packages (runcore, runjob, runcli, taro, runtoolsio) are released in lockstep during the beta.
**Breaking** marks changes that need action when upgrading.

## 0.3.0b4 — 2026-10-29

- runjob: stopping a job (`taro stop`, timeout, SIGTERM to `run job`) now also stops the processes it started, for
  jobs not running in a terminal's foreground — cron, systemd, services, and background jobs (`run job … &`,
  `nohup run job … &`). Before, only the program itself was stopped; a surviving child kept the output open and the
  run never ended. Such jobs now run in their own process group.
- Unchanged: jobs in a terminal's foreground still stop only the program itself. A background job brought back with
  `fg` stays in its own process group, so Ctrl+C reaches only `run job` and terminal input stops the job.
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
