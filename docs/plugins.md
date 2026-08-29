# Vigil Plugin Reference

Every monitor type Vigil ships, with its config keys, metrics, actions, and
config examples. For what Vigil is and how to run it, see the
[README](../README.md); for agent setup, see [Agent](agent.md).

## Contents

- [Summary](#summary) — the one-line table of every plugin type
- Plugin types:
  [`uptime`](#uptime) ·
  [`push`](#push) ·
  [`http`](#http) ·
  [`dns_record`](#dns_record) ·
  [`ddns_updater`](#ddns_updater) ·
  [`systemd_service`](#systemd_service) ·
  [`service_list`](#service_list) ·
  [`smart`](#smart) ·
  [`zfs`](#zfs) ·
  [`md`](#md) ·
  [`disk_io`](#disk_io) ·
  [`disk_space`](#disk_space) ·
  [`cpu`](#cpu) ·
  [`memory`](#memory) ·
  [`load`](#load) ·
  [`temperature`](#temperature) ·
  [`interrupts`](#interrupts) ·
  [`gpu`](#gpu) ·
  [`oom`](#oom) ·
  [`processes`](#processes) ·
  [`throughput`](#throughput) ·
  [`connections`](#connections) ·
  [`wifi`](#wifi) ·
  [`containers`](#containers) ·
  [`command`](#command) ·
  [`filesystems`](#filesystems) ·
  [`folders`](#folders) ·
  [`vms`](#vms) ·
  [`cloud`](#cloud) ·
  [`group`](#group)
- [Plugin Layout](#plugin-layout) — arranging a plugin's widgets on its page
- [SSH Config](#ssh-config) — the `ssh_config` block SSH-based plugins accept

---

## Plugin Types

### Summary

| Type | Monitors | Collection | Key metrics | Actions |
|------|----------|------------|-------------|---------|
| [`uptime`](#uptime)                     | Host reachability                     | ICMP ping                                        | `up`, `latency_ms`                              | — |
| [`push`](#push)                         | External heartbeat (dead man's switch) | REST API (caller pushes in)                     | `last_push_epoch`, `reported_up`, `value`       | — |
| [`http`](#http)                         | HTTP(S)/WebSocket endpoint health     | HTTP from the Vigil host, or `websocat` over SSH | `probe_ok`, `probe_status`, `probe_latency_ms`  | — |
| [`dns_record`](#dns_record)             | DNS record resolution                 | DNS query (via dnspython, in-process)            | `resolved`, `ttl`, `matches_expected`            | — |
| [`ddns_updater`](#ddns_updater)         | Dynamic DNS record kept current       | Public IP lookup + DNS query (in-process)        | `in_sync`, `last_update_epoch`                   | Force Update |
| [`systemd_service`](#systemd_service)   | systemd unit state / last run         | SSH (`systemctl`)                                | `active` *or* `last_run_epoch`, `last_run_success` | Restart, Stop, Enable, Disable |
| [`service_list`](#service_list)         | Systemd unit browser and control      | SSH (`systemctl`)                                | `services_total`, `services_active`, `services_failed` | Start, Stop, Restart, Enable, Disable, View Status |
| [`smart`](#smart)                       | SMART health of every physical disk    | SSH (`smartctl`)                                | `disks_total`, `disks_ok`, `disks_failed`       | — |
| [`zfs`](#zfs)                           | ZFS pool state and capacity            | SSH (`zpool list`)                              | `pools_total`, `pools_degraded`, `zfs_usage_max` | — |
| [`md`](#md)                             | mdadm array health                     | SSH (`/proc/mdstat`)                            | `arrays_total`, `arrays_ok`, `arrays_degraded`  | — |
| [`disk_io`](#disk_io)                   | Disk read/write throughput             | SSH (`/proc/diskstats`)                         | `read_kbps`, `write_kbps`                       | — |
| [`disk_space`](#disk_space)             | Filesystem usage for a path           | SSH (`df`)                                       | `used_pct`, `size_gb`, `used_gb`, `avail_gb`    | — |
| [`cpu`](#cpu)                           | CPU utilization                        | SSH (`/proc/stat`)                              | `cpu_pct`                                       | — |
| [`memory`](#memory)                     | Memory and swap use                    | SSH (`/proc/meminfo`)                           | `memory_pct`, `memory_used_gb`                  | — |
| [`load`](#load)                         | Load average, scaled by core count     | SSH (`/proc/loadavg`)                           | `load_pct_1m`, `load_pct_5m`, `load_pct_15m`    | — |
| [`temperature`](#temperature)           | Thermal zone temperatures              | SSH (`/sys/class/thermal`)                      | `temp_c`, `temp_zone_<zone>`                    | — |
| [`interrupts`](#interrupts)             | Interrupt and context-switch rates     | SSH (`/proc/stat`)                              | `irq_per_sec`, `ctxt_per_sec`                   | — |
| [`gpu`](#gpu)                           | NVIDIA GPU utilization, memory, temperature | SSH (`nvidia-smi`)                         | `gpu_util`, `gpu_mem_pct`, `gpu_temp`           | — |
| [`oom`](#oom)                           | Kernel OOM kills                       | SSH (`/proc/vmstat`) + agent journal push       | `oom_kills_total`, `oom_kills_new`              | — |
| [`processes`](#processes)               | Running processes by CPU              | SSH (`ps`)                                       | `process_count`, `top_cpu_pct` *(ephemeral)*    | SIGTERM, SIGKILL |
| [`throughput`](#throughput)             | Network interface throughput           | SSH (`/proc/net/dev`)                           | `rx_kbps`, `tx_kbps`                            | — |
| [`connections`](#connections)           | TCP connection counts by state         | SSH (`/proc/net/tcp`)                           | `conn_total`, `conn_established`, `conn_listen` | — |
| [`wifi`](#wifi)                         | WiFi link quality and signal strength  | SSH (`/proc/net/wireless`)                      | `link_quality`, `signal_dbm`                    | — |
| [`ports`](#ports)                       | TCP port / URL reachability           | SSH (`/dev/tcp`, `curl`)                         | `<check>_up`, `<check>_latency_ms`              | — |
| [`borg`](#borg)                         | Borg backup freshness                 | SSH (`borg list`)                                | `archive_count`, `last_backup_epoch`            | — |
| [`containers`](#containers)             | Docker / Podman container states      | SSH (`docker`/`podman ps`)                       | `containers_total`, `containers_running`, `containers_stopped` | Restart (per expected container) |
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
| `type`   | Plugin type — one of `uptime`, `push`, `http`, `dns_record`, `ddns_updater`, `systemd_service`, `service_list`, `cpu`, `memory`, `load`, `temperature`, `interrupts`, `gpu`, `oom`, `throughput`, `connections`, `wifi`, `smart`, `zfs`, `md`, `disk_io`, `disk_space`, `ports`, `processes`, `borg`, `containers`, `command`, `filesystems`, `folders`, `vms`, `cloud`, `group` |
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

### `push`
The inverse of every other monitor: instead of Vigil reaching out to a target, an external script, cron job, or task with no fixed host calls Vigil's REST API to say "I'm alive." Vigil reports `failed` once a heartbeat hasn't arrived within `max_age` — a dead man's switch, not a poll.

`interval` controls how often Vigil *checks* for staleness, not how often heartbeats are expected — that's `max_age`'s job. A missing `max_age` defaults to twice the interval, tolerating one missed beat before alarming.

| Option      | Description                                                                 |
|-------------|------------------------------------------------------------------------------|
| `max_age`   | Seconds since the last heartbeat before reporting `failed` (default: `interval * 2`) |
| `token`     | Shared secret the caller must present when pushing. **Required** — without one, anyone who can reach the API could mark this monitor healthy. Generate with `openssl rand -hex 20`. |
| `interval`  | How often the staleness check itself runs (default: `60`)                    |

**Metrics**: `last_push_epoch` (Unix timestamp of the last heartbeat), `reported_up` (1/0, the caller's own status), `value` (optional, if the caller supplies one)

To push a heartbeat, hit `GET` or `POST /api/push/{id}/{token}`, optionally with `status` (`up` or `down`, default `up`), `msg`, and `value` query parameters. This endpoint is not covered by the dashboard's HTTP Basic Auth (see [Authentication](../README.md#authentication)) — the per-monitor token is its credential instead.

```yaml
- name: "Nightly Backup Job"
  id: "nightly-backup"
  type: "push"
  interval: 1h
  max_age: 26h   # daily job, tolerate one slipped run
  token: "a1b2c3d4e5f6..."   # openssl rand -hex 20
```

```bash
# At the end of the cron job:
curl "https://vigil.example.com/api/push/nightly-backup/a1b2c3d4e5f6...?status=up"

# Or report a failure the job detected itself, while still checking in on time:
curl "https://vigil.example.com/api/push/nightly-backup/a1b2c3d4e5f6...?status=down&msg=disk+full"
```

---

### `http`
Generic endpoint probe: is this URL reachable and answering sanely, and how fast. Covers the thin "GET this URL with this auth, check the body looks right" service checks that don't warrant a dedicated plugin. Two probe kinds, selected by the URL scheme:

- **`http://` / `https://`** — the request runs from the Vigil host (the URL must be reachable from it), with configurable method, headers, body and basic auth. Latency is recorded and charted.
- **`ws://` / `wss://`** — the probe pipes `body` into `websocat` on the *target* over SSH (websocat must be installed there); on agent-backed hosts the agent samples it locally. Success is a reply matching `expect`.

A reply matching every expectation is online; anything else — a connection failure, an unexpected status, or a body that fails the `expect` checks (e.g. a login page served with a 200) — is failed. There is no warning tier.

| Option              | Description                                                              |
|---------------------|--------------------------------------------------------------------------|
| `url`               | Endpoint to probe *(required)*                                          |
| `method`            | HTTP method (default: `GET`)                                            |
| `headers`           | *(Optional)* Extra request headers                                      |
| `body`              | *(Optional)* Request body, or the text sent into the websocket          |
| `username`          | *(Optional)* Basic-auth username (http only)                            |
| `password` / `password_command` | Basic-auth secret, literal or resolved once at startup      |
| `request_timeout`   | Probe timeout in seconds (default: `10`)                                |
| `check_title`       | Card and chart title in the UI (default: `PROBE`)                       |
| `expect.status`     | Accepted HTTP status, int or list (default: `200`)                      |
| `expect.body_contains` | String or list — all must appear in the body, case-insensitive       |
| `expect.body_contains_any` | String or list — at least one must appear, case-insensitive      |

**Metrics**: `probe_ok` (always); `probe_status`, `probe_latency_ms` (http only)

```yaml
# An OPDS feed behind basic auth: a 200 that is actually a login page fails
- name: "Calibre-Web"
  type: "http"
  url: "http://books.example.com:8083/opds"
  username: "vigil"
  password_command: "cat /run/secrets/calibre-vigil"
  check_title: "OPDS FEED"
  expect:
    body_contains: "<feed"
    body_contains_any: ["atom", "opds"]

# A CalDAV server: PROPFIND with the 207 Multi-Status reply as success
- name: "Radicale"
  type: "http"
  url: "https://dav.example.com/"
  method: "PROPFIND"
  headers: { "Depth": "0", "Content-Type": "application/xml" }
  body: '<?xml version="1.0"?><propfind xmlns="DAV:"><prop><current-user-principal/></prop></propfind>'
  username: "vigil"
  password_command: "cat /run/secrets/radicale-vigil"
  check_title: "PROPFIND"
  expect:
    status: 207

# A websocket service, probed on the target itself (needs websocat there)
- name: "OpenBooks"
  type: "http"
  url: "ws://127.0.0.1:9777/ws"
  body: '{"type":1,"payload":{}}'
  check_title: "IRC BRIDGE"
  expect:
    body_contains: '"appearance":1'
  ssh_config:
    host: "server.example.com"
```

---

### `dns_record`
Resolves a DNS record and reports failed on NXDOMAIN, no answer, a timeout, or (when `expected` is set) an answer outside the accepted values — catching a stale record, a botched migration, or a DNS provider outage.

Runs in-process via [dnspython](https://www.dnspython.org/) rather than over SSH: there is no target host, only a domain to ask about. Query the system resolver by default, or point `resolver` at a specific one (public or internal) — doing so doubles as a liveness probe for that resolver, distinct from [`unbound`](#unbound)'s SERVFAIL-rate monitoring of one resolver's own stats.

| Option        | Description                                                                 |
|---------------|-------------------------------------------------------------------------------|
| `domain`      | Domain name to query *(required)*                                            |
| `record_type` | One of `A`, `AAAA`, `CNAME`, `MX`, `TXT`, `NS`, `SOA` (default: `A`)          |
| `resolver`    | Resolver IP to query directly (default: system resolver)                     |
| `port`        | Resolver port (default: `53`)                                                |
| `timeout`     | Query timeout in seconds (default: `5`)                                      |
| `expected`    | *(Optional)* List of acceptable answer values. Any answer outside this list fails the monitor. Order-independent — only presence in the answer set is checked. |

**Metrics**: `resolved` (1/0), `ttl` (seconds), `matches_expected` (1/0, only when `expected` is set)

```yaml
# Pin an A record to known IPs — fails if it ever points elsewhere
- name: "Website A Record"
  id: "website-a-record"
  type: "dns_record"
  domain: "example.com"
  record_type: "A"
  expected:
    - "93.184.216.34"
  interval: 5m

# Confirm MX still points at the expected mail provider
- name: "Mail Routing"
  id: "example-mx"
  type: "dns_record"
  domain: "example.com"
  record_type: "MX"
  expected:
    - "10 mail.example.com"
  interval: 1h

# Query a specific resolver directly, e.g. to check an internal DNS server
- name: "Internal DNS"
  id: "internal-dns-check"
  type: "dns_record"
  domain: "heimdall.technet"
  resolver: "10.0.0.1"
  interval: 1m
```

---

### `ddns_updater`
Keeps a DNS record pointed at this network's current public IP, and reports on it while doing so — a built-in replacement for standalone dynamic-DNS-updater services. Each cycle: look up the current public IP, resolve what the domain currently answers publicly, and push an update to the provider only when the two differ. Because a provider update is a real side effect (and most providers rate-limit or ban accounts that call too often), it is never fired on a fixed schedule — only on detected drift, and even then no more often than `min_interval`.

Currently speaks FreeDNS's (afraid.org, including `*.mooo.com` and its other free subdomains) per-host dynamic update URL convention: a plain HTTPS GET to a secret, account-specific URL that updates the record to the caller's apparent IP, responding `good <ip>` or `nochg <ip>` on success. Other providers using the same "secret update URL" convention work too; anything returning JSON or requiring a signed request does not, yet.

Resolves the public record against an explicit `resolver` (default `8.8.8.8`) rather than the local/default resolver — a local network commonly has a hosts-file override pinning this exact hostname to a LAN IP (so internal clients don't route out to the internet and back for it), which would mask real DDNS drift by always answering with that LAN IP instead.

| Option        | Description                                                                 |
|---------------|-------------------------------------------------------------------------------|
| `domain`      | Domain whose public record is kept current *(required)*                      |
| `update_url`  | Provider's per-host dynamic update URL, including its own secret token       |
| `update_url_file` | Path to a file containing the update URL (keeps the token out of config.yaml) |
| `update_url_command` | Shell command whose stdout is the update URL                         |
| `resolver`    | Resolver IP to check the current public record against (default: `8.8.8.8`)  |
| `record_type` | Record type being kept current (default: `A`)                                |
| `timeout`     | Timeout in seconds for both the IP lookup and the update request (default: `10`) |
| `min_interval`| Minimum seconds between update attempts regardless of how often `interval` ticks (default: `300`) |

Precedence when more than one update-URL source is set: `update_url` > `update_url_file` > `update_url_command`.

**Metrics**: `in_sync` (1/0), `last_update_epoch` (Unix timestamp of the last successful push)

**Actions**: Force Update — pushes an update immediately regardless of detected drift, bypassing `min_interval`.

```yaml
- name: "DDNS"
  id: "ddns-bltechnet"
  type: "ddns_updater"
  domain: "bltechnet.mooo.com"
  update_url_file: "/run/secrets/freedns_update_url"
  interval: 5m
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
| `allow_unit_file_edit` | Enable UI editing of the target service unit file. Defaults to `false`. |
| `allowed_write_paths` | Optional list of absolute paths where unit file writes are permitted. Defaults to standard systemd unit paths. |

**Continuous metrics**: `active` (1/0)

**Oneshot metrics**: `last_run_epoch` (Unix timestamp), `last_run_success` (1/0)

**Actions**: Restart Service, Stop Service, Enable on Boot, Disable on Boot

The service detail UI also includes:

* `View Unit File` for the configured service
* `Reload Daemon` to run `systemctl daemon-reload`
* `Edit Unit File` when `allow_unit_file_edit: true` is enabled

> Note: Remote editing requires passwordless sudo access for the service commands and the configured write helper (currently `python3` on the target). Restrict `allowed_write_paths` carefully.

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

### `service_list`
Lists all systemd services on a host and provides a sortable service browser with control actions.

| Option                | Description                                                                                       |
|----------------------|---------------------------------------------------------------------------------------------------|
| `lines`              | Number of recent log lines to preserve for this plugin's internal event log (default: `10`)        |
| `interval`           | Polling frequency in seconds (default: `60`)                                                      |
| `ssh_config`         | SSH connection details — see [SSH Config](#ssh-config) below                                      |
| `allow_unit_file_edit` | Allow editing of unit files from the UI (disabled by default).                                    |
| `allowed_write_paths` | Optional list of absolute paths where unit file writes are permitted. Defaults to standard systemd unit paths. |

**Metrics**: `services_total`, `services_active`, `services_failed`

**Actions**: Reload Daemon

The service browser renders a sortable table of all units, and offers per-unit actions for:

* Start Service
* Stop Service
* Restart Service
* Enable on Boot
* Disable on Boot
* View Status
* View Unit File
* Edit Unit File (when enabled)

```yaml
- name: "Systemd Service Browser"
  id: "systemd-service-browser"
  type: "service_list"
  interval: 60
  ssh_config:
    host: "web-01.example.com"
  allow_unit_file_edit: true
  allowed_write_paths:
    - /etc/systemd/system
```

```yaml
# Continuous with unit file editing enabled
- name: "Nginx"
  id: "nginx-service"
  type: "systemd_service"
  service_name: "nginx.service"
  interval: 60
  ssh_config:
    host: "web-01.example.com"
  allow_unit_file_edit: true
  allowed_write_paths:
    - /etc/systemd/system
```

---

### `smart`
SMART health of every physical disk on a host, via `smartctl`. Classification is by positive assertion: only an explicit `PASSED` verdict counts a disk as healthy, so a check that could not run reads as **failed** rather than as a clean disk. Virtual block devices (zram, ZFS zvols, loop/md/device-mapper nodes) are filtered out before probing, and a device that genuinely has no SMART support is skipped rather than counted.

> Needs passwordless `sudo` access to `smartctl` for the SSH user (e.g. `vigil ALL=(ALL) NOPASSWD: /usr/bin/smartctl`).

| Option       | Description                                                                 |
|--------------|-----------------------------------------------------------------------------|
| `interval`   | Polling frequency (default: `60`). `smartctl` is slow and its answer changes rarely — `1h` is a sensible setting. |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below                |

**Metrics**: `disks_total`, `disks_ok`, `disks_failed`

**Status**: `failed` if any disk fails or cannot be read, `offline` if the host has no SMART-capable disk, otherwise `online`.

```yaml
- name: "SMART"
  id: "ragnarok-smart"
  type: "smart"
  interval: 1h
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `zfs`
ZFS pool state and capacity, via `zpool list`. Every pool's health and used percentage is read each cycle; per-pool usage is charted individually as well as rolled up.

| Option       | Description                                                                 |
|--------------|-----------------------------------------------------------------------------|
| `pools`      | Pool names to query (default: every pool on the host)                       |
| `warning`    | Pool usage % that triggers `warning` (default: `80`)                        |
| `threshold`  | Pool usage % that triggers `failed` (default: `90`)                         |
| `interval`   | Polling frequency (default: `60`; `1h` suits a pool that changes slowly)    |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below                |

**Metrics**: `pools_total`, `pools_ok`, `pools_degraded`, `zfs_usage_max`, `pool_usage_<pool>`

**Status**: `failed` for any pool not `ONLINE`, or usage at `threshold`; `warning` above `warning`; `offline` if the host has no pools.

```yaml
- name: "ZFS"
  id: "ragnarok-zfs"
  type: "zfs"
  interval: 1h
  warning: 80
  threshold: 90
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `md`
Linux software RAID health, read from `/proc/mdstat` — the mdadm sibling of [`zfs`](#zfs). Counts arrays that are clean, degraded or rebuilding.

| Option       | Description                                                  |
|--------------|--------------------------------------------------------------|
| `interval`   | Polling frequency (default: `60`)                            |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `arrays_total`, `arrays_ok`, `arrays_degraded`

**Status**: `failed` for a degraded array, `warning` while one is rebuilding, `offline` on a host with no arrays.

```yaml
- name: "RAID"
  id: "ragnarok-md"
  type: "md"
  interval: 10m
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `disk_io`
Disk read/write throughput, from two `/proc/diskstats` samples a second apart taken on the target. With no `device`, it auto-detects the busiest whole disk (ignoring partitions and virtual devices) and persists the choice, showing it on the card.

| Option       | Description                                                              |
|--------------|--------------------------------------------------------------------------|
| `device`     | Block device to measure, e.g. `sda` (default: auto-detect the busiest)    |
| `interval`   | Polling frequency (default: `60`, recommend `30s`)                        |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below             |

**Metrics**: `read_kbps`, `write_kbps`

```yaml
- name: "Disk I/O"
  id: "ragnarok-disk-io"
  type: "disk_io"
  interval: 30s
  ssh_config:
    host: "ragnarok.example.com"
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

### `cpu`
CPU utilization from two `/proc/stat` samples a second apart, taken on the target so the sleep costs one round trip rather than two.

| Option       | Description                                                  |
|--------------|--------------------------------------------------------------|
| `warning`    | CPU % that triggers `warning` (default: `70`)                |
| `threshold`  | CPU % that triggers `failed` (default: `85`)                 |
| `interval`   | Polling frequency (default: `60`)                            |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `cpu_pct`

```yaml
- name: "CPU"
  id: "ragnarok-cpu"
  type: "cpu"
  interval: 1m
  warning: 70
  threshold: 85
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `memory`
Memory use from `/proc/meminfo`, reported as the share of total memory that is *unavailable* — the number that matters, since cache is reclaimable.

| Option       | Description                                                  |
|--------------|--------------------------------------------------------------|
| `warning`    | Memory % that triggers `warning` (default: `75`)             |
| `threshold`  | Memory % that triggers `failed` (default: `90`)              |
| `interval`   | Polling frequency (default: `60`)                            |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `memory_pct`, `memory_used_gb`, `memory_total_gb`

```yaml
- name: "Memory"
  id: "ragnarok-memory"
  type: "memory"
  interval: 1m
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `load`
Load average from `/proc/loadavg`, normalized by core count so 100% means the host is exactly at capacity. Thresholds are optional: without both, load is collected and charted but never changes the status — useful on a box where a high load average is normal.

| Option       | Description                                                              |
|--------------|--------------------------------------------------------------------------|
| `warning`    | 1m load as % of cores that triggers `warning` (optional)                  |
| `threshold`  | 1m load as % of cores that triggers `failed` (optional)                   |
| `interval`   | Polling frequency (default: `60`)                                         |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below             |

**Metrics**: `load_pct_1m`, `load_pct_5m`, `load_pct_15m`, `load_1m`, `cpu_count`

```yaml
- name: "Load"
  id: "ragnarok-load"
  type: "load"
  interval: 1m
  warning: 70
  threshold: 100
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `temperature`
Every thermal zone under `/sys/class/thermal`. The hottest zone sets the status; each zone is also kept as its own metric and chip, so a single hot sensor is identifiable rather than hidden in a maximum. A host with no zones (a VM, typically) stays `online` with no metric rather than reporting a problem it cannot see.

| Option       | Description                                                  |
|--------------|--------------------------------------------------------------|
| `warning`    | °C that triggers `warning` (default: `70`)                   |
| `threshold`  | °C that triggers `failed` (default: `80`)                    |
| `interval`   | Polling frequency (default: `60`)                            |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `temp_c`, `temp_zone_<zone>`

```yaml
- name: "Temperature"
  id: "ragnarok-temperature"
  type: "temperature"
  interval: 1m
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `interrupts`
Interrupt and context-switch rates, from two `/proc/stat` snapshots a second apart. Takes its own sample rather than sharing the [`cpu`](#cpu) monitor's, so a host can run either without the other.

| Option       | Description                                                       |
|--------------|-------------------------------------------------------------------|
| `warning`    | Interrupts/sec that triggers `warning` (default: `20000`)         |
| `threshold`  | Interrupts/sec that triggers `failed` (default: `50000`)          |
| `interval`   | Polling frequency (default: `60`)                                 |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below      |

**Metrics**: `irq_per_sec`, `ctxt_per_sec`

```yaml
- name: "Interrupts"
  id: "ragnarok-interrupts"
  type: "interrupts"
  interval: 1m
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `gpu`
NVIDIA GPU utilization, memory and temperature via `nvidia-smi`. The peak across cards sets the status; every card is also kept individually.

A GPU that sleeps with a laptop lid can wedge `nvidia-smi` uninterruptibly. After `timeout_trip` consecutive timeouts the monitor stops issuing the probe entirely for `suspend_seconds` and reports `offline` rather than stranding a process per cycle.

| Option            | Description                                                       |
|-------------------|-------------------------------------------------------------------|
| `util_warning`    | GPU utilization % that triggers `warning` (default: `85`)         |
| `util_threshold`  | GPU utilization % that triggers `failed` (default: `95`)          |
| `mem_warning`     | GPU memory % that triggers `warning` (default: `85`)              |
| `mem_threshold`   | GPU memory % that triggers `failed` (default: `95`)               |
| `temp_warning`    | °C that triggers `warning` (default: `80`)                        |
| `temp_threshold`  | °C that triggers `failed` (default: `90`)                         |
| `timeout_trip`    | Consecutive timeouts before the probe is suspended (default: `2`) |
| `suspend_seconds` | How long the probe stays suspended (default: `1800`)              |
| `ssh_config`      | SSH connection details — see [SSH Config](#ssh-config) below      |

**Metrics**: `gpu_util`, `gpu_mem_pct`, `gpu_temp`, `gpu<n>_util`, `gpu<n>_mem_pct`, `gpu<n>_temp`

```yaml
- name: "GPU"
  id: "odin-gpu"
  type: "gpu"
  interval: 1m
  temp_threshold: 88
  ssh_config:
    host: "odin.example.com"   # target needs nvidia-smi
```

---

### `oom`
Kernel OOM kills, from `/proc/vmstat`'s `oom_kill` counter. An OOM kill is an event, not a level: memory is back to normal before the next sample, so the [`memory`](#memory) monitor cannot see it. The counter is read every cycle so no kill is ever missed, and the alert is held for `alert_for` cycles afterwards so a kill is still visible when you look.

On an agent-backed host the monitor also follows the kernel journal, so a kill is reported the moment it happens and names the process the counter can only total.

| Option       | Description                                                                |
|--------------|----------------------------------------------------------------------------|
| `is_warning` | Report a kill as `warning` instead of `failed` (default: `false`)          |
| `alert_for`  | Collections a kill keeps the monitor alerting (default: `3`)               |
| `interval`   | Polling frequency (default: `60`)                                           |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below               |

**Metrics**: `oom_kills_total`, `oom_kills_new`

```yaml
- name: "OOM Kills"
  id: "ragnarok-oom"
  type: "oom"
  interval: 1m
  alert_for: 3
  ssh_config:
    host: "ragnarok.example.com"
```
---

### `processes`
Monitors running processes over SSH via `ps`, sorted by CPU usage. Process data is ephemeral and stored in memory only — not persisted to the database. Per-row SIGTERM and SIGKILL buttons are available directly in the UI.

| Option          | Description                                                                  |
|-----------------|------------------------------------------------------------------------------|
| `max_processes` | Maximum number of processes to display (default: `20`)                       |
| `require_sudo`  | Prefix kill commands with `sudo` (default: `false`)                          |
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

### `throughput`
Network interface throughput, from two `/proc/net/dev` samples a second apart. With no `interface`, it auto-detects the non-virtual interface with the highest cumulative byte count (ignoring `lo`, `veth`, `docker`, `virbr`, `br-`, `tun`, `tap` prefixes) and persists the choice, showing it on the card.

| Option       | Description                                                              |
|--------------|--------------------------------------------------------------------------|
| `interface`  | Interface to measure, e.g. `eth0` (default: auto-detect the busiest)      |
| `interval`   | Polling frequency (default: `60`, recommend `30s`)                        |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below             |

**Metrics**: `rx_kbps`, `tx_kbps`

```yaml
- name: "Throughput"
  id: "ragnarok-throughput"
  type: "throughput"
  interval: 30s
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `connections`
TCP connection counts by state, read from `/proc/net/tcp`. Thresholds apply to the total, which is what catches a connection leak or a flood.

| Option       | Description                                                  |
|--------------|--------------------------------------------------------------|
| `warning`    | Total connections that trigger `warning` (default: `500`)    |
| `threshold`  | Total connections that trigger `failed` (default: `1000`)    |
| `interval`   | Polling frequency (default: `60`)                            |
| `ssh_config` | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `conn_total`, `conn_established`, `conn_listen`, `conn_timewait`

```yaml
- name: "Connections"
  id: "ragnarok-connections"
  type: "connections"
  interval: 1m
  warning: 500
  threshold: 1000
  ssh_config:
    host: "ragnarok.example.com"
```

---

### `wifi`
WiFi link quality and signal strength from `/proc/net/wireless`. With no `interface`, it picks the interface with the best link quality and persists the choice. Quality thresholds are inverted — *lower* is worse.

| Option              | Description                                                  |
|---------------------|--------------------------------------------------------------|
| `interface`         | Wireless interface, e.g. `wlan0` (default: the strongest)    |
| `quality_warning`   | Link quality at or below which the status is `warning` (default: `40`) |
| `quality_threshold` | Link quality at or below which the status is `failed` (default: `20`)  |
| `interval`          | Polling frequency (default: `60`)                            |
| `ssh_config`        | SSH connection details — see [SSH Config](#ssh-config) below |

**Metrics**: `link_quality`, `signal_dbm`

```yaml
- name: "WiFi"
  id: "odin-wifi"
  type: "wifi"
  interval: 1m
  quality_warning: 40
  quality_threshold: 20
  ssh_config:
    host: "odin.example.com"
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
A logical container for other monitors. Aggregates the worst-case status of all descendants, and renders them one of two ways: as **collapsible cards**, one per child, or — when the group declares a `layout:` — as a **composite layout** built out of individual widgets pulled from its children.

| Option          | Description                                                                                     |
|-----------------|-------------------------------------------------------------------------------------------------|
| `children`      | A list of nested plugin definitions                                                              |
| `layout`        | Rows of widget references. Present ⇒ composite layout; absent ⇒ collapsible cards.               |
| `grid_min_width`| Card mode only: minimum width of a child card before it wraps (default: `"320px"`)               |

Groups can be nested to arbitrary depth.

#### Card mode (default)

Each child is a collapsible card. Expansion state is preserved across page refreshes within the same server session. Each child entry can set:

| Child Option     | Description                                                                                  |
|------------------|------------------------------------------------------------------------------------------------|
| `grid_col_span`  | How many card widths this child occupies (default: `1`)                                       |
| `grid_height`    | Explicit CSS height for the child cell, e.g. `"400px"` (default: auto). Adds a scrollbar if content overflows. |
| `grid_min_width` | Overrides the group's `grid_min_width` for this child                                         |

```yaml
- name: "System Stats"
  type: "group"
  children:
    - name: "Ragnarok System"
      type: "group"
      children:
        - name: "CPU"
          type: "cpu"
          ...

# Child spanning two card widths
- name: "Overview"
  type: "group"
  children:
    - name: "Processes"
      type: "processes"
      grid_col_span: 2
      grid_height: "600px"
      ...
```

#### Composite mode

Give the group a `layout:` and it stops wrapping children in cards: it builds its own rows of cells and renders **one widget of one child** into each. Any widget of any descendant can be shown, hidden, resized or retitled, so cards, charts and tables from different monitors can share a row.

A cell names its widget as `"<child_id>.<widget_name>"` — the same widget names listed under [Plugin Layout](#plugin-layout). A child's id is its `id:` if set, otherwise its `name:`, so give a child an explicit `id` before referencing it (a `name` containing a dot is ambiguous). Nested groups are reachable too: a layout may address any descendant, at any depth, not just direct children.

Three refs are special:

| Ref form            | Renders                                                                                       |
|---------------------|-----------------------------------------------------------------------------------------------|
| `"<child_id>.<widget>"` | One widget of that child                                                                  |
| `"<child_id>.status"`   | A status card the group itself draws (child name + live state), for children that declare no `status_card` of their own |
| `"<child_id>"`          | The whole child, laid out by its own default layout — the escape hatch for `systemd_service` and nested groups, whose hand-written UIs have no addressable widgets |

Widgets a layout does not name are never built, so a composite page costs only what it shows. Each cell accepts the same per-widget properties as a plugin layout (`flex`, `height`, `min_width`, `visible`), plus `title`, which overrides the widget's own heading — the way to tell two hosts' CPU cards apart.

```yaml
- name: "Fleet Overview"
  type: "group"
  layout:
    # One row: both hosts' status, then both CPU cards side by side.
    - - "ragnarok.status"
      - "heimdall.status"
      - widget: "ragnarok-cpu.cpu_card"
        title: "RAGNAROK CPU"
      - widget: "heimdall-cpu.cpu_card"
        title: "HEIMDALL CPU"
    # One row: the two CPU charts, each half the width.
    - - widget: "ragnarok-cpu.cpu_chart"
        height: "300px"
      - widget: "heimdall-cpu.cpu_chart"
        height: "300px"
    # One row: a wide table beside a narrow card.
    - - widget: "ragnarok-procs.table"
        flex: 3
      - "ragnarok-procs.count_card"
    # A monitor with a hand-written UI, rendered whole under a heading.
    - - widget: "nginx"
        title: "Nginx"
  children:
    - name: "Ragnarok"
      id: "ragnarok"
      type: "uptime"
      target_host: "ragnarok.lan"
    - name: "Ragnarok CPU"
      id: "ragnarok-cpu"
      type: "cpu"
      ssh_config:
        host: "ragnarok.lan"
    - name: "Nginx"
      id: "nginx"
      type: "systemd_service"
      service_name: "nginx.service"
      ssh_config:
        host: "ragnarok.lan"
```

A ref naming a child or widget that does not exist renders nothing and is logged as a warning at startup of the page.

---

### Plugin Layout

Every plugin with a declarative UI supports a `layout:` key controlling how its widgets are arranged on its detail page. Without one the plugin uses its built-in default (its `_DEFAULT_LAYOUT`).

A layout is a list of **rows**; each row is a list of widgets sharing that row, laid out as flex cells that wrap when they run out of width. An entry is either a bare widget name or a mapping with these keys:

| Per-widget option | Description                                                                                         |
|-------------------|-------------------------------------------------------------------------------------------------------|
| `widget`          | The widget name (required in mapping form)                                                          |
| `flex`            | Share of the row's width relative to its siblings (default: `1`)                                    |
| `min_width`       | Width below which the cell wraps to its own line (default: `"280px"`)                               |
| `height`          | Explicit CSS height for this cell, e.g. `"400px"`. Adds a scrollbar on overflow (default: auto).     |
| `visible`         | `false` to hide the widget (default: `true`)                                                        |

The same keys can instead be given as a **mapping of widget name → properties**, which keeps the plugin's default rows and only overrides the named widgets — the usual way to hide a panel or make one chart taller.

**Widget names by plugin type:**

| Plugin             | Widget names                                                                 |
|--------------------|------------------------------------------------------------------------------|
| `uptime`           | `host_card`, `status_card`, `latency_card`, `chart`, `events`               |
| `systemd_service`  | hand-written UI — not individually addressable                              |
| `cpu`              | `host_card`, `cpu_card`, `cpu_chart`, `events`                              |
| `memory`           | `host_card`, `mem_pct_card`, `mem_used_card`, `memory_chart`, `events`      |
| `load`             | `host_card`, `load_1m_card`, `load_5m_card`, `load_15m_card`, `load_chart`, `events` |
| `temperature`      | `host_card`, `temp_card`, `sensors`, `temp_chart`, `events`                 |
| `interrupts`       | `host_card`, `irq_card`, `ctxt_card`, `irq_chart`, `ctxt_chart`, `events`   |
| `gpu`              | `host_card`, `gpu_util_card`, `gpu_mem_card`, `gpu_temp_card`, `gpus`, `gpu_chart`, `events` |
| `oom`              | `host_card`, `oom_total_card`, `oom_recent_card`, `oom_chart`, `events`     |
| `throughput`       | `host_card`, `iface_card`, `rx_card`, `tx_card`, `rx_chart`, `tx_chart`, `events` |
| `connections`      | `host_card`, `conn_total_card`, `conn_established_card`, `conn_listen_card`, `conn_timewait_card`, `conn_total_chart`, `conn_established_chart`, `events` |
| `wifi`             | `host_card`, `wifi_iface_card`, `quality_card`, `signal_card`, `quality_chart`, `signal_chart`, `events` |
| `smart`            | `host_card`, `smart_total_card`, `smart_ok_card`, `smart_failed_card`, `events` |
| `zfs`              | `host_card`, `zfs_total_card`, `zfs_ok_card`, `zfs_degraded_card`, `zfs_usage_card`, `zfs_pools`, `zfs_chart`, `events` |
| `md`               | `host_card`, `md_total_card`, `md_ok_card`, `md_degraded_card`, `events`    |
| `disk_io`          | `host_card`, `io_device_card`, `read_card`, `write_card`, `read_chart`, `write_chart`, `events` |
| `processes`        | `host_card`, `count_card`, `top_cpu_card`, `table`, `logs`                  |
| `disk_space`       | `host_card`, `path_card`, `threshold_card`, `usage_card`, `avail_card`, `total_card`, `chart`, `logs` |

Any other plugin's names are the keys of its `UI_SPEC` `cards`, `charts` and `tables`, plus `events`, `logs`, `jobs` and `host_card` where it has them.

**Examples:**

```yaml
# Overrides only: keep the default rows, make the chart taller, hide the logs.
- name: "Ragnarok CPU"
  type: "cpu"
  layout:
    cpu_chart:
      height: "500px"
    logs:
      visible: false

# Full replacement: two stat cards on one row, a double-width chart below.
- name: "Heimdall Memory"
  type: "memory"
  layout:
    - - "host_card"
      - "mem_pct_card"
      - "mem_used_card"
    - - widget: "memory_chart"
        height: "400px"
```

---

### SSH Config

All SSH-based plugins (`systemd_service`, `cpu`, `smart`, `disk_space`, `throughput`, …) accept an `ssh_config` block:

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

Vigil speaks SSH natively (AsyncSSH) rather than shelling out to the system
`ssh` client, and opens one persistent connection per host — every SSH-based
monitor on that host runs its commands as a channel on that one connection
rather than a separate connection each. Host key verification is
trust-on-first-use: the first successful connection to a host stores its key
(under `$VIGIL_SSH_CONTROL_DIR/known_hosts`, defaulting to a `vigil-ssh`
directory under the system temp dir), and every later connection is checked
against it — a changed key is refused rather than silently accepted.

**The number of monitors you can point at one host is bounded by how many
concurrent SSH sessions that host's `sshd` allows** (`MaxSessions` in
`sshd_config`, default `10`). Vigil caps its own concurrency per host below
that default (8 regular monitors + 2 for long-running jobs like `borg`, at
most 10 total in flight at once), so a host running its `sshd` at the
OpenSSH default is safe by construction — extra monitors queue rather than
fail. Hosts with many monitors, or where jobs may overlap with a burst of
polling, benefit from raising `MaxSessions` in that host's own `sshd_config`
(e.g. `MaxSessions 50`) to reduce queuing — or from running the
[agent](agent.md) on that host, where commands are frames multiplexed on one
socket and no session ceiling applies.

