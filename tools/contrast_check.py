#!/usr/bin/env python3
"""WCAG contrast check for the colour pairs the shell defines, in both themes.

Reads the real CSS custom properties from app/static/wptk/tokens.css and
app/static/css/wptk-ext.css (no copies of the values live here), composites
translucent fills over every ground the page can show, and takes the worst
case. Text must reach 4.5:1; control borders, focus rings and status markers
must reach 3:1. Exits 1 if any pair falls short.

    python tools/contrast_check.py            # table, exit code
    python tools/contrast_check.py --markdown # table in markdown (for docs/DS_PROPOSAL.md)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "app/static/wptk/tokens.css", ROOT / "app/static/css/wptk-ext.css"]
TEXT_MIN, UI_MIN = 4.5, 3.0

# Largest alpha of the three body gradient blobs (wptk.css): orchid, teal, cobalt.
BLOBS = {"dark": [("accent-1", 0.18), ("accent-2", 0.16), ("accent-0", 0.14)],
         "light": [("accent-1", 0.12), ("accent-2", 0.10), ("accent-0", 0.10)]}


# ---------- parsing ----------
def parse_vars() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {"dark": {}, "light": {}}
    for f in FILES:
        css = re.sub(r"/\*.*?\*/", "", f.read_text(), flags=re.S)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            sel = sel.strip()
            if not sel.startswith(":root"):
                continue
            targets = ["light"] if "light" in sel else (["dark", "light"] if sel == ":root" else ["dark"])
            for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body):
                for t in targets:
                    out[t][name[2:]] = val.strip()
    return {k: resolve(v) for k, v in out.items()}


def resolve(vars_: dict[str, str]) -> dict[str, str]:
    """Expand var(--x) references between custom properties."""
    out = dict(vars_)
    for _ in range(5):
        for k, val in out.items():
            m = re.fullmatch(r"var\(--([\w-]+)\)", val)
            if m and m.group(1) in out:
                out[k] = out[m.group(1)]
    return out


def color(s: str) -> tuple[float, float, float, float]:
    s = s.strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.match(r"rgba?\(([^)]+)\)", s)
    if m:
        p = [x.strip() for x in m.group(1).split(",")]
        return (float(p[0]), float(p[1]), float(p[2]), float(p[3]) if len(p) > 3 else 1.0)
    raise ValueError(f"unsupported colour {s!r}")


def over(fg, bg):
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def lum(c) -> float:
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def ratio(a, b) -> float:
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------- grounds ----------
def grounds(v: dict[str, str], theme: str) -> dict[str, list]:
    c = lambda n: color(v[n])  # noqa: E731
    page = [c("bg-0"), c("bg-1")]
    for name, alpha in BLOBS[theme]:
        b = c(name)
        page.append(over((b[0], b[1], b[2], alpha), c("bg-0")))
    surface = [over(c("surface"), g) for g in page]
    surface2 = [over(c("surface-2"), g) for g in surface]            # chips, inputs, card-level fills on a panel
    nav = [over(c("surface-3"), g) for g in page]
    dialog = [c("bg-1")]
    hit = [over(c("tint-hover"), g) for g in surface]                # highlighted leg row
    active = [over(c("tint-active"), g) for g in surface2]           # active chip, pressed token
    hover2 = [over(c("tint-hover"), g) for g in surface2]            # button hover
    dangerhover = [over(c("tint-danger"), g) for g in surface2]
    return {"page": page, "surface": surface, "surface-2": surface2, "nav": nav, "dialog": dialog,
            "hit row": hit, "active chip": active, "button hover": hover2, "danger hover": dangerhover}


def checks(v: dict[str, str], theme: str):
    G = grounds(v, theme)
    c = lambda n: color(v[n])  # noqa: E731
    solid = lambda n: [c(n)]  # noqa: E731
    T, U = TEXT_MIN, UI_MIN
    rows = []   # (kind, label, fg list, ground list, min)

    def add(kind, label, fg, ground, need):
        rows.append((kind, label, fg, ground, need))

    for g in ("page", "surface", "surface-2", "nav", "dialog", "hit row", "active chip", "button hover"):
        add("text", f"text-0 on {g}", solid("text-0"), G[g], T)
    for g in ("page", "surface", "surface-2", "dialog", "hit row", "button hover"):
        add("text", f"text-1 on {g} (secondary text, dimmed rows, placeholders)", solid("text-1"), G[g], T)
    add("text", "on-accent on accent-0-strong (primary button)", solid("on-accent"), solid("accent-0-strong"), T)
    add("text", "on-accent on accent-0-strong-hover", solid("on-accent"), solid("accent-0-strong-hover"), T)
    for g in ("page", "surface", "surface-2", "dialog", "danger hover"):
        add("text", f"danger on {g} (error text, danger button)", solid("danger"), G[g], T)

    # UI components: borders, rings, markers (3:1 against the ground they sit on)
    for g in ("page", "surface", "dialog", "nav"):
        add("ui", f"line-control on {g} (button, input, chip, toggle borders)", solid("line-control"), G[g], U)
    add("ui", "line-control vs input fill (surface-2 on surface)", solid("line-control"), G["surface-2"], U)
    for g in ("page", "surface", "dialog", "surface-2", "nav", "hit row", "active chip"):
        add("ui", f"focus-ring on {g} (focus ring, hover and active borders)", solid("focus-ring"), G[g], U)
    add("ui", "danger on surface-2 (error border, danger button border)", solid("danger"), G["surface-2"], U)
    # Informational: DS values used as-is. Not required by WCAG (container borders, DS hover text).
    for g in ("page", "surface", "nav"):
        add("info", f"DS line on {g} (card and nav borders, decorative)", solid("line"), G[g], U)
    for g in ("page", "surface", "nav"):
        add("info", f"DS accent-0-text on {g} (link hover)", solid("accent-0-text"), G[g], T)
    for s in ("airborne", "ground", "scheduled", "landed", "delayed", "cancelled", "unverified"):
        add("ui", f"st-{s} marker and border on chip fill (surface-2)", solid(f"st-{s}"), G["surface-2"], U)
        add("ui", f"st-{s} border on surface", solid(f"st-{s}"), G["surface"], U)
    add("ui", "st-ground banner edge on surface-2", solid("st-ground"), G["surface-2"], U)
    return rows


def worst(fg_list, ground_list):
    best = None
    for f in fg_list:
        for g in ground_list:
            r = ratio(over(f, g) if f[3] < 1 else f, g)
            best = r if best is None else min(best, r)
    return best


def main() -> int:
    md = "--markdown" in sys.argv
    vars_ = parse_vars()
    failed = 0
    if md:
        print("| Theme | Kind | Pair | Worst ratio | Needs |\n|---|---|---|---|---|")
    for theme in ("dark", "light"):
        for kind, label, fg, ground, need in checks(vars_[theme], theme):
            r = worst(fg, ground)
            ok = r >= need
            failed += 0 if (ok or kind == "info") else 1
            name = "Midnight" if theme == "dark" else "Daylight"
            if md:
                print(f"| {name} | {kind} | {label} | {r:.2f}:1 | {need}:1 |")
            else:
                print(f"{'ok  ' if ok else ('note' if kind == 'info' else 'FAIL')} {name:8} {kind:4} {r:6.2f}:1 (needs {need})  {label}")
    if failed:
        print(f"\n{failed} pair(s) below the floor", file=sys.stderr)
        return 1
    if not md:
        print("\nall pairs pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
