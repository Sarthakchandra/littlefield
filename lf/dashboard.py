"""Writes state/dashboard.json: the data the GitHub Pages dashboard reads."""
from __future__ import annotations

import json
import re
from pathlib import Path

MAX_HISTORY, MAX_ACTIONS = 3000, 600

HEAD_RE = re.compile(
    r"^(\S+) day ([\d.]+) cash \$([\d,]+) debt \$([\d,]+) machines \{1: (\d+), 2: (\d+), 3: (\d+)\}"
    r".*?contract (\d) wip (\d+)(.*)$")


def _kpi_fields(tail: str) -> dict:
    out = {}
    m = re.search(r"done (\d+)/d", tail)
    if m:
        out["done"] = int(m.group(1))
    m = re.search(r"c(\d) lt ([\d.]+)h rev \$([\d,]+)", tail)
    if m:
        out["lt_h"] = float(m.group(2))
        out["rev"] = float(m.group(3).replace(",", ""))
    m = re.search(r"accepted (\d+)/d", tail)
    if m:
        out["acc"] = int(m.group(1))
    m = re.search(r"queued (\d+) jobs", tail)
    if m:
        out["queued"] = int(m.group(1))
    m = re.search(r"util ([\d.]+)/([\d.]+)/([\d.]+)", tail)
    if m:
        out["util"] = [float(m.group(i)) for i in (1, 2, 3)]
    m = re.search(r"rank (\d+)/(\d+) \(leader \$([\d,]+)\)", tail)
    if m:
        out["rank"], out["teams"] = int(m.group(1)), int(m.group(2))
        out["leader"] = float(m.group(3).replace(",", ""))
    return out


def backfill(log_path: Path) -> dict:
    """Rebuild history and actions from the plain-text bot log."""
    hist, acts, cur = [], [], None
    if not log_path.exists():
        return {"history": hist, "actions": acts}
    for line in log_path.read_text().splitlines():
        m = HEAD_RE.match(line)
        if m:
            cur = dict(t=m.group(1), day=float(m.group(2)),
                       cash=float(m.group(3).replace(",", "")), debt=float(m.group(4).replace(",", "")),
                       m=[int(m.group(5)), int(m.group(6)), int(m.group(7))],
                       contract=int(m.group(8)), wip=int(m.group(9)), **_kpi_fields(m.group(10)))
            hist.append(cur)
            continue
        m = re.match(r"\s+(OK|FAIL)\s+(.*?)\s{2}\((.*?)\)(?:\s*\|\s*(.*))?$", line)
        if m and cur:
            res = (m.group(4) or "").replace("Processing request...", "").strip()
            acts.append(dict(t=cur["t"], day=cur["day"], ok=m.group(1) == "OK",
                             act=m.group(2).strip(), why=m.group(3), res=res[-160:]))
    return {"history": hist, "actions": acts}


PRICE = {1: 90_000, 2: 80_000, 3: 100_000}
RESALE = {1: 9_000, 2: 8_000, 3: 10_000}
LOAN_FEE = 0.05


def apply_actions(row: dict, acts: list, table: list | None, team: str) -> None:
    """The snapshot is read before the bot acts. Fold this run's successful changes
    into the row (and our leaderboard entry) so the dashboard is current now,
    not one run later."""
    net0 = row["cash"] - row["debt"]
    for a in acts:
        if not a.get("ok"):
            continue
        s = a["act"]
        m = re.match(r"(buy|sell) S(\d) -> (\d+)", s)
        if m:
            k, target = int(m.group(2)), int(m.group(3))
            i = k - 1
            if m.group(1) == "buy":
                n = max(0, target - row["m"][i])
                row["on_order"][i] += n
                row["cash"] -= n * PRICE[k]
            else:
                n = max(0, row["m"][i] - target)
                row["m"][i] -= n
                row["cash"] += n * RESALE[k]
            continue
        m = re.match(r"(loan|repay) -> (\d+)", s)
        if m:
            v = int(m.group(2))
            if m.group(1) == "loan":
                row["debt"] += v
                row["cash"] += v * (1 - LOAN_FEE)
            else:
                row["debt"] -= v
                row["cash"] -= v
            continue
        m = re.match(r"(contract|wip|rop|roq) -> (\d+)", s)
        if m:
            row[{"contract": "contract", "wip": "wip"}.get(m.group(1), m.group(1))] = int(m.group(2))
    shift = (row["cash"] - row["debt"]) - net0
    if not shift:
        return
    if row.get("our_score") is not None:
        row["our_score"] += shift
    if table:
        for r in table:
            if str(r["team"]).lower() == team.lower():
                r["cash"] += shift
        table.sort(key=lambda r: -r["cash"])
        for i, r in enumerate(table, 1):
            if str(r["team"]).lower() == team.lower() and row.get("rank"):
                row["rank"] = i


def update(path: Path, log_path: Path, team: str, snap, kp: dict, rank: dict | None,
           done_lines: list) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    if not data.get("history"):
        data.update(backfill(log_path))   # this run's lines aren't in the log yet
    row = dict(t=kp.get("t"), day=round(snap.day, 2), cash=snap.cash, debt=snap.debt,
               m=[snap.machines[k] for k in (1, 2, 3)], on_order=[snap.on_order.get(k, 0) for k in (1, 2, 3)],
               contract=snap.contract, wip=snap.wip_limit, inv=snap.inventory,
               **{k: v for k, v in kp.items() if k != "t"})
    if rank and rank.get("rank"):
        row.update(rank=rank["rank"], teams=rank["teams"], leader=rank["leader"], our_score=rank.get("cash"))
    table = [dict(r) for r in rank["table"]] if rank and rank.get("table") else None
    apply_actions(row, done_lines, table, team)
    if table:
        rank = dict(rank, table=table)
        row["leader"] = table[0]["cash"]
    hist = data.setdefault("history", [])
    if not hist or hist[-1].get("t") != row["t"]:
        hist.append(row)
    data["history"] = hist[-MAX_HISTORY:]
    data["actions"] = (data.get("actions", []) + done_lines)[-MAX_ACTIONS:]
    if rank and rank.get("table"):
        data["standings"] = rank["table"]
        sh = data.setdefault("standings_hist", {})
        last = data.get("standings_hist_day", -1)
        if snap.day - last >= 0.5:
            for r in rank["table"]:
                sh.setdefault(r["team"], []).append([round(snap.day, 2), r["cash"]])
            data["standings_hist_day"] = snap.day
    data.update(team=team, updated=kp.get("t"), day=round(snap.day, 2))
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")))
