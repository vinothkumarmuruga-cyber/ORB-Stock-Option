import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except ImportError:
    AUTOREFRESH_AVAILABLE = False

INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="ORB 1HR Scanner", layout="wide")

# ---------------------------------------------------------------------
# Tighten top spacing so the page starts higher / uses space better
# ---------------------------------------------------------------------
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1.2rem;
            padding-bottom: 1rem;
        }
        div[data-testid="stVerticalBlock"] > div:has(> .element-container) {
            gap: 0.4rem;
        }
    </style>
    """,
    unsafe_allow_html=True
)


# ---------------------------------------------------------------------
# Sidebar UI
# ---------------------------------------------------------------------
def render_sidebar():
    st.sidebar.header("Configuration")

    access_token = st.sidebar.text_input(
        "Upstox Access Token",
        type="password",
        value=st.session_state.get("access_token", ""),
        key="access_token"
    )

    st.sidebar.divider()
    st.sidebar.header("Expiry Settings")

    expiry_choice = st.sidebar.radio(
        "Select Expiry Month",
        options=["Current Month", "Next Month"],
        index=0,
        key="expiry_choice",
        help="Choose whether to scan the current month's or next month's F&O expiry."
    )

    st.sidebar.divider()
    st.sidebar.header("Auto Refresh")

    auto_refresh_enabled = st.sidebar.checkbox(
        "Enable Auto-Refresh",
        value=st.session_state.get("auto_refresh_enabled", False),
        key="auto_refresh_enabled"
    )

    refresh_interval = st.sidebar.slider(
        "Refresh Interval (seconds)",
        min_value=1,
        max_value=60,
        value=st.session_state.get("refresh_interval", 15),
        step=1,
        key="refresh_interval"
    )

    if auto_refresh_enabled and not AUTOREFRESH_AVAILABLE:
        st.sidebar.error(
            "Auto-refresh needs the `streamlit-autorefresh` package.\n\n"
            "Add `streamlit-autorefresh` to requirements.txt and redeploy."
        )

    # -------------------------------------------------------------
    # Telegram Alerts
    # -------------------------------------------------------------
    st.sidebar.divider()
    st.sidebar.header("Telegram Alerts")

    telegram_enabled = st.sidebar.checkbox(
        "Enable Trigger-Cross Alerts",
        value=st.session_state.get("telegram_enabled", False),
        key="telegram_enabled",
        help="Sends a Telegram message the moment an option's live price crosses its Trigger (first 1-hour candle high)."
    )

    telegram_bot_token = st.sidebar.text_input(
        "Bot Token",
        type="password",
        value=st.session_state.get("telegram_bot_token", ""),
        key="telegram_bot_token",
        help="Create a bot via @BotFather on Telegram to get this token."
    )

    telegram_chat_id = st.sidebar.text_input(
        "Chat ID",
        value=st.session_state.get("telegram_chat_id", ""),
        key="telegram_chat_id",
        help="Your personal or group chat ID. Message @userinfobot to find yours."
    )

    tg_col1, tg_col2 = st.sidebar.columns(2)
    test_telegram_clicked = tg_col1.button("Send Test", use_container_width=True)
    reset_alert_state_clicked = tg_col2.button("Reset Alerts", use_container_width=True)

    st.sidebar.divider()
    st.sidebar.header("Data Management")

    refresh_clicked = st.sidebar.button("⚡ Refresh LTP Now", use_container_width=True)

    st.sidebar.subheader("NSE Instrument JSON")
    download_clicked = st.sidebar.button("⬇️ Download Latest", use_container_width=True)

    return (
        access_token,
        expiry_choice,
        refresh_clicked,
        download_clicked,
        auto_refresh_enabled,
        refresh_interval,
        telegram_enabled,
        telegram_bot_token,
        telegram_chat_id,
        test_telegram_clicked,
        reset_alert_state_clicked
    )


# ---------------------------------------------------------------------
# Instrument / expiry helpers
# ---------------------------------------------------------------------
def normalize_expiry(series):
    return pd.to_datetime(
        pd.to_numeric(series, errors="coerce"),
        unit="ms",
        errors="coerce"
    ).dt.date


def chunk_list(items, size=300):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


@st.cache_data(ttl=3600, show_spinner="Loading instrument file...")
def load_nse_fo_instruments():
    instruments = pd.read_json(INSTRUMENT_URL, compression="gzip")
    instruments["expiry_date"] = normalize_expiry(instruments["expiry"])

    futures = instruments[
        (instruments["segment"] == "NSE_FO") &
        (instruments["instrument_type"] == "FUT") &
        (instruments["underlying_type"] == "EQUITY")
    ].copy()

    options = instruments[
        (instruments["segment"] == "NSE_FO") &
        (instruments["instrument_type"].isin(["CE", "PE"])) &
        (instruments["underlying_type"] == "EQUITY")
    ].copy()

    return futures, options


def get_expiry_for_choice(df, choice):
    today = date.today()
    valid = sorted(df[df["expiry_date"] >= today]["expiry_date"].unique())

    if not valid:
        return None

    if choice == "Current Month":
        return valid[0]

    current_month = today.month
    for exp in valid:
        if exp.month != current_month:
            return exp

    return valid[-1] if len(valid) > 1 else valid[0]


# ---------------------------------------------------------------------
# Quote fetchers (live data - refreshed every cycle)
# ---------------------------------------------------------------------
def fetch_future_open_v3(instrument_keys, headers):
    """Fetch today's live OHLC (used only for the future's Open price).
    Returns (dataframe, raw_sample) where raw_sample is a small slice of
    the raw API response for diagnostics if open prices come back empty.
    """
    url = "https://api.upstox.com/v3/market-quote/ohlc"
    rows = []
    raw_sample = None

    for keys in chunk_list(instrument_keys):
        params = {"instrument_key": ",".join(keys), "interval": "1d"}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception as e:
            st.warning(f"OHLC request error: {e}")
            continue

        if response.status_code != 200:
            st.warning(f"OHLC Error {response.status_code}: {response.text[:300]}")
            continue

        try:
            payload = response.json()
            data = payload.get("data", {})
        except Exception as e:
            st.warning(f"Invalid OHLC response: {e}")
            continue

        if raw_sample is None:
            # keep first couple of entries verbatim for diagnostics
            raw_sample = dict(list(data.items())[:2])

        for response_key, item in data.items():
            if not isinstance(item, dict):
                continue

            live = item.get("live_ohlc") or item.get("ohlc") or {}
            prev = item.get("prev_ohlc") or {}

            # IMPORTANT: Upstox keys this response dict by trading symbol
            # (e.g. "NSE_FO:SBIN26AUGFUT"), NOT by instrument_key. The
            # actual instrument_key (pipe format, e.g. "NSE_FO|58382")
            # that matches the instrument master is nested inside the
            # item itself as "instrument_token". Using response_key was
            # causing every merge to silently fail (future_open -> NaN)
            # even though Upstox was returning valid open prices.
            true_key = item.get("instrument_token") or response_key

            # Prefer live_ohlc.open; fall back to prev_ohlc.close, then
            # last_price, so a partially-populated response doesn't
            # zero out the whole row.
            open_price = (
                live.get("open")
                if live.get("open") not in (None, 0)
                else prev.get("close") if prev.get("close") not in (None, 0)
                else item.get("last_price")
            )

            rows.append({
                "instrument_key": true_key,
                "future_open": open_price,
                "future_ltp": item.get("last_price"),
            })

    return pd.DataFrame(rows), raw_sample


def fetch_ltp_v3(instrument_keys, headers):
    url = "https://api.upstox.com/v3/market-quote/ltp"
    rows = []

    for keys in chunk_list(instrument_keys):
        params = {"instrument_key": ",".join(keys)}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception as e:
            st.warning(f"LTP request error: {e}")
            continue

        if response.status_code != 200:
            st.warning(f"LTP Error {response.status_code}: {response.text[:300]}")
            continue

        try:
            data = response.json().get("data", {})
        except Exception:
            continue

        for response_key, item in data.items():
            if not isinstance(item, dict):
                continue

            # Same defensive fix as OHLC: prefer the instrument_token
            # nested in the payload over the dict key, in case this
            # endpoint also keys by trading symbol for some instruments.
            true_key = item.get("instrument_token") or response_key

            rows.append({
                "instrument_key": true_key,
                "ltp": item.get("last_price"),
                "prev_close": item.get("cp"),
                "volume": item.get("volume"),
            })

    return pd.DataFrame(rows)


def nearest_option(options_df, underlying_key, expiry, option_type, future_open):
    chain = options_df[
        (options_df["underlying_key"] == underlying_key) &
        (options_df["expiry_date"] == expiry) &
        (options_df["instrument_type"] == option_type)
    ].copy()

    if chain.empty or pd.isna(future_open):
        return None

    chain["strike_diff"] = (chain["strike_price"] - future_open).abs()
    return chain.sort_values("strike_diff").iloc[0]


# ---------------------------------------------------------------------
# ORB (Opening Range Breakout) — 1-hour candle logic
#
#   Trigger = High of the FIRST 1-hour candle of the trading day
#             (e.g. 9:15-10:15 IST). This locks in once that first
#             hourly candle closes; before that it reflects the
#             developing high of the opening hour.
#
#   TGT = Trigger, SL = Trigger — both equal to the Trigger itself.
#         No multiplier, no separate levels.
#
#   No first-hour range filter, no confirming-candle condition.
#   Every strike that has a valid first-hour high gets a Trigger.
#
#   Breakout ("crossed") = live LTP >= Trigger, checked directly
#   against the live price every refresh (computed in the main
#   scanner below, not here).
#
# Uses Upstox's intraday historical-candle endpoint (unit=hours,
# interval=1), which returns today's hourly candles including the
# currently-forming one, purely to read off the first hour's high.
# Cached for a short 15s TTL per refresh cycle so repeated
# auto-refreshes don't hammer the API.
# ---------------------------------------------------------------------
def _fetch_single_orb_data(instrument_key, headers):
    safe_key = quote(instrument_key, safe="|")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{safe_key}/hours/1"

    try:
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            return instrument_key, None, f"HTTP {response.status_code}: {response.text[:200]}"

        payload = response.json()
        candles = payload.get("data", {}).get("candles", [])

        if not candles:
            return instrument_key, None, "No intraday hourly candles yet today"

        # Each candle: [timestamp, open, high, low, close, volume, oi].
        # Upstox returns these newest-first; sort chronologically so
        # index 0 is genuinely the first (opening) hour of the day.
        candles_sorted = sorted(candles, key=lambda c: c[0])

        first_hour = candles_sorted[0]
        first_hour_high = first_hour[2]
        trigger = first_hour_high  # high of the first 1-hour candle

        if trigger in (None, 0):
            return instrument_key, None, "First-hour candle has no valid high"

        info = {
            "trigger": trigger,
            "tgt": trigger,
            "sl": trigger,
            "first_hour_complete": len(candles_sorted) >= 2,
        }
        return instrument_key, info, None

    except Exception as e:
        return instrument_key, None, f"Exception: {e}"


@st.cache_data(ttl=15, show_spinner="Computing ORB trigger levels...")
def fetch_orb_map(instrument_keys, headers_tuple):
    headers = dict(headers_tuple)
    result = {}
    sample_errors = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_fetch_single_orb_data, key, headers): key
            for key in instrument_keys
        }

        for future in as_completed(futures):
            key, info, error = future.result()
            result[key] = info

            if error and len(sample_errors) < 5:
                sample_errors.append(f"{key} -> {error}")

    return result, sample_errors


# ---------------------------------------------------------------------
# Telegram Alerts
#
# Fires once per symbol, the moment its live price crosses Trigger
# (LTP >= Trigger). State is tracked in st.session_state so it
# doesn't re-alert on every refresh.
# ---------------------------------------------------------------------
def send_telegram_alert(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        return False, "Missing bot token or chat ID"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return False, f"Exception: {e}"


def check_and_alert_orb_breakouts(df, bot_token, chat_id):
    if df.empty:
        return

    alerted = st.session_state.setdefault("orb_alert_state", set())
    newly_confirmed = []

    for _, row in df.iterrows():
        symbol = row["Symbol"]

        if row["Breakout"] == "✅" and symbol not in alerted:
            newly_confirmed.append(row)
            alerted.add(symbol)

    if not newly_confirmed:
        return

    message_lines = ["🚀 <b>Trigger Crossed</b>"]
    for row in newly_confirmed:
        message_lines.append(
            f"\n<b>{row['Symbol']}</b>\n"
            f"LTP: {row['LTP']:.2f}  ›  Trigger: {row['Trigger']:.2f}"
        )

    message = "\n".join(message_lines)
    success, error = send_telegram_alert(bot_token, chat_id, message)

    if success:
        st.sidebar.success(f"Telegram alert sent for {len(newly_confirmed)} trigger cross(es).")
    else:
        st.sidebar.warning(f"Telegram alert failed: {error}")


# ---------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------
def build_open_strike_scanner(access_token, expiry_choice, top_n=20):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    futures, options = load_nse_fo_instruments()
    expiry = get_expiry_for_choice(futures, expiry_choice)

    if expiry is None:
        st.error("No futures expiry found")
        return pd.DataFrame(), pd.DataFrame()

    futures = futures[futures["expiry_date"] == expiry].copy()
    options = options[options["expiry_date"] == expiry].copy()

    st.caption(f"Expiry: {expiry}  |  Stock futures: {len(futures)}")

    # -----------------------------------------------------------------
    # Futures Open (for ATM strike selection)
    # -----------------------------------------------------------------
    fut_quotes, ohlc_raw_sample = fetch_future_open_v3(futures["instrument_key"].tolist(), headers)

    if fut_quotes.empty:
        st.error("No futures open data received")
        return pd.DataFrame(), pd.DataFrame()

    futures = futures.merge(fut_quotes, on="instrument_key", how="left")
    futures = futures.dropna(subset=["future_open"])

    if futures.empty:
        st.error(
            "All futures were dropped after the Open-price fetch "
            "(future_open came back empty/NaN for every instrument)."
        )
        with st.expander("⚠️ Raw OHLC API response sample — diagnostics", expanded=True):
            st.write(
                "This is exactly what Upstox returned for the first couple "
                "of futures instruments requested:"
            )
            st.json(ohlc_raw_sample if ohlc_raw_sample else {"note": "response 'data' was empty"})
        return pd.DataFrame(), pd.DataFrame()

    # -----------------------------------------------------------------
    # Select nearest CE/PE for each future
    # -----------------------------------------------------------------
    selected_rows = []

    for _, fut in futures.iterrows():
        ce = nearest_option(options, fut["underlying_key"], expiry, "CE", fut["future_open"])
        pe = nearest_option(options, fut["underlying_key"], expiry, "PE", fut["future_open"])

        for opt in [ce, pe]:
            if opt is None:
                continue

            selected_rows.append({
                "underlying_symbol": fut["underlying_symbol"],
                "strike": opt["strike_price"],
                "option_type": opt["instrument_type"],
                "option_key": opt["instrument_key"],
                "Open": fut["future_open"],
                "Lot": opt["lot_size"]
            })

    selected = pd.DataFrame(selected_rows)

    if selected.empty:
        # Diagnostics: figure out WHY nothing matched instead of
        # just saying "No CE/PE options found" with no way to tell why.
        fut_keys = set(futures["underlying_key"].dropna().unique())
        opt_keys = set(options["underlying_key"].dropna().unique())
        overlap = fut_keys & opt_keys

        st.error("No CE/PE options found")
        with st.expander("⚠️ Why no options matched — diagnostics", expanded=True):
            st.write(f"Futures rows (with valid Open): {len(futures)}")
            st.write(f"Options rows for this expiry: {len(options)}")
            st.write(f"Distinct underlying_key in futures: {len(fut_keys)}")
            st.write(f"Distinct underlying_key in options: {len(opt_keys)}")
            st.write(f"Overlapping underlying_key between the two: {len(overlap)}")
            st.write("Sample futures underlying_key values:", sorted(list(fut_keys))[:5])
            st.write("Sample options underlying_key values:", sorted(list(opt_keys))[:5])
            if len(overlap) == 0 and fut_keys and opt_keys:
                st.write(
                    "**No overlap at all** — the two dataframes are keyed "
                    "differently (e.g. one is empty, or the underlying_key "
                    "format differs). This is the actual bug to chase."
                )
        return pd.DataFrame(), pd.DataFrame()

    # -----------------------------------------------------------------
    # Option LTP (live, every refresh)
    # -----------------------------------------------------------------
    option_quotes = fetch_ltp_v3(selected["option_key"].tolist(), headers)

    if option_quotes.empty:
        st.error("No option quote data received")
        return pd.DataFrame(), pd.DataFrame()

    option_quotes = option_quotes.drop_duplicates("instrument_key")

    selected = selected.merge(
        option_quotes,
        left_on="option_key",
        right_on="instrument_key",
        how="left"
    )
    selected = selected.drop(columns=["instrument_key"])

    # -----------------------------------------------------------------
    # Symbol / Chg% / Ctr / Capital — computed first, since ranking by
    # Chg% only needs LTP data (already have it), not ORB levels.
    # -----------------------------------------------------------------
    selected["Symbol"] = (
        selected["underlying_symbol"].astype(str)
        + " "
        + selected["strike"].astype(int).astype(str)
        + " "
        + selected["option_type"].astype(str)
    )

    selected["Chg%"] = np.where(
        selected["prev_close"] > 0,
        ((selected["ltp"] - selected["prev_close"]) / selected["prev_close"]) * 100,
        np.nan
    )

    selected["Ctr"] = np.where(
        selected["Lot"] > 0,
        selected["volume"] / selected["Lot"],
        np.nan
    )

    selected["Capital"] = selected["ltp"] * selected["Lot"]

    selected = selected.rename(columns={
        "ltp": "LTP",
        "volume": "Vol",
        "Capital": "Capital Required"
    })

    # -----------------------------------------------------------------
    # Rank down to only what will actually be shown (top_n CE + top_n
    # PE) BEFORE fetching ORB levels. The intraday candle endpoint is
    # one request per instrument — fetching it for all ~400 candidates
    # when only ~40 get displayed would trip Upstox's rate limits.
    # -----------------------------------------------------------------
    ce_candidates = (
        selected[selected["Symbol"].str.endswith("CE")]
        .sort_values("Chg%", ascending=False)
        .head(top_n)
    )
    pe_candidates = (
        selected[selected["Symbol"].str.endswith("PE")]
        .sort_values("Chg%", ascending=False)
        .head(top_n)
    )
    shortlisted = pd.concat([ce_candidates, pe_candidates], ignore_index=True)

    # -----------------------------------------------------------------
    # ORB Trigger — first-hour High. TGT and SL are simply set equal
    # to Trigger (no separate levels, no filters).
    # -----------------------------------------------------------------
    orb_map, orb_errors = fetch_orb_map(
        tuple(sorted(shortlisted["option_key"].unique())),
        tuple(headers.items())
    )

    def _orb_field(option_key, field, default=None):
        info = orb_map.get(option_key)
        return info.get(field, default) if info else default

    shortlisted["Trigger"] = shortlisted["option_key"].apply(lambda k: _orb_field(k, "trigger"))
    shortlisted["TGT"] = shortlisted["option_key"].apply(lambda k: _orb_field(k, "tgt"))
    shortlisted["SL"] = shortlisted["option_key"].apply(lambda k: _orb_field(k, "sl"))

    shortlisted["Trigger"] = pd.to_numeric(shortlisted["Trigger"], errors="coerce")

    missing_count = shortlisted["Trigger"].isna().sum()
    total_count = len(shortlisted)

    if missing_count > 0:
        with st.expander(
            f"⚠️ ORB Trigger not yet available for {missing_count}/{total_count} options — click for details",
            expanded=(missing_count == total_count)
        ):
            st.write(
                "This is normal before the first hourly candle (9:15-10:15 IST) "
                "has any data, or if Upstox hasn't returned candles yet."
            )
            if orb_errors:
                st.write("Sample errors from the intraday candle API:")
                for err in orb_errors:
                    st.code(err)

    # -----------------------------------------------------------------
    # Breakout ("crossed") = live LTP >= Trigger. No hourly-candle
    # confirmation, no range filter — pure price cross, checked live.
    # -----------------------------------------------------------------
    shortlisted["Breakout"] = np.where(
        (shortlisted["Trigger"].notna()) & (shortlisted["LTP"] >= shortlisted["Trigger"]),
        "✅",
        "—"
    )

    shortlisted["Away %"] = np.where(
        shortlisted["Trigger"] > 0,
        (shortlisted["LTP"] / shortlisted["Trigger"]) * 100,
        np.nan
    )
    shortlisted["Away %"] = shortlisted["Away %"].clip(lower=0)

    # -----------------------------------------------------------------
    # Final column order: Symbol, Open, LTP, Trigger, Away%, TGT, SL,
    # Breakout, Vol, Lot, Capital Required.
    # Chg% and Ctr are still computed above (Chg% drives the top_n
    # shortlist ranking) but are intentionally left out of the table
    # that's actually shown to the user.
    # -----------------------------------------------------------------
    result = shortlisted[[
        "Symbol",
        "Open",
        "LTP",
        "Trigger",
        "Away %",
        "TGT",
        "SL",
        "Breakout",
        "Vol",
        "Lot",
        "Capital Required"
    ]].copy()

    for col in ["Open", "Trigger", "TGT", "SL", "LTP", "Away %"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(2)

    result["Vol"] = pd.to_numeric(result["Vol"], errors="coerce").fillna(0).astype(int)
    result["Lot"] = pd.to_numeric(result["Lot"], errors="coerce").fillna(0).astype(int)
    result["Capital Required"] = pd.to_numeric(
        result["Capital Required"], errors="coerce"
    ).round(0).fillna(0).astype(int)

    ce_table = (
        result[result["Symbol"].str.endswith("CE")]
        .sort_values("Away %", ascending=False, na_position="last")
        .reset_index(drop=True)
    )

    pe_table = (
        result[result["Symbol"].str.endswith("PE")]
        .sort_values("Away %", ascending=False, na_position="last")
        .reset_index(drop=True)
    )

    return ce_table, pe_table


# ---------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------
def table_height(df, row_px=35, header_px=38, max_px=900):
    return min(header_px + row_px * max(len(df), 1) + 3, max_px)


DECIMAL_COLS = {
    "Open": "{:.2f}",
    "Trigger": "{:.2f}",
    "TGT": "{:.2f}",
    "SL": "{:.2f}",
    "LTP": "{:.2f}",
    "Away %": "{:.2f}%",
}


def style_away_percent(value):
    try:
        value = float(value)
        if value >= 100:
            return "background-color: darkgreen; color: white; font-weight: bold;"
        elif value >= 90:
            return "background-color: lightgreen; color: black; font-weight: bold;"
    except Exception:
        pass
    return ""


def style_breakout(value):
    if value == "✅":
        return "background-color: gold; color: black; font-weight: bold; text-align: center;"
    return "text-align: center; color: #999;"


# Static column tints so Trigger / TGT / SL are visually distinct at a
# glance, AND so CE vs PE tables don't look identical to each other.
# CE side uses blue/green/red; PE side uses a deeper purple/teal/amber
# so the two panels are instantly distinguishable even without reading
# the "Calls (CE)" / "Puts (PE)" labels.
CE_COLUMN_TINTS = {
    "Trigger": {"background-color": "#E3F2FD", "color": "#0D47A1", "font-weight": "600"},
    "TGT": {"background-color": "#E8F5E9", "color": "#1B5E20", "font-weight": "600"},
    "SL": {"background-color": "#FFEBEE", "color": "#B71C1C", "font-weight": "600"},
}

PE_COLUMN_TINTS = {
    "Trigger": {"background-color": "#EDE7F6", "color": "#4527A0", "font-weight": "600"},
    "TGT": {"background-color": "#E0F2F1", "color": "#00695C", "font-weight": "600"},
    "SL": {"background-color": "#FFF3E0", "color": "#E65100", "font-weight": "600"},
}


def apply_column_tints(styler, tints):
    for col, css in tints.items():
        styler = styler.set_properties(subset=[col], **css)
    return styler


def show_side_by_side(ce_table, pe_table):
    last_updated = datetime.now(IST).strftime("%H:%M:%S")
    st.caption(f"Last Updated: {last_updated} IST")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Calls (CE)**")
        if ce_table.empty:
            st.info("No CE data available.")
        else:
            ce_style = (
                ce_table.style
                .map(style_away_percent, subset=["Away %"])
                .map(style_breakout, subset=["Breakout"])
                .pipe(apply_column_tints, CE_COLUMN_TINTS)
                .format(DECIMAL_COLS, na_rep="-")
            )
            st.dataframe(
                ce_style,
                use_container_width=True,
                hide_index=True,
                height=table_height(ce_table)
            )

    with col2:
        st.markdown("**Puts (PE)**")
        if pe_table.empty:
            st.info("No PE data available.")
        else:
            pe_style = (
                pe_table.style
                .map(style_away_percent, subset=["Away %"])
                .map(style_breakout, subset=["Breakout"])
                .pipe(apply_column_tints, PE_COLUMN_TINTS)
                .format(DECIMAL_COLS, na_rep="-")
            )
            st.dataframe(
                pe_style,
                use_container_width=True,
                hide_index=True,
                height=table_height(pe_table)
            )


# ---------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------
def main():
    st.title("ORB 1HR Scanner")

    (
        access_token,
        expiry_choice,
        refresh_clicked,
        download_clicked,
        auto_refresh_enabled,
        refresh_interval,
        telegram_enabled,
        telegram_bot_token,
        telegram_chat_id,
        test_telegram_clicked,
        reset_alert_state_clicked
    ) = render_sidebar()

    if auto_refresh_enabled and AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=refresh_interval * 1000, key="scanner_autorefresh")

    if reset_alert_state_clicked:
        st.session_state["orb_alert_state"] = set()
        st.sidebar.info("Alert state cleared — crossed triggers will alert again.")

    if test_telegram_clicked:
        success, error = send_telegram_alert(
            telegram_bot_token,
            telegram_chat_id,
            "✅ Test alert from ORB 1HR Scanner — Telegram is wired up correctly."
        )
        if success:
            st.sidebar.success("Test message sent — check Telegram.")
        else:
            st.sidebar.error(f"Test message failed: {error}")

    if download_clicked:
        with st.spinner("Downloading latest NSE instrument JSON..."):
            resp = requests.get(INSTRUMENT_URL)

            if resp.status_code == 200:
                st.sidebar.download_button(
                    label="Save instruments.json.gz",
                    data=resp.content,
                    file_name="NSE_instruments.json.gz",
                    mime="application/gzip"
                )
            else:
                st.sidebar.error(f"Download failed: {resp.status_code}")

    should_fetch = refresh_clicked or (auto_refresh_enabled and access_token)

    if should_fetch:
        if not access_token:
            st.warning("Enter your Upstox Access Token in the sidebar first.")
            return

        with st.spinner("Fetching latest data..."):
            ce_table, pe_table = build_open_strike_scanner(
                access_token, expiry_choice, top_n=20
            )

        if not ce_table.empty or not pe_table.empty:
            st.session_state["ce_table"] = ce_table
            st.session_state["pe_table"] = pe_table

            # Check for price crossing Trigger and alert on Telegram,
            # once per symbol, only for newly-crossed ones.
            if telegram_enabled:
                combined = pd.concat([ce_table, pe_table], ignore_index=True)
                check_and_alert_orb_breakouts(combined, telegram_bot_token, telegram_chat_id)

    if "ce_table" in st.session_state:
        show_side_by_side(st.session_state["ce_table"], st.session_state["pe_table"])
    else:
        st.info("Enter your access token and click **Refresh LTP Now** to load data.")


if __name__ == "__main__":
    main()
