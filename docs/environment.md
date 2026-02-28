# Environment Configuration

Environments isolate job instances from each other. Each environment has its own runtime space and persistence, allowing you to separate jobs by project, purpose, or any other criteria.

## Configuration Files

Configuration files match the pattern `*env*.toml` (e.g., `env.toml`, `myenv.toml`).

### Search Locations

Files are searched in the following locations, listed from **lowest to highest priority**:

| Priority | Location | Description |
|----------|----------|-------------|
| 1 (lowest) | [Packaged default](https://github.com/runtoolsio/runcore/blob/main/src/runtools/runcore/config/env.toml) | Built-in default shipped with runcore |
| 2 | `/etc/runtools/` | System-wide configuration |
| 3 | `/etc/xdg/runtools/` | XDG system configuration |
| 4 | `~/.config/runtools/` | User configuration |
| 5 (highest) | `./.runtools/config/` or `./` | Project/current directory |

**Higher priority files override lower priority files.** This means:
- Project config overrides user config
- User config overrides system config
- System config overrides packaged defaults

### How Merging Works

When the same environment ID exists in multiple files, settings are **deep-merged**:

```toml
# Packaged default (priority 1)
[[environment]]
id = "local"
[environment.persistence]
type = "sqlite"
enabled = true

# User config ~/.config/runtools/env.toml (priority 4)
[[environment]]
id = "local"
[environment.persistence]
database = "~/my-custom-path.db"
```

**Merged result:**
```toml
[[environment]]
id = "local"
[environment.persistence]
type = "sqlite"           # from packaged default
enabled = true            # from packaged default
database = "~/my-custom-path.db"  # from user config (overrides)
```

## Configuration Schema

```toml
[[environment]]
id = "local"              # Required: unique identifier
type = "local"            # Required: "local" or "isolated"
default = true            # Optional: mark as default environment

[environment.layout]
root_dir = "/custom/path" # Optional: root directory for runtime files

[environment.persistence]
type = "sqlite"           # Required: persistence type
enabled = true            # Optional: enable/disable (default: true)
database = "path/to/db"   # Optional: database path

[environment.persistence.params]
timeout = 5.0             # Optional: database-specific parameters
```

## Environment Types

### Local

A local environment is restricted to a single machine. Communication between job instances and connectors happens via Unix domain sockets, which only work on the same host. This is the standard environment type for running and monitoring jobs.

The default `local` environment is defined in the [packaged configuration](https://github.com/runtoolsio/runcore/blob/main/src/runtools/runcore/config/env.toml) and can be customized by adding your own `env.toml` file in any of the higher-priority locations.

### Isolated

An isolated environment exists only within a single process. It uses in-memory SQLite and memory-based locks with no external communication (no sockets). This makes it useful for testing and scenarios where jobs should be completely isolated from other processes.

## Default Environment

When no environment ID is specified, runcore selects the default:

1. Looks for an environment with `default = true`
2. Falls back to `id = "local"`
3. Raises `EnvironmentNotFoundError` if neither exists

## Inspecting from the CLI

Both `run` (runcli) and `taro` provide an `env` command that shows the resolved environment configuration
after all files have been merged:

```bash
# Show default environment
run env
taro env

# Show a specific environment
run env -e myproject
taro env -e myproject

# Show all environments
run env --all
taro env --all
```

The output is the fully resolved TOML configuration, useful for verifying which files took effect.

## Environments Without Config

Both `node.connect(env_id)` and `connector.connect(env_id)` try config first and fall back to a
default `LocalEnvironmentConfig` when no config entry exists. The socket paths and database location
are derived deterministically from the environment ID — no config file is needed.

This means commands like `taro ps -e myenv` work even when there is no config entry for `myenv`:

```bash
# Works even without a config entry for "demo"
taro ps -e demo
taro stop -e demo "*"
taro history -e demo
```

The fallback only applies to explicitly named environments. The default environment (used when
`-e` is omitted) always requires config.

## Usage in Code

```python
from runtools.runcore import connector
from runtools.runcore.env import LocalEnvironmentConfig

# Connect to an environment (config lookup with local fallback)
with connector.connect("myenv") as conn:
    runs = conn.get_active_runs()

# Create from explicit config
with connector.create(LocalEnvironmentConfig(id="myenv")) as conn:
    runs = conn.get_active_runs()
```

```python
from runtools.runjob import node
from runtools.runcore.env import LocalEnvironmentConfig

# Create a node (config lookup with local fallback)
with node.connect("myenv") as env:
    inst = env.create_instance(...)

# Create from explicit config
with node.create(LocalEnvironmentConfig(id="myenv")) as env:
    inst = env.create_instance(...)
```
