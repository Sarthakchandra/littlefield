"""Decision policy. Pure function of an Observation -> list of Actions.

Same code runs against the live game (via lf.client) and the digital twin
(via lf.sim), so what we test offline is exactly what ships.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .sim import CONTRACTS, PRICES, SETUP, PER_UNIT, CALIB

GAME_END = 300


@dataclass
class Observation:
    day: float
    cash: float
    machines: dict            # {1: n, 2: n, 3: n} installed
    on_order: dict            # {1: n, 2: n, 3: n} machines bought, not yet arrived
    inventory: int
    kits_on_order: int        # 0 if none outstanding
    rop: int
    roq: int
    kit_unit: float
    kit_order_cost: float
    contract: int
    lots_per_job: int
    wip_limit: int
    rule: str
    series: dict              # name -> {day: value}; JOBIN, JOBOUT, S1Q, S2Q, S3Q, JOBQ
    can_edit_orders: bool = True   # contract / lot size / WIP editable?
    debt: float = 0.0


@dataclass
class Action:
    kind: str     # buy | sell | rop | roq | contract | wip | rule | lots | loan
    value: object
    station: int | None = None
    reason: str = ""

    def __str__(self):
        st = f" S{self.station}" if self.station else ""
        return f"{self.kind}{st} -> {self.value}  ({self.reason})"


DEFAULTS = dict(
    util_target=0.90,          # planned utilisation at forecast demand
    flush_days=6.0,            # clear existing backlog within this many days
    plan_ahead=7.0,            # days of demand beyond the 3-day machine lead time
    min_payback_mult=1.3,      # machine must earn >= this x its net cost before day 300
    safety_days=1.0,           # kit safety stock, in days of demand
    holding_per_kit_day=0.098,
    cash_buffer=20_000,
    max_spend_per_run=600_000,
    max_machines=25,
    rule="pri4",
    enable_c3=False,
    c2_switch_lt=1.0,          # switch to c2 once a new job's predicted lead time fits the 1-day quote
    c2_revert_lt=1.5,          # back to c1 past break-even: c2 pays $750 at 1.5 days (quote 1d, $0 at 3d)
    wip_days=0.6,              # WIP cap = throughput x this many days
    min_wip=8,
    sell_from_day=200,
    allow_loans=False,
    max_debt=600_000,
    low_frac_prior=0.6,        # prior: post-day-180 demand as share of peak
    backlog_term=True,         # size machines to also clear the day-50 backlog
    buy_order="loaded",        # loaded = highest load first; binding = lowest installed capacity first
    loan_upfront=False,        # take max_debt on the first run, before buying
    kit_margin=None,           # None = old rule; else Q >= 4 days x (1+margin), ROP = 4 days + z sd
    kit_z=2.0,
    sell_mode="final",        # surplus = sell surplus from sell_from_day; final = sell down to 1 each at the end
    stop_accept_day=299.3,
    final_sale_day=299.5,
    wave_only=True,
    day50_wave=None,           # e.g. {1: 4, 2: 5, 3: 2}: fixed first purchase instead of the rule
    buy_while_pending=False,   # top up a station even if machines are still in transit
    c3_mode=False,             # split lots and run contract 3 (6h quote) after the flush
    target_lots=5,             # lots per job in c3 mode (5 x 12 kits: no-wait flow ~4.3h)
    c3_lt=0.27,                # switch to c3 when predicted lead time (days) is at or below this
    c3_revert_lt=0.33,         # c3 pays $1,000 at 8h; above that c2 is better
    c3_wip_days=0.22,          # WIP cap = throughput x this in c3 mode
    hold_after_day=55,
    no_edit_mode="hold",       # if contract/WIP can't be edited: "hold" cash or "invest" anyway
)


def hours_per_job(station: int, lots: int = 1) -> float:
    """Machine-hours one 60-kit job needs at a station (both test passes at S2)."""
    size = 60 // lots
    per_lot = SETUP[station] + PER_UNIT[station] * size
    h = per_lot * lots * CALIB[station]
    return h * 2 if station == 2 else h


def _ceil60(x):
    return int(max(60, math.ceil(x / 60.0) * 60))


# ---------------------------------------------------------------- demand
class Forecaster:
    def __init__(self, obs: Observation, cfg: dict):
        self.cfg = cfg
        s = obs.series
        jobin = s.get("JOBIN", {})
        wip_est = {}
        for d in jobin:
            q = sum(s.get(f"S{k}Q", {}).get(d, 0.0) for k in (1, 2, 3)) / 60.0
            wip_est[d] = q + s.get("JOBQ", {}).get(d, 0.0)
        cap = obs.wip_limit
        out = s.get("JOBOUT", {})
        # a day is censored if orders were turned away: the factory sat near its WIP cap,
        # or (with a tight bot-set cap, where queues stay short) it accepted about as much
        # as it could finish. Accepted orders on those days understate demand.
        def censored(d, v):
            if d < 50:   # before we took over the cap was the default 300
                return wip_est.get(d, 0) >= 0.85 * 300
            # From day 50 the bot sets the WIP cap and kit supply, so accepted orders
            # measure our own limits (capacity, cap, kit stock-outs), never demand.
            return True
        self.clean = {d: v for d, v in jobin.items() if d >= 1 and not censored(d, v)}
        pts = [(d, v) for d, v in self.clean.items() if d <= 95]
        if len(pts) >= 8:
            n = len(pts)
            mx = sum(d for d, _ in pts) / n
            my = sum(v for _, v in pts) / n
            sxx = sum((d - mx) ** 2 for d, _ in pts)
            b = sum((d - mx) * (v - my) for d, v in pts) / sxx if sxx else 0.0
            self.a, self.b = my - b * mx, max(0.0, b)
        else:
            self.a, self.b = 3.69, 0.521
        self.peak = self.a + self.b * 90
        self.day = obs.day
        recent = [v for d, v in self.clean.items() if obs.day - 10 <= d <= obs.day]
        self.recent_mean = sum(recent) / len(recent) if len(recent) >= 5 else None
        # plateau observed after day 100
        plateau = [v for d, v in self.clean.items() if 100 <= d <= 180]
        if len(plateau) >= 7:
            self.peak = sum(plateau) / len(plateau)
        low = [v for d, v in self.clean.items() if 185 <= d <= 240]
        self.low = sum(low) / len(low) if len(low) >= 5 else self.peak * cfg["low_frac_prior"]

    def rate(self, t: float) -> float:
        if t <= 90:
            r = self.a + self.b * t
        elif t <= 180:
            r = self.peak
        elif t <= 240:
            r = self.low
        else:
            r = self.low * max(0.0, (GAME_END - t) / 60.0)
        # if we have clean recent data near t, trust it over the curve
        if self.recent_mean is not None and abs(t - self.day) <= 3 and t > 90:
            r = self.recent_mean
        return max(0.0, r)

    def max_rate(self, t0, t1):
        if t1 <= t0:
            return 0.0 if t0 >= GAME_END else self.rate(t0)
        return max(self.rate(t0 + i * 0.5) for i in range(int((t1 - t0) / 0.5) + 1))


# ---------------------------------------------------------------- policy
def decide(obs: Observation, cfg: dict | None = None) -> list[Action]:
    cfg = {**DEFAULTS, **(cfg or {})}
    acts: list[Action] = []
    if not obs.can_edit_orders and cfg["no_edit_mode"] == "hold":
        # give the game a few days to unlock order edits before giving up on them
        return _hold_mode(obs) if obs.day >= cfg["hold_after_day"] else []
    f = Forecaster(obs, cfg)
    d = obs.day
    lots = obs.lots_per_job
    h = {k: hours_per_job(k, lots) for k in (1, 2, 3)}
    plan_lots = cfg["target_lots"] if cfg["c3_mode"] else lots
    hp = {k: hours_per_job(k, plan_lots) for k in (1, 2, 3)}
    u = cfg["util_target"]
    s = obs.series

    def last(name):
        ser = s.get(name, {})
        return ser[max(ser)] if ser else 0.0

    q = {k: last(f"S{k}Q") for k in (1, 2, 3)}
    wip_jobs = (q[1] + q[2] + q[3]) / 60.0 + last("JOBQ")
    # backlog work by station (machine-hours)
    j1, j2, j3 = q[1] / 60, q[2] / 60, q[3] / 60
    backlog_h = {
        1: j1 * h[1],
        2: (j1 * 2 + j2 * 1.4 + j3) * h[2] / 2,
        3: (j1 + j2 * 0.75 + j3) * h[3],
    }

    # ---- contract + WIP cap (Little's law: lead time = WIP / throughput)
    contract = obs.contract
    eff = {k: obs.machines[k] + obs.on_order.get(k, 0) for k in (1, 2, 3)}
    mu = min(eff[k] * 24 / hp[k] for k in (1, 2, 3))           # jobs/day once orders land
    mu_now = min(obs.machines[k] * 24 / hp[k] for k in (1, 2, 3))
    size = 60 // plan_lots
    flow = (SETUP[1] + PER_UNIT[1] * size + 2 * PER_UNIT[2] * size * CALIB[2] + SETUP[3] + PER_UNIT[3] * size) / 24
    lt_new = max(flow, wip_jobs / max(mu_now, 0.1))
    if obs.can_edit_orders:
        # cap WIP so a new job's lead time stays inside the c2 quote. When demand
        # beats capacity this rejects the excess instead of letting every job go late.
        wd = cfg["c3_wip_days"] if cfg["c3_mode"] else cfg["wip_days"]
        cap = int(max(cfg["min_wip"], round(mu_now * wd)))
        if cfg["sell_mode"] == "final" and d >= cfg["stop_accept_day"]:
            cap = 1   # nothing accepted now can finish by day 300
        if abs(cap - obs.wip_limit) >= max(2, 0.12 * obs.wip_limit):
            acts.append(Action("wip", cap, reason=f"μ {mu_now:.1f} jobs/d x {cfg['wip_days']}d"))
        if cfg["c3_mode"] and obs.lots_per_job != plan_lots:
            acts.append(Action("lots", plan_lots, reason=f"{60 // plan_lots}-kit lots: flow {flow * 24:.1f}h fits the 6h quote"))
        if cfg["c3_mode"] and contract != 3 and lt_new <= cfg["c3_lt"]:
            contract = 3
            acts.append(Action("contract", 3, reason=f"pred lead {lt_new * 24:.1f}h"))
        elif cfg["c3_mode"] and contract == 3 and lt_new > cfg["c3_revert_lt"]:
            contract = 2
            acts.append(Action("contract", 2, reason=f"pred lead {lt_new * 24:.1f}h, c2 pays more past 8h"))
        elif contract == 1 and lt_new <= cfg["c2_switch_lt"]:
            contract = 2
            acts.append(Action("contract", 2, reason=f"backlog flushed, pred lead {lt_new:.2f}d"))
        elif contract == 2 and lt_new > cfg["c2_revert_lt"]:
            contract = 1
            acts.append(Action("contract", 1, reason=f"pred lead {lt_new:.2f}d, c1 pays more"))
    target_contract = (3 if cfg["c3_mode"] else 2) if obs.can_edit_orders else contract
    margin = CONTRACTS[target_contract]["rev"] - 60 * obs.kit_unit - 60 * 1.5 * cfg["holding_per_kit_day"]

    # ---- capacity
    t0 = d + 3.0
    lam_plan = f.max_rate(t0, min(GAME_END, t0 + cfg["plan_ahead"]))
    lam_life = f.max_rate(t0, GAME_END) if t0 < GAME_END else 0.0
    rem = max(0.0, GAME_END - t0)
    spend_left = min(cfg["max_spend_per_run"],
                     obs.cash - (obs.roq * obs.kit_unit + obs.kit_order_cost) - cfg["cash_buffer"])
    upfront = 0
    if (cfg["allow_loans"] and cfg["loan_upfront"] and obs.can_edit_orders and obs.debt == 0
            and d < 52 and lt_new > 3):
        upfront = int(cfg["max_debt"])
        acts.insert(0, Action("loan", upfront, reason="day-50 loan to fund the first capacity wave"))
        spend_left = min(cfg["max_spend_per_run"], spend_left + upfront)
    want = {}
    for k in (1, 2, 3):
        steady = lam_plan * hp[k] / (24 * u)
        flush = backlog_h[k] / (24 * cfg["flush_days"]) / u if cfg["backlog_term"] else 0.0
        need = math.ceil(steady + flush - 1e-9)
        life_need = math.ceil(lam_life * hp[k] / (24 * u) + 1e-9)
        need = min(need, life_need + 1, cfg["max_machines"])
        want[k] = max(1, need)

    # payback gate: a machine only pays if it serves enough jobs before day 300
    def pays(k):
        if not obs.can_edit_orders:
            return True   # no WIP cap to shed excess demand: any shortage makes every job late
        price, salvage = PRICES[k]
        per_day = (24 * u / hp[k]) * margin
        return per_day * rem >= cfg["min_payback_mult"] * (price - salvage)

    buys = []
    wave = cfg["day50_wave"]
    if wave and d < 52 and lt_new > 3 and not any(obs.on_order.values()):
        for k in (1, 2, 3):
            want[k] = max(want[k] if not cfg["wave_only"] else 0, obs.machines[k] + int(wave.get(k, wave.get(str(k), 0))))
    shortfall = {k: want[k] - eff[k] for k in (1, 2, 3)}
    # buy one at a time on the most loaded station, so a cash limit hits the
    # least critical station last
    alloc = {k: 0 for k in (1, 2, 3)}
    while True:
        cands = [k for k in (1, 2, 3) if shortfall[k] - alloc[k] > 0 and pays(k)]
        if not cands:
            break
        if cfg["buy_order"] == "binding":
            k = min(cands, key=lambda k: (eff[k] + alloc[k]) * 24 / hp[k])
        else:
            k = max(cands, key=lambda k: (lam_plan * hp[k] + backlog_h[k] / cfg["flush_days"]) /
                    (24 * (eff[k] + alloc[k])))
        price = PRICES[k][0]
        if spend_left < price:
            break
        alloc[k] += 1
        spend_left -= price
    for k in (1, 2, 3):
        if alloc[k] and (obs.on_order.get(k, 0) == 0 or cfg["buy_while_pending"]):
            buys.append(Action("buy", obs.machines[k] + alloc[k], station=k,
                               reason=f"want {want[k]} (λplan {lam_plan:.1f}/d, backlog {backlog_h[k]:.0f}h)"))
    acts += buys
    unfunded = sum(PRICES[k][0] * (shortfall[k] - alloc[k]) for k in (1, 2, 3)
                   if shortfall[k] - alloc[k] > 0 and pays(k))
    if upfront:
        pass
    elif cfg["allow_loans"] and obs.can_edit_orders and unfunded > 0 and d < 150 and obs.debt < cfg["max_debt"]:
        amt = min(unfunded * 1.05, cfg["max_debt"] - obs.debt)
        if amt >= 50_000:
            acts.append(Action("loan", int(amt), reason=f"unfunded high-payback capacity ${unfunded:,.0f}"))
    elif obs.debt > 0 and unfunded == 0:
        free = obs.cash - (obs.roq * obs.kit_unit + obs.kit_order_cost) * 2 - cfg["cash_buffer"] * 3
        if free > 20_000 and obs.debt >= 1_000:
            acts.append(Action("repay", int(min(free, obs.debt)), reason="debt costs 20%, cash earns 10%"))

    # ---- end of game: stop accepting, then sell down once the plant is empty
    if cfg["sell_mode"] == "final":
        if d >= cfg["final_sale_day"] and wip_jobs < 3:   # plots lag a day; a few jobs still finish on 1 machine
            for k in (1, 2, 3):
                if obs.machines[k] > 1:
                    acts.append(Action("sell", 1, station=k, reason="game over: resale beats idle machine"))
    # ---- sell surplus late in the game
    elif d >= cfg["sell_from_day"]:
        for k in (1, 2, 3):
            keep = max(1, math.ceil(lam_life * h[k] / (24 * u) + 1e-9) + 1)
            if obs.machines[k] > keep and obs.on_order.get(k, 0) == 0 and backlog_h[k] < 24:
                acts.append(Action("sell", keep, station=k,
                                   reason=f"life demand needs {keep - 1}+1 spare"))

    # ---- tester rule
    if obs.rule != cfg["rule"]:
        acts.append(Action("rule", cfg["rule"], station=2, reason="config"))

    # ---- kits
    # Scale the demand curve up to what we actually accept. The curve comes from days 1-50
    # and last year's shape; if orders run ahead of it (e.g. no drop at day 180), kits
    # planned off the curve alone would run out, and a stock-out stops the whole factory.
    jin = s.get("JOBIN", {})
    recent = [jin[x] for x in sorted(jin)[-3:]]
    seen = sum(recent) / len(recent) if recent else 0.0
    base = f.rate(d)
    kscale = min(10.0, max(1.0, seen / base)) if (base > 1 and cfg.get("kit_follow_orders", True)) else 1.0
    D = 60 * min(f.max_rate(d, d + 5) * kscale, mu)     # kits/day we will actually consume
    if obs.can_edit_orders and wip_jobs > 0:
        # while the WIP cap is binding we accept roughly what we ship
        pass
    S = obs.kit_order_cost
    hold = cfg["holding_per_kit_day"]
    eoq = math.sqrt(2 * D * S / hold) if D > 0 else 60
    lead = 4.0
    if cfg["kit_margin"] is None:
        rop = _ceil60((lead + cfg["safety_days"]) * D)
        roq = _ceil60(max(eoq, (lead + 0.5) * D))
    else:
        dj = D / 60
        rop = _ceil60(lead * D + cfg["kit_z"] * 60 * math.sqrt(lead * dj))
        roq = _ceil60(max(eoq, (1 + cfg["kit_margin"]) * lead * D))
    # endgame: never buy kits we can't turn into revenue
    need_rest = sum(60 * min(f.rate(t + 0.5) * kscale, mu) for t in range(int(d), GAME_END - 1))
    have = obs.inventory + obs.kits_on_order
    endgame = obs.rop <= 60 and d > 200
    if have >= need_rest or (endgame and have >= 0.8 * need_rest):
        rop = 60
        roq = 60
    else:
        roq = min(roq, _ceil60(need_rest - have))
    afford = int((obs.cash - cfg["cash_buffer"] - sum(PRICES[a.station][0] * (a.value - obs.machines[a.station])
                                                      for a in buys)) / obs.kit_unit)
    roq = max(60, min(roq, (afford // 60) * 60))
    # the game changes one field per submit: post Q before ROP so a raised ROP
    # can't fire an order at the old, smaller Q
    if _changed(obs.roq, roq, 0.3 if roq > obs.roq else 0.15):
        acts.append(Action("roq", roq, reason=f"EOQ {eoq:.0f}, cover {lead + 0.5}d"))
    if _changed(obs.rop, rop):
        acts.append(Action("rop", rop, reason=f"D {D:.0f} kits/d, lead 4d + {cfg['safety_days']}d safety"))
    return acts


def _changed(old, new, tol=0.15):
    if old == new:
        return False
    if new <= 60 or old <= 60:
        return True
    return abs(new - old) > max(tol * old, 600)


def _hold_mode(obs: Observation) -> list[Action]:
    """Contract and WIP locked: every job finishes late and earns ~$0 while each
    still costs $600 of kits. Stop feeding the factory and keep the cash."""
    acts = []
    if obs.rop != 1:
        acts.append(Action("rop", 1, reason="orders not editable: starve kits, hold cash"))
    if obs.roq != 60:
        acts.append(Action("roq", 60, reason="orders not editable: minimum kit order"))
    return acts
