#!/usr/bin/env python3
"""
Post Sleeper fantasy basketball league updates to separate Discord channels.

Usage:
    python sleeper_discord.py transactions   # new trades / waivers / FA moves
    python sleeper_discord.py matchups [wk]  # matchup scores (default: current week)
    python sleeper_discord.py standings      # league standings

Required environment variables:
    SLEEPER_LEAGUE_ID
    WEBHOOK_TRANSACTIONS
    WEBHOOK_MATCHUPS
    WEBHOOK_STANDINGS

Install dependency:  pip install requests
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = "https://api.sleeper.app/v1"
SPORT = "nba"
LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID", "")
WEBHOOKS = {
    "transactions": os.environ.get("WEBHOOK_TRANSACTIONS"),
    "matchups": os.environ.get("WEBHOOK_MATCHUPS"),
    "standings": os.environ.get("WEBHOOK_STANDINGS"),
}
STATE_FILE = Path(__file__).with_name("state.json")
PLAYERS_FILE = Path(__file__).with_name("players_cache.json")


def get(path):
    r = requests.get(f"{BASE}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def post(channel, text):
    url = WEBHOOKS.get(channel)
    if not url:
        sys.exit(f"Missing webhook for '{channel}'. Set WEBHOOK_{channel.upper()}.")
    # Discord caps messages at 2000 characters, so split on line breaks.
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 1900:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    for chunk in chunks:
        requests.post(url, json={"content": chunk}, timeout=30).raise_for_status()
        time.sleep(1)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_transactions": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def player_names():
    """Sleeper asks apps to fetch the big player list at most about once a day."""
    if not PLAYERS_FILE.exists() or time.time() - PLAYERS_FILE.stat().st_mtime > 86400:
        PLAYERS_FILE.write_text(json.dumps(get(f"/players/{SPORT}")))
    players = json.loads(PLAYERS_FILE.read_text())

    def name(pid):
        p = players.get(str(pid), {})
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        return full or str(pid)

    return name


def team_names():
    users = {u["user_id"]: u for u in get(f"/league/{LEAGUE_ID}/users")}
    names = {}
    for r in get(f"/league/{LEAGUE_ID}/rosters"):
        u = users.get(r.get("owner_id"), {})
        names[r["roster_id"]] = (
            u.get("metadata", {}).get("team_name")
            or u.get("display_name")
            or f"Team {r['roster_id']}"
        )
    return names


def current_week():
    return get(f"/state/{SPORT}")["week"]


# ---------- transactions ----------
def describe_transaction(tx, teams, pname):
    adds = tx.get("adds") or {}
    drops = tx.get("drops") or {}
    picks = tx.get("draft_picks") or []

    if tx["type"] == "trade":
        lines = ["**TRADE ALERT**"]
        for rid in tx["roster_ids"]:
            got = [pname(p) for p, r in adds.items() if r == rid]
            got += [f"{d['season']} round {d['round']} pick" for d in picks if d["owner_id"] == rid]
            lines.append(f"- {teams.get(rid, rid)} receives: {', '.join(got) or 'nothing'}")
        return "\n".join(lines)

    rid = tx["roster_ids"][0]
    label = "Waiver claim" if tx["type"] == "waiver" else "Free agent move"
    parts = []
    if adds:
        parts.append("added " + ", ".join(pname(p) for p in adds))
    if drops:
        parts.append("dropped " + ", ".join(pname(p) for p in drops))
    bid = (tx.get("settings") or {}).get("waiver_bid")
    suffix = f" (bid: ${bid})" if bid else ""
    return f"**{label}:** {teams.get(rid, rid)} " + " and ".join(parts) + suffix


def cmd_transactions():
    first_run = not STATE_FILE.exists()
    state = load_state()
    seen = state["seen_transactions"]
    txs = get(f"/league/{LEAGUE_ID}/transactions/{current_week()}")
    txs = sorted(txs, key=lambda t: t.get("created", 0))
    new = [
        t for t in txs
        if t.get("status") == "complete" and t["transaction_id"] not in seen
    ]
    # The big player list is only downloaded when there is something to post.
    # On the very first run, existing moves are recorded but not posted, so the
    # channel isn't flooded with old activity.
    if new and not first_run:
        teams, pname = team_names(), player_names()
        for tx in new:
            post("transactions", describe_transaction(tx, teams, pname))
    seen.extend(t["transaction_id"] for t in new)
    state["seen_transactions"] = seen[-500:]
    save_state(state)


# ---------- matchups ----------
def cmd_matchups(week=None):
    week = week or current_week()
    teams = team_names()
    groups = {}
    for m in get(f"/league/{LEAGUE_ID}/matchups/{week}"):
        if m.get("matchup_id") is not None:
            groups.setdefault(m["matchup_id"], []).append(m)
    lines = [f"**Week {week} matchups**"]
    for pair in groups.values():
        if len(pair) == 2:
            a, b = pair
            lines.append(
                f"- {teams[a['roster_id']]} **{a['points']:.1f}** vs "
                f"{teams[b['roster_id']]} **{b['points']:.1f}**"
            )
    post("matchups", "\n".join(lines))


# ---------- standings ----------
def cmd_standings():
    teams = team_names()
    rows = []
    for r in get(f"/league/{LEAGUE_ID}/rosters"):
        s = r["settings"]
        pts = s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100
        rows.append((s.get("wins", 0), pts, s.get("losses", 0), s.get("ties", 0), teams[r["roster_id"]]))
    rows.sort(key=lambda x: (-x[0], -x[1]))
    lines = ["**League standings**", "```"]
    for i, (w, pts, l, t, name) in enumerate(rows, 1):
        lines.append(f"{i:>2}. {name[:22]:<22} {w}-{l}-{t}  {pts:,.1f} pts")
    lines.append("```")
    post("standings", "\n".join(lines))


if __name__ == "__main__":
    if not LEAGUE_ID:
        sys.exit("Set SLEEPER_LEAGUE_ID first.")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "transactions":
        cmd_transactions()
    elif cmd == "matchups":
        cmd_matchups(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "standings":
        cmd_standings()
    else:
        sys.exit(__doc__)
