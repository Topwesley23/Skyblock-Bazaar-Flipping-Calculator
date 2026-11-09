# Day Trader dashboard

One page that runs the `trader-*` skills in `.claude/skills` for you and shows
everything they find.

```bash
python3 dashboard/server.py        # then open http://127.0.0.1:8050
```

Python 3.8+, nothing to install. It only listens on your own computer.

## What it does

- **Scan now** runs the whole chain on your watchlist: scanner, entry timer,
  exit planner, Time Machine, news, budget and risk check. Each stock gets a card
  with its plan, the odds, the headlines and a verdict.
- **Find ideas** repeats that every few minutes while the market is open
  (9:50 AM to your last-entry time). You click to paper trade.
- **Auto paper-trade** also buys the ideas that pass every check, in the
  simulated paper account, and sells them at the stop, the target or your
  "out by" time.
- **Settings** holds your rules (budget, max loss, daily limit...) and what to scan.

## Paper only

Every trade here is simulated at a delayed Yahoo price, in `trader/paper/`.
Nothing on this page talks to a broker. To place a real order, open an idea's
"Real order ticket" and give it to Claude with `/trader-broker`, which checks it
again and asks for your yes.

Ideas only count as "ready" when the breakout happened in the last 10 minutes:
an old breakout is a different trade. So leave it running from the open.

All data lives in `trader/`, which git ignores.
