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
**Open:** the `postgres` producer slice (node access point + advisory locks —
the node raises until then); heartbeat/liveness (point 4, deferred); split the
`JobInstance` contract (deferred until it bites); signals-as-state (point 5).

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
  │ _Connector         │                   │ _Node              │
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
lock provider        threading | file | advisory     -> LockFactory / LockProvider
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
    POSTGRES = "postgres"  # postgres + polling directory (observe-only until the producer slice)
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
# EnvironmentStoreNotProvisionedError; directory = PollingInstanceDirectory(env_db).
# runjob/node.py connect: same match; LOCAL builds stores/features/access point inline;
# POSTGRES raises NotImplementedError ("observed but not produced") until the producer slice.
```

Each branch names its driver directly — no entry→db helper on the connect path (an earlier
version routed through kind-dispatched `_create_env_db`, re-running inside the branch the
dispatch the branch had just decided; the shared exists-guard also masked postgres's more
precise not-provisioned error). `_create_env_db` stays env-private for the generic config
functions (`load_env_config`/`save_env_config`), which do need runtime dispatch.

The open+load boilerplate is deliberately duplicated across the per-kind functions (four sites,
~6 lines each). Known follow-up, applied only once all repos are green: extract
`_open_configured_environment(entry, config_type)`. Kept out of the slice so the first landing
introduces zero new abstractions.

**Deleted:** `load_database_module` (env.py's `_db_module` table replaces it), `TransportType`,
the transport-config classes, the `TransportConfig` union, `EnvironmentConfig.transport`,
`EnvironmentConfig.id` (callers hold the entry), `default_local()` (the default local config is
just `LocalEnvironmentConfig()`), and the db backends' `load_config` id injection. The built-in
local entry is `EnvironmentEntry(id="local", kind=LOCAL)`.

**Settled decisions:**
- `in_process` stays a direct constructor (`node.in_process(...)`), *not* a registry kind; its
  `node._create` branch is removed (registry kinds are LOCAL/POSTGRES).
- `postgres` kind is observe-only until the producer slice (node access point + advisory locks)
  lands — the node raises for it.
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

Unchanged in shape by the redesign.

```python
# runtools/runjob/transport/__init__.py
class InstanceAccessPoint(Protocol):
    def register_instance(self, job_instance: JobInstance) -> None: ...
    def unregister_instance(self, job_instance: JobInstance) -> None: ...
    def emit(self, event) -> None: ...
    lock_factory: Callable[[str], object]
    def start(self) -> None: ...
    def close(self) -> None: ...
```

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

`get_output_tail` carries `mode` on the wire (optional param defaulting to
TAIL, so older clients keep working); the node's `InMemoryTailBuffer`
applies HEAD/TAIL natively. Known gap, shared server idiom: an invalid enum
string on the wire (`Mode[mode]`, `StopReason[...]`) surfaces as an internal
error rather than invalid-params.

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
- **`heartbeat_at`** — **deferred, not implemented.** The liveness column +
  reconciler are a later track (see Remaining work); for now an abruptly-died
  node's active rows simply persist until reconciliation exists.

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

**Liveness.** `heartbeat_at` per non-ended run, touched on the flush tick
(~10–30s; staleness threshold ~3×) independent of state writes. Readers
*interpret, never mutate*: an active row with a stale `heartbeat_at` displays
as lost/unknown — writing `ABANDONED` would break single-writer-per-instance
and race across connectors; an explicit reaper is a separate later decision.
This makes abruptly-died nodes' instances *visible and attributable* — a
capability RPC discovery structurally lacks (dead socket = invisible instance).
Per-node heartbeat (one row per node + writer-id column) is the measured
optimisation if envs grow large.

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
`SnapshotJobInstanceProxy` + snapshot-diff synthesis. What remains is *selecting*
it as a backend kind (Remaining work #1), plus the heartbeat track. The poll
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
`runs`); commands/RPC remain separate until signals-as-state lands.

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
`TIMESTAMPTZ`, lets the DB serialize writers). The criteria→SQL translation and
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

**Agreed direction (post-Phase C).** Phase ops (and eventually `stop`) stop
being RPC calls and become consumer-written *signal records* the owning
phase reconciles against — the Kubernetes desired-state model. What RPC
cannot give: **durability** (an `approve` survives node restarts and
consumer disconnects) and a free audit trail (who signalled, when).

Rules:

- **Separate signal store, consumer-written** — never the node's run row
  (preserves single-writer-per-instance). Keyed by instance/phase/op.
- **State is truth, event is doorbell.** Write the signal, nudge over the
  transport; the phase checks on nudge, coarse poll (~seconds) as fallback.
  Zero steady-state polling.
- **Remote phase ops return `None` and are idempotent.** Both existing ops
  (`approve`, `resume`) already comply — they set an event and return
  nothing; re-delivery is harmless. Outcome is observed via phase events.
- **Queries never cross the wire.** `control_api` query properties
  (`approved`, `is_resumed`) are local-only; remote state reads come from
  `snap()` — export to phase `variables` anything a consumer is missing.

End-state across the transport: discovery via DB (point 4), state via
events, output deep-reads as the lone fetch, commands as signals — **the
node accepts no calls**; its access point reduces to a doorbell receiver.
Accepted cost: command latency goes from RPC-RTT to write+nudge (tens of
ms) — irrelevant at approve/resume/stop pacing.

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

1. **The `postgres` producer slice.** Node access point + advisory locks;
   `node.connect` raises `NotImplementedError` for the `postgres` kind until it
   lands. Intervals already set (active 250ms, idle 2s, flush 250ms — see
   point 4).
2. **Shared open helper (mechanical refactor).** The per-kind connect functions
   duplicate the open-db/load-config boilerplate (four sites); extract
   `_open_configured_environment(entry, config_type)` as its own small commit.
3. Liveness/heartbeat (deferred slice of point 4): `heartbeat_at` column touched
   per flush tick for non-ended runs; readers interpret-never-mutate (stale
   heartbeat → lost/unknown). Makes abruptly-died nodes' instances visible.
4. Signals-as-state for commands (design point 5, post-transport).
5. Resolve the `JobInstance` contract split when it bites (open point 1;
   remote-run question).

Done since the last revision: the backend-kind slice — `EnvironmentKind` enum,
`EnvironmentEntry.kind` (typed, registry key `kind`, fixing the old `driver`
write/read mismatch), per-kind config classes replacing the `transport`
discriminator (config carries neither `id` nor `kind`), one `match entry.kind`
per entry point (connector + node), `load_database_module` replaced by env.py's
`_db_module` table, unix factories taking `root_dir` directly. The poll lane is
now user-selectable as the `postgres` kind (connector-side); `in_process`
remains a direct constructor. Landed across runcore, runjob, taro, and runcli.

## Cross-references

- Standing process rule: never `git commit` without explicit user approval —
  show the diff and wait for an explicit go, per repo.
