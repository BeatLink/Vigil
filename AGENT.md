# Vigil Project Context

## Vision

Vigil is a Python-based, pull-based monitoring system. It aims to be the "Swiss Army Knife" of system monitoring for small to medium environments where agent-based solutions like Prometheus or Zabbix might be too heavy.

Similar to **Uptime Kuma**, Vigil provides a centralized dashboard to configure and manage various types of monitors, allowing users to oversee diverse infrastructure from a single pane of glass.

## Repository Structure

- **`vigil/core/main.py`**: The application entry point and orchestrator. It manages the lifecycle of the engine, plugin instantiation, and the main execution loop.
- **`vigil/core/data/`**: Handles all persistence-related logic. This includes SQLite database management (via Peewee) and YAML configuration parsing.
- **`vigil/core/modules/`**: Contains the core business logic and shared libraries. This includes:
    - **Collectors**: Helpers to abstract data gathering (SSH, HTTP, etc.).
    - **Controllers**: Logic for sending remediation or control commands back to services.
    - **Alerting**: Modules for notifications (Email, Slack, Webhooks).
    - **UI Helpers**: Libraries used by the dashboard to present data.
- **`vigil/core/common/`**: Stores shared models and base classes, including the `BasePlugin` class and common utility libraries used across the core modules.
- **`vigil/core/ui/`**: Responsible for the web-based user interface, built using **NiceGUI**.
- **`vigil/plugins/`**: Domain-specific monitoring logic. Each directory here represents a plugin type (e.g., `systemd`, `hardware`, `network`).
    - *Note*: Plugins are designed to be instantiated multiple times with different parameters to monitor various devices and services.

## Data Flow

1.  **Initialization**: `main.py` loads infrastructure definitions from the YAML configuration via `core/data`.
2.  **Registry Building**: The Engine scans `vigil/plugins/`, instantiating the required plugins based on the configuration. It injects shared dependencies from `core/modules` (collectors/loggers) into each instance.
3.  **Polling Loop**: The Engine runs an asynchronous loop, triggering the `run_cycle()` method of each plugin instance.
4.  **Collection & Processing**: Plugins use modules in `core/modules/collectors` to gather metrics or logs from targets.
5.  **Persistence**: Results are processed and stored in the SQLite database via the persistence layer in `core/data`.
6.  **Visualization**: The `core/ui` layer queries the database to provide real-time updates and historical visualization on the web dashboard.
7.  **Alerting & Control**: If a plugin detects a failure state, it utilizes `core/modules` to trigger alerts or execute remediation commands via controllers.

## Technical Stack

- **Language**: Python 3.9+
- **Connectivity**: Paramiko (SSH), Requests/Httpx (HTTP)
- **Configuration**: YAML
- **Concurrency**: `asyncio` for non-blocking I/O
- **Storage**: SQLite (managed by Peewee ORM)
- **Frontend**: NiceGUI

## Design Principles

1. **Simplicity First**: Configuration should be intuitive.
2. **No Remote Agent**: All logic stays on the Vigil server; remote hosts only need SSH.
3. **Domain Encapsulation**: Plugins are grouped by domain (e.g., Systemd, HTTP, Hardware). Each plugin handles its own collection, alerting, and control logic.
4. **Fail-Safe Control**: Control actions must be logged and confirmable.
5. **Standard-Aware**: While the core is lightweight, it aims for OpenTelemetry compatibility in data naming and export capability.
6. **Hybrid Config**: Use YAML for infrastructure definitions (Source of Truth) and SQLite for runtime state/overrides.

## Roadmap

- [X] Core engine implementation with YAML config loader.
- [X] Core Database utility (SQLite).
- [X] Core SSH utility for remote access.
- [ ] OpenTelemetry/OpenMetrics export module.
- [ ] SSH Collector module with standard metric parsing (CPU, RAM, Disk).
- [ ] Ping/ICMP module.
- [ ] Basic Alerting (Email, Slack, or Webhook).
- [ ] Control module for service remediation.
- [X] Web Dashboard for real-time visualization (NiceGUI).
