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

### Summary

| Type | Monitors | Collection | Key metrics | Actions |
|------|----------|------------|-------------|---------|
| [`uptime`](#uptime)                     | Host reachability                     | ICMP ping                                        | `up`, `latency_ms`                              | — |
| [`systemd_service`](#systemd_service)   | systemd unit state / last run         | SSH (`systemctl`)                                | `active` *or* `last_run_epoch`, `last_run_success` | Restart, Stop |
| [`smart_disk`](#smart_disk)             | Physical disk SMART health            | SSH (`smartctl`)                                 | `disks_total`, `disks_ok`, `disks_failed`       | — |
| [`zfs_health`](#zfs_health)             | ZFS pool health state                 | SSH (`zpool list`)                               | `pools_total`, `pools_ok`, `pools_degraded`     | — |
| [`zfs_pool`](#zfs_pool)                 | ZFS pool capacity                     | SSH (`zpool list`)                               | `usage_pct`                                     | — |
| [`disk_space`](#disk_space)             | Filesystem usage for a path           | SSH (`df`)                                       | `used_pct`, `size_gb`, `used_gb`, `avail_gb`    | — |
| [`cpu_usage`](#cpu_usage)               | CPU utilization                       | SSH (`/proc/stat`, 2-sample)                     | `cpu_pct`                                       | — |
| [`memory_usage`](#memory_usage)         | RAM usage                             | SSH (`/proc/meminfo`)                            | `memory_pct`, `memory_used_gb`, `memory_total_gb` | — |
| [`temperature`](#temperature)           | Max thermal-zone temperature          | SSH (`/sys/class/thermal`)                       | `temp_c`                                        | — |
| [`load_average`](#load_average)         | Load average (normalized by cores)    | SSH (`/proc/loadavg`, `nproc`)                   | `load_pct_1m`, `load_pct_5m`, `load_pct_15m`    | — |
| [`processes`](#processes)               | Running processes by CPU              | SSH (`ps`)                                       | `process_count`, `top_cpu_pct` *(ephemeral)*    | SIGTERM, SIGKILL |
| [`network_usage`](#network_usage)       | Network interface throughput          | SSH (`/proc/net/dev`, 2-sample)                  | `rx_kbps`, `tx_kbps`                            | — |
| [`diskio`](#diskio)                     | Per-disk read/write throughput        | SSH (`/proc/diskstats`, 2-sample)                | `read_kbps`, `write_kbps`                       | — |
| [`interrupts`](#interrupts)             | Interrupt & context-switch rates      | SSH (`/proc/stat`, 2-sample)                     | `irq_per_sec`, `ctxt_per_sec`                   | — |
| [`connections`](#connections)           | TCP connection counts by state        | SSH (`/proc/net/tcp`)                            | `total` + per-state (`established`, `listen`, …) | — |
| [`wifi`](#wifi)                         | WiFi link quality & signal            | SSH (`/proc/net/wireless`)                       | `link_quality`, `signal_dbm`                    | — |
| [`ports`](#ports)                       | TCP port / URL reachability           | SSH (`/dev/tcp`, `curl`)                         | `<check>_up`, `<check>_latency_ms`              | — |
| [`borg`](#borg)                         | Borg backup freshness                 | SSH (`borg list`)                                | `archive_count`, `last_backup_epoch`            | — |
| [`gpu`](#gpu)                           | NVIDIA GPU util / VRAM / temperature  | SSH (`nvidia-smi`)                               | `gpu_util`, `gpu_mem_pct`, `gpu_temp` (+ per-GPU) | — |
| [`containers`](#containers)             | Docker / Podman container states      | SSH (`docker`/`podman ps`)                       | `containers_total`, `containers_running`, `containers_stopped` | Restart (per expected container) |
| [`raid`](#raid)                         | Linux software RAID (mdadm) health    | SSH (`/proc/mdstat`)                             | `arrays_total`, `arrays_ok`, `arrays_degraded`  | — |
| [`command`](#command)                   | Arbitrary command (generic check)     | SSH (any command)                                | `exit_code` (+ `value` in pattern mode)         | — |
| [`filesystems`](#filesystems)           | All mounted filesystems (auto-discovered) | SSH (`df`)                                    | `worst_used_pct`, `fs_<mount>_used_pct`         | — |
| [`folders`](#folders)                   | Sizes of arbitrary directories        | SSH (`du`)                                        | `worst_folder_gb`, `folder_<path>_gb`           | — |
| [`vms`](#vms)                           | libvirt/KVM virtual machines          | SSH (`virsh`)                                     | `vms_total`, `vms_running`, `vms_stopped`       | Start, Shutdown (per expected VM) |
| [`cloud`](#cloud)                       | Cloud instance metadata (AWS/GCP/Azure) | SSH (metadata endpoint)                         | `on_cloud`                                      | — |
| [`group`](#group)                       | Container for nested monitors         | — (aggregates children)                          | —                                               | — |

All plugin types share these common fields:

| Field    | Description                                                          |
|----------|----------------------------------------------------------------------|
| `name`   | Display name shown in the sidebar and dashboard                      |
| `id`     | Unique identifier used internally (defaults to `name` if omitted)    |
| `type`   | Plugin type — one of `uptime`, `systemd_service`, `smart_disk`, `zfs_health`, `zfs_pool`, `disk_space`, `network_usage`, `diskio`, `interrupts`, `connections`, `wifi`, `ports`, `cpu_usage`, `memory_usage`, `temperature`, `load_average`, `processes`, `borg`, `gpu`, `containers`, `raid`, `command`, `filesystems`, `folders`, `vms`, `cloud`, `group` |
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

### `processes`
Monitors running processes over SSH via `ps`, sorted by CPU usage. Process data is ephemeral and stored in memory only — not persisted to the database. Per-row SIGTERM and SIGKILL buttons are available directly in the UI.

| Option          | Description                                                                  |
|-----------------|------------------------------------------------------------------------------|
| `max_processes` | Maximum number of processes to display (default: `20`)                       |
| `require_sudo`  | Prefix kill commands with `sudo` (default: `false`)                          |
| `kill_signal`   | Default signal for the kill action: `TERM` or `KILL` (default: `TERM`). Per-row buttons always offer both regardless of this setting. |
| `cpu_warning`   | Top-process CPU % that triggers `warning` (optional — omit to disable)       |
| `cpu_threshold` | Top-process CPU % that triggers `failed`  (optional — omit to disable)       |
| `interval`      | Polling frequency (default: `60`)                                             |
| `ssh_config`    | SSH connection details — see [SSH Config](#ssh-config) below                 |

**Metrics**: `process_count`, `top_cpu_pct`

```yaml
- name: "Heimdall Processes"
  id: "heimdall-processes"
  type: "processes"
  interval: 30s
  max_processes: 20
  cpu_warning: 80
  cpu_threshold: 95
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

### `gpu`
Monitors NVIDIA GPU utilization, VRAM usage, and temperature over SSH via a single `nvidia-smi --query-gpu` call. Handles multiple GPUs per host — each gets its own per-GPU metrics, and the overall status is the worst level across utilization, memory, and temperature for any GPU.

If `nvidia-smi` isn't installed or no NVIDIA GPU is present, the monitor reports **offline** rather than failed, so it degrades gracefully on mixed fleets.

| Option           | Description                                              |
|------------------|----------------------------------------------------------|
| `util_warning` / `util_threshold`   | GPU utilization % bounds (default: `85` / `95`)   |
| `mem_warning` / `mem_threshold`     | VRAM usage % bounds (default: `85` / `95`)        |
| `temp_warning` / `temp_threshold`   | Temperature °C bounds (default: `80` / `90`)      |
| `ssh_config`     | SSH connection details — target must have `nvidia-smi`   |

**Metrics**: `gpu_util`, `gpu_mem_pct`, `gpu_temp` (busiest GPU); `gpu<idx>_util`, `gpu<idx>_mem_pct`, `gpu<idx>_temp` (per GPU)

```yaml
- name: "GPU"
  id: "server-gpu"
  type: "gpu"
  interval: 1m
  temp_threshold: 88
  ssh_config:
    host: "server.example.com"
```

---

### `containers`
Monitors Docker or Podman containers over SSH via `<runtime> ps -a`, counting running vs. stopped containers. Paused/created containers are treated as benign. Named containers listed in `expect_running` are required — any that are missing or not running drive the status to **failed** and expose a per-container **Restart** action in the UI. Other unexpectedly-stopped containers drive **warning** (unless `stopped_warning: false`).

For safety, the restart action only ever targets containers explicitly listed in `expect_running`.

| Option            | Description                                                            |
|-------------------|------------------------------------------------------------------------|
| `runtime`         | `docker` (default) or `podman`                                         |
| `expect_running`  | *(Optional)* List of container names that must be running (→ Restart actions) |
| `stopped_warning` | Treat any stopped container as a warning (default: `true`)             |
| `ssh_config`      | SSH connection details — see [SSH Config](#ssh-config) below           |

**Metrics**: `containers_total`, `containers_running`, `containers_stopped`

```yaml
- name: "Docker"
  id: "server-docker"
  type: "containers"
  interval: 1m
  runtime: "docker"
  expect_running:
    - "nginx"
    - "postgres"
  ssh_config:
    host: "server.example.com"
```

---

### `raid`
Monitors Linux software RAID (mdadm) array health over SSH by parsing `/proc/mdstat`. Each array's `[N/M] [UU__]` status is checked: any array with a down disk (`_`) or fewer active disks than expected reports **failed**; an array undergoing resync/recovery/reshape reports **warning**; all-clean reports **online**. Complements the ZFS plugins for hosts using classic mdraid. Reports **offline** when no arrays are present.

| Option       | Description                                                  |
|--------------|--------------------------------------------------------------|
| `interval`   | Polling frequency (default: `60`; `5m` is usually plenty)   |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `arrays_total`, `arrays_ok`, `arrays_degraded`

```yaml
- name: "Software RAID"
  id: "server-raid"
  type: "raid"
  interval: 5m
  ssh_config:
    host: "server.example.com"
```

---

### `command`
The generic escape hatch: runs an arbitrary command over SSH and derives status from it, for checks that don't warrant a dedicated plugin. Two modes:

- **Exit-code mode** (no `pattern`): exit `0` → online, non-zero → failed (or warning with `nonzero_is_warning: true`).
- **Pattern mode** (`pattern` set): a regex with one capture group extracts a number from stdout, stored as the `value` metric and charted, then compared against `warning`/`threshold` — same semantics as the numeric plugins. Set `invert: true` when *lower* is worse (e.g. free space, days-until-expiry).

Every run is wrapped in `timeout` so a hung target can't stall the polling loop.

| Option              | Description                                                              |
|---------------------|--------------------------------------------------------------------------|
| `command`           | Shell command to run on the target *(required)*                         |
| `timeout`           | Per-run timeout in seconds (default: `30`)                              |
| `pattern`           | *(Optional)* Regex with one capture group extracting a number           |
| `warning` / `threshold` | Value bounds (pattern mode only)                                    |
| `invert`            | If true, values *below* the bounds are bad (default: `false`)           |
| `nonzero_is_warning`| Treat non-zero exit as warning instead of failed (default: `false`)     |
| `value_label` / `value_unit` | UI label / unit suffix for the extracted value                 |
| `ssh_config`        | SSH connection details — see [SSH Config](#ssh-config) below            |

**Metrics**: `exit_code` (always); `value` (pattern mode)

```yaml
# Pattern mode: TLS cert expiry, fewer days left is worse
- name: "Cert Expiry"
  id: "server-cert"
  type: "command"
  interval: 6h
  command: 'echo "days=$(( ($(date -d "$(openssl x509 -enddate -noout -in /etc/ssl/cert.pem | cut -d= -f2)" +%s) - $(date +%s)) / 86400 ))"'
  pattern: 'days=(-?\d+)'
  warning: 21
  threshold: 7
  invert: true
  value_label: "DAYS LEFT"
  value_unit: " d"
  ssh_config:
    host: "server.example.com"

# Exit-code mode: pending reboot -> warning
- name: "Reboot Required"
  id: "server-reboot"
  type: "command"
  interval: 1h
  command: "test ! -f /var/run/reboot-required"
  nonzero_is_warning: true
  ssh_config:
    host: "server.example.com"
```

---

### `filesystems`
Auto-discovers and monitors **every** mounted filesystem on the target over SSH via a single `df` call — no per-path configuration. This is the fleet-wide counterpart to [`disk_space`](#disk_space) (which watches one explicit path). Pseudo/virtual filesystems (tmpfs, proc, cgroup, overlay, …) are excluded so only real storage appears. Overall status is the worst usage across all filesystems.

| Option       | Description                                                  |
|--------------|--------------------------------------------------------------|
| `warning`    | Usage % that triggers warning (default: `80`)               |
| `threshold`  | Usage % that triggers failed (default: `90`)                |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `worst_used_pct`; `fs_<mount>_used_pct`, `fs_<mount>_size_gb` per filesystem

```yaml
- name: "Filesystems"
  id: "server-filesystems"
  type: "filesystems"
  interval: 5m
  warning: 80
  threshold: 90
  ssh_config:
    host: "server.example.com"
```

---

### `folders`
Monitors the size of arbitrary directories over SSH via `du` — for watching things a filesystem check can't see: a growing log directory, a download spool, a media library nearing a soft cap. Each folder may set its own `warning`/`threshold` (in GB); a folder with neither is size-only. A folder that can't be read (missing/permission/timeout) reports failed.

| Option     | Description                                                                 |
|------------|-----------------------------------------------------------------------------|
| `folders`  | List of `{ path, warning?, threshold? }` — warning/threshold are sizes in GB |
| `timeout`  | Per-`du` timeout in seconds (default: `60`)                                 |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below              |

**Metrics**: `worst_folder_gb`; `folder_<path>_gb` per folder

```yaml
- name: "Folders"
  id: "server-folders"
  type: "folders"
  interval: 1h
  folders:
    - path: "/var/log"
      warning: 5
      threshold: 10
    - path: "/srv/media"   # size-only
  ssh_config:
    host: "server.example.com"
```

---

### `vms`
Monitors libvirt/KVM virtual machines over SSH via `virsh list --all`, counting running vs. off. Domains in an error state (paused, crashed) drive warning; "shut off" is treated as benign. Named domains in `expect_running` are required — any not running drives status to **failed** and exposes per-VM **Start**/**Shutdown** actions (restricted to listed domains for safety).

| Option           | Description                                                       |
|------------------|-------------------------------------------------------------------|
| `uri`            | libvirt connection URI (default: `qemu:///system`)               |
| `expect_running` | *(Optional)* Domain names that must be running (→ Start/Shutdown) |
| `offline_warning`| Any error-state domain => warning (default: `true`)              |
| `ssh_config`     | SSH connection details — see [SSH Config](#ssh-config) below      |

**Metrics**: `vms_total`, `vms_running`, `vms_stopped`

```yaml
- name: "Virtual Machines"
  id: "server-vms"
  type: "vms"
  interval: 1m
  expect_running:
    - "web"
  ssh_config:
    host: "server.example.com"
```

---

### `cloud`
Detects the cloud provider of the target and surfaces its instance metadata (id, type, region/zone) over SSH via the link-local metadata endpoint (`169.254.169.254`). Auto-detects across AWS (IMDSv2), GCP, and Azure, or query one provider explicitly. Informational — no thresholds; reports online when metadata is reachable, offline when the host isn't on a recognized cloud.

| Option       | Description                                                   |
|--------------|--------------------------------------------------------------|
| `provider`   | `auto` (default), `aws`, `gcp`, or `azure`                   |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `on_cloud` (1 = on a recognized cloud, 0 = not)

```yaml
- name: "Instance Metadata"
  id: "server-cloud"
  type: "cloud"
  interval: 15m
  provider: "auto"
  ssh_config:
    host: "server.example.com"
```

---

### `group`
A logical container for other monitors. Aggregates the worst-case status of all descendants and displays each child as a collapsible card. Expansion state is preserved across page refreshes within the same server session.

Groups support a CSS grid layout, configurable at both the group level and per-child.

| Option          | Description                                                                                  |
|-----------------|----------------------------------------------------------------------------------------------|
| `children`      | A list of nested plugin definitions                                                           |
| `grid_columns`  | Number of equal-width columns in the grid (default: `1` — full-width stacked layout)         |

Each child entry can also set:

| Child Option     | Description                                                                                  |
|------------------|----------------------------------------------------------------------------------------------|
| `grid_col_span`  | How many grid columns this child occupies (default: `1`)                                     |
| `grid_height`    | Explicit CSS height for the child cell, e.g. `"400px"` (default: auto). Adds a scrollbar if content overflows. |

Groups can be nested to arbitrary depth. Inner groups inherit their own `grid_columns` independently.

```yaml
- name: "System Stats"
  type: "group"
  grid_columns: 3       # 3 equal columns — one subgroup per host
  children:
    - name: "Ragnarok System"
      type: "group"
      grid_columns: 4   # 4 columns — one card per stat (CPU / Mem / Temp / Load)
      children:
        - name: "Ragnarok CPU"
          type: "cpu_usage"
          ...
        - name: "Ragnarok Memory"
          type: "memory_usage"
          ...

# Child spanning multiple columns
- name: "Overview"
  type: "group"
  grid_columns: 3
  children:
    - name: "Processes"
      type: "processes"
      grid_col_span: 2   # spans 2 of 3 columns
      grid_height: "600px"
      ...
    - name: "Uptime"
      type: "uptime"
      ...
```

---

### Plugin Layout

Every leaf plugin supports a `layout:` key that controls how its widgets are arranged on the detail page. Without a `layout:` block the plugin uses its built-in default grid (defined in the plugin's `_DEFAULT_LAYOUT`).

| `layout` option  | Description                                                                                          |
|------------------|------------------------------------------------------------------------------------------------------|
| `grid_columns`   | Number of equal-width columns in this plugin's detail grid. Defaults vary by plugin type.            |

Each named widget within a plugin can be overridden:

| Per-widget option | Description                                                                                         |
|-------------------|-----------------------------------------------------------------------------------------------------|
| `col`             | Start column (1-based). Omit to use CSS auto-placement.                                             |
| `row`             | Start row (1-based). Omit to use CSS auto-placement.                                                |
| `col_span`        | How many columns this widget occupies (default: `1`).                                               |
| `row_span`        | How many rows this widget occupies (default: `1`).                                                  |
| `height`          | Explicit CSS height for this cell, e.g. `"400px"`. Adds a scrollbar on overflow (default: auto).   |
| `visible`         | `false` to hide the widget entirely (default: `true`).                                              |

**Widget names by plugin type:**

| Plugin             | Widget names                                                                 |
|--------------------|------------------------------------------------------------------------------|
| `uptime`           | `host_card`, `status_card`, `latency_card`, `chart`, `logs`                 |
| `systemd_service`  | `host_card`, `service_card`, `status_card`, `time_card`, `logs` *(continuous)* / `host_card`, `service_card`, `maxage_card`, `state_card`, `history`, `logs` *(oneshot)* |
| `cpu_usage`        | `host_card`, `cpu_card`, `chart`, `logs`                                    |
| `memory_usage`     | `host_card`, `mem_pct_card`, `mem_used_card`, `chart`, `logs`               |
| `temperature`      | `host_card`, `temp_card`, `chart`, `logs`                                   |
| `load_average`     | `host_card`, `load_1m_card`, `load_5m_card`, `load_15m_card`, `chart`, `logs` |
| `processes`        | `host_card`, `count_card`, `top_cpu_card`, `table`, `logs`                  |
| `network_usage`    | `host_card`, `iface_card`, `rx_card`, `tx_card`, `rx_chart`, `tx_chart`, `logs` |
| `smart_disk`       | `host_card`, `total_card`, `ok_card`, `failed_card`, `logs`                 |
| `disk_space`       | `host_card`, `path_card`, `threshold_card`, `usage_card`, `avail_card`, `total_card`, `chart`, `logs` |
| `zfs_health`       | `host_card`, `total_card`, `ok_card`, `degraded_card`, `logs`               |
| `zfs_pool`         | `host_card`, `pool_card`, `usage_card`, `threshold_card`, `chart`, `logs`   |

**Examples:**

```yaml
# Make the chart taller and hide the logs panel
- name: "Ragnarok CPU"
  type: "cpu_usage"
  layout:
    chart:
      height: "500px"
    logs:
      visible: false

# Custom 3-column grid: stat cards left, chart occupies right two columns
- name: "Heimdall Memory"
  type: "memory_usage"
  layout:
    grid_columns: 3
    host_card:
      col: 1
      row: 1
    mem_pct_card:
      col: 1
      row: 2
    mem_used_card:
      col: 1
      row: 3
    chart:
      col: 2
      row: 1
      col_span: 2
      row_span: 3
    logs:
      col: 1
      row: 4
      col_span: 3
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

## Integrations

Beyond the dashboard, Vigil exposes its state to external tools. All of the following are served on the same port as the web UI.

### Events feed

An **Events** view in the sidebar shows a unified, filterable feed of every event Vigil has recorded across all monitors — status changes, threshold crossings, and collection errors — filterable by level, target host, and message text.

### REST API

Read-only JSON endpoints for consuming Vigil's state programmatically:

| Endpoint | Returns |
|----------|---------|
| `GET /api/health` | `{"status": "ok"}` |
| `GET /api/monitors` | All monitors with id, name, type, target, and current status |
| `GET /api/monitors/{id}` | A single monitor plus its latest metrics |
| `GET /api/metrics` | Latest value of every collected metric |
| `GET /api/events` | Recent events — supports `?level=`, `?target=`, `?search=`, `?limit=` |

```bash
curl http://localhost:8080/api/monitors
curl "http://localhost:8080/api/events?level=ERROR&limit=50"
```

### Prometheus

A Prometheus exposition endpoint is always available at `GET /metrics` (pull) — no configuration required. It exports `vigil_up` (per-monitor status: `1` online, `0.5` warning, `0` failed, `-1` offline) and `vigil_metric` (every collected metric, labeled by monitor/target/metric). Point a Prometheus scrape config at it:

```yaml
scrape_configs:
  - job_name: vigil
    static_configs:
      - targets: ['vigil-host:8080']
```

### InfluxDB

An optional **push** exporter ships metrics to InfluxDB (1.x or 2.x) on an interval. Enable it under `exporters:` in `config.yaml`:

```yaml
exporters:
  influxdb:
    url: "http://localhost:8086"
    interval: 30
    # InfluxDB 2.x:
    org: "my-org"
    bucket: "vigil"
    token: "my-api-token"
    # InfluxDB 1.x: use `database:` instead of org/bucket/token
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
- [x] GPU monitor (NVIDIA util/VRAM/temperature via `nvidia-smi`)
- [x] Container monitor (Docker/Podman, with per-container restart)
- [x] Software RAID (mdadm) health monitor
- [x] Generic command monitor (arbitrary check, exit-code or regex-extracted value)
- [x] Filesystem auto-discovery monitor (all mounts via `df`)
- [x] Folder size monitor (arbitrary directories via `du`)
- [x] VM monitor (libvirt/KVM via `virsh`, with start/shutdown)
- [x] Cloud instance metadata monitor (AWS/GCP/Azure)
- [x] Unified, filterable events feed
- [x] REST API for monitors, metrics, and events
- [x] Prometheus `/metrics` export endpoint (pull)
- [x] InfluxDB export (push, 1.x and 2.x)
- [ ] Basic alerting (Email, Slack, or Webhook)

---

## Credits

- App icon: [Guard Protection Safe 3](https://www.svgrepo.com/svg/421980/guard-protection-safe-3) from [SVG Repo](https://www.svgrepo.com)

---

## License

GPL 3.0