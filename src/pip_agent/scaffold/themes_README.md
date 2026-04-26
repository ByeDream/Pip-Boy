# Local Themes

Drop a theme directory under this folder to install it for the current
workspace without modifying the package. The discovery walker
(`pip_agent.tui.ThemeManager`) scans this directory at boot and merges
results with the built-in catalogue:

```
.pip/themes/<slug>/
    theme.toml      # required: manifest (name, palette, widget toggles)
    theme.tcss      # required: Textual CSS
    art.txt         # optional: ASCII art (≤ 32 cols × 8 rows)
```

Slug rules: lowercase letters, digits, and dashes; must start with a
letter; must match the directory name (`name = "<slug>"` in
`theme.toml`).

Conflicts: a local theme with the same slug as a built-in **overrides**
the built-in. The override is logged at boot.

Listing & switching from the CLI:

* `/theme list` — show all themes (built-in + local) with origin tags.
* `/theme show` — print the active slug + persisted preference.
* `/theme set <slug>` — persist the selection to
  `.pip/host_state.json`. Live reload is out of scope in v1; restart
  pip-boy to apply.

Environment override: set `PIP_TUI_THEME=<slug>` for a one-shot boot
to bypass the persisted preference.

Authoring guide: see `docs/themes.md` (added in Phase C).
