# Vigil

Vigil is a web-based network and systems monitor for Linux systems, homelabs, and small networks. Inspired by Uptime Kuma, Prometheus, Grafana, and Loki, it provides a centralized dashboard to configure and manage diverse infrastructure from a single pane of glass — without requiring agents on remote hosts.

Unlike most network and system monitors, Vigil is designed to be highly extensible and capable of performing actions on monitored targets, not just observing them.

---

## Features

- **Pull-Based & Agentless**: Uses a pull-based design over SSH and ICMP to collect events, logs, and metrics — no software needed on target nodes.
- **Web Dashboard**: Real-time interactive visualizations built with NiceGUI and ECharts, featuring latency history, status distribution, and log views.
- **Alerting & Notifications**: Sends alerts to various channels when events or metric thresholds are detected. *(WIP)*
- **Target Control**: Trigger actions on monitored targets (e.g. restarting systemd services) directly from the UI.
- **Plugin Architecture**: Core features are implemented through plugins called "monitors" which handle specific domains (systemd services, host uptime, hardware parameters, etc.).
- **Hierarchical Organization**: Organize monitors into nested groups by location, service, or environment.
- **Lightweight**: Minimal dependencies and low resource footprint.
- **Easy Development**: Fully written in Python.

---

## Architecture

Vigil is organized around a **CPAC** model — each plugin is responsible for its own implementation of these four functions:

- **Collection**: Gathering events, metrics, and logs from specific targets via SSH, ICMP, HTTP, etc.
- **Presentation**: Rendering collected data on the web dashboard via each plugin's `render_ui()` method.
- **Alerting**: Monitoring configured thresholds and sending notifications through channels (Email, Slack, Webhooks) when criteria are met.
- **Control**: Sending remediation commands back to targets (e.g. restarting services) directly from the platform.

### Data Flow

1. **Initialization**: `main.py` loads config definitions and recursively instantiates plugins. `GroupPlugin` instances act as containers for nested monitors.
2. **Polling Loop**: The engine runs an async loop, calling `run_cycle()` on all non-group plugin instances.
3. **Collection & Processing**: Plugins use collectors in `core/modules/collectors` to gather data from targets.
4. **Persistence**: Results are stored in the SQLite database via the persistence layer in `core/data`.
5. **Visualization**: The dashboard renders a recursive sidebar; each plugin renders its own detail view.
6. **Alerting & Control**: On failure detection, plugins trigger alerts or execute remediation via controllers.

### Project Structure

```
vigil/
├── core/
│   ├── main.py          # Main engine orchestrator and plugin loader
│   ├── data/            # SQLite persistence layer (Peewee ORM) and config parsing
│   ├── common/          # Base classes (BasePlugin), shared utilities (SSH, etc.)
│   ├── modules/
│   │   ├── collectors/  # Abstractions for data gathering (SSH, HTTP, etc.)
│   │   ├── controllers/ # Logic for sending control/remediation commands
│   │   └── alerting/    # Notification modules (Email, Slack, Webhooks)
│   └── ui/              # NiceGUI-based dashboard logic
└── plugins/             # Domain-specific monitoring implementations (Uptime, Systemd, etc.)
```

### Technical Stack

| Concern        | Technology                          |
|----------------|--------------------------------------|
| Language       | Python 3.9+                          |
| Connectivity   | Paramiko (SSH), Requests/Httpx (HTTP)|
| Configuration  | YAML                                 |
| Concurrency    | `asyncio`                            |
| Storage        | SQLite via Peewee ORM                |
| Frontend       | NiceGUI                              |

---

## Theme

All colors used in the dashboard can be overridden in the `theme:` section of `config.yaml`. All fields are optional — omit any field to keep its default.

| Field             | Default       | Description                              |
|-------------------|---------------|------------------------------------------|
| `primary`         | `#00ACFF`     | Header, links, and primary accents       |
| `accent`          | `#FF5500`     | Secondary accent color                   |
| `background`      | `#FFFFFF`     | Sidebar and card backgrounds             |
| `background_muted`| `#FAFAFA`     | Page body background                     |
| `text`            | `#111827`     | Primary text                             |
| `text_muted`      | `#6B7280`     | Labels and secondary text                |
| `status_online`   | `lime`        | Color shown when a monitor is online     |
| `status_warning`  | `gold`        | Color shown when a monitor is in warning |
| `status_failed`   | `red`         | Color shown when a monitor has failed    |
| `status_offline`  | `lightgray`   | Color shown when a monitor is offline    |

```yaml
theme:
  primary: "#7C3AED"
  status_online: "limegreen"
  status_warning: "orange"
```

---

## Plugin Types

All plugin types share these common fields:

| Field    | Description                                                          |
|----------|----------------------------------------------------------------------|
| `name`   | Display name shown in the sidebar and dashboard                      |
| `id`     | Unique identifier used internally (defaults to `name` if omitted)    |
| `type`   | Plugin type — one of `uptime`, `systemd_service`, `smart_disk`, `zfs_health`, `disk_space`, `network_usage`, `cpu_usage`, `memory_usage`, `temperature`, `load_average`, `processes`, `group` |
| `interval` | Polling frequency in seconds (default: 60)                         |

---

### `uptime`
Checks host availability using ICMP ping.

| Option        | Description                                        |
|---------------|----------------------------------------------------|
| `target_host` | IP address or hostname to ping                     |
| `interval`    | Polling frequency in seconds (default: `60`)       |

**Metrics**: `up` (1/0), `latency_ms`

```yaml
- name: "Core Gateway"
  id: "gateway-ping"
  type: "uptime"
  target_host: "192.168.1.1"
  interval: 30
```

---

### `systemd_service`
Monitors systemd units over SSH. Operates in two modes depending on whether `max_age` is set.

**Continuous mode** (default) — for long-running daemons. Checks `systemctl is-active` each cycle and reports `online`/`warning`/`failed`.

**Oneshot mode** (`max_age` set) — for timer-driven services that run and exit (e.g. `nixos-upgrade`, backup jobs). Checks the result and timestamp of the last completed run via `systemctl show`. Reports `failed` if the last run did not succeed or completed more than `max_age` seconds ago.

| Option         | Description                                                                     |
|----------------|---------------------------------------------------------------------------------|
| `service_name` | Name of the systemd unit (e.g. `nginx.service`)                                 |
| `lines`        | Number of `journalctl` log lines to fetch per cycle (default: `10`)             |
| `interval`     | Polling frequency in seconds (default: `60`)                                    |
| `max_age`      | *(Oneshot mode)* Max seconds since last successful run before reporting `failed` |
| `ssh_config`   | SSH connection details — see [SSH Config](#ssh-config) below                    |

**Continuous metrics**: `active` (1/0)

**Oneshot metrics**: `last_run_epoch` (Unix timestamp), `last_run_success` (1/0)

**Actions**: Restart Service, Stop Service

```yaml
# Continuous — long-running daemon
- name: "Nginx"
  id: "nginx-service"
  type: "systemd_service"
  service_name: "nginx.service"
  interval: 60
  ssh_config:
    host: "web-01.example.com"

# Oneshot — weekly timer-driven service
- name: "NixOS Upgrade"
  id: "myhost-nixos-upgrade"
  type: "systemd_service"
  service_name: "nixos-upgrade.service"
  interval: 3600
  max_age: 604800  # 1 week
  ssh_config:
    host: "myhost.example.com"
```

---

### `smart_disk`
Monitors SMART health of all physical disks over SSH. Discovers disks automatically via `lsblk` and runs `smartctl -H` on each one. USB-attached disks are probed with `-d sat`.

> The SSH user must have passwordless `sudo` access to `smartctl` (e.g. `vigil ALL=(ALL) NOPASSWD: /usr/bin/smartctl`).

| Option      | Description                                                        |
|-------------|--------------------------------------------------------------------|
| `interval`  | Polling frequency in seconds (default: `60`, recommend `3600`)     |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below      |

**Metrics**: `disks_total`, `disks_ok`, `disks_failed`

```yaml
- name: "Ragnarok SMART"
  id: "ragnarok-smart"
  type: "smart_disk"
  interval: 3600
  ssh_config:
    host: "ragnarok.technet"
```

---

### `zfs_health`
Monitors ZFS pool health states over SSH via `zpool list -H -o name,health`. Reports failed if any pool is in a `DEGRADED`, `FAULTED`, `OFFLINE`, `UNAVAIL`, or `REMOVED` state. Complements `zfs_pool` (capacity) with structural integrity monitoring.

| Option      | Description                                                        |
|-------------|--------------------------------------------------------------------|
| `interval`  | Polling frequency in seconds (default: `60`, recommend `3600`)     |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below      |

**Metrics**: `pools_total`, `pools_ok`, `pools_degraded`

```yaml
- name: "Ragnarok ZFS Health"
  id: "ragnarok-zfs-health"
  type: "zfs_health"
  interval: 3600
  ssh_config:
    host: "ragnarok.technet"
```

---

### `disk_space`
Monitors disk space usage for a path or mountpoint over SSH via `df`. Works on any mounted Linux filesystem — no ZFS or other tools required. Marks the path failed when usage exceeds the configured threshold.

| Option      | Description                                                       |
|-------------|-------------------------------------------------------------------|
| `path`      | Filesystem path or mountpoint to monitor (e.g. `/`, `/Storage`)  |
| `threshold` | Usage percentage that triggers a `failed` status (default: `90`) |
| `interval`  | Polling frequency (default: `60`, recommend `10m`)                |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below     |

**Metrics**: `used_pct`, `size_gb`, `used_gb`, `avail_gb`

```yaml
- name: "Root Disk"
  id: "myhost-disk-root"
  type: "disk_space"
  path: "/"
  threshold: 90
  interval: 10m
  ssh_config:
    host: "myhost.example.com"
```

---

### `cpu_usage`
Monitors CPU utilization over SSH. Takes two `/proc/stat` snapshots one second apart in a single SSH command and computes the usage delta — no agents or extra tools required.

| Option          | Description                                      |
|-----------------|--------------------------------------------------|
| `cpu_warning`   | CPU % that triggers `warning` (default: `70`)   |
| `cpu_threshold` | CPU % that triggers `failed`  (default: `85`)   |
| `interval`      | Polling frequency (default: `60`)                |
| `ssh_config`    | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `cpu_pct`

```yaml
- name: "Heimdall CPU"
  id: "heimdall-cpu"
  type: "cpu_usage"
  interval: 1m
  cpu_warning: 70
  cpu_threshold: 85
  ssh_config:
    host: "heimdall.example.com"
```

---

### `memory_usage`
Monitors memory usage over SSH via `/proc/meminfo`. Uses `MemAvailable` (not `MemFree`) so filesystem cache is not counted as used. Single SSH read — no sleep required.

| Option              | Description                                           |
|---------------------|-------------------------------------------------------|
| `memory_warning`    | Memory % that triggers `warning` (default: `75`)     |
| `memory_threshold`  | Memory % that triggers `failed`  (default: `90`)     |
| `interval`          | Polling frequency (default: `60`)                     |
| `ssh_config`        | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `memory_pct`, `memory_used_gb`, `memory_total_gb`

```yaml
- name: "Heimdall Memory"
  id: "heimdall-memory"
  type: "memory_usage"
  interval: 1m
  memory_warning: 75
  memory_threshold: 90
  ssh_config:
    host: "heimdall.example.com"
```

---

### `temperature`
Monitors system temperature over SSH via `/sys/class/thermal/thermal_zone*/temp`. Reports the maximum temperature across all thermal zones. Gracefully stays `online` with no metric when no thermal zones are present (e.g. VMs).

| Option           | Description                                            |
|------------------|--------------------------------------------------------|
| `temp_warning`   | °C that triggers `warning` (default: `70`)            |
| `temp_threshold` | °C that triggers `failed`  (default: `80`)            |
| `interval`       | Polling frequency (default: `60`)                      |
| `ssh_config`     | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `temp_c`

```yaml
- name: "Heimdall Temperature"
  id: "heimdall-temperature"
  type: "temperature"
  interval: 1m
  temp_warning: 70
  temp_threshold: 80
  ssh_config:
    host: "heimdall.example.com"
```

---

### `load_average`
Monitors system load averages over SSH via `/proc/loadavg`. Load values are normalized by CPU core count (via `nproc`) and stored as a percentage — 100% means the system is exactly at capacity. Falls back to treating core count as 1 if `nproc` is unavailable. Thresholds are optional — when unset, load is collected and displayed but does not affect status.

| Option           | Description                                                                  |
|------------------|------------------------------------------------------------------------------|
| `load_warning`   | 1m load as % of cores that triggers `warning` (optional — omit to disable)  |
| `load_threshold` | 1m load as % of cores that triggers `failed`  (optional — omit to disable)  |
| `interval`       | Polling frequency (default: `60`)                                             |
| `ssh_config`     | SSH connection details — see [SSH Config](#ssh-config) below                 |

**Metrics**: `load_pct_1m`, `load_pct_5m`, `load_pct_15m`

```yaml
- name: "Heimdall Load"
  id: "heimdall-load"
  type: "load_average"
  interval: 1m
  load_warning: 70    # warn when 1m load exceeds 70% of available cores
  load_threshold: 100 # fail when 1m load exceeds 100% of available cores
  ssh_config:
    host: "heimdall.example.com"
```

---

### `network_usage`
Monitors network interface throughput over SSH. Takes two snapshots of `/proc/net/dev` one second apart in a single SSH command — no extra tools required on the remote host.

The interface to monitor can be specified explicitly or auto-detected. In auto-detect mode, Vigil picks the non-virtual, non-loopback interface with the highest cumulative byte count, ignoring interfaces with prefixes like `lo`, `veth`, `docker`, `virbr`, `br-`, `tun`, and `tap`.

| Option       | Description                                                                            |
|--------------|----------------------------------------------------------------------------------------|
| `interface`  | *(Optional)* Interface name to monitor (e.g. `eth0`). Omit to auto-detect.            |
| `interval`   | Polling frequency (default: `60`). Shorter intervals give finer-grained trend history. |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below                           |

**Metrics**: `rx_kbps`, `tx_kbps`

```yaml
# Auto-detect the primary interface
- name: "Heimdall Network"
  id: "heimdall-network"
  type: "network_usage"
  interval: 30s
  ssh_config:
    host: "heimdall.example.com"

# Monitor a specific interface
- name: "Ragnarok Network"
  id: "ragnarok-network"
  type: "network_usage"
  interval: 30s
  interface: "eth0"
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `group`
A logical container for other monitors. Aggregates the worst-case status of all descendants and displays a summary card for each child.

| Option     | Description                                  |
|------------|----------------------------------------------|
| `children` | A list of nested plugin definitions          |

Groups can be nested to arbitrary depth.

```yaml
- name: "Infrastructure"
  type: "group"
  children:
    - name: "Web Tier"
      type: "group"
      children:
        - name: "Nginx"
          type: "systemd_service"
          ...
```

---

### SSH Config

All SSH-based plugins (`systemd_service`, `smart_disk`, `zfs_health`, `disk_space`, `network_usage`) accept an `ssh_config` block:

| Field        | Description                                                         |
|--------------|---------------------------------------------------------------------|
| `host`       | Remote hostname or IP address                                       |
| `user`       | SSH username (defaults to the current OS user if omitted)           |
| `port`       | SSH port (default: `22`)                                            |
| `key_file`   | Path to a private key file (uses the SSH agent / default key if omitted) |

```yaml
ssh_config:
  host: "myhost.example.com"
  user: "vigil"
  port: 22
  key_file: "/home/vigil/.ssh/id_ed25519"
```

---

## Getting Started

### Prerequisites

- Python 3.9+
- SSH access to target machines (SSH key auth recommended)

### Installation

```bash
pip install .
```

### Quick Start

1. Create a `config.yaml` (see [Configuration](#configuration) below).
2. Start the system: `vigil --config config.yaml`
3. Open your browser to `http://localhost:8080`.

---

## Configuration

Vigil uses a YAML file to define the hierarchy of your infrastructure. The YAML config is the **source of truth** for infrastructure definitions; SQLite is used for runtime state and overrides.

```yaml
database:
  path: "vigil.db"

plugins:
  - name: "Internal Network"
    type: "group"
    children:
      - name: "Core Gateway"
        id: "gateway-ping"
        type: "uptime"
        target_host: "192.168.1.1"
        interval: 30

  - name: "Web Servers"
    type: "group"
    children:
      - name: "Nginx Service"
        type: "systemd_service"
        service_name: "nginx.service"
        ssh_config:
          host: "web-01.example.com"
          user: "vigil"
```

---

## Usage

The primary entry point starts both the background engine and the web dashboard:

```bash
vigil --config config.yaml
```

To run just the dashboard against an existing database:

```bash
vigil-gui --db vigil.db --port 8080
```

### Nix Integration

Vigil supports Flakes for reproducible environments:

```bash
# Enter dev shell
nix develop

# Run via Nix
nix run . -- --config config.yaml
```

---

## Design Principles

1. **Simplicity First**: Configuration should be intuitive.
2. **No Remote Agent**: All logic stays on the Vigil server; remote hosts only need SSH.
3. **Domain Encapsulation**: Each plugin handles its own collection, alerting, and control logic.
4. **Hierarchical Organization**: Supports nested groups for organizing monitors by location, service, or environment.
5. **Fail-Safe Control**: Control actions must be logged and confirmable.
6. **Standard-Aware**: Aims for OpenTelemetry compatibility in data naming and export capability.

---

## Roadmap

- [x] Core engine with YAML config loader
- [x] Core database utility (SQLite)
- [x] Core SSH utility for remote access
- [x] Hierarchical plugin/group support
- [x] Ping/ICMP uptime module
- [x] Web dashboard (NiceGUI)
- [x] Disk space monitor (any path/mountpoint via `df`, threshold alerting)
- [x] ZFS pool health monitor (DEGRADED/FAULTED detection)
- [x] SMART disk health monitor
- [x] CPU usage monitor (via `/proc/stat` two-sample delta, warning/failed thresholds)
- [x] Memory usage monitor (via `/proc/meminfo`, warning/failed thresholds)
- [x] Temperature monitor (via `/sys/class/thermal`, graceful degradation on VMs)
- [x] Load average monitor (via `/proc/loadavg` normalized by `nproc`, optional thresholds)
- [x] Network usage monitor (RX/TX throughput via `/proc/net/dev`, auto-detect or explicit interface)
- [ ] SSH collector module with standard metric parsing (CPU, RAM, Disk)
- [ ] Basic alerting (Email, Slack, or Webhook)
- [ ] Control module for service remediation
- [ ] OpenTelemetry/OpenMetrics export module

---

## License

GPL 3.0