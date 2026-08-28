# Theme Tokens

Per-token overrides for the Halon theme, set under the `theme:` block in
`config.yaml` — see the [Theme](../README.md#theme) section of the README.


Individual tokens can be overridden from the same block. Each key maps to one
token and applies to **both** schemes, so an override that only suits one of
them is yours to audit — the shipped values are contrast-audited in both.

| Field              | Token                   | Role                                     |
|--------------------|-------------------------|------------------------------------------|
| `scheme`           | —                       | `auto`, `light`, or `dark`                |
| `primary`          | `--accent`              | Links, focus, the one filled button       |
| `background`       | `--surface-default`     | Cards and panels                          |
| `background_muted` | `--surface-root`        | The page behind them                      |
| `text`             | `--text-body`           | Body text                                 |
| `text_muted`       | `--text-secondary`      | Labels, captions, icons                   |
| `status_online`    | `--status-success`      | A monitor that is up                      |
| `status_warning`   | `--status-warning-text` | A monitor in warning                      |
| `status_failed`    | `--status-danger`       | A monitor that has failed                 |
| `status_offline`   | `--text-tertiary`       | A monitor not reporting                   |

```yaml
theme:
  scheme: auto
  primary: "#7c3aed"
```
