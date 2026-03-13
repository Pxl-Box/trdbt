import streamlit as st
import json
import datetime
import os
from pathlib import Path
from trading212_client import Trading212Client

st.set_page_config(
    page_title="T212 Algo Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

CONFIG_FILE = "config.json"
LOG_FILE    = "logs/bot.log"

# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

config = load_config()

if "tickers" not in st.session_state:
    st.session_state.tickers = config.get("tickers", [])


# ────────────────────────────────────────────────────────────────────────────
# Sidebar navigation
# ────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.image(
        "https://upload.wikimedia.org/wikipedia/commons/thumb/1/16/Trading_212_logo.svg/320px-Trading_212_logo.svg.png",
        width=180,
    )
    st.markdown("## T212 Algo Bot")
    page = st.radio(
        "Navigate",
        ["📊 Dashboard", "⚙️ Settings"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    st.caption(f"Bot Status: `{config.get('bot_status', 'UNKNOWN')}`")
    st.caption(f"Mode: `{config.get('api_mode', 'Practice')}`")

# ────────────────────────────────────────────────────────────────────────────
# API client
# ────────────────────────────────────────────────────────────────────────────

api_key  = config.get("api_key", "")
api_mode = config.get("api_mode", "Practice")
client   = None
equity   = {"free": 0.0, "total": 0.0, "invested": 0.0, "ppl": 0.0}

if api_key:
    try:
        client = Trading212Client(api_key, None, api_mode)
        equity = client.get_account_cash() or equity
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════════════
# PAGE: DASHBOARD
# ════════════════════════════════════════════════════════════════════════════

if page == "📊 Dashboard":
    st.title("📊 Performance Dashboard")

    # ── Key metrics ─────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💰 Total Equity",  f"£{float(equity.get('total',     0)):.2f}")
    c2.metric("💵 Free Cash",     f"£{float(equity.get('free',      0)):.2f}")
    c3.metric("📈 Invested",      f"£{float(equity.get('invested',  0)):.2f}")
    ppl = float(equity.get('ppl', 0))
    c4.metric("🔄 Unrealised P/L", f"£{ppl:.2f}", delta=f"{ppl:.2f}")

    st.markdown("---")

    # ── Open positions ───────────────────────────────────────────────────────
    st.subheader("Open Positions")
    if client:
        try:
            positions = client.get_open_positions()
            if positions and isinstance(positions, list):
                import pandas as pd
                df = pd.DataFrame(positions)
                # Surface the most useful columns first
                priority = ["ticker", "quantity", "averagePrice",
                            "currentPrice", "ppl", "fxPpl"]
                cols = [c for c in priority if c in df.columns] + \
                       [c for c in df.columns if c not in priority]
                st.dataframe(df[cols], use_container_width=True)
            else:
                st.info("No open positions found.")
        except Exception as e:
            st.warning(f"Could not load positions: {e}")
    else:
        st.info("Add your API key in ⚙️ Settings → API to see live data.")

    # ── Active pending orders ────────────────────────────────────────────────
    st.subheader("Pending Orders")
    if client:
        try:
            orders = client.get_active_orders()
            if orders and isinstance(orders, list):
                import pandas as pd
                df_o = pd.DataFrame(orders)
                st.dataframe(df_o, use_container_width=True)
            else:
                st.info("No pending orders.")
        except Exception as e:
            st.warning(f"Could not load orders: {e}")

# ════════════════════════════════════════════════════════════════════════════
# PAGE: SETTINGS
# ════════════════════════════════════════════════════════════════════════════

else:
    st.title("⚙️ Settings")

    tab_api, tab_tickers, tab_strategy, tab_logs = st.tabs(
        ["🔑 API & Bot Control", "📋 Watchlist", "🧠 Strategy", "🛠 Diagnostics"]
    )

    # ── Tab 1: API & Bot Control ────────────────────────────────────────────
    with tab_api:
        st.subheader("API Credentials")
        new_key  = st.text_input("Trading 212 API Key", value=config.get("api_key", ""), type="password")
        new_mode = st.selectbox("Account Mode", ["Practice", "Live"],
                                index=0 if config.get("api_mode") == "Practice" else 1)

        st.subheader("Bot Control")
        status_opts = ["RUNNING", "PAUSED", "LOCKED"]
        cur_status  = config.get("bot_status", "LOCKED")
        new_status  = st.radio("Bot Status", status_opts,
                               index=status_opts.index(cur_status) if cur_status in status_opts else 2,
                               horizontal=True)

        if new_status == "LOCKED":
            st.error("⚠️ Bot is LOCKED (kill-switch triggered or manually set). "
                     "Review logs before setting back to RUNNING.")

        st.subheader("Risk Management")
        r1, r2, r3 = st.columns(3)
        new_risk_pct     = r1.number_input("Risk per Trade (%)", value=float(config.get("risk_per_trade_pct", 0.01)) * 100, min_value=0.1, max_value=10.0, step=0.1, format="%.1f") / 100
        new_sl_atr       = r2.number_input("SL ATR Multiplier",  value=float(config.get("sl_atr_multiplier", 1.5)),           min_value=0.5, max_value=5.0,  step=0.1, format="%.1f")
        new_max_pos      = r3.number_input("Max Open Positions",  value=int(config.get("max_open_positions", 5)),              min_value=1,   max_value=50,   step=1)
        new_cap_util     = st.slider("Capital Utilisation (%)", 10, 100, int(float(config.get("capital_utilization_pct", 0.95)) * 100))
        new_mkt_hrs      = st.toggle("Market Hours Guard (US only)", value=bool(config.get("market_hours_check", True)))
        new_regime       = st.text_input("Regime Filter Ticker (blank to disable)", value=config.get("regime_ticker", "SPY"))
        new_cycle        = st.number_input("Cycle Interval (seconds)", value=int(config.get("cycle_interval_secs", 900)), step=60)

        if st.button("💾 Save API & Control Settings", use_container_width=True):
            config.update({
                "api_key":               new_key,
                "api_mode":              new_mode,
                "bot_status":            new_status,
                "risk_per_trade_pct":    new_risk_pct,
                "sl_atr_multiplier":     new_sl_atr,
                "max_open_positions":    new_max_pos,
                "capital_utilization_pct": new_cap_util / 100,
                "market_hours_check":    new_mkt_hrs,
                "regime_ticker":         new_regime if new_regime.strip() else None,
                "cycle_interval_secs":   new_cycle,
            })
            save_config(config)
            st.success("✅ Settings saved. Bot will pick up changes on the next cycle.")

    # ── Tab 2: Watchlist ─────────────────────────────────────────────────────
    with tab_tickers:
        st.subheader("Current Watchlist")
        tickers = st.session_state.tickers

        if tickers:
            st.write(f"Tracking **{len(tickers)}** tickers.")

            # Display badge-style list
            cols = st.columns(6)
            for i, t in enumerate(tickers):
                cols[i % 6].code(t)

            st.markdown("---")
            remove_choice = st.selectbox("Remove a Ticker", [""] + tickers)
            if st.button("🗑 Remove Selected") and remove_choice:
                st.session_state.tickers.remove(remove_choice)
                st.rerun()
        else:
            st.info("No tickers configured yet. Add some below.")

        st.markdown("---")
        st.subheader("Add Tickers")

        # Manual add
        manual_in = st.text_input("Add Ticker Manually (e.g. NVDA_US_EQ)", "")
        if st.button("➕ Add Manual") and manual_in.strip():
            sym = manual_in.strip().upper()
            if sym not in st.session_state.tickers:
                st.session_state.tickers.append(sym)
                st.success(f"Added {sym}")
                st.rerun()
            else:
                st.warning(f"{sym} already in watchlist.")

        # Yahoo search
        with st.expander("🔍 Search Yahoo Finance"):
            search_q = st.text_input("Search (e.g. Apple, Gold, Bitcoin)", "")
            if search_q:
                import requests
                try:
                    url  = f"https://query2.finance.yahoo.com/v1/finance/search?q={search_q}"
                    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    quotes = resp.json().get("quotes", [])
                    found  = [f"{q['symbol']} — {q.get('shortname', '')}"
                              for q in quotes if "symbol" in q]
                    if found:
                        pick = st.selectbox("Results", found)
                        if st.button("➕ Add from Search"):
                            sym = pick.split(" ")[0].strip().upper()
                            if sym not in st.session_state.tickers:
                                st.session_state.tickers.append(sym)
                                st.rerun()
                            else:
                                st.warning(f"{sym} already tracked.")
                    else:
                        st.info("No results.")
                except Exception:
                    st.error("Search failed. Check your connection.")

        st.markdown("---")
        st.subheader("Import / Export")
        left, right = st.columns(2)

        # Import
        uploaded = left.file_uploader("Import JSON Ticker List", type=["json"])
        if uploaded:
            try:
                data = json.load(uploaded)
                import_list = data if isinstance(data, list) else data.get("combined_list", data.get("tickers", []))
                before = len(st.session_state.tickers)
                merged = list(dict.fromkeys(st.session_state.tickers + import_list))
                st.session_state.tickers = merged
                left.success(f"Imported {len(merged) - before} new tickers.")
            except Exception:
                left.error("Invalid JSON file.")

        # Export
        if tickers:
            right.download_button(
                "📥 Export Watchlist",
                data=json.dumps(tickers, indent=4),
                file_name="trdbt_tickers.json",
                mime="application/json",
                use_container_width=True,
            )

        # Save to config
        if st.button("💾 Save Watchlist to Config", use_container_width=True):
            config["tickers"] = st.session_state.tickers
            save_config(config)
            st.success("✅ Watchlist saved.")

    # ── Tab 3: Strategy ──────────────────────────────────────────────────────
    with tab_strategy:
        st.subheader("Strategy Preset")

        preset_options = [
            "Ultra Conservative",
            "Conservative",
            "Moderate",
            "Aggressive",
            "Ultra Aggressive",
            "Manual Custom",
        ]
        cur_preset  = config.get("preset_mode", "Conservative")
        preset_idx  = preset_options.index(cur_preset) if cur_preset in preset_options else len(preset_options) - 1
        preset_mode = st.selectbox("Preset", preset_options, index=preset_idx)

        preset_map = {
            "Ultra Conservative": (20, 3.0, 14, 20),
            "Conservative":       (20, 2.5, 14, 25),
            "Moderate":           (20, 2.0, 14, 30),
            "Aggressive":         (20, 1.5, 14, 40),
            "Ultra Aggressive":   (10, 1.0,  7, 50),
        }

        if preset_mode in preset_map:
            bb_len, bb_std, rsi_len, rsi_thr = preset_map[preset_mode]
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("BB Length",  bb_len)
            col2.metric("BB StdDev",  bb_std)
            col3.metric("RSI Length", rsi_len)
            col4.metric("RSI Buy ≤",  rsi_thr)
        else:
            c1, c2, c3, c4 = st.columns(4)
            bb_len  = c1.number_input("BB Length",      value=int(config.get("bb_length",     20)))
            bb_std  = c2.number_input("BB StdDev",      value=float(config.get("bb_std",       2.0)), step=0.1)
            rsi_len = c3.number_input("RSI Length",     value=int(config.get("rsi_length",    14)))
            rsi_thr = c4.number_input("RSI Buy Threshold", value=int(config.get("rsi_threshold", 30)))

        st.markdown("---")
        st.subheader("Signal Scoring Weights")
        w1, w2 = st.columns(2)
        rsi_w = w1.slider("RSI Weight",  0.0, 1.0, float(config.get("rsi_score_weight", 0.6)), step=0.05)
        bb_w  = w2.slider("BB Weight",   0.0, 1.0, float(config.get("bb_score_weight",  0.4)), step=0.05)
        if abs((rsi_w + bb_w) - 1.0) > 0.01:
            st.warning(f"Weights sum to {rsi_w + bb_w:.2f}. They should add up to 1.0.")

        st.subheader("Trailing Stop Tiers")
        t1, t2 = st.columns(2)
        tier1 = t1.number_input("Tier 1 ATR (Break-even trigger)",   value=float(config.get("trailing_sl_tier1_atr", 1.5)), step=0.1)
        tier2 = t2.number_input("Tier 2 ATR (Lock-profit trigger)",  value=float(config.get("trailing_sl_tier2_atr", 3.0)), step=0.1)

        if st.button("💾 Save Strategy Settings", use_container_width=True):
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
            })
            save_config(config)
            st.success("✅ Strategy settings saved.")

    # ── Tab 4: Diagnostics & Logs ───────────────────────────────────────────
    with tab_logs:
        st.subheader("📥 Download Bot Logs")
        st.markdown(
            "Select a date and time range to extract a filtered slice of the log file. "
            "The file is downloaded to your device — no live streaming required."
        )

        today = datetime.date.today()
        d1, d2 = st.columns(2)
        start_date = d1.date_input("Start Date", value=today)
        end_date   = d2.date_input("End Date",   value=today)

        t1c, t2c = st.columns(2)
        start_time = t1c.time_input("Start Time", value=datetime.time(0, 0))
        end_time   = t2c.time_input("End Time",   value=datetime.time(23, 59, 59))

        if st.button("🔍 Filter & Download Logs", use_container_width=True):
            log_path = Path(LOG_FILE)
            if not log_path.exists():
                st.error(f"Log file not found at `{LOG_FILE}`.")
            else:
                start_dt = datetime.datetime.combine(start_date, start_time)
                end_dt   = datetime.datetime.combine(end_date,   end_time)

                matched = []
                skipped = 0
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        # Log lines start: "YYYY-MM-DD HH:MM:SS,mmm - ..."
                        try:
                            ts_str = line[:23]                           # "2026-03-12 22:24:45,453"
                            ts_str = ts_str[:19]                         # drop the ,ms
                            ts     = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                            if start_dt <= ts <= end_dt:
                                matched.append(line)
                            else:
                                skipped += 1
                        except ValueError:
                            # Non-timestamped continuation lines — include if we're inside range
                            if matched:
                                matched.append(line)

                if matched:
                    content  = "".join(matched)
                    filename = (
                        f"bot_logs_{start_date}_{start_time.strftime('%H%M')}"
                        f"_to_{end_date}_{end_time.strftime('%H%M')}.txt"
                    )
                    st.success(f"Found **{len(matched)}** matching lines.")
                    st.download_button(
                        "⬇️ Download Filtered Logs",
                        data=content,
                        file_name=filename,
                        mime="text/plain",
                        use_container_width=True,
                    )
                else:
                    st.warning("No log entries found in the selected time range.")

        st.markdown("---")
        st.subheader("Log File Info")
        log_path = Path(LOG_FILE)
        if log_path.exists():
            size_kb = log_path.stat().st_size / 1024
            mtime   = datetime.datetime.fromtimestamp(log_path.stat().st_mtime)
            st.info(
                f"📄 `{LOG_FILE}` — "
                f"Size: **{size_kb:.1f} KB** | "
                f"Last modified: **{mtime.strftime('%Y-%m-%d %H:%M:%S')}**"
            )
        else:
            st.info("Log file not found. The bot may not have started yet.")
