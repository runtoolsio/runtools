# Transport Refactor

> **Status:** Planned. Not yet implemented.

Replace `EnvironmentType` and its discriminated config Union with a single `EnvironmentConfig`
whose process topology, communication mechanism, and coordination primitives are all selected
by one discriminator: `transport`.

## Motivation

Today `EnvironmentType` is a two-value enum (`LOCAL`, `IN_PROCESS`) that pretends to be a
discriminator. It actually bundles three independent concerns into two preset combinations:

- **Transport** (unix sockets or none) — hardcoded inside `LocalConnector` / `LocalNode`
- **Persistence** (sqlite on-disk or in-memory) — already pluggable via
  `EnvironmentEntry.driver` + `db.load_database_module()`, but invisible at the type level
- **Output backends** (file, s3) — already pluggable via `output.load_output_module()`, listed
  in `OutputConfig.storages`

Persistence and output are already orthogonal in code; they don't need to live inside the type
enum. Transport is the truly fused axis — once Redis or postgres LISTEN/NOTIFY transports land,
a flat enum re-bundles "transport + DB + output" for every new variant.

## New Model

```
EnvironmentConfig
├── id
├── transport: TransportConfig      # discriminated union — selects process topology,
│                                   # comms mechanism, AND coordination/lock factory
├── output:    OutputConfig         # already-orthogonal, unchanged
├── retention: RetentionConfig      # unchanged
└── plugins:   Dict[str, dict]      # unchanged
```

```
TransportConfig =
    InProcessTransportConfig        # same-process, no comms, no connector
  | UnixSocketTransportConfig       # current local-socket runtime
  # | RedisTransportConfig          # future
  # | PostgresNotifyTransportConfig # future
```

Definition: **transport defines the runtime boundary and communication mechanism.**

- `in_process` — no boundary; calls stay in memory.
- `unix_socket` — process boundary over Unix domain sockets.
- `redis` / `postgres_notify` — process/machine boundary over shared services.

### What goes away

- `EnvironmentType` (enum)
- `LocalEnvironmentConfig`
- `InProcessEnvironmentConfig`
- `EnvironmentConfigUnion` and its discriminator
- `LayoutConfig` at the env-config level (moves under `UnixSocketTransportConfig`)

### What stays unchanged

- `EnvironmentEntry { id, driver, location }` — registry bootstrap. Persistence stays outside
  `EnvironmentConfig` because the DB must be opened before the config can be loaded.
- `OutputConfig` / `OutputStorageConfig` and the output backend loader.
- `db.load_database_module()` and the persistence driver loader.
- The persistence/output module loader pattern. Both remain dynamically pluggable.

### Asymmetry kept honest

Unifying at the config-shape level does not eliminate runtime asymmetries — it moves them out
of the type system and into code, which is the right place:

| Concern | Durable transports | `in_process` |
|---|---|---|
| Registry membership | yes | never registered; constructed in-code |
| Persistence | bootstrapped from registry → loaded from DB | in-memory sqlite, ephemeral, never serialized |
| Connector | separate from node | absent — clients talk to the node directly |
| Coordination locks | provided by transport | provided by transport (memory locks) |

## Why Transport Is Not Pluggable

`EnvironmentEntry.driver` and `OutputStorageConfig.type` use a module-loader pattern that
accepts any third-party implementation. Transport explicitly does not.

Transport owns: node discovery, RPC routing, event delivery, listener lifecycle, connector
liveness, active-run visibility, cleanup semantics, locking, and failure modes. The current
`LocalConnector` and `LocalNode` *are* the contract — there is no extracted ABC, and the
implicit invariants are not yet stable enough to commit to as a public plugin interface.

A closed discriminated union keeps each transport first-class and auditable. Once a second
transport (Redis) has been built and the contract has survived two real implementations,
extraction can be revisited.

## Transport Owns Coordination

Today there are two distinct lock-like concerns, both filesystem-based for the current local
runtime:

1. **Component liveness** — "is this connector/listener/node still alive?" Currently a
   `fcntl.flock` on a `.lock` file under the component directory.
2. **Job coordination** — `@mutex` / `@queue` group locking. Currently a file-lock factory
   injected by `LocalNode`.

Both are coordination concerns and both belong with the transport. Each transport owns the
appropriate primitive for both:

| Transport | Component liveness | Job coordination |
|---|---|---|
| `in_process` | n/a (no boundary) | in-memory lock factory |
| `unix_socket` | file lock | file lock factory |
| `redis` (future) | redis key + heartbeat | redis lock |
| `postgres_notify` (future) | postgres advisory | postgres advisory |

No top-level `lock` config — locking is implied by transport.

There is no top-level `lock` config and no separate `runtime` config block. `transport` is the
single discriminator for everything coordination-related.

## Plan

### 1. Introduce `TransportConfig` and collapse `EnvironmentConfig` onto it

Define `TransportConfig` as a discriminated union with two initial variants:

```python
class InProcessTransportConfig(BaseModel):
    type: Literal["in_process"] = "in_process"

class UnixSocketTransportConfig(BaseModel):
    type: Literal["unix_socket"] = "unix_socket"
    root_dir: Optional[Path] = None  # transport root: holds component dirs,
                                     # socket files, and liveness lock files.
                                     # Replaces today's LayoutConfig.root_dir.

TransportConfig = Annotated[
    Union[InProcessTransportConfig, UnixSocketTransportConfig],
    Discriminator("type"),
]
```

Replace the four removed types with one:

```python
class EnvironmentConfig(BaseModel):
    id: str
    transport: TransportConfig
    output: OutputConfig = Field(default_factory=OutputConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    plugins: Dict[str, dict] = Field(default_factory=dict)
```

Default factory exposes the out-of-the-box preset (sqlite + unix sockets + file output + file
locks):

```python
@classmethod
def default_local(cls, env_id: str) -> "EnvironmentConfig":
    return cls(id=env_id, transport=UnixSocketTransportConfig())
```

### 2. Port the current local runtime under `unix_socket`

Move from `runcore/env.py` and the connector/node layers:

- `LayoutConfig.root_dir` → `UnixSocketTransportConfig.root_dir`
- Socket-path conventions (`LocalConnectorLayout`, `LocalNodeLayout`) live alongside or under
  the unix_socket transport — they are transport-specific layout details, not generic
  environment concerns.
- The component-liveness `fcntl.flock` mechanism becomes part of the unix_socket transport's
  liveness contract.
- The default `lock_factory` consumed by job coordination (`@mutex`, `@queue`) comes from the
  unix_socket transport (file-lock factory).

### 3. Model in-process as a transport

`InProcessTransportConfig` is empty — it's a discriminator marker.

`node.in_process(env_id, ...)` (the factory) constructs an `EnvironmentConfig` with
`transport=InProcessTransportConfig()` and:

- Persistence: bypass the registry/DB-load flow — instantiate in-memory sqlite directly.
- Connector: none. Clients talk to the node directly.
- Lock factory: in-memory.

The factory is the documented entry point for in-process; in-process configs are not loaded
from disk.

### 4. Plumb coordination through transport

Both lock flavors come from the transport implementation, not from the environment type or a
hardcoded local default:

- Component liveness — used by connectors/listeners; transport exposes a liveness primitive.
- Job coordination — `LocalNode` no longer owns a hardcoded file-lock factory; it asks the
  transport. `@mutex` / `@queue` continue to consume `lock_factory` through the existing job
  context wiring.

This is the most cross-cutting wire — the lock factory currently flows
`LocalNode → job phases → @mutex/@queue` and after the refactor flows
`transport → node → job phases → @mutex/@queue`.

### 5. Rewire factories, connectors, and config loading

- `_open_environment` — loads `EnvironmentConfig` (single shape, no Union validation) and
  dispatches the connector/node construction based on `config.transport.type`.
- `node._create` / `connector._create` — switch on transport variant instead of `EnvironmentType`.
- Built-in local defaults — `EnvironmentConfig.default_local(BUILTIN_LOCAL)` produces today's
  behavior with no caller-visible change.
- `paths.py` — default socket-dir resolution moves under `UnixSocketTransportConfig`
  (either as a method on the config class or as a function in the transport implementation
  that consumes `paths.py` as a util).

### 6. Update `taro env` subcommands

`taro env create` becomes transport-aware. Minimum surface:

```bash
taro env create dev                                  # implicit unix_socket
taro env create dev --transport unix_socket
taro env create dev --transport unix_socket --root-dir /tmp/runtools/dev
# Future:
# taro env create dev --transport redis --url redis://...
```

`taro env show` / `env edit` reflect the new TOML shape. The registry format
(`environments.toml`) is unaffected — it still stores only `driver` + `location` bootstrap.

### 7. Update call sites and tests

- All imports of the removed types (`EnvironmentType`, `LocalEnvironmentConfig`,
  `InProcessEnvironmentConfig`, `EnvironmentConfigUnion`) — sweep across runcore, runjob,
  runcli, taro and any in-repo consumers.
- Test fixtures that construct `LocalEnvironmentConfig` / `InProcessEnvironmentConfig`
  directly — replace with `EnvironmentConfig.default_local(...)` or the `in_process` factory.
- Verify behavior:
  - `runcore/test/` — config (de)serialization, env lookup, registry I/O.
  - `runjob/test/` — node + connector wiring, coordination lock plumbing.
  - `taro/test/` — env CLI commands, env display.

## Out of Scope

- **Backwards compatibility** — pre-beta; no migration shim. Existing DBs with the old config
  shape are unsupported and must be recreated manually.
- **Combination validation** — no runtime rejection of unusual but harmless combos. Document
  recommended combinations; trust users.
- **Persistence driver expansion** — no postgres persistence in this refactor. Only sqlite
  stays. The orthogonality is in place so adding postgres later is a strict additive change.
- **Output backend expansion** — file + s3 stay as today.
- **Locking as a fifth top-level axis** — explicitly attached to transport instead.
