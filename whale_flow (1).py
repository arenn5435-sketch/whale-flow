"""
whale_flow.py

Given a search query, finds a Polymarket market/game, pulls its full trade
history, and classifies each large holder's activity as AGGRESSOR (taker,
crossed the spread) vs PASSIVE (maker, posted a resting order that got hit).

Data sources:
  - Gamma API   (gamma-api.polymarket.com)  -> market/event metadata, conditionId, clobTokenIds
  - Data API v2 (data-api.polymarket.com/v2) -> /v2/trades (with taker_only filter),
                                               /v2/holders, /v2/positions, /v2/leaderboard

Core idea:
  1. Pull ALL trades for the market's condition_id.
  2. Pull TAKER-ONLY trades for the same market.
  3. For every (transaction_hash, proxy_wallet) pair present in the full set,
     check whether it's also present in the taker-only set.
       - present in both  -> that wallet was the TAKER (aggressor) on that trade
       - present only in full set -> that wallet was a MAKER (passive) on that trade
  4. Aggregate by wallet: $ volume as taker vs $ volume as maker, net direction.
  5. Cross-reference against /holders to flag which wallets are actually whales
     (large current position), vs. small fish who happen to trade a lot.

NOTE: Polymarket's public API surface changes over time. If takerOnly, /holders,
or field names below don't match what you get back, print a raw response and
check https://docs.polymarket.com -- this script is a starting point, not a
guaranteed-stable client.

CHANGELOG (this version -- Data API v1 -> v2 migration):
  - FIX: Polymarket retired Data API v1 (the old /trades, /holders,
    /positions, /closed-positions, /v1/leaderboard routes). Everything now
    goes through https://data-api.polymarket.com/v2 -- see
    https://docs.polymarket.com/migrate/data-api-v1-to-v2.md
  - CHANGE: v2 responses are wrapped as {"data": [...], "pagination": {...}}
    and use snake_case (proxy_wallet, transaction_hash, condition_id,
    token_id) instead of camelCase (proxyWallet, transactionHash,
    conditionId, asset). All field reads below were updated to match.
  - CHANGE: pagination is cursor-only. `offset` is rejected with a 400, so
    all pulls go through _paginate()/_walk_holders(), which follow
    pagination.next_cursor. The old "offset ceiling" is now a row ceiling.
  - CHANGE: `market=` is now `condition=`; `takerOnly` is `taker_only`.
    /closed-positions is folded into /v2/positions?status=CLOSED, and
    positions can be sorted server-side by recency for open AND closed.
    /v2/positions has no cashPnl, so get_wallet_pnl() now reports
    unrealizedPnl instead (nothing in this script consumed cashPnl).
  - CHANGE: the leaderboard is /v2/leaderboard (lowercase category and
    time_period, rows keyed by user_id). Finite windows (day/week/month) are
    now marked-to-market equity change, not purely realized PnL.
  - ADD: wallets_vs_cash() -- for each side of the game, how many distinct
    wallets hold it vs. how much cash is on it. Printed by default after the
    whale summary (skip with --no-side-totals).
  - ADD: HTTP retry on 429/503, honoring Retry-After.

CHANGELOG (previous version):
  - FIX: /trades defaults takerOnly to true when the param is omitted -- it
    does NOT default to "give me everything." Both get_trades() and
    get_wallet_trades() now send takerOnly explicitly as true/false on every
    call, so the "all trades" pull actually includes maker rows.
  - REMOVED: the entire points-based final_verdict() scoring system (net
    exposure / profitable-taker% / hot-money / $-per-holder weighted into a
    0-10 score), along with compute_holder_concentration(),
    get_current_outcome_prices(), and prediction logging. That scoring
    system is gone -- see score_backtest.py's history if you ever want to
    revisit it.
  - REWORK: market_structure() now reports the top 3 maker whales (not just
    #1), each with which side they're concentrated on and an account status
    (PROFITABLE / LOSING / NEW) based on realized PnL, via the new
    classify_account_status() helper.
  - CHANGE: account-status PnL is no longer lifetime. get_wallet_pnl() and
    get_wallet_closed_positions() now cap at the wallet's most recent 150
    positions (open + closed combined, closed side sorted server-side via
    sortBy=TIMESTAMP/sortDirection=DESC) instead of paginating through full
    history. Pass limit=N to either function to change the cap.
  - ADD: pagination-ceiling warnings. get_trades() and get_wallet_trades()
    now print a WARNING and note the returned list is truncated if they hit
    the 20,000-offset safety ceiling before the API actually runs out of
    rows, instead of silently returning a partial picture.
  - ADD: size-vs-performance correlation section (new, see below) --
    wallet_size_vs_pnl(), absorption_vs_pnl(), and aggressor_pct_vs_size()
    correlate different notions of "size" against different notions of
    "result" across ALL wallets in a market (not just the top N), using a
    dependency-free Spearman rank correlation plus a quartile-bucketed
    table. Wired into main() via --correlate.
"""

import requests
import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
DATA_V2 = DATA_BASE + "/v2"


# ---------------------------------------------------------------------------
# 0. Data API v2 plumbing: retrying GET + cursor pagination
# ---------------------------------------------------------------------------

def _get(path: str, params: dict = None, timeout: int = 20, retries: int = 4):
    """
    GET a Data API v2 route (path like "/trades" or "/holders") and return the
    parsed JSON body. Retries on 429 (rate limited) and 503 (server busy),
    sleeping for the Retry-After header when present. Other HTTP errors raise
    with the API's own error message included, which is far more useful than a
    bare "400 Bad Request" (v2 returns a JSON body naming the bad parameter).
    """
    url = f"{DATA_V2}{path}"
    for attempt in range(retries + 1):
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code in (429, 503) and attempt < retries:
            try:
                delay = float(resp.headers.get("Retry-After", 2 ** attempt))
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(min(max(delay, 0.5), 30))
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} from {url}: {resp.text[:300]}")
        return resp.json()


def _paginate(path: str, params: dict, max_rows: int = None):
    """
    Walk a cursor-paginated v2 route to the end (or until max_rows).
    Returns (rows, truncated). `truncated` is True only if max_rows was hit
    while the API still had more pages -- i.e. rows is a partial picture.

    The same params are re-sent on every page on purpose: on the feed routes
    (/trades, /activity) the cursor carries only its seek position, so
    changing or dropping a filter mid-walk would silently re-anchor the walk.
    """
    rows = []
    cursor = None
    while True:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        body = _get(path, page_params)
        rows.extend(body.get("data") or [])
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if max_rows is not None and len(rows) >= max_rows:
            return rows[:max_rows], bool(cursor) or len(rows) > max_rows
        if not cursor:
            return rows, False


def _num(x):
    """v2 uses null for 'unavailable'; treat that as 0 for summing."""
    return float(x) if x is not None else 0.0


# ---------------------------------------------------------------------------
# 1. Market resolution: turn a free-text query into a condition_id + token ids
# ---------------------------------------------------------------------------

def find_market(query: str, only_live: bool = False, limit_per_type: int = 10):
    """
    Use Polymarket's dedicated search endpoint (GET /public-search) instead of
    hand-filtering a small page of /markets results. Returns a flat list of
    market dicts (each with conditionId, clobTokenIds, etc.), pulled out of
    the matching events, most-relevant first.

    only_live=True restricts to events currently in progress (event['live'] == True),
    which is what you want for an active game rather than a future/past one.
    """
    resp = requests.get(
        f"{GAMMA_BASE}/public-search",
        params={
            "q": query,
            "limit_per_type": limit_per_type,
            "events_status": "active",   # active markets only, not archived/resolved
            "search_tags": "false",
            "search_profiles": "false",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    events = data.get("events") or []
    if only_live:
        events = [e for e in events if e.get("live")]

    markets = []
    for ev in events:
        for m in ev.get("markets", []):
            m["_event_title"] = ev.get("title")
            m["_event_live"] = ev.get("live")
            markets.append(m)

    return markets


def resolve_username(username: str):
    """
    Turn a Polymarket username (from a profile URL like polymarket.com/@name)
    into an actual proxy wallet address, using the same /public-search
    endpoint but with search_profiles enabled instead of events.
    """
    resp = requests.get(
        f"{GAMMA_BASE}/public-search",
        params={
            "q": username,
            "limit_per_type": 10,
            "search_tags": "false",
            "search_profiles": "true",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    profiles = data.get("profiles") or []
    for p in profiles:
        if (p.get("name") or "").lower() == username.lower() or (p.get("pseudonym") or "").lower() == username.lower():
            return p.get("proxyWallet") or p.get("wallet") or p.get("address")
    # fall back to first result if no exact match
    if profiles:
        return profiles[0].get("proxyWallet") or profiles[0].get("wallet") or profiles[0].get("address")
    return None


# ---------------------------------------------------------------------------
# 2. Trade pulls
# ---------------------------------------------------------------------------

def get_trades(condition_id: str, taker_only: bool = False, limit: int = 1000, row_ceiling: int = 20000):
    """
    Pull trades for a market via GET /v2/trades. taker_only=True restricts to
    aggressor-side rows.

    IMPORTANT: the API defaults `taker_only` to true when the param is absent
    from the request -- there is no "send nothing, get everything" behavior.
    We send taker_only explicitly as "true"/"false" on every call, otherwise
    the taker_only=False path silently gets taker-only data too, and no
    maker rows ever show up. (Must be the lowercase string: requests would
    otherwise serialize a Python bool as "True".)

    v2 pages by cursor (no offset). Stops after `row_ceiling` rows as a sane
    guard against runaway pulls on a market with an enormous trade history
    (newest trades come first, so it's the OLDEST rows that get cut). If that
    ceiling is hit before the API runs out of rows, this PRINTS A WARNING --
    every count, total, and ranking computed from the returned list should be
    treated as a possibly-truncated partial picture, not the full market.
    """
    params = {
        "condition": condition_id,
        "limit": min(limit, 1000),
        "taker_only": "true" if taker_only else "false",
    }
    all_trades, truncated = _paginate("/trades", params, max_rows=row_ceiling)
    if truncated:
        print(f"WARNING: get_trades({condition_id}, taker_only={taker_only}) hit the "
              f"row ceiling ({row_ceiling}) with more rows still "
              f"available -- returning {len(all_trades)} trades, which is TRUNCATED, "
              f"not the full trade history for this market.")
    return all_trades


def get_leaderboard(category: str = "sports", time_period: str = "week", limit: int = 50):
    """
    Polymarket's own trader leaderboard (GET /v2/leaderboard), scoped by
    category and time window.
    category: "overall" or a Gamma market category such as "sports"
              (case-insensitive here; v2 wants lowercase)
    time_period: day, week, month, all (case-insensitive here)
    Returns [{rank, user_id, user_name, pnl, volume, ...}, ...] -- this is a
    ranked top-N list (top `limit`, default 50 -- kept small on purpose so
    "on the leaderboard" still means "genuinely near the top"), not an
    exhaustive lookup, so a wallet not appearing here just means they're not
    currently in the top ranks for that window, not that they have zero
    activity.

    NOTE: in v2, day/week/month boards are marked-to-market equity change
    (realized + unrealized moves, net of deposits/withdrawals); only "all" is
    the realized-only lifetime ledger. `volume` is in shares, not USD.
    """
    body = _get(
        "/leaderboard",
        params={
            "category": category.lower(),
            "time_period": time_period.lower(),
            "limit": min(limit, 1000),
        },
    )
    return body.get("data") or []


def recent_form_breakdown(market: dict, top_n: int = 10):
    """
    Cross-references this market's actual top holders (from /holders) against
    Polymarket's own SPORTS leaderboard across DAY/WEEK/MONTH windows. Answers:
    is this wallet's size backed by recent hot form, or just a large position
    with no recent signal either way? Lifetime realizedPnl (used elsewhere in
    this script) can't tell these apart -- a wallet up big from one trade a
    year ago looks identical to one that's been consistently sharp lately.
    """
    condition_id = market.get("conditionId")
    team_map = build_token_team_map(market)

    try:
        holders_data = get_holders(condition_id)
    except Exception as e:
        print(f"\ncould not fetch holders for recent-form check: {e}")
        return

    periods = ["DAY", "WEEK", "MONTH"]
    leaderboards = {}
    for p in periods:
        try:
            lb = get_leaderboard(category="sports", time_period=p)
            leaderboards[p] = {(row.get("user_id") or "").lower(): row for row in lb}
        except Exception as e:
            print(f"could not fetch {p} SPORTS leaderboard: {e}")
            leaderboards[p] = {}

    print("\n--- Recent form check (Polymarket SPORTS leaderboard: day/week/month) ---")
    if not isinstance(holders_data, list):
        print("Unexpected /holders shape, skipping.")
        return

    for entry in holders_data:
        token_id = entry.get("token_id")
        team_name = team_map.get(token_id, token_id)
        holders = entry.get("holders", [])[:top_n]

        print(f"\n{team_name}:")
        for h in holders:
            wallet = (h.get("proxy_wallet") or "").lower()
            display_name = h.get("name") or h.get("pseudonym") or "?"
            amount = h.get("amount")

            tags = []
            for p in periods:
                row = leaderboards[p].get(wallet)
                if row:
                    pnl = row.get("pnl", 0)
                    tags.append(f"{p} #{row.get('rank')} (${pnl:,.0f})")
            tag_str = " | ".join(tags) if tags else "not on recent SPORTS leaderboard"

            amount_str = f"{amount:,.2f}" if isinstance(amount, (int, float)) else str(amount)
            print(f"  {display_name:<20} {wallet[:12]:<14} {amount_str:>12} shares -> {tag_str}")

    print("\nRead this as: a wallet tagged for DAY and/or WEEK is trending hot RIGHT NOW in sports "
          "specifically -- a much stronger 'this person is sharp today' signal than lifetime PnL. "
          "A wallet with a big position but no tag here just isn't in the current top ranks; it "
          "doesn't necessarily mean they're unprofitable, just not currently trending.")


def _walk_holders(condition_id: str, limit: int = 100, include_pnl: bool = False,
                  min_balance: float = None, max_pages: int = 1):
    """
    Walk GET /v2/holders. Returns (groups, truncated) where groups is a list
    of {"token_id": ..., "holders": [...]} -- one group per outcome token,
    holders sorted by amount descending.

    `limit` is rows PER OUTCOME TOKEN per page (default 100; max 1000, or 100
    when include_pnl=True). Pages advance every token group together, so
    groups are merged by token_id rather than by array position. `truncated`
    is True if max_pages was hit while more pages remained -- since holders
    are ranked by size, what's cut is always the smallest holders.

    include_pnl=True adds per-side position economics to every holder row
    (avg_price, entry_cost_usdc, current_price, current_value, realized_pnl,
    unrealized_pnl, total_pnl) and switches `amount` to the per-side GROSS
    balance, so a wallet holding both outcomes shows up under both tokens.
    """
    cap = 100 if include_pnl else 1000
    merged = {}
    cursor = None
    pages = 0
    truncated = False
    while True:
        params = {"condition": condition_id, "limit": min(limit, cap)}
        if include_pnl:
            params["include_pnl"] = "true"
        if min_balance is not None:
            params["min_balance"] = min_balance
        if cursor:
            params["cursor"] = cursor
        body = _get("/holders", params)
        for grp in body.get("data") or []:
            merged.setdefault(grp.get("token_id"), []).extend(grp.get("holders") or [])
        pages += 1
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
        if pages >= max_pages:
            truncated = True
            break
    return [{"token_id": t, "holders": h} for t, h in merged.items()], truncated


def get_holders(condition_id: str, limit: int = 100):
    """
    Top current holders for a market (first page only: top `limit` per
    outcome token). IMPORTANT: this takes the CONDITION ID, not a token id --
    and returns holders for BOTH outcome tokens in one response, as a list of
    {"token_id": ..., "holders": [...]}. For a full walk (every holder, with
    cost/value economics) use _walk_holders() -- see wallets_vs_cash().
    """
    groups, _ = _walk_holders(condition_id, limit=limit, max_pages=1)
    return groups


def current_holdings(market: dict, top_n: int = 10):
    """
    Snapshot of CURRENT position size per team -- this is the end-state
    (e.g. "129,100 CLE shares"), independent of how that position was built.
    Complements classify_flow(), which shows the trading behavior (aggressor
    vs passive) that led to these holdings.

    Shape (Data API v2, see --dump-holders-raw to inspect a live response):
    get_holders() returns a list, one entry per outcome token, each like:
      {"token_id": "<token_id>", "holders": [{"proxy_wallet":..., "name":...,
                                               "pseudonym":..., "amount":...}, ...]}
    """
    condition_id = market.get("conditionId")
    team_map = build_token_team_map(market)  # token_id -> team name, for labeling

    try:
        data = get_holders(condition_id)
    except Exception as e:
        print(f"\ncould not fetch holders: {e}")
        return team_map

    print("\n--- Current holdings snapshot (top holders) ---")

    if not isinstance(data, list):
        print("Unexpected shape (not a list) -- printing raw so we can fix it:")
        print(data)
        return team_map

    for entry in data:
        token_id = entry.get("token_id")
        team_name = team_map.get(token_id, token_id)
        holders = entry.get("holders", [])

        print(f"\n{team_name}:")
        for h in holders[:top_n]:
            wallet = h.get("proxy_wallet")
            display_name = h.get("name") or h.get("pseudonym") or "?"
            amount = h.get("amount")
            amount_str = f"{amount:,.2f}" if isinstance(amount, (int, float)) else str(amount)
            print(f"  {display_name:<20} {str(wallet)[:12]:<14} {amount_str} shares")

    return team_map


# ---------------------------------------------------------------------------
# 3. Classification: aggressor vs passive per wallet
# ---------------------------------------------------------------------------

@dataclass
class WalletFlow:
    wallet: str
    taker_buy_usd: float = 0.0
    taker_sell_usd: float = 0.0
    maker_buy_usd: float = 0.0
    maker_sell_usd: float = 0.0
    trade_count: int = 0
    # team/outcome label -> signed net $ (buys - sells): tells you WHICH side
    # of the game this wallet is actually on
    team_net_usd: dict = field(default_factory=lambda: defaultdict(float))
    # team label -> $ volume where this wallet was the taker/maker specifically
    # on that team's token (lets you see "aggressive on Cavs, passive on Pelicans")
    team_taker_usd: dict = field(default_factory=lambda: defaultdict(float))
    team_maker_usd: dict = field(default_factory=lambda: defaultdict(float))

    @property
    def taker_usd(self):
        return self.taker_buy_usd + self.taker_sell_usd

    @property
    def maker_usd(self):
        return self.maker_buy_usd + self.maker_sell_usd

    @property
    def aggressor_pct(self):
        total = self.taker_usd + self.maker_usd
        return 100 * self.taker_usd / total if total else 0.0

    @property
    def net_direction_usd(self):
        # positive = net aggressive/passive buying, negative = net selling
        buys = self.taker_buy_usd + self.maker_buy_usd
        sells = self.taker_sell_usd + self.maker_sell_usd
        return buys - sells


def build_token_team_map(market: dict):
    """
    Map each outcome token id to its human-readable team/outcome name, using
    the market's own 'outcomes' and 'clobTokenIds' fields. Gamma returns both
    as JSON-encoded strings (e.g. '["Cleveland Cavaliers","New Orleans Pelicans"]'),
    so this handles that encoding.
    """
    def _load(field_name):
        val = market.get(field_name)
        if val is None:
            return []
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return []
        return val  # already a list

    outcomes = _load("outcomes")
    token_ids = _load("clobTokenIds")
    return dict(zip(token_ids, outcomes))


def classify_flow(condition_id: str, token_team_map: dict = None, all_trades=None, taker_trades=None, market: dict = None):
    """
    IMPORTANT: pass all_trades/taker_trades in from a single shared snapshot
    (fetched once per run) rather than letting this fetch its own -- on a
    LIVE game, trades arrive continuously, so two independent fetches taken
    even a few seconds apart will disagree with each other, producing
    inconsistent results across different functions in the same run.

    Some markets are structured as team-vs-team (outcome = actual team name),
    others as a single Yes/No question (outcome = literally "Yes"/"No"). For
    the latter, pass `market` so labels can include the question text instead
    of a bare, meaningless "Yes"/"No".
    """
    if all_trades is None:
        all_trades = get_trades(condition_id, taker_only=False)
    if taker_trades is None:
        taker_trades = get_trades(condition_id, taker_only=True)

    # Build a lookup of (transaction_hash, proxy_wallet) that were confirmed TAKER rows
    taker_keys = {(t.get("transaction_hash"), t.get("proxy_wallet")) for t in taker_trades}

    flows: dict = defaultdict(lambda: WalletFlow(wallet=""))
    token_team_map = token_team_map or {}

    for t in all_trades:
        wallet = t.get("proxy_wallet")
        side = t.get("side")  # BUY or SELL
        usd = float(t.get("size", 0)) * float(t.get("price", 0))
        key = (t.get("transaction_hash"), wallet)

        raw_outcome = t.get("outcome") or token_team_map.get(t.get("token_id")) or t.get("token_id") or "unknown"
        if raw_outcome in ("Yes", "No") and market:
            team_label = f"{raw_outcome} ({market.get('question', 'this market')})"
        else:
            team_label = raw_outcome

        wf = flows[wallet]
        wf.wallet = wallet
        wf.trade_count += 1

        signed_usd = usd if side == "BUY" else -usd
        wf.team_net_usd[team_label] += signed_usd

        is_taker = key in taker_keys
        if is_taker:
            wf.team_taker_usd[team_label] += usd
            if side == "BUY":
                wf.taker_buy_usd += usd
            else:
                wf.taker_sell_usd += usd
        else:
            wf.team_maker_usd[team_label] += usd
            if side == "BUY":
                wf.maker_buy_usd += usd
            else:
                wf.maker_sell_usd += usd

    return flows


# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------

def report(market: dict, min_usd: float = 500.0, top_n: int = 15, all_trades=None, taker_trades=None):
    """market is a resolved market dict (from find_market), not a search query --
    this keeps a single search/pick decision consistent across the whole run.
    Pass all_trades/taker_trades from a shared snapshot -- see main()."""
    condition_id = market.get("conditionId")
    print(f"Market: {market.get('question')}")
    print(f"Condition ID: {condition_id}")
    print(f"Slug: {market.get('slug')}\n")

    flows = classify_flow(condition_id, token_team_map=build_token_team_map(market), market=market,
                           all_trades=all_trades, taker_trades=taker_trades)

    ranked = sorted(
        (wf for wf in flows.values() if (wf.taker_usd + wf.maker_usd) >= min_usd),
        key=lambda wf: wf.taker_usd + wf.maker_usd,
        reverse=True,
    )[:top_n]

    print(f"{'Wallet':<12} {'TotalUSD':>10} {'Aggressor%':>10} {'NetDir($)':>10} {'Trades':>7}  Team (net $)")
    for wf in ranked:
        total = wf.taker_usd + wf.maker_usd
        team_str = ", ".join(f"{team}: {v:+.0f}" for team, v in wf.team_net_usd.items())
        print(f"{wf.wallet[:10]:<12} {total:>10.0f} {wf.aggressor_pct:>9.1f}% {wf.net_direction_usd:>10.0f} {wf.trade_count:>7}  {team_str}")

    print("\nInterpretation:")
    print("  High Aggressor% + positive NetDir  -> whale is LIFTING THE ASK, buying aggressively (conviction)")
    print("  Low  Aggressor% + positive NetDir   -> whale is SITTING ON BIDS, getting filled passively (patient accumulation)")
    print("  High Aggressor% + negative NetDir   -> whale is HITTING BIDS, selling/betting against aggressively")
    return ranked


# ---------------------------------------------------------------------------
# 5. Market structure: is one whale absorbing everyone, or many whales duking it out?
# ---------------------------------------------------------------------------

def classify_account_status(wallet: str, profitable_threshold: float = 0.0):
    """
    Classifies a wallet's RECENT trading history as PROFITABLE, LOSING, or
    NEW/UNKNOWN, based on realized PnL summed across its most recent
    positions (currently-open via /positions, plus settled via
    /closed-positions, capped at get_wallet_pnl()'s `limit`, default 150 --
    NOT lifetime). Returns a short human-readable string.

    "NEW" specifically means both endpoints came back empty for this wallet
    -- i.e. it has no position record at all, as opposed to a wallet that
    has traded plenty and simply hasn't turned a profit yet.
    """
    pnl = get_wallet_pnl_cached(wallet)
    if pnl is None:
        return "UNKNOWN (could not fetch PnL data)"
    if pnl.get("n_positions", 0) == 0:
        return "NEW (no historical position record)"
    detail = f"most recent {pnl['n_positions']} positions ({pnl.get('n_open', '?')} open, {pnl.get('n_closed', '?')} closed)"
    if pnl["realizedPnl"] > profitable_threshold:
        return f"PROFITABLE (realized PnL: ${pnl['realizedPnl']:,.0f} across {detail})"
    else:
        return f"LOSING (realized PnL: ${pnl['realizedPnl']:,.0f} across {detail})"


def market_structure(condition_id: str, all_trades=None, taker_trades=None, market: dict = None, top_n: int = 3):
    """
    For each maker wallet in this market, count how many DISTINCT taker
    wallets traded against them, and how much $ volume that represents.
    High distinct-counterparty count + high volume = a single whale is
    quietly absorbing flow from many different smaller traders (classic
    "smart money sitting on the bid/ask" pattern).

    Reports the top `top_n` maker wallets (by $ absorbed), each with: which
    side they're concentrated on, and their account status (PROFITABLE /
    LOSING / NEW) based on realized PnL over their most recent positions
    (see get_wallet_pnl(), default cap 150) -- so a "dominant whale"
    finding immediately comes with whether that whale has a recent track
    record worth trusting.

    Pass all_trades/taker_trades from a shared snapshot -- see main().
    """
    if all_trades is None:
        all_trades = get_trades(condition_id, taker_only=False)
    if taker_trades is None:
        taker_trades = get_trades(condition_id, taker_only=True)
    taker_keys = {(t.get("transaction_hash"), t.get("proxy_wallet")) for t in taker_trades}

    team_map = build_token_team_map(market) if market else {}

    by_tx = defaultdict(list)
    for t in all_trades:
        by_tx[t.get("transaction_hash")].append(t)

    maker_counterparties = defaultdict(set)
    maker_volume = defaultdict(float)
    maker_names = {}
    # wallet -> team_label -> $ absorbed on that side specifically. Tallied
    # once per maker row (not once per counterparty pair like maker_volume
    # above), so this is a clean, non-duplicated per-team total.
    maker_team_usd = defaultdict(lambda: defaultdict(float))

    for tx_hash, rows in by_tx.items():
        takers = [r for r in rows if (tx_hash, r.get("proxy_wallet")) in taker_keys]
        makers = [r for r in rows if (tx_hash, r.get("proxy_wallet")) not in taker_keys]
        for m in makers:
            usd = float(m.get("size", 0)) * float(m.get("price", 0))
            raw_outcome = m.get("outcome") or team_map.get(m.get("token_id")) or m.get("token_id") or "unknown"
            if raw_outcome in ("Yes", "No") and market:
                team_label = f"{raw_outcome} ({market.get('question', 'this market')})"
            else:
                team_label = raw_outcome
            wallet = m["proxy_wallet"]
            maker_team_usd[wallet][team_label] += usd
            if wallet not in maker_names:
                maker_names[wallet] = m.get("name") or m.get("pseudonym") or None
            for t in takers:
                maker_counterparties[wallet].add(t["proxy_wallet"])
                maker_volume[wallet] += usd

    structure = sorted(maker_volume.items(), key=lambda kv: kv[1], reverse=True)
    total_vol = sum(maker_volume.values())

    print(f"\n--- Top {top_n} maker whales (liquidity providers) ---")
    for rank, (wallet, vol) in enumerate(structure[:top_n], start=1):
        n_cp = len(maker_counterparties[wallet])
        share = 100 * vol / total_vol if total_vol else 0
        display_name = maker_names.get(wallet)
        label = f"{display_name} ({wallet[:10]}...)" if display_name else wallet

        print(f"\n#{rank}: {label}")
        print(f"  Absorbed {share:.0f}% of all resting-side volume from {n_cp} distinct counterparties.")

        team_breakdown = maker_team_usd.get(wallet, {})
        if team_breakdown:
            team_total = sum(team_breakdown.values())
            dominant_team = max(team_breakdown, key=team_breakdown.get)
            dominant_usd = team_breakdown[dominant_team]
            dominant_pct = 100 * dominant_usd / team_total if team_total else 0
            print(f"  -> That whale's absorption is concentrated on {dominant_team} "
                  f"(${dominant_usd:,.0f}, {dominant_pct:.0f}% of their own maker volume).")

        status = classify_account_status(wallet)
        print(f"  -> Account status: {status}")

    return structure, maker_counterparties, maker_team_usd


# ---------------------------------------------------------------------------
# 6. Per-wallet history across ALL markets (not just this game)
# ---------------------------------------------------------------------------

def get_wallet_trades(wallet: str, taker_only: bool = False, limit: int = 1000, row_ceiling: int = 20000):
    """Same explicit-taker_only fix as get_trades() -- must be sent on every call.
    Same row-ceiling warning too -- see get_trades().

    On the per-wallet shape, v2 floors an omitted `start` to THREE YEARS back;
    start=1 asks for full history, which is what wallet_profile() wants
    ("every market they've ever traded")."""
    params = {
        "user": wallet,
        "limit": min(limit, 1000),
        "taker_only": "true" if taker_only else "false",
        "start": 1,
    }
    all_trades, truncated = _paginate("/trades", params, max_rows=row_ceiling)
    if truncated:
        print(f"WARNING: get_wallet_trades({wallet}, taker_only={taker_only}) hit the "
              f"row ceiling ({row_ceiling}) with more rows still "
              f"available -- returning {len(all_trades)} trades, which is TRUNCATED, "
              f"not this wallet's full trade history.")
    return all_trades


def get_wallet_closed_positions(wallet: str, limit: int = 150):
    """
    MOST RECENT `limit` settled/closed positions for a wallet, via
    GET /v2/positions?status=CLOSED. (In v1 this was a separate
    /closed-positions route; v2 folds the whole position lifecycle into
    /v2/positions behind a `status` filter.) status=OPEN, the default, only
    returns positions still held; a wallet that has closed out most of its
    history will show almost none of its recent PnL if you only query that.
    This pulls the exited side so realized PnL can be summed across BOTH.

    Sorted server-side by recency (sort_by=TIMESTAMP, i.e. last_event_at,
    sort_direction=DESC) and stops as soon as it has `limit` rows -- see
    get_wallet_pnl() for how this combines with open positions.
    """
    if limit <= 0:
        return []
    params = {
        "user": wallet,
        "status": "CLOSED",
        "sort_by": "TIMESTAMP",
        "sort_direction": "DESC",
        "limit": min(limit, 1000),
    }
    rows, _ = _paginate("/positions", params, max_rows=limit)
    return rows


def get_wallet_pnl(wallet: str, limit: int = 150):
    """
    Realized/current PnL context -- useful to see whether an aggressive
    history has actually been a profitable one.

    NOT lifetime. Sums realized_pnl across the wallet's most recent `limit`
    positions (open + closed combined) rather than its entire history --
    `limit` is a practical cap (default 150), not a hard API ceiling.

    Open positions come from /v2/positions (status=OPEN, which also includes
    settled-but-unredeemed ones), sorted server-side by recency and capped at
    `limit`. Whatever budget remains after that is filled from the CLOSED
    side via get_wallet_closed_positions(). If a wallet has >= limit open
    positions alone, no closed positions are pulled at all.

    Returns unrealizedPnl (sum of unrealized_pnl over the open positions
    included) alongside realizedPnl. v1's cashPnl has no v2 equivalent;
    unrealizedPnl is the same kind of number (mark-to-market vs. cost basis).
    """
    open_positions, _ = _paginate(
        "/positions",
        {
            "user": wallet,
            "status": "OPEN",
            "sort_by": "TIMESTAMP",
            "sort_direction": "DESC",
            "filter_type": "TOKENS",
            "filter_amount": 1,        # ignore dust holdings (< 1 share), same as v1's sizeThreshold=1
            "limit": min(limit, 1000),
        },
        max_rows=limit,
    )

    remaining = max(0, limit - len(open_positions))
    closed_positions = get_wallet_closed_positions(wallet, limit=remaining) if remaining > 0 else []

    open_realized = sum(_num(p.get("realized_pnl")) for p in open_positions)
    closed_realized = sum(_num(p.get("realized_pnl")) for p in closed_positions)
    realized = open_realized + closed_realized
    unrealized = sum(_num(p.get("unrealized_pnl")) for p in open_positions)
    n_positions = len(open_positions) + len(closed_positions)
    return {
        "realizedPnl": realized,
        "unrealizedPnl": unrealized,
        "n_positions": n_positions,
        "n_open": len(open_positions),
        "n_closed": len(closed_positions),
        "limit": limit,
    }


def wallet_profile(wallet: str):
    """Long-run aggressor vs. passive profile for a wallet, across every market
    they've ever traded -- not just the current game."""
    all_trades = get_wallet_trades(wallet, taker_only=False)
    taker_trades = get_wallet_trades(wallet, taker_only=True)
    taker_tx = {t.get("transaction_hash") for t in taker_trades}

    wf = WalletFlow(wallet=wallet)
    for t in all_trades:
        usd = float(t.get("size", 0)) * float(t.get("price", 0))
        side = t.get("side")
        wf.trade_count += 1
        if t.get("transaction_hash") in taker_tx:
            if side == "BUY":
                wf.taker_buy_usd += usd
            else:
                wf.taker_sell_usd += usd
        else:
            if side == "BUY":
                wf.maker_buy_usd += usd
            else:
                wf.maker_sell_usd += usd

    pnl = None
    try:
        pnl = get_wallet_pnl(wallet)
    except Exception:
        pass

    print(f"\n--- Wallet history: {wallet} ---")
    print(f"Total trades: {wf.trade_count}")
    print(f"Aggressor (taker) %: {wf.aggressor_pct:.1f}%")
    print(f"Taker volume: ${wf.taker_usd:,.0f}  |  Maker volume: ${wf.maker_usd:,.0f}")
    if pnl:
        print(f"Realized PnL (most recent {pnl['n_positions']} open/closed positions): "
              f"${pnl['realizedPnl']:,.0f}")
    return wf, pnl


# ---------------------------------------------------------------------------
# 7. Smart-money segmentation
# ---------------------------------------------------------------------------

_pnl_cache: dict = {}  # wallet -> pnl dict, so repeat runs/wallets don't re-hit the API


def get_wallet_pnl_cached(wallet: str):
    if wallet in _pnl_cache:
        return _pnl_cache[wallet]
    try:
        pnl = get_wallet_pnl(wallet)
    except Exception:
        pnl = None
    _pnl_cache[wallet] = pnl
    return pnl


def smart_money_breakdown(condition_id: str, market: dict, min_usd: float = 250.0, profitable_threshold: float = 0.0,
                           all_trades=None, taker_trades=None):
    """
    For wallets with meaningful size in this market, classify them as
    'profitable' or 'losing' based on realizedPnl over their most recent
    positions (see get_wallet_pnl(), default cap 150), then break down
    taker $ volume and maker $ volume by that classification -- AND by team.
    Pass all_trades/taker_trades from a shared snapshot -- see main().
    """
    team_map = build_token_team_map(market)
    flows = classify_flow(condition_id, token_team_map=team_map, market=market,
                           all_trades=all_trades, taker_trades=taker_trades)
    relevant = {w: f for w, f in flows.items() if (f.taker_usd + f.maker_usd) >= min_usd}

    print(f"\n--- Smart-money breakdown ({len(relevant)} wallets over ${min_usd:.0f}, "
          f"out of {len(flows)} total wallets in this market) ---")

    buckets = {
        ("taker", "profitable"): 0.0, ("taker", "losing"): 0.0, ("taker", "unknown"): 0.0,
        ("maker", "profitable"): 0.0, ("maker", "losing"): 0.0, ("maker", "unknown"): 0.0,
    }
    team_buckets = defaultdict(float)
    wallet_labels = {}

    for wallet, f in relevant.items():
        pnl = get_wallet_pnl_cached(wallet)
        if pnl is None:
            label = "unknown"
        else:
            label = "profitable" if pnl["realizedPnl"] > profitable_threshold else "losing"
        wallet_labels[wallet] = (label, pnl)

        buckets[("taker", label)] += f.taker_usd
        buckets[("maker", label)] += f.maker_usd

        for team, usd in f.team_taker_usd.items():
            team_buckets[(team, "taker", label)] += usd
        for team, usd in f.team_maker_usd.items():
            team_buckets[(team, "maker", label)] += usd

    taker_total = sum(v for (role, _), v in buckets.items() if role == "taker")
    maker_total = sum(v for (role, _), v in buckets.items() if role == "maker")

    def pct(v, total):
        return 100 * v / total if total else 0.0

    print(f"\nTAKER side (${taker_total:,.0f} total, both teams combined):")
    print(f"  from historically PROFITABLE wallets: {pct(buckets[('taker','profitable')], taker_total):.0f}%")
    print(f"  from historically LOSING wallets:      {pct(buckets[('taker','losing')], taker_total):.0f}%")
    print(f"  unknown/no PnL data:                    {pct(buckets[('taker','unknown')], taker_total):.0f}%")

    print(f"\nMAKER side (${maker_total:,.0f} total, both teams combined):")
    print(f"  from historically PROFITABLE wallets: {pct(buckets[('maker','profitable')], maker_total):.0f}%")
    print(f"  from historically LOSING wallets:      {pct(buckets[('maker','losing')], maker_total):.0f}%")
    print(f"  unknown/no PnL data:                    {pct(buckets[('maker','unknown')], maker_total):.0f}%")

    all_teams = {team for (team, role, label) in team_buckets}
    print(f"\n--- Same breakdown, split by team ---")
    for team in all_teams:
        t_profit = team_buckets.get((team, "taker", "profitable"), 0.0)
        t_lose = team_buckets.get((team, "taker", "losing"), 0.0)
        t_unk = team_buckets.get((team, "taker", "unknown"), 0.0)
        t_total = t_profit + t_lose + t_unk

        m_profit = team_buckets.get((team, "maker", "profitable"), 0.0)
        m_lose = team_buckets.get((team, "maker", "losing"), 0.0)
        m_unk = team_buckets.get((team, "maker", "unknown"), 0.0)
        m_total = m_profit + m_lose + m_unk

        print(f"\n{team}:")
        print(f"  TAKER (${t_total:,.0f}): {pct(t_profit,t_total):.0f}% profitable, "
              f"{pct(t_lose,t_total):.0f}% losing, {pct(t_unk,t_total):.0f}% unknown")
        print(f"  MAKER (${m_total:,.0f}): {pct(m_profit,m_total):.0f}% profitable, "
              f"{pct(m_lose,m_total):.0f}% losing, {pct(m_unk,m_total):.0f}% unknown")

    taker_profitable_pct = pct(buckets[("taker", "profitable")], taker_total)
    maker_profitable_pct = pct(buckets[("maker", "profitable")], maker_total)

    print("\nInterpretation:")
    if taker_profitable_pct < 30 and maker_profitable_pct > 60:
        print("  => Aggressive flow here is mostly historically-losing money paying to cross,")
        print("     while the resting/maker side is dominated by historically-profitable wallets.")
        print("     This is the clearest 'smart money quietly farming dumb aggression' pattern.")
    elif taker_profitable_pct > 60:
        print("  => The aggressive/taker side is itself dominated by historically-profitable wallets.")
        print("     This looks more like sharp money moving with urgency/conviction, not noise.")
    elif abs(taker_profitable_pct - maker_profitable_pct) < 20:
        print("  => No strong split -- both sides have a similar mix of profitable and losing wallets.")
        print("     Weaker signal; this game's flow may not tell you much on its own.")

    return buckets, wallet_labels


# ---------------------------------------------------------------------------
# 7b. Size-vs-performance correlation
# ---------------------------------------------------------------------------
#
# "Size" and "results" are each ambiguous, so this section is deliberately
# split into three separate pairings rather than one do-everything function:
#
#   wallet_size_vs_pnl()   -- in-market $ volume (taker+maker) vs. recent
#                              realized PnL. "Do bigger players in this
#                              market tend to be the ones with better recent
#                              track records?"
#   absorption_vs_pnl()    -- $ absorbed as a MAKER specifically (the whale-
#                              absorption angle from market_structure()) vs.
#                              recent realized PnL. "Do whales who absorb
#                              more flow tend to be sharper?"
#   aggressor_pct_vs_size() -- aggressor% vs. $ volume, no PnL lookups
#                              needed. "Do bigger wallets trade more
#                              aggressively or more passively?"
#
# All three use every wallet classify_flow() sees for the market, not just
# a pre-filtered top-N -- correlations computed over a handful of
# hand-picked whales aren't meaningful. Even so, treat these as exploratory,
# not proof of anything:
#   - Sample size is one market's worth of wallets. Small, noisy, and not
#     independent draws (many of these wallets also traded each other).
#   - The "recent PnL" window (see get_wallet_pnl(); default 150 positions)
#     confounds with size: a hyperactive wallet's last 150 positions might
#     span two weeks, a light trader's might span years. A correlation here
#     partly measures "trades a lot" as much as "is skilled."
#   - Correlation, even if real, isn't causation -- a wallet trading bigger
#     could just mean better capitalized, not better informed.

def _spearman_corr(xs: list, ys: list):
    """
    Spearman rank correlation, computed from scratch (no numpy/scipy
    dependency) so this doesn't add a requirement to a script that's
    otherwise just `requests`. Preferred over Pearson here because $ size
    is almost always heavy-tailed (a few whales, a long tail of small
    fry) -- Pearson gets dominated by the single biggest wallet and tells
    you little; rank correlation doesn't care how big the gaps between
    values are, only their order.

    Returns None if there are fewer than 3 pairs (not enough to say
    anything) or if either series has zero variance (e.g. every wallet
    has the same $ volume -- degenerate, no correlation is definable).
    """
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(values):
        # Average ranks on ties (the standard Spearman tie-handling), 1-indexed.
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx = ranks(xs)
    ry = ranks(ys)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    cov = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry))
    var_x = sum((a - mean_rx) ** 2 for a in rx)
    var_y = sum((b - mean_ry) ** 2 for b in ry)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x * var_y) ** 0.5


def _quartile_report(pairs: list, x_label: str, y_label: str, y_is_binary: bool = False, y_is_pct: bool = False):
    """
    Buckets `pairs` (list of (x, y) tuples) into quartiles by x and prints
    avg y (or, if y_is_binary, % of y==1) per bucket. Much more readable
    than a single correlation coefficient, and won't hide a nonlinear
    relationship (e.g. "performance improves with size up to a point, then
    it's mostly noise") the way one r value would.
    """
    if len(pairs) < 4:
        print(f"  (only {len(pairs)} wallets with both {x_label} and {y_label} -- too few to bucket)")
        return
    ordered = sorted(pairs, key=lambda p: p[0])
    n = len(ordered)
    q = n // 4
    buckets = [ordered[0:q], ordered[q:2 * q], ordered[2 * q:3 * q], ordered[3 * q:]]
    labels = ["Q1 (smallest)", "Q2", "Q3", "Q4 (largest)"]
    print(f"  {'Bucket':<16} {'n':>4} {x_label + ' range':<28} {'result':>18}")
    for label, bucket in zip(labels, buckets):
        if not bucket:
            continue
        xs = [p[0] for p in bucket]
        ys = [p[1] for p in bucket]
        x_range = f"${xs[0]:,.0f} - ${xs[-1]:,.0f}"
        if y_is_binary:
            result = f"{100 * sum(ys) / len(ys):.0f}% profitable"
        elif y_is_pct:
            result = f"avg {sum(ys) / len(ys):.1f}%"
        else:
            result = f"avg ${sum(ys) / len(ys):,.0f}"
        print(f"  {label:<16} {len(bucket):>4} {x_range:<28} {result:>18}")


def wallet_size_vs_pnl(condition_id: str, market: dict = None, all_trades=None, taker_trades=None,
                        min_usd: float = 0.0):
    """
    Correlates each wallet's total $ volume IN THIS MARKET (taker+maker
    combined) against its RECENT realized PnL (see get_wallet_pnl -- last
    150 positions by default, not lifetime). Answers: "are bigger players
    in this market the ones with better recent track records, or is size
    unrelated to (or even inversely related to) recent performance?"

    Skips wallets with no fetchable PnL data. min_usd filters out dust
    wallets before correlating, since a huge pile of $5 wallets mostly
    just adds noise at the bottom of the size distribution.
    """
    team_map = build_token_team_map(market) if market else {}
    flows = classify_flow(condition_id, token_team_map=team_map, market=market,
                           all_trades=all_trades, taker_trades=taker_trades)

    pairs, binary_pairs, skipped = [], [], 0
    for wallet, wf in flows.items():
        total_usd = wf.taker_usd + wf.maker_usd
        if total_usd < min_usd:
            continue
        pnl = get_wallet_pnl_cached(wallet)
        if pnl is None or pnl.get("n_positions", 0) == 0:
            skipped += 1
            continue
        pairs.append((total_usd, pnl["realizedPnl"]))
        binary_pairs.append((total_usd, 1 if pnl["realizedPnl"] > 0 else 0))

    print(f"\n--- Wallet size ($ volume, this market) vs. recent realized PnL ---")
    print(f"{len(pairs)} wallets with usable PnL data (skipped {skipped} with no PnL record), "
          f"min_usd=${min_usd:,.0f}")

    corr = _spearman_corr([p[0] for p in pairs], [p[1] for p in pairs])
    if corr is None:
        print("  Not enough data (or zero variance) to compute a correlation.")
    else:
        direction = "positive" if corr > 0 else "negative" if corr < 0 else "no"
        print(f"  Spearman correlation: {corr:+.2f} ({direction} -- "
              f"1.0 = bigger always more profitable, -1.0 = bigger always less profitable, 0 = no relationship)")

    print("\n  By size quartile:")
    _quartile_report(binary_pairs, "$ volume", "PnL", y_is_binary=True)
    return pairs


def absorption_vs_pnl(condition_id: str, market: dict = None, all_trades=None, taker_trades=None):
    """
    Correlates $ absorbed AS A MAKER (the whale-absorption metric from
    market_structure() -- resting orders that got hit) against recent
    realized PnL, across EVERY maker wallet in the market, not just the
    top few market_structure() prints. Answers: "do whales who absorb more
    flow tend to be the sharper ones, or is absorption volume unrelated to
    recent track record?"
    """
    if all_trades is None:
        all_trades = get_trades(condition_id, taker_only=False)
    if taker_trades is None:
        taker_trades = get_trades(condition_id, taker_only=True)
    taker_keys = {(t.get("transaction_hash"), t.get("proxy_wallet")) for t in taker_trades}

    by_tx = defaultdict(list)
    for t in all_trades:
        by_tx[t.get("transaction_hash")].append(t)

    maker_volume = defaultdict(float)
    for tx_hash, rows in by_tx.items():
        takers = [r for r in rows if (tx_hash, r.get("proxy_wallet")) in taker_keys]
        makers = [r for r in rows if (tx_hash, r.get("proxy_wallet")) not in taker_keys]
        if not takers:
            continue
        for m in makers:
            usd = float(m.get("size", 0)) * float(m.get("price", 0))
            maker_volume[m["proxy_wallet"]] += usd

    pairs, binary_pairs, skipped = [], [], 0
    for wallet, vol in maker_volume.items():
        pnl = get_wallet_pnl_cached(wallet)
        if pnl is None or pnl.get("n_positions", 0) == 0:
            skipped += 1
            continue
        pairs.append((vol, pnl["realizedPnl"]))
        binary_pairs.append((vol, 1 if pnl["realizedPnl"] > 0 else 0))

    print(f"\n--- Maker/absorption $ volume vs. recent realized PnL ---")
    print(f"{len(pairs)} maker wallets with usable PnL data (skipped {skipped} with no PnL record)")

    corr = _spearman_corr([p[0] for p in pairs], [p[1] for p in pairs])
    if corr is None:
        print("  Not enough data (or zero variance) to compute a correlation.")
    else:
        direction = "positive" if corr > 0 else "negative" if corr < 0 else "no"
        print(f"  Spearman correlation: {corr:+.2f} ({direction} -- "
              f"higher means wallets that absorb more also tend to have better recent PnL)")

    print("\n  By absorption-volume quartile:")
    _quartile_report(binary_pairs, "$ absorbed", "PnL", y_is_binary=True)
    return pairs


def aggressor_pct_vs_size(condition_id: str, market: dict = None, all_trades=None, taker_trades=None,
                           min_usd: float = 0.0):
    """
    Correlates each wallet's $ volume in this market against its
    aggressor% (see WalletFlow.aggressor_pct). No PnL lookups needed, so
    this is fast and safe to run even if wallet-PnL endpoints are flaky.
    Answers: "do bigger wallets tend to trade more aggressively (crossing
    the spread) or more passively (sitting on resting orders), or is
    aggressor% unrelated to size?"
    """
    team_map = build_token_team_map(market) if market else {}
    flows = classify_flow(condition_id, token_team_map=team_map, market=market,
                           all_trades=all_trades, taker_trades=taker_trades)

    pairs = []
    for wallet, wf in flows.items():
        total_usd = wf.taker_usd + wf.maker_usd
        if total_usd < min_usd:
            continue
        pairs.append((total_usd, wf.aggressor_pct))

    print(f"\n--- Wallet size ($ volume, this market) vs. aggressor% ---")
    print(f"{len(pairs)} wallets, min_usd=${min_usd:,.0f}")

    corr = _spearman_corr([p[0] for p in pairs], [p[1] for p in pairs])
    if corr is None:
        print("  Not enough data (or zero variance) to compute a correlation.")
    else:
        direction = "positive" if corr > 0 else "negative" if corr < 0 else "no"
        print(f"  Spearman correlation: {corr:+.2f} ({direction} -- "
              f"positive means bigger wallets trade MORE aggressively, negative means bigger "
              f"wallets sit back and let the market come to them)")

    print("\n  By size quartile (avg aggressor%):")
    _quartile_report(pairs, "$ volume", "aggressor%", y_is_pct=True)
    return pairs


# ---------------------------------------------------------------------------
# 7c. Suggested play + unit size (heuristic synthesis, NOT a proven signal)
# ---------------------------------------------------------------------------
#
# NOTE ON WHY THIS EXISTS AS A SEPARATE, LOUDLY-CAVEATED SECTION:
# this script's own history already includes a points-based scoring system
# (final_verdict()) that was DELETED -- see the module docstring's
# CHANGELOG and score_backtest.py's history. That's evidence someone
# concluded a black-box "0-10 score" wasn't trustworthy enough to keep
# around. This function is NOT a resurrection of that system with a new
# name. It's a transparent combination of numbers this script ALREADY
# prints elsewhere (team_net_usd, PROFITABLE/LOSING status, the size-vs-PnL
# correlation) -- every input is visible, the weighting is a few lines of
# arithmetic you can read below, and it has NOT been backtested. Treat the
# output as "here's what the signals already computed above add up to,"
# not "the model says bet this."

def suggested_play(condition_id: str, market: dict = None, all_trades=None, taker_trades=None,
                    min_usd: float = 100.0, bankroll_units: float = 100.0, max_unit_pct: float = 5.0):
    """
    Combines two signals this script already computes into one team-level
    read and a capped, conviction-scaled unit-size suggestion:

      1. Smart-money-weighted net flow per team: for each wallet with a
         known recent PnL sign, its net $ on each team (wf.team_net_usd --
         positive = net buying that team) is weighted +1 if the wallet is
         recently PROFITABLE, -1 if LOSING, and 0 (excluded) if unknown.
         Summed per team, this answers "net of everyone whose recent track
         record we can see, which side is 'smart' money actually on."
      2. The wallet_size_vs_pnl correlation (see above) as a confidence
         multiplier on the whole read: if size doesn't predict recent PnL
         AT ALL in this specific market (corr near 0 or negative), the
         entire "bigger/more-active wallets are the smart ones" premise
         this function leans on is shaky HERE, so conviction gets scaled
         down accordingly, not just left at face value.

    Unit size is `conviction * max_unit_pct` of `bankroll_units`, where
    conviction in [0, 1] = (how dominant the leading team's weighted score
    is, relative to total $ volume) * (correlation confidence multiplier).
    It is capped at max_unit_pct of bankroll_units no matter how strong the
    read looks -- there's no scenario here that should ever look like "bet
    it all," because a single market's wallet sample is small and noisy.

    Prints its reasoning, not just its conclusion -- every number that goes
    into the recommendation is shown, so you can decide for yourself
    whether to trust it. NOT gambling or financial advice, not backtested,
    and small/noisy by construction (see the size-vs-performance section's
    caveats -- they all apply here too, compounded).
    """
    team_map = build_token_team_map(market) if market else {}
    flows = classify_flow(condition_id, token_team_map=team_map, market=market,
                           all_trades=all_trades, taker_trades=taker_trades)

    team_weighted = defaultdict(float)   # team -> smart-money-weighted net $ flow
    team_total_abs = defaultdict(float)  # team -> total |net $| seen (for the confidence denominator)
    pnl_pairs = []                       # (total_usd, 1-if-profitable) for the correlation confidence check
    n_profitable = n_losing = n_unknown = 0

    for wallet, wf in flows.items():
        total_usd = wf.taker_usd + wf.maker_usd
        if total_usd < min_usd:
            continue
        pnl = get_wallet_pnl_cached(wallet)
        if pnl is None or pnl.get("n_positions", 0) == 0:
            n_unknown += 1
            weight = 0.0
        else:
            is_profitable = pnl["realizedPnl"] > 0
            weight = 1.0 if is_profitable else -1.0
            n_profitable += 1 if is_profitable else 0
            n_losing += 0 if is_profitable else 1
            pnl_pairs.append((total_usd, 1 if is_profitable else 0))
        for team, net_usd in wf.team_net_usd.items():
            team_weighted[team] += net_usd * weight
            team_total_abs[team] += abs(net_usd)

    print(f"\n--- Suggested play (heuristic, NOT backtested -- read the reasoning, don't just take the number) ---")
    print(f"Wallets considered (>=${min_usd:,.0f} volume): "
          f"{n_profitable} recently PROFITABLE, {n_losing} recently LOSING, {n_unknown} unknown/no PnL data")

    if not team_weighted:
        print("Not enough data to suggest anything.")
        return None

    ranked_teams = sorted(team_weighted.items(), key=lambda kv: kv[1], reverse=True)
    print("\nSmart-money-weighted net flow by team (positive = net PROFITABLE-wallet buying):")
    for team, score in ranked_teams:
        print(f"  {team:<30} {score:+12,.0f}   (${team_total_abs[team]:,.0f} total abs volume on this team)")

    top_team, top_score = ranked_teams[0]
    total_abs_all = sum(team_total_abs.values()) or 1.0
    raw_conviction = abs(top_score) / total_abs_all

    corr = _spearman_corr([p[0] for p in pnl_pairs], [p[1] for p in pnl_pairs]) if len(pnl_pairs) >= 3 else None
    if corr is None:
        corr_confidence = 0.5  # neutral -- not enough data to say size predicts PnL here one way or the other
        corr_note = "not enough wallets with PnL data to check whether size predicts performance in this market"
    else:
        corr_confidence = max(0.0, min(1.0, (corr + 1) / 2))
        corr_note = f"wallet_size_vs_pnl correlation here is {corr:+.2f}"

    conviction = max(0.0, min(1.0, raw_conviction * corr_confidence))
    suggested_units = conviction * (max_unit_pct / 100.0) * bankroll_units
    suggested_pct = 100 * suggested_units / bankroll_units if bankroll_units else 0.0

    print(f"\nLeading side: {top_team}")
    print(f"  raw conviction (score dominance):     {raw_conviction:.2f}")
    print(f"  correlation confidence multiplier:    {corr_confidence:.2f}  ({corr_note})")
    print(f"  combined conviction:                  {conviction:.2f}")
    print(f"\nSuggested unit size: {suggested_units:.2f} units "
          f"({suggested_pct:.1f}% of a {bankroll_units:.0f}-unit bankroll, "
          f"capped at {max_unit_pct:.0f}% no matter how strong the read looks)")

    if n_profitable + n_losing < 5:
        print("\nCAUTION: fewer than 5 wallets with known PnL went into this -- treat conviction as "
              "close to meaningless with a sample this small.")
    if conviction < 0.15:
        print("\nThis is a weak/ambiguous read (low conviction) -- the honest takeaway here is "
              "'no strong signal either way', not 'small bet on the leader'.")

    print("\nThis is a transparent heuristic over noisy, small-sample data -- not gambling or "
          "financial advice, and it has NOT been backtested. Use it as one more input alongside "
          "your own judgment and the rest of this script's output, not as a standalone signal.")

    return {"top_team": top_team, "conviction": conviction, "suggested_units": suggested_units,
            "team_weighted": dict(team_weighted)}


# ---------------------------------------------------------------------------
# 7d. Wallets vs. cash, per side of the game
# ---------------------------------------------------------------------------

def _json_list(market: dict, field_name: str):
    """Gamma returns 'outcomes' / 'clobTokenIds' as JSON-encoded strings."""
    val = market.get(field_name)
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return val or []


def wallets_vs_cash(market: dict, min_shares: float = 1.0, max_pages: int = 100, top_k: int = 10):
    """
    For each side (outcome) of the market: how many distinct wallets are on
    it vs. how much cash is on it. Answers "is this side crowded with lots of
    small wallets, or is it a few big ones -- and which side is the MONEY on
    vs. which side is the CROWD on?"

    Walks EVERY current holder (not just the top 100) via
    /v2/holders?include_pnl=true, which serves per-side gross balances with
    cost basis, so each row carries:
      - entry_cost_usdc: cash the wallet put in on that side (cost basis of
        the shares it still holds, fee-exclusive) -> "cash on this side"
      - current_value:   those same shares marked at the current price

    Caveats worth keeping in mind when reading the numbers:
      - This is a snapshot of CURRENT holders. Wallets that already sold out
        aren't counted, and cash that's been withdrawn from a side isn't
        either. Use the trade-flow sections for who traded, this one for who
        is still holding.
      - A wallet hedged on both sides is counted on both (so wallet shares
        below are over per-side counts, not unique wallets).
      - min_shares drops dust holders (default: under 1 share) so a swarm of
        $0.30 positions doesn't inflate the wallet count.
      - If max_pages (100 holders per outcome per page) is hit, holders are
        ranked biggest-first, so what's cut is the smallest ones: cash is
        nearly complete but the wallet counts are lower bounds.
    """
    condition_id = market.get("conditionId")
    team_map = build_token_team_map(market)
    outcomes = _json_list(market, "outcomes")

    print(f"\n--- Wallets vs. cash by side (current holders of >= {min_shares:g} share) ---")
    try:
        groups, truncated = _walk_holders(condition_id, limit=100, include_pnl=True,
                                          min_balance=min_shares, max_pages=max_pages)
    except Exception as e:
        print(f"could not fetch holders for side totals: {e}")
        return None
    if not groups:
        print("No holders returned for this market.")
        return None

    sides = []
    for grp in groups:
        holders = grp["holders"]
        label = team_map.get(grp["token_id"])
        if not label and holders:
            idx = holders[0].get("outcome_index")
            if isinstance(idx, int) and 0 <= idx < len(outcomes):
                label = outcomes[idx]
        label = label or str(grp["token_id"])
        if label in ("Yes", "No"):
            label = f"{label} ({market.get('question', 'this market')})"

        costs = [h["entry_cost_usdc"] for h in holders if h.get("entry_cost_usdc") is not None]
        values = [h["current_value"] for h in holders if h.get("current_value") is not None]
        cash = sum(costs)
        costs_desc = sorted(costs, reverse=True)
        sides.append({
            "label": label,
            "wallets": len({h.get("proxy_wallet") for h in holders}),
            "cash": cash,
            "value": sum(values),
            "avg": cash / len(costs) if costs else 0.0,
            "median": statistics.median(costs) if costs else 0.0,
            "top_share": 100 * sum(costs_desc[:top_k]) / cash if cash else 0.0,
            "missing": len(holders) - len(costs),
        })
    sides.sort(key=lambda x: x["cash"], reverse=True)

    print(f"  {'Side':<28} {'Wallets':>8} {'Cash in $':>13} {'Value now $':>13} "
          f"{'Avg $/wlt':>10} {'Median $/wlt':>13} {'Top-' + str(top_k) + ' share':>12}")
    for sd in sides:
        print(f"  {sd['label'][:28]:<28} {sd['wallets']:>8,} {sd['cash']:>13,.0f} {sd['value']:>13,.0f} "
              f"{sd['avg']:>10,.0f} {sd['median']:>13,.0f} {sd['top_share']:>11.0f}%")

    tot_w = sum(sd["wallets"] for sd in sides)
    tot_cash = sum(sd["cash"] for sd in sides)
    if tot_w and tot_cash:
        print("\n  Share of wallets vs. share of cash:")
        for sd in sides:
            print(f"    {sd['label'][:28]:<28} {100 * sd['wallets'] / tot_w:5.1f}% of wallets | "
                  f"{100 * sd['cash'] / tot_cash:5.1f}% of cash")
        if len(sides) >= 2:
            most_wallets = max(sides, key=lambda x: x["wallets"])
            most_cash = max(sides, key=lambda x: x["cash"])
            if most_wallets is not most_cash:
                print(f"\n  => More wallets are on {most_wallets['label']}, but more cash is on "
                      f"{most_cash['label']}: the crowd and the money disagree.")
            else:
                print(f"\n  => Both more wallets and more cash are on {most_cash['label']}.")

    if any(sd["missing"] for sd in sides):
        print("\n  NOTE: some holder rows had no cost data (the API reports null, not zero), "
              "so cash for those wallets is excluded.")
    if truncated:
        print(f"\n  WARNING: stopped after {max_pages} pages with more holders still available. "
              f"Holders come biggest-first, so cash totals are close to complete but wallet "
              f"counts are LOWER BOUNDS. Raise --holder-max-pages for a fuller count.")
    print("\n  Snapshot of current holders only (exited wallets not counted); a wallet hedged on "
          "both sides is counted on both.")
    return sides


def diagnose_taker_filter(condition_id: str, all_trades=None, taker_trades=None, quiet: bool = True):
    """Sanity check: confirm the takerOnly param is actually restricting results.
    Silent when everything looks fine (quiet=True, the default) -- only prints
    when there's an actual problem worth stopping for."""
    if all_trades is None:
        all_trades = get_trades(condition_id, taker_only=False)
    if taker_trades is None:
        taker_trades = get_trades(condition_id, taker_only=True)

    if len(all_trades) == 0:
        print("No trades at all for this market -- likely too new/thin, or wrong condition_id.")
        return
    if len(all_trades) == len(taker_trades):
        print("WARNING: takerOnly filter isn't being honored -- every row is being treated as a "
              "taker row, so the maker/whale breakdown below can't be trusted. Raw sample row:")
        print(all_trades[0])
    elif not quiet:
        print(f"takerOnly filter OK: {len(all_trades) - len(taker_trades)} maker-side rows found.")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Find a Polymarket sports market and report its top maker whales."
    )
    parser.add_argument("query", help="Search text, e.g. \"England Argentina\" or \"Yankees Red Sox\"")
    parser.add_argument("--live", action="store_true",
                         help="Only match events currently in progress (event['live'] == True)")
    parser.add_argument("--min-usd", type=float, default=500.0,
                         help="[--verbose only] Minimum $ volume for a wallet to show up in the whale ranking (default 500)")
    parser.add_argument("--smart-money-min-usd", type=float, default=250.0,
                         help="[--verbose only] Minimum $ volume for a wallet to get a PnL lookup (default 250)")
    parser.add_argument("--top-n", type=int, default=15,
                         help="[--verbose only] How many wallets to show in the ranking table (default 15)")
    parser.add_argument("--whale-n", type=int, default=3,
                         help="How many top maker whales to report (default 3)")
    parser.add_argument("--pick", type=int, default=0,
                         help="If multiple markets match, index into the results (default 0)")
    parser.add_argument("--list-only", action="store_true",
                         help="Just list matching markets and exit, without running analysis")
    parser.add_argument("--dump-holders-raw", action="store_true",
                         help="Print the raw, unparsed /holders response for this market and exit")
    parser.add_argument("--check-wallet", type=str, default=None,
                         help="Polymarket username (without @) or wallet address to directly "
                              "check against this market")
    parser.add_argument("--verbose", action="store_true",
                         help="Also print the full detailed report: wallet ranking, team-level "
                              "taker/maker split, current holdings, recent-form leaderboard check, "
                              "and smart-money breakdown -- on top of the whale summary")
    parser.add_argument("--correlate", action="store_true",
                         help="Print size-vs-performance correlations for every wallet in this "
                              "market: $ volume vs. recent PnL, absorption $ vs. recent PnL, and "
                              "$ volume vs. aggressor%%. Makes one PnL lookup per wallet, so this "
                              "can be slow on a market with many participants.")
    parser.add_argument("--suggest-play", action="store_true",
                         help="Print a heuristic suggested side and unit size, synthesized from "
                              "smart-money-weighted flow and the size-vs-PnL correlation. NOT "
                              "backtested, NOT financial/gambling advice -- see the printed "
                              "reasoning before treating this as a signal.")
    parser.add_argument("--bankroll-units", type=float, default=100.0,
                         help="[--suggest-play only] Hypothetical bankroll size in units, for "
                              "sizing the suggestion (default 100)")
    parser.add_argument("--max-unit-pct", type=float, default=5.0,
                         help="[--suggest-play only] Hard cap on suggested unit size as %% of "
                              "bankroll, regardless of conviction (default 5.0)")
    parser.add_argument("--no-side-totals", action="store_true",
                         help="Skip the wallets-vs-cash-per-side table (it walks every holder of "
                              "the market, which is extra API calls on very busy markets)")
    parser.add_argument("--min-shares", type=float, default=1.0,
                         help="[side totals] Ignore holders with fewer than this many shares "
                              "(dust filter, default 1)")
    parser.add_argument("--holder-max-pages", type=int, default=100,
                         help="[side totals] Max pages of 100 holders per side to walk before "
                              "stopping with a lower-bound warning (default 100)")
    args = parser.parse_args()

    matches = find_market(args.query, only_live=args.live)
    if not matches:
        print(f"No market found for query: {args.query!r}")
        return

    if args.list_only or len(matches) > 1:
        print(f"Found {len(matches)} matching market(s):")
        for i, m in enumerate(matches):
            live_tag = " [LIVE]" if m.get("_event_live") else ""
            print(f"  [{i}] {m.get('question')}{live_tag}  (slug: {m.get('slug')})")
        if args.list_only:
            return
        print(f"\nUsing index {args.pick} (pass --pick N to choose a different one, "
              f"or --list-only to just browse).\n")

    market = matches[args.pick]
    condition_id = market.get("conditionId")

    if args.dump_holders_raw:
        try:
            raw = _get("/holders", {"condition": condition_id, "limit": 5})
            print(json.dumps(raw, indent=2)[:5000])
        except Exception as e:
            print(f"get_holders failed: {e}")
        return

    print(f"Market: {market.get('question')}  (slug: {market.get('slug')})")

    all_trades = get_trades(condition_id, taker_only=False)
    taker_trades = get_trades(condition_id, taker_only=True)

    mismatched = validate_market_filter(condition_id, all_trades=all_trades)
    if mismatched:
        print("Stopping here -- fix the market filter before trusting any further output.")
        return
    diagnose_taker_filter(condition_id, all_trades=all_trades, taker_trades=taker_trades)

    market_structure(condition_id, all_trades=all_trades, taker_trades=taker_trades,
                      market=market, top_n=args.whale_n)

    if not args.no_side_totals:
        wallets_vs_cash(market, min_shares=args.min_shares, max_pages=args.holder_max_pages)

    if args.verbose:
        report(market, min_usd=args.min_usd, top_n=args.top_n,
               all_trades=all_trades, taker_trades=taker_trades)
        team_level_breakdown(condition_id, market, all_trades=all_trades, taker_trades=taker_trades)
        current_holdings(market)
        recent_form_breakdown(market)
        diagnose_maker_rows(condition_id, all_trades=all_trades, taker_trades=taker_trades)
        smart_money_breakdown(condition_id, market, min_usd=args.smart_money_min_usd,
                              all_trades=all_trades, taker_trades=taker_trades)

    if args.correlate:
        aggressor_pct_vs_size(condition_id, market=market, all_trades=all_trades, taker_trades=taker_trades)
        wallet_size_vs_pnl(condition_id, market=market, all_trades=all_trades, taker_trades=taker_trades)
        absorption_vs_pnl(condition_id, market=market, all_trades=all_trades, taker_trades=taker_trades)

    if args.suggest_play:
        suggested_play(condition_id, market=market, all_trades=all_trades, taker_trades=taker_trades,
                        bankroll_units=args.bankroll_units, max_unit_pct=args.max_unit_pct)

    if args.check_wallet:
        wallet = args.check_wallet
        if not wallet.startswith("0x"):
            resolved = resolve_username(wallet)
            if resolved:
                print(f"\nResolved username '{wallet}' -> {resolved}")
                wallet = resolved
            else:
                print(f"\nCould not resolve username '{wallet}' to a wallet address.")
                wallet = None
        if wallet:
            check_wallet_in_market(wallet, condition_id)


def diagnose_maker_rows(condition_id: str, n: int = 3, all_trades=None, taker_trades=None):
    """Prints raw examples of taker rows vs isolated 'maker' rows so you can
    visually confirm the maker rows look like real fills, not SPLIT/MERGE/REDEEM
    ledger noise. Pass all_trades/taker_trades from a shared snapshot."""
    if all_trades is None:
        all_trades = get_trades(condition_id, taker_only=False)
    if taker_trades is None:
        taker_trades = get_trades(condition_id, taker_only=True)
    taker_keys = {(t.get("transaction_hash"), t.get("proxy_wallet")) for t in taker_trades}

    maker_rows = [t for t in all_trades if (t.get("transaction_hash"), t.get("proxy_wallet")) not in taker_keys]

    print(f"\n--- Diagnostic: raw taker row vs raw 'maker' row ---")
    print(f"Sample TAKER row (should be a normal trade fill):")
    if taker_trades:
        print(taker_trades[0])
    print(f"\nSample 'MAKER' row (verify this looks like a real trade, not a SPLIT/MERGE/REDEEM):")
    for row in maker_rows[:n]:
        print(row)
    print(f"\nTotal 'maker' rows: {len(maker_rows)}, summing to "
          f"${sum(float(r.get('size',0))*float(r.get('price',0)) for r in maker_rows):,.2f}")
    print("If these rows look structurally different from the taker row (missing fields, "
          "a different 'type', zero price, etc.), the maker/taker split above isn't reliable.")
    return maker_rows


def team_level_breakdown(condition_id: str, market: dict, all_trades=None, taker_trades=None):
    """Aggregates taker $ vs maker $ PER TEAM across all wallets. Pass
    all_trades/taker_trades from a shared snapshot -- see main()."""
    team_map = build_token_team_map(market)
    flows = classify_flow(condition_id, token_team_map=team_map, market=market,
                           all_trades=all_trades, taker_trades=taker_trades)

    team_taker_totals = defaultdict(float)
    team_maker_totals = defaultdict(float)

    for wf in flows.values():
        for team, usd in wf.team_taker_usd.items():
            team_taker_totals[team] += usd
        for team, usd in wf.team_maker_usd.items():
            team_maker_totals[team] += usd

    all_teams = set(team_taker_totals) | set(team_maker_totals)

    print("\n--- Team-level taker vs maker breakdown ---")
    print(f"{'Team':<25} {'Taker $':>12} {'Maker $':>12} {'Taker %':>8}")
    for team in all_teams:
        taker = team_taker_totals.get(team, 0.0)
        maker = team_maker_totals.get(team, 0.0)
        total = taker + maker
        p = 100 * taker / total if total else 0.0
        print(f"{team:<25} {taker:>12,.0f} {maker:>12,.0f} {p:>7.1f}%")

    print("\nRead this as: a team with a HIGH taker % means people betting on that team are")
    print("mostly paying up / crossing the spread to get in. A LOWER taker % (relatively)")
    print("means more of that team's volume came from resting orders getting filled.")
    return team_taker_totals, team_maker_totals


def check_wallet_in_market(wallet: str, condition_id: str):
    """Direct check on ONE wallet: current live position size, plus a fresh
    re-pull of trades to see if they show up in the trade feed at all."""
    print(f"\n--- Direct check: {wallet} in market {condition_id} ---")

    try:
        positions, _ = _paginate("/positions", {"user": wallet, "condition": condition_id, "limit": 100})
        print("Current position(s) in this market:")
        for p in positions:
            print(f"  outcome={p.get('outcome')} size={p.get('current_size')} "
                  f"avgPrice={p.get('avg_price')} currentValue={p.get('current_value')} "
                  f"realizedPnl={p.get('realized_pnl')}")
        if not positions:
            print("  (no positions returned -- either wrong market/wallet, or API lag)")
    except Exception as e:
        print(f"  could not fetch positions: {e}")

    try:
        fresh_trades = get_trades(condition_id, taker_only=False)
        wallet_trades = [t for t in fresh_trades if (t.get("proxy_wallet") or "").lower() == wallet.lower()]
        print(f"\nFresh trade pull just now: {len(fresh_trades)} total trades in market, "
              f"{len(wallet_trades)} belong to this wallet.")
        if wallet_trades:
            total_usd = sum(float(t.get("size", 0)) * float(t.get("price", 0)) for t in wallet_trades)
            print(f"  This wallet's trades sum to ${total_usd:,.0f} across {len(wallet_trades)} fills.")
        else:
            print("  This wallet has ZERO trades in the current /trades pull for this market.")
    except Exception as e:
        print(f"  could not fetch fresh trades: {e}")


def validate_market_filter(condition_id: str, n_sample: int = 5, quiet: bool = True, all_trades=None):
    """CRITICAL CHECK: confirms /trades is actually being filtered to this
    specific market, rather than silently returning a broader/unfiltered feed.
    Silent when everything checks out (quiet=True, the default) -- only
    prints when there's an actual problem worth stopping for. Pass
    all_trades in if you've already fetched it, to avoid a duplicate call."""
    trades = all_trades if all_trades is not None else get_trades(condition_id, taker_only=False)
    if not trades:
        print(f"No trades returned at all for {condition_id}.")
        return None

    mismatched = [t for t in trades
                  if t.get("condition_id") and t.get("condition_id").lower() != condition_id.lower()]

    if mismatched:
        print(f"WARNING: market filter contamination -- {len(mismatched)} of {len(trades)} rows "
              f"belong to a different market entirely. Every number below should be treated as "
              f"unreliable until this is fixed. Sample contaminating conditionIds:")
        seen = set()
        for t in mismatched:
            cid = t.get("condition_id")
            if cid not in seen:
                seen.add(cid)
                print(f"  {cid}")
            if len(seen) >= n_sample:
                break
    elif not quiet:
        print("Market filter validated OK -- all returned rows match the requested market.")
    return mismatched


if __name__ == "__main__":
    main()