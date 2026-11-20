"""
Polymarket whale flow -- Streamlit app.

Run:  streamlit run app.py
Needs: whale_flow.py and whale_data.py in the same folder.

Tab 1  Analyze a game : type a team / matchup (or paste a polymarket.com link)
Tab 2  Scan leagues   : pick sports/leagues, rank every live / soon-starting game
"""

import datetime as dt

import pandas as pd
import streamlit as st

import whale_data as wd

st.set_page_config(page_title="Polymarket Whale Flow", page_icon="🐋", layout="wide")

if getattr(wd, "DATA_VERSION", 0) < 15:
    st.error("whale_data.py in this app is an old version. Replace it with the latest whale_data.py "
             "(it should contain the line DATA_VERSION = 15), commit it, then reboot the app from 'Manage app'.")
    st.code(f"The app is loading: {getattr(wd, '__file__', '?')}\nIts DATA_VERSION: {getattr(wd, 'DATA_VERSION', 'missing')}")
    st.stop()

# ---------- shared formatting ----------
MONEY = st.column_config.NumberColumn(format="$%,.0f")
PCT = st.column_config.NumberColumn(format="%.0f%%")
PTS = st.column_config.NumberColumn(format="%+.0f")
CENTS = st.column_config.NumberColumn(format="%.0f¢")
INT = st.column_config.NumberColumn(format="%d")

SIDE_CONFIG = {
    "Line %": PCT, "Wallets": INT, "Wallets %": PCT, "Cash in $": MONEY, "Cash %": PCT,
    "Cash − Line (pts)": PTS, "Value now $": MONEY, "Avg entry ¢": CENTS,
    "Holders' P&L $": st.column_config.NumberColumn(format="$%,.0f"),
    "Median $/wallet": MONEY, "Top-10 share %": PCT,
}


def gap_color(v):
    if pd.isna(v) or abs(v) < 3:
        return ""
    return "color: #1a9850" if v > 0 else "color: #d73027"


def find_play(sides, wmin, wmax, reverse=False):
    """
    First side that meets all three rules, else None:
      1) its share of wallets is between wmin and wmax (default 40-49%)
      2) its share of cash is higher than its share of wallets
      3) the line (implied probability) is at least its share of cash
         (reverse=True flips this: the line is at most its share of cash)
    """
    for s in sides or []:
        w, c, l = s.get("wallets_pct"), s.get("cash_pct"), s.get("line_pct")
        if None in (w, c, l):
            continue
        if wmin <= w < wmax + 1 and c > w and ((l <= c) if reverse else (l >= c)):
            return s
    return None


def live_row(row):
    """Light gray wash across the whole row for live games (works in light and dark themes)."""
    if row.get("Play"):
        return ["background-color: rgba(255,215,0,0.45)"] * len(row)
    return ["background-color: rgba(128,128,128,0.25)" if row["Status"] == "LIVE" else ""] * len(row)


def top_scroll_table(df: pd.DataFrame):
    """Static HTML table whose horizontal scrollbar sits ABOVE the table (flip the box, flip the content back)."""
    fmt = {"Line %": "{:.0f}%", "Wallets %": "{:.0f}%", "Cash %": "{:.0f}%", "Cash − Line (pts)": "{:+.0f}",
           "Total cash $": "${:,.0f}", "Value now $": "${:,.0f}", "Wallets": "{:,.0f}", "Cash $": "${:,.0f}", "Profitable cash %": "{:.0f}%"}
    sty = (df.style.apply(live_row, axis=1).map(gap_color, subset=["Cash − Line (pts)"])
           .format({k: v for k, v in fmt.items() if k in df.columns}, na_rep="—").hide(axis="index"))
    st.markdown(
        """<style>
        .tscroll{overflow-x:auto;transform:rotateX(180deg);padding:2px 0}
        .tscroll .inner{transform:rotateX(180deg);display:inline-block;min-width:100%}
        .tscroll table{border-collapse:collapse;font-size:13px;white-space:nowrap;width:100%}
        .tscroll th{text-align:left;padding:6px 10px;border-bottom:2px solid rgba(128,128,128,.5)}
        .tscroll td{padding:5px 10px;border-bottom:1px solid rgba(128,128,128,.2)}
        </style><div class="tscroll"><div class="inner">""" + sty.to_html() + "</div></div>",
        unsafe_allow_html=True)


def show_sides(df: pd.DataFrame):
    """The small headline table (line, wallets %, cash %, cash in, value now) + a detail expander."""
    small = ["Side", "Line", "Wallets", "Wallets %", "Cash in $", "Cash %", "Value now $", "Cash − Line (pts)"]
    st.dataframe(df[small].style.map(gap_color, subset=["Cash − Line (pts)"]),
                 hide_index=True, use_container_width=True, column_config=SIDE_CONFIG)
    with st.expander("More columns (line normalised, avg entry, holders' P&L, concentration)"):
        st.dataframe(df, hide_index=True, use_container_width=True, column_config=SIDE_CONFIG)


# ---------- cached data calls ----------
@st.cache_data(ttl=120, show_spinner=False)
def c_search(query, only_live, hide_decided):
    return wd.search_games(query, only_live=only_live, hide_decided=hide_decided)


@st.cache_data(ttl=20, show_spinner=False)
def c_analyze(market, min_shares, pages, flow, sharp, top_n, tz):
    return wd.analyze_market(market, min_shares=min_shares, max_pages=pages, include_flow=flow,
                             sharp=sharp, top_holders=top_n, tz=tz)


@st.cache_data(ttl=300, show_spinner=False)
def c_sharp(market, top_n):
    return wd.play_sharp(market, top_n=top_n)


@st.cache_data(ttl=3600, show_spinner=False)
def c_leagues():
    return wd.list_leagues()


# ---------- sidebar ----------
with st.sidebar:
    st.header("Settings")
    tz = st.text_input("Time zone", wd.DEFAULT_TZ)
    min_shares = st.number_input("Ignore holders under (shares)", 0.0, 1000.0, 1.0, 1.0)
    st.caption("Holder depth = pages of 100 holders per side. More pages = fuller wallet counts, slower.")
    pages = st.slider("Holder depth (analyzer)", 1, 100, 30)
    top_n = st.slider("Top holders shown per side", 3, 25, 10)
    hide_decided = st.checkbox("Hide decided games (a side at 99¢ or more)", True,
                               help="Removes markets priced 99¢/1¢ or beyond, which are effectively settled.")
    if st.button("Refresh prices now"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption("Data: Polymarket Gamma + Data API v2 (public, no key). "
               "Cash in = what current holders paid for their open shares. "
               "Value now = those shares at the current price.")

st.title("🐋 Polymarket whale flow")
tab_game, tab_scan, tab_day = st.tabs(["Analyze a game", "Scan leagues", "Day scorecard"])


# =====================================================================
# TAB 1 -- analyze one game
# =====================================================================
def render_analysis(res: dict):
    m = res["market"]
    st.subheader(m.get("_event_title") or m.get("question") or "Market")
    meta = []
    if m.get("_event_live"):
        meta.append("🔴 LIVE")
    if m.get("sportsMarketType"):
        meta.append(m["sportsMarketType"])
    if m.get("line") is not None:
        meta.append(f"line {m['line']}")
    start = wd._parse_dt(m.get("_start_iso"))
    if start:
        meta.append(f"starts {wd.fmt_local(start, tz)}")
    meta.append(f"prices: {'live order book' if all(s['live_price'] for s in res['sides']) else 'cached (may lag)'}")
    meta.append(f"updated {wd.fmt_local(res['updated'], tz)}")
    st.caption(" · ".join(meta))

    for w in res["warnings"]:
        st.warning(w)

    sides = res["sides"]
    if sides:
        cols = st.columns(len(sides))
        for c, s in zip(cols, sides):
            c.metric(s["Side"], wd.line_text(s["price"]),
                     f"${s['cash']:,.0f} cash · {s['wallets']:,} wallets", delta_color="off")
    if res["verdict"]:
        st.info(res["verdict"])

    show_sides(res["sides_df"])
    st.download_button("Download table (CSV)", res["sides_df"].to_csv(index=False),
                       "sides.csv", "text/csv")

    t1, t2, t3, t4 = st.tabs(["Top holders", "Taker vs maker flow", "Maker whales", "Sharp check"])
    with t1:
        st.dataframe(res["top_holders_df"], hide_index=True, use_container_width=True, column_config={
            "Shares": st.column_config.NumberColumn(format="%,.0f"), "Cash in $": MONEY,
            "Value now $": MONEY, "Avg entry ¢": CENTS, "P&L this market $": MONEY})
        st.caption("Recent form: D/W/M # = rank on Polymarket's sports leaderboard (day / week / month).")
    with t2:
        if res["flow_df"] is None:
            st.caption("Flow was switched off for this run.")
        else:
            st.caption("Taker = paid up to trade now (aggressor). Maker = resting order that got filled.")
            st.dataframe(res["flow_df"], hide_index=True, use_container_width=True, column_config={
                "Taker buys $": MONEY, "Taker sells $": MONEY, "Net aggressor $": MONEY,
                "Maker $": MONEY, "Taker %": PCT})
            st.markdown("**Biggest aggressive fills**")
            st.dataframe(res["big_trades_df"], hide_index=True, use_container_width=True,
                         column_config={"Price ¢": CENTS, "Size $": MONEY})
    with t3:
        if res["whales_df"] is None or res["whales_df"].empty:
            st.caption("No maker whales found (or flow was off).")
        else:
            st.dataframe(res["whales_df"], hide_index=True, use_container_width=True, column_config={
                "Resting $ absorbed": MONEY, "Share of resting volume %": PCT, "% on that side": PCT,
                "Recent realized PnL $": MONEY})
    with t4:
        if res["sharp_df"] is None:
            st.caption("Tick “Sharp check” in the form above and run again. It looks up each top holder's recent "
                       "realized P&L (about 1 request per wallet, so it's slower).")
        else:
            st.dataframe(res["sharp_summary_df"], hide_index=True, use_container_width=True,
                         column_config={"% of checked cash from profitable wallets": PCT})
            st.dataframe(res["sharp_df"], hide_index=True, use_container_width=True,
                         column_config={"Cash in $": MONEY, "Recent realized PnL $": MONEY})


with tab_game:
    with st.form("game_form"):
        c1, c2 = st.columns([4, 1])
        query = c1.text_input("Game", placeholder="e.g. Cavaliers Pelicans  ·  or paste a polymarket.com/event/... link")
        only_live = c2.checkbox("Live only", False)
        o1, o2, o3 = st.columns(3)
        flow = o1.checkbox("Taker/maker flow", True)
        sharp = o2.checkbox("Sharp check (slower)", False)
        go = o3.form_submit_button("Find game", type="primary")

    if go and query.strip():
        try:
            with st.spinner("Searching Polymarket…"):
                st.session_state["found"] = c_search(query, only_live, hide_decided)
            st.session_state.pop("opened", None)
        except Exception as e:
            st.error(f"Search failed: {e}")

    found = st.session_state.get("found")
    if found is not None and not found:
        st.warning("No open markets matched. Try just a team name, or paste the event link.")
    if found:
        labels = [wd.market_label(m) for m in found]
        choice = st.selectbox("Market", range(len(found)), format_func=lambda i: labels[i])
        if st.button("Analyze", type="primary") or st.session_state.get("opened") == choice:
            st.session_state["opened"] = choice
            try:
                with st.spinner("Reading holders and trades… (big games can take a minute)"):
                    res = c_analyze(found[choice], min_shares, pages, flow, sharp, top_n, tz)
                render_analysis(res)
            except Exception as e:
                st.error(f"Analysis failed: {e}")

    # a game opened from the scan tab
    if st.session_state.get("from_scan"):
        st.divider()
        st.caption("Opened from the scan")
        try:
            with st.spinner("Loading…"):
                render_analysis(c_analyze(st.session_state["from_scan"], min_shares, pages, True, False, top_n, tz))
        except Exception as e:
            st.error(f"Analysis failed: {e}")


# =====================================================================
# TAB 2 -- scan leagues
# =====================================================================
with tab_scan:
    try:
        leagues = c_leagues()
    except Exception as e:
        leagues = []
        st.error(f"Couldn't load the league list: {e}")

    if leagues:
        # ---- day banner: is today a money-side day or a crowd-side day? ----
        from zoneinfo import ZoneInfo
        _sports_now = st.session_state.get("scan_sports") or [x for x in sorted({l["sport"] for l in leagues})
                                                              if x in ("Basketball", "Football", "Hockey", "Tennis")]
        _today = dt.datetime.now(ZoneInfo(tz)).date()
        with st.container(border=True):
            b1, b2 = st.columns([5, 1])
            banner = st.session_state.get("day_banner")
            if banner and banner["day"] == _today and banner["summary"]:
                s_ = banner["summary"]
                mw, cw, n_ = s_["money_won_split"], s_["crowd_won_split"], s_["split_games"]
                if n_ < 3:
                    head, rec = "Too early to call", f"only {n_} game(s) where the money and the crowd disagreed so far"
                elif mw > cw:
                    head, rec = "Money day", f"Money games are {mw}-{cw} straight up"
                elif cw > mw:
                    head, rec = "Crowd day", f"Crowd games are {cw}-{mw} straight up"
                else:
                    head, rec = "Split day", f"Crowd games are {cw}-{mw} straight up"
                b1.markdown(f"### Day: {head} ({rec})")
                b1.caption(f"Finished games today in {', '.join(banner['sports'])}: money side and crowd side compared "
                           f"on the {n_} games where they picked different teams · {s_['games']} games scored · "
                           f"updated {wd.fmt_local(banner['at'], tz)}")
            elif banner and banner["day"] == _today:
                b1.markdown("### Day: no finished games to score yet")
                b1.caption(f"Sports checked: {', '.join(banner['sports'])}")
            else:
                b1.markdown("### Day: not scored yet")
                b1.caption("Press “Update day record” to score today's finished games for the sports picked below.")
            if b2.button("Update day record"):
                chosen_ = [l for l in leagues if l["sport"] in _sports_now]
                lo_ = dt.datetime.combine(_today, dt.time.min, ZoneInfo(tz)).astimezone(dt.timezone.utc)
                with st.spinner("Scoring today's finished games…"):
                    try:
                        _df, _sum, _rep = wd.day_scorecard(chosen_, lo_, dt.datetime.now(dt.timezone.utc), min_cash=500, tz=tz,
                                                                  market_types=("moneyline", "spread", "total"))
                        st.session_state["day_banner"] = {"day": _today, "sports": list(_sports_now), "summary": _sum,
                                                          "at": dt.datetime.now(dt.timezone.utc)}
                        st.rerun()
                    except Exception as e:
                        st.error(f"Day record failed: {e}")

        sports = sorted({l["sport"] for l in leagues})
        with st.form("scan_form"):
            s1, s2 = st.columns(2)
            pick_sports = s1.multiselect("Sports", sports, default=[x for x in sports if x in ("Basketball", "Football", "Hockey", "Tennis")] or sports[:1], key="scan_sports")
            league_opts = [l for l in leagues if l["sport"] in pick_sports] if pick_sports else leagues
            pick_leagues = s2.multiselect("Leagues (blank = all in the chosen sports)",
                                          [f"{l['sport']} · {l['league']}" for l in league_opts])
            st.caption("Leagues are split out under each sport (e.g. Basketball · NBA, NCAAM, WNBA; Tennis · ATP, WTA).")
            a1, a2, a3, a4 = st.columns(4)
            ahead = a1.slider("Show games starting in the next (hours)", 1, 72, 24)
            back = a2.slider("Also show games that started up to (hours ago)", 0, 12, 6)
            depth = a3.slider("Holder depth (pages of 100/side)", 1, 10, 1)
            max_games = a4.slider("Max games", 5, 100, 100)
            type_labels = st.multiselect("Bet types", ["Moneyline", "Spread", "Over/Under"], default=["Moneyline", "Spread", "Over/Under"],
                                         help="Spread and Over/Under use each game's main (busiest) line.")
            market_types = tuple({"Moneyline": "moneyline", "Spread": "spread", "Over/Under": "total"}[t]
                                 for t in type_labels) or ("moneyline",)
            status_label = st.radio("Games to include", ["All games", "Live only", "Exclude live (not started yet)"],
                                    horizontal=True)
            status = {"All games": "all", "Live only": "live"}.get(status_label, "upcoming")
            d1, d2 = st.columns(2)
            deepen = d1.checkbox("Take a closer look at the biggest games", True,
                                 help="After the quick scan, re-reads the games with the most money on them, "
                                      "counting far more holders so their wallet counts and cash totals are fuller.")
            deepen_n = d2.slider("How many of the biggest games", 1, 15, 5)
            run = st.form_submit_button("Scan", type="primary")

        if run:
            chosen = [l for l in league_opts if not pick_leagues or f"{l['sport']} · {l['league']}" in pick_leagues]
            if not chosen:
                st.warning("Pick at least one sport.")
            else:
                bar = st.progress(0.0, text="Starting…")

                def prog(done, total, text):
                    bar.progress(min(done / max(total, 1), 1.0), text=f"{text}: {done}/{total}")

                try:
                    df, items, report = wd.scan_games(chosen, hours_ahead=ahead, hours_back=back, depth_pages=depth,
                                                      min_shares=min_shares, max_games=max_games, tz=tz, progress=prog,
                                                      deepen_top=deepen_n if deepen else 0, status=status, hide_decided=hide_decided,
                                                      market_types=market_types)
                    st.session_state["scan"] = (df, items, report, dt.datetime.now(dt.timezone.utc))
                except Exception as e:
                    st.error(f"Scan failed: {e}")
                bar.empty()

        scan = st.session_state.get("scan")
        if scan:
            df, items, report, when = scan
            st.caption(f"Scanned {report['leagues']} leagues · {report['events']} games found · "
                       f"{report['analyzed']} analysed (of {report['markets']} markets) · {wd.fmt_local(when, tz)}")
            if report["markets"] > report["analyzed"] + len(report["errors"]) and report["markets"] > 0:
                st.caption("Raise “Max games” to cover the rest; games with no holders yet are skipped.")
            if not df.empty:
                k1, k2 = st.columns(2)
                highlight = k1.checkbox("Highlight plays in yellow", True)
                reverse = k2.checkbox("Reverse the line rule (line at or below cash %)", False,
                                      help="Off: line % must be at least the cash %. On: line % must be at or below the cash %, "
                                           "i.e. the price is lower than the money on that side implies.")
                use_sharp = st.checkbox("Pick using wallet track records on plays (slower)", True,
                                        help="For each yellow play, checks the biggest holders on both sides and picks the side "
                                             "with more of its cash coming from wallets that have been profitable recently. "
                                             "Off: the pick is simply the side with the most cash.")
                with st.expander("Play rules (yellow rows)", expanded=False):
                    r1, r2 = st.columns(2)
                    wmin = r1.number_input("Wallets % from", 0, 100, 40)
                    wmax = r2.number_input("Wallets % to", 0, 100, 49)
                    st.caption("A side is a play when: its wallets % is in that range, AND its cash % is higher than its "
                               "wallets %, AND the line % is " + ("at most" if reverse else "at least") + " its cash %.")
                def row_is_play(r):
                    one = [{"wallets_pct": r["Wallets %"], "cash_pct": r["Cash %"], "line_pct": r["Line %"]}]
                    return bool(highlight and find_play(one, wmin, wmax, reverse))
                df["Play"] = ["✓" if row_is_play(r) else "" for _, r in df.iterrows()]
                # Pick on play rows: by wallet track record (profitable-cash share) or, if off, by most cash
                df["Money pick"] = ""
                df["Rating"] = ""
                df["Profitable cash %"] = None
                df["Wallets W / L"] = ""
                play_games = {}
                for idx, r in df.iterrows():
                    if r["Play"]:
                        play_games.setdefault((r["Game"], r["Market"]), idx)
                sharp = {}
                if use_sharp and play_games:
                    from concurrent.futures import ThreadPoolExecutor
                    with st.spinner(f"Checking wallet track records for {len(play_games)} play game(s)…"):
                        def one(kv):
                            try:
                                return kv[0], c_sharp(items[kv[1]]["market"], 10)
                            except Exception:
                                return kv[0], {}
                        with ThreadPoolExecutor(4) as ex:
                            sharp = dict(ex.map(one, play_games.items()))
                for key, idx in play_games.items():
                    rows_g = df[(df["Game"] == key[0]) & (df["Market"] == key[1])]
                    if use_sharp:
                        info = sharp.get(key, {})
                        for j, r in rows_g.iterrows():
                            d = info.get(r["Side"])
                            if d:
                                df.at[j, "Profitable cash %"] = d["pct"]
                                df.at[j, "Wallets W / L"] = f"{d['profitable']} / {d['losing']}"
                        known = {sd: d["pct"] for sd, d in info.items() if d.get("pct") is not None}
                        pick = max(known, key=known.get) if known else rows_g.loc[rows_g["Cash $"].idxmax(), "Side"]
                        pick_note = "" if known else " *"      # * = no wallet history, so picked by most cash
                    else:
                        pick = rows_g.loc[rows_g["Cash $"].idxmax(), "Side"]
                        pick_note = ""
                    # rating (out of 5) for the picked side: share of checks it passes
                    prow = rows_g[rows_g["Side"] == pick]
                    prow = prow.iloc[0] if len(prow) else rows_g[rows_g["Play"] != ""].iloc[0]
                    # four checks; ones that can't be evaluated (no track record) count as not passed
                    pc = df.at[prow.name, "Profitable cash %"]
                    others = [x for x in rows_g.loc[rows_g.index != prow.name, "Profitable cash %"] if pd.notna(x)]
                    checks = [
                        prow["Cash %"] - prow["Wallets %"] >= 3,
                        prow["Cash $"] >= 1000,
                        bool(use_sharp and pd.notna(pc) and pc >= 55),
                        bool(use_sharp and pd.notna(pc) and others and pc - max(others) >= 20),
                    ]
                    stars = int(5 * sum(checks) / 4 + 0.5)
                    for j, r in rows_g.iterrows():
                        if r["Play"]:
                            df.at[j, "Money pick"] = pick + pick_note
                            df.at[j, "Rating"] = "★" * stars + "☆" * (5 - stars)
            if df.empty:
                st.warning("No games with holder data in that window. Widen the hours or pick more leagues.")
            else:
                f1, f2, f3 = st.columns(3)
                only_div = f1.checkbox("Only crowd ≠ money")
                only_plays = f1.checkbox("Only plays (yellow)")
                min_cash = f2.number_input("Min total cash $", 0, 10_000_000, 0, 1000)
                sort_by = f3.selectbox("Sort by", ["Live first, then cash", "Biggest |cash − line|", "Most cash",
                                                   "Cash %", "Wallets %"])
                v = df.copy()
                v["_i"] = range(len(v))
                if only_plays:
                    v = v[v["Play"] != ""]
                if only_div:
                    v = v[v["Crowd ≠ money"]]
                v = v[v["Total cash $"] >= min_cash]
                if sort_by == "Biggest |cash − line|":
                    v = v.reindex(v["Cash − Line (pts)"].abs().sort_values(ascending=False).index)
                elif sort_by == "Most cash":
                    v = v.sort_values("Total cash $", ascending=False)
                elif sort_by == "Cash %":
                    v = v.sort_values("Cash %", ascending=False)
                elif sort_by == "Wallets %":
                    v = v.sort_values("Wallets %", ascending=False)

                cols = ["Status", "Sport", "League", "Game", "Market", "Starts", "Side", "Play", "Money pick", "Rating", "Profitable cash %", "Wallets W / L", "Line %", "Wallets %",
                        "Cash %", "Cash − Line (pts)", "Cash $", "Value now $", "Wallets", "Total cash $",
                        "Crowd side", "Crowd ≠ money", "Partial count"]
                view = st.radio("Table view", ["Interactive (sortable, pinned columns)", "Scrollbar on top"],
                                horizontal=True, label_visibility="collapsed")
                if view.startswith("Scrollbar"):
                    top_scroll_table(v[cols])
                else:
                  st.dataframe(v[cols].style.apply(live_row, axis=1).map(gap_color, subset=["Cash − Line (pts)"]), hide_index=True,
                               use_container_width=True, height=min(80 + 36 * len(v), 700), column_config={
                                   "Status": st.column_config.TextColumn(pinned=True),
                                   "Game": st.column_config.TextColumn(pinned=True),
                                   "Line %": PCT, "Wallets %": PCT, "Cash %": PCT, "Cash − Line (pts)": PTS,
                                                                      "Cash $": MONEY, "Profitable cash %": PCT, "Total cash $": MONEY, "Value now $": MONEY, "Wallets": INT})
                st.caption("Yellow rows are plays, gray rows are live. Each game has one row per side. Money pick (yellow rows; * = no wallet history was available, so it is the side with the most cash) = the side with more of its biggest holders’ cash coming from recently profitable wallets (or the side with the most cash if track records are off). Wallets W / L = profitable / losing wallets among the top holders. Rating (out of 5) = share of these checks the pick passes: cash % at least 3 points above wallets %; at least $1,000 cash in on that side; at least 55% of its checked cash from profitable wallets; and at least 20 points more profitable-cash share than the other side (the last two need track records on; checks that can\u2019t be evaluated count as not passed, so with track records off the most a play can get is 3 stars). It describes where the money is, not a recommendation. Cash − Line = its share of cash minus the line's "
                           "implied probability (positive: more money than the price implies). "
                           "“Partial count” = holder depth cut off, so wallet counts are lower bounds.")
                st.download_button("Download scan (CSV)", v[cols].to_csv(index=False), "scan.csv", "text/csv")

                if not v.empty:
                    pick = st.selectbox("Open a game in the analyzer", v["_i"],
                                        format_func=lambda i: f"{df.loc[i, 'Game']} · {df.loc[i, 'Side']} ({df.loc[i, 'Market']})")
                    if st.button("Open full analysis"):
                        st.session_state["from_scan"] = items[pick]["market"]
                        st.success("Loaded. Switch to the “Analyze a game” tab.")
            if report.get("debug"):
                with st.expander("Why each game was counted as live or upcoming (send me this if a game is wrong)"):
                    st.dataframe(pd.DataFrame(report["debug"]), hide_index=True, use_container_width=True)
            if report["errors"]:
                with st.expander(f"Diagnostics ({len(report['errors'])} errors)"):
                    for e in report["errors"][:50]:
                        st.text(e)



# =====================================================================
# TAB 3 -- day scorecard: money-side day or crowd-side day?
# =====================================================================
with tab_day:
    st.caption("For a day's finished games: rebuilds who held what just before each game started, then checks whether "
               "the side with the most cash (money side) or the side with the most wallets (crowd side) won. "
               "Moneyline, spread and over/under markets with two sides (soccer's three-way Yes/No markets are skipped). Positions are rebuilt from trade history, so they are close, not exact.")
    try:
        day_leagues = c_leagues()
    except Exception as e:
        day_leagues = []
        st.error(f"Couldn't load the league list: {e}")

    if day_leagues:
        from zoneinfo import ZoneInfo
        today_local = dt.datetime.now(ZoneInfo(tz)).date()
        with st.form("day_form"):
            d1, d2, d3 = st.columns([1, 2, 1])
            day = d1.date_input("Day", today_local)
            day_sports = sorted({l["sport"] for l in day_leagues})
            pick_day_sports = d2.multiselect(
                "Sports", day_sports, key="day_sports",
                default=[x for x in day_sports if x in ("Basketball", "Football", "Hockey", "Tennis")] or day_sports[:1])
            min_pre = d3.number_input("Min pre-game cash $", 0, 10_000_000, 500, 100)
            day_type_labels = st.multiselect("Bet types", ["Moneyline", "Spread", "Over/Under"],
                                             default=["Moneyline", "Spread", "Over/Under"], key="day_types",
                                             help="Spread and Over/Under use each game's main (busiest) line.")
            day_types = tuple({"Moneyline": "moneyline", "Spread": "spread", "Over/Under": "total"}[t]
                              for t in day_type_labels) or ("moneyline",)
            run_day = st.form_submit_button("Score the day", type="primary")

        if run_day:
            chosen = [l for l in day_leagues if l["sport"] in pick_day_sports]
            if not chosen:
                st.warning("Pick at least one sport.")
            else:
                tzinfo = ZoneInfo(tz)
                lo = dt.datetime.combine(day, dt.time.min, tzinfo).astimezone(dt.timezone.utc)
                hi = lo + dt.timedelta(days=1)
                bar = st.progress(0.0, text="Starting…")

                def prog_day(done, total, text):
                    bar.progress(min(done / max(total, 1), 1.0), text=f"{text}: {done}/{total}")

                try:
                    st.session_state["day_result"] = (*wd.day_scorecard(chosen, lo, hi, min_cash=min_pre, tz=tz, market_types=day_types,
                                                                        progress=prog_day), day)
                except Exception as e:
                    st.error(f"Scorecard failed: {e}")
                bar.empty()

        res_day = st.session_state.get("day_result")
        if res_day:
            ddf, dsum, drep, dday = res_day
            st.caption(f"{dday} · {drep['leagues']} leagues · {drep['events']} finished games found · "
                       f"{drep['scored']} scored")
            if ddf.empty:
                st.warning("No finished games could be scored for that day. Try another day or more sports, or lower the "
                           "minimum cash.")
            else:
                st.subheader(dsum["verdict"])
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Money side won", f"{dsum['money_win_rate']:.0f}%", f"{dsum['money_won_split']} of {dsum['split_games']} split games", delta_color="off")
                m2.metric("Crowd side won", f"{dsum['crowd_win_rate']:.0f}%", f"{dsum['crowd_won_split']} of {dsum['split_games']} split games", delta_color="off")
                m3.metric("Favorite won", f"{dsum['favorite_win_rate']:.0f}%" if dsum["favorite_win_rate"] is not None else "—")
                m4.metric("Games scored", dsum["games"])
                st.caption("The big percentages cover every scored game. The “split games” counts are games where the money "
                           "side and crowd side were different teams. Those are what decide the verdict. "
                           "A single day is a small sample, so treat it as a description, not a trend.")
                show = ddf.drop(columns=["_start"])

                def day_style(row):
                    if row["Money ≠ crowd"]:
                        return ["background-color: rgba(46,160,67,0.25)" if row["Money won"] else
                                "background-color: rgba(70,130,220,0.25)"] * len(row)
                    return [""] * len(row)

                st.dataframe(show.style.apply(day_style, axis=1), hide_index=True, use_container_width=True,
                             column_config={"Pre-game cash $": MONEY, "Money cash %": PCT, "Crowd wallets %": PCT,
                                            "Favorite line %": PCT})
                st.caption("Green rows: money and crowd disagreed and the money side won. Blue rows: they disagreed and "
                           "the crowd side won. Plain rows: both pointed at the same team. “Partial trades” = the trade "
                           "history was cut off, so that game's pre-game split is incomplete.")
                st.download_button("Download scorecard (CSV)", show.to_csv(index=False), "day_scorecard.csv", "text/csv")
        if res_day and res_day[2]["errors"]:
            with st.expander(f"Diagnostics ({len(res_day[2]['errors'])} errors)"):
                for e in res_day[2]["errors"][:50]:
                    st.text(e)
