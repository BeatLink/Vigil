# Vigil

Vigil is a web-based network and systems monitor for Linux systems, homelabs, and small networks. Inspired by Uptime Kuma, Prometheus, Grafana, and Loki, it provides a centralized dashboard to configure and manage diverse infrastructure from a single pane of glass.

Hosts you control run the **Vigil agent**, which connects outward to Vigil and streams events as they happen. Hosts you can't install software on — routers, switches, NAS firmware, appliances — are still monitored agentlessly over SSH, HTTP, DNS and ICMP. The same monitors run over either.

Unlike most network and system monitors, Vigil is designed to be highly extensible and capable of performing actions on monitored targets, not just observing them.

---

## Features

- **Two Transports, One Plugin Set**: Reach a host through the **Vigil agent** — a small daemon that dials out to Vigil and pushes events the instant they happen — or **agentlessly** over SSH, HTTP, DNS, and ICMP. Every monitor works over either; switching a host is one config key.
- **Event-Driven Collection**: Agent-backed monitors subscribe to live streams (journal follow, path watches, sub-second local sampling), so detection latency stops being a function of the poll interval.
- **Web Dashboard**: Real-time interactive visualizations built with NiceGUI and ECharts, featuring latency history, status distribution, and log views.
- **Alerting & Notifications**: Sends alerts to various channels when events or metric thresholds are detected. *(WIP)*
- **Target Control**: Trigger actions on monitored targets (e.g. restarting systemd services) directly from the UI.
- **Plugin Architecture**: Core features are implemented through plugins called "monitors" which handle specific domains (systemd services, host uptime, hardware parameters, etc.).
- **Hierarchical Organization**: Organize monitors into nested groups by location, service, or environment.
- **Lightweight**: Minimal dependencies and low resource footprint.
- **Easy Development**: Fully written in Python.

---

## Architecture

Vigil is built around **pure plugins and a Coordination Engine that owns all IO and persistence**. A plugin is constructed with only its `(name, config)`; it declares *what* to collect and *how to interpret* the results as pure functions, and the engine performs every side effect — SSH, HTTP/DNS/ICMP, database writes, thread offloading. This keeps plugins small, side-effect-free, and testable without mocks.

A plugin's contract is:

- **Declare** — `requests()` returns a list of connector requests (`Command` / `HttpRequest` / `DnsQuery` / `PingRequest`); an SSH-only plugin overrides the `commands()` shorthand instead. Both are pure.
- **Interpret** — `parse_results()` (or `parse()` for the SSH shorthand) turns the connector results into a single `CollectResult` describing everything to persist (metrics, logs, status, an optional snapshot). Pure — no IO.
- **Subscribe** (optional) — `subscriptions()` declares event streams an agent should watch on the target, and `parse_event()` turns each pushed frame into a `CollectResult`. Both pure; ignored for targets reached over SSH.
- **Act** (optional) — `plan_action()` / `interpret_action()` describe control actions (restart a service, force a backup) the same declarative way.
- **Present** (optional) — a declarative `UI_SPEC` dict renders the plugin's dashboard page; only genuinely bespoke plugins hand-write `render_ui()`.

The engine executes the declared IO, persists the returned `CollectResult`, and drives one independent polling loop per monitor.

### Data Flow

1. **Initialization**: `vigil/__main__.py` builds one `VigilEngine` (`core/coordination/engine.py`), which loads `config.yaml` and instantiates plugins (`setup_modules`). Group plugins act as containers for nested monitors.
2. **Wiring**: for each plugin the engine builds its engine-owned `ExecContext` — an agent connection or a pooled SSH connection, chosen by config — and injects a read-only `PluginDataView` as `plugin.data`.
3. **Polling**: each monitor runs its own async loop at its own `interval`. Per cycle the engine runs the plugin's declared requests through the **Connector Engine** — which routes each `Command` to that target's agent or its SSH connection — then calls the plugin's pure `parse_results()`.
3b. **Events**: monitors on an agent-backed target also declare `subscriptions()`. The agent watches those sources locally and pushes a frame the instant one changes; the engine hands it to the plugin's pure `parse_event()` and persists the result immediately, outside the polling schedule.
4. **Persistence**: the engine writes the resulting `CollectResult` via `db.apply_result(...)` into SQLite (Peewee ORM). A background writer thread batches commits off the event loop.
5. **Visualization**: every write publishes a change, and the NiceGUI dashboard refreshes off that — one subscription per connected client — rendering the sidebar tree plus each plugin's detail page. It reads only through `plugin.data` / the database — never through a plugin's IO.
6. **Export**: metrics are exposed to Prometheus (pull, `/metrics`) and optionally pushed to InfluxDB.

### Project Structure

```
vigil/
├── __main__.py              # Entry point: build engine, load plugins, start GUI
├── core/
│   ├── coordination/        # VigilEngine (Coordination Engine) + PluginDataView
│   ├── connectors/          # All IO: agent + SSH + HTTP/DNS/ICMP sub-connectors, request types
│   ├── database/            # DatabaseManager (SQLite/Peewee), models, read-result types
│   ├── settings/            # config.yaml loader + typed schema
│   ├── exporters/           # Prometheus pull + InfluxDB push
│   └── ui/                  # NiceGUI dashboard, declarative UI_SPEC renderer
└── plugins/
    ├── base/                # Plugin ABC + shared config/helper mixins
    └── *.py                 # One module per monitor type (uptime, systemd_service, …)

vigil_agent/                 # The daemon that runs on a monitored host
├── protocol.py              # Wire format, shared with the server
├── client.py                # Outbound WebSocket, reconnect, frame dispatch
├── executor.py              # Local command execution
└── watchers.py              # Event sources (journal / path / sample)
```

See [DEVELOP.md](DEVELOP.md) for the architectural rationale — the pure-plugin contract, the collection lifecycle, the SQLite writer/reader model, and the declarative UI spec.

### Technical Stack

| Concern        | Technology                          |
|----------------|--------------------------------------|
| Language       | Python 3.9+                          |
| Connectivity   | WebSocket (agent), AsyncSSH (SSH), `httpx` (HTTP), dnspython (DNS) |
| Configuration  | YAML                                 |
| Concurrency    | `asyncio`                            |
| Storage        | SQLite via Peewee ORM                |
| Frontend       | NiceGUI + ECharts                    |

---

## Theme

The dashboard is themed with **Halon**: slate plus a single blue, a recessed
navigation frame, ghost-first controls and hairline structure, in a light and a
dark scheme that share one token set. Every color lives in
`vigil/core/ui/static/halon-tokens.css`; the component rules that consume those
tokens live beside it in `halon.css`, and no Python module states a color.

The scheme follows the browser's `prefers-color-scheme` unless you pin it:

```yaml
theme:
  scheme: dark      # auto (default), light, or dark
```

Individual tokens can be overridden from the same block — the full field-to-token
table lives in [docs/theme.md](docs/theme.md).

---

## Getting Started

### Prerequisites

- Python 3.9+
- For agent-backed targets: `vigil-agent` installed on the host, able to reach Vigil's port outbound
- For agentless targets: SSH access to the target machine (SSH key auth recommended)

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

Every monitor type, its config keys, and its `ssh_config` / layout options are
documented in the [plugin reference](docs/plugins.md). Agent declaration and
installation are covered in the [agent guide](docs/agent.md).

### Authentication

By default the dashboard and REST API are unauthenticated — anyone who can reach the port has full read access and can trigger control actions. Set `auth.username` and `auth.password` (or `auth.password_file`, to keep the secret out of the YAML config) to put every route behind a sign-in page:

```yaml
auth:
  username: "admin"
  password_file: "/run/secrets/vigil_dashboard_password"
  # Optional — sign a session with a fixed key so restarts do not sign everyone
  # out. Without it a key is generated per start.
  session_secret_file: "/run/secrets/vigil_session_secret"
  session_hours: 12      # How long a sign-in lasts (default 12)
  remember_days: 30      # How long "keep me signed in" lasts (default 30)
```

An unauthenticated browser is redirected to `/login`, where it exchanges the credentials for a signed, expiring session cookie (`HttpOnly`, `SameSite=Lax`, and `Secure` when Vigil is reached over HTTPS — including behind a reverse proxy that sets `X-Forwarded-Proto`). "Keep me signed in" is what makes the cookie outlive the browser session. The dashboard header gains an account menu with **Sign out**, which drops the session at `/logout`. Repeated failures from one address are throttled after five attempts in five minutes.

Scripts and scrapers cannot follow a form redirect, so `/api/...` and `/metrics` also accept HTTP Basic credentials — the same username and password — and answer `401` rather than redirecting. No `WWW-Authenticate` header is sent, so a browser never sees the native credential dialog. `/api/push/...` stays public; it carries its own per-monitor token.

Every `*_file` value is read once at startup. If only one of `username`/`password` is set, auth stays disabled and a warning is logged.

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

## Documentation

- [Plugin reference](docs/plugins.md) — every monitor type, its config keys, metrics, actions, and layout options
- [Agent guide](docs/agent.md) — installing `vigil-agent`, server-side declaration, event streams, agent vs SSH
- [Integrations](docs/integrations.md) — the events feed, REST API, Prometheus endpoint, and InfluxDB export
- [Theme tokens](docs/theme.md) — per-token color overrides for the Halon theme
- [Roadmap](docs/roadmap.md) — what's done and what's planned
- [DEVELOP.md](DEVELOP.md) — architectural rationale and development notes

---

## Design Principles

1. **Simplicity First**: Configuration should be intuitive.
2. **No Remote Agent**: All logic stays on the Vigil server; remote hosts only need SSH.
3. **Pure Plugins**: Each plugin owns its domain logic (what to collect, how to interpret it, what actions it offers) as pure functions; the engine owns all IO and persistence.
4. **Hierarchical Organization**: Supports nested groups for organizing monitors by location, service, or environment.
5. **Fail-Safe Control**: Control actions must be logged and confirmable.
6. **Standard-Aware**: Aims for OpenTelemetry compatibility in data naming and export capability.

---

## Credits

- App icon: [Guard Protection Safe 3](https://www.svgrepo.com/svg/421980/guard-protection-safe-3) from [SVG Repo](https://www.svgrepo.com)

---

## License

GPL 3.0
