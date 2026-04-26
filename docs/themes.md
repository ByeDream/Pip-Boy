# Pip-Boy TUI Themes

This guide covers authoring a Pip-Boy TUI theme: the schema, the layout
contract, the constraints, and how to install one without modifying
the package.

> **TL;DR** — A theme is three files in a slug-named directory.
> Drop the directory under `<workspace>/.pip/themes/` and run
> `/theme refresh`. No Python, no widget rearrangement, no shell-out
> hooks; only colours, borders, padding, optional ASCII art, and a
> handful of widget toggles.

## Where themes live

Themes live in **one** directory: `<workspace>/.pip/themes/`. Pip-Boy
seeds a few example themes there on first boot (e.g. `wasteland/`,
`vault-amber/`) and from that point on the directory is yours to
edit, extend, or prune. There is no "built-in vs local" distinction:
the seeded examples are ordinary theme directories that happen to ship
with the package, and the scaffold respects deletions — once you
remove a seeded theme it stays gone across reboots.

`pip-boy doctor` lists everything that was found, plus a `Skipped`
section for any theme whose manifest failed to validate — broken
themes never crash the host.

## Anatomy of a theme

```
<slug>/
├── theme.toml      # required — manifest (palette, metadata, toggles)
├── theme.tcss      # required — Textual CSS
├── banner.txt      # optional — big ASCII title (≤ 32 cols × 6 rows)
├── deco.txt        # optional — small decoration (≤ 32 cols × 6 rows)
└── art.txt         # optional legacy — single block (≤ 32 cols × 8 rows);
                    #                    used only when `banner.txt` is absent
```

The `<slug>` directory name MUST match `theme.name` in `theme.toml`,
and MUST satisfy: lowercase letters, digits, dashes, starts with a
letter (e.g. `vault-amber`, `terminal-green-7`).

### `theme.toml`

The manifest is a small TOML file with two sections: `[theme]` for
metadata and widget toggles, and `[palette]` for the colour table.

```toml
[theme]
name           = "vault-amber"
display_name   = "Vault Amber"
version        = "0.1.0"
author         = "You"
description    = "Amber-on-black retro Vault-Tec console palette."
show_art       = true       # render `art.txt` in the side pane
show_app_log   = true       # render the app-log pane
show_status_bar = true      # render the top status bar
footer_template = "<{tools} tools · {turns} turns · {elapsed_s}s · ${cost}>"

[palette]
background      = "#1a0e00"
foreground      = "#ffb000"
accent          = "#ffb000"
accent_dim      = "#7a5400"
user_input      = "#ffd97f"
agent_text      = "#ffb000"
thinking        = "#a07000"
tool_call       = "#ffe27a"
log_info        = "#ffb000"
log_warning     = "#ffd34d"
log_error       = "#ff6b3d"
status_bar      = "#3a2200"
status_bar_text = "#ffd97f"
```

#### Palette tokens (locked v1)

Every key listed below is **required**; missing or extra keys fail
manifest validation and the theme is skipped.

| Token | Used in |
|---|---|
| `background` | `Screen` background |
| `foreground` | Default text colour |
| `accent` | Borders, status-bar text, primary highlights |
| `accent_dim` | Inactive borders, panel separators |
| `user_input` | Echoed user-typed lines |
| `agent_text` | Streamed assistant replies |
| `thinking` | Extended-thinking deltas (italic) |
| `tool_call` | `[tool: name args]` traces |
| `log_info` / `log_warning` / `log_error` | Records routed via `TuiLogHandler` |
| `status_bar` / `status_bar_text` | Top status bar background / text |

Each value must be a CSS-style hex colour: `#RGB` or `#RRGGBB`.

#### `footer_template`

A Python `str.format` string applied to the per-turn footer. Available
fields: `{tools}` (tool-call count), `{turns}` (turns this session),
`{elapsed_s}` (whole seconds), `{cost}` (USD, 4 decimal places). Keep
it under ~80 characters — long footers wrap and obscure the input
box on narrow terminals.

### `theme.tcss`

Textual CSS for the locked widget topology. The file is loaded as the
app's `CSS` and applied on mount; you reference colours via Textual
design tokens (`$surface`, `$text`, `$accent`, …). Those tokens are
**not** read directly from `[palette]` — at boot,
`pip_agent.tui.textual_theme.textual_theme_from_bundle` registers a
Textual `Theme` named `pipboy-<slug>` whose `ColorSystem` is derived
from the manifest palette, then `PipBoyTuiApp` sets `App.theme` to that
name. Without that bridge, `$accent` would stay on Textual's default
orange even when the manifest says "Wasteland Radiation".

**Widget IDs (themes MUST NOT change):**

| ID | Role |
|---|---|
| `#status-bar` | Top status row (1 line tall) |
| `#main` | Horizontal split between agent + side panes |
| `#agent-pane` | 1-fr column hosting the agent log + input |
| `#agent-log` | `RichLog` for assistant text + tool traces |
| `#input` | `Input` widget (operator types here) |
| `#side-pane` | Fixed 34-col column: banner / status / log |
| `#side-top` | Vertical container grouping banner + deco + clock |
| `#pipboy-banner` | `Static` showing `banner.txt` (theme title) |
| `#pipboy-deco` | `Static` showing `deco.txt` (small decoration) |
| `#pipboy-clock` | `Static` date + time, re-drawn every second |
| `#side-status` | `Static` snapshot of host state (agent/model/…) |
| `#app-log` | `RichLog` for stdlib `logging` records |

You may style any of those IDs. You may add custom classes on the
`agent-log` lines (`.agent-thinking`, `.agent-tool`, `.agent-error`,
`.log-warning`, `.log-error`). You **may not** dock new widgets, swap
ratios in a way that hides a pane, or rearrange the topology — those
choices live in `pip_agent/tui/app.py` and are guarded by the
snapshot tests.

### `banner.txt`, `deco.txt`, `art.txt` (optional)

Plain UTF-8 ASCII. The side pane is 34 columns wide with 1-column
horizontal padding, so the usable budget is **32 columns**.

| File | Max cols | Max rows | Rendered in |
|---|---|---|---|
| `banner.txt` | 32 | 6 | `#pipboy-banner` (big title) |
| `deco.txt` | 32 | 6 | `#pipboy-deco` (small motif under banner) |
| `art.txt` | 32 | 8 | `#pipboy-banner` fallback — used only when `banner.txt` is absent |

Over-long lines are right-trimmed; over-long files are bottom-trimmed.
The bundle records `banner_truncated` / `deco_truncated` /
`art_truncated` so `pip-boy doctor` can flag silently-clipped assets.

If `show_art = false` in the manifest, all three slots are hidden
and the files are ignored.

## Starter theme: `terminal-green`

Save these three files to `<workspace>/.pip/themes/terminal-green/`
and run `/theme refresh` — no restart required.

### `theme.toml`

```toml
[theme]
name           = "terminal-green"
display_name   = "Terminal Green"
version        = "0.1.0"
author         = "starter"
description    = "Bright green-on-black, evenly lit; minimal accents."
show_art       = false
show_app_log   = true
show_status_bar = true
footer_template = "[{tools} tools | {turns} turns | {elapsed_s}s | ${cost}]"

[palette]
background      = "#000000"
foreground      = "#33ff33"
accent          = "#33ff33"
accent_dim      = "#0d4d0d"
user_input      = "#bbffbb"
agent_text      = "#33ff33"
thinking        = "#0d4d0d"
tool_call       = "#a0ffa0"
log_info        = "#33ff33"
log_warning     = "#ffe066"
log_error       = "#ff5050"
status_bar      = "#0d4d0d"
status_bar_text = "#33ff33"
```

### `theme.tcss`

```text
Screen {
    background: $surface;
    color: $text;
    layers: base overlay;
}

#status-bar {
    dock: top;
    height: 1;
    background: $boost;
    color: $text;
    padding: 0 1;
    text-style: bold;
}

#main {
    layout: horizontal;
    height: 1fr;
}

#agent-pane {
    width: 3fr;
    height: 1fr;
    layout: vertical;
}

#agent-log {
    height: 1fr;
    background: $surface;
    color: $text;
    border: none;
    padding: 0 1;
    scrollbar-gutter: stable;
}

#input {
    height: 3;
    border: round $accent;
    background: $surface;
    color: $accent;
}

#input:focus {
    border: round $accent;
    background: $surface;
}

#side-pane {
    width: 1fr;
    height: 1fr;
    layout: vertical;
    border-left: vkey $accent-darken-2;
    padding: 0 1;
}

#pipboy-art {
    height: auto;
    color: $accent;
    text-align: center;
    padding: 1 0;
}

#app-log {
    height: 1fr;
    background: $surface;
    color: $text-muted;
    border-top: hkey $accent-darken-2;
    padding: 0 1;
}

.agent-thinking {
    color: $text-muted;
    text-style: italic;
}

.agent-tool {
    color: $accent;
    text-style: dim;
}

.agent-error {
    color: $error;
    text-style: bold;
}

.log-warning {
    color: $warning;
}

.log-error {
    color: $error;
}
```

(`art.txt` omitted because the manifest has `show_art = false`.)

## Switching themes at runtime

The persisted selection lives in `<workspace>/.pip/host_state.json`.
Pip-Boy reads it at boot; when unset it falls back to `wasteland`.

From inside Pip-Boy:

```text
/theme list                # all installed themes (active marked with *)
/theme set <slug>          # switch NOW + persist the new default
/theme refresh             # rescan .pip/themes/ (no restart)
```

`/theme set` applies the new bundle to the live TUI in one shot —
colours, TCSS, ASCII art, and status-bar display name all flip, and
the `#agent-log` conversation history is preserved. There is no
environment-variable override and no restart loop.

`/theme refresh` is what you want after dropping a new theme directory
into `.pip/themes/` or hand-editing a manifest; it prints the diff
(added / removed / broken) and leaves the active theme alone. Follow
it up with `/theme set <new-slug>` to actually switch.

## Validating a theme locally

```text
pip-boy doctor             # full report incl. theme catalogue + issues
pip-boy doctor --no-tui    # same, but force user_optout in the ladder
```

The `Themes` section lists every theme found, marks the active one
with `*`, and surfaces broken themes in an `issues` block with the
first line of the validation error. The `Recent capability log`
section shows the last 20 boot decisions so you can see whether your
new theme was actually loaded on the most recent run.

## Known constraints (v1)

These are deliberate limits, not oversights — they exist so themes
stay declarative and the host stays predictable. Each one came from
shipping the underlying widget topology, capability ladder, and
pump; revisiting them later is fine, but unilateral relaxation is
how broken themes start crashing host boots.

* **No Python entry point.** Themes are pure data; the loader reads
  TOML, TCSS, and text. Signed Python plugins may land later but
  only after the data-driven surface is stable.
* **No widget rearrangement.** Themes own appearance, not topology.
  Layout decisions belong to `pip_agent/tui/app.py` and are guarded
  by SVG snapshot tests. `show_*` toggles can hide panes (via CSS
  `display`) but cannot move them.
* **No background work, network, subprocesses, or stdout writes.**
  Themes can't run code, so this is enforced by construction. If a
  theme directory ships a `.py` file, the loader ignores it.
* **Slug determinism.** Slug, directory name, and `theme.name` MUST
  match exactly; the loader rejects mismatches up front. This keeps
  `/theme set <slug>` and the on-disk state file in sync.
* **Palette is closed.** The 13 tokens above are the entire
  palette; introducing a new one is a v1.x contract change. Use
  TCSS `$accent-lighten-N` / `$accent-darken-N` if you need
  variations rather than expanding the palette.
* **ASCII art is bounded** (32 cols × 8 rows). Anything bigger is
  truncated; `pip-boy doctor` flags the truncation in the listing.

## Snapshot tests for new themes

If you want regression coverage on a theme (yours or an upstreamed
one), drop a driver under `tests/tui_snapshot_apps/<slug>.py`:

```python
from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump

_bundle = load_builtin_theme("<slug>")
_pump = UiPump()
app = PipBoyTuiApp(theme=_bundle, pump=_pump)
```

…then add the test entry in `tests/test_tui_snapshots.py` and run:

```bash
pytest --snapshot-update tests/test_tui_snapshots.py
```

Review the generated SVG in `tests/__snapshots__/test_tui_snapshots/`
visually before committing — the snapshot is the contract reviewers
will diff against on every future change.
