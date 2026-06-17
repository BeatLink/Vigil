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

## Plugin Types

### `uptime`
Checks host availability using ICMP ping.

| Option        | Description                              |
|---------------|------------------------------------------|
| `target_host` | The IP or hostname to ping               |
| `interval`    | Polling frequency in seconds (default: 60) |

**Metrics**: `up` (1/0), `latency_ms`

---

### `systemd_service`
Monitors status and logs for a systemd unit over SSH.

| Option         | Description                                      |
|----------------|--------------------------------------------------|
| `service_name` | Name of the systemd unit (e.g. `nginx.service`)  |
| `lines`        | Number of `journalctl` log lines to fetch per cycle |
| `ssh_config`   | SSH connection details (`host`, `user`, etc.)    |

**Actions**: Restart Service, Stop Service

---

### `group`
A logical container for other monitors. Aggregates the status of all children.

| Option     | Description                          |
|------------|--------------------------------------|
| `children` | A list of nested plugin definitions  |

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
- [ ] SSH collector module with standard metric parsing (CPU, RAM, Disk)
- [ ] Basic alerting (Email, Slack, or Webhook)
- [ ] Control module for service remediation
- [ ] OpenTelemetry/OpenMetrics export module

---

## License

GPL 3.0