"""backtest — the offline evaluation apparatus (REPLAY_CONTRACT.md §10).

Dependency direction, non-negotiable (mirror of Rule A8):

    backtest  ──imports──►  v2
    backtest  ◄──────────╳  v2        (v2 NEVER imports backtest)

Nothing in this package may be imported by the production runtime. Nothing in
this package may change what `v2` decides; it exists to *measure* the behavior
`v2` already has.

Contents grow one milestone at a time. Present today:

- `backtest.data` — the read-only historical kline repository (Milestone M3).
"""
