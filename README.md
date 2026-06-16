# Vigil

A lightweight, pluggable network and system monitor for Linux.

Vigil is designed to provide high-visibility monitoring of remote systems using a pull-based architecture over SSH. It focuses on simplicity, low overhead, and ease of extensibility.

## Features

* **Lightweight**: Minimal dependencies and low resource footprint.
* **Agentless**: Uses standard SSH for data collection—no need to install software on target nodes.
* **Pluggable Architecture**: Easily add new collectors, alert handlers, or control mechanisms.
* **Data Types**: Supports both structured metrics and log aggregation.
* **Collection Modules**:
  * **Ping / ICMP**: Basic reachability checks.
  * **SSH commands**: Execute remote commands and parse output.
  * **HTTP/Web APIs**: Monitor status codes and response content.
* **Alerting**: Flexible alerting rules with multiple output channels.
* **Control**: Ability to trigger remediation actions on remote systems.


Collecting

Alerting

Control

Logging

Presenting

## Getting Started

### Prerequisites

* Python 3.9+
* SSH access to target machines (SSH keys recommended)

### Installation

```bash
pip install .
```

### Quick Start

1. Configure your targets in `config.yaml`.
2. Run the vigil engine:
   ```bash
   python -m vigil.core.engine --config config.yaml
   ```

## Project Structure

* `vigil/core/`: The main orchestrator and base classes.
* `vigil/gui/`: NiceGUI-based dashboard for visualization.
* `vigil/modules/`: Individual monitoring and control modules.
* `vigil/presentation/`: Formatters for CLI, Web, or file logging.
* `vigil/alerting/`: Logic for handling thresholds and notifications.

## License

GPL 3.0 License
