# Design system proposal: local extensions

The WPTK Design System (artifact version 1790705302-ba80, tokens.json v1) has no buttons, inputs, chips, status colours or dialog. Hypertracker builds them locally in `app/static/css/wptk-ext.css`. The DS artifact was not modified. This file is for the owner to approve, change or reject each item before anything goes upstream.

Contrast numbers come from `tools/contrast_check.py`, which reads the real CSS and takes the worst case across every ground the page can show (bg-0, bg-1, and each gradient blob at its peak alpha, with translucent surfaces composited on top). Text needs 4.5:1, control borders, focus rings and status markers 3:1. The full table is at the end.

## 1. Findings in the DS itself

| Finding | Worst case | Local handling |
|---|---|---|
| `line` clears 3:1 on bg-0 (3.30 Midnight, 3.37 Daylight) but not over the gradient blobs: 2.65:1 on the page in Midnight, 2.85:1 in Daylight, 2.95:1 on `surface`. | Decorative card and nav borders, so not a WCAG failure. Control borders are. | New `--line-control` for controls (item 2). `--line` stays on cards. |
| `accent-0-text` (link hover) is 4.54:1 on bg-0 as documented, but 3.65:1 on the page, 4.06:1 on `surface`, 4.31:1 on `surface-3` (nav) over the blobs. In Daylight 4.16:1 on the page. | Hover text only. | Hover on tokens and the nav wordmark no longer changes text colour (underline or border instead). |
| `tokens.css` in the artifact lags `tokens.json` (old `line`, no `accent-0-text`, no `shadow-sm`). `colors_and_type.css` is stale. | | Vendored `tokens.css` is regenerated from `tokens.json`. |
| Theme ids in `tokens.css` (`default`, `root-light`) differ from the bundle (`data-theme="light"`). | | Vendored copy follows the bundle. |
| ThemeToggle label is uppercase, README says no ALL CAPS. | | Sentence case locally. |

## 2. Tokens

Fragment ready for `tokens.json` (`color.tokens`), values per theme as `default` / `root-light`:

```json
[
  {"name": "line-control", "value": {"default": "#6a7fa3", "root-light": "#667a9c"},
   "usage": "Border of buttons, inputs, chips and the theme toggle. 3.71:1 (Midnight) and 3.49:1 (Daylight) on the page worst case, 4.1:1 or better on surface."},
  {"name": "focus-ring", "value": {"default": "{accent-0-text}", "root-light": "{accent-0-text}"},
   "usage": "Focus outline, hover and active control borders. 2px, offset 2px. Cobalt family, lighter in Midnight so it clears 3:1 over the backdrop blobs."},
  {"name": "accent-0-strong", "value": {"default": "#2d5bff", "root-light": "#2d5bff"},
   "usage": "Fill of the primary button. White text on it is 5.18:1."},
  {"name": "accent-0-strong-hover", "value": {"default": "#2450e6", "root-light": "#1f48e0"},
   "usage": "Primary button hover fill."},
  {"name": "on-accent", "value": {"default": "#ffffff", "root-light": "#ffffff"}, "usage": "Text on accent-0-strong."},
  {"name": "st-airborne", "value": {"default": "#34d399", "root-light": "#047857"}, "usage": "Status marker and border: airborne."},
  {"name": "st-ground", "value": {"default": "#fbbf24", "root-light": "#92400e"}, "usage": "Status marker and border: on ground; stale banner edge."},
  {"name": "st-scheduled", "value": {"default": "#8fa8ff", "root-light": "#2d5bff"}, "usage": "Status marker and border: scheduled."},
  {"name": "st-landed", "value": {"default": "#aab4c2", "root-light": "#4a5568"}, "usage": "Status marker and border: landed."},
  {"name": "st-delayed", "value": {"default": "#fb923c", "root-light": "#b45309"}, "usage": "Status marker and border: delayed."},
  {"name": "st-cancelled", "value": {"default": "#f87171", "root-light": "#b91c1c"}, "usage": "Status marker and border: cancelled."},
  {"name": "st-unverified", "value": {"default": "#c98bff", "root-light": "#7e22ce"}, "usage": "Status marker and border: unverified (dashed)."},
  {"name": "danger", "value": {"default": "#ff8f8f", "root-light": "#b42318"}, "usage": "Error text and destructive control border."},
  {"name": "tint-hover", "value": {"default": "rgba(45,91,255,.12)", "root-light": "rgba(45,91,255,.08)"}, "usage": "Hover fill, highlighted row."},
  {"name": "tint-active", "value": {"default": "rgba(45,91,255,.22)", "root-light": "rgba(45,91,255,.14)"}, "usage": "Active chip, pressed token."},
  {"name": "tint-danger", "value": {"default": "rgba(248,113,113,.14)", "root-light": "rgba(185,28,28,.08)"}, "usage": "Danger button hover fill."},
  {"name": "backdrop", "value": {"default": "rgba(5,8,15,.62)", "root-light": "rgba(14,20,34,.45)"}, "usage": "Dialog backdrop."}
]
```

## 3. Components

All in `wptk-ext.css`. Proposed README wording is below each.

**Button** (`.btn`, `--primary`, `--ghost`, `--danger`, `--sm`). Pill (`radius: 999px`, like nav links), `line-control` border, `surface-2` fill, 15px/600. Hover: `focus-ring` border and `tint-hover`. Primary is Cobalt fill with white text. 40px high, 32px for `--sm`, 44px on coarse pointers.
> Button. Pill-shaped action. Use `primary` once per view, the default for everything else, `ghost` for low-emphasis actions and `danger` for removal. Keep the label a verb.

**Field and input** (`.field`, `.field__label`, `.field__hint`, `.field__error`, `.input`). 16px text (stops iOS zoom), `radius-sm`, `line-control` border, `surface-2` fill. `aria-invalid="true"` thickens the border and turns it `danger`; the error text is bold, so error is not hue alone.
> Input. Always paired with a visible label. Hints sit under the label, errors replace the hint and are linked with `aria-describedby`.

**Chip** (`.chip`, `.is-active`). Pill toggle for filters. Active state is a filled dot, bold weight and a Cobalt border, not colour alone. Use with `aria-pressed`.

**Status** (`.status` plus `--airborne`, `--ground`, `--scheduled`, `--landed`, `--delayed`, `--cancelled`, `--unverified`). A pill with a text label and a leading marker whose shape differs by state: filled circle, filled square, ring, hollow square, triangle, diamond, dashed ring. The label is always `text-0`, so the hue only decorates.
> Status. Say the state in words. The marker shape repeats it. Never ship a status that is only a coloured dot.

**Dialog** (`dialog.dialog`, `.dialog__head`, `__title`, `__body`, `__foot`). Native `<dialog>` with `showModal()`, `bg-1` fill, `radius-lg`, `shadow` (the elevation the DS reserves for layers above a panel).
> Dialog. Use the native element so focus trapping and Escape come free. Title it with `aria-labelledby`.

**Token button** (app.css, not proposed): airport codes and flight numbers as real buttons with `aria-pressed`.

**Utilities**: `.sr-only`, `.is-hidden`, and one focus rule: `outline: 2px solid var(--focus-ring); outline-offset: 2px` on every focusable element.

## 4. Contrast table (generated)

Regenerate with `python tools/contrast_check.py --markdown`. Rows with kind `info` are DS values used unchanged; they are listed in section 1 and are not required by WCAG.

| Theme | Kind | Pair | Worst ratio | Needs |
|---|---|---|---|---|
| Midnight | text | text-0 on page | 13.87:1 | 4.5:1 |
| Midnight | text | text-0 on surface | 15.46:1 | 4.5:1 |
| Midnight | text | text-0 on surface-2 | 15.68:1 | 4.5:1 |
| Midnight | text | text-0 on nav | 16.37:1 | 4.5:1 |
| Midnight | text | text-0 on dialog | 15.86:1 | 4.5:1 |
| Midnight | text | text-0 on hit row | 13.88:1 | 4.5:1 |
| Midnight | text | text-0 on active chip | 12.68:1 | 4.5:1 |
| Midnight | text | text-0 on button hover | 14.08:1 | 4.5:1 |
| Midnight | text | text-1 on page (secondary text, dimmed rows, placeholders) | 7.17:1 | 4.5:1 |
| Midnight | text | text-1 on surface (secondary text, dimmed rows, placeholders) | 7.99:1 | 4.5:1 |
| Midnight | text | text-1 on surface-2 (secondary text, dimmed rows, placeholders) | 8.10:1 | 4.5:1 |
| Midnight | text | text-1 on dialog (secondary text, dimmed rows, placeholders) | 8.20:1 | 4.5:1 |
| Midnight | text | text-1 on hit row (secondary text, dimmed rows, placeholders) | 7.17:1 | 4.5:1 |
| Midnight | text | text-1 on button hover (secondary text, dimmed rows, placeholders) | 7.28:1 | 4.5:1 |
| Midnight | text | on-accent on accent-0-strong (primary button) | 5.18:1 | 4.5:1 |
| Midnight | text | on-accent on accent-0-strong-hover | 6.24:1 | 4.5:1 |
| Midnight | text | danger on page (error text, danger button) | 6.85:1 | 4.5:1 |
| Midnight | text | danger on surface (error text, danger button) | 7.64:1 | 4.5:1 |
| Midnight | text | danger on surface-2 (error text, danger button) | 7.75:1 | 4.5:1 |
| Midnight | text | danger on dialog (error text, danger button) | 7.84:1 | 4.5:1 |
| Midnight | text | danger on danger hover (error text, danger button) | 6.39:1 | 4.5:1 |
| Midnight | ui | line-control on page (button, input, chip, toggle borders) | 3.71:1 | 3.0:1 |
| Midnight | ui | line-control on surface (button, input, chip, toggle borders) | 4.13:1 | 3.0:1 |
| Midnight | ui | line-control on dialog (button, input, chip, toggle borders) | 4.24:1 | 3.0:1 |
| Midnight | ui | line-control on nav (button, input, chip, toggle borders) | 4.37:1 | 3.0:1 |
| Midnight | ui | line-control vs input fill (surface-2 on surface) | 4.19:1 | 3.0:1 |
| Midnight | ui | focus-ring on page (focus ring, hover and active borders) | 3.65:1 | 3.0:1 |
| Midnight | ui | focus-ring on surface (focus ring, hover and active borders) | 4.06:1 | 3.0:1 |
| Midnight | ui | focus-ring on dialog (focus ring, hover and active borders) | 4.17:1 | 3.0:1 |
| Midnight | ui | focus-ring on surface-2 (focus ring, hover and active borders) | 4.12:1 | 3.0:1 |
| Midnight | ui | focus-ring on nav (focus ring, hover and active borders) | 4.31:1 | 3.0:1 |
| Midnight | ui | focus-ring on hit row (focus ring, hover and active borders) | 3.65:1 | 3.0:1 |
| Midnight | ui | focus-ring on active chip (focus ring, hover and active borders) | 3.33:1 | 3.0:1 |
| Midnight | ui | danger on surface-2 (error border, danger button border) | 7.75:1 | 3.0:1 |
| Midnight | info | DS line on page (card and nav borders, decorative) | 2.65:1 | 3.0:1 |
| Midnight | info | DS line on surface (card and nav borders, decorative) | 2.95:1 | 3.0:1 |
| Midnight | info | DS line on nav (card and nav borders, decorative) | 3.13:1 | 3.0:1 |
| Midnight | info | DS accent-0-text on page (link hover) | 3.65:1 | 4.5:1 |
| Midnight | info | DS accent-0-text on surface (link hover) | 4.06:1 | 4.5:1 |
| Midnight | info | DS accent-0-text on nav (link hover) | 4.31:1 | 4.5:1 |
| Midnight | ui | st-airborne marker and border on chip fill (surface-2) | 8.84:1 | 3.0:1 |
| Midnight | ui | st-airborne border on surface | 8.71:1 | 3.0:1 |
| Midnight | ui | st-ground marker and border on chip fill (surface-2) | 10.18:1 | 3.0:1 |
| Midnight | ui | st-ground border on surface | 10.03:1 | 3.0:1 |
| Midnight | ui | st-scheduled marker and border on chip fill (surface-2) | 7.45:1 | 3.0:1 |
| Midnight | ui | st-scheduled border on surface | 7.35:1 | 3.0:1 |
| Midnight | ui | st-landed marker and border on chip fill (surface-2) | 8.10:1 | 3.0:1 |
| Midnight | ui | st-landed border on surface | 7.99:1 | 3.0:1 |
| Midnight | ui | st-delayed marker and border on chip fill (surface-2) | 7.51:1 | 3.0:1 |
| Midnight | ui | st-delayed border on surface | 7.40:1 | 3.0:1 |
| Midnight | ui | st-cancelled marker and border on chip fill (surface-2) | 6.14:1 | 3.0:1 |
| Midnight | ui | st-cancelled border on surface | 6.05:1 | 3.0:1 |
| Midnight | ui | st-unverified marker and border on chip fill (surface-2) | 6.97:1 | 3.0:1 |
| Midnight | ui | st-unverified border on surface | 6.87:1 | 3.0:1 |
| Midnight | ui | st-ground banner edge on surface-2 | 10.18:1 | 3.0:1 |
| Daylight | text | text-0 on page | 14.78:1 | 4.5:1 |
| Daylight | text | text-0 on surface | 17.44:1 | 4.5:1 |
| Daylight | text | text-0 on surface-2 | 18.01:1 | 4.5:1 |
| Daylight | text | text-0 on nav | 15.48:1 | 4.5:1 |
| Daylight | text | text-0 on dialog | 15.66:1 | 4.5:1 |
| Daylight | text | text-0 on hit row | 15.63:1 | 4.5:1 |
| Daylight | text | text-0 on active chip | 14.78:1 | 4.5:1 |
| Daylight | text | text-0 on button hover | 16.11:1 | 4.5:1 |
| Daylight | text | text-1 on page (secondary text, dimmed rows, placeholders) | 6.05:1 | 4.5:1 |
| Daylight | text | text-1 on surface (secondary text, dimmed rows, placeholders) | 7.14:1 | 4.5:1 |
| Daylight | text | text-1 on surface-2 (secondary text, dimmed rows, placeholders) | 7.37:1 | 4.5:1 |
| Daylight | text | text-1 on dialog (secondary text, dimmed rows, placeholders) | 6.41:1 | 4.5:1 |
| Daylight | text | text-1 on hit row (secondary text, dimmed rows, placeholders) | 6.39:1 | 4.5:1 |
| Daylight | text | text-1 on button hover (secondary text, dimmed rows, placeholders) | 6.59:1 | 4.5:1 |
| Daylight | text | on-accent on accent-0-strong (primary button) | 5.18:1 | 4.5:1 |
| Daylight | text | on-accent on accent-0-strong-hover | 6.86:1 | 4.5:1 |
| Daylight | text | danger on page (error text, danger button) | 5.28:1 | 4.5:1 |
| Daylight | text | danger on surface (error text, danger button) | 6.23:1 | 4.5:1 |
| Daylight | text | danger on surface-2 (error text, danger button) | 6.44:1 | 4.5:1 |
| Daylight | text | danger on dialog (error text, danger button) | 5.60:1 | 4.5:1 |
| Daylight | text | danger on danger hover (error text, danger button) | 5.63:1 | 4.5:1 |
| Daylight | ui | line-control on page (button, input, chip, toggle borders) | 3.49:1 | 3.0:1 |
| Daylight | ui | line-control on surface (button, input, chip, toggle borders) | 4.12:1 | 3.0:1 |
| Daylight | ui | line-control on dialog (button, input, chip, toggle borders) | 3.70:1 | 3.0:1 |
| Daylight | ui | line-control on nav (button, input, chip, toggle borders) | 3.66:1 | 3.0:1 |
| Daylight | ui | line-control vs input fill (surface-2 on surface) | 4.26:1 | 3.0:1 |
| Daylight | ui | focus-ring on page (focus ring, hover and active borders) | 4.16:1 | 3.0:1 |
| Daylight | ui | focus-ring on surface (focus ring, hover and active borders) | 4.91:1 | 3.0:1 |
| Daylight | ui | focus-ring on dialog (focus ring, hover and active borders) | 4.41:1 | 3.0:1 |
| Daylight | ui | focus-ring on surface-2 (focus ring, hover and active borders) | 5.07:1 | 3.0:1 |
| Daylight | ui | focus-ring on nav (focus ring, hover and active borders) | 4.36:1 | 3.0:1 |
| Daylight | ui | focus-ring on hit row (focus ring, hover and active borders) | 4.40:1 | 3.0:1 |
| Daylight | ui | focus-ring on active chip (focus ring, hover and active borders) | 4.17:1 | 3.0:1 |
| Daylight | ui | danger on surface-2 (error border, danger button border) | 6.44:1 | 3.0:1 |
| Daylight | info | DS line on page (card and nav borders, decorative) | 2.85:1 | 3.0:1 |
| Daylight | info | DS line on surface (card and nav borders, decorative) | 3.37:1 | 3.0:1 |
| Daylight | info | DS line on nav (card and nav borders, decorative) | 2.99:1 | 3.0:1 |
| Daylight | info | DS accent-0-text on page (link hover) | 4.16:1 | 4.5:1 |
| Daylight | info | DS accent-0-text on surface (link hover) | 4.91:1 | 4.5:1 |
| Daylight | info | DS accent-0-text on nav (link hover) | 4.36:1 | 4.5:1 |
| Daylight | ui | st-airborne marker and border on chip fill (surface-2) | 5.37:1 | 3.0:1 |
| Daylight | ui | st-airborne border on surface | 5.20:1 | 3.0:1 |
| Daylight | ui | st-ground marker and border on chip fill (surface-2) | 6.94:1 | 3.0:1 |
| Daylight | ui | st-ground border on surface | 6.72:1 | 3.0:1 |
| Daylight | ui | st-scheduled marker and border on chip fill (surface-2) | 5.07:1 | 3.0:1 |
| Daylight | ui | st-scheduled border on surface | 4.91:1 | 3.0:1 |
| Daylight | ui | st-landed marker and border on chip fill (surface-2) | 7.37:1 | 3.0:1 |
| Daylight | ui | st-landed border on surface | 7.14:1 | 3.0:1 |
| Daylight | ui | st-delayed marker and border on chip fill (surface-2) | 4.92:1 | 3.0:1 |
| Daylight | ui | st-delayed border on surface | 4.76:1 | 3.0:1 |
| Daylight | ui | st-cancelled marker and border on chip fill (surface-2) | 6.33:1 | 3.0:1 |
| Daylight | ui | st-cancelled border on surface | 6.13:1 | 3.0:1 |
| Daylight | ui | st-unverified marker and border on chip fill (surface-2) | 6.84:1 | 3.0:1 |
| Daylight | ui | st-unverified border on surface | 6.62:1 | 3.0:1 |
| Daylight | ui | st-ground banner edge on surface-2 | 6.94:1 | 3.0:1 |
