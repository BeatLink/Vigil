# Vigil Project Context

## Vision

Vigil is a Python-based, pull-based monitoring system. It aims to be the "Swiss Army Knife" of system monitoring for small to medium environments where agent-based solutions like Prometheus or Zabbix might be too heavy.

## Technical Stack

- **Language**: Python 3.9+
- **Connectivity**: Paramiko (SSH), Requests (HTTP)
- **Configuration**: YAML
- **Concurrency**: `asyncio` for non-blocking I/O
- **Storage**: SQLite (managed by Peewee ORM)

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

## Instructions for AI Agents

When adding new features, follow the domain-based directory structure:
- `vigil/modules/<domain>/`: Contains the `Plugin` class inheriting from `BasePlugin`.
