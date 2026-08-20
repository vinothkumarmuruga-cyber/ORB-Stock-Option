import streamlit as st
import pandas as pd
import numpy as np
import requests
import os
import time
import json
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

# ============================================================
# IST
# ============================================================

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def get_ist_now():
    return datetime.now(IST)


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="1HR BO Scanner",
    layout="wide"
)


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown("""
    <style>

        .block-container {
            padding-top: 1rem !important;
            padding-bottom: 1rem !important;
        }

        h1 {
            font-size: 1.8rem !important;
            margin-bottom: 0rem !important;
            white-space: nowrap !important;
        }

        h2 {
            font-size: 1.1rem !important;
            padding-top: 0.2rem !important;
            margin-bottom: 0.1rem !important;
        }

        h3 {
            font-size: 1.0rem !important;
            padding-top: 0.1rem !important;
            margin-bottom: 0.1rem !important;
        }

        /* Prevent graying during refresh */

        .stApp {
            transition: none !important;
        }

        [data-testid="stAppViewContainer"],
        [data-testid="stHeader"] {
            opacity: 1 !important;
            transition: none !important;
        }

        /* Dataframe */

        div[data-testid="stDataFrame"] {
            font-weight: 600 !important;
        }

    </style>
""", unsafe_allow_html=True)


# ============================================================
# PERSISTENT STORAGE
# ============================================================

DATA_DIR = "data"

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)


TOKEN_FILE = os.path.join(DATA_DIR, "token.json")
TRIGGER_ALERT_FILE = os.path.join(DATA_DIR, "trigger_alert_state.json")
ALERT_LOG_FILE = os.path.join(DATA_DIR, "alert_log.csv")


# ============================================================
# OPTIONAL EXTERNAL PERSISTENCE (GitHub Gist)
#
# Local disk under DATA_DIR is NOT reliable on Streamlit Cloud — the
# container (and everything on its filesystem) gets wiped on restarts,
# redeploys, or after a period of inactivity. That means the Upstox
# token and, worse, the Telegram alert-dedup state can silently reset
# mid-day, causing duplicate alerts.
#
# If you add these two secrets in .streamlit/secrets.toml (or the
# Streamlit Cloud secrets UI), the token and alert-dedup state are
# additionally backed up to a private GitHub Gist, which survives
# app restarts:
#
#   GITHUB_GIST_TOKEN = "ghp_xxx..."   # PAT with the "gist" scope
#   GITHUB_GIST_ID    = "abcdef123..."  # id of an existing (empty) gist
#
# To create the gist: go to https://gist.github.com/, add any one
# file (e.g. "placeholder.txt" with any content), save it as a
# SECRET gist, then copy the id from its URL
# (https://gist.github.com/<username>/<THIS PART>).
#
# Without these secrets set, everything falls back to local-disk-only
# behavior exactly as before — nothing breaks if you skip this.
# ============================================================

GIST_TOKEN = st.secrets.get("GITHUB_GIST_TOKEN", "")
GIST_ID = st.secrets.get("GITHUB_GIST_ID", "")
USE_GIST_PERSISTENCE = bool(GIST_TOKEN and GIST_ID)


def _gist_headers():
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }


def _gist_read_file(filename):
    """Returns the raw text content of one file inside the configured
    Gist, or None if not configured / not found / on any error."""
    if not USE_GIST_PERSISTENCE:
        return None
    try:
        resp = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers(),
            timeout=10
        )
        if resp.status_code != 200:
            return None
        file_info = resp.json().get("files", {}).get(filename)
        return file_info.get("content") if file_info else None
    except Exception:
        return None


def _gist_write_file(filename, content_str):
    """Writes (creates/overwrites) one file inside the configured Gist.
    Returns True on success, False otherwise (including if not
    configured) — callers should treat this as best-effort."""
    if not USE_GIST_PERSISTENCE:
        return False
    try:
        payload = {"files": {filename: {"content": content_str}}}
        resp = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers(),
            json=payload,
            timeout=10
        )
        return resp.status_code == 200
    except Exception:
        return False


# ============================================================
# TOKEN — local disk first (fast), Gist as durable backup/fallback
# ============================================================

def load_token():
    today_str = get_ist_now().strftime("%Y-%m-%d")

    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return data.get("token", "")
        except:
            pass

    # Local copy missing/stale (likely a fresh container after a restart)
    # — try the Gist backup before giving up.
    if USE_GIST_PERSISTENCE:
        raw = _gist_read_file("token.json")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today_str:
                    # Warm the local cache too, so we don't hit the Gist
                    # API again this session.
                    try:
                        with open(TOKEN_FILE, "w") as f:
                            json.dump(data, f)
                    except:
                        pass
                    return data.get("token", "")
            except Exception:
                pass

    return ""


def save_token(token):
    data = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "token": token
    }
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

    if USE_GIST_PERSISTENCE:
        _gist_write_file("token.json", json.dumps(data))


# ============================================================
# TELEGRAM TRIGGER-ALERT STATE
#
# Persisted to disk (and, if configured, to the Gist backup — see the
# "OPTIONAL EXTERNAL PERSISTENCE" section above) so alert
# de-duplication survives restarts. Without a durable backup, a
# Streamlit Cloud restart mid-day wipes this file and can cause
# duplicate Telegram alerts for options that already fired earlier.
# Resets automatically each new trading day. Each entry is
# "1HR BO:<symbol>".
# ============================================================

def load_trigger_alert_state():
    today_str = get_ist_now().strftime("%Y-%m-%d")

    if os.path.exists(TRIGGER_ALERT_FILE):
        try:
            with open(TRIGGER_ALERT_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return set(data.get("keys", []))
        except:
            pass

    # Local copy missing/stale — likely a fresh container after a
    # restart. Try the Gist backup before falling back to empty.
    if USE_GIST_PERSISTENCE:
        raw = _gist_read_file("trigger_alert_state.json")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today_str:
                    keys = set(data.get("keys", []))
                    try:
                        with open(TRIGGER_ALERT_FILE, "w") as f:
                            json.dump({"date": today_str, "keys": list(keys)}, f)
                    except:
                        pass
                    return keys
            except Exception:
                pass

    return set()


def save_trigger_alert_state(keys):
    data = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "keys": list(keys)
    }
    try:
        with open(TRIGGER_ALERT_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

    if USE_GIST_PERSISTENCE:
        _gist_write_file("trigger_alert_state.json", json.dumps(data))


# ============================================================
# ALERT LOG (CSV) — every fired 1HR BO alert gets one row here: when it
# crossed, at what LTP, and what the Trigger/TGT/SL levels were at that
# moment. Lets you go back later and check whether price actually
# reached TGT before SL, instead of trusting the fixed TGT/SL
# percentages blind. Also mirrored to the Gist backup (if configured)
# so the day's alert history isn't lost on a restart — see the
# "OPTIONAL EXTERNAL PERSISTENCE" section above.
# ============================================================

ALERT_LOG_HEADER = "timestamp_ist,tab,symbol,ltp,trigger,tgt,sl\n"


def log_alert_event(tab, symbol, ltp, trigger, tgt=None, sl=None):
    ts = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    tgt_str = f"{tgt:.2f}" if tgt is not None else ""
    sl_str = f"{sl:.2f}" if sl is not None else ""
    line = f"{ts},{tab},{symbol},{ltp:.2f},{trigger:.2f},{tgt_str},{sl_str}\n"

    try:
        is_new = not os.path.exists(ALERT_LOG_FILE)
        with open(ALERT_LOG_FILE, "a", newline="") as f:
            if is_new:
                f.write(ALERT_LOG_HEADER)
            f.write(line)
    except Exception:
        pass

    if USE_GIST_PERSISTENCE:
        try:
            existing = _gist_read_file("alert_log.csv")
            if not existing:
                existing = ALERT_LOG_HEADER
            elif not existing.startswith("timestamp_ist"):
                existing = ALERT_LOG_HEADER + existing
            _gist_write_file("alert_log.csv", existing + line)
        except Exception:
            pass


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


# The first 1-hour candle (9:15-10:15 IST) is only fully formed once
# the clock passes 10:15 IST, which is when the Trigger (first-hour
# high) becomes final. The breakout itself is only ever evaluated
# against the SECOND 1-hour candle (10:15-11:15 IST) — the "next one
# hour" candle after the trigger forms. Outside that window (before
# 10:15, or after 11:15) no new entry is considered valid, matching
# the backtest logic: entries only fire off the 10:15 candle.
ORB_ENTRY_WINDOW_START = datetime.strptime("10:15", "%H:%M").time()
ORB_ENTRY_WINDOW_END = datetime.strptime("11:15", "%H:%M").time()

# Kept as an alias for readability where only the start matters.
ORB_ALERT_CUTOFF = ORB_ENTRY_WINDOW_START

# Breakout-quality filter (matches the "Drop % (breakout limit)" in the
# reference backtest): during the 10:15 candle, price's low must not
# have dropped more than this % below the Trigger before crossing back
# above it. A deep dip below Trigger before the "breakout" disqualifies
# it as a clean breakout.
BREAKOUT_DROP_PCT_LIMIT = 5.0


def check_and_alert_1hr_bo(df, telegram_enabled, bot_token, chat_id):
    """
    1HR BO tab: alerts the moment an option's live LTP crosses its
    Trigger (first 1-hour candle high), evaluated ONLY during the
    10:15-11:15 IST candle (the "next one hour" candle after the
    trigger forms) and only when the breakout-quality filter passed
    (see BREAKOUT_DROP_PCT_LIMIT / "_crossed" in build_open_strike_scanner).
    Uses the persisted alert-state file (tagged "1HR BO:<symbol>") so
    de-duplication survives restarts. The crossed flag is checked internally
    here even though the Breakout column itself is no longer shown in
    the table.

    No entries before 10:15 (trigger not final yet) or after 11:15
    (the 10:15 candle has closed — matches the backtest rule that
    entries only fire off that one candle).
    """
    if not telegram_enabled:
        return
    if df.empty:
        return

    now_time = get_ist_now().time()
    if not (ORB_ENTRY_WINDOW_START <= now_time <= ORB_ENTRY_WINDOW_END):
        return

    alerted = load_trigger_alert_state()
    newly_triggered = []

    for _, row in df.iterrows():
        symbol = row.get("Symbol")
        if not symbol:
            continue

        alert_id = f"1HR BO:{symbol}"

        if row.get("_crossed") and alert_id not in alerted:
            newly_triggered.append((alert_id, row))

    if not newly_triggered:
        return

    # One Telegram message per option, sent and persisted independently.
    # Also logs each fired alert (LTP/Trigger/TGT/SL at the moment of
    # crossing) to the CSV alert log for later TGT/SL review.
    sent_count = 0
    fail_count = 0

    for alert_id, row in newly_triggered:
        message = (
            "🚀 <b>1HR BO — Trigger Crossed</b>\n\n"
            f"<b>{row['Symbol']}</b>\n"
            f"LTP: {row['LTP']:.2f}  ›  Trigger: {row['Trigger']:.2f}\n"
            f"TGT: {row['TGT']:.2f}  |  SL: {row['SL']:.2f}"
        )

        success, error = send_telegram_alert(bot_token, chat_id, message)

        if success:
            alerted.add(alert_id)
            save_trigger_alert_state(alerted)
            log_alert_event(
                "1HR BO",
                row['Symbol'],
                row['LTP'],
                row['Trigger'],
                tgt=row.get('TGT'),
                sl=row.get('SL')
            )
            sent_count += 1
        else:
            fail_count += 1

    if sent_count:
        st.toast(f"Telegram alert sent for {sent_count} 1HR BO cross(es).", icon="🚀")
    if fail_count:
        st.toast(f"{fail_count} 1HR BO alert(s) failed — will retry next refresh.", icon="⚠️")


# ============================================================
# LIVE INSTRUMENT FILE (fetched directly from Upstox)
# ============================================================

LIVE_INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def normalize_expiry(series):
    return pd.to_datetime(
        pd.to_numeric(series, errors="coerce"),
        unit="ms",
        errors="coerce"
    ).dt.date


@st.cache_data(ttl=3600, show_spinner="Loading live instrument file...")
def load_live_fo_instruments():
    instruments = pd.read_json(LIVE_INSTRUMENT_URL, compression="gzip")
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


def chunk_list(items, size=300):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ============================================================
# 1HR BO — LIVE ORB SCANNER (uses Upstox v3 OHLC/LTP + intraday
# hourly candles directly)
#
#   Trigger = high of the first 1-hour candle (9:15-10:15 IST).
#   TGT = Trigger + 20%, SL = Trigger - 5%.
#   Breakout is only ever evaluated off the NEXT one-hour candle
#   (10:15-11:15 IST), using that candle's own OHLC rather than a live
#   LTP snapshot:
#     1) breakout-quality filter: the LOW of the 10:15 candle must not
#        have dropped more than BREAKOUT_DROP_PCT_LIMIT (5%) below
#        Trigger, AND
#     2) the HIGH of the 10:15 candle must have actually reached
#        Trigger.
#   Both together = "_crossed" (valid breakout). Because this is based
#   on the candle's OHLC rather than current LTP, it stays True for the
#   rest of the day even if price later pulls back — matching the
#   reference backtest, which keeps every entered trade listed under
#   Hit TGT / Hit SL / Still open regardless of where price ends up.
#   Only rows with "_crossed" True are shown in the CE/PE tables at
#   all (no more "every shortlisted top mover" display). The Telegram
#   alert additionally only fires new alerts while the clock is inside
#   the 10:15-11:15 window (see check_and_alert_1hr_bo) — no NEW alerts
#   start after 11:15, even though "_crossed" itself stays True.
# ============================================================

def fetch_future_open_v3(instrument_keys, headers):
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
            raw_sample = dict(list(data.items())[:2])

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


def _fetch_single_orb_data(instrument_key, headers, max_retries=2):
    safe_key = quote(instrument_key, safe="|")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{safe_key}/hours/1"

    attempt = 0
    while True:
        try:
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 429:
                # Rate limited (Cloudflare in front of Upstox). Back off and
                # retry a couple of times before giving up on this instrument.
                if attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait_s = float(retry_after) if retry_after else (1.5 * (attempt + 1))
                    except ValueError:
                        wait_s = 1.5 * (attempt + 1)
                    time.sleep(min(wait_s, 5))
                    attempt += 1
                    continue
                return instrument_key, None, "HTTP 429: Rate limited (gave up after retries)"

            if response.status_code != 200:
                return instrument_key, None, f"HTTP {response.status_code}: {response.text[:200]}"

            payload = response.json()
            candles = payload.get("data", {}).get("candles", [])

            if not candles:
                return instrument_key, None, "No intraday hourly candles yet today"

            candles_sorted = sorted(candles, key=lambda c: c[0])

            first_hour = candles_sorted[0]
            first_hour_high = first_hour[2]
            trigger = first_hour_high

            if trigger in (None, 0):
                return instrument_key, None, "First-hour candle has no valid high"

            # candle format: [timestamp, open, high, low, close, volume, oi]
            # The second candle (10:15-11:15 IST) is what the breakout is
            # actually evaluated against — both its low (quality filter)
            # and its high (did it ever actually reach Trigger). While
            # that hour is still forming these are the running low/high
            # so far; once the hour closes they're final. Neither is
            # present yet before 10:15. Using the candle's high (not the
            # live LTP snapshot) to decide whether Trigger was reached
            # means a strike that broke out and later pulled back still
            # correctly stays flagged as having triggered for the rest
            # of the day, instead of disappearing once price falls back.
            second_hour_low = None
            second_hour_high = None
            if len(candles_sorted) >= 2:
                second_hour_low = candles_sorted[1][3]
                second_hour_high = candles_sorted[1][2]

            # Status = which of TGT/SL got hit FIRST after entry (or
            # "Open" if neither has been hit yet). Walk every candle from
            # the entry candle (10:15) onward in chronological order and
            # stop at the first one whose high reached TGT or whose low
            # reached SL. If a single candle's range spans BOTH levels
            # (common for cheap, volatile options), we can't tell from
            # OHLC alone which was touched first within that hour — as a
            # simple tie-break, a candle that closed above its open is
            # treated as having pushed up into TGT first, and one that
            # closed below its open as having dropped into SL first.
            tgt = trigger * 1.20
            sl = trigger * 0.95
            status = "Open"

            for c in candles_sorted[1:]:
                c_open, c_high, c_low, c_close = c[1], c[2], c[3], c[4]
                hit_tgt = c_high is not None and c_high >= tgt
                hit_sl = c_low is not None and c_low <= sl

                if hit_tgt and hit_sl:
                    status = "TGT Hit" if (c_close or 0) >= (c_open or 0) else "SL Hit"
                    break
                elif hit_tgt:
                    status = "TGT Hit"
                    break
                elif hit_sl:
                    status = "SL Hit"
                    break

            info = {
                "trigger": trigger,
                "second_hour_low": second_hour_low,
                "second_hour_high": second_hour_high,
                "status": status,
            }
            return instrument_key, info, None

        except Exception as e:
            return instrument_key, None, f"Exception: {e}"


@st.cache_data(ttl=60, show_spinner="Computing 1HR BO trigger levels...")
def fetch_orb_map(instrument_keys, headers_tuple):
    headers = dict(headers_tuple)
    result = {}
    sample_errors = []

    instrument_keys = list(instrument_keys)

    # Fetching the ORB candle for ~100 instruments at once was hitting
    # Cloudflare's rate limiter (HTTP 429) in front of Upstox. Spread the
    # requests out: modest concurrency (5 at a time) processed in small
    # batches with a short pause between batches, instead of firing
    # everything at once with 12 workers. Now scanning the FULL ATM
    # universe (~400+ options, no top-N pre-filter — see
    # build_open_strike_scanner) rather than a top-50 shortlist, so this
    # takes noticeably longer per refresh (the cache ttl above is set to
    # 60s to match).
    batch_size = 10
    pause_between_batches = 0.6

    with ThreadPoolExecutor(max_workers=5) as executor:
        for i in range(0, len(instrument_keys), batch_size):
            batch = instrument_keys[i:i + batch_size]
            futures = {
                executor.submit(_fetch_single_orb_data, key, headers): key
                for key in batch
            }

            for future in as_completed(futures):
                key, info, error = future.result()
                result[key] = info

                if error and len(sample_errors) < 5:
                    sample_errors.append(f"{key} -> {error}")

            if i + batch_size < len(instrument_keys):
                time.sleep(pause_between_batches)

    return result, sample_errors


def build_open_strike_scanner(access_token, expiry_choice):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    futures, options = load_live_fo_instruments()
    expiry = get_expiry_for_choice(futures, expiry_choice)

    if expiry is None:
        st.error("No futures expiry found")
        return pd.DataFrame(), pd.DataFrame()

    futures = futures[futures["expiry_date"] == expiry].copy()
    options = options[options["expiry_date"] == expiry].copy()

    st.caption(f"Expiry: {expiry}  |  Stock futures: {len(futures)}")

    fut_quotes, ohlc_raw_sample = fetch_future_open_v3(futures["instrument_key"].tolist(), headers)

    if fut_quotes.empty:
        st.error("No futures open data received")
        return pd.DataFrame(), pd.DataFrame()

    futures = futures.merge(fut_quotes, on="instrument_key", how="left")
    futures = futures.dropna(subset=["future_open"])

    if futures.empty:
        st.error("All futures were dropped after the Open-price fetch (future_open came back empty/NaN for every instrument).")
        with st.expander("⚠️ Raw OHLC API response sample — diagnostics", expanded=True):
            st.json(ohlc_raw_sample if ohlc_raw_sample else {"note": "response 'data' was empty"})
        return pd.DataFrame(), pd.DataFrame()

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
        return pd.DataFrame(), pd.DataFrame()

    option_quotes = fetch_ltp_v3(selected["option_key"].tolist(), headers)

    if option_quotes.empty:
        st.error("No option quote data received")
        return pd.DataFrame(), pd.DataFrame()

    option_quotes = option_quotes.drop_duplicates("instrument_key")

    selected = selected.merge(option_quotes, left_on="option_key", right_on="instrument_key", how="left")
    selected = selected.drop(columns=["instrument_key"])

    selected["Symbol"] = (
        selected["underlying_symbol"].astype(str) + " "
        + selected["strike"].astype(int).astype(str) + " "
        + selected["option_type"].astype(str)
    )

    selected["Chg%"] = np.where(
        selected["prev_close"] > 0,
        ((selected["ltp"] - selected["prev_close"]) / selected["prev_close"]) * 100,
        np.nan
    )

    selected["Cap"] = selected["ltp"] * selected["Lot"]

    selected = selected.rename(columns={"ltp": "LTP"})

    # Evaluate the ENTIRE ATM CE/PE universe (every stock's nearest CE
    # and nearest PE — matches the backtest's "Total ATM options" count,
    # e.g. 414-416). No top-N-by-day-Chg% pre-filter: that was cutting
    # out genuine 10:15-candle breakouts whose overall day change % just
    # didn't happen to be in the top movers, which is unrelated to
    # whether the ORB condition itself was actually met.
    shortlisted = selected.copy()

    orb_map, orb_errors = fetch_orb_map(
        tuple(sorted(shortlisted["option_key"].unique())),
        tuple(headers.items())
    )

    def _orb_field(option_key, field, default=None):
        info = orb_map.get(option_key)
        return info.get(field, default) if info else default

    shortlisted["Trigger"] = shortlisted["option_key"].apply(lambda k: _orb_field(k, "trigger"))
    shortlisted["Trigger"] = pd.to_numeric(shortlisted["Trigger"], errors="coerce")

    shortlisted["_second_hour_low"] = shortlisted["option_key"].apply(lambda k: _orb_field(k, "second_hour_low"))
    shortlisted["_second_hour_low"] = pd.to_numeric(shortlisted["_second_hour_low"], errors="coerce")

    shortlisted["_second_hour_high"] = shortlisted["option_key"].apply(lambda k: _orb_field(k, "second_hour_high"))
    shortlisted["_second_hour_high"] = pd.to_numeric(shortlisted["_second_hour_high"], errors="coerce")

    # Status = which of TGT/SL was hit FIRST after entry, or "Open" if
    # neither has been hit yet — computed candle-by-candle in
    # _fetch_single_orb_data (see its comments for the same-candle
    # tie-break rule).
    shortlisted["Status"] = shortlisted["option_key"].apply(lambda k: _orb_field(k, "status", "Open"))

    # TGT = Trigger + 20%, SL = Trigger - 5%
    shortlisted["TGT"] = shortlisted["Trigger"] * 1.20
    shortlisted["SL"] = shortlisted["Trigger"] * 0.95

    missing_count = shortlisted["Trigger"].isna().sum()
    total_count = len(shortlisted)

    if missing_count > 0:
        with st.expander(
            f"⚠️ 1HR BO Trigger not available for {missing_count}/{total_count} options (hidden from table below)",
            expanded=(missing_count == total_count)
        ):
            st.write("Normal before the first hourly candle (9:15-10:15 IST) has data yet. Can also happen if the API rate-limited some requests — those will retry on the next refresh.")
            if orb_errors:
                for err in orb_errors:
                    st.code(err)

    # Drop % — how far the 10:15 candle's low (so far) has dipped below
    # Trigger. 0 means it never went below Trigger. This is the same
    # "breakout limit" concept as the reference backtest's Drop % column.
    shortlisted["Drop %"] = np.where(
        shortlisted["_second_hour_low"].notna() & (shortlisted["Trigger"] > 0),
        ((shortlisted["Trigger"] - shortlisted["_second_hour_low"]) / shortlisted["Trigger"]) * 100,
        np.nan
    )
    shortlisted["Drop %"] = shortlisted["Drop %"].clip(lower=0)

    # Breakout-quality filter: the 10:15 candle's low must not have
    # dropped more than BREAKOUT_DROP_PCT_LIMIT (5%) below Trigger before
    # price crosses back above it. NaN Drop % (no second-hour candle yet)
    # fails the filter, same as before 10:15.
    quality_ok = shortlisted["Drop %"].notna() & (shortlisted["Drop %"] <= BREAKOUT_DROP_PCT_LIMIT)

    # Valid breakout = quality filter passed AND the 10:15 candle's HIGH
    # actually reached Trigger. Using the candle's high (not the live LTP
    # snapshot) makes this persistent for the rest of the day — a strike
    # that broke out and later pulled back below Trigger (e.g. it went on
    # to hit SL) still correctly stays flagged as "triggered" instead of
    # disappearing once price falls back, matching how the reference
    # backtest keeps every entered trade in its Hit TGT / Hit SL / Still
    # open lists all day. This is also what only ever fires the Telegram
    # alert (gated separately to the 10:15-11:15 window in
    # check_and_alert_1hr_bo, so no NEW alerts start after 11:15 even
    # though the flag itself stays True).
    shortlisted["_crossed"] = (
        quality_ok
        & shortlisted["_second_hour_high"].notna()
        & (shortlisted["_second_hour_high"] >= shortlisted["Trigger"])
    )

    shortlisted["Away %"] = np.where(
        shortlisted["Trigger"] > 0,
        (shortlisted["LTP"] / shortlisted["Trigger"]) * 100,
        np.nan
    )
    shortlisted["Away %"] = shortlisted["Away %"].clip(lower=0)

    result = shortlisted[[
        "Symbol", "Open", "LTP", "Trigger", "Away %", "Drop %", "TGT", "SL", "Status", "_crossed", "Lot", "Cap"
    ]].copy()

    for col in ["Open", "Trigger", "TGT", "SL", "LTP", "Away %", "Drop %"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(2)

    result["Lot"] = pd.to_numeric(result["Lot"], errors="coerce").fillna(0).astype(int)
    result["Cap"] = pd.to_numeric(result["Cap"], errors="coerce").round(0).fillna(0).astype(int)

    # Hide rows with no Trigger (ORB level not fetched yet — before
    # 10:15 IST, or dropped due to a rate-limited/failed API call). Only
    # options with an actual computed Trigger/TGT/SL get plotted; the
    # diagnostics expander above already explains why the rest are missing.
    result = result[result["Trigger"].notna()].reset_index(drop=True)

    # Only show GENUINE breakouts — matches the backtest's "Triggered
    # (entered)" list, not just "top movers by day Chg%". A row qualifies
    # once "_crossed" is True: the 10:15 candle's low stayed within
    # BREAKOUT_DROP_PCT_LIMIT (5%) below Trigger AND that candle's high
    # actually reached Trigger. Because "_crossed" is based on the 10:15
    # candle's OHLC (not the live LTP snapshot), a strike stays listed
    # here for the rest of the day even after it goes on to hit TGT or
    # SL and price moves away from Trigger again — exactly like the
    # reference backtest, which keeps every entered trade in its Hit
    # TGT / Hit SL / Still open lists regardless of where price ends up.
    result = result[result["_crossed"]].reset_index(drop=True)

    ce_table = result[result["Symbol"].str.endswith("CE")].sort_values("Away %", ascending=False, na_position="last").reset_index(drop=True)
    pe_table = result[result["Symbol"].str.endswith("PE")].sort_values("Away %", ascending=False, na_position="last").reset_index(drop=True)

    return ce_table, pe_table


def table_height(df, row_px=35, header_px=38, max_px=900):
    return min(header_px + row_px * max(len(df), 1) + 3, max_px)


# Breakout column removed from display — this dict now only formats
# the columns actually shown in the table.
DECIMAL_COLS = {
    "Open": "{:.2f}",
    "Trigger": "{:.2f}",
    "TGT": "{:.2f}",
    "SL": "{:.2f}",
    "LTP": "{:.2f}",
    "Away %": "{:.2f}%",
    "Drop %": "{:.2f}%",
}

# Columns actually rendered in the 1HR BO table — "_crossed" (the
# internal alert flag) is deliberately excluded. "Drop %" shows how far
# the 10:15 candle dipped below Trigger (breakout-quality filter, must
# be <= BREAKOUT_DROP_PCT_LIMIT for a valid breakout). "Status" shows
# whether TGT or SL was hit first since entry (or "Open").
DISPLAY_COLS_1HR_BO = ["Symbol", "Open", "LTP", "Trigger", "Away %", "Drop %", "TGT", "SL", "Status", "Lot", "Cap"]


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


def style_status(value):
    if value == "TGT Hit":
        return "background-color: darkgreen; color: white; font-weight: bold;"
    if value == "SL Hit":
        return "background-color: darkred; color: white; font-weight: bold;"
    if value == "Open":
        return "background-color: #FFF3CD; color: #856404; font-weight: bold;"
    return ""


# Static column tints so Trigger / TGT / SL are visually distinct at a
# glance, AND so CE vs PE tables don't look identical to each other.
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
    last_updated = get_ist_now().strftime("%H:%M:%S")
    st.caption(f"Last Updated: {last_updated} IST")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Calls (CE)**")
        if ce_table.empty:
            st.info("No CE data available.")
        else:
            ce_style = (
                ce_table[DISPLAY_COLS_1HR_BO].style
                .map(style_away_percent, subset=["Away %"])
                .map(style_status, subset=["Status"])
                .pipe(apply_column_tints, CE_COLUMN_TINTS)
                .format(DECIMAL_COLS, na_rep="-")
            )
            st.dataframe(ce_style, width="stretch", hide_index=True, height=table_height(ce_table))

    with col2:
        st.markdown("**Puts (PE)**")
        if pe_table.empty:
            st.info("No PE data available.")
        else:
            pe_style = (
                pe_table[DISPLAY_COLS_1HR_BO].style
                .map(style_away_percent, subset=["Away %"])
                .map(style_status, subset=["Status"])
                .pipe(apply_column_tints, PE_COLUMN_TINTS)
                .format(DECIMAL_COLS, na_rep="-")
            )
            st.dataframe(pe_style, width="stretch", hide_index=True, height=table_height(pe_table))



# ============================================================
# CONFIGURATION (sidebar)
# ============================================================

is_client_view = "UPSTOX_ACCESS_TOKEN" in st.secrets and st.secrets["UPSTOX_ACCESS_TOKEN"].strip() != ""

if is_client_view:
    access_token = st.secrets["UPSTOX_ACCESS_TOKEN"]

    st.markdown("""
        <style>
            [data-testid="stSidebar"] { display: none; }
        </style>
    """, unsafe_allow_html=True)

    auto_refresh = True
    refresh_interval = 15
    expiry_type = "Current Month"

    telegram_bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    telegram_enabled = bool(telegram_bot_token and telegram_chat_id)

else:
    with st.sidebar:
        st.header("Configuration")

        if USE_GIST_PERSISTENCE:
            st.caption("🔒 Persistence: local + Gist backup (survives restarts)")
        else:
            st.caption("⚠️ Persistence: local disk only — resets on app restart/redeploy. See GITHUB_GIST_TOKEN / GITHUB_GIST_ID in the code comments to enable a durable backup.")

        saved_token = load_token()
        access_token = st.text_input("Upstox Access Token", value=saved_token, type="password")

        if access_token and access_token != saved_token:
            save_token(access_token)

        st.markdown("---")
        st.header("Expiry Settings")

        expiry_type = st.radio(
            "Select Expiry Month",
            options=["Current Month", "Next Month"],
            index=0,
            help="Which monthly expiry's ATM options the scanner tracks."
        )

        st.markdown("---")
        st.header("Telegram Alerts")

        telegram_enabled = st.checkbox(
            "Enable Trigger Alerts",
            value=st.session_state.get("telegram_enabled", False),
            key="telegram_enabled",
            help="Sends a Telegram message the moment an option triggers a confirmed 1HR BO breakout (10:15-11:15 IST window only)."
        )

        telegram_bot_token = st.text_input(
            "Bot Token",
            type="password",
            value=st.session_state.get("telegram_bot_token", ""),
            key="telegram_bot_token",
            help="Create a bot via @BotFather on Telegram to get this token."
        )

        telegram_chat_id = st.text_input(
            "Chat ID",
            value=st.session_state.get("telegram_chat_id", ""),
            key="telegram_chat_id",
            help="Your personal or group chat ID. Message @userinfobot to find yours."
        )

        tg_col1, tg_col2 = st.columns(2)
        test_telegram_clicked = tg_col1.button("Send Test", width="stretch")
        reset_alert_state_clicked = tg_col2.button("Reset Alerts", width="stretch")

        if reset_alert_state_clicked:
            save_trigger_alert_state(set())
            st.success("Alert state cleared — already-triggered options will alert again.")

        if test_telegram_clicked:
            success, error = send_telegram_alert(
                telegram_bot_token,
                telegram_chat_id,
                "✅ Test alert from 1HR BO Scanner — Telegram is wired up correctly."
            )
            if success:
                st.success("Test message sent — check Telegram.")
            else:
                st.error(f"Test message failed: {error}")

        st.markdown("---")
        st.header("Auto Refresh")

        auto_refresh = st.checkbox("Enable Auto-Refresh", value=False)
        refresh_interval = st.slider("Refresh Interval (seconds)", min_value=5, max_value=60, value=15)


# ============================================================
# MAIN PAGE — single live scanner:
#   1HR BO: Trigger = 1st hour High, TGT = +20%, SL = -5%, breakout
#           evaluated off the 10:15-11:15 candle.
# ============================================================

st.title("1HR BO Scanner")

run_every = refresh_interval if auto_refresh else None

if not access_token:
    st.warning("Enter your Upstox Access Token in the sidebar first.")
else:
    st.header("1HR Breakout Options (Live)")

    @st.fragment(run_every=run_every)
    def show_1hr_bo():
        ce_table, pe_table = build_open_strike_scanner(
            access_token, expiry_type
        )

        if not ce_table.empty or not pe_table.empty:
            if telegram_enabled:
                combined = pd.concat([ce_table, pe_table], ignore_index=True)
                check_and_alert_1hr_bo(combined, telegram_enabled, telegram_bot_token, telegram_chat_id)

            show_side_by_side(ce_table, pe_table)
        else:
            st.info("No data yet — waiting for market data.")

    show_1hr_bo()
