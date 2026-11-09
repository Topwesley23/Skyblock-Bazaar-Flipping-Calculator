# Day Trader dashboard

One page that runs the `trader-*` skills in `.claude/skills` for you and shows
everything they find.

```bash
python3 dashboard/server.py        # then open http://127.0.0.1:8050
```

Python 3.8+, nothing to install. It only listens on your own computer.

## On your phone

```bash
python3 dashboard/server.py --lan
```

It prints a link like `http://192.168.1.23:8050/?key=...`. Open that on your
phone while it is on the same Wi-Fi as the computer. The key is remembered, so
after the first visit you can bookmark the page or use "Add to Home Screen" to
get an app icon. Keep the link private: anyone with it, on your Wi-Fi, can use
the dashboard. Delete `trader/dashboard/key.txt` and restart for a new key.

If the phone can't connect, allow Python through the computer's firewall
(Windows asks the first time; on a Mac, accept "incoming connections").
The computer has to stay on and awake while you use it.

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
