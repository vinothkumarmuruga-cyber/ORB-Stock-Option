import streamlit as st
import pandas as pd
import numpy as np
import requests
import math
import os
import time
import gzip
import shutil
import json
import re
from datetime import date, datetime, timedelta, timezone
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
import zipfile
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
    page_title="R4 And 1 HR Scanner",
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

        /* Tabs */

        .stTabs [data-baseweb="tab-list"] {
            gap: 10px;
        }

        .stTabs [data-baseweb="tab"] {
            height: 45px;
            white-space: pre-wrap;
            background-color: #f0f2f6;
            border-radius: 5px;
            padding: 10px 20px;
            font-size: 1.1rem;
            font-weight: 600;
            border: 1px solid #d6d6d6;
        }

        .stTabs [aria-selected="true"] {
            background-color: #007bff;
            color: white !important;
            border-color: #007bff;
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

        /* Hide uploader instructions */

        [data-testid="stFileUploaderDropzone"] div div span {
            display: none !important;
        }

        [data-testid="stFileUploaderDropzone"] div div small {
            display: none !important;
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


BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")
TOKEN_FILE = os.path.join(DATA_DIR, "token.json")
META_FILE = os.path.join(DATA_DIR, "meta.json")
LTP_CACHE_FILE = os.path.join(DATA_DIR, "ltp_cache.json")
TRIGGER_ALERT_FILE = os.path.join(DATA_DIR, "trigger_alert_state.json")
ALERT_LOG_FILE = os.path.join(DATA_DIR, "alert_log.csv")
CROSSED_SINCE_FILE = os.path.join(DATA_DIR, "crossed_since.json")


FILES = {
    "Intraday": os.path.join(DATA_DIR, "intraday.csv")
}


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
# META
# ============================================================

def load_meta():
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_meta(key, date_str):
    try:
        meta = load_meta()
        meta[key] = date_str
        with open(META_FILE, "w") as f:
            json.dump(meta, f)
    except:
        pass


# ============================================================
# LTP CACHE (Intraday bhavcopy tab)
# ============================================================

def load_ltp_cache():
    if os.path.exists(LTP_CACHE_FILE):
        try:
            with open(LTP_CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_ltp_cache(new_data):
    try:
        cache = load_ltp_cache()
        cache.update(new_data)
        with open(LTP_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except:
        pass


# ============================================================
# DATE FROM FILE NAME
# ============================================================

def extract_date_from_filename(filename):
    match = re.search(r"(\d{8})", filename)
    if match:
        d = match.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return None


# ============================================================
# ZIP -> CSV
# ============================================================

def extract_csv_from_zip(zip_file):
    try:
        with zipfile.ZipFile(zip_file) as z:
            csv_files = [f for f in z.namelist() if f.lower().endswith(".csv")]
            if not csv_files:
                st.error("No CSV file found in the ZIP archive.")
                return None, None
            csv_filename = csv_files[0]
            with z.open(csv_filename) as f:
                return f.read(), csv_filename
    except Exception as e:
        st.error(f"Error extracting ZIP file: {e}")
        return None, None


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
# BLACKLIST (Intraday tab)
# ============================================================

def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == get_ist_now().strftime("%Y-%m-%d"):
                    return set(data.get("keys", []))
        except:
            pass
    return set()


def save_blacklist(keys):
    try:
        data = {
            "date": get_ist_now().strftime("%Y-%m-%d"),
            "keys": list(keys)
        }
        with open(BLACKLIST_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass


# ============================================================
# CROSSED-SINCE TRACKER (1HR BO tab)
#
# Records the first moment each option was seen with LTP >= Trigger
# today, so the table can show "how long ago" a breakout happened
# instead of just a static "crossed / not crossed" flag. Once a
# symbol's first-cross time is recorded it's never overwritten again
# that day, even if the option later pulls back below Trigger. Local
# disk only (not backed by the Gist) — losing this on a rare restart
# just resets the "since" clock for that symbol, it doesn't cause
# duplicate alerts or any other correctness issue.
# ============================================================

def load_crossed_since():
    if os.path.exists(CROSSED_SINCE_FILE):
        try:
            with open(CROSSED_SINCE_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == get_ist_now().strftime("%Y-%m-%d"):
                    return data.get("times", {})
        except:
            pass
    return {}


def save_crossed_since(times):
    try:
        data = {
            "date": get_ist_now().strftime("%Y-%m-%d"),
            "times": times
        }
        with open(CROSSED_SINCE_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass


def format_since(iso_timestamp):
    """'12m ago' / '1h 5m ago' style label for a crossed-since timestamp."""
    if not iso_timestamp:
        return "-"
    try:
        crossed_dt = datetime.fromisoformat(iso_timestamp)
    except Exception:
        return "-"

    delta_minutes = int((get_ist_now() - crossed_dt).total_seconds() // 60)
    if delta_minutes < 1:
        return "just now"
    if delta_minutes < 60:
        return f"{delta_minutes}m ago"
    hours, minutes = divmod(delta_minutes, 60)
    return f"{hours}h {minutes}m ago"


# ============================================================
# TELEGRAM TRIGGER-ALERT STATE (shared across both tabs)
#
# Persisted to disk (and, if configured, to the Gist backup — see the
# "OPTIONAL EXTERNAL PERSISTENCE" section above) so alert
# de-duplication survives restarts. Without a durable backup, a
# Streamlit Cloud restart mid-day wipes this file and can cause
# duplicate Telegram alerts for options that already fired earlier.
# Resets automatically each new trading day. Each entry is
# "<tab>:<id>" so Intraday / 1HR BO track their own alert
# history independently, but a single Reset Alerts button in
# the sidebar clears everything at once.
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
# ALERT LOG (CSV) — every fired alert (Intraday R4 or 1HR BO) gets one
# row here: when it crossed, at what LTP, and what the Trigger/TGT/SL
# levels were at that moment. Lets you go back later and check whether
# price actually reached TGT before SL, instead of trusting the fixed
# TGT/SL percentages blind.
# ============================================================

# ============================================================
# ALERT LOG (CSV) — every fired alert (Intraday R4 or 1HR BO) gets one
# row here: when it crossed, at what LTP, and what the Trigger/TGT/SL
# levels were at that moment. Lets you go back later and check whether
# price actually reached TGT before SL, instead of trusting the fixed
# TGT/SL percentages blind. Also mirrored to the Gist backup (if
# configured) so the day's alert history isn't lost on a restart —
# see the "OPTIONAL EXTERNAL PERSISTENCE" section above.
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


def check_and_alert_triggers(df, key_suffix, telegram_enabled, bot_token, chat_id):
    """
    Intraday bhavcopy tab: alerts the moment an option's change %
    (LTP / Trigger x 100) crosses 100%.
    """
    if not telegram_enabled:
        return
    if df.empty:
        return
    if "instrument_key" not in df.columns:
        return

    alerted = load_trigger_alert_state()
    newly_triggered = []

    for _, row in df.iterrows():
        inst_key = row.get("instrument_key")
        if not inst_key:
            continue

        alert_id = f"{key_suffix}:{inst_key}"

        try:
            change_pct = float(row.get("change %", 0.0))
        except:
            continue

        if change_pct >= 100 and alert_id not in alerted:
            newly_triggered.append((alert_id, row))

    if not newly_triggered:
        return

    # One Telegram message per option, sent and persisted independently.
    # If one send fails (bad formatting, transient network issue, etc.)
    # it no longer blocks or gets rolled back with the rest of the batch
    # — every other option in this batch still alerts and gets recorded.
    sent_count = 0
    fail_count = 0

    for alert_id, row in newly_triggered:
        message = (
            f"🚀 <b>R4 Trigger Crossed — {key_suffix}</b>\n\n"
            f"<b>{row['Symbol']} {row['StrikePrice']:.0f} {row['OptionType']}</b>\n"
            f"LTP: {row['ltp']:.2f}  ›  Cam R4: {row['Trigger']:.2f}\n"
            f"Change: {row['change %']:.2f}%"
        )

        success, error = send_telegram_alert(bot_token, chat_id, message)

        if success:
            alerted.add(alert_id)
            save_trigger_alert_state(alerted)
            log_alert_event(
                key_suffix,
                f"{row['Symbol']} {row['StrikePrice']:.0f} {row['OptionType']}",
                row['ltp'],
                row['Trigger']
            )
            sent_count += 1
        else:
            fail_count += 1

    if sent_count:
        st.toast(f"Telegram alert sent for {sent_count} trigger cross(es) on {key_suffix}.", icon="🚀")
    if fail_count:
        st.toast(f"{fail_count} Telegram alert(s) failed on {key_suffix} — will retry next refresh.", icon="⚠️")


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
    Uses the SAME persisted alert-state file as the Intraday tab
    (tagged "1HR BO:<symbol>"). The crossed flag is checked internally
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

    # One Telegram message per option — same reasoning as the Intraday
    # tab above. Also logs each fired alert (LTP/Trigger/TGT/SL at the
    # moment of crossing) to the CSV alert log for later TGT/SL review.
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
# NSE JSON (local file — used by Intraday tab's ATM match)
# ============================================================

NSE_JSON_PATH = "NSE.json"


@st.cache_data
def load_nse_json():
    if os.path.exists(NSE_JSON_PATH):
        try:
            df = pd.read_json(NSE_JSON_PATH)
            if "segment" in df.columns:
                df = df[df["segment"] == "NSE_FO"]
            df["expiry_dt"] = pd.to_datetime(df["expiry"], unit="ms").dt.normalize()
            return df
        except Exception as e:
            st.error(f"Error loading NSE.json: {e}")
            return pd.DataFrame()
    else:
        st.error(f"NSE.json not found at {NSE_JSON_PATH}")
        return pd.DataFrame()


# ============================================================
# LIVE INSTRUMENT FILE (1HR BO tab — fetched directly from Upstox,
# independent of the local NSE.json file above)
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
# PROCESS BHAVCOPY (Intraday tab)
# ============================================================

def process_bhavcopy(bhav_file, df_json, target_expiry_index=0):
    try:
        df_bhav = pd.read_csv(bhav_file)

        required_cols = ["FinInstrmTp", "TckrSymb", "XpryDt", "ClsPric", "StrkPric", "OptnTp", "HghPric", "LwPric", "LastPric"]
        if not all(col in df_bhav.columns for col in required_cols):
            st.error(f"Uploaded file missing required columns: {required_cols}")
            return pd.DataFrame()

        futures = df_bhav[df_bhav["FinInstrmTp"].isin(["STF", "IDF"])].copy()
        if futures.empty:
            st.warning("No Futures data found in uploaded file.")
            return pd.DataFrame()

        futures["XpryDt"] = pd.to_datetime(futures["XpryDt"])

        ist_now = get_ist_now()
        today = ist_now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)

        futures = futures[futures["XpryDt"] >= today]
        if futures.empty:
            st.warning("No future expiries found in the uploaded file.")
            return pd.DataFrame()

        futures = futures.sort_values("XpryDt")
        available_expiries = sorted(futures["XpryDt"].unique())

        if not available_expiries:
            st.warning("No future expiry dates found in uploaded file.")
            return pd.DataFrame(), None, []

        if target_expiry_index >= len(available_expiries):
            target_expiry = available_expiries[-1]
        else:
            target_expiry = available_expiries[target_expiry_index]

        near_futures = futures[futures["XpryDt"] == target_expiry].copy()
        near_futures = near_futures[["TckrSymb", "ClsPric", "XpryDt"]]
        near_futures = near_futures.rename(columns={"ClsPric": "FuturePrice", "XpryDt": "FutureExpiryDate"})

        options = df_bhav[df_bhav["OptnTp"].isin(["CE", "PE"])].copy()
        if options.empty:
            st.warning("No Options data found in uploaded file.")
            return pd.DataFrame(), target_expiry, available_expiries

        options["XpryDt"] = pd.to_datetime(options["XpryDt"])

        merged = pd.merge(options, near_futures, on="TckrSymb")
        merged = merged[merged["XpryDt"] == merged["FutureExpiryDate"]]

        merged["Diff"] = abs(merged["StrkPric"] - merged["FuturePrice"])

        best_strikes = merged[["TckrSymb", "StrkPric", "Diff"]].drop_duplicates()
        best_strikes = best_strikes.sort_values(by=["TckrSymb", "Diff", "StrkPric"])
        best_strikes = best_strikes.groupby("TckrSymb").first().reset_index()

        atm_options = pd.merge(merged, best_strikes[["TckrSymb", "StrkPric"]], on=["TckrSymb", "StrkPric"])
        atm_rows = atm_options[[
            "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "FuturePrice", "ClsPric",
            "FinInstrmNm", "HghPric", "LwPric", "LastPric"
        ]].copy()

        atm_rows["XpryDt"] = atm_rows["XpryDt"].dt.normalize()

        result = pd.merge(
            atm_rows,
            df_json,
            left_on=["TckrSymb", "StrkPric", "OptnTp", "XpryDt"],
            right_on=["underlying_symbol", "strike_price", "instrument_type", "expiry_dt"],
            how="inner"
        )

        if result.empty and not atm_rows.empty:
            st.error(
                "Data mismatch: Found options in Bhavcopy but couldn't find "
                "them in NSE.json. Please update NSE.json via the sidebar."
            )

        final_df = result[[
            "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
            "FuturePrice", "ClsPric", "instrument_key",
            "HghPric", "LwPric", "LastPric"
        ]].copy()

        final_df = final_df.rename(columns={
            "TckrSymb": "Symbol",
            "XpryDt": "ExpiryDate",
            "StrkPric": "StrikePrice",
            "OptnTp": "OptionType",
            "ClsPric": "Trigger",
            "HghPric": "HighPrice",
            "LwPric": "LowPrice",
            "LastPric": "LastPrice"
        })

        # --------------------------------------------------------
        # Camarilla levels — computed off the raw close price.
        #   R3 = C + (H-L) * 1.1/4   -> used as SL
        #   R4 = C + (H-L) * 1.1/2   -> used as Trigger ("Cam R4")
        #   R5 = (H/L) * C           -> used as TGT
        #   R6 = R5 + 1.1*(R5-R4)    -> kept for reference
        # --------------------------------------------------------
        close = final_df["Trigger"]
        hl_range = final_df["HighPrice"] - final_df["LowPrice"]

        final_df["Camarilla_R3"] = close + hl_range * 1.1 / 4
        final_df["Camarilla_R4"] = close + hl_range * 1.1 / 2
        final_df["Camarilla_R5"] = (final_df["HighPrice"] / final_df["LowPrice"]) * close
        final_df["Camarilla_R6"] = final_df["Camarilla_R5"] + 1.1 * (final_df["Camarilla_R5"] - final_df["Camarilla_R4"])

        return final_df, target_expiry, available_expiries

    except Exception as e:
        st.error(f"Error processing file: {e}")
        return pd.DataFrame(), None, []


# ============================================================
# FETCH LTP — Intraday bhavcopy tab (Upstox v3 LTP endpoint)
# ============================================================

def fetch_ltp(instrument_keys, token):
    if not token:
        return {}

    url = "https://api.upstox.com/v3/market-quote/ltp"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    batch_size = 50
    ltp_map = {}

    batches = [instrument_keys[i:i + batch_size] for i in range(0, len(instrument_keys), batch_size)]

    def fetch_batch(batch):
        params = {"instrument_key": ",".join(batch)}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    quotes = data.get("data", {})
                    result = {}
                    for key, details in quotes.items():
                        inst_token = details.get("instrument_token")
                        last_price = details.get("last_price")
                        if inst_token is not None:
                            result[inst_token] = last_price
                    return result
        except Exception:
            pass
        return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_batch, batch) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            try:
                batch_result = future.result()
                if batch_result:
                    ltp_map.update(batch_result)
            except Exception:
                pass

    return ltp_map


# ============================================================
# DISPLAY — Intraday option chain
# ============================================================

def display_option_chain(df, access_token, key_suffix, telegram_enabled=False, telegram_bot_token="", telegram_chat_id=""):
    st.caption(f"Last Updated: {get_ist_now().strftime('%H:%M:%S')} IST")
    if df.empty:
        st.info("No data to display. Please upload a valid Bhavcopy in the sidebar.")
        return

    if access_token:
        all_keys = df["instrument_key"].dropna().unique().tolist()

        ist_now = get_ist_now()
        current_time = ist_now.time()
        start_time = datetime.strptime("09:00", "%H:%M").time()
        end_time = datetime.strptime("15:40", "%H:%M").time()

        is_market_hours = start_time <= current_time <= end_time

        ltp_cache = load_ltp_cache()
        missing_keys = [k for k in all_keys if k not in ltp_cache]

        force_refresh = st.session_state.get("force_refresh_ltp", False)

        should_fetch = False

        if is_market_hours:
            should_fetch = True
        elif force_refresh:
            should_fetch = True
            st.session_state["force_refresh_ltp"] = False
        elif missing_keys:
            should_fetch = True

        if should_fetch:
            keys_to_fetch = all_keys if is_market_hours else missing_keys
            fetched_data = fetch_ltp(keys_to_fetch, access_token)
            if fetched_data:
                save_ltp_cache(fetched_data)
                ltp_cache = load_ltp_cache()

        ltp_data = {k: ltp_cache.get(k, 0.0) for k in all_keys}
        df["ltp"] = df["instrument_key"].map(ltp_data).fillna(0.0)
    else:
        df["ltp"] = 0.0
        st.warning("Enter Access Token in sidebar to see live LTP.")

    # Trigger = Camarilla R4, TGT = Camarilla R5, SL = Camarilla R3
    if "Camarilla_R4" in df.columns:
        df["Trigger"] = df["Camarilla_R4"]
    if "Camarilla_R5" in df.columns:
        df["TGT"] = df["Camarilla_R5"]
    if "Camarilla_R3" in df.columns:
        df["SL"] = df["Camarilla_R3"]

    def calculate_numeric_change(row):
        try:
            ocp = float(row["Trigger"])
            ltp = float(row["ltp"])
            if ocp > 0 and ltp > 0:
                return (ltp / ocp * 100)
            return 0.0
        except:
            return 0.0

    df["change_val"] = df.apply(calculate_numeric_change, axis=1)
    df["change %"] = df["change_val"]

    blacklist = load_blacklist()
    current_time = get_ist_now().time()
    cutoff_time = datetime.strptime("09:30", "%H:%M").time()

    if current_time < cutoff_time:
        violators = df[df["change %"] >= 100]["instrument_key"].tolist()
        if violators:
            blacklist.update(violators)
            save_blacklist(blacklist)

    if blacklist:
        df = df[~df["instrument_key"].isin(blacklist)]

    # Telegram trigger alerts — full CE+PE set, before the split below.
    check_and_alert_triggers(df, key_suffix, telegram_enabled, telegram_bot_token, telegram_chat_id)

    calls_df = df[df["OptionType"] == "CE"].copy()
    puts_df = df[df["OptionType"] == "PE"].copy()

    calls_df = calls_df.sort_values(by="change %", ascending=False)
    puts_df = puts_df.sort_values(by="change %", ascending=False)

    # Rename Trigger -> "Cam R4" for display only (internal calcs above
    # still use the "Trigger" column name).
    calls_display = calls_df.rename(columns={"Trigger": "Cam R4"})
    puts_display = puts_df.rename(columns={"Trigger": "Cam R4"})

    display_cols = ["Symbol", "StrikePrice", "Cam R4", "TGT", "SL", "ltp", "change %"]

    def color_change(val):
        try:
            val = float(val)
        except:
            return ""
        if val >= 100:
            return "background-color: darkgreen; color: white; font-weight: bold;"
        elif val >= 80:
            return "background-color: lightgreen; color: black;"
        return ""

    format_dict = {
        "change %": "{:.2f}%",
        "Cam R4": "{:.2f}",
        "TGT": "{:.2f}",
        "SL": "{:.2f}",
        "ltp": "{:.2f}",
        "StrikePrice": "{:.2f}"
    }

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Calls (CE)")
        st.dataframe(
            calls_display[display_cols].style
            .map(color_change, subset=["change %"])
            .format(format_dict)
            .set_properties(**{"font-weight": "600", "text-align": "center", "font-size": "16px"}),
            hide_index=True,
            width="stretch",
            height=1800
        )

    with col2:
        st.subheader("Puts (PE)")
        st.dataframe(
            puts_display[display_cols].style
            .map(color_change, subset=["change %"])
            .format(format_dict)
            .set_properties(**{"font-weight": "600", "text-align": "center", "font-size": "16px"}),
            hide_index=True,
            width="stretch",
            height=1800
        )


# ============================================================
# 1HR BO — LIVE ORB SCANNER (uses Upstox v3 OHLC/LTP + intraday
# hourly candles directly, independent of the Intraday bhavcopy tab)
#
#   Trigger = high of the first 1-hour candle (9:15-10:15 IST).
#   TGT = Trigger + 20%, SL = Trigger - 5%.
#   Breakout is only ever evaluated off the NEXT one-hour candle
#   (10:15-11:15 IST) — the window is enforced by
#   ORB_ENTRY_WINDOW_START / ORB_ENTRY_WINDOW_END above.
#   "Crossed" (valid breakout) requires BOTH:
#     1) live LTP >= Trigger, evaluated only inside the 10:15-11:15
#        window (no entries before 10:15 or after 11:15), AND
#     2) the breakout-quality filter: the low of the 10:15 candle
#        (so far) did not drop more than BREAKOUT_DROP_PCT_LIMIT
#        (5%) below Trigger before crossing back above it.
#   Used only to fire the Telegram alert — no Breakout column shown
#   in the table, though the Drop % column surfaces the same check.
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
            # actually evaluated against. While that hour is still forming,
            # this is its running low so far; once the hour closes it's the
            # final low. Not present yet before 10:15.
            second_hour_low = None
            if len(candles_sorted) >= 2:
                second_hour_low = candles_sorted[1][3]

            info = {
                "trigger": trigger,
                "second_hour_low": second_hour_low,
            }
            return instrument_key, info, None

        except Exception as e:
            return instrument_key, None, f"Exception: {e}"


@st.cache_data(ttl=15, show_spinner="Computing 1HR BO trigger levels...")
def fetch_orb_map(instrument_keys, headers_tuple):
    headers = dict(headers_tuple)
    result = {}
    sample_errors = []

    instrument_keys = list(instrument_keys)

    # Fetching the ORB candle for ~100 instruments at once was hitting
    # Cloudflare's rate limiter (HTTP 429) in front of Upstox. Spread the
    # requests out: modest concurrency (5 at a time) processed in small
    # batches with a short pause between batches, instead of firing
    # everything at once with 12 workers.
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


def build_open_strike_scanner(access_token, expiry_choice, top_n=50, min_volume=0, min_lots=0):
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

    selected["Ctr"] = np.where(selected["Lot"] > 0, selected["volume"] / selected["Lot"], np.nan)
    selected["Capital"] = selected["ltp"] * selected["Lot"]

    selected = selected.rename(columns={"ltp": "LTP", "volume": "Vol", "Capital": "Capital Required"})

    # Top-N cutoff — the N CE and N PE with the biggest day Chg% are
    # tracked (N=50 by default), out of every stock's ATM CE/PE universe.
    ce_candidates = selected[selected["Symbol"].str.endswith("CE")].sort_values("Chg%", ascending=False).head(top_n)
    pe_candidates = selected[selected["Symbol"].str.endswith("PE")].sort_values("Chg%", ascending=False).head(top_n)
    shortlisted = pd.concat([ce_candidates, pe_candidates], ignore_index=True)

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

    # Breakout only evaluated off the 10:15-11:15 candle (the "next one
    # hour" candle) — no entries before 10:15 or after 11:15.
    within_entry_window = ORB_ENTRY_WINDOW_START <= get_ist_now().time() <= ORB_ENTRY_WINDOW_END

    # Breakout-quality filter: the 10:15 candle's low must not have
    # dropped more than BREAKOUT_DROP_PCT_LIMIT (5%) below Trigger before
    # price crosses back above it. NaN Drop % (no second-hour candle yet)
    # fails the filter, same as before 10:15.
    quality_ok = shortlisted["Drop %"].notna() & (shortlisted["Drop %"] <= BREAKOUT_DROP_PCT_LIMIT)

    # Internal-only flag: used purely to fire the Telegram alert.
    # Not shown as a column in the table (Breakout column removed).
    shortlisted["_crossed"] = (
        within_entry_window
        & shortlisted["Trigger"].notna()
        & quality_ok
        & (shortlisted["LTP"] >= shortlisted["Trigger"])
    )

    shortlisted["Away %"] = np.where(
        shortlisted["Trigger"] > 0,
        (shortlisted["LTP"] / shortlisted["Trigger"]) * 100,
        np.nan
    )
    shortlisted["Away %"] = shortlisted["Away %"].clip(lower=0)

    result = shortlisted[[
        "Symbol", "Open", "LTP", "Trigger", "Away %", "Drop %", "TGT", "SL", "_crossed", "Vol", "Ctr", "Lot", "Capital Required"
    ]].copy()

    for col in ["Open", "Trigger", "TGT", "SL", "LTP", "Away %", "Drop %"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(2)

    result["Vol"] = pd.to_numeric(result["Vol"], errors="coerce").fillna(0).astype(int)
    result["Ctr"] = pd.to_numeric(result["Ctr"], errors="coerce").fillna(0).astype(int)
    result["Lot"] = pd.to_numeric(result["Lot"], errors="coerce").fillna(0).astype(int)
    result["Capital Required"] = pd.to_numeric(result["Capital Required"], errors="coerce").round(0).fillna(0).astype(int)

    # Hide rows with no Trigger (ORB level not fetched yet — before
    # 10:15 IST, or dropped due to a rate-limited/failed API call). Only
    # options with an actual computed Trigger/TGT/SL get plotted; the
    # diagnostics expander above already explains why the rest are missing.
    result = result[result["Trigger"].notna()].reset_index(drop=True)

    # Volume / Lots filter ("Strong BO") — a strike can show a big Away %
    # purely from one or two stale/illiquid trades. Filtering on today's
    # traded volume (or lots traded — Ctr = Vol / Lot, comparable across
    # stocks with very different lot sizes) weeds those out so what's
    # left is breakouts with real participation behind them. A value of
    # 0 disables that particular filter.
    if min_volume > 0:
        result = result[result["Vol"] >= min_volume].reset_index(drop=True)
    if min_lots > 0:
        result = result[result["Ctr"] >= min_lots].reset_index(drop=True)

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
# be <= BREAKOUT_DROP_PCT_LIMIT for a valid breakout).
DISPLAY_COLS_1HR_BO = ["Symbol", "Open", "LTP", "Trigger", "Away %", "Drop %", "TGT", "SL", "Vol", "Ctr", "Lot", "Capital Required"]


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
                .pipe(apply_column_tints, PE_COLUMN_TINTS)
                .format(DECIMAL_COLS, na_rep="-")
            )
            st.dataframe(pe_style, width="stretch", hide_index=True, height=table_height(pe_table))


# ============================================================
# CONFIGURATION (shared sidebar for both tabs)
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
    min_volume = 0
    min_lots = 0

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
            help="Used by the 1HR BO tab. Intraday tab always uses the nearest expiry."
        )

        st.markdown("---")
        st.header("1HR BO Filters")

        min_volume = st.number_input(
            "Min Volume — shares (Strong BO filter)",
            min_value=0,
            value=0,
            step=50000,
            help=(
                "Hides options from the 1HR BO tables whose today's traded "
                "volume (in shares) is below this. Set 0 to disable."
            )
        )

        min_lots = st.number_input(
            "Min Lots traded — Ctr (Strong BO filter)",
            min_value=0,
            value=0,
            step=50,
            help=(
                "Hides options whose today's traded Lots (Ctr = Volume / "
                "Lot size) is below this. Lot sizes vary a lot across "
                "stocks (25 to a few thousand shares), so filtering by "
                "lots traded is a fairer 'strong participation' measure "
                "across different stocks than a flat volume threshold. "
                "Set 0 to disable. Use either filter, or both together."
            )
        )

        st.markdown("---")
        st.header("Telegram Alerts")

        telegram_enabled = st.checkbox(
            "Enable Trigger Alerts",
            value=st.session_state.get("telegram_enabled", False),
            key="telegram_enabled",
            help="Sends a Telegram message the moment either tab's option LTP crosses its Trigger price. 1HR BO alerts only fire after 10:15 IST."
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
            st.success("Alert state cleared for both tabs — already-triggered options will alert again.")

        if test_telegram_clicked:
            success, error = send_telegram_alert(
                telegram_bot_token,
                telegram_chat_id,
                "✅ Test alert from Stock Option Scanner — Telegram is wired up correctly."
            )
            if success:
                st.success("Test message sent — check Telegram.")
            else:
                st.error(f"Test message failed: {error}")

        st.markdown("---")
        st.header("Data Management")

        if st.button("⚡ Refresh LTP Now", width="stretch"):
            st.session_state["force_refresh_ltp"] = True
            st.rerun()

        st.subheader("NSE Instrument JSON (Intraday match)")

        if st.button("🔄 Download Latest"):
            try:
                with st.spinner("Downloading latest NSE.json..."):
                    url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
                    headers = {"User-Agent": "Mozilla/5.0"}
                    response = requests.get(url, headers=headers, stream=True)

                    if response.status_code == 200:
                        with open(NSE_JSON_PATH, "wb") as f_out:
                            with gzip.GzipFile(fileobj=response.raw) as f_in:
                                shutil.copyfileobj(f_in, f_out)
                        st.cache_data.clear()
                        st.success("Updated successfully!")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"Failed to download. Status: {response.status_code}")
            except Exception as e:
                st.error(f"Error: {e}")

        st.subheader("Intraday Bhavcopy")
        up_i = st.file_uploader("Upload Intraday Bhavcopy", type=["zip"], key="i_up")
        if up_i is not None:
            csv_content, csv_name = extract_csv_from_zip(up_i)
            if csv_content:
                with open(FILES["Intraday"], "wb") as f:
                    f.write(csv_content)
                date_str = extract_date_from_filename(csv_name)
                if date_str:
                    save_meta("Intraday", date_str)
                st.success(f"Intraday file updated from {csv_name}!")

        meta = load_meta()
        if "Intraday" in meta and os.path.exists(FILES["Intraday"]):
            st.caption(f"📅 Data Date: {meta['Intraday']}")
        elif os.path.exists(FILES["Intraday"]):
            i_time = os.path.getmtime(FILES["Intraday"])
            st.caption(f"📅 Last Updated: {datetime.fromtimestamp(i_time).strftime('%Y-%m-%d %H:%M')}")

        st.markdown("---")
        st.header("Auto Refresh")
        st.caption("Drives both the Intraday and 1HR BO tabs.")

        auto_refresh = st.checkbox("Enable Auto-Refresh", value=False)
        refresh_interval = st.slider("Refresh Interval (seconds)", min_value=5, max_value=60, value=15)


# ============================================================
# MAIN PAGE
# ============================================================

st.title("Stock Option Scanner")

nse_json_df = load_nse_json()

if not nse_json_df.empty:
    tab_intraday, tab_1hr_bo = st.tabs(["Intraday", "1HR BO"])

    run_every = refresh_interval if auto_refresh else None

    # --------------------------------------------------------
    # INTRADAY
    # --------------------------------------------------------
    with tab_intraday:
        st.header("Intraday Options")
        if os.path.exists(FILES["Intraday"]):
            @st.fragment(run_every=run_every)
            def show_intraday():
                df_i, target_exp, all_exps = process_bhavcopy(FILES["Intraday"], nse_json_df, target_expiry_index=0)
                if target_exp:
                    st.info(f"📅 Displaying Expiry: **{target_exp.strftime('%d-%b-%Y')}**")
                display_option_chain(df_i, access_token, "Intraday", telegram_enabled, telegram_bot_token, telegram_chat_id)
            show_intraday()
        else:
            st.warning("Intraday Bhavcopy file not found. Please upload in the sidebar.")

    # --------------------------------------------------------
    # 1HR BO — live ORB scanner (Trigger = 1st hour High, TGT=+35%, SL=-20%)
    # --------------------------------------------------------
    with tab_1hr_bo:
        st.header("1HR Breakout Options (Live)")

        if not access_token:
            st.warning("Enter your Upstox Access Token in the sidebar first.")
        else:
            @st.fragment(run_every=run_every)
            def show_1hr_bo():
                ce_table, pe_table = build_open_strike_scanner(
                    access_token, expiry_type, top_n=50, min_volume=min_volume, min_lots=min_lots
                )

                filter_notes = []
                if min_volume > 0:
                    filter_notes.append(f"Vol ≥ {min_volume:,}")
                if min_lots > 0:
                    filter_notes.append(f"Lots ≥ {min_lots:,}")
                if filter_notes:
                    st.caption("Strong BO filter active — showing only options with " + " and ".join(filter_notes))

                if not ce_table.empty or not pe_table.empty:
                    if telegram_enabled:
                        combined = pd.concat([ce_table, pe_table], ignore_index=True)
                        check_and_alert_1hr_bo(combined, telegram_enabled, telegram_bot_token, telegram_chat_id)

                    show_side_by_side(ce_table, pe_table)
                else:
                    st.info("No data yet — click Refresh LTP Now or wait for market data.")

            show_1hr_bo()

else:
    st.error("Critical Error: NSE.json could not be loaded.")
