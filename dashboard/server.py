#!/usr/bin/env python3
"""Day Trader dashboard: one page that runs the trader-* skills for you.

    python3 dashboard/server.py            # then open http://127.0.0.1:8050

It chains the skills in .claude/skills (scanner -> entry -> exit -> time machine
-> news -> budget -> risk) on a timer, shows every idea with its numbers, and
runs a SIMULATED paper account: fills are pretend, at the delayed Yahoo price.

It never talks to a broker and never places a real order. To place a real one,
copy the ticket into Claude and use /trader-broker, which asks for your yes.

Standard library only. Binds to 127.0.0.1 so only this computer can open it.
"""

import datetime
import hmac
import json
import os
import secrets
import socket
import subprocess
import urllib.parse
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILLS = os.path.join(ROOT, ".claude", "skills")
TRADER = os.path.join(ROOT, "trader")
DASH = os.path.join(TRADER, "dashboard")
PAPER = os.path.join(TRADER, "paper")
WORK = os.path.join(DASH, "work")

CONFIG = os.path.join(TRADER, "config.json")
SETTINGS = os.path.join(DASH, "settings.json")
IDEAS = os.path.join(DASH, "ideas.json")
ACTIVITY = os.path.join(DASH, "activity.json")
PRICES = os.path.join(DASH, "prices.json")
PF = os.path.join(PAPER, "portfolio.json")
LOG = os.path.join(PAPER, "broker.json")
POSITIONS = os.path.join(PAPER, "positions.json")
TRADES = os.path.join(PAPER, "trades.json")

PY = sys.executable or "python3"

# Liquid US stocks, most under $150, so a small budget can size them.
DEFAULT_WATCHLIST = ("AAL,AMD,BAC,C,CCL,CSCO,DAL,F,GM,HOOD,INTC,JBLU,KO,LCID,MARA,NIO,NKE,"
                     "PFE,PLTR,PLUG,PYPL,RIVN,RIOT,SBUX,SNAP,SOFI,T,UBER,VZ,WBD,WFC,XOM")

DEFAULT_SETTINGS = {
    "mode": "off",               # off | ideas | auto
    "universe": "watchlist",     # watchlist | market
    "watchlist": DEFAULT_WATCHLIST,
    "min_price": 5.0,
    "scan_every_min": 5,
    "max_candidates": 6,
    "fresh_minutes": 10,         # a trigger older than this is a late entry: shown, never auto-taken
    "stop": "or-mid",
    "target": "2R",
    "replays": 5000,
    "skip_if_odds_against": True,
}

CONFIG_KEYS = {  # what the page may change in trader/config.json, and its type
    "budget": float, "cash_buffer_pct": float, "max_position_pct": float, "max_order_usd": float,
    "max_loss_per_trade_usd": float, "max_daily_loss_usd": float, "max_trades_per_day": int,
    "min_reward_risk": float, "no_entries_before": str, "no_entries_after": str, "flat_by": str,
    "blocked": list, "sec_user_agent": str,
}

LOCK = threading.RLock()     # one pipeline or monitor pass at a time
STATUS = {"busy": "", "last_scan": None, "last_monitor": None, "clock": None, "error": ""}


# ------------------------------------------------------------------ files

def read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(read(SETTINGS, {}) or {})
    return s


def config():
    return read(CONFIG)


def ensure_config():
    """trader/config.json from the budget skill's example, if there is none yet."""
    if not os.path.exists(CONFIG):
        ex = read(os.path.join(SKILLS, "trader-budget", "config.example.json"), {})
        ex["budget"] = 1000
        ex["max_order_usd"] = 1000
        ex["max_loss_per_trade_usd"] = 10
        ex["max_daily_loss_usd"] = 30
        ua = os.environ.get("SEC_USER_AGENT", "")
        ex["sec_user_agent"] = ua or ex.get("sec_user_agent", "")
        write(CONFIG, ex)


def activity(msg, kind="info"):
    log = read(ACTIVITY, []) or []
    log.append({"at": utc_iso(), "kind": kind, "msg": msg})
    write(ACTIVITY, log[-300:])


def utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ the skills

def tool(script, *args, timeout=600):
    """Run one skill's tool from the repo root. Returns (exit code, JSON or None, stderr)."""
    path = os.path.join(SKILLS, script)
    try:
        p = subprocess.run([PY, path] + [str(a) for a in args], cwd=ROOT, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 99, None, "%s timed out" % script
    try:
        data = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        data = None
    # the tools print "tool.py: reason" on a refusal; keep the reason
    prefix = os.path.basename(script) + ": "
    lines = [l[len(prefix):] if l.startswith(prefix) else l for l in (p.stderr or "").strip().splitlines()]
    return p.returncode, data, " ".join(lines[-3:])


def clock():
    code, data, err = tool("trader-alerts/alerts.py", "clock", "--json", timeout=30)
    if data:
        STATUS["clock"] = data
    return data or {}


def hhmm(s):
    try:
        h, m = str(s).split(":")[:2]
        return int(h) * 60 + int(m[:2])
    except (ValueError, AttributeError):
        return None


def ny_minutes(clk):
    t = (clk.get("now_new_york") or "")[11:16]
    return hhmm(t)


def quotes(symbols):
    """Delayed Yahoo quotes, merged into trader/dashboard/prices.json for sizing and the risk check."""
    symbols = sorted({s.upper() for s in symbols if s})
    if not symbols:
        return {}
    code, data, err = tool("trader-alerts/alerts.py", "quote", *symbols, "--write", PRICES, "--json",
                           timeout=120)
    out = {}
    for sym, q in ((data or {}).get("quotes") or {}).items():
        if q and q.get("price"):
            out[sym.upper()] = q
    return out


# ------------------------------------------------------------------ paper account (simulated)

def paper_portfolio():
    pf = read(PF)
    if pf is None:
        cfg = config() or {}
        pf = {"cash": float(cfg.get("budget") or 1000), "positions": [], "paper": True,
              "note": "Simulated account run by the dashboard. Not a broker account."}
        write(PF, pf)
    return pf


def apply_fill(symbol, side, qty, price, date):
    """Same portfolio shape as trader-broker's ticket.py, so trader-risk reads it."""
    pf = paper_portfolio()
    cost = round(qty * price, 2)
    pos = next((p for p in pf["positions"] if p["ticker"] == symbol), None)
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        pos["lots"].append({"id": "%s-%d" % (symbol, len(pos["lots"]) + 1), "date": date,
                            "shares": float(qty), "cost": cost})
        pos["shares"] = round(pos["shares"] + qty, 6)
        pf["cash"] = round(pf["cash"] - cost, 2)
    else:
        if pos:
            pf["positions"].remove(pos)
        pf["cash"] = round(pf["cash"] + cost, 2)
    write(PF, pf)


def log_order(rec):
    log = read(LOG, {"orders": []}) or {"orders": []}
    log.setdefault("orders", []).append(rec)
    write(LOG, log)


def ny_date(clk=None):
    clk = clk or STATUS.get("clock") or clock()
    return (clk.get("now_new_york") or "")[:10] or datetime.date.today().isoformat()


def paper_buy(idea, price, why="manual"):
    sym = idea["symbol"]
    qty = int(idea["qty"])
    clk = STATUS.get("clock") or clock()
    today = ny_date(clk)
    positions = read(POSITIONS, []) or []
    if any(p["symbol"] == sym for p in positions):
        return "already holding %s" % sym
    entry_ref = idea["entry"]
    stop, target = idea["stop"], idea["target"]
    if price <= stop:
        return "%s is at %.2f, already at or under the stop %.2f" % (sym, price, stop)
    max_loss = round(qty * (price - stop), 2)
    log_order({"at": utc_iso(), "date": today, "symbol": sym, "side": "buy", "qty": str(qty),
               "kind": "entry", "max_loss": max_loss, "stop": stop, "status": "placed",
               "plain": "BUY %d %s (paper, simulated)" % (qty, sym), "source": "dashboard"})
    log_order({"at": utc_iso(), "date": today, "symbol": sym, "side": "buy", "qty": str(qty),
               "price": price, "status": "filled", "source": "dashboard"})
    apply_fill(sym, "buy", qty, price, today)
    positions.append({"symbol": sym, "qty": qty, "entry": price, "planned_entry": entry_ref,
                      "stop": stop, "target": target, "flat_by": idea.get("flat_by") or "15:55",
                      "date": today, "opened_at": utc_iso(), "max_loss": max_loss, "last": price,
                      "how": why})
    write(POSITIONS, positions)
    activity("Paper BUY %d %s at $%.2f (stop $%.2f, target $%.2f, max loss $%.2f) [%s]"
             % (qty, sym, price, stop, target, max_loss, why), "buy")
    return None


def paper_sell(pos, price, reason):
    today = ny_date()
    sym, qty = pos["symbol"], pos["qty"]
    log_order({"at": utc_iso(), "date": today, "symbol": sym, "side": "sell", "qty": str(qty),
               "kind": "exit", "price": price, "status": "filled", "source": "dashboard"})
    apply_fill(sym, "sell", qty, price, today)
    positions = [p for p in (read(POSITIONS, []) or []) if p["symbol"] != sym]
    write(POSITIONS, positions)
    pnl = round((price - pos["entry"]) * qty, 2)
    risk = pos["entry"] - pos["stop"]
    r = round((price - pos["entry"]) / risk, 2) if risk > 0 else None
    trades = read(TRADES, []) or []
    trades.append({"symbol": sym, "qty": qty, "entry": pos["entry"], "exit": price, "stop": pos["stop"],
                   "target": pos["target"], "opened_at": pos["opened_at"], "closed_at": utc_iso(),
                   "date": pos["date"], "reason": reason, "pnl": pnl, "r": r})
    write(TRADES, trades)
    activity("Paper SELL %d %s at $%.2f: %s. P&L $%+.2f%s"
             % (qty, sym, price, reason, pnl, (" (%+.2fR)" % r) if r is not None else ""),
             "win" if pnl > 0 else "loss")


def monitor(force=False):
    """Check open paper positions against their stop, target and flat-by time."""
    positions = read(POSITIONS, []) or []
    if not positions:
        return
    with LOCK:
        clk = clock()
        q = quotes([p["symbol"] for p in positions])
        now_m = ny_minutes(clk)
        today = clk.get("date")
        for pos in list(positions):
            quote = q.get(pos["symbol"])
            if not quote:
                continue
            px = round(float(quote["price"]), 2)
            pos["last"] = px
            flat = hhmm(pos.get("flat_by") or "15:55")  # trader-exit already moves it up on early closes
            if not clk.get("open") and pos["date"] == today and not force:
                continue
            if px <= pos["stop"]:
                paper_sell(pos, px, "stop hit (stop $%.2f)" % pos["stop"])
            elif px >= pos["target"]:
                paper_sell(pos, pos["target"], "target hit")
            elif pos["date"] != today and clk.get("open"):
                paper_sell(pos, px, "held past its day: closed at the next open")
            elif clk.get("open") and now_m is not None and flat is not None and now_m >= flat:
                paper_sell(pos, px, "flat by %s" % pos.get("flat_by"))
        left = read(POSITIONS, []) or []
        for p in left:
            for o in positions:
                if o["symbol"] == p["symbol"]:
                    p["last"] = o.get("last", p.get("last"))
        write(POSITIONS, left)
        STATUS["last_monitor"] = utc_iso()


# ------------------------------------------------------------------ the pipeline

def tm_against(verdict):
    v = (verdict or "").lower()
    return "toward the stop" in v


def evaluate(hit, s, cfg, prices_ok):
    """Run one scanner hit through entry, exit, time machine, news, budget and risk."""
    sym = hit["symbol"]
    d = os.path.join(WORK, sym)
    os.makedirs(d, exist_ok=True)
    idea = {"symbol": sym, "scan_price": hit.get("price"), "or_high": hit.get("or_high"),
            "or_low": hit.get("or_low"), "pct_over": hit.get("pct_over"), "status": "checking",
            "reasons": [], "news": [], "checked_at": utc_iso()}

    code, e, err = tool("trader-entry/entry.py", "check", sym, "--write", os.path.join(d, "entry.json"), "--json")
    if not e:
        idea.update(status="error", reasons=[err or "entry check failed"])
        return idea
    idea["entry_status"] = e.get("status")
    trig = e.get("trigger") or {}
    idea["trigger_time"] = trig.get("time")
    idea["minutes_since"] = e.get("minutes_since")
    idea["last"] = (e.get("forming") or {}).get("price") or (e.get("last") or {}).get("close")
    if e.get("status") != "triggered":
        idea.update(status="waiting", reasons=[e.get("reason") or e.get("status")])
        return idea
    fresh = (e.get("minutes_since") is not None and e["minutes_since"] <= s["fresh_minutes"])
    idea["fresh"] = fresh

    code, x, err = tool("trader-exit/exit.py", "plan", "--from", os.path.join(d, "entry.json"),
                        "--stop", s["stop"], "--target", s["target"],
                        "--write", os.path.join(d, "plan.json"), "--json")
    plan = read(os.path.join(d, "plan.json")) if code == 0 else None
    if not plan:
        idea.update(status="refused", reasons=[err or "the exit planner refused it"])
        return idea
    idea.update(entry=plan["entry"], stop=plan["stop"], target=plan["target"],
                risk_per_share=plan["risk_per_share"], flat_by=plan.get("flat_by"),
                stop_label=plan.get("stop_label"), target_label=plan.get("target_label"))

    code, tm, err = tool("trader-timemachine/timemachine.py", "run", "--plan", os.path.join(d, "plan.json"),
                         "--replays", s["replays"], "--until", plan.get("flat_by") or "15:55", "--json")
    if tm and tm.get("mc"):
        mc, hist = tm["mc"], tm.get("history") or {}
        idea["tm"] = {"target": mc.get("p_target"), "stop": mc.get("p_stop"), "neither": mc.get("p_neither"),
                      "avg_r": mc.get("avg_R"), "seed": mc.get("seed"), "replays": mc.get("replays"),
                      "verdict": tm.get("verdict"), "notes": tm.get("verdict_notes") or [],
                      "history": "fired %s of %s past sessions: %s target, %s stop, %s neither"
                                 % (hist.get("triggered"), hist.get("sessions"), hist.get("target"),
                                    hist.get("stop"), hist.get("close"))}
    else:
        idea["tm"] = {"verdict": err or "the time machine could not run"}

    code, nw, err = tool("trader-news/news.py", "--tickers", sym, "--hours", 24, "--all", "--dry-run",
                         "--no-moves", "--state", os.path.join(d, "news-state.json"), "--json", timeout=90)
    for it in ((nw or {}).get("items") or [])[:5]:
        idea["news"].append({"title": it.get("title"), "link": it.get("link"), "time": it.get("time"),
                             "tags": it.get("tags") or [], "kind": it.get("kind"),
                             "important": bool(it.get("important"))})

    if not prices_ok:
        idea.update(status="error", reasons=["no fresh price to size it with"])
        return idea
    code, sz, err = tool("trader-budget/size.py", "--plan", os.path.join(d, "plan.json"), "--prices", PRICES,
                         "--portfolio", PF, "--write", os.path.join(d, "orders.json"), "--json")
    sizing = (sz or {}).get("sizing") or {}
    if code != 0 or not (sz or {}).get("orders"):
        idea.update(status="refused", reasons=[sizing.get("refusal") or err or "the budget manager refused it"])
        return idea
    order = sz["orders"][0]
    idea.update(qty=int(sizing["qty"]), cost=sizing.get("cost"), max_loss=sizing.get("max_loss"),
                reward_risk=sizing.get("reward_risk"), bound_by=sizing.get("bound_by"),
                entry=sizing.get("entry", idea["entry"]), risk_per_share=sizing.get("risk_per_share"),
                order={k: v for k, v in order.items() if k not in ("why", "plan")})

    code, rk, err = tool("trader-risk/check.py", "--orders", os.path.join(d, "orders.json"), "--portfolio", PF,
                         "--prices", PRICES, "--state", LOG, "--out", os.path.join(d, "risk.json"), "--json")
    res = ((rk or {}).get("results") or [{}])[0]
    idea["risk"] = {"verdict": res.get("verdict"), "reasons": res.get("reasons") or [],
                    "warnings": res.get("warnings") or [], "info": res.get("info") or []}
    if res.get("verdict") != "PASS":
        idea.update(status="blocked", reasons=res.get("reasons") or [err or "the risk check blocked it"])
        return idea
    if not fresh:
        idea.update(status="late", reasons=["triggered at %s, %s minutes ago: a late entry is a different trade"
                                            % (idea.get("trigger_time"), idea.get("minutes_since"))])
        return idea
    if s.get("skip_if_odds_against") and tm_against(idea["tm"].get("verdict")):
        idea.update(status="odds against", reasons=["the Time Machine leans toward the stop"])
        return idea
    idea["status"] = "ready"
    return idea


def pipeline(auto_take=False, reason="manual"):
    if not LOCK.acquire(blocking=False):
        return "already running"
    try:
        ensure_config()
        s, cfg = settings(), config() or {}
        clk = clock()
        STATUS["busy"] = "Scanning charts"
        STATUS["error"] = ""
        args = ["orb", "--min-price", s["min_price"], "--out", os.path.join(TRADER, "watchlist.json"),
                "--json", "--quiet"]
        if s["universe"] == "watchlist":
            args += ["--symbols", s["watchlist"].replace(" ", "")]
        elif not os.path.exists(os.path.join(TRADER, "universe.txt")):
            STATUS["busy"] = "Building the stock list (once)"
            tool("trader-scanner/scan.py", "universe", timeout=300)
            STATUS["busy"] = "Scanning the whole market (a few minutes)"
        code, scan, err = tool("trader-scanner/scan.py", *args, timeout=900)
        STATUS["last_scan"] = utc_iso()
        if not scan:
            msg = err or "the scan found nothing"
            write(IDEAS, {"at": utc_iso(), "scan": msg, "ideas": []})
            activity("Scan: %s" % msg)
            return None

        budget = float(cfg.get("budget") or 1000)
        cap = budget * float(cfg.get("max_position_pct") or 75) / 100.0
        blocked = {b.upper() for b in cfg.get("blocked") or []}
        hits = [h for h in scan.get("hits", []) if h["symbol"].upper() not in blocked]
        affordable = [h for h in hits if (h.get("price") or 0) <= cap]
        skipped = len(hits) - len(affordable)
        cands = affordable[: int(s["max_candidates"])]
        counts = scan.get("counts") or {}
        summary = ("%d of %d charts broke their opening range" % (len(hits), counts.get("scanned") or 0)
                   + (" (%d priced over your $%.0f position cap)" % (skipped, cap) if skipped else ""))

        STATUS["busy"] = "Getting prices"
        q = quotes([h["symbol"] for h in cands])
        ideas = []
        for i, h in enumerate(cands):
            STATUS["busy"] = "Checking %s (%d of %d)" % (h["symbol"], i + 1, len(cands))
            try:
                idea = evaluate(h, s, cfg, h["symbol"] in q)
            except Exception as ex:  # one bad chart never stops the rest
                idea = {"symbol": h["symbol"], "status": "error", "reasons": [str(ex)]}
            if h["symbol"] in q:
                idea["last"] = q[h["symbol"]]["price"]
            ideas.append(idea)
            if auto_take and idea["status"] == "ready":
                px = round(float(q[h["symbol"]]["price"]), 2)
                problem = paper_buy(idea, px, "autopilot")
                idea["status"] = "taken" if not problem else "ready"
                if problem:
                    idea["reasons"] = [problem]
        write(IDEAS, {"at": utc_iso(), "scan": summary, "ideas": ideas, "as_of": scan.get("as_of")})
        ready = sum(1 for i in ideas if i["status"] in ("ready", "taken"))
        activity("Scan (%s): %s; %d checked, %d ready" % (reason, summary, len(ideas), ready))
        return None
    except Exception as ex:
        STATUS["error"] = str(ex)
        activity("Error: %s" % ex, "error")
        traceback.print_exc()
        return str(ex)
    finally:
        STATUS["busy"] = ""
        LOCK.release()


# ------------------------------------------------------------------ autopilot

def autopilot():
    last_scan = 0.0
    last_mon = 0.0
    while True:
        try:
            s = settings()
            now = time.time()
            if now - last_mon >= 60:
                last_mon = now
                clk = clock()
                if (read(POSITIONS, []) or []) and clk.get("open"):
                    monitor()
            if s["mode"] in ("ideas", "auto") and now - last_scan >= float(s["scan_every_min"]) * 60:
                clk = STATUS.get("clock") or clock()
                cfg = config() or {}
                m = ny_minutes(clk)
                start = hhmm("09:50")
                stop = hhmm(cfg.get("no_entries_after") or "15:30")
                if clk.get("open") and m is not None and start <= m <= stop:
                    last_scan = now
                    pipeline(auto_take=(s["mode"] == "auto"), reason="autopilot")
        except Exception as ex:
            STATUS["error"] = str(ex)
            traceback.print_exc()
        time.sleep(10)


# ------------------------------------------------------------------ web

def state():
    ensure_config()
    cfg = config() or {}
    pf = paper_portfolio()
    positions = read(POSITIONS, []) or []
    trades = read(TRADES, []) or []
    today = ny_date()
    value = sum(p["qty"] * (p.get("last") or p["entry"]) for p in positions)
    log = (read(LOG, {}) or {}).get("orders", [])
    entries_today = [r for r in log if r.get("date") == today and r.get("kind") == "entry"
                     and r.get("status") == "placed"]
    closed_today = [t for t in trades if t.get("date") == today]
    return {
        "status": STATUS, "settings": settings(), "config": cfg,
        "account": {"cash": pf["cash"], "positions_value": round(value, 2),
                    "equity": round(pf["cash"] + value, 2), "start": float(cfg.get("budget") or 0),
                    "trades_today": len(entries_today),
                    "risk_used_today": round(sum(float(r.get("max_loss") or 0) for r in entries_today), 2),
                    "pnl_today": round(sum(t["pnl"] for t in closed_today), 2),
                    "pnl_total": round(sum(t["pnl"] for t in trades), 2),
                    "wins": sum(1 for t in trades if t["pnl"] > 0), "losses": sum(1 for t in trades if t["pnl"] <= 0)},
        "positions": positions, "trades": trades[-50:][::-1],
        "ideas": read(IDEAS, {"ideas": []}), "activity": (read(ACTIVITY, []) or [])[-60:][::-1],
    }


def update_config(body):
    cfg = config() or {}
    for k, v in body.items():
        if k not in CONFIG_KEYS:
            continue
        typ = CONFIG_KEYS[k]
        if typ is list:
            v = [x.strip().upper() for x in (v.split(",") if isinstance(v, str) else v) if x.strip()]
        elif typ is str:
            v = str(v).strip()
            if k != "sec_user_agent" and hhmm(v) is None:
                raise ValueError("%s wants a time like 15:30" % k)
        else:
            v = typ(v)
            if v < 0:
                raise ValueError("%s cannot be negative" % k)
        cfg[k] = v
    cfg["paper"] = True
    write(CONFIG, cfg)
    activity("Rules updated")


def update_settings(body):
    s = settings()
    for k, v in body.items():
        if k not in DEFAULT_SETTINGS:
            continue
        d = DEFAULT_SETTINGS[k]
        if isinstance(d, bool):
            v = bool(v)
        elif isinstance(d, int):
            v = max(1, int(v))
        elif isinstance(d, float):
            v = float(v)
        else:
            v = str(v).strip()
        if k == "mode" and v not in ("off", "ideas", "auto"):
            raise ValueError("mode is off, ideas or auto")
        if k == "universe" and v not in ("watchlist", "market"):
            raise ValueError("universe is watchlist or market")
        s[k] = v
    if "mode" in body:
        activity({"off": "Autopilot off", "ideas": "Autopilot: find ideas (you click to paper trade)",
                  "auto": "Autopilot: find ideas AND paper trade them automatically (simulated)"}[s["mode"]])
    write(SETTINGS, s)


def find_idea(sym):
    for i in (read(IDEAS, {}) or {}).get("ideas", []):
        if i["symbol"] == sym:
            return i
    return None


KEY = None   # set with --lan: phones on your Wi-Fi need it, this computer does not


def lan_key():
    path = os.path.join(DASH, "key.txt")
    try:
        with open(path) as f:
            k = f.read().strip()
        if len(k) >= 12:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(12)
    os.makedirs(DASH, exist_ok=True)
    with open(path, "w") as f:
        f.write(k)
    return k


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # picks the Wi-Fi address; sends nothing
        return s.getsockname()[0]
    except OSError:
        return "YOUR-COMPUTER-IP"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def allowed(self):
        """With --lan, anything not from this computer needs the key (in the link once, then a cookie)."""
        if KEY is None or self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, val = part.strip().partition("=")
            if name == "dtkey" and hmac.compare_digest(val, KEY):
                return True
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if hmac.compare_digest((q.get("key") or [""])[0], KEY):
            self.send_response(303)
            self.send_header("Set-Cookie", "dtkey=%s; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict" % KEY)
            self.send_header("Location", "/")
            self.end_headers()
            return None
        self.send(403, b"<h3>Day Trader: open the link with the key that the server printed.</h3>",
                  "text/html; charset=utf-8")
        return False

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.allowed():
            return
        if urllib.parse.urlsplit(self.path).path == "/manifest.json":
            return self.send(200, {"name": "Day Trader", "short_name": "Trader", "start_url": "/",
                                   "display": "standalone", "background_color": "#0f1216",
                                   "theme_color": "#0f1216"}, "application/manifest+json")
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "app.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self.send(200, state())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return
        # Only this page may change things: block requests from other websites.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin.split("://", 1)[-1] != host:
            return self.send(403, {"error": "cross-site request refused"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "send JSON"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            return self.route(body)
        except ValueError as ex:
            return self.send(400, {"error": str(ex)})
        except Exception as ex:
            traceback.print_exc()
            return self.send(500, {"error": str(ex)})

    def route(self, body):
        p = self.path
        if p == "/api/scan":
            s = settings()
            threading.Thread(target=pipeline, kwargs={"auto_take": s["mode"] == "auto", "reason": "Scan now"},
                             daemon=True).start()
            return self.send(200, {"ok": True})
        if p == "/api/settings":
            update_settings(body)
            return self.send(200, {"ok": True})
        if p == "/api/config":
            update_config(body)
            return self.send(200, {"ok": True})
        if p == "/api/buy":
            sym = str(body.get("symbol", "")).upper()
            idea = find_idea(sym)
            if not idea or not idea.get("qty") or idea.get("risk", {}).get("verdict") != "PASS":
                raise ValueError("%s has no passed, sized plan to paper trade" % sym)
            with LOCK:
                q = quotes([sym])
                if sym not in q:
                    raise ValueError("no price for %s right now" % sym)
                problem = paper_buy(idea, round(float(q[sym]["price"]), 2), "you clicked")
            if problem:
                raise ValueError(problem)
            return self.send(200, {"ok": True})
        if p == "/api/sell":
            sym = str(body.get("symbol", "")).upper()
            pos = next((x for x in (read(POSITIONS, []) or []) if x["symbol"] == sym), None)
            if not pos:
                raise ValueError("no paper position in %s" % sym)
            with LOCK:
                q = quotes([sym])
                if sym not in q:
                    raise ValueError("no price for %s right now" % sym)
                paper_sell(pos, round(float(q[sym]["price"]), 2), "you closed it")
            return self.send(200, {"ok": True})
        if p == "/api/check":
            threading.Thread(target=monitor, daemon=True).start()
            return self.send(200, {"ok": True})
        if p == "/api/reset":
            if body.get("confirm") != "RESET":
                raise ValueError("type RESET to confirm")
            with LOCK:
                for f in (PF, LOG, POSITIONS, TRADES):
                    if os.path.exists(f):
                        os.remove(f)
                paper_portfolio()
                activity("Paper account reset to the budget")
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def main():
    global KEY
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lan = "--lan" in sys.argv[1:]
    port = int(os.environ.get("PORT") or (args[0] if args else 8050))
    for d in (DASH, PAPER, WORK):
        os.makedirs(d, exist_ok=True)
    ensure_config()
    paper_portfolio()
    threading.Thread(target=autopilot, daemon=True).start()
    if lan:
        KEY = lan_key()
    srv = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
    print("Day Trader dashboard: http://127.0.0.1:%d   (Ctrl+C to stop)" % port)
    if lan:
        print("\nOn your phone (same Wi-Fi), open:\n\n    http://%s:%d/?key=%s\n" % (lan_ip(), port, KEY))
        print("Keep that link private: it controls this dashboard. Delete trader/dashboard/key.txt for a new one.")
    print("Paper trading is simulated. Nothing here can place a real order.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
