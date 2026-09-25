# Notifications

Vigil sends a notification when a monitor's status changes in a way you care about: it starts
failing, it is still failing, or it recovers. Notifications go to **channels**. Each monitor
follows a **rule** that sets which channels it uses and which changes count.

Only one channel type exists so far: `desktop`. More are planned; see
[notifications-plan.md](notifications-plan.md).

## Config

```yaml
agents:
  - id: laptop-desktop
    token_file: /run/secrets/vigil_agent_laptop_desktop

notifications:
  base_url: https://vigil.lan        # the dashboard's address, used to link to a monitor
  channels:
    - id: laptop
      type: desktop
      agent: laptop-desktop
  defaults:                          # the rule every monitor starts from
    on: [failed]
    after: 1
    repeat: 0
    recovery: true

plugins:
  - name: Storage
    type: group
    notify: {on: [failed, warning]}  # passed down to every monitor in the group
    children:
      - name: tank
        type: zfs
        notify: {after: 3}           # adds to what it inherits
      - name: scratch
        type: disk_space
        path: /scratch
        notify: false                # never notifies
```

### Rule settings

Use these in `notifications.defaults` or in a monitor's `notify:`. A group passes its `notify:`
down to its children, and each child can override any setting. `notify: false` turns a monitor
off. A child can turn itself back on with its own mapping.

| Setting    | Default          | Meaning |
|------------|------------------|---------|
| `channels` | every channel    | The channel ids to send to |
| `on`       | `[failed]`       | Statuses that count as a problem: `failed`, `warning`, `unavailable` |
| `after`    | `1`              | Problem cycles in a row before the first notification, so a single blip is ignored |
| `repeat`   | `0` (off)        | How often to remind while the problem lasts, e.g. `1h` |
| `recovery` | `true`           | Also notify when the monitor stops having a problem |

A group never notifies for itself. Its status only repeats its children's, so it would duplicate
their notifications.

`unavailable` is off by default. When a host goes down, every monitor on it turns unavailable at
once. An availability monitor (`uptime`, `http`) reports that host as failed, which gives one
notification instead of one per monitor.

If Vigil restarts while a monitor has a problem, it is not announced again. Its recovery still
is.

## Monitor links

Every monitor has its own dashboard address, `/monitor/<id>`, and the events feed is at
`/events`. A notification links to its monitor when `base_url` is set.

## Desktop notifications

A `desktop` channel shows a notification on a computer that runs a Vigil agent. Clicking it opens
the monitor's page in the browser.

The agent must run **inside your graphical session, as you**. Desktop notifications travel over
your session's message bus, and a system service, such as the regular Vigil agent, cannot reach
it. Run a second agent for this, with its own `id` and token, and set `notify_only: true`. That
agent shows notifications and refuses everything else, including commands. Without it, the server
could run commands as you.

```yaml
# ~/.config/vigil-agent-desktop.yaml
url: wss://vigil.lan/api/agent/ws
id: laptop-desktop
token_file: /home/you/.config/vigil/desktop-token   # an absolute path
notify_only: true
```

It needs `notify-send` (libnotify) to show the notification and `xdg-open` (xdg-utils) to follow
the link.

### Icon and urgency

```yaml
channels:
  - id: laptop
    type: desktop
    agent: laptop-desktop
    icon: vigil                      # the default: Vigil's own icon, shipped with the agent
    urgency:                         # one value for everything, or per status as here
      failed: critical
      recovered: low
```

`icon` is `vigil`, an icon name from the desktop's icon theme (such as `dialog-error`), or the path
of an image on that computer. `urgency` is `low`, `normal` or `critical`. Most desktops keep a
critical notification on screen until you dismiss it.

Either setting can be one value for every notification, or a mapping by status: `failed`,
`warning`, `unavailable` and `recovered`. Statuses the mapping leaves out keep their defaults:

| Status        | Default urgency |
|---------------|-----------------|
| `failed`      | `critical`      |
| `warning`     | `normal`        |
| `unavailable` | `normal`        |
| `recovered`   | `low`           |

On NixOS the agent module sets this up as a user service that starts with the graphical session:

```nix
services.vigil-agent = {
  url = "wss://vigil.lan/api/agent/ws";
  desktop = {
    enable = true;
    id = "laptop-desktop";
    tokenFile = "/run/secrets/vigil_agent_laptop_desktop";  # readable by the desktop user
  };
};
```

This works with or without `services.vigil-agent.enable`. A newly added user service does not
start in a session that is already running. Start it with
`systemctl --user start vigil-agent-desktop`, or log in again.

## Checking a channel

```bash
curl -X POST -u admin https://vigil.lan/api/notifications/laptop/test
```

This sends a test notification and returns `{"sent": true}`, or the error if it could not be
delivered. A delivery that fails during normal operation is retried twice. If it still fails, it
appears in the events feed.
