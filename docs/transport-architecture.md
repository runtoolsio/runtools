# Transport Architecture

## Purpose

Design brief for the `runtools` environment/transport layer. Records the agreed
design, the open points, and (briefly) the rejected paths so they are not
re-litigated. Written for a reader picking up the work mid-flight.

## Status

**Agreed:** the proxy-first access model below. It supersedes the earlier
flat-RPC design (an `InstanceAccess` bundle consumed by a generic
`JobInstanceProxy`); that design's naming difficulties were a symptom of its
grab-bag seam.
**Implemented:** the directory sweep — `InstanceDirectory`/`InstanceDiscovery`
seams, `InstanceDirectoryBase` (identity map, buffered replay, ended
tombstones), the unix-socket directory/discovery/proxy, the slim `_Connector`,
and the access-point renames. **The persistence write+read path (design point 4):**
`RunStatePersister` (coalesced flush), `read_active_runs` +
`DbInstanceDiscovery`, the `updated_at` + `state_updated_at` columns with the
newer-wins guard, `active_run_versions()`, and the poll directory itself
(`PollingInstanceDirectory` + `SnapshotJobInstanceProxy` + snapshot-diff
synthesis) — all across both storage backends (PostgreSQL alongside SQLite,
sharing `runcore/db/sql.py`). **The backend-kind slice:** environments are
selected as curated backend kinds (`EnvironmentKind.LOCAL` / `POSTGRES` on the
entry, per-kind config classes, one `match entry.kind` per entry point — see
the implementation plan below; the earlier backend-registry sketch was
superseded by this simpler shape). The poll lane is live as the `postgres`
kind's connector directory. Internally, storage, directory/live transport,
locks, and output remain separate units with narrow interfaces; a kind wires a
supported combination. `NOTIFY` doorbells/triggers stay the deferred
escalation path.
**Implemented:** coordination redesign — claim-then-act (point 6): mutex holds
its group claim, queue claims slots with seniority-staggered attempts;
`signal_dispatch` and the distributed dispatcher are deleted. The `postgres`
kind's coordination prerequisite is met.
**Implemented:** the `postgres` producer slice — `node.connect` composes
postgres storage + polling sibling directory + advisory locks +
`PostgresInstanceAccessPoint` (an empty receiver at that point — now the
signal reconciler, point 5);
nodes hold the sibling `InstanceDirectory` directly (the embedded sibling
connector is gone — it duplicated the node's own db/store references and
muddled close ownership); node live reads merge own instances over the
directory view.
**Implemented:** heartbeat/liveness (point 4) — nodes touch `heartbeat_at` on
the flush thread; readers get server-clock heartbeat *ages* through the version
scan and mark stale proxies lost (interpret-never-mutate).
**Implemented:** signals-as-state (point 5) — remote `stop` and phase control
work on the `postgres` kind: consumer proxies write mailbox rows, the node's
signal reconciler (`PostgresInstanceAccessPoint`, coarse poll) applies them at
the instance's control apply point, which records `JobRun.control_requests`
(the durable, single-writer audit) and emits the generalized
`InstanceControlEvent`; polled consumers synthesize the same event from the
snapshot diff. Applied rows are deleted; orphans swept on a slow cadence.
**Implemented:** the output lane (point 7) — live tail through a bounded,
UNLOGGED per-instance tail table in the env db (the node's tail buffer made
shared); publisher owned by the db access point (registration-time observer
subscription), pull-only consumer reads via the snapshot proxy. `taro tail -f`
on polled kinds remains a consumer-side follow-up.
**Open:** split the `JobInstance` contract (deferred until it bites).

## Mental model

A `runtools` *environment* is a shared space: producer processes (nodes) own
and run job instances; consumer processes reach in to find, observe, and
control them.

The consumer API is **proxy-first**: consumers obtain `JobInstance` objects
and work with them directly. A remote instance presents the same interface as
a local one.

```
   consumer process                          producer process
  ┌────────────────────┐                   ┌────────────────────┐
  │ _Connector         │                   │ _ComposedNode      │
  │   directory ──────┐│                   │   access_point ───┐│
  │   db (history)    ││                   │   instances       ││
  └───────────────────││                   └───────────────────││
                       ││   transport (wire+layout+lifecycle)   ││
                       │└────────────────────────────────────────┘
                       └─────────────────────────────────────────┘

   directory.get_instance(id) -> JobInstance proxy (transport-specific)
```

A **transport** is the umbrella term for how the two sides communicate: wire
protocol, addressing, discovery, layout, resource lifecycle. It is realised as
a matched pair — one `InstanceDirectory`, one `InstanceAccessPoint` — plus the
transport's own proxy classes. No class is literally named `Transport`.

## Backend Bundles

**Decision.** The user-facing environment choice is a curated backend kind, not
an a-la-carte cross-product of storage, live transport, locking, and output.

Internal units stay separate and reusable:

```text
storage driver       sqlite | postgres              -> ConfigStorage + RunStorage
directory/transport  unix_socket | polling | redis   -> InstanceDirectory
lock provider        memory | file | advisory        -> LockProvider
output backend       file | s3 | ...                 -> OutputBackend
```

A backend kind is a recipe that wires a supported set of units:

```text
local       -> sqlite   + unix_socket directory            + local locks
postgres    -> postgres + postgres-backed directory         + advisory locks
distributed -> postgres + redis directory                   + redis/advisory locks
```

This keeps implementation flexibility without exposing meaningless or
unsupported user combinations such as SQLite plus a Postgres live transport. If
a mixed topology becomes useful, ship it as a named backend kind (for example
`distributed` / `postgres_redis`) with one documented failure model and tests,
not as independent user knobs.

Consequences:

- `EnvironmentEntry.driver` becomes `EnvironmentEntry.kind`.
- `EnvironmentConfig.transport` goes away; transport/directory selection is an
  internal part of the backend recipe. `EnvironmentConfig` becomes per-kind
  (`LocalEnvironmentConfig` / `PostgresEnvironmentConfig`), chosen by `entry.kind`
  at load (no stored `config.kind`); kind-specific settings such as `root_dir`
  live there, not on the entry.
- Backend implementations may compose multiple substrates internally, but the
  public config names supported bundles.
- The current DB-polling directory is an internal state-observation unit. It is
  not, by itself, a complete user-facing transport because output, control, and
  coordination are separate lanes.
- PostgreSQL live state should use the already opened PostgreSQL environment
  database/pool by default. Separate connections or brokers are backend-internal
  choices, added only when the backend kind requires them.
- `in_process` remains a direct constructor for tests/dev embedding, not a
  registry kind. Registry kinds start as `local`, `postgres`, and later
  `distributed`.

Implementation plan (kind slice — **no backend registry**):

An earlier draft introduced a `Backend` dataclass + `StorageDriver` Protocol + `backend.py`
registry as the single dispatch point. Dropped before implementation: at two kinds, where the
postgres kind derives its directory (and later its locks) from the already-open DB, a recipe
object is more machinery than the variation it encodes. The chosen shape is maximally explicit —
each entry point branches **once** on `entry.kind` into a per-kind function that reads
top-to-bottom. Shared helpers are extracted *later* from real duplication, after the slice is
green everywhere, not designed up front.

**Kind** — closed discriminator (`runcore/env.py`):

```python
class EnvironmentKind(StrEnum):
    LOCAL = "local"        # sqlite + unix-socket directory + file locks
    POSTGRES = "postgres"  # postgres + polling directory + advisory locks
    # DISTRIBUTED = "distributed"  # later: postgres + redis directory/locks
```

**Entry = pure locator** — only what is needed to *reach and open* the config store; flat, no
per-kind fields (kind-specific settings live in the config, readable only once the store is open):

```python
class EnvironmentEntry(BaseModel):
    id: str
    kind: EnvironmentKind = EnvironmentKind.LOCAL
    location: str | None = None        # sqlite path | postgres DSN
```

The registry key is `kind` — this also fixes the old round-trip mismatch where
`create_environment` wrote `driver` while `EnvironmentEntry` declared `kind`.

**Config = per-kind configuration, discriminated externally by `entry.kind`.** No `kind` and no
`id` field in the config — identity and discrimination live on the entry; the config is pure
behaviour. Kind-specific settings (`root_dir`, later `redis`) live here because they are used
only *after* the store is open — configuration, not locator:

```python
class _EnvironmentConfigBase(BaseModel):
    output: OutputConfig = Field(default_factory=OutputConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    plugins: dict[str, dict] = Field(default_factory=dict)

class LocalEnvironmentConfig(_EnvironmentConfigBase):
    root_dir: Path | None = None       # unix socket/lock root — local only

class PostgresEnvironmentConfig(_EnvironmentConfigBase):
    pass                               # + future postgres behaviour config

EnvironmentConfig = LocalEnvironmentConfig | PostgresEnvironmentConfig       # annotation alias (no Discriminator)
```

**Data tables, not dispatch objects (`runcore/env.py`).** Environment management
(create/delete/exists/ensure) needs the storage driver without building components; config
editing (`load_env_config`/`save_env_config`) needs the config class. Both are two-entry
mappings keyed by kind — data consulted by different operations, not a second dispatch inside
the connect flow:

```python
def _db_module(kind: EnvironmentKind):     # lazy imports keep psycopg optional
    match kind:
        case EnvironmentKind.LOCAL:
            from runtools.runcore.db import sqlite
            return sqlite
        case EnvironmentKind.POSTGRES:
            from runtools.runcore.db import postgres
            return postgres

_CONFIG_TYPES = {EnvironmentKind.LOCAL: LocalEnvironmentConfig,
                 EnvironmentKind.POSTGRES: PostgresEnvironmentConfig}
```

**Flow — one branch per entry point; each kind's function is linear:**

```python
# runcore/connector.py — consumer side
def connect(env_ref=None):
    entry = resolve_env_ref(env_ref)
    ensure_environment(entry)
    match entry.kind:
        case EnvironmentKind.LOCAL:    return _connect_local(entry)
        case EnvironmentKind.POSTGRES: return _connect_postgres(entry)

def _connect_local(entry):
    from runtools.runcore.db import sqlite
    from runtools.runcore.transport import unix_socket
    # exists-guard: opening a missing sqlite file would silently provision a fresh schema
    if not sqlite.exists(entry):
        raise EnvironmentNotFoundError(f"Database for environment '{entry.id}' not found", {entry.id})
    env_db = sqlite.create(entry)
    env_db.open()
    try:
        config = LocalEnvironmentConfig.model_validate(env_db.load_config(entry.id))
        backends = output.create_backends(entry.id, config.output.storages)    # cheap first
        directory = unix_socket.create_instance_directory(entry.id, config.root_dir)
        return compose(entry.id, env_db, directory, backends)
    except BaseException:
        env_db.close()
        raise

# _connect_postgres: same shape with postgres.create(entry) and NO exists pre-check — postgres
# open() is validate-only (never DDL) and raises the more precise
# EnvironmentStoreNotProvisionedError; directory = PollingInstanceDirectory(env_db,
# lambda run: SnapshotJobInstanceProxy(run, env_db)) — proxy capabilities (command channel via
# the SignalSender facet) are composition-site wiring, not the directory's.
# runjob/node.py connect: same match; each kind's function composes the node inline
# (LOCAL: unix transport pair + file locks; POSTGRES: polling directory + advisory locks
# + PostgresInstanceAccessPoint, the signal reconciler).
```

Each branch names its driver directly — no entry→db helper on the connect path (an earlier
version routed through kind-dispatched `_create_env_db`, re-running inside the branch the
dispatch the branch had just decided; the shared exists-guard also masked postgres's more
precise not-provisioned error). `_create_env_db` stays env-private for the generic config
functions (`load_env_config`/`save_env_config`), which do need runtime dispatch.

The open+load boilerplate was deliberately duplicated across the per-kind functions at first
(four sites, ~6 lines each) so the first landing introduced zero new abstractions; once all
repos were green it was extracted as `env.open_configured_db(env_db, env_id,
config_type)` — a context manager (close-on-failure covers the branch's whole composition, not
just the load; the store stays open on success). It takes the created db, not the entry: an
entry-taking helper would re-run inside the branch the kind dispatch the branch had just
decided — the same flaw that killed `_create_env_db` on this path.

**Deleted:** `load_database_module` (env.py's `_db_module` table replaces it), `TransportType`,
the transport-config classes, the `TransportConfig` union, `EnvironmentConfig.transport`,
`EnvironmentConfig.id` (callers hold the entry), `default_local()` (the default local config is
just `LocalEnvironmentConfig()`), and the db backends' `load_config` id injection. The built-in
local entry is `EnvironmentEntry(id="local", kind=LOCAL)`.

**Settled decisions:**
- `in_process` stays a direct constructor (`node.in_process(...)`), *not* a registry kind; its
  `node._create` branch is removed (registry kinds are LOCAL/POSTGRES).
- `postgres` kind shipped running and observing jobs first; remote control (`stop`/phase ops)
  was deferred to signals-as-state — since implemented (point 5), the receiving end being the
  signal reconciler.
- `kind` lives once, on the entry; the config carries neither `kind` nor `id` (external
  discrimination) — no `entry == config` invariant.
- `root_dir` (and later `redis`) are configuration (used post-open), not locator — on the per-kind
  config, not the entry.
- Unix directory/access-point factories drop the `transport_config` param, taking `root_dir`
  directly: `create_instance_directory(env_id, root_dir=None)`.
- Cheap-first construction is preserved *inside each per-kind function* (output backends before
  the directory, which allocates a component dir + flock).
- Three-repo change: runcore, runjob, and taro move together (taro's `cmd/env.py` constructs
  entries/configs and reads `transport` directly), plus a one-line runcli touch (`run_env`
  header used `config.id`); edit + test all, then commit per repo.

## The seams

### `InstanceDirectory` (consumer side)

The consumer's directory of live instances in an env. One per connector. Owns
the wire resources and the env-wide event hub. Proxies it returns borrow its
resources — closing the directory invalidates them.

```python
# runtools/runcore/transport/__init__.py
class InstanceDirectory(Protocol):

    def get_instances(self, run_match=None) -> list[JobInstance]:
        """One discovery sweep. Returned proxies carry their discovery snapshot."""

    def get_instance(self, instance_id: InstanceID) -> Optional[JobInstance]:
        """Single lookup; None if not found."""

    @property
    def notifications(self) -> InstanceNotifications:
        """Env-wide typed event hub. Single wire subscription, owned by the directory."""

    def open(self) -> None:
        """Start the event receiver in buffering mode; discovery replays buffered events."""

    def close(self) -> None: ...
```

### Per-transport proxies (not a Protocol)

Each transport implements its own `JobInstance` proxy, module-private
(e.g. `transport/unix_socket.py` → `_UnixSocketInstance`). The public type is
`JobInstance`. This is deliberate:

- The flat-RPC seam it replaces forced every operation through call-and-reply
  — itself a transport assumption. A Redis proxy may serve `snap()` from a
  broker-held key and `output.tail()` from a stream with no node round-trip.
- Routing (socket path, channel name) is private proxy state, not API surface.

Shared transport-neutral mechanics (event-driven state cache, unbind-on-ENDED,
`PhaseControlProxy`) live as optional helpers in `runcore.proxy`. Transports
may reuse or ignore them. One-way rule: transports import core modules
(`job`, `listening`, `proxy`), never `connector`.

### `InstanceAccessPoint` (producer side)

Pure exposure seam — coordination locks were extracted to `LockProvider` (below), so the
access point carries only wire concerns:

```python
# runtools/runjob/transport/__init__.py
class InstanceAccessPoint(Protocol):
    def start(self) -> None: ...
    def register_instance(self, job_instance: JobInstance) -> None: ...
    def unregister_instance(self, job_instance: JobInstance) -> None: ...
    def close(self) -> None: ...
```

### `LockProvider` (node component)

Named exclusive locks for job coordination, a node component independent of the access
point (`runcore/util/lock.py`). One method — `lock(lock_id) -> Lock` — with three
guarantees: env-scoped ids (provider constructed per env; same id + same env = same lock
across all nodes, different envs never contend), arbitrary string ids (encoding to the
medium is the implementation's job), and crash release (holder death frees the lock —
flock via the kernel, advisory via session end). Implementations: `MemoryLockProvider`
(in_process), `FileLockProvider` (local kind, env-scoped lock dir), and
`AdvisoryLockProvider` (`runcore/db/postgres.py` — session-level `pg_advisory_lock`,
one dedicated connection per held lock so the session is the lock token and release =
close, env-namespaced 64-bit key from a stable hash, server-side `lock_timeout` →
`LockAcquireTimeoutError`). What remains of the producer slice: composing
`node._connect_postgres` (via `postgres.create_lock_provider(entry)`). Design point 6
moves both in-house lock clients to **no-wait claims held for the protected
operation's duration**; the `Lock` contract grows a no-wait acquire accordingly.

## Design points

Point 2 is designed; points 1 and 3 still have open details to settle before
the sweep.

### 1. Split the `JobInstance` contract

`JobInstance` becomes the wire contract, and today it doesn't qualify: the
current proxy stubs `run()` with `pass` and raises on `tracking` — a Liskov
violation the redesign would make load-bearing. Split:

- **`JobInstance`** (consumer contract; proxies implement it fully):
  `metadata`, `id`, `snap()`, `output`, `stop()`, `find_phase_control()`,
  `notifications`.
- **Producer-side extension** (name TBD — `RunnableInstance`?): adds `run()`
  and `tracking`. Declared in `runcore.job`, implemented in `runjob`.

Open: extension name; whether `tracking` later moves to the consumer surface
(remote tracking support).

### 2. Event ownership and startup consistency — designed

The directory owns the single wire subscription and the typed hub. The connector
re-exposes `directory.notifications`. Proxies bind per-instance filters to the
hub internally.

Discovery uses buffered startup to avoid missing terminal events:

1. `open()` starts the transport event receiver, but buffers incoming events
   instead of dispatching them.
2. The first discovery sweep fetches current instances and creates proxies from
   those snapshots.
3. Proxies bind their per-instance filters to `directory.notifications`.
4. The directory replays buffered events in receive order, then switches to live
   dispatch.

This preserves one wire subscription per connector and closes the important
race: an `ENDED` event received between discovery and proxy binding is replayed
after the proxy exists.

**Staleness guard.** Replay can deliver events that *predate* the discovery
snapshot — applying them unguarded would regress proxy state. Guard with
`JobRun.last_updated` (computed property: max timestamp across lifecycle,
status, and the phase tree — no schema change) encapsulated in one comparison
site, `candidate.is_newer_than(current)`:

- apply on `candidate.last_updated >= current.last_updated` — `>=`, not `>`:
  rapid updates share clock resolution and ties must apply;
- never replace an ended state with a non-ended one (terminal guard,
  belt-and-braces over clock weirdness and unbind-in-flight replay).

Long-term: a per-instance monotonic `revision` on the wire — trivial since the
owning node is the single writer (it's `OutputLine.ordinal` for state). One
comparison site means that swap touches nothing else.

### 3. Proxy state: updated by events, never refreshed on read

**Decision.** Proxy mutable state is push-maintained: events mutate the local
cache; `snap()` returns it with no I/O. The alternative — each read of mutable
state sends a refresh request — is rejected:

- Bulk reads must cost one discovery sweep; refresh-on-read turns a dashboard
  listing N instances into N round-trips. (Proxies are created from the sweep's
  snapshots, which also removes the current double-fetch in
  `JobInstanceProxy.__init__`.)
- Pull buys no real consistency — state can change the instant after the
  response; a pulled read is a fresher-by-milliseconds snapshot at round-trip
  cost.
- Events already carry full `JobRun` payloads; pull re-fetches what the wire
  just delivered.
- Failure mode: pull turns transient network errors into read errors; push
  degrades to stale-but-available (buffered replay covers missed `ENDED`).
- Brokered transports: pub/sub (Redis) and LISTEN/NOTIFY (Postgres) make push
  the *native* primitive, while request-reply over a broker is an emulated
  contraption (reply channels, correlation IDs, timeouts). Push reserves
  round-trips for genuine commands (`stop`, phase ops); a Redis transport can
  later serve `snap()` from broker-held state — the node contacted only to
  act, never to ask.

Reads are therefore eventually consistent. The connector's `get_active_runs()`
becomes snapshots of swept proxies.

**Output: two sources, never merged.** Output events (`InstanceOutputEvent`)
feed a bounded per-proxy buffer (100 lines — the system's conventional read
size; the node's 2 MB tail buffer remains the deep store). One rule decides
every read: a TAIL read for `max_lines` within the buffered count is served
locally (the event window always ends at the live head, so its last N *are*
the stream's last N); every other read — HEAD, `max_lines=0`, or deeper than
buffered — is forwarded to the node with `mode` + `max_lines` and the result
returned directly, **never merged into the buffer**. Discovery responses
stay `JobRun`-only.

Consciously dropped: merging fetched lines into the buffer, ordinal
bookkeeping, and bootstrap-once state. An earlier merge-based design
produced four review findings (capped deep reads, wrong HEAD slices, frozen
post-bootstrap horizon, mid-fetch eviction gaps) — all symptoms of caching
with an ad-hoc coherence story. Two isolated sources make those states
unrepresentable. Accepted costs: uncovered reads pay one RPC per call
(exactly the old per-call semantics, never worse), and locally served lines
are the wire-event versions (possibly truncated per `truncate_length`) —
full lines always come from an uncovered read.

`get_output_tail` carries only `max_lines` on the wire (`0` = all retained).
The `Mode` enum (HEAD/TAIL) was removed from the whole tail chain — HEAD
never delivered its implied meaning on any live path (a bounded buffer's
"head" is the oldest *retained* line, not the start of job output; true
head-of-output belongs to the complete stored-output read, a different API),
and no production caller used it. One contract everywhere: newest retained
lines, oldest first. Known gap, shared server idiom: an invalid enum string
on the wire (`StopReason[...]`) surfaces as an internal error rather than
invalid-params.

Open: shape of the opt-in escape hatch for confirmed-current reads
(`refresh()` / `snap(fresh=True)` — never the default path); how discovery
partial failure surfaces (today: log and return the reachable subset).

### 4. Persistence and DB-backed live state

**Agreed direction (phased, see below).** The node's memory is authoritative;
it flows out on three channels:

```
node memory
  -> events               event-driven backends: proxies / UI
  -> coalesced DB writes  DB-backed backends: active-state snapshots + history
  -> output backends      output history (own path — never the instance-state writer)
```

**The writer — `RunStatePersister`, node-owned and transport-neutral**
(`runcore/db/persister.py`). Persistence and live-state sync are separate
internal units, but user-facing backend kinds choose the supported combination.
The persister attaches per instance — the node calls
`attach(job_instance.notifications)` in `create_instance` (before `CREATED`, so
the first flush completes the `init_run` row) and `detach(...)` when the
instance detaches — and a single node-level flush thread
(`PERSIST_FLUSH_INTERVAL`) drives writes. Interval: **250ms** (implemented),
matched to the PostgreSQL backend's active poll.

**Implemented model — one coalesced lane, not immediate-vs-debounce.** The
original split (immediate writes for lifecycle/phase, debounced writes for
status) was dropped for a single buffered model — simpler and free of the
older-clobbers-newer race:

- Every lifecycle / phase / status event records the *latest* snapshot per
  instance in a dirty map (coalesced; last-write-wins). Single writer per
  instance, so newest always wins.
- An **ended** snapshot drops the instance from the dirty map and is **never
  written by the persister** — the authoritative terminal write stays with the
  node's `_finalize_run` (runs after the output router closes, so it captures
  final output locations).
- `flush()` (called on the timer, single-threaded by contract) writes the
  coalesced snapshots via `store_active_runs(*runs)`, **propagates** DB errors
  after retaining the unwritten snapshots (retry next tick), and clears only
  entries untouched since the copy. `close()` does a best-effort final flush
  (logs + swallows — a failed last write can't be retried).
- Two write methods on `RunStorage`: `store_runs` (authoritative, terminal
  included) and `store_active_runs` (the persister's path, guarded by
  `WHERE ended IS NULL` so a stale active snapshot can never resurrect a row
  that ended during the unlocked write).
- **`state_updated_at` column + newer-wins guard (implemented).** The active
  write is
  `WHERE ended IS NULL AND (state_updated_at IS NULL OR state_updated_at <= :incoming)`,
  with every write storing `state_updated_at = JobRun.last_updated`. This turns
  single-writer-per-instance from an *assumption* into a DB-enforced invariant
  (robust to retries, bugs, future multi-writer); under the current single
  writer it should never reject, so a rejected active write (rowcount 0) is an
  anomaly worth logging. The terminal `store_runs` sets `state_updated_at` but
  stays **unconditional** (single authoritative write; guarding it risks
  rejecting the snapshot that carries final output locations). Same column +
  guard on both backends (parity); `SCHEMA_VERSION` bumped with it. (SQLite
  stores it at full-microsecond precision so sub-ms-distinct snapshots can't
  collapse past the `<=` guard.)

Timestamps:

- **domain time** — `JobRun.last_updated` (computed from the phase tree +
  status), when the run's *state* changed. The in-process staleness guard
  (`is_newer_than`) uses it; persisted as `state_updated_at` (below).
- **`updated_at`** (column, `NOT NULL`, **implemented**) — when this *row* was
  last *written* (wall clock). Set at `init_run` and on every snapshot write.
  Distinguishes "persistence stalled" from "nothing happened." **Write-time, not
  state-time — never use it as the apply/newer-wins comparator** (a later-written
  row can carry older state under reorder/multi-writer).
- **`state_updated_at`** (column, **implemented**) — the persisted
  `JobRun.last_updated`: *domain* freshness. The apply/newer-wins comparator (the
  active-write guard above). Note: the poll receiver's change-detection cursor is
  the write-time `updated_at`, not this — `state_updated_at` is the *apply* guard,
  `updated_at` is the *re-read* cursor (two roles, see the poll section).
- **`heartbeat_at`** (column, **implemented**) — liveness attestation, touched by the
  node on a slow cadence (below). Written in the *storage's* clock (postgres:
  `now()`), and read back only as a server-computed **age** on the version scan —
  one clock measures both edges, so node/consumer skew cancels and consumers never
  parse or compare liveness timestamps. Never a change-detection cursor and never
  bumps `updated_at` (a heartbeat must not trigger consumer deep reads).

Calibration: coalescing makes writes/sec ≈ active-changing-instances × (1/flush)
— at the 250ms flush, ~400/s for a 100-instance env (still well inside a small
Postgres or SQLite-WAL budget; the limiter at scale is MVCC/autovacuum churn on
the hot rows, not raw TPS). Uncoalesced status (10–100 ev/s/instance) is the rate
that breaks small DBs; that is why coalescing is a rule, not an optimisation.

**PostgreSQL read side: poll, not event replay.** The PostgreSQL backend does
not subscribe to lifecycle/phase/status events. On open and every poll tick, the
directory reads a cheap active-run version cursor, deep-reads only changed rows,
updates `SnapshotJobInstanceProxy` objects under `is_newer_than`, and evicts ids
absent from the complete active scan. A `get_instance` miss means the instance
is not in the current live view — no read-path fault-in. The poll is the
reconciler; the detailed algorithm is below in "PostgreSQL backend live state."

**Event-driven read side: subscribe → discover → replay.** Unix-socket-style
directories still use the event-driven startup invariant from point 2: start the
receiver in buffering mode, discover active instances once, admit proxies, then
flush buffered events under the staleness guard. Membership is push-maintained:
events admit unknown instances, update proxies, and remove/tombstone on `ENDED`.
That model deliberately has a liveness gap until heartbeat/reconciliation lands:
an instance whose node dies without emitting `ENDED` can remain in the view.

**Liveness (implemented).** `heartbeat_at` per non-ended run, touched by the node
on the node's flush thread every `HEARTBEAT_INTERVAL` (15s; retry-fast on the
250ms tick after a failed touch) — deliberately the *same thread* as the flush,
so the heartbeat attests exactly the lane consumers depend on (a wedged flush
thread reads as lost, never alive-but-frozen). The version scan returns a
server-computed `heartbeat_age`; `PollingInstanceDirectory` marks a proxy lost
past 3× the interval (logged once on the transition), and
`SnapshotJobInstanceProxy` exposes `heartbeat_age`/`is_lost`. Readers
*interpret, never mutate*: a lost run stays in the view, visible and
attributable — writing `ABANDONED` would break single-writer-per-instance and
race across connectors; automatic reaping was later considered and **rejected**
(point 8) — resolving a lost run is a deliberate operator act, never a clock's;
surfacing the lost verdict in taro is queued in Remaining work. This
is a capability RPC discovery structurally lacks (dead socket = invisible
instance). Per-node heartbeat (one row per node + writer-id column) is the
measured optimisation if envs grow large.

**The discovery seam: `InstanceDiscovery`.** Event-driven directories never
think in storage — they think in *initial snapshots + live events*. Discovery is
a one-method, transport-chosen strategy (service-discovery analogy: broadcast ≈
mDNS, DB ≈ registry query, Redis ≈ presence):

```python
class InstanceDiscovery(Protocol):
    def discover_active_runs(self, run_match=None) -> DiscoveredRuns: ...
```

Returns `DiscoveredRuns(runs, complete)` — `complete` flags whether the sweep
covered every source (an incomplete enumeration must not be read as "stopped").

Implementations: `UnixSocketInstanceDiscovery` (wraps the existing broadcast —
implicitly live, blind to dead nodes, no persistence dependency; `complete`
False if any node fails to answer); **`DbInstanceDiscovery(db)` — implemented**
(`runcore/transport/db.py`): wraps `RunStorage.read_active_runs`, returns
`complete=True` (a DB sweep has one authoritative source), lags by the flush
interval. `read_active_runs` selects `ended IS NULL AND root_phase IS NOT NULL`
(active rows with a real snapshot, skipping init-only) and post-filters with the
full `run_match`. In the PostgreSQL backend this one-shot discovery seam is not
the steady-state live lane; `PollingInstanceDirectory` owns the repeated
version-scan/deep-read/evict loop. Future `RedisInstanceDiscovery` (presence
keys). The earlier `RunReader`/`RunQuery` ISP split was dropped as unnecessary —
`read_active_runs` on `RunStorage` is sufficient. Heartbeat translation is
deferred with the liveness track.

**Phasing — independent tracks.** Directory ships with broadcast discovery (a
permanent per-transport choice behind the seam). The persistence write+read
path now ships dark and complete: `RunStatePersister` (per-instance attach +
flush thread), the `updated_at` + `state_updated_at` columns, `store_active_runs`,
`read_active_runs`, `active_run_versions()`, and `DbInstanceDiscovery` — across
both storage backends (SQLite, PostgreSQL). The poll directory is built too:
`PollingInstanceDirectory` (version scan + deep-read-changed + evict-absent) +
`SnapshotJobInstanceProxy` + snapshot-diff synthesis. What remained was *selecting*
it as a backend kind and the heartbeat track — both since done (the `postgres`
kind composes it; heartbeats ride the flush thread). The poll
subsumes the reconciler — it *is* the periodic complete sweep.

**PostgreSQL backend live state — poll + snapshot-diff (default).** The
PostgreSQL backend keeps the `runs` table as the authoritative active-state
snapshot and syncs the directory by **polling**, not by pushing events. State is
already coalesced at the persister flush, and remote consumers need current
*state*, not every transition — so a cheap periodic poll is the whole state
lane: it is discovery, refresh, and the reconciler in one read path.

Producer-side: unchanged. `RunStatePersister` coalesces lifecycle/phase/status
into the latest active `JobRun` and flushes to `runs` (bumping `updated_at`);
terminal writes stay with `_finalize_run`. No event dispatch, no trigger needed
for the default.

Consumer-side — one operation per tick:

1. **Cheap version scan.** `SELECT job_id, run_id, ordinal, updated_at FROM runs
   WHERE ended IS NULL AND root_phase IS NOT NULL` — ids + freshness, no blobs,
   no deserialization (a new `active_run_versions()` on `RunStorage`).
2. **Deep-read only changed.** Compare each `updated_at` to the directory's
   `{id: updated_at}` cursor; `read_active_runs(match_ids(changed))` for the
   ones that moved.
3. **Apply + synthesize.** `SnapshotJobInstanceProxy.update_from_snapshot(run)`
   applies under `is_newer_than` (domain `last_updated`) and diffs old→new to emit
   lifecycle/phase/status callbacks — reconstructable from the snapshot's stage
   timestamps (`created_at`/`started_at`/`terminated_at`); status is coalesced by
   design. CREATED is state, not a phase transition, so it yields only the root
   lifecycle event, no phase event.
4. **Evict absent.** Ids in the directory but not in the scan ended or were
   removed → emit + drop. The scan is a *complete* enumeration, so eviction
   (incl. rows deleted by `remove_history_runs`) is free — no DELETE trigger,
   no tombstones.

```python
# implemented as PollingInstanceDirectory.reconcile()
def reconcile():
    versions = dict(db.active_run_versions())              # cheap: ids + updated_at cursor, no blobs
    changed = [iid for iid, cur in versions.items() if seen.get(iid) != cur]
    for run in db.read_active_runs(instances_match(changed)):   # deep-read ONLY changed
        _apply(run)                                        # admit new, else update_from_snapshot (is_newer_than + synth)
    for iid in seen.keys() - versions.keys():
        _evict_absent(iid)                                 # terminal → emit ENDED then drop; else drop
    seen = versions
```

Two timestamp roles, kept distinct: **`updated_at`** (column, write-time,
single-writer-monotonic *per instance*) is the cheap **change-detection cursor**
— "re-read?"; **`is_newer_than`** (domain `last_updated`) is the **apply guard**
— "apply?". Never use `updated_at` as the apply comparison (clock mismatch).

Why poll over trigger/`NOTIFY`: granularity is bounded by the flush anyway,
remote consumers need state not transitions, and the poll unifies steady-state
sync + reconciler into one path — no lost-notify gap, no per-instance dirty-hint
state machine, no `NOTIFY` payload limit, no trigger DDL, no watermark/skew.

Intervals:

- **Active poll: 250ms** (while ≥1 active run) — cheap version scans; deep-read
  only on change.
- **Idle poll: 2s** (no active runs) — enough to notice a new run appearing.
- **Flush (`PERSIST_FLUSH_INTERVAL`): 250ms** — matched to the active poll so
  granularity and detection align. Coalesced ⇒ ≈ active-changing-instances × 4
  writes/s (hundreds/s at job-runner scale — far inside Postgres's budget; the
  limiter is MVCC/autovacuum churn, not raw TPS, and we are nowhere near it).
- **Time-derived display values** (elapsed, countdowns) are computed locally at
  the UI repaint rate, never from the poll.

Output remains separate (own transport/storage path, not reconstructed from
`runs` — live tail rides its own bounded table, point 7); commands ride the
signals mailbox (point 5) — a separate lane from observation by design.

**Escalation path (not built — only if measured scale demands it).** A
payload-less `NOTIFY` **doorbell**, fired by the producer on write, lets the
receiver poll immediately — cutting detection lag below the interval without
faster polling, while the poll stays the durability floor (a lost doorbell just
waits for the next tick). At very large scale or with many idle watchers, a
trigger emitting a minimal dirty hint `(job_id, run_id, ordinal, updated_at)`
gated on `root_phase IS NOT NULL AND OLD IS DISTINCT FROM NEW` is silent when
idle and reads only changed ids. Both were considered and deferred: full
`JobRun` payloads through `NOTIFY` are unsafe (payload limit), and a trigger sees
only `OLD`/`NEW`, not domain events. The poll model is the default because it is
simpler and adequate; the doorbell/trigger earn their machinery only when load
or latency is shown to require them.

**Design affordance — keep the doorbell a drop-in.** Build the poll as an
on-demand `reconcile()` callable that the timer invokes, not logic buried in a
timer loop. Then `NOTIFY/LISTEN` is a pure add-on: "also call `reconcile()` on a
doorbell." The `(id, updated_at)` cursor + `is_newer_than` make a
doorbell-triggered poll idempotent (an early `reconcile()` is just a normal one,
can only advance timing). Keep the directory's listen/connection handling
separable so a `LISTEN` connection bolts on without touching the proxy/diff/apply
path (all downstream of `reconcile()`). Designing to this now means the latency
upgrade later is additive, not a rewrite.

**Storage backends.** `EnvironmentDatabase` has two implementations: `SQLite`
(in-process/dev, one connection guarded by a process lock) and `PostgreSQL`
(networked/concurrent, `psycopg_pool` connection pool, native `JSONB` +
`TIMESTAMPTZ`, lets the DB serialize writers).

**Storage model — document + projections (implemented).** The `runs` table
stores each run as **one whole document** (`run` column = `JobRun.serialize()`
— the wire format *is* the storage format; `NULL` marks an init-only row,
which is also the materialized-row check). The discrete columns are strictly
**query projections** derived from the document at write (`created`,
`started`, `ended`, `exec_time`, `termination_status`, `warnings` — criteria
SQL, sorts, stats; plus the `run_tags` junction) and the persistence
machinery's own timestamps (`updated_at`, `state_updated_at`,
`heartbeat_at`). A JobRun is never reassembled from projections. Rationale:
rows were already written whole on every store and criteria SQL is by design
a superset *prefilter* (Python re-applies the full match), so the table
behaved as a document store with indexes — the schema now admits it, new
`JobRun` fields cost zero storage changes, and the two serializations of one
aggregate (wire vs column-pieces) collapsed into one. Init-only rows carry
identity + tags only; params/features become readable once the first
snapshot lands. The criteria→SQL translation and
the read-path helpers are shared in `runcore/db/sql.py` (`build_where_clause`
returning `(where, params, complete)`, `matching_pks`, `last_run_ids`,
`build_order_by`, `build_job_stats`); each backend supplies only a small
`Dialect` (`placeholder`, `bind_dt`). Key contract: the SQL clause is a
*superset* prefilter (only EXACT id/tag + lifecycle are expressed; PARTIAL/
FN_MATCH/phase are not), so the full `run_match` is re-applied in Python on every
read — the `complete` flag tells write/aggregate paths (`remove_runs`,
`read_run_stats`) when the prefilter is authoritative and a post-filter can be
skipped. Postgres tests run against a throwaway container via testcontainers.

### 5. Commands are signals: desired state, not calls

**Agreed direction.** Remote control ops — `stop` and phase ops alike —
stop being RPC calls and become consumer-written *signal records* the owning
node reconciles against — the Kubernetes desired-state model. What RPC
cannot give: **durability** (an `approve` survives node restarts and
consumer disconnects) and a free audit trail (who signalled, when).

Rules:

- **Separate signal store, consumer-written** — never the node's run row
  (preserves single-writer-per-instance). Keyed by instance/phase/op.
- **State is truth, event is doorbell.** Write the signal, nudge over the
  transport; the node checks on nudge, coarse poll (~seconds) as the floor.
  The first slice runs the coarse poll alone; the payload-less `NOTIFY`
  doorbell is the same additive escalation as the state lane's (point 4) —
  "zero steady-state polling" is the doorbell end state, not the entry bar.
- **Remote phase ops return `None` and are idempotent.** All remaining ops
  (`approve`, `resume`, and eventually `stop`) comply — they set an event and
  return nothing; re-delivery is harmless. Outcome is observed via phase
  events. (`signal_dispatch` — the one op with a return value, which would
  have forced request/response emulation over signals — is eliminated by the
  claim-then-act queue, design point 6. This rule is an invariant, not an
  aspiration.)
- **Queries never cross the wire — `control_api` is commands-only.** The
  decorator rejects properties (`TypeError` at class definition); readable
  state — local or remote — comes from `snap()`. Export to phase `variables`
  anything a consumer is missing. (Query properties like `approved` existed
  briefly as "local-only"; they were removed once the control surface became
  the recording apply point — reads are not part of the command channel.)

End-state across the transport: discovery via DB (point 4), state via
events, output deep-reads as the lone fetch, commands as signals — **the
node accepts no calls**; its access point reduces to a doorbell receiver.
Accepted cost: command latency goes from RPC-RTT to write+nudge (tens of
ms) — irrelevant at approve/resume/stop pacing.

**Implementation plan (implemented as designed).**

**One envelope for all remote control — `stop` is the degenerate phase-less
op.** Signals reuse the shape the unix RPC already speaks
(`_exec_phase_op(phase_id, op_name, *args)`); no per-op signal kinds:

```text
signals table (env db, consumer-written mailbox):
  job_id | run_id | ordinal      -- target instance
  phase_id                       -- NULL for instance-level ops (stop)
  op | args (JSON)               -- any control_api op; args JSON-serializable
  requested_at                   -- row timestamp; the orphan sweep's age bound
```

Because the envelope is generic, every `None`-returning idempotent
`control_api` op — user-defined phases included — becomes remotely invocable
with zero op-specific code. Queries stay local per the rule above.

**Lifecycle — row = in-flight command; run = permanent record; logs =
anomalies:**

1. The consumer proxy (`stop()` / `_exec_phase_op`) inserts the row and
   returns — no reply lane exists or is needed.
2. The node's signal reconciler (`PostgresInstanceAccessPoint`) reads pending
   rows for its registered instances and decodes each envelope onto the
   instance's **public control surface** — instance ops → `instance.stop(...)`;
   phase ops → `find_phase_control_by_id(phase_id)` → the `control_api`
   method — exactly the call path a local caller or the unix RPC server uses.
   There is no separate command-dispatch API on the instance (an earlier
   `exec_control_op` was deleted): transports convert envelopes into direct
   calls; the envelope is plumbing, not a conceptual API.
3. The instance records the request into the run's own state at the control
   surface itself: `find_phase_control` returns a recording wrapper over the
   phase's raw `PhaseControl`, so **every** command path — local call, RPC,
   signal — records **`JobRun.control_requests`** (op, phase_id, args,
   applied_at) — node-written (single-writer preserved), flushed by the
   persister like any state, retained with the run's history. The record is
   appended after validation but *before* the op is invoked — an op that
   immediately finalizes the run (approve unblocking the last gate) would
   otherwise store a terminal snapshot missing the command, and late appends
   never reach a terminal row. Because
   control activity is *state*, polled consumers synthesize the control event
   from the snapshot diff — observability parity across kinds without a wire
   event. (`InstanceControlEvent` extends from stop-only to the generic
   (phase_id, op) payload, emitted at the same apply point — the live-lane
   nicety; the durable record is `control_requests`.)
4. The reconciler **deletes the applied row** — the applied op is already on
   the run record, so the row holds nothing
   unique. Un-appliable rows (unknown instance/phase, non-`control_api` op)
   are logged loudly (full row) and deleted; never-applied rows stay pending
   until an orphan sweep — visible for exactly as long as they mean something.
   Orphan = an aged row whose target run is gone *or whose owner stopped
   heartbeating* — a crashed node's runs stay non-ended forever, so lifecycle
   state alone would leak their pending signals indefinitely.
   If compliance-grade request retention is ever demanded, delete-on-apply
   flips to mark-processed + TTL additively (the read adds a WHERE clause).

**Why the run-side record** (not signal-row status flips, not wire events):
one authority owned by the single writer; synthesizable for polled kinds;
audit travels with the run into history; and it collapses consumption
semantics to delete-on-apply — the mailbox stays a mailbox.

Rejected along the way: message-only delivery (proxy → access point call
without the mailbox) — loses durability, the core motivation: a command for
an absent node must wait, not evaporate; on the DB kind the only carrier
would be `NOTIFY`, which drops when nobody listens.

### 6. Coordination: claim-then-act — the lock is the state

**Implemented (replaced the check-then-act protocol in `runjob/coord.py`).**

**The finding that forced this.** Coordination correctness does not need
*fresh* state — it needs the check and the occupancy marker to be **serialized
with the lock**: anything that won its check before I acquired the lock must be
visible to my check. The persister→poll pipeline is an eventually-consistent
replica and cannot provide that ordering *by construction* — no interval tuning
fixes it (a mutex with a detection window is not a mutex). Reading storage
directly does not fix it either: the occupancy marker itself (the mutex phase's
RUNNING stage) reaches the DB only at the next persister flush — the marker is
written on the async lane, so no synchronous read can see what was not written.
Check-then-act is therefore broken on the `postgres` kind (and any future
brokered kind), and only incidentally correct on unix sockets, whose live RPC
reads happen to be current.

**Two lanes, stated as doctrine:**

- **Observation lane** (persister → storage → poll; events): dashboards,
  monitoring, wake-ups, soft ordering. Eventual consistency is its *contract*,
  not a defect.
- **Coordination**: correctness decisions come only from **atomic primitives**
  (`LockProvider` claims). Coordination never reads the observation lane to
  *decide* — only to be polite (queue ordering below) or to decorate
  diagnostics.

**Decision — claim-then-act.** The atomic operation's success *is* the
decision; there is no view to be stale:

- **Mutex** (`MutualExclusionPhase`): no-wait claim of the group lock at phase
  start, **held for the protected child's whole run**, released after; a failed
  claim = OVERLAP. Deletes the check (env-wide query + phase search) entirely.
  Also fixes the pre-existing false-OVERLAP race (near-simultaneous starters
  could kill each other — a checker cannot distinguish *contending* from
  *occupying* when reading RUNNING stages; an atomic try-claim has no such
  ambiguity). Lost: the "overlapped with instance X" log detail — recoverable
  best-effort from the observation lane for the message only, never for the
  decision.
- **Queue** (`ExecutionQueue`): capacity becomes **N slot claims**
  (`eq-{group}#0..N-1`) — try-claim any, hold while the child runs, release
  after. Capacity is a hard invariant enforced with no shared view. Strict FIFO
  is **relaxed to seniority-staggered claims**: before claiming, count the
  visibly older IN_QUEUE instances of the group in the observation lane and
  delay the attempt proportionally (`rank × stagger interval`), so older
  waiters get first pick after each wake-up. Politeness, not protocol: a stale
  view can invert order but never capacity, and a ghost entry from a dead node
  merely adds one stagger step per attempt — no deadline or override machinery
  needed. (An earlier yield-with-deadline variant was replaced: its budget,
  anchored at queue entry, expired during saturation — exactly when fairness
  matters — and decline-based handoff had no channel to wake the older waiter.)
  Wake-ups stay event-driven (phase-ended events + rescan timeout) — a stale or
  late wake-up is just a failed try. Deletes `_dispatch_next` (the distributed
  dispatcher: env-wide query, sort, remote state parsing, RPC dispatch loop)
  and the cross-instance `signal_dispatch` control call; instances dispatch
  themselves. `IN_QUEUE`/`DISPATCHED` phase states remain for observability.

**Consequence for point 5.** `signal_dispatch()` was the only remote phase op
with a return value — the case that would have forced request/response
emulation over signals. After this redesign the whole remote control surface
(`approve`/`resume`/`stop`) is `None`-returning and idempotent, and the queue
no longer depends on remote phase control at all — it unblocks on the
`postgres` kind without waiting for signals-as-state.

**Crash story.** Claims release with their holder (flock: kernel; advisory:
session end) — coordination has **no dependency on heartbeat/liveness** and no
cleanup machinery. The rejected alternative — repairing check-then-act with
write-through occupancy under the lock plus poll-bypassing reads — needed a
fairness tiebreak on top and left a dead node's RUNNING marker blocking its
group until heartbeat lands: most machinery, worst crash story. Recorded so it
is not re-litigated.

**Long holds on Postgres — allowed, with care.** A session advisory lock on an
autocommit connection holds **no transaction**: no xmin pin, no vacuum/bloat
impact, no table locks — one idle backend plus one lock-table entry. The
pattern is production-precedented (Que/GoodJob hold an advisory lock per job
for its whole execution; leader election holds one per process lifetime). Care
items, owed to the connection rather than the server: TCP keepalives on lock
connections; the environment `location` must be a **direct DSN** (session
advisory locks are incompatible with transaction-mode poolers); release already
warns if the connection died mid-hold. Cost profile: one pinned connection per
*running* protected job; the consolidation path if it ever bites is one session
per node holding many keys, fronted by in-process per-key gates.

**Lock contract addition.** `Lock` grows a **no-wait acquire** (the deferred
try-lock — its consumer arrived; the advisory implementation already has the
zero-timeout path, file/memory pass the flag through). The blocking-with-timeout
acquire remains for the public `node.lock()` surface. Migration blast radius:
the two `coord.py` call sites are the only in-house lock clients.

### 7. Output lane: bounded tail through the DB — implemented

**The need, exactly.** `inst.output.tail(mode, max_lines)` on a *running*
instance from another machine. The surface already exists — `taro tail` and
the TUI output panel call it, and the local kind serves it (event-fed proxy
buffer + RPC `get_output_tail` reading the node's `InMemoryTailBuffer`). On
the `postgres` kind `SnapshotJobInstanceProxy._fetch_output_tail` raises —
there is no wire to the node and nothing readable elsewhere mid-run (S3 sinks
buffer-and-PUT at close; file sinks are node-local). No new user-facing API:
the work is giving that one hook something to read. History output is out of
scope — it already works via `output_locations` + backends after finalize.

**Decision.** The node's tail buffer, made shared: a bounded per-instance
output tail in the environment database. The db is this kind's transport —
signals proved the pattern for commands; this is the same move for output.
Full output continues to flow to the configured output backends (durable,
history path, unchanged); the db carries only a capped rolling tail per
*active* instance — a cache, not a second durable copy.

```text
output_tail table (env db, node-written, bounded per instance, UNLOGGED):
  job_id | run_id | ordinal      -- instance
  line_ordinal                   -- OutputLine.ordinal (already monotonic)
  line (JSON)                    -- serialized OutputLine
```

- **Per-line rows, not a tail document.** Appends are cheap (no whole-document
  rewrite — the runs-row lesson), `line_ordinal` gives incremental reads
  (`WHERE line_ordinal > ?`) for follow-style tailing, and pruning is a DELETE
  of everything below `max - cap`.
- **UNLOGGED (Postgres).** The schema itself declares the tail expendable:
  WAL for state (runs, signals, heartbeats), no WAL for cache. Append+prune
  churn skips the WAL stream, replication, and checkpoint pressure. Crash
  recovery truncates the table — it refills within seconds from ongoing
  appends of still-running instances, and the durable output was never here.
  Not replicated to standbys — irrelevant: this kind already requires a
  direct primary DSN (advisory locks). SQLite has no equivalent; the sqlite
  facet implementation simply ignores the concept (backend-internal asymmetry,
  like `now()` vs `julianday`) and exists for test parity — the local kind
  serves tail over RPC.
- **Producer side — always-on writes, owned by the access point.** The db
  access point subscribes its `OutputTailPublisher` to each registered
  instance's output notifications — the unix kind already publishes instance
  events through registration-time observer subscription; the db kind does
  the same for the output tail. The router and node never learn the tail
  exists (universal storage-lane machinery on the node; kind-specific
  transport-lane machinery in the access point bundle). Staged lines flush
  coalesced on the access point's poll tick; prune to the cap. Cap is in lines,
  configured under `PostgresEnvironmentConfig.output`; `tail_cap=0` disables
  publishing entirely — the per-env escape hatch for pathological producers
  (negative values are rejected at config load). Every running instance
  publishes its tail unconditionally, like the run-state persister: a
  request-triggered variant (write only while someone tails) was considered
  and rejected — it needs a full subscription/lease protocol (rows, renewal,
  expiry, node-side activation state) that dwarfs the writes it saves, adds
  1–2s first-read latency, and loses the crash-forensics property: with
  always-on, the last output lines of a *lost* run are already in the table
  when its node dies (the in-memory buffer that held them just evaporated).
  If fleet-scale cost ever demands it, lease-based activation is additive
  behind the same facet — readers never know the difference.
  Two write-cost guards: **one batched flush per node** per cadence (all its
  instances in one statement — txn rate scales with nodes, not instances),
  and **insert only the last `cap` lines of each batch** (anything beyond
  would be pruned immediately; this bounds per-instance write rate at
  cap × cadence regardless of output volume — outlier-safe by construction).
  Budget check: this lane is *lighter* than the run-state lane (tiny
  WAL-free rows vs whole logged JSONB rewrites at up to 4/s per instance);
  the live set is bounded (~cap × line size per instance), so vacuum churn
  can't compound.
- **Consumer side: pull-only, on demand.** `_fetch_output_tail` reads the
  facet. No output events on the polled kind — the directory never polls
  output (volume would dwarf the run-state version scan; cost lands only on
  instances someone actually tails). `_ProxyOutput`'s completeness logic
  already routes to the remote fetch when its event-fed buffer is empty, so
  the proxy base is untouched. Follow mode = repeated incremental fetches by
  `line_ordinal`; ~poll-interval latency, fine at human-watching pacing.
- **Wiring:** a narrow facet pair split like the signals one — write side for
  the node's sink, read side for proxies — and the proxy factory earns its
  keep as designed: the composition-site lambda gains the output reader; the
  directory is untouched.
- **Lifecycle:** rows removed by a *delayed* sweep after the run ends, not
  synchronously at `_finalize_run` — a follower polls incrementally, and
  delete-at-finalize would race away the lines between its last poll and the
  cleanup; a grace period covering a poll cycle closes that. A crashed
  owner's rows are orphans for the same age+heartbeat sweep family (another
  reaper datapoint).
- **Reader obligation:** tolerate `line_ordinal` gaps — the lower bound moves
  by design (prune), and an UNLOGGED truncate restarts mid-stream. Tail
  semantics already permit both. The cap itself is *amortized*: pruning
  triggers after ~cap appended lines, so a running instance may briefly
  retain up to ~2× cap rows — the bound governs churn and storage, not an
  exact read size (reads bound themselves via ``max_lines``).

**Accepted costs.** Bounded MVCC churn per active chatty instance (append +
prune — same budget argument as the run-state flush); the cap means live reads
cannot page unbounded scrollback of a running job — complete output arrives
with finalize via the backends.

**Rejected:**

- **Live reads from stored output (S3 batch upload).** Multipart parts are
  invisible until `CompleteMultipartUpload` — useless for live reads; the real
  variant is chunk objects (periodic small PUTs, compacted at close), which is
  a sink-lifecycle redesign (chunk naming/indexing, compaction, crash-orphaned
  chunks, list+merge readers) with 5–30s latency and a hard availability
  regression: it works only where a *shared* backend is configured, making
  live tail conditional on extra infrastructure the kind doesn't otherwise
  need.
- **NOTIFY-carried output** — payload caps, drops when nobody listens, no
  late joiners; NOTIFY stays escalation-only per point 4.
- **Direct node RPC for output** — breaks "the node accepts no calls";
  reintroduces addressing exactly where the kind removed it.

**Decoupled track — crash-durable output storage.** Rejected above as the
*tail* mechanism, durable incremental output writing is independently
valuable: close-time-only sinks mean a crashed node's stored output is lost
entirely today. Preferred candidate: a **postgres output storage type** —
one more entry in the existing sink/backend family (`output.storages`
config), not a new seam and *not combined with the tail* (the tail stays
the uniform live mechanism regardless of storage choice; the two meet only
as sinks on the same router). Fit profile: light-to-moderate output — the
output lives next to the run history (one store, one retention, SQL over
lines) and completes the kind's cross-machine *history* output story with
zero extra infrastructure; heavy-output estates keep S3 (per-env choice),
where chunked upload remains the durability option. A later read
optimization (serving tail from the durable table where pg storage is
configured) is possible but deliberately unplanned — S3/file envs need the
cache anyway. Own track, own slice — explicitly not a tail dependency.

### 8. Lost runs: classification, not reaping — decided

**The three-state model.** A stale heartbeat proves silence, not death. Consumers
distinguish:

```text
storage-active     = row non-terminal          (the active scan)
operationally-live = heartbeat fresh           (attestation)
lost               = active AND not live       (SnapshotJobInstanceProxy.is_lost)
```

**Decision: no automatic reaper.** A crashed owner's rows stay non-terminal;
consumers classify them as lost and treat them as not operationally active.
An automatic LOST-terminalizer was designed in full and rejected:

- **Silence is not death.** A partitioned or wedged node's job keeps producing
  real side effects and may finish with a true outcome; leaving the row
  non-terminal lets the recovered owner finalize *truthfully*. Auto-reaping at
  any threshold discards that recoverable truth permanently (first-writer-wins
  cuts both ways).
- **The sweeps don't need it.** Signals and output tails already converge via
  heartbeat attestation without terminalizing the run — liveness-based cleanup
  of *transport artifacts* (re-sendable commands, expendable cache rows) is a
  categorically smaller claim than fabricating a run's terminal history.
- **What remained on the reaper's side was cosmetic**: active-scan tidiness
  (solved by surfacing the classification) and stats inclusion (lost runs are
  genuinely unresolved; excluding them is honest).

**Principle:** *observation never mutates; resolving an unknown state is a
deliberate act* — an operator's, not a clock's.

**Deferred, designed: the operator claim (`taro env mark-lost`).** When an
operator resolves a lost run deliberately, the write must still be race-safe
against a recovering owner. The designed contract, kept for that day:

```python
def claim_lost_runs(stale_after: float) -> list[InstanceID]:
    # One atomic claim: staleness proven at write time on the storage clock
    # (candidate discovery cannot be trusted — the owner may attest between
    # read and write), ended IS NULL, run IS NOT NULL (init-only rows have no
    # document to terminalize). Transforms the stored document into a
    # structurally terminal root phase so eviction replays ENDED(LOST).
    # Implementation freedom: SELECT ... FOR UPDATE + Python-side stamp inside
    # one transaction beats SQL document surgery (single serializer).
```

- New `TerminationStatus.LOST` mapped to `Outcome.FAULT` (UNKNOWN stays
  reserved for unrecognized persisted codes); `terminated_at =
  LEAST(GREATEST(heartbeat_at, started), storage_now)` — the last
  provably-alive moment, floored against negative execution time and capped so
  the claim never writes a timestamp in the storage clock's future (`started`
  is node-clock domain data, so a skewed-ahead node would otherwise leak a
  future timestamp through the floor). A lost run's duration is inherently a
  cross-clock approximation bounded by skew — same reasoning that makes
  heartbeat *ages* server-computed.
- Prerequisite: **all terminal writes become conditional** (`ended IS NULL`) —
  today's normal finalizer is unconditional, so first-writer-wins does not yet
  hold in either direction. Required the day any second terminal writer exists.

## Kept decisions

- **Transport split & one-way dependency.** Connector/node are
  transport-agnostic; transport modules never import them. (Once violated,
  caused a cycle.)
- **One owner per resource; cheap-first construction.** The
  directory/access-point owns wire resources; connector/node owns those plus
  env-level state and closes env-level state first. Construct in-memory things
  before OS-resource holders — reversing this once leaked a component dir +
  flock.
- **Shared layout, single factory.** When both sides need a layout, one
  factory call builds it once and returns the matched pair. Stale-dir cleanup
  runs before new allocation, never after.
- **Instance-keyed surface.** No node concept and no transport addresses in
  any public type. Both were unix-socket leaks — brokered transports
  (Redis, Postgres) have neither.

## Naming

Resolved: **transport** (umbrella concept), **`InstanceDirectory`**,
**`InstanceAccessPoint`**; proxy classes module-private and typed as
`JobInstance`. Naming stakes dropped with the redesign — the proxies are the
public citizens and they already have a name.

Rejected along the way (keep this list — the candidates keep coming back):

- `Client`/`Server` and variants — wire-role lie under brokered transports;
  both sides are clients of a broker.
- `Agent` — names the node-level process, not its transport bundle; AI-agent
  collision in a tool that runs jobs.
- `Endpoint`, `Gateway`, `Bridge` — role-symmetric middle-infrastructure
  words; their true referent here is the broker. Messaging "bridge" means
  broker-to-broker link; "bridge" already names the event-decode handler in
  this codebase.
- `Exposure` / `Exporter` — security-finding flavour / Prometheus collision
  (`redis_exporter` is an existing tool).
- `InstanceRegistry` (consumer side) — a registry is named for registration,
  which happens on the producer side (`register_instance`); the consumer side
  is lookup-only. Also collides with the access-point impl's internal
  instance registry. Directory = consult, registry = register.
- `InstanceAccess` (producer side) — bare "access" names the capability,
  which belongs to the holder/consumer ("I have access"); the granting side
  needs the granting-side noun, which is exactly what the "Point" suffix
  adds. Also meant the consumer side in earlier design rounds — recycling it
  for the opposite role would poison old references.
- `InstanceAccess` + flat RPC bundle — superseded by this redesign; its
  resistance to naming was a symptom of one seam bundling discovery, RPC,
  events, and a routing cache.

## Remaining work

1. **`taro tail -f` on polled kinds.** Follow mode rides output events, which
   polled transports do not carry — it needs a taro-side incremental poll loop
   (client-side dedup by line ordinal; ``read_output_tail``'s ``after_ordinal``
   parameter is the reserved server-side optimization). One-shot `taro tail`
   works remotely today via the proxy's tail read. Decoupled from the lane:
   chunked/durable backend upload (crash-durable output; pg output storage is
   the preferred candidate — see point 7) is its own later track.
2. **Operator lost-run resolution + no-live-node cleanup.** `taro env
   mark-lost` implementing the deferred claim contract (point 8), plus the
   `taro env prune` extension deleting orphan signal/tail rows of
   ended-or-missing instances — the env-without-nodes gap stays an accepted
   operator-maintenance matter (operator commands may mutate; the observation
   lane never does).
3. Resolve the `JobInstance` contract split when it bites (open point 1;
   remote-run question).
4. Decide `read_instance_ids` (`RunStorage`): its production caller vanished
   when the orphan sweep switched to heartbeat attestation — keep as a generic
   identity-projection primitive or drop it.

Done since the last revision: liveness surfaced to consumers end-to-end (the
lost-run answer, point 8) — `InstanceLiveness` on the `JobInstance` surface
(default presumed-live; snapshot proxies carry the polling directory's
verdict); rendered everywhere actives appear: `taro ps`, the dashboard and
per-job TUI active tables, and the instance selector — `LOST <age>` badge in
the STATUS cell (which the badge qualifies — that status is exactly what's
stale) with the whole row dimmed via `lost_aware` column wrapping; text badge,
not colour-only, so it survives pipes and NO_COLOR; the verdict attaches at
paint time (`active_row`), read fresh from the proxy each repaint. The jobs
overview counts are lost-aware: lost instances never inflate "running" (RUNNING
cell shows them separately, e.g. `2 +1 lost`; header gains an `N lost` metric
when nonzero). Lost runs stay listed everywhere: visible and attributable.
Earlier: the control-surface redesign — `exec_control_op`
deleted as a conceptual API; recording moved into the instance-returned
`_RecordingPhaseControl` wrapper (record-before-invoke preserved), so local
calls, the unix RPC server, and the signal reconciler all record through the
same public call path (the former remaining-work item "route unix RPC through
the apply point" dissolved — the RPC server already resolved controls via the
instance); `control_api` is commands-only (properties rejected, the query
properties removed); requester audit (`requested_by`/`requested_at` on
`ControlRequest` and the signals row's `requested_by`) dropped for now — no
consumer reads it; re-adding is additive (document storage absorbs new
`ControlRequest` fields; a transport-set ambient context can carry identity).
Earlier: signals-as-state (point 5) — the reconciler in
`PostgresInstanceAccessPoint` (coarse poll + orphan sweep), consumer proxy
signal writes, snapshot-diff synthesis
of control events, and the persister observing control events (a control-only
change must reach consumers). Before that: the document + projections storage
model (point 4 — whole-run `run` column, discrete columns demoted to query
projections, both backends); the signals-as-state storage slice
(`signals` mailbox + `ControlRequest`/`JobRun.control_requests`);
heartbeat/liveness (point 4) — `heartbeat_at`
column (no schema bump, pre-release), node touches on the flush thread
(`HEARTBEAT_INTERVAL` 15s), server-clock `heartbeat_age` on the version scan,
lost-marking in the polling directory with `heartbeat_age`/`is_lost` on the
snapshot proxy; and the coordination redesign (point 6) — mutex holds
its no-wait group claim for the protected child's run; the queue claims per-group
slot locks with seniority-staggered attempts (rank × stagger delay per visibly
older waiter — ghosts only delay, never block); `_dispatch_next`, remote
`signal_dispatch`, and the remote-state parsing are deleted; the `Lock` contract
grew `try_acquire` across all three providers (memory locks made non-reentrant to
match file/advisory claim semantics). Before that: the backend-kind slice —
`EnvironmentKind` on the entry, per-kind config classes, one `match entry.kind`
per entry point, the `LockProvider` seam extraction, and `AdvisoryLockProvider`.

## Cross-references

- Standing process rule: never `git commit` without explicit user approval —
  show the diff and wait for an explicit go, per repo.
