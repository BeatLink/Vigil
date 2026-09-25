# Notifications

Vigil sends a notification when a monitor's status changes in a way you care about: it starts
failing, it is still failing, or it recovers. Notifications go to **channels**. Each monitor
follows a **rule** that sets which channels it uses and which changes count.

Five channel types exist: [`desktop`](#desktop-notifications), [`webhook`](#webhook),
[`ntfy`](#ntfy), [`smtp`](#email) and [`apprise`](#apprise). See
[notifications-plan.md](notifications-plan.md) for what is planned.

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
| `for`      | `0` (off)        | How long a problem must last, with no good reading in between, before the first notification, e.g. `1h`. Suits load and usage monitors that spike |
| `repeat`   | `0` (off)        | How often to remind while the problem lasts, e.g. `1h` |
| `recovery` | `true`           | Also notify when the monitor stops having a problem |
| `flapping` | off              | `{changes: 4, within: 1h}`: a monitor whose problem starts or ends that many times within that window is flapping. See [Flapping](#flapping) |

A group never notifies for itself. Its status only repeats its children's, so it would duplicate
their notifications.

`unavailable` is off by default. When a host goes down, every monitor on it turns unavailable at
once. An availability monitor (`uptime`, `http`) reports that host as failed, which gives one
notification instead of one per monitor.

While it is off, `unavailable` is neutral. A monitor that turns unavailable during a problem has
not recovered, and the problem carries on once it measures again.

If Vigil restarts while a monitor has a problem, it is not announced again. Its recovery still
is.

## Flapping

A monitor that keeps failing and recovering would send a notification every time. With
`flapping` set, the change that reaches `changes` within `within` sends one "is flapping"
notification instead, and nothing more is sent for that monitor until it has held one status for
the whole window. Then one message says it stopped flapping, and whether it is still failing or
has recovered.

```yaml
notifications:
  defaults:
    flapping: {changes: 4, within: 1h}
```

## Grouping

When several notifications arrive close together, for example when a host goes down and takes
its monitors with it, `group_window` sends them as one. Vigil waits that long after the first
notification, then sends everything that arrived meanwhile to each channel as one digest, such as
"Vigil: 3 failed, 1 recovered" with a line per monitor. A notification that arrives alone is sent
as itself, only later by the window. It is off by default.

```yaml
notifications:
  group_window: 30s
```

On the desktop, a group of failures is one notification listing them. It shrinks as each
monitor recovers and closes when the last one does.

## Muting

The **Mute** button on a monitor's page stops its notifications until you unmute it, for example
during maintenance. Muting a group mutes everything in it, and each monitor in the group says
which group muted it. The bell icon in the dashboard's header lists everything that is muted,
with a button to unmute each one.

A mute is saved, so it survives a restart. A failure that happens while a monitor is muted is not
announced, and neither is its recovery, even if you unmute first. Unmuting while a monitor is
still failing does not send a notification.

## Maintenance windows

A maintenance window holds back problem notifications for chosen monitors at planned times, such
as nightly upgrades or a one-off migration. Times are the Vigil server's local time.

```yaml
notifications:
  maintenance:
    - name: Nightly upgrades
      monitors: [heimdall-host]      # monitor or group ids; leave out to cover every monitor
      days: [sun]                    # weekday names; leave out for every day
      from: "03:00"
      to: "05:00"                    # may run past midnight, and then belongs to the day it started
    - name: NAS migration
      start: 2026-10-01T09:00        # a one-off window
      end: 2026-10-01T17:00
```

During a window, covered monitors send no problem notifications, and a group id covers everything
in the group. Unlike a mute, a window does not hide what it leaves behind: a problem that began
during the window and is still there when it ends is announced then. A problem that clears inside
the window is never sent. A notification already on screen from before the window still clears
when its monitor recovers.

A monitor's page says when a window covers it, and the bell dialog lists every window with the
ones in effect marked.

## Monitor links

Every monitor has its own dashboard address, `/monitor/<id>`, and the events feed is at
`/events`. A notification links to its monitor when `base_url` is set.

## Desktop notifications

A `desktop` channel shows a notification on a computer that runs a Vigil agent. Clicking it opens
the monitor's page in the browser.

Every notification has a **Dismiss** button beside **Open**, and closes itself after `timeout`
(30 seconds unless set). The agent closes it itself when the time runs out, because most desktops
keep a `critical` notification on screen until it is clicked.

Each monitor has at most one notification on screen. A reminder or a worse status replaces it,
and when the monitor recovers the notification closes itself instead of a "recovered" one
appearing. Set `dismiss_on_recovery: false` on the channel to get the recovery notification
instead. A notification is still cleared if you mute the monitor after it appeared. Closing uses
`busctl` (part of systemd) to reach the desktop's notification service.

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
    dismiss_on_recovery: true        # the default: close the notification when the monitor recovers
    timeout: 30s                     # the default: close it after this long; 0 keeps it until dismissed
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

`recovered` only applies when `dismiss_on_recovery` is off.

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

## Webhook

A `webhook` channel sends each notification to a URL. It suits Home Assistant, n8n, chat apps
that accept incoming webhooks, and your own scripts.

```yaml
channels:
  - id: home-assistant
    type: webhook
    url_file: /run/secrets/ha_vigil_webhook_url   # or `url:` inline
    method: POST                                  # POST (default), PUT or PATCH
    headers: {X-Source: vigil}
    bearer_token_file: /run/secrets/hook_token    # or `bearer_token:`; sent as "Authorization: Bearer ..."
```

With no `payload`, the request body is this JSON:

```json
{
  "title": "NAS is failed",
  "body": "Pool tank is DEGRADED\nHost: nas.lan",
  "status": "failed",
  "kind": "problem",
  "monitor": {"id": "nas-zfs", "name": "NAS", "host": "nas.lan"},
  "url": "https://vigil.lan/monitor/nas-zfs",
  "timestamp": "2026-09-24T22:10:00-05:00"
}
```

`kind` is `problem`, `reminder`, `recovered` or `test`.

Set `payload` to send something else. A mapping or list is sent as JSON, and a string is sent as
plain text. `{name}` placeholders in any string are filled in, and values are escaped
correctly in JSON:

```yaml
    payload:
      text: "{title} on {host}: {url}"
```

The placeholders are `{title}`, `{body}`, `{status}`, `{kind}`, `{monitor_id}`, `{monitor_name}`,
`{host}`, `{url}` and `{timestamp}`. Any other `{...}` is left as it is.

## ntfy

An `ntfy` channel publishes to an [ntfy](https://ntfy.sh) topic, so notifications reach your
phone. Tapping one opens the monitor's page.

```yaml
channels:
  - id: phone
    type: ntfy
    url: https://ntfy.sh          # the default; or your own server
    topic_file: /run/secrets/ntfy_topic   # or `topic:`
    token_file: /run/secrets/ntfy_token   # optional access token, or `token:`
    # username: vigil             # or a user and `password` / `password_file`
    priority: {failed: urgent}
    tags: {failed: "rotating_light,server"}
```

On the public ntfy.sh server anyone who knows a topic's name can read it, so choose a name that
is hard to guess, and keep it in `topic_file`.

`priority` is `min`, `low`, `default`, `high`, `max`/`urgent`, or 1 to 5. `tags` is a list or a
comma-separated string. ntfy shows tags that name an emoji as that emoji. Like the desktop
settings, either one can be one value or set per status:

| Status        | Default priority | Default tags       |
|---------------|------------------|--------------------|
| `failed`      | `high`           | `rotating_light`   |
| `warning`     | `default`        | `warning`          |
| `unavailable` | `default`        | `grey_question`    |
| `recovered`   | `low`            | `white_check_mark` |

## Email

An `smtp` channel emails each notification. The subject is the notification's title, and the body
is its text followed by the monitor's link.

```yaml
channels:
  - id: mail
    type: smtp
    host: smtp.example.com
    security: starttls           # starttls (default, port 587), tls (port 465) or none (port 25)
    # port: 587                  # only when the server uses another port
    username: vigil@example.com
    password_file: /run/secrets/smtp_password   # or `password:`
    from: vigil@example.com      # defaults to `username`
    to: [me@example.com, oncall@example.com]    # or one address, or a comma-separated string
    subject_prefix: "[Vigil] "   # optional
```

## Apprise

An `apprise` channel sends each notification through [Apprise](https://github.com/caronc/apprise),
which reaches most chat, push and SMS services: Discord, Slack, Telegram, Matrix, Pushover,
Gotify, Teams, Signal and around a hundred more. Each service is one Apprise URL; see
[Apprise's list](https://github.com/caronc/apprise/wiki) for the format.

```yaml
channels:
  - id: chat
    type: apprise
    urls_file: /run/secrets/apprise_urls   # one URL per line; lines starting with # are skipped
    # urls: ["tgram://bottoken/ChatID"]    # or inline, as a list or a single URL
```

Apprise URLs usually carry the service's token, so prefer `urls_file`. The channel needs the
`apprise` Python package, which the Nix package includes; with pip, install `vigil[apprise]`. Apprise
shows failures, warnings and recoveries with the service's own colors or icons where it has them.

## Checking a channel

The bell icon in the dashboard's header lists every channel with a **Send test** button. From a
script, use the API:

```bash
curl -X POST -u admin https://vigil.lan/api/notifications/laptop/test
```

This sends a test notification and returns `{"sent": true}`, or the error if it could not be
delivered. A delivery that fails during normal operation is retried twice. If it still fails, it
appears in the events feed.
