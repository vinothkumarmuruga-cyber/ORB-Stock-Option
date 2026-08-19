"""
Stock Option Scanner — 1HR Breakout (Live)
===========================================
Single-file Streamlit app. Deploy by pushing this repo to GitHub and
connecting it on share.streamlit.io (or any Streamlit Cloud account).

What it does
------------
For every NSE F&O stock, finds the ATM Call and ATM Put (nearest strike to
the stock future's opening price), computes:

    Trigger = high of the FIRST 1-hour candle today (9:15-10:15 IST)
    Away %  = LTP / Trigger * 100   (100% = right at Trigger, never negative
              — e.g. 80% means LTP is 20% below Trigger, 120% means 20% above)
    TGT     = Trigger * (1 + TGT% / 100)
    SL      = Trigger * (1 - SL% / 100)

Drop %, TGT % and SL % are entered by you in the sidebar. Any option whose
Away % has fallen below (100 - Drop%) is hidden from the table — it pulled
back too far below its Trigger to still be "in play".

This is a LIVE dashboard: nothing is written to disk. Every number you see
is fetched fresh from Upstox on each refresh. Closing the app / a Streamlit
Cloud restart just means you re-paste your token and start clean — there is
no CSV/JSON state file to go stale or get out of sync.

Upstox token
------------
Upstox access tokens expire every trading day. Paste a fresh one into the
sidebar each morning (or set it once via Streamlit Cloud's Secrets as
UPSTOX_ACCESS_TOKEN — the sidebar field will pre-fill from it, and you can
still override it by typing a new one).

Telegram alerts
----------------
Create a bot via @BotFather to get a Bot Token, message @userinfobot to get
your Chat ID, paste both in the sidebar and tick "Enable Trigger Alerts".
You'll get one Telegram message the first time each option's Away % crosses
100% (i.e. LTP reaches its Trigger) — deduplicated in-memory for the running
session so you don't get spammed on every refresh. Alerts only fire after
10:15 IST, since the Trigger itself isn't final before then.
"""

import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
import pandas as pd
import numpy as np
import streamlit as st

# ============================================================
# TIME (IST)
# ============================================================
IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now():
    return datetime.now(IST)


ORB_ALERT_CUTOFF = datetime.strptime("10:15", "%H:%M").time()

# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(page_title="Stock Option Scanner", layout="wide")

st.markdown("""
    <style>
        .block-container { padding-top: 1rem !important; padding-bottom: 1rem !important; }
        h1 { font-size: 1.8rem !important; margin-bottom: 0rem !important; }
        div[data-testid="stDataFrame"] { font-weight: 600 !important; }
    </style>
""", unsafe_allow_html=True)

INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
MAX_WORKERS = 5
BATCH_SIZE = 10
PAUSE_BETWEEN_BATCHES = 0.6


# ============================================================
# INSTRUMENT MASTER — cached 1 hour; "Reload Instrument Master" clears it
# ============================================================
@st.cache_data(ttl=3600, show_spinner="Loading NSE F&O instrument master...")
def load_instruments():
    instruments = pd.read_json(INSTRUMENT_URL, compression="gzip")
    instruments["expiry_date"] = pd.to_datetime(
        pd.to_numeric(instruments["expiry"], errors="coerce"), unit="ms", errors="coerce"
    ).dt.date

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


def get_expiry(futures_df, choice):
    today = get_ist_now().date()
    valid = sorted(futures_df[futures_df["expiry_date"] >= today]["expiry_date"].unique())
    if not valid:
        return None
    if choice == "Current Month":
        return valid[0]
    current_month = today.month
    for exp in valid:
        if exp.month != current_month:
            return exp
    return valid[-1] if len(valid) > 1 else valid[0]


def chunk_list(items, size=300):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_atm_option(options_df, underlying_key, expiry, option_type, future_open):
    chain = options_df[
        (options_df["underlying_key"] == underlying_key) &
        (options_df["expiry_date"] == expiry) &
        (options_df["instrument_type"] == option_type)
    ].copy()

    if chain.empty or pd.isna(future_open):
        return None

    chain["strike_diff"] = (chain["strike_price"] - future_open).abs()
    return chain.sort_values("strike_diff").iloc[0]


# ============================================================
# LIVE QUOTES — Upstox v3
# ============================================================
def fetch_future_open(instrument_keys, headers):
    url = "https://api.upstox.com/v3/market-quote/ohlc"
    rows = []

    for keys in chunk_list(instrument_keys):
        params = {"instrument_key": ",".join(keys), "interval": "1d"}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException:
            continue

        if response.status_code != 200:
            continue

        data = response.json().get("data", {})
        for response_key, item in data.items():
            if not isinstance(item, dict):
                continue

            live = item.get("live_ohlc") or item.get("ohlc") or {}
            prev = item.get("prev_ohlc") or {}
            true_key = item.get("instrument_token") or response_key

            open_price = (
                live.get("open")
                if live.get("open") not in (None, 0)
                else prev.get("close") if prev.get("close") not in (None, 0)
                else item.get("last_price")
            )
            rows.append({"instrument_key": true_key, "future_open": open_price})

    return pd.DataFrame(rows)


def fetch_ltp(instrument_keys, headers):
    url = "https://api.upstox.com/v3/market-quote/ltp"
    rows = []

    for keys in chunk_list(instrument_keys, size=200):
        params = {"instrument_key": ",".join(keys)}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException:
            continue

        if response.status_code != 200:
            continue

        data = response.json().get("data", {})
        for response_key, item in data.items():
            if not isinstance(item, dict):
                continue
            true_key = item.get("instrument_token") or response_key
            rows.append({"instrument_key": true_key, "ltp": item.get("last_price")})

    return pd.DataFrame(rows)


def fetch_first_hour_high(instrument_key, headers, max_retries=2):
    """Returns (instrument_key, trigger_or_None). Trigger = high of the
    first 1-hour candle today. None if that candle isn't formed yet
    (before 10:15 IST) or the request failed after retries."""
    safe_key = quote(instrument_key, safe="|")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{safe_key}/hours/1"

    attempt = 0
    while True:
        try:
            response = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException:
            return instrument_key, None

        if response.status_code == 429:
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                attempt += 1
                continue
            return instrument_key, None

        if response.status_code != 200:
            return instrument_key, None

        candles = response.json().get("data", {}).get("candles", [])
        if not candles:
            return instrument_key, None

        first_hour = sorted(candles, key=lambda c: c[0])[0]
        trigger = first_hour[2]  # high
        return instrument_key, (trigger if trigger not in (None, 0) else None)


def fetch_missing_triggers(instrument_keys, headers):
    """Only fetches keys not already resolved in session_state — a
    completed hourly candle never changes, so once a key gets a Trigger
    it's never re-fetched again today. This is what keeps a 15s
    auto-refresh cheap instead of re-hitting the historical-candle
    endpoint for every option on every tick."""
    result = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for i in range(0, len(instrument_keys), BATCH_SIZE):
            batch = instrument_keys[i:i + BATCH_SIZE]
            futures_map = {
                executor.submit(fetch_first_hour_high, k, headers): k for k in batch
            }
            for f in as_completed(futures_map):
                key, trigger = f.result()
                result[key] = trigger
            if i + BATCH_SIZE < len(instrument_keys):
                time.sleep(PAUSE_BETWEEN_BATCHES)
    return result


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        return False, "Missing bot token or chat ID"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if response.status_code == 200:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except Exception as exc:
        return False, str(exc)


def alert_breakouts(df, bot_token, chat_id):
    if "alerted_keys" not in st.session_state:
        st.session_state.alerted_keys = set()

    if get_ist_now().time() < ORB_ALERT_CUTOFF:
        return  # Trigger isn't final before the first hourly candle closes

    newly = df[
        (df["Away %"] >= 100) & (~df["instrument_key"].isin(st.session_state.alerted_keys))
    ]
    sent = 0
    for _, r in newly.iterrows():
        message = (
            "🚀 <b>1HR BO — Trigger Crossed</b>\n\n"
            f"<b>{r['Symbol']}</b>\n"
            f"LTP: {r['LTP']:.2f}  ›  Trigger: {r['Trigger']:.2f}\n"
            f"TGT: {r['TGT']:.2f}  |  SL: {r['SL']:.2f}"
        )
        ok, _err = send_telegram(bot_token, chat_id, message)
        if ok:
            st.session_state.alerted_keys.add(r["instrument_key"])
            sent += 1

    if sent:
        st.toast(f"Telegram alert sent for {sent} breakout(s).", icon="🚀")


# ============================================================
# CORE SCAN
# ============================================================
def build_scanner_table(access_token, expiry_choice, drop_pct, tgt_pct, sl_pct):
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}

    futures_all, options_all = load_instruments()
    expiry = get_expiry(futures_all, expiry_choice)
    if expiry is None:
        st.error("No valid futures expiry found.")
        return pd.DataFrame(), pd.DataFrame(), None, 0, 0, 0, pd.DataFrame()

    futures = (
        futures_all[futures_all["expiry_date"] == expiry]
        .drop_duplicates(subset="underlying_key")
        .copy()
    )
    options = options_all[options_all["expiry_date"] == expiry].copy()

    fut_open = fetch_future_open(futures["instrument_key"].tolist(), headers)
    futures = futures.merge(fut_open, on="instrument_key", how="left").dropna(subset=["future_open"])

    if futures.empty:
        st.error("No futures Open prices received — check your access token or market hours.")
        return pd.DataFrame(), pd.DataFrame(), expiry, 0, 0, 0, pd.DataFrame()

    rows = []
    for _, fut in futures.iterrows():
        for opt_type in ("CE", "PE"):
            opt = get_atm_option(options, fut["underlying_key"], expiry, opt_type, fut["future_open"])
            if opt is None:
                continue
            rows.append({
                "Symbol": f"{fut['underlying_symbol']} {int(opt['strike_price'])} {opt_type}",
                "Type": opt_type,
                "Open": round(float(fut["future_open"]), 2),
                "instrument_key": opt["instrument_key"],
            })

    atm_df = pd.DataFrame(rows)
    if atm_df.empty:
        st.error("No ATM CE/PE options resolved.")
        return pd.DataFrame(), pd.DataFrame(), expiry, 0, 0, len(futures), pd.DataFrame()

    # ---- Trigger = first 1-hour candle high, cached per trading day ----
    today_key = get_ist_now().strftime("%Y-%m-%d")
    if st.session_state.get("trigger_day") != today_key:
        st.session_state.trigger_day = today_key
        st.session_state.triggers = {}
        st.session_state.alerted_keys = set()

    known_triggers = st.session_state.triggers
    missing_keys = [k for k in atm_df["instrument_key"] if known_triggers.get(k) is None]
    if missing_keys:
        fresh = fetch_missing_triggers(missing_keys, headers)
        known_triggers.update(fresh)

    atm_df["Trigger"] = pd.to_numeric(atm_df["instrument_key"].map(known_triggers), errors="coerce")

    no_trigger_count = int(atm_df["Trigger"].isna().sum())
    have_trigger = atm_df[atm_df["Trigger"].notna()].copy()

    if have_trigger.empty:
        return pd.DataFrame(), pd.DataFrame(), expiry, no_trigger_count, 0, len(futures), pd.DataFrame()

    # ---- Live LTP ----
    ltp_df = fetch_ltp(have_trigger["instrument_key"].tolist(), headers)
    have_trigger = have_trigger.merge(ltp_df, on="instrument_key", how="left")
    have_trigger = have_trigger.dropna(subset=["ltp"]).rename(columns={"ltp": "LTP"})
    have_trigger["LTP"] = pd.to_numeric(have_trigger["LTP"], errors="coerce")
    have_trigger = have_trigger.dropna(subset=["LTP"])

    if have_trigger.empty:
        return pd.DataFrame(), pd.DataFrame(), expiry, no_trigger_count, 0, len(futures), pd.DataFrame()

    # Away % = LTP as a % of Trigger. Never negative: 80% = 20% below
    # Trigger, 120% = 20% above Trigger.
    have_trigger["Away %"] = (have_trigger["LTP"] / have_trigger["Trigger"] * 100).clip(lower=0)
    have_trigger["TGT"] = have_trigger["Trigger"] * (1 + tgt_pct / 100)
    have_trigger["SL"] = have_trigger["Trigger"] * (1 - sl_pct / 100)

    for col in ["Open", "LTP", "Trigger", "Away %", "TGT", "SL"]:
        have_trigger[col] = have_trigger[col].round(2)

    drop_floor = 100 - drop_pct
    in_play = have_trigger[have_trigger["Away %"] >= drop_floor].copy()
    dropped_count = len(have_trigger) - len(in_play)

    ce_table = in_play[in_play["Type"] == "CE"].sort_values("Away %", ascending=False).reset_index(drop=True)
    pe_table = in_play[in_play["Type"] == "PE"].sort_values("Away %", ascending=False).reset_index(drop=True)

    return ce_table, pe_table, expiry, no_trigger_count, dropped_count, len(futures), in_play


# ============================================================
# DISPLAY
# ============================================================
DISPLAY_COLS = ["Symbol", "Open", "LTP", "Trigger", "Away %", "TGT", "SL"]
FORMAT_DICT = {
    "Open": "{:.2f}", "LTP": "{:.2f}", "Trigger": "{:.2f}",
    "Away %": "{:.2f}%", "TGT": "{:.2f}", "SL": "{:.2f}",
}


def style_away(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ""
    if value >= 100:
        return "background-color: darkgreen; color: white; font-weight: bold;"
    if value >= 90:
        return "background-color: lightgreen; color: black; font-weight: bold;"
    return ""


def show_table(df, label):
    st.markdown(f"**{label}**")
    if df.empty:
        st.info(f"No {label} data yet.")
        return
    styled = (
        df[DISPLAY_COLS].style
        .map(style_away, subset=["Away %"])
        .set_properties(subset=["TGT"], **{"color": "#1B5E20", "font-weight": "600"})
        .set_properties(subset=["SL"], **{"color": "#B71C1C", "font-weight": "600"})
        .format(FORMAT_DICT)
    )
    st.dataframe(styled, hide_index=True, width="stretch")


# ============================================================
# SIDEBAR
# ============================================================
with st.sidebar:
    st.header("Upstox")
    default_token = st.session_state.get("access_token") or st.secrets.get("UPSTOX_ACCESS_TOKEN", "")
    access_token = st.text_input(
        "Access Token (update daily)",
        value=default_token,
        type="password",
        help="Upstox access tokens expire every trading day — paste a fresh one each morning.",
    )
    st.session_state.access_token = access_token

    st.markdown("---")
    st.header("Expiry")
    expiry_choice = st.radio("Expiry", ["Current Month", "Next Month"], index=0)

    st.markdown("---")
    st.header("Strategy Inputs")
    drop_pct = st.number_input(
        "Drop % (max pullback below Trigger to stay listed)",
        min_value=0.0, value=15.0, step=1.0,
        help="An option is hidden once its Away % falls below (100 - Drop%) — i.e. it pulled back too far below its Trigger.",
    )
    tgt_pct = st.number_input("TGT % (above Trigger)", min_value=0.0, value=35.0, step=1.0)
    sl_pct = st.number_input("SL % (below Trigger)", min_value=0.0, value=20.0, step=1.0)

    st.markdown("---")
    st.header("Telegram Alerts")
    telegram_enabled = st.checkbox("Enable Trigger Alerts", value=False)
    bot_token = st.text_input("Bot Token", type="password")
    chat_id = st.text_input("Chat ID")
    tcol1, tcol2 = st.columns(2)
    send_test_clicked = tcol1.button("Send Test", width="stretch")
    reset_alerts_clicked = tcol2.button("Reset Alerts", width="stretch")

    if reset_alerts_clicked:
        st.session_state.alerted_keys = set()
        st.success("Alert history cleared — already-alerted options will alert again.")

    if send_test_clicked:
        ok, err = send_telegram(bot_token, chat_id, "✅ Test alert from Stock Option Scanner.")
        if ok:
            st.success("Test message sent — check Telegram.")
        else:
            st.error(f"Test message failed: {err}")

    st.markdown("---")
    st.header("Data Management")
    if st.button("Reload Instrument Master", width="stretch"):
        load_instruments.clear()
        st.session_state.trigger_day = None
        st.rerun()

    st.markdown("---")
    st.header("Auto Refresh")
    auto_refresh = st.checkbox("Enable Auto-Refresh", value=False)
    refresh_interval = st.slider("Refresh Interval (seconds)", min_value=5, max_value=60, value=15)


# ============================================================
# MAIN
# ============================================================
st.title("Stock Option Scanner")
st.subheader("1HR Breakout Options (Live)")

if not access_token:
    st.warning("Enter your Upstox Access Token in the sidebar to begin.")
else:
    run_every = refresh_interval if auto_refresh else None

    @st.fragment(run_every=run_every)
    def live_view():
        (ce_table, pe_table, expiry, no_trigger_count,
         dropped_count, futures_count, in_play) = build_scanner_table(
            access_token, expiry_choice, drop_pct, tgt_pct, sl_pct
        )

        if expiry is None:
            return

        st.caption(f"Expiry: {expiry} | Stock futures: {futures_count}")

        if no_trigger_count:
            st.info(
                f"1HR BO Trigger not available yet for {no_trigger_count} option(s) "
                f"(normal before 10:15 IST — the first hourly candle isn't closed yet; "
                f"will fill in on a later refresh)."
            )
        if dropped_count:
            st.caption(
                f"{dropped_count} option(s) hidden — pulled back more than "
                f"{drop_pct:g}% below their Trigger."
            )
        st.caption(f"Last Updated: {get_ist_now().strftime('%H:%M:%S')} IST")

        if telegram_enabled and not in_play.empty:
            alert_breakouts(in_play, bot_token, chat_id)

        col1, col2 = st.columns(2)
        with col1:
            show_table(ce_table, "Calls (CE)")
        with col2:
            show_table(pe_table, "Puts (PE)")

    live_view()
