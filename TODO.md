# TODO — Feature gaps vs LNXlink and Valent

Gap analysis of Vigil's 45 plugins against two adjacent Linux projects, 2026-08-11.

- **[LNXlink](https://github.com/bkbilly/lnxlink)** — 60 modules. An MQTT agent that runs *on* the
  monitored host and publishes to Home Assistant. Overlaps Vigil's host monitoring substantially, so
  most real gaps come from here.
- **[Valent](https://github.com/andyholmes/valent)** — 26 plugins. A GTK implementation of the KDE
  Connect protocol (phone↔desktop pairing). Almost entirely device-sync; included for completeness,
  with the handful of transferable ideas separated from the non-goals.

Items already on the [roadmap](docs/roadmap.md) are cross-referenced rather than repeated.
Nothing here is committed work — this is the candidate list.

---

## 1. LNXlink — actionable gaps

### 1a. New monitor types

- [ ] **Battery / UPS** — LNXlink `battery` tracks level and charging state for every connected
      battery. Vigil has no equivalent. Relevant for laptop-as-server nodes and, via `upower`/`apcupsd`/
      `nut`, for UPS monitoring — arguably the single most requested homelab monitor Vigil lacks.
      SSH-collectable from `/sys/class/power_supply/*`.
- [ ] **Pending package updates** — LNXlink `sys_updates` counts available packages and flags
      security updates. Vigil can approximate this with a `command` plugin, but a first-class type
      would normalize across `apt`/`dnf`/`pacman` and give a real metric to threshold on. The Nix
      half is covered: [nixos_upgrade.py](vigil/plugins/nixos_upgrade.py) compares the running
      system closure to what its flake evaluates to, and carries the update and switch actions.
- [ ] **Reboot required** — LNXlink `required_restart`. [docs/plugins.md](docs/plugins.md#command)
      already shows this as a `command` example; promoting it to a dedicated plugin removes the
      per-distro shell snippet from every user's config. NixOS hosts already get this from
      `nixos_upgrade`'s `reboot_required` metric.
- [ ] **Network interface inventory** — LNXlink `interfaces` lists active NICs and their assigned
      IPs. The `network` plugin's `throughput` module auto-detects *one* interface but never reports the
      set of interfaces or their addressing. Useful for catching a dropped link or a changed DHCP lease.
- [ ] **Logged-in users / active sessions** — LNXlink `current_user` reports the active graphical
      user. The server-side reframing is `who`/`loginctl`/`last`: who is logged in, from where, since
      when. That is a legitimate security-adjacent monitor and Vigil has nothing in the space.
- [ ] **Per-plugin collection latency** — LNXlink `inference_time` measures how long each module's
      data collection takes. Vigil has [vigil_self.py](vigil/plugins/vigil_self.py) but does not
      expose per-monitor cycle duration. Would make SSH contention and slow targets visible, and
      pairs naturally with the `MaxSessions` queuing behaviour documented at
      [docs/plugins.md](docs/plugins.md#ssh-config).

### 1b. Extensions to existing plugins

- [ ] **GPU: AMD and Intel** — LNXlink `gpu` covers NVIDIA, AMD, and Intel.
      [gpu.py](vigil/plugins/gpu.py) is `nvidia-smi`-only and reports offline on anything else.
      Add `amdgpu_top`/`sysfs` and `intel_gpu_top` paths.
- [ ] **Containers: update-available detection and prune** — LNXlink `docker` checks whether a newer
      image exists and can prune. [containers.py](vigil/plugins/containers.py) counts running/stopped
      and offers restart only. An `image_update_available` metric plus a Prune action would be a
      genuine addition.
- [ ] *(Already on roadmap)* Per-core CPU breakdown — LNXlink's `cpu` exposes load 1m/5m/15m as
      attributes; Vigil already splits these into `load_pct_*` metrics, so only the per-core split
      is outstanding.

### 1b2. Plugin granularity

Every host signal is its own monitor — `cpu`, `memory`, `load`, `temperature`, `interrupts`,
`gpu`, `oom`, `throughput`, `connections`, `wifi`, `smart`, `zfs`, `md`, `disk_io` — each with
its own thresholds, interval, status and history, grouped per host and per domain in config.
They share only [signal_plugin.py](vigil/plugins/base/signal_plugin.py): the severity ordering
and the page assembly.

- [ ] **Shared delta sampling** — `cpu`, `interrupts`, `disk_io` and `throughput` each take their
      own 1s sample, so a host running several pays one sleep apiece. Collapsing them would need
      a shared snapshot the four monitors slice, which cuts against one-monitor-one-signal;
      open whether it is worth it.
- [ ] **Filesystem monitors** — whether [filesystems.py](vigil/plugins/filesystems.py),
      `disk_space` and `folders` should stay three plugins is open. `disk_space` and `folders`
      exist so a named path gets its *own* status and alert, which is the same argument the
      per-signal split rests on.

### 1c. Control actions

Vigil positions itself on target control ([README.md:5](README.md#L5)), but has no host-level power
or hardware controls. All four map cleanly onto the existing
`plan_action()`/`interpret_action()` contract in
[plugin_base.py:104-119](vigil/plugins/base/plugin_base.py#L104-L119).

- [ ] **Host power actions** — reboot / shutdown / suspend (LNXlink `restart`, `shutdown`,
      `suspend`). Needs a confirmation gate and an allowlist; design principle #5 ("Fail-Safe
      Control") already asks for confirmable, logged actions.
- [ ] **Wake-on-LAN** — LNXlink `wol` only toggles WoL support on the NIC. The more valuable Vigil
      feature is the inverse: *sending* a magic packet to wake a host that an `uptime` monitor
      reports down. Vigil is the only one of the three projects positioned to do this, since it sits
      on the network watching the offline host.
- [ ] **Power profile** — LNXlink `power_profile` toggles performance/balanced/power-saver via
      `power-profiles-daemon`.
- [ ] **Next-boot selection** — LNXlink `boot_select` picks the OS for the next boot. Niche; pairs
      with the reboot action for dual-boot or kernel-testing hosts.

### 1d. Engine and platform capabilities

- [ ] **Config hot-reload** — LNXlink `watch_changes` watches its config file and restarts on
      change. Vigil reads `config.yaml` once at startup
      ([engine.py:111-144](vigil/core/coordination/engine.py#L111-L144)); adding or retuning a
      monitor requires a full restart, dropping every persistent SSH connection. Reloading the
      plugin tree in place is the highest-leverage item in this section.
- [ ] **Out-of-tree plugin loading** — LNXlink auto-imports modules from a directory, including
      user-supplied ones. Vigil resolves a plugin type by importing `vigil.plugins.{type}`
      ([engine.py:120](vigil/core/coordination/engine.py#L120)), so a third-party plugin must be
      dropped inside the installed package. A configurable plugin path would let people extend Vigil
      without vendoring it.
- [ ] **Runtime log-level control** — LNXlink `logging_level` changes verbosity without a restart.
      Small, and genuinely useful while debugging a flaky target.
- [ ] *(Already on roadmap)* MQTT export — LNXlink's entire integration story is MQTT autodiscovery.
      The roadmap's "Additional export backends" item ([docs/roadmap.md](docs/roadmap.md)) is the
      right shape: an HA-discovery MQTT exporter next to the Prometheus and InfluxDB ones would make
      every Vigil monitor a Home Assistant entity without an agent.

### 1e. Raspberry Pi / hardware — decide in or out

LNXlink supports these because it already runs on the box. Vigil would need a remote equivalent, and
all three are arguably outside "network and systems monitor".

- [ ] `gpio` — read/write Pi GPIO pins.
- [ ] `ir_remote` — IR send/receive.
- [ ] `fingerprint` — R503 scanner over UART.

### 1f. LNXlink modules that are explicit non-goals

Desktop/session features with no server-monitoring meaning. Recorded so they are not re-raised:
`active_window`, `audio_select`, `beacondb`, `bluetooth`, `brightness`, `camera_used`, `clipboard`,
`display_env`, `fullscreen`, `gamepad`, `idle`, `keep_alive`, `keyboard_hotkeys`, `media`, `mouse`,
`microphone_used`, `notify`, `screen_onoff`, `screenshot`, `send_keys`, `speaker_used`,
`speech_recognition`, `steam`, `webcam`, `xdg_open`.

Also excluded as agent-lifecycle concerns that do not apply to an agentless design: `update`
(self-update), `update_entities`, `statistics` (opt-in telemetry), `lwt` (MQTT last-will —
Vigil's DB *is* the availability record), `restful` (Vigil already has a REST API),
`bash` (covered by [command.py](vigil/plugins/command.py)), `systemd` (covered by
[systemd_service.py](vigil/plugins/systemd_service.py) and
[service_list.py](vigil/plugins/service_list.py)).

---

## 2. Valent — mostly non-goals, with three transferable ideas

Valent is a KDE Connect client. Nine of its 26 plugin directories are platform backends, not
features at all (`bluez`, `eds`, `fdo`, `gnome`, `gtk`, `lan`, `pipewire`, `pulseaudio`, `xdp`).
Of the remaining 17, almost everything is phone↔desktop sync.

### Worth stealing

- [ ] **Per-device pairing and certificate auth** — Valent pairs each device with its own
      certificate identity rather than a shared password. Vigil currently offers a single HTTP Basic
      Auth credential for the whole dashboard and API
      ([README.md](README.md#authentication)), with HTTPS still open on the roadmap. A
      per-client token/identity model is the natural next step and would also give the REST API and
      the `push` endpoint a common auth story instead of `push`'s bespoke per-monitor token.
- [ ] **Battery monitoring** — Valent `battery` reports level and charging state, independently
      confirming §1a's battery item. Two of two surveyed projects have it; Vigil has none.
- [ ] **Identify-this-host action** — Valent `findmyphone` makes a device ring. The datacenter
      analogue is a locate action: `ledctl` to blink a drive/chassis LED, or a console beep, to
      physically identify a machine you are SSH'd into. Marginal, but it fits the control model.

### Adjacent to existing roadmap items

- `notification` (sync notifications) and `connectivity_report` (link/carrier status) are weak
  analogues of the roadmap's alerting item and of Vigil's existing
  [network.py](vigil/plugins/network.py) / [uptime.py](vigil/plugins/uptime.py). No new work implied.
- `ping` is covered by [uptime.py](vigil/plugins/uptime.py); `runcommand` by
  [command.py](vigil/plugins/command.py) plus the action framework.

### Non-goals

Device-sync features with no server-monitoring analogue: `clipboard`, `contacts`, `findmyphone`
(except as above), `lock`, `mousepad`, `mpris`, `presenter`, `sftp`, `share`, `sms`,
`systemvolume`, `telephony`.

---

## 3. Summary

| Source | Total plugins | Already covered by Vigil | Actionable gaps | Non-goals |
|---|---|---|---|---|
| LNXlink | 60 | 14 | 17 (+3 undecided RPi) | 26 |
| Valent  | 26 (17 features, 9 backends) | 2 | 3 | 12 |

Highest leverage, in order:

1. Config hot-reload (§1d) — removes the restart-to-reconfigure penalty that every other item makes worse.
2. Battery/UPS monitor (§1a) — the clearest missing monitor type, confirmed by both projects.
3. Host power actions + Wake-on-LAN (§1c) — WoL in particular is something neither comparison project
   can do and Vigil's central, agentless position makes natural.
4. MQTT/HA export (§1d) — already on the roadmap; delivers LNXlink's entire integration value
   without adopting its architecture.
