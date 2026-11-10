"""One bot run: log in, read the factory, decide, act, log, log out.

Run by GitHub Actions on a schedule. Safe to run as often as you like:
every action is computed from the live state, so repeated runs converge.

Env:
  LF_ID, LF_PASSWORD     team login (repo secrets)
  LF_BASE                default https://op2.responsive.net
  LF_INSTITUTION         default iimb
  DRY_RUN=1              decide and log, change nothing
  PAUSE=1                skip the run entirely (kill switch, set as a repo variable)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lf.client import LittlefieldClient, GameError
from lf import notify, dashboard
from lf.policy import Observation, decide, PRICES, DEFAULTS, Forecaster, hours_per_job, GAME_END

ROOT = Path(__file__).parent
STATE = ROOT / "state" / "state.json"
LOG = ROOT / "logs" / "actions.log"


def load_cfg():
    p = ROOT / "config.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"runs": 0, "actions": [], "last_day": None}


def save_state(st):
    STATE.parent.mkdir(exist_ok=True)
    st["actions"] = st["actions"][-500:]
    STATE.write_text(json.dumps(st, indent=1))


def pending_from_log(day: float) -> dict:
    """Machines bought but not yet delivered (3-day lead), rebuilt from our own log.
    The live station page doesn't word in-transit machines the way the parser expects."""
    pend = {1: 0, 2: 0, 3: 0}
    if not LOG.exists():
        return pend
    cur = None
    for line in LOG.read_text().splitlines():
        m = re.search(r" day (\d+(?:\.\d+)?) cash", line)
        if m:
            cur = float(m.group(1))
            continue
        m = re.search(r"(\d+) machines? (?:was|were) purchased for station (\d)", line)
        if m and cur is not None and cur + 3.05 > day:
            pend[int(m.group(2))] += int(m.group(1))
    return pend


def to_obs(snap) -> Observation:
    series = {}
    for k in ("JOBIN", "JOBQ", "S1Q", "S2Q", "S3Q"):
        series[k] = {int(round(x)): y for x, y in snap.series.get(k, {}).items()}
    out = {}
    for k, v in snap.series.items():
        if k.startswith("JOBOUT"):
            for x, y in v.items():
                out[int(round(x))] = out.get(int(round(x)), 0) + y
    series["JOBOUT"] = out
    can_edit = all(k in snap.controls for k in ("contract", "wip"))
    led = pending_from_log(snap.day)
    snap.on_order = {k: max(snap.on_order.get(k, 0), led[k]) for k in (1, 2, 3)}
    return Observation(
        day=snap.day, cash=snap.cash, machines=snap.machines, on_order=snap.on_order,
        inventory=snap.inventory, kits_on_order=snap.kits_on_order, rop=snap.rop, roq=snap.roq,
        kit_unit=snap.kit_unit, kit_order_cost=snap.kit_order_cost, contract=snap.contract,
        lots_per_job=snap.lots_per_job, wip_limit=snap.wip_limit, rule=snap.rule, series=series,
        can_edit_orders=can_edit, debt=snap.debt)


def kpi_dict(snap) -> dict:
    def last(key):
        ser = snap.series.get(key) or {}
        return ser[max(ser)] if ser else None
    d = {"done": sum(v for k, v in ((k, last(k)) for k in snap.series if k.startswith("JOBOUT")) if v)}
    for c in (1, 2, 3):
        if last(f"JOBOUT_c{c}"):
            d["lt_h"], d["rev"] = round(last(f"JOBT_c{c}") * 24, 2), last(f"JOBREV_c{c}")
    d["queued"] = round(sum((last(f"S{k}Q") or 0) for k in (1, 2, 3)) / 60, 1)
    if last("JOBIN") is not None:
        d["acc"] = last("JOBIN")
    u = [last(f"S{k}UTIL") for k in (1, 2, 3)]
    if all(x is not None for x in u):
        d["util"] = [round(x, 3) for x in u]
    d["q"] = [round((last(f"S{k}Q") or 0) / 60, 1) for k in (1, 2, 3)]
    d["kits_on_order"] = snap.kits_on_order
    d["kits_due_in"] = snap.kits_due_in
    if snap.cash_detail:
        d["cs"] = snap.cash_detail
    return d


def model_kpis(obs, snap, cfg, acts_rec, rev_now=None) -> dict:
    """Plain capacity model for the dashboard: demand now, capacity per station, and
    the cash we add between now and day 300 if we buy nothing more."""
    pol = {**DEFAULTS, **(cfg.get("policy") or {})}
    f = Forecaster(obs, pol)
    lots = obs.lots_per_job
    hp = {k: hours_per_job(k, lots) for k in (1, 2, 3)}
    eff = {k: obs.machines[k] + obs.on_order.get(k, 0) for k in (1, 2, 3)}
    debt = obs.debt
    for a in acts_rec:
        if not a.get("ok"):
            continue
        m = re.match(r"buy S(\d) -> (\d+)", a["act"])
        if m:
            k = int(m.group(1))
            eff[k] += max(0, int(m.group(2)) - obs.machines[k])
        m = re.match(r"(loan|repay) -> (\d+)", a["act"])
        if m:
            debt += int(m.group(2)) * (1 if m.group(1) == "loan" else -1)
    u = 0.9
    cap_now = [round(obs.machines[k] * 24 * u / hp[k], 1) for k in (1, 2, 3)]
    cap_eff = [round(eff[k] * 24 * u / hp[k], 1) for k in (1, 2, 3)]
    rev = rev_now if (rev_now and obs.contract == 3) else {1: 750, 2: 1000, 3: 1500}[obs.contract]
    margin = rev - 60 * obs.kit_unit
    day = obs.day
    fut, t = 0.0, day
    while t < GAME_END - 0.7:          # orders stop around 299.3
        cap = min(cap_eff) if t >= day + 3 else min(cap_now)
        fut += min(f.rate(t), cap) * margin * 0.5 - debt * 0.20 / 365 * 0.5
        t += 0.5
    resale = sum((eff[k] - 1) * PRICES[k][1] for k in (1, 2, 3))
    return dict(demand=round(f.rate(day), 1), cap=cap_now, cap_eff=cap_eff,
                int_day=round(debt * 0.20 / 365), model_gain=round(fut + resale), margin=round(margin))


def strategy(snap, cfg, st) -> tuple[dict, dict, list]:
    """Live tweaks on top of config.yaml.
    WIP probe: run the contract-3 WIP cap a bit looser (more orders accepted). If the
    average price per job over the last two days falls below the floor, drop back to
    the safe setting for 3 game days, then try again.
    Loan lock: once the bot has started repaying, it never borrows again (each new loan
    costs a 5% fee)."""
    pol = dict(cfg.get("policy") or {}) | {"allow_loans": cfg.get("allow_loans", False)}
    mode, notes = {}, []
    probe = pol.get("c3_wip_days_probe")
    if probe and snap.contract == 3:
        ser = snap.series.get("JOBREV_c3") or {}
        revs = [ser[d] for d in sorted(ser)[-2:] if ser[d] > 0]
        avg = sum(revs) / len(revs) if revs else None
        floor = pol.get("c3_min_rev", 1320)
        if st.get("wip_probe_off_until", 0) > snap.day:
            mode["wip_mode"] = "safe"
        elif (avg is not None and avg < floor and st.get("wip_probe_since") is not None
              and snap.day - st["wip_probe_since"] >= 1.0):
            st["wip_probe_off_until"] = snap.day + 3
            st.pop("wip_probe_since", None)
            mode["wip_mode"] = "safe"
            notes.append(f"  wip probe off for 3 days: price/job ${avg:,.0f} < ${floor:,.0f}")
        else:
            pol["c3_wip_days"] = probe
            st.setdefault("wip_probe_since", snap.day)
            mode["wip_mode"] = "probe"
        mode["rev2d"] = round(avg) if avg else None
    if st.get("repaid"):
        pol["allow_loans"] = False
    mode["loans_locked"] = bool(st.get("repaid"))
    return pol, mode, notes


def gate_buys(acts, snap, cfg) -> tuple[list, list]:
    """Only buy at a station that is actually busy. The policy sizes capacity to forecast
    demand, but the WIP cap (which protects the 6h quote) accepts fewer jobs than that,
    so a station can look short on paper while sitting idle. Below `buy_min_util`
    (2-day average) a new machine adds no output. Loans tied to blocked buys go too."""
    pol = cfg.get("policy") or {}
    floor = pol.get("buy_min_util")
    if not floor or snap.day < pol.get("buy_gate_from_day", 0):
        return acts, []
    util = {}
    for k in (1, 2, 3):
        ser = snap.series.get(f"S{k}UTIL") or {}
        vals = [ser[d] for d in sorted(ser)[-2:]]
        util[k] = sum(vals) / len(vals) if vals else None
    keep, notes, blocked = [], [], 0
    for a in acts:
        if a.kind == "buy" and util.get(a.station) is not None and util[a.station] < floor:
            blocked += 1
            notes.append(f"  skip {a}: station {a.station} only {util[a.station]:.0%} busy (2-day avg)")
            continue
        keep.append(a)
    if blocked and not any(a.kind == "buy" for a in keep):
        dropped = [a for a in keep if a.kind == "loan"]
        keep = [a for a in keep if a.kind != "loan"]
        notes += [f"  skip {a}: nothing left to fund" for a in dropped]
    return keep, notes


def kpis(snap) -> str:
    """Yesterday's numbers from the game's own plots, for the log."""
    def last(key):
        ser = snap.series.get(key) or {}
        return ser[max(ser)] if ser else None
    out = []
    jobs = sum(v for k, v in ((k, last(k)) for k in snap.series if k.startswith("JOBOUT")) if v)
    out.append(f"done {jobs:.0f}/d")
    for c in (1, 2, 3):
        lt, rev, n = last(f"JOBT_c{c}"), last(f"JOBREV_c{c}"), last(f"JOBOUT_c{c}")
        if n:
            out.append(f"c{c} lt {lt * 24:.1f}h rev ${rev:,.0f}")
    q = sum((last(f"S{k}Q") or 0) for k in (1, 2, 3)) / 60
    out.append(f"queued {q:.0f} jobs")
    a = last("JOBIN")
    if a is not None:
        out.append(f"accepted {a:.0f}/d")
    u = [last(f"S{k}UTIL") for k in (1, 2, 3)]
    if all(x is not None for x in u):
        out.append("util " + "/".join(f"{x:.2f}" for x in u))
    return " ".join(out)


def guard(acts, obs, cfg):
    """Hard limits that hold whatever the policy says."""
    ok, spend = [], 0
    cap = cfg.get("max_spend_per_run", 600_000)
    for a in acts:
        if a.kind == "buy":
            n = a.value - obs.machines[a.station]
            if n <= 0 or obs.on_order.get(a.station, 0) > 0:
                continue
            n = min(n, cfg.get("max_buy_per_station_per_run", 8), int((cap - spend) // PRICES[a.station][0]))
            if n <= 0:
                continue
            a.value = obs.machines[a.station] + n
            spend += n * PRICES[a.station][0]
        if a.kind == "sell":
            if a.value < 1 or a.value >= obs.machines[a.station]:
                continue
            if obs.machines[a.station] - a.value > cfg.get("max_sell_per_run", 3):
                a.value = obs.machines[a.station] - cfg.get("max_sell_per_run", 3)
        if a.kind == "loan" and not cfg.get("allow_loans", False):
            continue
        if a.kind in ("rop", "roq") and a.value < 1:
            continue
        ok.append(a)
    return ok


def execute(client, snap, a):
    c = snap.controls
    if a.kind == "buy":
        # the game's count is installed + on order, so add what's in transit
        return client.submit(c[f"count{a.station}"], a.value + snap.on_order.get(a.station, 0))
    if a.kind == "sell":
        return client.submit(c[f"count{a.station}"], a.value + snap.on_order.get(a.station, 0))
    if a.kind in ("rop", "roq", "contract", "wip", "rule", "lots"):
        if a.kind not in c:
            raise GameError(f"no '{a.kind}' control on the page")
        return client.submit(c[a.kind], a.value)
    if a.kind in ("loan", "repay"):
        if not snap.loan_url:
            raise GameError("loan form not found on CashStatus")
        return client.loan(snap.loan_url, a.value, increase=(a.kind == "loan"))
    raise GameError("unknown action " + a.kind)


def summary(lines):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as f:
            f.write("\n".join(lines) + "\n")


def main(client=None, now=None) -> int:
    if os.environ.get("PAUSE", "").strip() in ("1", "true", "yes"):
        print("PAUSE set, skipping run")
        return 0
    cfg = load_cfg()
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes") or cfg.get("dry_run", False)
    st = load_state()
    st["runs"] += 1
    ts = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = client or LittlefieldClient(os.environ["LF_ID"], os.environ["LF_PASSWORD"],
                                         base=os.environ.get("LF_BASE", "https://op2.responsive.net"),
                                         institution=os.environ.get("LF_INSTITUTION", "iimb"))
    lines = []
    rc = 0
    try:
        client.login()
        snap = client.snapshot()
        obs = to_obs(snap)
        head = (f"{ts} day {snap.day:.1f} cash ${snap.cash:,.0f} debt ${snap.debt:,.0f} "
                f"machines {snap.machines} on-order {snap.on_order} inv {snap.inventory} "
                f"rop/roq {snap.rop}/{snap.roq} contract {snap.contract} wip {snap.wip_limit} "
                f"rule {snap.rule} edits={'on' if obs.can_edit_orders else 'OFF'} | {kpis(snap)}")
        rank = client.standing() if hasattr(client, "standing") else None
        rank_txt = (f"rank {rank['rank']}/{rank['teams']} (leader ${rank['leader']:,.0f})"
                    if rank and rank.get("rank") else "rank n/a")
        head += f" | {rank_txt}"
        lines.append(head)
        done, acts_rec, mode = [], [], {}
        if snap.day >= cfg.get("stop_after_day", 299.5):
            lines.append("  game over or about to end: no actions")
        else:
            pol, mode, notes = strategy(snap, cfg, st)
            lines.extend(notes)
            if pol.get("c3_mode") and "lots" not in snap.controls:
                pol["c3_mode"] = False   # can't split lots: contract 3 can't hit its 6h quote
                lines.append("  c3 mode off: no lot-size control on the page")
            acts, gnotes = gate_buys(decide(obs, pol), snap, cfg)
            lines.extend(gnotes)
            acts = guard(acts, obs, cfg)
            for a in acts:
                if dry:
                    lines.append(f"  DRY  {a}")
                    continue
                try:
                    res = execute(client, snap, a)
                    lines.append(f"  OK   {a} | {res}")
                    done.append(str(a).split("  (")[0])
                    acts_rec.append(dict(t=ts, day=round(snap.day, 2), ok=True, act=str(a).split("  (")[0],
                                         why=a.reason, res=str(res).replace("Processing request...", "").strip()[-160:]))
                    st["actions"].append({"ts": ts, "day": snap.day, "act": str(a)})
                    if a.kind == "repay":
                        st["repaid"] = True
                except GameError as e:
                    lines.append(f"  FAIL {a} | {e}")
                    acts_rec.append(dict(t=ts, day=round(snap.day, 2), ok=False, act=str(a).split("  (")[0],
                                         why=a.reason, res=str(e)[:160]))
                    rc = 2
            if not acts:
                lines.append("  no changes needed")
        st["last_day"] = snap.day
        st["last"] = head
        try:
            kp = dict(kpi_dict(snap), t=ts, **mode)
            try:
                kp.update(model_kpis(obs, snap, cfg, acts_rec, kp.get("rev")))
            except Exception as e:  # noqa: BLE001
                lines.append(f"  dashboard model failed: {e}")
            dashboard.update(STATE.parent / "dashboard.json", LOG, snap.team or cfg.get("team", ""),
                             snap, kp, rank, acts_rec)
        except Exception as e:  # noqa: BLE001
            lines.append(f"  dashboard data failed: {e}")
        major = [x for x in done if not x.startswith(("rop", "roq"))]
        if done and not dry and (cfg.get("notify_on", "all") == "all" or major):
            msg = (f"Littlefield day {snap.day:.1f}: cash ${snap.cash:,.0f}, debt ${snap.debt:,.0f}, {rank_txt}\n"
                   f"{kpis(snap)}\nmachines {snap.machines}, contract {snap.contract}, wip cap {snap.wip_limit}\n"
                   f"changes: " + "; ".join(done))
            lines.append("  notify: " + ", ".join(notify.send(msg, st, cfg.get("notify_owner"))))
    except Exception as e:  # noqa: BLE001
        lines.append(f"{ts} ERROR {e}")
        lines.append(traceback.format_exc())
        rc = 1
    finally:
        client.quit()
    print("\n".join(lines))
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write("\n".join(lines) + "\n")
    summary(lines)
    save_state(st)
    return rc


if __name__ == "__main__":
    sys.exit(main())
