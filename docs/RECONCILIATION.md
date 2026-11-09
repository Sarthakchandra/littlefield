# Reconciliation: "Winning Strategy and Reconciliation Guide" vs the bot

Date: 9 Nov 2026, before the 9:30 pm start. Every proposal was tested in `lf/twin.py`, our discrete-event twin. It is calibrated to team 8a's day-50 data and models setups and two-pass testing. Unless stated otherwise, figures are the mean net cash at day 300 over 5 runs, with the worst run in brackets.

## Headline

The guide's twin reports $3.0M for its plan against $2.0M for the bot. Ours does not reproduce that gap. Its day-50 purchase (+4 stuffers, +5 testers, +2 tuners) plus dropping the backlog term scores $2.33M ($2.28M), level with the bot's $2.33M ($2.27M). The plan is also fragile:

- **Peak demand 30% below forecast:** the guide's plan averages $1.34M, with a worst run of **-$0.29M**. The bot holds $1.80M ($1.50M). Buying ~$960k up front before seeing demand is the risk.
- **Peak demand 30% above forecast:** the guide's plan wins slightly, $2.47M vs $2.42M.

So the bot's core sizing rule stays. Three changes from the guide are adopted.

## Proposal by proposal

| # | Guide proposal | Our twin | Decision |
|---|---|---|---|
| E1 | Drop the backlog term from machine sizing | $2.18M vs $2.32M | Not adopted |
| E2 | Buy the lowest-capacity station first | $2.23M vs $2.32M | Not adopted |
| E3 | Purchase lock per station | Already per station | No change |
| E4 | WIP cap on installed capacity, not completions | Already uses installed machines x rate | No change |
| E5 | Kits: Q >= 1.2 x 4 days, ROP = 4 days + 2 sd | $2.27M vs $2.32M; +$66k at -30% demand, -$32k at +30% | Not adopted (kept as `kit_margin` option) |
| E6 | Revert to contract 1 on economics, not at 1.8 days | Break-even: c2 pays $750 at 1.5 days | **Adopted: revert at 1.5 days** |
| E7 | Never refit demand on capped days | Forecaster already drops days with WIP near the cap | No change |
| O1/O2 | No mid-game sales; stop accepting at 299.3, sell down to 1 per station at the end | +$11k | **Adopted** |
| O3 | No kit order after 295.5, last order sized to need | Already stops once stock covers demand to day 300 | No change |
| O5 | 12-day planning horizon | $2.28M ($2.12M) vs $2.32M ($2.25M) | Not adopted |
| O6 | Save cash for the binding station | Already breaks the buy loop when the chosen station is unaffordable | No change |
| - | Day-50 wave +4/+5/+2 with a $600k up-front loan | Level in the base case, fails at -30% demand | Not adopted (kept as `day50_wave` / `loan_upfront` options) |
| - | Post order quantity before reorder point when raising both | Logic is right: one field per submit | **Adopted** |
| - | Final test first, lot 60, c2 after the flush, $600k loan, hard limits | Same as the bot | Keep |

## Why the twins disagree

The guide's twin does not model setups ("the twin does not model setups"). Ours does: stuffer 1.4 h per lot and tuner 1.5 h per lot. Ours reproduces the live day-50 lead time, tester queue and throughput within 15%. Without setups, stuffer and tuner capacity is overstated, which makes an aggressive early purchase look better than it is.

## Result after the changes

8 runs, demand as forecast: mean **$2.29M**, worst $2.13M; do nothing $44k. All 18 tests pass.

## Config switches added (all off by default)

`backlog_term`, `buy_order: binding`, `kit_margin`, `day50_wave`, `loan_upfront`. Set them in `config.yaml` under `policy:` to try the guide's variants. Score any change first with `python scripts/evaluate.py key=value`.
