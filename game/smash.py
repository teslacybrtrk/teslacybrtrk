#!/usr/bin/env python3
"""Cybertruck window smash: a community game played through GitHub issues.

Usage:
  smash.py play     Apply the throw described by ISSUE_TITLE / ISSUE_AUTHOR /
                    ISSUE_NUMBER, then re-render README.md and the SVG.
  smash.py render   Re-render README.md and the SVG from state.json only.

When run by the workflow, the reply for the issue is written to $COMMENT_FILE,
the commit message to $COMMIT_MSG_FILE, and `valid=true|false` to $GITHUB_OUTPUT.
"""

import json
import math
import os
import random
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "game" / "state.json"
SVG_FILE = ROOT / "game" / "cybertruck.svg"
README_FILE = ROOT / "README.md"

REPO = os.environ.get("GITHUB_REPOSITORY", "teslacybrtrk/teslacybrtrk")
WINDOWS = 4
HITS_TO_SHATTER = 3
TITLE_RE = re.compile(r"^\s*cybertruck\|smash\|(\d+)\s*$", re.IGNORECASE)

START_MARK = "<!-- SMASH:START -->"
END_MARK = "<!-- SMASH:END -->"

# Geometry of the side profile (viewBox 0 0 800 320).
ROOF = [(30, 200), (330, 70), (770, 150)]
BELT_Y = 178
GLASS_INSET = 14
PANELS = [(140, 250), (258, 365), (373, 480), (488, 590)]

STATUS = {0: "🪟 Intact", 1: "💥 Cracked", 2: "🕸️ Shattering", 3: "🕳️ Gone"}

CRACK_LINES = [
    "💥 **CRACK!** Window {w} took a hit. Well... it didn't go through. Yet.",
    "💥 **Bonk.** The steel ball left a lovely spiderweb on window {w}.",
    "💥 **Oh my...** window {w} is cracked. Armor glass, they said.",
]
SHATTER_LINES = [
    "🕳️ **SHATTERED!** Window {w} is gone. It definitely went through this time.",
    "🕳️ **KSSSH!** Window {w} has left the chat.",
]


# ---------------------------------------------------------------- state

def new_state():
    return {
        "round": 1,
        "windows": [0] * WINDOWS,
        "total_throws": 0,
        "throwers": {},
        "recent": [],
        "finishers": [],
        "last_hit": None,
    }


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return new_state()


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- game

def play(state, title, author, issue):
    """Apply one throw. Returns (valid, comment, commit_message)."""
    match = TITLE_RE.match(title or "")
    if not match or not 1 <= int(match.group(1)) <= WINDOWS:
        return False, (
            f"🤔 I couldn't read that throw. Use one of the **🏐 Throw** links in the "
            f"[README](https://github.com/{REPO}) to pick a window from 1 to {WINDOWS}."
        ), None

    w = int(match.group(1))
    if state["windows"][w - 1] >= HITS_TO_SHATTER:
        return False, (
            f"🫥 The ball sailed straight through: window {w} is already gone. "
            f"Pick another one in the [README](https://github.com/{REPO})!"
        ), None

    state["windows"][w - 1] += 1
    level = state["windows"][w - 1]
    state["total_throws"] += 1
    state["throwers"][author] = state["throwers"].get(author, 0) + 1
    state["last_hit"] = {"round": state["round"], "window": w, "level": level}

    rng = random.Random(f"{issue}-{w}")
    if level >= HITS_TO_SHATTER:
        text = rng.choice(SHATTER_LINES).format(w=w)
        verb = "shattered"
    else:
        text = rng.choice(CRACK_LINES).format(w=w)
        verb = "cracked"

    state["recent"].insert(0, {
        "user": author,
        "window": w,
        "verb": verb,
        "issue": issue,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    del state["recent"][5:]

    lines = [f"@{author} {text}", ""]
    if all(v >= HITS_TO_SHATTER for v in state["windows"]):
        state["finishers"].insert(0, {
            "round": state["round"],
            "user": author,
            "throws": state["total_throws"],
        })
        del state["finishers"][5:]
        lines.append(
            f"🏆 **That was the finishing blow!** Every window in round {state['round']} "
            f"is destroyed and you're in the Hall of Finishers. A fresh Cybertruck "
            f"has rolled in for round {state['round'] + 1}."
        )
        state["round"] += 1
        state["windows"] = [0] * WINDOWS
        state["last_hit"] = None
    else:
        left = sum(HITS_TO_SHATTER - v for v in state["windows"])
        lines.append(f"Round {state['round']}: {left} more hits until the truck is glassless.")
    lines += ["", f"You've thrown **{state['throwers'][author]}** ball(s) so far. "
              f"The [README](https://github.com/{REPO}) updates in a few seconds."]

    commit = f"🏐 @{author} {verb} window {w} (#{issue})"
    return True, "\n".join(lines), commit


# ---------------------------------------------------------------- svg

def roof_y(x):
    (x0, y0), (x1, y1), (x2, y2) = ROOF
    if x <= x1:
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


def panel_points(a, b):
    pts = [(a, roof_y(a) + GLASS_INSET)]
    if a < ROOF[1][0] < b:
        pts.append((ROOF[1][0], ROOF[1][1] + GLASS_INSET))
    pts += [(b, roof_y(b) + GLASS_INSET), (b, BELT_Y), (a, BELT_Y)]
    return pts


def fmt(pts):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def impact_point(rng, a, b):
    x = rng.uniform(a + (b - a) * 0.3, b - (b - a) * 0.3)
    top = roof_y(x) + GLASS_INSET
    y = rng.uniform(top + (BELT_Y - top) * 0.35, top + (BELT_Y - top) * 0.7)
    return x, y


def damage_svg(i, level, round_no):
    """Cracks / hole for one window, clipped to its glass panel."""
    a, b = PANELS[i]
    rng = random.Random(f"r{round_no}-w{i}")
    cx, cy = impact_point(rng, a, b)
    out = []

    if level >= 3:
        hole = []
        for k in range(18):
            ang = 2 * math.pi * k / 18
            r = rng.uniform(35, 80) if k % 2 else rng.uniform(15, 40)
            hole.append((cx + r * math.cos(ang), cy + r * 0.8 * math.sin(ang)))
        out.append(f'<polygon points="{fmt(hole)}" fill="#07090c"/>')

    spokes = {1: 7, 2: 12, 3: 14}[min(level, 3)]
    for k in range(spokes):
        ang = 2 * math.pi * k / spokes + rng.uniform(-0.25, 0.25)
        length = rng.uniform(40, 110)
        pts, x, y = [(cx, cy)], cx, cy
        for _ in range(3):
            x += math.cos(ang) * length / 3 + rng.uniform(-4, 4)
            y += math.sin(ang) * length / 3 + rng.uniform(-4, 4)
            pts.append((x, y))
        out.append(f'<polyline points="{fmt(pts)}" fill="none" stroke="#e8f1f8" '
                   f'stroke-opacity="0.85" stroke-width="1.2"/>')

    for ring in range(min(level, 3)):
        r = 9 + ring * 13
        pts = []
        for k in range(spokes + 1):
            ang = 2 * math.pi * k / spokes
            rr = r * rng.uniform(0.8, 1.2)
            pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
        out.append(f'<polyline points="{fmt(pts)}" fill="none" stroke="#e8f1f8" '
                   f'stroke-opacity="0.7" stroke-width="1"/>')

    if level == 2:
        out.insert(0, f'<polygon points="{fmt(panel_points(a, b))}" fill="#cfe3f2" fill-opacity="0.12"/>')
    return out, (cx, cy)


def render_svg(state):
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 320" width="800" height="320" '
        'role="img" aria-label="Cybertruck side profile with smashable windows">',
        "<defs>",
        '<linearGradient id="steel" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#e3e6e9"/><stop offset="0.55" stop-color="#b9bec4"/>'
        '<stop offset="1" stop-color="#8d939a"/></linearGradient>',
        '<linearGradient id="glass" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#3b4a57"/><stop offset="1" stop-color="#11171d"/></linearGradient>',
        '<radialGradient id="ball" cx="0.35" cy="0.35" r="0.7">'
        '<stop offset="0" stop-color="#ffffff"/><stop offset="0.4" stop-color="#b8bdc2"/>'
        '<stop offset="1" stop-color="#4a4f55"/></radialGradient>',
    ]
    for i, (a, b) in enumerate(PANELS):
        parts.append(f'<clipPath id="clip{i}"><polygon points="{fmt(panel_points(a, b))}"/></clipPath>')
    parts.append("</defs>")

    # Shadow, body, details.
    parts.append('<ellipse cx="400" cy="296" rx="390" ry="9" fill="#000" fill-opacity="0.22"/>')
    body = [(30, 200), (330, 70), (770, 150), (776, 162), (776, 236), (768, 250),
            (706, 250), (690, 198), (590, 198), (574, 250), (236, 250), (220, 198),
            (120, 198), (104, 250), (36, 250), (26, 232)]
    parts.append(f'<polygon points="{fmt(body)}" fill="url(#steel)" stroke="#6f757c" stroke-width="1.5"/>')
    parts.append('<line x1="82" y1="178" x2="776" y2="178" stroke="#80868d" stroke-width="1"/>')
    for x in (254, 369, 484, 598):
        parts.append(f'<line x1="{x}" y1="{roof_y(x) + 6:.1f}" x2="{x}" y2="246" stroke="#80868d" stroke-width="1"/>')
    parts.append('<line x1="29" y1="201" x2="52" y2="191" stroke="#fdfdfd" stroke-width="4" stroke-linecap="round"/>')
    parts.append('<line x1="773" y1="156" x2="773" y2="176" stroke="#d63a3a" stroke-width="4" stroke-linecap="round"/>')

    # Wheels with aero covers.
    for cx in (170, 640):
        parts.append(f'<circle cx="{cx}" cy="250" r="44" fill="#16191c" stroke="#4a5058" stroke-width="2"/>')
        parts.append(f'<circle cx="{cx}" cy="250" r="30" fill="#3a3f45" stroke="#565c63" stroke-width="2"/>')
        for k in range(6):
            ang = math.pi * k / 3
            parts.append(f'<line x1="{cx}" y1="250" x2="{cx + 28 * math.cos(ang):.1f}" '
                         f'y2="{250 + 28 * math.sin(ang):.1f}" stroke="#4d535a" stroke-width="3"/>')
        parts.append(f'<circle cx="{cx}" cy="250" r="6" fill="#565c63"/>')

    # Windows.
    ball_at = None
    last = state.get("last_hit")
    for i, (a, b) in enumerate(PANELS):
        level = state["windows"][i]
        pts = fmt(panel_points(a, b))
        parts.append(f'<polygon points="{pts}" fill="url(#glass)"/>')
        if level == 0:
            mx = a + (b - a) * 0.3
            parts.append(f'<polygon clip-path="url(#clip{i})" points="{mx:.1f},60 {mx + 18:.1f},60 '
                         f'{mx - 22:.1f},190 {mx - 40:.1f},190" fill="#ffffff" fill-opacity="0.08"/>')
        else:
            dmg, point = damage_svg(i, level, state["round"])
            parts.append(f'<g clip-path="url(#clip{i})">{"".join(dmg)}</g>')
            if last and last["window"] == i + 1:
                ball_at = point
        parts.append(f'<text x="{(a + b) / 2:.1f}" y="206" text-anchor="middle" '
                     f'font-family="Helvetica, Arial, sans-serif" font-size="15" font-weight="700" '
                     f'fill="#5d636a">{i + 1}</text>')

    if ball_at:
        parts.append(f'<circle cx="{ball_at[0] + 3:.1f}" cy="{ball_at[1] + 3:.1f}" r="8" fill="#000" fill-opacity="0.3"/>')
        parts.append(f'<circle cx="{ball_at[0]:.1f}" cy="{ball_at[1]:.1f}" r="8" fill="url(#ball)"/>')

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- readme

def throw_link(w):
    query = urllib.parse.urlencode({
        "title": f"cybertruck|smash|{w}",
        "body": "Just click **Create** (or **Submit new issue**) to throw the ball. "
                "A GitHub Action will reply in under a minute.",
    })
    return f"https://github.com/{REPO}/issues/new?{query}"


def render_section(state):
    hits = sum(state["windows"])
    total = WINDOWS * HITS_TO_SHATTER
    status = " | ".join(STATUS[min(v, 3)] for v in state["windows"])
    throws = " | ".join(
        "—" if v >= HITS_TO_SHATTER else f"[🏐 Throw]({throw_link(i + 1)})"
        for i, v in enumerate(state["windows"])
    )

    out = [
        START_MARK,
        "## 🛻 Smash the Cybertruck",
        "",
        "> *\"Well... it didn't go through.\"* Pick a window and throw a steel ball at it. "
        "Every window takes three hits to destroy. Take out all four and you land the finishing blow.",
        "",
        '<p align="center">',
        f'  <img src="game/cybertruck.svg?v={state["round"]}-{state["total_throws"]}" '
        'width="720" alt="Cybertruck with smashable windows">',
        "</p>",
        "",
        "| Window | " + " | ".join(str(i + 1) for i in range(WINDOWS)) + " |",
        "|:--|" + ":-:|" * WINDOWS,
        f"| **Status** | {status} |",
        f"| **Throw** | {throws} |",
        "",
        f"**Round {state['round']}** · {hits}/{total} hits · {state['total_throws']} balls thrown all-time",
        "",
    ]

    if state["recent"]:
        out += ["### 🕒 Recent throws", ""]
        for r in state["recent"]:
            out.append(f"- [@{r['user']}](https://github.com/{r['user']}) {r['verb']} window "
                       f"{r['window']} · [#{r['issue']}](https://github.com/{REPO}/issues/{r['issue']}) · {r['date']}")
        out.append("")

    if state["throwers"]:
        out += ["### 🏆 Top throwers", "", "| | Player | Balls |", "|:-:|:--|--:|"]
        top = sorted(state["throwers"].items(), key=lambda kv: (-kv[1], kv[0].lower()))[:10]
        medals = ["🥇", "🥈", "🥉"]
        for n, (user, count) in enumerate(top):
            out.append(f"| {medals[n] if n < 3 else n + 1} | [@{user}](https://github.com/{user}) | {count} |")
        out.append("")

    if state["finishers"]:
        out += ["### 🪦 Hall of Finishers", ""]
        for f in state["finishers"]:
            out.append(f"- Round {f['round']}: finished off by [@{f['user']}](https://github.com/{f['user']})")
        out.append("")

    out += [
        "<details>",
        "<summary>How does this work?</summary>",
        "",
        "Each **🏐 Throw** link opens a pre-filled issue. When you submit it, a "
        "[GitHub Action](.github/workflows/smash.yml) runs [`game/smash.py`](game/smash.py), "
        "updates [`game/state.json`](game/state.json), redraws the truck, commits the new README "
        "and closes your issue with a reply. There's no server. The whole game lives in this repo.",
        "",
        "</details>",
        END_MARK,
    ]
    return "\n".join(out)


def render_readme(state):
    section = render_section(state)
    text = README_FILE.read_text() if README_FILE.exists() else ""
    if START_MARK in text and END_MARK in text:
        head, rest = text.split(START_MARK, 1)
        _, tail = rest.split(END_MARK, 1)
        text = head + section + tail
    else:
        text = text.rstrip() + "\n\n" + section + "\n"
    README_FILE.write_text(text)


def render(state):
    SVG_FILE.write_text(render_svg(state))
    render_readme(state)


# ---------------------------------------------------------------- main

def write_env_file(var, content):
    path = os.environ.get(var)
    if path:
        Path(path).write_text(content)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "render"
    state = load_state()
    if cmd == "render":
        save_state(state)
        render(state)
        return
    if cmd != "play":
        sys.exit(f"unknown command: {cmd}")

    valid, comment, commit = play(
        state,
        os.environ.get("ISSUE_TITLE", ""),
        os.environ.get("ISSUE_AUTHOR", "someone"),
        int(os.environ.get("ISSUE_NUMBER", "0")),
    )
    write_env_file("COMMENT_FILE", comment + "\n")
    if valid:
        save_state(state)
        render(state)
        write_env_file("COMMIT_MSG_FILE", commit + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as fh:
            fh.write(f"valid={'true' if valid else 'false'}\n")


if __name__ == "__main__":
    main()
