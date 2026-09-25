# Notifications plan

The plan for Vigil's notifications, modelled on the notification providers Uptime Kuma supports.
How to use what exists today is in [notifications.md](notifications.md).

## What Uptime Kuma supports

Uptime Kuma ships **109 providers** (`server/notification-providers/`, September 2026). They fall
into five groups:

| Group | Kuma providers (examples) | Plan for Vigil |
|---|---|---|
| Generic | webhook, smtp, apprise | Built in |
| Push and chat apps | ntfy, gotify, pushover, telegram, discord, slack, matrix, mattermost, rocket-chat, teams, google-chat, signal, home-assistant, nextcloudtalk, pushbullet, bark, threema | Build about 8 in; reach the rest through Apprise |
| On-call | pagerduty, opsgenie, grafana-oncall, goalert, squadcast, splunk, signl4, pagertree, alerta, keep | Build pagerduty and opsgenie in. An alert is given an id and closed when the monitor recovers, which Apprise cannot do |
| Regional SMS and messengers | twilio, plivo, 46elks, clicksendsms, aliyun-sms, dingding, feishu, wecom, line, serverchan, pushplus, kook, vk | Through Apprise, or a webhook with a message template |
| Out of scope | google-sheets, webpush, HaloPSA, jira-service-management, clickup, lunasea | Skip: ticketing or logging tools, or they need a browser subscription |

**Apprise** is the shortcut. It is a Python library that covers most of the Kuma list, and Kuma
itself uses it through its command line tool. Apprise plus about ten built-in providers covers
roughly what Kuma does, without porting 109 files.

Kuma has no desktop notifications. Vigil has them, through its agent.

## How it fits Vigil

- **Detecting changes:** the Notification Engine (`vigil/core/notifications/`) subscribes to the
  change bus. Every collection cycle writes a status, so it sees each cycle. It hands each write
  to the event loop and never sends anything on the writer's thread.
- **Restarts:** it starts from the status history Vigil loads at startup, so a restart does not
  announce a problem again.
- **Sending:** each delivery runs as its own task and is retried twice, after 5s and 30s. A
  delivery that still fails is written to the events feed. Events never trigger notifications,
  so this cannot loop.
- **Message:** the title is `<monitor> is failed`, `is still failed` or `recovered`. The body
  gives the monitor's latest event as the reason, or how long it was down, and its host. A link
  to `/monitor/<id>` is added when `notifications.base_url` is set.

## Rules, mapped from Kuma

| Kuma | Vigil |
|---|---|
| Only a change between down and up notifies | `on: [failed]` by default, with `recovery: true`. `warning` and `unavailable` are opt-in |
| Retries before marking down | `after: N` problem cycles in a row |
| Resend interval | `repeat: 1h` while the problem lasts |
| Default enabled / apply to all monitors | `notifications.defaults` apply to every monitor |
| Per-monitor notification list | `notify:` on a monitor, passed down from its group. `notify: false` mutes it |
| Maintenance | A mute switch per monitor, saved as a setting (not built yet) |

A group never notifies for itself. Its status only repeats its children's.

`unavailable` is off by default. When a host goes down, every monitor on it turns unavailable at
once, while the host's availability monitor reports failed. That gives one notification instead
of one per monitor.

## Decisions taken

- **Config key:** `notifications:` replaces the unused `alerting:` list.
- **`warning`:** off by default.
- **`unavailable`:** off by default, for the reason above.
- **Desktop delivery:** a `notify` frame on the agent WebSocket, not a shell command, so a monitor
  name cannot inject shell. The agent runs `notify-send` with a default action and opens the link
  with `xdg-open` when it is clicked.
- **Desktop agent:** runs as the logged-in user, because only that user's processes can reach the
  session bus. It is a separate, `notify_only` agent that refuses commands. The NixOS agent
  module creates it as a user service.
- **Monitor links:** the dashboard has `/monitor/<id>` and `/events` routes, and changing the view
  updates the address bar.

## Phases

1. **Done:** the engine, rules, config, the `desktop` channel with icon and urgency settings, the
   notify-only agent, monitor links, the NixOS user service, and
   `POST /api/notifications/<channel>/test`.
2. **Generic channels:** webhook and ntfy are done: a JSON body or a payload template for the
   webhook, and a tap link, priority and tags for ntfy. Every secret can be given as `*_file`.
   smtp and Apprise (the optional extra `vigil[apprise]`) are done too.
3. **More providers and UI:** the dashboard part is done: a mute switch on each monitor's page,
   and a header dialog that tests channels and lists mutes. Still to do: telegram, discord,
   slack, gotify, pushover, matrix, home-assistant and signal built in.
4. **On-call and flapping:** flapping detection and grouping are done. Still to do: pagerduty and
   opsgenie, using the monitor id as the alert id so a recovery closes the alert. Maintenance
   windows are next.
