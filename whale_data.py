"""
whale_data.py -- data layer for the Streamlit app (app.py).

Builds on whale_flow.py (Data API v2 plumbing: _get / _paginate / _walk_holders,
get_trades, get_wallet_pnl, leaderboard) but returns plain dicts and pandas
DataFrames instead of printing. Nothing in here imports Streamlit, so it can
be tested or reused from a script.

Two entry points the app uses:
  analyze_market(market, ...)  -> everything shown for ONE game
  scan_games(leagues, ...)     -> a league/sport-wide scan, one row per game

Gamma endpoints used (all public, no key):
  /public-search            find a game by text
  /events?slug=             find a game from a polymarket.com link
  /sports                   leagues -> tag ids
  /tags/{id}                tag id -> readable label
  /events/keyset            games in a league (tag_id), live or starting soon
"""

import datetime as dt
import heapq
import json
import re
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import whale_flow as wf

DEFAULT_TZ = "America/Edmonton"
POLY_EVENT_URL = "https://polymarket.com/event/"

# Market types that are "who wins the game". Soccer splits this into one
# Yes/No market per outcome (home / draw / away), all typed "moneyline".
MAIN_MARKET_TYPES = {"moneyline", "esports_match_result"}

# /sports lists tags like "1,100639,100350,102771": "1" (sports) and "100639"
# are on everything; the rest are a sport-category tag and a league tag, in no
# reliable order. These hints (inferred from the live /sports listing) pick
# out the category tag; labels are resolved from /tags/{id} when reachable.
GENERIC_TAGS = {"1", "100639"}
SPORT_TAG_HINTS = {
    "100350": "Soccer", "517": "Cricket", "100088": "Hockey", "102897": "Handball",
    "102883": "Volleyball", "102193": "Rugby", "102393": "Lacrosse", "103767": "Table tennis",
}
# League-code hints so US leagues group under a sport instead of each being
# their own "sport". Anything not matched falls back to the tag label.
CODE_SPORT = {
    "nba": "Basketball", "wnba": "Basketball", "cbb": "Basketball", "ncaab": "Basketball",
    "nfl": "Football", "cfb": "Football", "mlb": "Baseball", "nhl": "Hockey", "ufc": "MMA",
    "epl": "Soccer", "lal": "Soccer", "bun": "Soccer", "ucl": "Soccer", "mls": "Soccer",
    "lol": "Esports", "cs2": "Esports", "dota2": "Esports", "val": "Esports",
    "atp": "Tennis", "wta": "Tennis", "liv": "Golf", "pga": "Golf", "f1": "Racing",
}
# Readable league names (override the tag label, which can be generic or missing).
LEAGUE_NAMES = {
    "nba": "NBA", "wnba": "WNBA", "cbb": "NCAAM (men's college basketball)", "ncaab": "NCAAM (men's college basketball)",
    "cwbb": "NCAAW (women's college basketball)", "nfl": "NFL", "cfb": "NCAAF (college football)", "mlb": "MLB",
    "nhl": "NHL", "ufc": "UFC", "epl": "EPL", "lal": "La Liga", "bun": "Bundesliga", "ucl": "Champions League",
    "mls": "MLS", "atp": "ATP (men's tennis)", "wta": "WTA (women's tennis)", "lol": "League of Legends",
    "cs2": "Counter-Strike 2", "dota2": "Dota 2", "val": "Valorant",
}
PREFIX_SPORT = (("tennis", "Tennis"), ("itf", "Tennis"), ("challenger", "Tennis"), ("cwbb", "Basketball"), ("cric", "Cricket"), ("vb", "Volleyball"), ("hb", "Handball"),
                ("ru", "Rugby"), ("nascar", "Racing"), ("indycar", "Racing"))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _gamma(path: str, params: dict = None, timeout: int = 20, retries: int = 3):
    """GET a Gamma API route with retry on 429/503; raises with the body on errors."""
    for attempt in range(retries + 1):
        resp = requests.get(f"{wf.GAMMA_BASE}{path}", params=params, timeout=timeout)
        if resp.status_code in (429, 503) and attempt < retries:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} from Gamma {path}: {resp.text[:200]}")
        return resp.json()


def _as_list(val):
    """Gamma serves outcomes / outcomePrices / clobTokenIds as JSON-encoded strings."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return val or []


def _parse_dt(s):
    if not s:
        return None
    s = re.sub(r"([+-]\d{2})$", r"\1:00", str(s).strip().replace("Z", "+00:00"))
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def event_start(ev: dict):
    """Game start time. Deliberately NOT startDate (that's when the event was created)."""
    d = _parse_dt(ev.get("startTime"))
    if d:
        return d
    for m in ev.get("markets") or []:
        d = _parse_dt(m.get("gameStartTime"))
        if d:
            return d
    return None


def fmt_local(d, tz: str = DEFAULT_TZ) -> str:
    if d is None:
        return ""
    return d.astimezone(ZoneInfo(tz)).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def short_wallet(w) -> str:
    w = w or ""
    return f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w


def market_prices(market: dict):
    out = []
    for x in _as_list(market.get("outcomePrices")):
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            out.append(None)
    return out


def american_odds(p):
    """Price (0-1) -> American moneyline. 0.62 -> -163, 0.38 -> +163."""
    if p is None or p <= 0 or p >= 1:
        return None
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def line_text(p) -> str:
    if p is None:
        return "—"
    a = american_odds(p)
    return f"{p * 100:.0f}¢" + (f" ({a:+d})" if a is not None else "")


def annotate_market(m: dict, ev: dict) -> dict:
    """Copy a Gamma market dict and attach the parent event's context."""
    m = dict(m)
    m["_event_title"] = ev.get("title")
    m["_event_slug"] = ev.get("slug")
    m["_event_live"] = bool(ev.get("live"))
    m["_event_ended"] = bool(ev.get("ended"))
    m["_event_start"] = ev.get("startTime") or m.get("gameStartTime")
    return m


def market_label(m: dict) -> str:
    title = m.get("_event_title") or ""
    q = m.get("question") or ""
    head = title if (not q or q == title) else (f"{title} — {q}" if title else q)
    typ = m.get("sportsMarketType")
    return ("[LIVE] " if m.get("_event_live") else "") + head + (f"  ({typ})" if typ else "")


# ---------------------------------------------------------------------------
# finding a game
# ---------------------------------------------------------------------------

def search_games(query: str, only_live: bool = False, limit_per_type: int = 10):
    """
    Search by text ("Cavaliers Pelicans") or paste a polymarket.com/event/<slug>
    link. Returns a flat list of annotated market dicts: live games first, then
    moneyline markets, otherwise in the search's relevance order.
    """
    q = query.strip()
    link = re.search(r"polymarket\.com/(?:[a-z\-]+/)?event/([^/?#\s]+)", q)
    if link:
        events = _gamma("/events", {"slug": link.group(1)})
        events = events if isinstance(events, list) else [events]
    else:
        data = _gamma("/public-search", {
            "q": q, "limit_per_type": limit_per_type, "events_status": "active",
            "search_tags": "false", "search_profiles": "false",
        })
        events = data.get("events") or []
    if only_live:
        events = [e for e in events if e.get("live")]

    out = []
    for ev in events:
        for m in ev.get("markets") or []:
            if m.get("conditionId") and not m.get("closed"):
                out.append(annotate_market(m, ev))
    out.sort(key=lambda m: (not m.get("_event_live"), m.get("sportsMarketType") not in MAIN_MARKET_TYPES))
    return out


# ---------------------------------------------------------------------------
# the core table: wallets vs cash vs line, per side
# ---------------------------------------------------------------------------

def side_table(market: dict, min_shares: float = 1.0, max_pages: int = 30):
    """
    Walk the market's holders (up to max_pages pages of 100 per side) and build
    one dict per side with: wallets, cash in, value now, line, avg entry,
    holders' P&L, and the percentage columns (share of wallets, share of cash,
    line normalised to 100%, and cash% minus line% in points).

    Returns (sides, groups, truncated). `groups` is the raw per-token holder
    lists (reused for the top-holders table). `truncated` means the page cap
    was hit: cash is nearly complete, wallet counts are lower bounds.
    """
    cond = market.get("conditionId")
    team_map = wf.build_token_team_map(market)
    tokens = [str(t) for t in _as_list(market.get("clobTokenIds"))]
    outcomes = _as_list(market.get("outcomes"))
    gprices = market_prices(market)

    groups, truncated = wf._walk_holders(cond, limit=100, include_pnl=True,
                                         min_balance=min_shares, max_pages=max_pages)
    sides = []
    for grp in groups:
        tid = str(grp["token_id"])
        holders = grp["holders"]
        idx = tokens.index(tid) if tid in tokens else None

        label = team_map.get(tid)
        if not label and holders:
            oi = holders[0].get("outcome_index")
            if isinstance(oi, int) and 0 <= oi < len(outcomes):
                label = outcomes[oi]
        label = label or tid
        if label in ("Yes", "No"):
            label = f"{label}: {market.get('groupItemTitle') or market.get('question') or 'this market'}"

        price = next((float(h["current_price"]) for h in holders if h.get("current_price") is not None), None)
        if price is None and idx is not None and idx < len(gprices):
            price = gprices[idx]

        costed = [h for h in holders if h.get("entry_cost_usdc") is not None]
        cash = sum(float(h["entry_cost_usdc"]) for h in costed)
        shares_costed = sum(float(h.get("amount") or 0) for h in costed)
        value = sum(float(h["current_value"]) for h in holders if h.get("current_value") is not None)
        pnl = sum(float(h["current_value"]) - float(h["entry_cost_usdc"])
                  for h in costed if h.get("current_value") is not None)
        costs_desc = sorted((float(h["entry_cost_usdc"]) for h in costed), reverse=True)
        sides.append({
            "order": idx if idx is not None else 99, "token_id": tid, "Side": label, "price": price,
            "wallets": len({h.get("proxy_wallet") for h in holders}),
            "shares": sum(float(h.get("amount") or 0) for h in holders),
            "cash": cash, "value": value, "pnl": pnl,
            "avg_entry": cash / shares_costed if shares_costed else None,
            "median": statistics.median(costs_desc) if costs_desc else 0.0,
            "top10_share": 100 * sum(costs_desc[:10]) / cash if cash else 0.0,
            "missing": len(holders) - len(costed),
        })

    tot_w = sum(s["wallets"] for s in sides)
    tot_c = sum(s["cash"] for s in sides)
    prices = [s["price"] for s in sides]
    psum = sum(prices) if prices and all(p is not None for p in prices) else None
    for s in sides:
        s["wallet_pct"] = 100 * s["wallets"] / tot_w if tot_w else None
        s["cash_pct"] = 100 * s["cash"] / tot_c if tot_c else None
        s["line_pct"] = 100 * s["price"] / psum if (s["price"] is not None and psum) else None
        s["gap"] = (s["cash_pct"] - s["line_pct"]) if (s["cash_pct"] is not None and s["line_pct"] is not None) else None
    sides.sort(key=lambda s: s["order"])
    return sides, groups, truncated


def sides_dataframe(sides: list) -> pd.DataFrame:
    return pd.DataFrame([{
        "Side": s["Side"],
        "Line": line_text(s["price"]),
        "Line %": s["line_pct"],
        "Wallets": s["wallets"],
        "Wallets %": s["wallet_pct"],
        "Cash in $": s["cash"],
        "Cash %": s["cash_pct"],
        "Cash − Line (pts)": s["gap"],
        "Value now $": s["value"],
        "Avg entry ¢": s["avg_entry"] * 100 if s["avg_entry"] is not None else None,
        "Holders' P&L $": s["pnl"],
        "Median $/wallet": s["median"],
        "Top-10 share %": s["top10_share"],
    } for s in sides])


def verdict_text(sides: list) -> str:
    """Plain-English read of wallets vs cash vs line. Descriptive, not a pick."""
    if len(sides) < 2 or not any(s["wallets"] for s in sides) or not any(s["cash"] for s in sides):
        return ""
    mw = max(sides, key=lambda s: s["wallets"])
    mc = max(sides, key=lambda s: s["cash"])
    if mw is mc:
        text = (f"More wallets ({mw['wallet_pct']:.0f}%) and more cash ({mc['cash_pct']:.0f}%) "
                f"are both on {mc['Side']}.")
    else:
        text = (f"More wallets are on {mw['Side']} ({mw['wallet_pct']:.0f}% of wallets), but more cash is on "
                f"{mc['Side']} ({mc['cash_pct']:.0f}% of cash): the crowd and the money disagree.")
    if mc.get("gap") is not None:
        text += (f" The line prices {mc['Side']} at {mc['line_pct']:.0f}%, so the cash on it sits "
                 f"{mc['gap']:+.0f} pts versus the line.")
    return text


# ---------------------------------------------------------------------------
# holders, form, track record
# ---------------------------------------------------------------------------

def hot_form_tags() -> dict:
    """wallet -> 'D#12 W#40' for wallets on Polymarket's SPORTS leaderboard (day/week/month)."""
    tags = defaultdict(list)
    for period, letter in (("day", "D"), ("week", "W"), ("month", "M")):
        try:
            board = wf.get_leaderboard("sports", period)
        except Exception:
            continue
        for row in board:
            tags[(row.get("user_id") or "").lower()].append(f"{letter}#{row.get('rank')}")
    return {w: " ".join(v) for w, v in tags.items()}


def top_holders_dataframe(groups: list, sides: list, top_n: int = 10, hot: dict = None) -> pd.DataFrame:
    label_by_token = {s["token_id"]: s["Side"] for s in sides}
    order = {s["token_id"]: s["order"] for s in sides}
    hot = hot or {}
    rows = []
    for grp in sorted(groups, key=lambda g: order.get(str(g["token_id"]), 99)):
        tid = str(grp["token_id"])
        top = sorted(grp["holders"], key=lambda h: float(h.get("entry_cost_usdc") or 0), reverse=True)[:top_n]
        for h in top:
            w = (h.get("proxy_wallet") or "")
            rows.append({
                "Side": label_by_token.get(tid, tid),
                "Holder": h.get("name") or h.get("pseudonym") or "?",
                "Wallet": short_wallet(w),
                "Shares": h.get("amount"),
                "Cash in $": h.get("entry_cost_usdc"),
                "Value now $": h.get("current_value"),
                "Avg entry ¢": h["avg_price"] * 100 if h.get("avg_price") is not None else None,
                "P&L this market $": h.get("total_pnl"),
                "Recent form": hot.get(w.lower(), ""),
            })
    return pd.DataFrame(rows)


def _pnl_status(w: str):
    pnl = wf.get_wallet_pnl_cached(w)
    if pnl is None:
        return "unknown", None, 0
    if pnl.get("n_positions", 0) == 0:
        return "new", 0.0, 0
    return ("profitable" if pnl["realizedPnl"] > 0 else "losing"), pnl["realizedPnl"], pnl["n_positions"]


def sharp_check(groups: list, sides: list, top_n: int = 10, workers: int = 8):
    """
    Track record of each side's biggest holders: recent realized PnL over each
    wallet's last ~150 positions (see wf.get_wallet_pnl). Returns
    (per-holder DataFrame, per-side summary DataFrame). The summary's key number
    is the share of the checked cash that comes from recently profitable wallets.
    """
    label_by_token = {s["token_id"]: s["Side"] for s in sides}
    picks = []
    for grp in groups:
        top = sorted(grp["holders"], key=lambda h: float(h.get("entry_cost_usdc") or 0), reverse=True)[:top_n]
        picks += [(label_by_token.get(str(grp["token_id"]), str(grp["token_id"])), h) for h in top]
    wallets = list({h.get("proxy_wallet") for _, h in picks if h.get("proxy_wallet")})
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(wf.get_wallet_pnl_cached, wallets))

    rows = []
    for side, h in picks:
        status, realized, n = _pnl_status(h.get("proxy_wallet"))
        rows.append({"Side": side, "Holder": h.get("name") or h.get("pseudonym") or "?",
                     "Wallet": short_wallet(h.get("proxy_wallet")),
                     "Cash in $": float(h.get("entry_cost_usdc") or 0),
                     "Recent realized PnL $": realized, "Positions checked": n, "Status": status})
    df = pd.DataFrame(rows)
    summary = []
    for side, g in df.groupby("Side", sort=False):
        known = g[g["Status"].isin(["profitable", "losing"])]
        known_cash = known["Cash in $"].sum()
        prof_cash = known.loc[known["Status"] == "profitable", "Cash in $"].sum()
        summary.append({
            "Side": side, "Holders checked": len(g),
            "Profitable": int((g["Status"] == "profitable").sum()),
            "Losing": int((g["Status"] == "losing").sum()),
            "New / unknown": int(g["Status"].isin(["new", "unknown"]).sum()),
            "% of checked cash from profitable wallets": 100 * prof_cash / known_cash if known_cash else None,
        })
    return df, pd.DataFrame(summary)


# ---------------------------------------------------------------------------
# trade flow (taker vs maker), maker whales, biggest fills
# ---------------------------------------------------------------------------

def _trade_label(t: dict, team_map: dict, market: dict) -> str:
    raw = t.get("outcome") or team_map.get(t.get("token_id")) or t.get("token_id") or "unknown"
    if raw in ("Yes", "No"):
        return f"{raw}: {market.get('groupItemTitle') or market.get('question') or 'this market'}"
    return raw


def flow_dataframes(market: dict, all_trades: list, taker_trades: list, top_trades: int = 15,
                    tz: str = DEFAULT_TZ):
    """
    Per side: aggressive (taker) buying vs selling, resting (maker) volume, and
    distinct aggressive buyers. Plus the biggest aggressive fills. A fill is
    counted once on its taker row, so a big number here means somebody paid up
    to get it done, not that somebody got filled passively.
    """
    team_map = wf.build_token_team_map(market)
    taker_keys = {(t.get("transaction_hash"), t.get("proxy_wallet")) for t in taker_trades}
    agg = defaultdict(lambda: {"tb": 0.0, "ts": 0.0, "maker": 0.0, "buyers": set()})
    fills = []
    for t in all_trades:
        usd = float(t.get("size", 0)) * float(t.get("price", 0))
        label = _trade_label(t, team_map, market)
        a = agg[label]
        if (t.get("transaction_hash"), t.get("proxy_wallet")) in taker_keys:
            if t.get("side") == "BUY":
                a["tb"] += usd
                a["buyers"].add(t.get("proxy_wallet"))
            else:
                a["ts"] += usd
            fills.append((usd, t, label))
        else:
            a["maker"] += usd

    flow_rows = []
    for label, a in agg.items():
        taker = a["tb"] + a["ts"]
        total = taker + a["maker"]
        flow_rows.append({
            "Side": label, "Taker buys $": a["tb"], "Taker sells $": a["ts"],
            "Net aggressor $": a["tb"] - a["ts"], "Maker $": a["maker"],
            "Taker %": 100 * taker / total if total else None, "Aggressive buyers": len(a["buyers"]),
        })

    big_rows = []
    for usd, t, label in heapq.nlargest(top_trades, fills, key=lambda x: x[0]):
        ts = dt.datetime.fromtimestamp(int(t.get("timestamp") or 0), tz=dt.timezone.utc)
        big_rows.append({
            "Time": fmt_local(ts, tz), "Side": label, "Action": t.get("side"),
            "Price ¢": float(t.get("price", 0)) * 100, "Size $": usd,
            "Holder": t.get("name") or t.get("pseudonym") or "?", "Wallet": short_wallet(t.get("proxy_wallet")),
        })
    return pd.DataFrame(flow_rows), pd.DataFrame(big_rows)


def maker_whales_dataframe(market: dict, all_trades: list, taker_trades: list, top_n: int = 3) -> pd.DataFrame:
    """Biggest resting-order wallets: who absorbed the most aggressive flow, and how."""
    team_map = wf.build_token_team_map(market)
    taker_keys = {(t.get("transaction_hash"), t.get("proxy_wallet")) for t in taker_trades}
    by_tx = defaultdict(list)
    for t in all_trades:
        by_tx[t.get("transaction_hash")].append(t)

    vol = defaultdict(float)
    cps = defaultdict(set)
    side_usd = defaultdict(lambda: defaultdict(float))
    names = {}
    for tx, rows in by_tx.items():
        takers = [r for r in rows if (tx, r.get("proxy_wallet")) in taker_keys]
        if not takers:
            continue
        for m in (r for r in rows if (tx, r.get("proxy_wallet")) not in taker_keys):
            usd = float(m.get("size", 0)) * float(m.get("price", 0))
            w = m["proxy_wallet"]
            vol[w] += usd
            side_usd[w][_trade_label(m, team_map, market)] += usd
            names.setdefault(w, m.get("name") or m.get("pseudonym") or "?")
            cps[w].update(t["proxy_wallet"] for t in takers)

    total = sum(vol.values())
    rows = []
    for rank, (w, v) in enumerate(sorted(vol.items(), key=lambda kv: kv[1], reverse=True)[:top_n], start=1):
        sides = side_usd[w]
        top_side = max(sides, key=sides.get)
        status, realized, n = _pnl_status(w)
        rows.append({
            "#": rank, "Whale": names[w], "Wallet": short_wallet(w),
            "Resting $ absorbed": v, "Share of resting volume %": 100 * v / total if total else None,
            "Counterparties": len(cps[w]), "Concentrated on": top_side,
            "% on that side": 100 * sides[top_side] / sum(sides.values()),
            "Recent record": status, "Recent realized PnL $": realized,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# one game, end to end
# ---------------------------------------------------------------------------

def analyze_market(market: dict, min_shares: float = 1.0, max_pages: int = 30, include_flow: bool = True,
                   sharp: bool = False, top_holders: int = 10, tz: str = DEFAULT_TZ) -> dict:
    cond = market.get("conditionId")
    warnings = []

    sides, groups, truncated = side_table(market, min_shares=min_shares, max_pages=max_pages)
    if not sides:
        warnings.append("No holders came back for this market (too new, or the market has no open positions).")
    if truncated:
        warnings.append(f"Holder walk stopped at {max_pages} pages per side. Cash totals are close to complete, but "
                        f"wallet counts are lower bounds (the smallest holders are the ones cut). Raise the holder depth in the sidebar.")
    if any(s["missing"] for s in sides):
        warnings.append("Some holder rows have no cost data (the API reports null, not zero), so their cash is excluded.")

    res = {
        "market": market, "sides": sides, "sides_df": sides_dataframe(sides), "verdict": verdict_text(sides),
        "truncated": truncated, "warnings": warnings,
        "top_holders_df": top_holders_dataframe(groups, sides, top_n=top_holders, hot=hot_form_tags()),
        "flow_df": None, "big_trades_df": None, "whales_df": None, "sharp_df": None, "sharp_summary_df": None,
        "updated": dt.datetime.now(dt.timezone.utc),
    }

    if include_flow:
        all_trades = wf.get_trades(cond, taker_only=False)
        taker_trades = wf.get_trades(cond, taker_only=True)
        if len(all_trades) >= 20000:
            warnings.append("Trade history hit the 20,000-row ceiling, so older trades are missing from the flow tables.")
        if all_trades and len(all_trades) == len(taker_trades):
            warnings.append("Maker rows didn't come back (taker-only filter not honored), so the maker/whale tables can't be trusted.")
        if any(t.get("condition_id") and t["condition_id"].lower() != cond.lower() for t in all_trades):
            warnings.append("Some trade rows belong to a different market. Flow tables are unreliable until that's resolved.")
        res["flow_df"], res["big_trades_df"] = flow_dataframes(market, all_trades, taker_trades, tz=tz)
        res["whales_df"] = maker_whales_dataframe(market, all_trades, taker_trades)

    if sharp and groups:
        res["sharp_df"], res["sharp_summary_df"] = sharp_check(groups, sides, top_n=top_holders)
    return res


# ---------------------------------------------------------------------------
# leagues and the cross-league scan
# ---------------------------------------------------------------------------

_TAG_CACHE: dict = {}


def _tag_label(tid: str):
    if tid in _TAG_CACHE:
        return _TAG_CACHE[tid]
    try:
        label = _gamma(f"/tags/{tid}").get("label")
    except Exception:
        label = None
    _TAG_CACHE[tid] = label
    return label


def resolve_tag_labels(ids, workers: int = 12) -> dict:
    ids = list(ids)
    with ThreadPoolExecutor(workers) as ex:
        labels = list(ex.map(_tag_label, ids))
    return dict(zip(ids, labels))


def _sport_for(code: str, sport_tag: str, labels: dict):
    code = (code or "").lower()
    if code in CODE_SPORT:
        return CODE_SPORT[code]
    for prefix, sport in PREFIX_SPORT:
        if code.startswith(prefix):
            return sport
    return SPORT_TAG_HINTS.get(sport_tag) or labels.get(sport_tag) or f"Other ({sport_tag})"


def list_leagues() -> list:
    """Every league Polymarket lists, with a readable sport group and the tag id used to fetch its games."""
    rows = _gamma("/sports")
    leagues = []
    for r in rows:
        tags = [t.strip() for t in str(r.get("tags") or "").split(",") if t.strip()]
        specific = [t for t in tags if t not in GENERIC_TAGS]
        if not specific and tags and r.get("sport"):
            specific = [tags[-1]]   # league only carries generic tags: still scan it
        if not specific or not r.get("sport"):
            continue
        sport_tag = next((t for t in specific if t in SPORT_TAG_HINTS), specific[0])
        rest = [t for t in specific if t != sport_tag]
        leagues.append({"code": r["sport"], "series": r.get("series"), "sport_tag": sport_tag,
                        "league_tag": rest[-1] if rest else sport_tag})
    labels = resolve_tag_labels({l["sport_tag"] for l in leagues} | {l["league_tag"] for l in leagues})
    for l in leagues:
        l["sport"] = _sport_for(l["code"], l["sport_tag"], labels)
        l["league"] = LEAGUE_NAMES.get(l["code"].lower()) or labels.get(l["league_tag"]) or l["code"].upper()
    leagues.sort(key=lambda l: (l["sport"], l["league"]))
    return leagues


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_league_events(league: dict, hours_ahead: float = 24, hours_back: float = 8, max_pages: int = 3):
    """
    Games in one league that are live now or start within the window. Two
    queries (live=true, and a start-time window) merged by event id, then
    filtered client-side too, so a filter the server ignores can't leak
    finished or far-future games into the scan. Returns (events, errors).
    """
    now = dt.datetime.now(dt.timezone.utc)
    lo, hi = now - dt.timedelta(hours=hours_back), now + dt.timedelta(hours=hours_ahead)
    base = {"tag_id": league["league_tag"], "closed": "false", "limit": 100}
    seen, errors = {}, []

    def run(extra: dict):
        cursor = None
        for _ in range(max_pages):
            params = {**base, **extra}
            if cursor:
                params["after_cursor"] = cursor
            try:
                body = _gamma("/events/keyset", params)
            except Exception as e:
                errors.append(f"{league['league']}: {e}")
                return
            for ev in body.get("events") or []:
                seen[str(ev.get("id"))] = ev
            cursor = body.get("next_cursor")
            if not cursor:
                return

    run({"live": "true"})
    run({"start_time_min": _iso(lo), "start_time_max": _iso(hi)})
    raw_found = len(seen)
    if not seen and league.get("series"):
        # tag returned nothing: some leagues (tennis tours) are keyed by series instead
        base.pop("tag_id", None)
        base["series_id"] = league["series"]
        run({"live": "true"})
        run({"start_time_min": _iso(lo), "start_time_max": _iso(hi)})
        raw_found = len(seen)

    keep = []
    for ev in seen.values():
        if ev.get("closed") or ev.get("ended"):
            continue
        start = event_start(ev)
        if not ev.get("live") and (start is None or not (lo <= start <= hi)):
            continue
        ev["_league"] = league
        keep.append(ev)
    if not keep:
        errors.append(f"{league['league']} ({league['sport']}): 0 games in window "
                      f"(API returned {raw_found}; tag {league['league_tag']}, series {league.get('series')})")
    return keep, errors


def pick_markets(ev: dict) -> list:
    """The 'who wins' market(s) of an event: moneyline (up to 3 for soccer), else the biggest two-sided non-Yes/No market."""
    ms = [m for m in ev.get("markets") or [] if m.get("conditionId") and not m.get("closed")]
    main = [m for m in ms if m.get("sportsMarketType") in MAIN_MARKET_TYPES]
    if main:
        return main[:3]
    named = [m for m in ms if len(_as_list(m.get("outcomes"))) == 2
             and not set(_as_list(m.get("outcomes"))) & {"Yes", "No", "Over", "Under"}]
    return sorted(named, key=lambda m: m.get("volumeNum") or 0, reverse=True)[:1]


def _scan_row(ev: dict, m: dict, sides: list, truncated: bool, tz: str) -> dict:
    league = ev["_league"]
    start = event_start(ev)
    tot_w = sum(s["wallets"] for s in sides)
    tot_c = sum(s["cash"] for s in sides)
    money = max(sides, key=lambda s: s["cash"])
    crowd = max(sides, key=lambda s: s["wallets"])
    return {
        "Sport": league["sport"], "League": league["league"], "Game": ev.get("title"),
        "Market": m.get("groupItemTitle") or m.get("sportsMarketType") or "",
        "Status": "LIVE" if ev.get("live") else "Upcoming", "Starts": fmt_local(start, tz),
        "Money side": money["Side"], "Line %": money["line_pct"], "Wallets %": money["wallet_pct"],
        "Cash %": money["cash_pct"], "Cash − Line (pts)": money["gap"],
        "Total cash $": tot_c, "Value now $": sum(s["value"] for s in sides), "Wallets": tot_w,
        "Crowd side": crowd["Side"], "Crowd ≠ money": crowd is not money, "Partial count": truncated,
        "_start": start, "_live": bool(ev.get("live")),
    }


def scan_games(leagues: list, hours_ahead: float = 24, hours_back: float = 8, depth_pages: int = 1,
               min_shares: float = 1.0, max_games: int = 40, workers: int = 8, tz: str = DEFAULT_TZ,
               progress=None):
    """
    Scan the given leagues for live / soon-starting games and summarise each
    game's moneyline market (wallets vs cash vs line). Two phases: find games
    in every league, then pull holders for the soonest/live `max_games`.

    depth_pages is holders-per-side in pages of 100: 1 is quick (top 100 per
    side; wallet counts are lower bounds), higher is slower and more complete.

    Returns (DataFrame, items, report). items[i] = {"event", "market"} aligned
    with the DataFrame's index so a row can be opened in the analyzer; report
    has counts and error strings for the diagnostics panel.
    """
    progress = progress or (lambda done, total, text: None)
    report = {"leagues": len(leagues), "events": 0, "markets": 0, "analyzed": 0, "errors": []}

    events = []
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(fetch_league_events, l, hours_ahead, hours_back) for l in leagues]
        for i, f in enumerate(as_completed(futs), start=1):
            try:
                evs, errs = f.result()
                events += evs
                report["errors"] += errs
            except Exception as e:
                report["errors"].append(str(e))
            progress(i, len(futs), "Finding games")
    report["events"] = len(events)

    far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    events.sort(key=lambda e: (not e.get("live"), event_start(e) or far))
    tasks = [(ev, m) for ev in events for m in pick_markets(ev)]
    report["markets"] = len(tasks)
    tasks = tasks[:max_games]

    def work(task):
        ev, m = task
        sides, _, truncated = side_table(m, min_shares=min_shares, max_pages=depth_pages)
        if not sides:
            return None
        return _scan_row(ev, m, sides, truncated, tz), {"event": ev, "market": annotate_market(m, ev)}

    rows, items = [], []
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(work, t): t for t in tasks}
        for i, f in enumerate(as_completed(futs), start=1):
            try:
                out = f.result()
                if out:
                    rows.append(out[0])
                    items.append(out[1])
            except Exception as e:
                report["errors"].append(f"{futs[f][0].get('title')}: {e}")
            progress(i, len(futs), "Reading holders")
    report["analyzed"] = len(rows)

    df = pd.DataFrame(rows)
    if not df.empty:
        order = df.sort_values(["_live", "Total cash $"], ascending=[False, False]).index
        df = df.loc[order].reset_index(drop=True)
        items = [items[i] for i in order]
    return df, items, report
