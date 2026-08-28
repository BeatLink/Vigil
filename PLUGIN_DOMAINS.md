# Plugin domains

A review of the plugin tree against one question: **which of the 36 plugins are the same
functional domain wearing different names, and which are genuinely their own?**

The axis is functional domain — what a monitor is *about* — not collection cost. Cadence used
to force the boundary in places (`smartctl` cannot share a schedule with a `/proc` read), but
per-module intervals now remove that constraint, so cost no longer has a vote. See
[Per-module intervals](#per-module-intervals) below.

The tree already has a settled answer for "many signals, one host domain": the opt-in module
pattern in [system_stats.py](vigil/plugins/system_stats.py), [network.py](vigil/plugins/network.py)
and [disks.py](vigil/plugins/disks.py), now sharing
[module_plugin.py](vigil/plugins/base/module_plugin.py). The merges below mostly finish that
pattern rather than introduce a new one.

---

## The six domains

### 1. Storage

**Merge** [disks.py](vigil/plugins/disks.py) (smart/zfs/io) + [filesystems.py](vigil/plugins/filesystems.py)
+ [disk_space.py](vigil/plugins/disk_space.py) + [raid.py](vigil/plugins/raid.py)
+ [folders.py](vigil/plugins/folders.py)

Everything answering *how is this host's persistent storage doing* — device health, array
health, capacity, consumption.

- [disk_space.py](vigil/plugins/disk_space.py) is [filesystems.py](vigil/plugins/filesystems.py)
  narrowed to one path: same `df -B1`, same threshold coloring. It exists only because
  `filesystems` auto-discovers and cannot be pinned. That is a config key (`paths:`), not a plugin.
- [raid.py](vigil/plugins/raid.py) is 111 lines parsing `/proc/mdstat` — the direct sibling of the
  `zfs` module already inside `disks`. Pool health for mdadm instead of ZFS.
- [folders.py](vigil/plugins/folders.py) is `du` capacity on named paths. Same question as `df`,
  different granularity. It was previously excluded on cadence grounds; on a functional axis it
  belongs, and its cadence is now expressible as a module `interval`.

Modules: `smart`, `zfs`, `md`, `io`, `filesystems`, `paths`.

### 2. Workloads

**Merge** [systemd_service.py](vigil/plugins/systemd_service.py) + [service_list.py](vigil/plugins/service_list.py)
+ [containers.py](vigil/plugins/containers.py) + [vms.py](vigil/plugins/vms.py)
+ [processes.py](vigil/plugins/processes.py)

The largest merge the domain view unlocks. All five are structurally identical: enumerate units
of execution, report per-item state, expose per-item lifecycle actions.

| Plugin | Runtime | Action shape |
|--------|---------|--------------|
| [systemd_service.py](vigil/plugins/systemd_service.py) | systemd (one unit, with journal) | `restart_service`, `stop_service`, `enable_service` |
| [service_list.py](vigil/plugins/service_list.py) | systemd (all units) | per-unit restart, optional unit-file edit |
| [containers.py](vigil/plugins/containers.py) | podman/docker | `restart:{name}` |
| [vms.py](vigil/plugins/vms.py) | libvirt | `start:{name}`, `shutdown:{name}` |
| [processes.py](vigil/plugins/processes.py) | bare processes | `kill` |

Same table, same row actions, four runtimes. `systemd_service` and `service_list` in particular
differ only in arity — one unit with a journal tail vs. all units — the same relationship
`disk_space` has to `filesystems`. One plugin where `services: []` means all and
`services: [foo]` means these, with journals.

Modules: `systemd`, `containers`, `libvirt`, `processes`.

### 3. Reachability

**Merge** [uptime.py](vigil/plugins/uptime.py) + [ports.py](vigil/plugins/ports.py) + a new generic `http`

*Can this endpoint be reached, and how fast* — ICMP, TCP, HTTP. Nothing under `vigil/core/`
special-cases the `uptime` plugin type, so it is not load-bearing as a separate type.

This is also where the thin service checks land. [calibre_web.py](vigil/plugins/calibre_web.py),
[radicale.py](vigil/plugins/radicale.py) and [openbooks.py](vigil/plugins/openbooks.py) emit only
`*_ok` and `*_latency_ms` — roughly 120 lines each of boilerplate around "GET this URL with this
auth, check the body looks right". Their functional content is reachability, not their
application's domain. A generic `http` monitor with `url` / `auth` / `expect` retires all three.

This is the weakest recommendation in the document: it trades three readable, app-named files for
one parameterized one, which only pays off if more such services are coming.

### 4. Host telemetry

**Merge** `system_stats` + `network` + [cloud.py](vigil/plugins/cloud.py)

The first two are already merged. [cloud.py](vigil/plugins/cloud.py) folds in as an inventory
module: provider, instance type and region are host facts, not a monitored service.

### 5. DNS

**Merge** [dns_record.py](vigil/plugins/dns_record.py) + [ddns_updater.py](vigil/plugins/ddns_updater.py)

Resolution correctness and record maintenance are one domain, and
[ddns_updater.py](vigil/plugins/ddns_updater.py) already resolves the record it is about to update.

[unbound.py](vigil/plugins/unbound.py) and [pihole.py](vigil/plugins/pihole.py) do **not** belong
here. They are resolver products with product-specific statistics, which puts them in (6).

### 6. Applications — one plugin per service, unchanged

[pihole.py](vigil/plugins/pihole.py), [unbound.py](vigil/plugins/unbound.py),
[qbittorrent.py](vigil/plugins/qbittorrent.py), [syncthing.py](vigil/plugins/syncthing.py),
[borg.py](vigil/plugins/borg.py), [frigate.py](vigil/plugins/frigate.py),
[freshrss.py](vigil/plugins/freshrss.py), [trilium.py](vigil/plugins/trilium.py),
[traccar.py](vigil/plugins/traccar.py), [mosquitto.py](vigil/plugins/mosquitto.py),
[blockurl.py](vigil/plugins/blockurl.py).

Each application is its own domain, and each emits signals no other plugin can produce —
`notes_active`, `devices_stale`, `feeds_stale`, `camera_fps`, `detector_inference_ms`. Do not
group them into a "backup" or "media" super-domain: that is a UI taxonomy, and
[group.py](vigil/plugins/group.py) already provides it at config level.

### Outside the scheme

[command.py](vigil/plugins/command.py) (escape hatch), [vigil_self.py](vigil/plugins/vigil_self.py),
[push.py](vigil/plugins/push.py), [group.py](vigil/plugins/group.py) — engine-level concerns, not
target domains.

---

## What separates

Domain-first cutting makes files large, so the split is *within* a plugin, not between plugins.
`system_stats` is already 700+ lines; merged storage and workload monitors would reach similar
size. One directory per domain, one file per module, an `__init__.py` assembling the registry —
the same shape the rest of the tree uses for grouping.

The plugin identity is right in every case; the single file is not.

---

## Per-module intervals

The prerequisite for all of the above, now implemented.

A module in the `modules` block may set its own `interval`:

```yaml
- name: "Ragnarok Disks"
  type: "disks"
  interval: 1m
  modules:
    smart:
      interval: 1h
    io: {}
```

The monitor's `interval` is the floor, not the schedule. Each cycle the plugin issues the commands
of only the modules due, and a module asking for less than the monitor's interval simply collects
every cycle. A resting module contributes no result, so the plugin holds its last status and folds
that into the worst-status roll-up — an hourly `smart` check does not read as online for the 59
minutes between probes. Metrics are deliberately not carried forward: `latest_metric` already
returns the last value written, and forging samples would put points on a chart that were never
measured.

Implemented on [module_plugin.py](vigil/plugins/base/module_plugin.py), the scaffolding
`system_stats`, `network` and `disks` now share — the `Module` contract, the `modules` resolver,
the severity ordering, and a `ModularPlugin` base that concatenates due commands, slices results
back out, and assembles the composite `UI_SPEC`. That scaffolding was previously triplicated
verbatim across the three files; consolidating it removed 405 lines and meant the interval
feature was written once.

Covered by [tests/plugins/test_module_intervals.py](tests/plugins/test_module_intervals.py)
(dueness, result slicing across a skipped module, status carry-forward, UI stability).
