import streamlit as st
import json
import datetime
import requests
from pathlib import Path
from trading212_client import Trading212Client

st.set_page_config(
    page_title="T212 Algo Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

CONFIG_FILE = "config.json"
LOG_FILE    = "logs/bot.log"

# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

config = load_config()

if "tickers" not in st.session_state:
    st.session_state.tickers = config.get("tickers", [])

if "settings_open" not in st.session_state:
    st.session_state.settings_open = False

# ──────────────────────────────────────────────────────────────────────────
# Minimal CSS — dark card style, no sidebar clutter
# ──────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
    [data-testid="collapsedControl"] { display: none; }
    [data-testid="stSidebar"]        { display: none; }
    .metric-card {
        background: #1e1e2e;
        border-radius: 12px;
        padding: 18px 22px;
        margin-bottom: 8px;
    }
    .section-title {
        font-size: 1.1rem;
        font-weight: 600;
        margin: 20px 0 8px 0;
        color: #a0aec0;
        text-transform: uppercase;
        letter-spacing: .08em;
    }
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────────────────────────────────────
# Live API client
# ──────────────────────────────────────────────────────────────────────────

api_key    = config.get("api_key",    "")
api_secret = config.get("api_secret", "")
api_mode   = config.get("api_mode",   "Practice")
client     = None
equity     = {"free": 0.0, "total": 0.0, "invested": 0.0, "ppl": 0.0}

if api_key:
    try:
        client = Trading212Client(api_key, api_secret, api_mode)
        equity = client.get_account_cash() or equity
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────
# Settings overlay (st.dialog — Streamlit ≥ 1.35)
# ──────────────────────────────────────────────────────────────────────────

@st.dialog("⚙️ Settings", width="large")
def show_settings():
    tab_api, tab_tickers, tab_strategy, tab_diag = st.tabs([
        "🔑 API & Control",
        "📋 Watchlist",
        "🧠 Strategy",
        "🛠 Diagnostics",
    ])

    # ── API & Bot Control ─────────────────────────────────────────────────
    with tab_api:
        st.subheader("API Credentials")
        new_key    = st.text_input("API Key",    value=config.get("api_key",    ""), type="password")
        new_secret = st.text_input("API Secret", value=config.get("api_secret", ""), type="password")
        new_mode   = st.selectbox("Account Mode", ["Practice", "Live"],
                                  index=0 if config.get("api_mode") == "Practice" else 1)

        st.subheader("Bot Control")
        status_opts = ["RUNNING", "PAUSED", "LOCKED"]
        cur_status  = config.get("bot_status", "LOCKED")
        new_status  = st.radio("Bot Status", status_opts,
                               index=status_opts.index(cur_status) if cur_status in status_opts else 2,
                               horizontal=True)
        if new_status == "LOCKED":
            st.error("⚠️ Bot is LOCKED. Review Diagnostics logs before resuming.")

        st.subheader("Risk & Cycle")
        r1, r2, r3 = st.columns(3)
        new_risk   = r1.number_input("Risk/Trade (%)", min_value=0.1, max_value=10.0, step=0.1,
                                     value=round(float(config.get("risk_per_trade_pct", 0.01)) * 100, 2)) / 100
        new_sl_atr = r2.number_input("SL ATR Mult",   min_value=0.5, max_value=5.0,  step=0.1,
                                     value=float(config.get("sl_atr_multiplier", 1.5)))
        new_maxpos = r3.number_input("Max Positions",  min_value=1,   max_value=50,
                                     value=int(config.get("max_open_positions", 5)))
        new_cap    = st.slider("Capital Utilisation (%)", 10, 100,
                               int(float(config.get("capital_utilization_pct", 0.95)) * 100))
        new_cycle  = st.number_input("Cycle Interval (secs)", step=60,
                                     value=int(config.get("cycle_interval_secs", 900)))
        new_mkt    = st.toggle("Market Hours Guard (US session only)",
                               value=bool(config.get("market_hours_check", True)))
        new_regime = st.text_input("Regime Filter Ticker (blank = disabled)",
                                   value=config.get("regime_ticker", "SPY"))

        if st.button("💾 Save API & Control", use_container_width=True):
            config.update({
                "api_key":               new_key,
                "api_secret":            new_secret,
                "api_mode":              new_mode,
                "bot_status":            new_status,
                "risk_per_trade_pct":    new_risk,
                "sl_atr_multiplier":     new_sl_atr,
                "max_open_positions":    new_maxpos,
                "capital_utilization_pct": new_cap / 100,
                "cycle_interval_secs":   new_cycle,
                "market_hours_check":    new_mkt,
                "regime_ticker":         new_regime.strip() or None,
            })
            save_config(config)
            st.success("✅ Saved — bot picks up changes on the next cycle.")

    # ── Watchlist ─────────────────────────────────────────────────────────
    with tab_tickers:
        tickers = st.session_state.tickers
        st.subheader(f"Watchlist ({len(tickers)} tickers)")

        if tickers:
            cols = st.columns(5)
            for i, t in enumerate(tickers):
                cols[i % 5].code(t)

            st.markdown("---")
            remove_choice = st.selectbox("Remove a Ticker", [""] + tickers)
            if st.button("🗑 Remove") and remove_choice:
                st.session_state.tickers.remove(remove_choice)
                st.rerun()
        else:
            st.info("No tickers yet. Add some below.")

        st.markdown("---")
        st.subheader("Add Ticker")
        c1, c2 = st.columns([3, 1])
        manual_in = c1.text_input("Ticker symbol (e.g. NVDA_US_EQ)", label_visibility="collapsed", placeholder="e.g. NVDA_US_EQ")
        if c2.button("➕ Add", use_container_width=True) and manual_in.strip():
            sym = manual_in.strip().upper()
            if sym not in st.session_state.tickers:
                st.session_state.tickers.append(sym)
                st.rerun()
            else:
                st.warning(f"{sym} already in watchlist.")

        with st.expander("🔍 Search Yahoo Finance"):
            search_q = st.text_input("Search term", "")
            if search_q:
                try:
                    url    = f"https://query2.finance.yahoo.com/v1/finance/search?q={search_q}"
                    resp   = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    quotes = resp.json().get("quotes", [])
                    found  = [f"{q['symbol']} — {q.get('shortname','')}" for q in quotes if "symbol" in q]
                    if found:
                        pick = st.selectbox("Results", found)
                        if st.button("➕ Add from Search"):
                            sym = pick.split(" ")[0].strip().upper()
                            if sym not in st.session_state.tickers:
                                st.session_state.tickers.append(sym)
                                st.rerun()
                    else:
                        st.info("No results.")
                except Exception:
                    st.error("Search failed.")

        st.markdown("---")
        left, right = st.columns(2)
        uploaded = left.file_uploader("Import JSON", type=["json"])
        if uploaded:
            try:
                data        = json.load(uploaded)
                import_list = data if isinstance(data, list) else data.get("combined_list", data.get("tickers", []))
                before      = len(st.session_state.tickers)
                merged      = list(dict.fromkeys(st.session_state.tickers + import_list))
                st.session_state.tickers = merged
                left.success(f"Imported {len(merged) - before} new tickers.")
            except Exception:
                left.error("Invalid JSON.")

        if tickers:
            right.download_button("📥 Export Watchlist", data=json.dumps(tickers, indent=4),
                                  file_name="trdbt_tickers.json", mime="application/json",
                                  use_container_width=True)

        if st.button("💾 Save Watchlist", use_container_width=True):
            config["tickers"] = st.session_state.tickers
            save_config(config)
            st.success("✅ Watchlist saved.")

    # ── Strategy ──────────────────────────────────────────────────────────
    with tab_strategy:
        preset_options = [
            "Ultra Conservative", "Conservative", "Moderate",
            "Aggressive", "Ultra Aggressive", "Manual Custom",
        ]
        preset_map = {
            "Ultra Conservative": (20, 3.0, 14, 20),
            "Conservative":       (20, 2.5, 14, 25),
            "Moderate":           (20, 2.0, 14, 30),
            "Aggressive":         (20, 1.5, 14, 40),
            "Ultra Aggressive":   (10, 1.0,  7, 50),
        }
        cur_preset  = config.get("preset_mode", "Conservative")
        preset_idx  = preset_options.index(cur_preset) if cur_preset in preset_options else len(preset_options) - 1
        preset_mode = st.selectbox("Preset", preset_options, index=preset_idx)

        if preset_mode in preset_map:
            bb_len, bb_std, rsi_len, rsi_thr = preset_map[preset_mode]
            pc = st.columns(4)
            pc[0].metric("BB Length", bb_len)
            pc[1].metric("BB StdDev", bb_std)
            pc[2].metric("RSI Length", rsi_len)
            pc[3].metric("RSI Buy ≤", rsi_thr)
        else:
            pc = st.columns(4)
            bb_len  = pc[0].number_input("BB Length",          value=int(config.get("bb_length",     20)))
            bb_std  = pc[1].number_input("BB StdDev",          value=float(config.get("bb_std",       2.0)), step=0.1)
            rsi_len = pc[2].number_input("RSI Length",         value=int(config.get("rsi_length",    14)))
            rsi_thr = pc[3].number_input("RSI Buy Threshold",  value=int(config.get("rsi_threshold", 30)))

        st.markdown("---")
        st.subheader("Signal Scoring Weights")
        w1, w2 = st.columns(2)
        rsi_w = w1.slider("RSI Weight", 0.0, 1.0, float(config.get("rsi_score_weight", 0.6)), step=0.05)
        bb_w  = w2.slider("BB Weight",  0.0, 1.0, float(config.get("bb_score_weight",  0.4)), step=0.05)
        if abs((rsi_w + bb_w) - 1.0) > 0.01:
            st.warning(f"Weights sum to {rsi_w + bb_w:.2f} — should be 1.0.")

        st.subheader("Trailing Stop Tiers")
        t1, t2 = st.columns(2)
        tier1 = t1.number_input("Tier 1 ATR (break-even)", value=float(config.get("trailing_sl_tier1_atr", 1.5)), step=0.1)
        tier2 = t2.number_input("Tier 2 ATR (lock profit)", value=float(config.get("trailing_sl_tier2_atr", 3.0)), step=0.1)

        st.markdown("---")
        st.subheader("🧭 Regime Switching & TP Optimisation")
        new_regime_enabled = st.toggle(
            "Smart Regime Filter (block buys when stock is in a downtrend)",
            value=bool(config.get("smart_regime_enabled", False))
        )
        tp_modes = ["Dynamic (Auto-Switch)", "Fixed: Mean (Middle BB)", "Fixed: Upper Band"]
        cur_tp_mode = config.get("tp_target_mode", "Fixed: Mean (Middle BB)")
        new_tp_mode = st.selectbox(
            "Take Profit Target",
            tp_modes,
            index=tp_modes.index(cur_tp_mode) if cur_tp_mode in tp_modes else 1
        )
        st.caption("Dynamic — targets Upper Band in bullish regime, Middle Band in bearish. Upper Band — always targets max profit. Mean — safe/conservative.")

        if st.button("💾 Save Strategy", use_container_width=True):
            config.update({
                "preset_mode":           preset_mode,
                "bb_length":             int(bb_len),
                "bb_std":                float(bb_std),
                "rsi_length":            int(rsi_len),
                "rsi_threshold":         int(rsi_thr),
                "rsi_score_weight":      rsi_w,
                "bb_score_weight":       bb_w,
                "trailing_sl_tier1_atr": tier1,
                "trailing_sl_tier2_atr": tier2,
                "smart_regime_enabled":  new_regime_enabled,
                "tp_target_mode":        new_tp_mode,
            })
            save_config(config)
            st.success("✅ Strategy saved.")

    # ── Diagnostics & Logs ────────────────────────────────────────────────
    with tab_diag:
        st.subheader("📥 Download Bot Logs")
        st.caption("Filter the log by date/time range and download for debugging.")

        today = datetime.date.today()
        d1, d2 = st.columns(2)
        start_date = d1.date_input("Start Date", value=today)
        end_date   = d2.date_input("End Date",   value=today)
        t1c, t2c = st.columns(2)
        start_time = t1c.time_input("Start Time", value=datetime.time(0, 0))
        end_time   = t2c.time_input("End Time",   value=datetime.time(23, 59, 59))

        if st.button("🔍 Filter & Prepare Download", use_container_width=True):
            log_path = Path(LOG_FILE)
            if not log_path.exists():
                st.error(f"Log file not found at `{LOG_FILE}`.")
            else:
                start_dt = datetime.datetime.combine(start_date, start_time)
                end_dt   = datetime.datetime.combine(end_date,   end_time)
                matched  = []
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                            if start_dt <= ts <= end_dt:
                                matched.append(line)
                        except ValueError:
                            if matched:
                                matched.append(line)

                if matched:
                    fname = (f"bot_logs_{start_date}_{start_time.strftime('%H%M')}"
                             f"_to_{end_date}_{end_time.strftime('%H%M')}.txt")
                    st.success(f"Found **{len(matched)}** matching lines.")
                    st.download_button("⬇️ Download", data="".join(matched),
                                       file_name=fname, mime="text/plain",
                                       use_container_width=True)
                else:
                    st.warning("No entries in the selected range.")

        st.markdown("---")
        log_path = Path(LOG_FILE)
        if log_path.exists():
            size_kb = log_path.stat().st_size / 1024
            mtime   = datetime.datetime.fromtimestamp(log_path.stat().st_mtime)
            st.info(f"📄 `{LOG_FILE}` — **{size_kb:.1f} KB** | last modified **{mtime:%Y-%m-%d %H:%M:%S}**")
            
            with st.expander("Live Bot Trading Feed (Most Recent)"):
                try:
                    with open(LOG_FILE, "r") as f:
                        lines = f.readlines()
                        # Show last 30 lines, reversed so newest is at the top
                        log_content = "".join(reversed(lines[-30:]))
                        st.text_area("Recent Logs", log_content, height=300, label_visibility="collapsed")
                        if st.button("Refresh Feed"):
                            st.rerun()
                except Exception as e:
                    st.error(f"Error reading log: {e}")
        else:
            st.info("Log file not found yet.")


# ──────────────────────────────────────────────────────────────────────────
# ⚙ Cog button — top-right corner
# ──────────────────────────────────────────────────────────────────────────

_, cog_col = st.columns([12, 1])
with cog_col:
    if st.button("⚙️", help="Open Settings", use_container_width=True):
        show_settings()

st.markdown("---")

# ──────────────────────────────────────────────────────────────────────────
# Main Dashboard
# ──────────────────────────────────────────────────────────────────────────

status_color = {"RUNNING": "🟢", "PAUSED": "🟡", "LOCKED": "🔴"}.get(
    config.get("bot_status", "LOCKED"), "⚪"
)
st.markdown(
    f"## T212 Algo Dashboard &nbsp;&nbsp; {status_color} `{config.get('bot_status','UNKNOWN')}` "
    f"&nbsp;·&nbsp; `{api_mode}` mode",
    unsafe_allow_html=True,
)

# ── Metrics ───────────────────────────────────────────────────────────────
m1, m2, m3, m4 = st.columns(4)
m1.metric("💰 Total Equity",   f"£{float(equity.get('total',    0)):.2f}")
m2.metric("💵 Free Cash",      f"£{float(equity.get('free',     0)):.2f}")
m3.metric("📈 Invested",       f"£{float(equity.get('invested', 0)):.2f}")
ppl = float(equity.get("ppl", 0))
m4.metric("🔄 Unrealised P/L", f"£{ppl:.2f}", delta=f"{ppl:.2f}")

st.markdown("---")

# ── Market Regime Indicator ───────────────────────────────────────────────
try:
    import yfinance as yf
    import pandas as _pd
    _spy = yf.download("SPY", period="90d", interval="1d", progress=False)
    if not _spy.empty:
        if isinstance(_spy.columns, _pd.MultiIndex):
            _spy.columns = _spy.columns.droplevel(1)
        _sma50 = float(_spy['Close'].rolling(50).mean().iloc[-1])
        _cur   = float(_spy['Close'].iloc[-1])
        _bullish = _cur > _sma50
        regime_label  = "🟢 BULLISH Market" if _bullish else "🔴 BEARISH Market"
        regime_detail = f"SPY @ {_cur:.2f} vs 50-SMA {_sma50:.2f}"
    else:
        regime_label  = "⚪ Regime Unknown"
        regime_detail = "SPY data unavailable"
except Exception:
    regime_label  = "⚪ Regime Unknown"
    regime_detail = "Could not fetch SPY data"

st.markdown(f"### {regime_label}")
st.caption(regime_detail)

st.markdown("---")

# ── Open positions ────────────────────────────────────────────────────────
st.markdown('<div class="section-title">Open Positions</div>', unsafe_allow_html=True)
if client:
    try:
        positions = client.get_open_positions()
        if positions and isinstance(positions, list):
            import pandas as pd
            df = pd.DataFrame(positions)
            priority = ["ticker", "quantity", "averagePrice", "currentPrice", "ppl", "fxPpl"]
            cols = [c for c in priority if c in df.columns] + \
                   [c for c in df.columns if c not in priority]
            st.dataframe(df[cols], use_container_width=True, height=250)
        else:
            st.info("No open positions found.")
    except Exception as e:
        st.warning(f"Could not load positions: {e}")
else:
    st.info("No API key set. Open ⚙️ Settings → API & Control to add your key.")

# ── Pending orders ────────────────────────────────────────────────────────
st.markdown('<div class="section-title">Pending Orders</div>', unsafe_allow_html=True)
if client:
    try:
        orders = client.get_active_orders()
        if orders and isinstance(orders, list):
            import pandas as pd
            st.dataframe(pd.DataFrame(orders), use_container_width=True, height=200)
        else:
            st.info("No pending orders.")
    except Exception as e:
        st.warning(f"Could not load orders: {e}")
