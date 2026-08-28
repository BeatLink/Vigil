# The Vigil Agent

The Vigil agent is a small daemon that runs on a monitored host, dials **outward** to
Vigil over a WebSocket, and holds that connection open. It needs no inbound port, no
certificate of its own, and no firewall rule — it reuses the dashboard's port, so a host
behind NAT works with no extra setup.

Two things travel on that one connection:

- **Commands** — the same shell command strings the SSH transport carried. Every monitor
  works over an agent unmodified, because both transports return the same
  `(exit_code, stdout, stderr)`.
- **Events** — sources the agent watches locally and pushes the instant they change. This
  is what a polled transport cannot do at any interval.

## Server config

Declare each agent that may connect, then point monitors at it by id:

```yaml
agents:
  - id: "web-01"
    token_file: "/run/secrets/vigil_agent_web01"   # or `token:` inline
    host: "web-01.example.com"   # label only; Vigil never dials the agent

plugins:
  - name: "Web Servers"
    type: "group"
    agent: "web-01"              # inherited by every child below
    children:
      - name: "Nginx"
        type: "systemd_service"
        service_name: "nginx.service"
      - name: "Disk"
        type: "disk_space"
```

| Field   | Description                                                                 |
|---------|-----------------------------------------------------------------------------|
| `id`    | Agent identity; a monitor's `agent:` key refers to this                      |
| `token` | Shared secret the agent authenticates with, inline                            |
| `token_file` | Path to a file holding that secret, read once at startup. Prefer this wherever `config.yaml` is generated — under Nix it lands world-readable in the store |
| `host`  | Display label for the target. Optional — defaults to the id                  |

An agent with neither `token` nor a readable `token_file` can never connect.

`agent:` on a group is inherited by every monitor beneath it, so moving a whole host
between transports is one line. A monitor with both `agent:` and `ssh_config:` uses the
agent; drop the `agent:` key to fall back to SSH.

## Installing the agent

```bash
pip install vigil            # ships the `vigil-agent` command
```

```yaml
# /etc/vigil-agent.yaml
url: "ws://vigil.example.com:8080/api/agent/ws"   # wss:// behind TLS
id: "web-01"
token_file: "/run/secrets/vigil_agent_token"      # or `token:` inline
```

| Field        | Description                                                          |
|--------------|----------------------------------------------------------------------|
| `url`        | The server's agent endpoint. `wss://` when the dashboard is behind TLS |
| `id`         | Must match an `id` in the server's `agents:` list                     |
| `token`      | The shared secret, inline                                             |
| `token_file` | Path to a file holding the token, read at runtime. Prefer this: the secret stays with your secret manager and never enters a config file, a unit's environment, or a Nix store path |
| `hostname`   | Hostname reported to the server. Defaults to the machine's own         |

```bash
vigil-agent --config /etc/vigil-agent.yaml
```

Every setting can come from the environment instead (`VIGIL_AGENT_URL`, `VIGIL_AGENT_ID`,
`VIGIL_AGENT_TOKEN`, `VIGIL_AGENT_TOKEN_FILE`, `VIGIL_AGENT_HOSTNAME`), so a systemd unit or
container can supply the token without writing it to disk.

On NixOS the flake exports `nixosModules.agent` for the monitored host, alongside the
existing `nixosModules.default` for the server. The agent module defaults to
`packages.agent`, a standalone build carrying only what the agent imports — a monitored
host never builds nicegui, peewee, dnspython or asyncssh to run one:

```nix
services.vigil-agent = {
  enable = true;
  url = "ws://vigil.example.com:8080/api/agent/ws";
  id = "web-01";
  tokenFile = config.sops.secrets.vigil_agent_token.path;
  extraGroups = [ "systemd-journal" ];      # for journal streams and unit logs
  path = [ pkgs.smartmontools ];            # tools the monitors' commands invoke
};
```

The agent runs unprivileged as `vigil-agent`. Grant that user the same scoped `NOPASSWD`
sudo rules the SSH user had — a narrow grant per command is a better posture than running
the agent as root.

The agent reconnects on its own with exponential backoff and jitter, so a server restart
needs no action on the host. A monitor whose agent is not currently connected reports
failed with an explicit message — the same way a refused SSH dial behaves — and recovers
by itself.

## Event streams

A plugin declares the streams it wants via `subscriptions()`; the server sends that set to
the agent on connect, and the agent watches them locally. Three watcher kinds ship today:

| Kind      | What it does                                                | Params |
|-----------|-------------------------------------------------------------|--------|
| `journal` | Follows the systemd journal and pushes matching entries as they are written | `unit`, `identifier`, `priority`, `kernel`, `grep` |
| `path`    | Pushes when a path's mtime or size changes                   | `path`, `interval` (default `0.25`) |
| `sample`  | Runs a command locally on a fast interval and pushes its output | `command`, `interval` (default `1.0`), `on_change` |

`path` and `sample` still sample — but *locally*, where a `stat()` or a fork costs
microseconds rather than an SSH round trip. That is what makes per-second resolution
practical without the target's `sshd` ever seeing it.

Two plugins use this today:

- **`oom`** follows the kernel journal for the OOM killer's own message. The polled
  `/proc/vmstat` counter still runs and remains the authority on totals, but a kill is now
  reported the moment it happens and carries the process name — which the counter cannot.
- **`systemd_service`** follows its unit's journal, so the log view is live rather than a
  snapshot of the last *n* lines taken up to `interval` ago, and a crash-restart loop
  between two polls is no longer invisible.

In both cases the **poll still owns status**. A streamed log line adds detail; it never
flips a monitor's state on its own.

## Agent vs SSH

| | Agent | SSH |
|---|---|---|
| Software on target | `vigil-agent` | none |
| Concurrency per host | unbounded (frames on one socket) | capped by the target's `MaxSessions` |
| Per-command cost | one local fork | SSH channel + fork |
| Detection latency | immediate, for subscribed streams | one `interval` |
| Works on appliances | no | yes |
| Privilege | whatever the commands need (same `sudo` rules) | whatever the commands need |

The agent does **not** change the privilege story: `smartctl` needs root either way, and
the same `NOPASSWD` rules apply. Run the agent as an unprivileged user with the same narrow
sudo grants the SSH user had rather than as root.
