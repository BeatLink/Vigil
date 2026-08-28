# Lint follow-up checklist

What `ruff check vigil vigil_agent tests` still reports after the mechanical
fix pass (unused imports, whitespace) was applied. All remaining items are
docstring presence; the complexity ceiling (C901 = 21) currently passes and
only ratchets down from here.

CI command:

```
ruff check vigil vigil_agent tests
```

## D100 — module docstring missing (11)

- [ ] vigil/core/connectors/ssh_connector.py
- [ ] vigil/core/connectors/types.py
- [ ] vigil/core/exporters/influxdb.py
- [ ] vigil/core/exporters/prometheus.py
- [ ] vigil/core/settings/config_file.py
- [ ] vigil/core/ui/api.py
- [ ] vigil/core/ui/auth.py
- [ ] vigil/core/ui/components.py
- [ ] vigil/core/ui/layout.py
- [ ] vigil/core/ui/main_dashboard.py
- [ ] vigil/core/ui/orchestration.py

## D103 — public function docstring missing (42)

- [ ] vigil_agent/__main__.py — ?
- [ ] vigil_agent/protocol.py — ?
- [ ] vigil_agent/protocol.py — ?
- [ ] vigil_agent/protocol.py — ?
- [ ] vigil_agent/protocol.py — ?
- [ ] vigil_agent/protocol.py — ?
- [ ] vigil_agent/protocol.py — ?
- [ ] vigil_agent/protocol.py — ?
- [ ] vigil/core/connectors/ssh_connector.py — ?
- [ ] vigil/core/connectors/ssh_connector.py — ?
- [ ] vigil/core/database/database.py — ?
- [ ] vigil/core/exporters/influxdb.py — ?
- [ ] vigil/core/exporters/prometheus.py — ?
- [ ] vigil/core/ui/agent_endpoint.py — ?
- [ ] vigil/core/ui/api.py — ?
- [ ] vigil/core/ui/auth.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/components.py — ?
- [ ] vigil/core/ui/layout.py — ?
- [ ] vigil/core/ui/spec.py — ?
- [ ] vigil/core/ui/spec.py — ?
- [ ] vigil/core/ui/spec.py — ?
- [ ] vigil/core/ui/theme.py — ?
- [ ] vigil/core/ui/theme.py — ?
- [ ] vigil/__main__.py — ?
- [ ] vigil/plugins/base/plugin_helpers.py — ?
- [ ] vigil/plugins/base/plugin_helpers.py — ?
- [ ] vigil/plugins/base/plugin_helpers.py — ?
- [ ] vigil/plugins/base/plugin_helpers.py — ?
- [ ] vigil/plugins/base/plugin_helpers.py — ?
