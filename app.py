import streamlit as st
import json
import datetime
import requests
from pathlib import Path
from trading212_client import Trading212Client
from quant_inference import QuantInference

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

@st.cache_resource
def get_ai_engine(model_path):
    try:
        return QuantInference(model_path)
    except Exception:
        return None

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
    .pos-card{background:#1e1e2e;border-radius:14px;padding:18px 22px;margin-bottom:14px;border-left:4px solid #6c63ff;}
    .pos-ticker{font-size:1.2rem;font-weight:700;color:#e2e8f0;}
    .pos-sub{font-size:0.82rem;color:#a0aec0;margin-top:2px;}
    .pos-row{display:flex;gap:28px;margin-top:12px;flex-wrap:wrap;}
    .pos-item{display:flex;flex-direction:column;}
    .pos-label{font-size:0.72rem;color:#718096;text-transform:uppercase;letter-spacing:.06em;}
    .pos-value{font-size:1.05rem;font-weight:600;color:#e2e8f0;}
    .pnl-pos{color:#68d391!important;}
    .pnl-neg{color:#fc8181!important;}
    .pnl-banner{background:#1a202c;border-radius:12px;padding:16px 22px;margin-bottom:16px;border:1px solid #2d3748;}
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
    tab_api, tab_strategy, tab_risk, tab_ai, tab_watchlist, tab_discovery, tab_diag, tab_history = st.tabs([
        "📡 System & API",
        "📈 Core Strategy",
        "🛡️ Risk & Exits",
        "🤖 AI Brain",
        "📋 Watchlist",
        "🌎 Discovery",
        "🛠️ Diagnostics",
        "📊 Performance"
    ])

    # ── 1. System & API ──────────────────────────────────────────────────
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

        st.subheader("Core Loop Mechanics")
        new_cycle  = st.number_input("Cycle Interval (secs)", step=60,
                                     value=int(config.get("cycle_interval_secs", 900)))
        new_mkt    = st.toggle("Market Hours Guard (US session only)",
                               value=bool(config.get("market_hours_check", True)))

        if st.button("💾 Save System & API", use_container_width=True):
            config.update({
                "api_key":               new_key,
                "api_secret":            new_secret,
                "api_mode":              new_mode,
                "bot_status":            new_status,
                "cycle_interval_secs":   new_cycle,
                "market_hours_check":    new_mkt,
            })
            save_config(config)
            st.success("✅ Saved — bot picks up changes on the next cycle.")

    # ── 2. Core Strategy ─────────────────────────────────────────────────
    with tab_strategy:
        st.subheader("Preset Profiles")
        PRESET_PROFILES = {
            "Ultra Conservative": {
                "bb_length": 20, "bb_std": 3.0, "rsi_length": 14, "rsi_threshold": 20,
                "rsi_score_weight": 0.70, "bb_score_weight": 0.30,
                "smart_regime_enabled": True, "regime_ticker": "SPY",
                "risk_per_trade_pct": 0.005, "sl_atr_multiplier": 2.0, "max_open_positions": 3,
                "capital_utilization_pct": 0.70, "per_ticker_cooldown_mins": 60,
                "trailing_sl_tier1_atr": 2.0, "trailing_sl_tier2_atr": 4.0, "tp_target_mode": "Fixed: Mean (Middle BB)"
            },
            "Conservative": {
                "bb_length": 20, "bb_std": 2.5, "rsi_length": 14, "rsi_threshold": 25,
                "rsi_score_weight": 0.65, "bb_score_weight": 0.35,
                "smart_regime_enabled": True, "regime_ticker": "SPY",
                "risk_per_trade_pct": 0.01, "sl_atr_multiplier": 1.75, "max_open_positions": 5,
                "capital_utilization_pct": 0.80, "per_ticker_cooldown_mins": 45,
                "trailing_sl_tier1_atr": 1.75, "trailing_sl_tier2_atr": 3.5, "tp_target_mode": "Fixed: Mean (Middle BB)"
            },
            "Moderate": {
                "bb_length": 20, "bb_std": 2.0, "rsi_length": 14, "rsi_threshold": 30,
                "rsi_score_weight": 0.60, "bb_score_weight": 0.40,
                "smart_regime_enabled": True, "regime_ticker": "SPY",
                "risk_per_trade_pct": 0.015, "sl_atr_multiplier": 1.5, "max_open_positions": 7,
                "capital_utilization_pct": 0.85, "per_ticker_cooldown_mins": 30,
                "trailing_sl_tier1_atr": 1.5, "trailing_sl_tier2_atr": 3.0, "tp_target_mode": "Dynamic (Auto-Switch)"
            },
            "Aggressive": {
                "bb_length": 20, "bb_std": 1.5, "rsi_length": 14, "rsi_threshold": 40,
                "rsi_score_weight": 0.50, "bb_score_weight": 0.50,
                "smart_regime_enabled": False, "regime_ticker": "SPY",
                "risk_per_trade_pct": 0.02, "sl_atr_multiplier": 1.25, "max_open_positions": 10,
                "capital_utilization_pct": 0.90, "per_ticker_cooldown_mins": 20,
                "trailing_sl_tier1_atr": 1.25, "trailing_sl_tier2_atr": 2.5, "tp_target_mode": "Dynamic (Auto-Switch)"
            },
            "Ultra Aggressive": {
                "bb_length": 10, "bb_std": 1.0, "rsi_length": 7, "rsi_threshold": 50,
                "rsi_score_weight": 0.40, "bb_score_weight": 0.60,
                "smart_regime_enabled": False, "regime_ticker": "SPY",
                "risk_per_trade_pct": 0.03, "sl_atr_multiplier": 1.0, "max_open_positions": 15,
                "capital_utilization_pct": 0.95, "per_ticker_cooldown_mins": 10,
                "trailing_sl_tier1_atr": 1.0, "trailing_sl_tier2_atr": 2.0, "tp_target_mode": "Fixed: Upper Band"
            },
        }

        preset_options = list(PRESET_PROFILES.keys()) + ["Manual Custom"]
        cur_preset  = config.get("preset_mode", "Conservative")
        preset_idx  = preset_options.index(cur_preset) if cur_preset in preset_options else len(preset_options) - 1
        preset_mode = st.selectbox("Preset (Overrides specific Risk limits)", preset_options, index=preset_idx)

        # Baseline strategy logic values:
        bb_len  = int(config.get("bb_length", 20))
        bb_std  = float(config.get("bb_std", 2.0))
        rsi_len = int(config.get("rsi_length", 14))
        rsi_thr = int(config.get("rsi_threshold", 30))
        rsi_w   = float(config.get("rsi_score_weight", 0.6))
        bb_w    = float(config.get("bb_score_weight", 0.4))
        new_regime_enabled = bool(config.get("smart_regime_enabled", False))
        new_regime_ticker = config.get("regime_ticker", "SPY")

        if preset_mode in PRESET_PROFILES:
            p = PRESET_PROFILES[preset_mode]
            bb_len  = p["bb_length"];   bb_std  = p["bb_std"]
            rsi_len = p["rsi_length"];  rsi_thr = p["rsi_threshold"]
            rsi_w   = p["rsi_score_weight"]; bb_w = p["bb_score_weight"]
            new_regime_enabled = p["smart_regime_enabled"]
            new_regime_ticker = p["regime_ticker"]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("BB Length", bb_len)
            c2.metric("BB StdDev", bb_std)
            c3.metric("RSI Length", rsi_len)
            c4.metric("RSI Buy ≤", rsi_thr)
            st.info(f"🧭 Smart Regime Filter {'ON' if new_regime_enabled else 'OFF'} (Ticker: {new_regime_ticker})")
        else:
            pc = st.columns(4)
            bb_len  = pc[0].number_input("BB Length", value=bb_len)
            bb_std  = pc[1].number_input("BB StdDev", value=bb_std, step=0.1)
            rsi_len = pc[2].number_input("RSI Length", value=rsi_len)
            rsi_thr = pc[3].number_input("RSI Buy Threshold", value=rsi_thr)

            st.markdown("---")
            st.subheader("Signal Scoring Weights")
            w1, w2 = st.columns(2)
            rsi_w = w1.slider("RSI Weight", 0.0, 1.0, rsi_w, step=0.05)
            bb_w  = w2.slider("BB Weight",  0.0, 1.0, bb_w, step=0.05)

            st.markdown("---")
            st.subheader("Market Filters")
            new_regime_enabled = st.toggle("Smart Regime Filter", value=new_regime_enabled)
            new_regime_ticker = st.text_input("Regime Filter Ticker", value=new_regime_ticker)

        if st.button("💾 Save Strategy", use_container_width=True):
            update = {"preset_mode": preset_mode}
            if preset_mode in PRESET_PROFILES:
                update.update(PRESET_PROFILES[preset_mode])
            else:
                update.update({
                    "bb_length": int(bb_len), "bb_std": float(bb_std),
                    "rsi_length": int(rsi_len), "rsi_threshold": int(rsi_thr),
                    "rsi_score_weight": rsi_w, "bb_score_weight": bb_w,
                    "smart_regime_enabled": new_regime_enabled,
                    "regime_ticker": new_regime_ticker.strip() or "SPY"
                })
            config.update(update)
            save_config(config)
            st.success("✅ Strategy Saved.")

    # ── 3. Risk & Exits ──────────────────────────────────────────────────
    with tab_risk:
        st.subheader("Risk & Configuration")
        if cur_preset in PRESET_PROFILES:
            st.info(f"Using {cur_preset} profile overrides. Change to 'Manual Custom' in Strategy to edit these manually.")
            p = PRESET_PROFILES[cur_preset]
            risk_pct  = p["risk_per_trade_pct"] * 100
            sl_atr    = p["sl_atr_multiplier"]
            max_pos   = p["max_open_positions"]
            cap_use   = p["capital_utilization_pct"] * 100
            tier1     = p["trailing_sl_tier1_atr"]
            tier2     = p["trailing_sl_tier2_atr"]
            tp_mode   = p["tp_target_mode"]
            cool_mins = p["per_ticker_cooldown_mins"]
            
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Risk / Trade", f"{risk_pct:.1f}%")
            c2.metric("SL ATR Multiplier", sl_atr)
            c3.metric("Max Positions", max_pos)
            c4.metric("Capital Use", f"{cap_use:.0f}%")
            
            t1, t2, t3, t4 = st.columns(4)
            t1.metric("Trail Tier 1", tier1)
            t2.metric("Trail Tier 2", tier2)
            t3.metric("Cooldown (mins)", cool_mins)
            st.info(f"🎯 Default TP Mode: {tp_mode}")
        else:
            r1, r2, r3 = st.columns(3)
            risk_pct = r1.number_input("Risk/Trade (%)", min_value=0.1, max_value=10.0, step=0.1,
                                       value=round(float(config.get("risk_per_trade_pct", 0.01)) * 100, 2))
            sl_atr   = r2.number_input("SL ATR Mult.", min_value=0.5, max_value=5.0, step=0.1,
                                       value=float(config.get("sl_atr_multiplier", 1.5)))
            max_pos  = r3.number_input("Max Positions", min_value=1, max_value=50,
                                       value=int(config.get("max_open_positions", 5)))

            cap_use  = st.slider("Capital Utilisation (%)", 10, 100,
                                 int(float(config.get("capital_utilization_pct", 0.95)) * 100))
            cool_mins = st.number_input("Cooldown per ticker (mins)", value=int(config.get("per_ticker_cooldown_mins", 30)))

            st.markdown("---")
            st.subheader("Exit Rules")
            t1c, t2c = st.columns(2)
            tier1 = t1c.number_input("Trail Tier 1 (break-even ATR)", value=float(config.get("trailing_sl_tier1_atr", 1.5)), step=0.1)
            tier2 = t2c.number_input("Trail Tier 2 (lock profit ATR)", value=float(config.get("trailing_sl_tier2_atr", 3.0)), step=0.1)

            tp_modes = ["Dynamic (Auto-Switch)", "Fixed: Mean (Middle BB)", "Fixed: Upper Band"]
            tp_mode = st.selectbox("Take Profit Target", tp_modes,
                                   index=tp_modes.index(config.get("tp_target_mode", "Fixed: Mean (Middle BB)")) if config.get("tp_target_mode", "Fixed: Mean (Middle BB)") in tp_modes else 1)
            tp_atr_mult = st.number_input("Take Profit (Target ATR Fallback)", value=float(config.get("tp_atr_multiplier", 2.0)), step=0.1)
            
            new_heartbeat = st.slider("Heartbeat Interval (secs)", 30, 300, 
                                     value=int(config.get("heartbeat_interval_secs", 60)),
                                     help="How often the bot checks TP/SL. Increase to 60-120s if getting 429 Rate Limit errors.")

            if st.button("💾 Save Risk Settings", use_container_width=True):
                config.update({
                    "risk_per_trade_pct": risk_pct / 100.0,
                    "sl_atr_multiplier":  sl_atr,
                    "max_open_positions": max_pos,
                    "capital_utilization_pct": cap_use / 100.0,
                    "per_ticker_cooldown_mins": cool_mins,
                    "trailing_sl_tier1_atr": tier1,
                    "trailing_sl_tier2_atr": tier2,
                    "tp_target_mode": tp_mode,
                    "tp_atr_multiplier": tp_atr_mult,
                    "heartbeat_interval_secs": new_heartbeat,
                })
                save_config(config)
                st.success("✅ Risk Saved.")

    # ── 4. AI Brain ──────────────────────────────────────────────────────
    with tab_ai:
        st.subheader("🤖 AI Inference Engine")
        new_quant_sizing = st.toggle("Enable Kelly Criterion Sizing (Tri-Node Architecture)",
                                     value=bool(config.get("quant_sizing_enabled", False)))
        st.caption("When enabled, bypasses static Risk/Trade % and dynamically sizes bets using Kelly Criterion.")
        
        kf_idx = 1
        kelly_opts = {
            "1.0 (Full Kelly - Max Risk)": 1.0, 
            "0.5 (Half Kelly - Moderate)": 0.5, 
            "0.25 (Quarter Kelly - Standard)": 0.25, 
            "0.125 (Eighth Kelly - Safe)": 0.125
        }
        labels = list(kelly_opts.keys())
        current_k = float(config.get("kelly_fraction", 0.5))
        for i, v in enumerate(kelly_opts.values()):
            if abs(v - current_k) < 0.01: kf_idx = i

        sel_kf = st.selectbox("Kelly Fraction Calibration", labels, index=kf_idx)
        new_kelly_frac = kelly_opts[sel_kf]

        st.markdown("---")
        st.subheader("Brain File path")
        new_quant_path = st.text_input("AI Model Local Path", value=config.get("ml_model_path", "trained_models/ai_brain_v1.pkl"))
        
        uploaded_file = st.file_uploader("Upload .pkl brain", type=["pkl"])
        if uploaded_file:
            save_path = Path(new_quant_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            st.success(f"✅ Brain saved to `{new_quant_path}`")
            st.balloons()

        if st.button("💾 Save AI Settings", use_container_width=True):
            config.update({
                "quant_sizing_enabled": new_quant_sizing,
                "kelly_fraction": new_kelly_frac,
                "ml_model_path": new_quant_path
            })
            save_config(config)
            st.success("✅ AI Settings Saved.")

    # ── 5. Watchlist ─────────────────────────────────────────────────────
    with tab_watchlist:
        tickers = list(st.session_state.tickers)
        st.subheader(f"Watchlist ({len(tickers)} tickers)")

        if tickers:
            cols = st.columns(5)
            for i, t in enumerate(tickers):
                if cols[i % 5].button(f"🗑️ {t}", key=f"del_{t}", use_container_width=True):
                    st.session_state.tickers.remove(t)
                    st.rerun()
        else:
            st.info("No tickers yet. Add some below.")

        st.markdown("---")
        st.subheader("Add Ticker")
        c1, c2 = st.columns([3, 1])
        manual_in = c1.text_input("Ticker symbol", label_visibility="collapsed", placeholder="e.g. NVDA_US_EQ")
        if c2.button("➕ Add", use_container_width=True) and manual_in.strip():
            sym = manual_in.strip().upper()
            if sym not in st.session_state.tickers:
                st.session_state.tickers.append(sym)
                st.rerun()
            else:
                st.warning(f"{sym} already in watchlist.")

        st.markdown("---")
        st.subheader("🌡️ Ticker Health")
        state_path = Path("bot_state.json")
        state_data = {}
        if state_path.exists():
            try:
                with open(state_path, "r") as f: state_data = json.load(f)
            except Exception: pass
        
        health_data = state_data.get("ticker_health", {})
        unhealthy_tickers = {k: v for k, v in health_data.items() if v.get("error_count", 0) > 0}
        
        if unhealthy_tickers:
            for ticker, info in unhealthy_tickers.items():
                is_paused = info.get("is_paused", False)
                with st.container(border=True):
                    hc1, hc2 = st.columns([3, 1])
                    status_lbl = "🚨 PAUSED" if is_paused else f"⚠️ AT RISK ({info.get('error_count')}/3)"
                    hc1.markdown(f"**{status_lbl} - {ticker}**")
                    hc1.caption(f"Last error: `{info.get('last_error', 'N/A')}`")
                    if is_paused:
                        if hc2.button("♻️ Resume", key=f"resume_{ticker}", use_container_width=True):
                            info["is_paused"] = False
                            info["error_count"] = 0
                            with open(state_path, "w") as f: json.dump(state_data, f, indent=4)
                            st.rerun()
                    else:
                        if hc2.button("🧹 Clear", key=f"clear_{ticker}", use_container_width=True):
                            info["error_count"] = 0
                            with open(state_path, "w") as f: json.dump(state_data, f, indent=4)
                            st.rerun()
        else:
            st.success("✅ All active tickers are healthy.")

        st.markdown("---")
        st.subheader("Import / Export")
        left, right = st.columns(2)
        uploaded = left.file_uploader("Import JSON", type=["json"])
        if uploaded:
            try:
                data = json.load(uploaded)
                import_list = data if isinstance(data, list) else data.get("combined_list", data.get("tickers", []))
                merged = list(dict.fromkeys(st.session_state.tickers + import_list))
                st.session_state.tickers = merged
                left.success("Imported new tickers.")
            except Exception:
                left.error("Invalid JSON.")

        if tickers:
            right.download_button("📥 Export Watchlist", data=json.dumps(tickers, indent=4),
                                  file_name="trdbt_tickers.json", mime="application/json", use_container_width=True)

        if st.button("💾 Save Watchlist", use_container_width=True):
            config["tickers"] = st.session_state.tickers
            save_config(config)
            st.success("✅ Watchlist saved.")

    # ── 6. Discovery ─────────────────────────────────────────────────────
    with tab_discovery:
        st.subheader("Market Discovery")
        st.caption("Fetch trending stocks from Yahoo Finance.")
        
        d_cols = st.columns(3)
        list_type = d_cols[0].selectbox("Category", ["Day Losers", "Day Gainers", "Most Active"])
        count = d_cols[1].number_input("Count", min_value=5, max_value=25, value=10)
        scr_id = {"Day Losers": "day_losers", "Day Gainers": "day_gainers", "Most Active": "most_active"}.get(list_type)

        if d_cols[2].button("🔍 Refresh Discovery", use_container_width=True):
            try:
                url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds={scr_id}&count={count}"
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                if resp.status_code == 200:
                    st.session_state.discovery_data = resp.json().get('finance', {}).get('result', [{}])[0].get('quotes', [])
                else:
                    st.error("Failed to fetch data.")
            except Exception as e:
                st.error(f"Error: {e}")

        if st.session_state.get("discovery_data"):
            for q in st.session_state.discovery_data:
                symbol = q.get('symbol')
                name = q.get('displayName') or q.get('shortName') or ""
                change = q.get('regularMarketChangePercent', 0.0)
                
                c1, c2, c3 = st.columns([1, 2, 1])
                c1.code(symbol)
                c2.write(f"**{name}**")
                color = "green" if change >= 0 else "red"
                c3.markdown(f"<span style='color:{color}'>{change:+.2f}%</span>", unsafe_allow_html=True)
                
                t212_guess = f"{symbol.replace('.L', '')}_UK_EQ" if ".L" in symbol else f"{symbol}_US_EQ"
                if st.button(f"➕ Add {symbol}", key=f"discovery_{symbol}"):
                    if t212_guess not in st.session_state.tickers:
                        st.session_state.tickers.append(t212_guess)
                        st.success(f"Added {t212_guess}")

    # ── 7. Diagnostics ───────────────────────────────────────────────────
    with tab_diag:
        st.subheader("📥 Download Bot Logs")
        st.caption("Filter historical logs by date/time and choose your log source.")
        
        log_source = st.radio("Log Source", ["Bot Internal Files (logs/bot.log)", "System Service (journalctl -u tradingbot)"], horizontal=True)

        today = datetime.date.today()
        d1, d2 = st.columns(2)
        start_date = d1.date_input("Start Date", value=today - datetime.timedelta(days=2))
        end_date   = d2.date_input("End Date",   value=today)
        t1c, t2c = st.columns(2)
        start_time = t1c.time_input("Start Time", value=datetime.time(0, 0))
        end_time   = t2c.time_input("End Time",   value=datetime.time(23, 59, 59))

        if st.button("🔍 Filter & Prepare Download", use_container_width=True):
            start_dt = datetime.datetime.combine(start_date, start_time)
            end_dt   = datetime.datetime.combine(end_date,   end_time)
            fname = f"bot_logs_{start_date}_{start_time.strftime('%H%M')}_to_{end_date}_{end_time.strftime('%H%M')}.txt"
            matched_text = ""

            if "Internal" in log_source:
                log_dir = Path("logs").absolute()
                matched = []
                if log_dir.exists() and log_dir.is_dir():
                    files_to_scan = sorted(log_dir.glob("bot.log*"))
                    for lpath in files_to_scan:
                        try:
                            with lpath.open("r", encoding="utf-8", errors="replace") as fh:
                                for line in fh:
                                    try:
                                        ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                                        if start_dt <= ts <= end_dt:
                                            matched.append(line)
                                    except ValueError:
                                        if matched:
                                            matched.append(line)
                        except Exception as e:
                            st.warning(f"Could not read {lpath.name}: {e}")
                if matched:
                    matched_text = "".join(matched)
            else:
                import subprocess
                since_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                until_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
                try:
                    res = subprocess.run(
                        ["journalctl", "-u", "tradingbot", "--since", since_str, "--until", until_str, "--no-pager"],
                        capture_output=True, text=True, check=True
                    )
                    matched_text = res.stdout
                except Exception as e:
                    st.error(f"Failed to fetch system logs: {e}")

            if matched_text.strip():
                st.success(f"Log retrieval successful ({len(matched_text.splitlines())} lines).")
                st.download_button("⬇️ Download Logs", data=matched_text, file_name=fname, mime="text/plain", use_container_width=True)
            else:
                st.warning("No entries in the selected range.")

        st.markdown("---")
        
        d_col1, d_col2 = st.columns(2)
        with d_col1:
            log_path = Path("logs/bot.log").absolute()
            if log_path.exists():
                size_kb = log_path.stat().st_size / 1024
                mtime   = datetime.datetime.fromtimestamp(log_path.stat().st_mtime)
                st.info(f"📄 `bot.log` — **{size_kb:.1f} KB** | Mod: **{mtime:%Y-%m-%d %H:%M:%S}**")
                with st.expander("Live Bot Feed (Internal)"):
                    try:
                        with log_path.open("r", encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()
                            log_content = "".join(reversed(lines[-30:]))
                            st.text_area("Recent Bot Logs", log_content, height=300, label_visibility="collapsed")
                    except Exception as e:
                        st.error(f"Error reading live feed: {e}")
            else:
                st.info("Log file not found yet.")

        with d_col2:
            st.info("🖥️ `tradingbot` Service Journal")
            with st.expander("Live Systemd Feed"):
                try:
                    import subprocess
                    res = subprocess.run(["journalctl", "-u", "tradingbot", "-n", "30", "--no-pager"],
                                         capture_output=True, text=True)
                    sys_content = "".join(reversed(res.stdout.splitlines(True)))
                    st.text_area("Recent System Logs", sys_content, height=300, label_visibility="collapsed")
                except Exception as e:
                    st.error(f"Could not read journalctl: {e}")
        
        if st.button("Refresh Feeds", use_container_width=True): st.rerun()

        st.markdown("---")
        st.subheader("🚨 High-Priority Activity Alerts (Last 24h)")
        bot_log_path = Path("logs/bot.log")
        if bot_log_path.exists():
            try:
                with open(bot_log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-1000:]
                alerts = []
                now = datetime.datetime.now()
                for line in reversed(lines):
                    if " - ERROR - " in line or " - CRITICAL - " in line or " - WARNING - " in line:
                        try:
                            ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                            if (now - ts).total_seconds() < 86400:
                                alerts.append(line.strip())
                        except:
                            pass
                    if len(alerts) >= 10: # Show more in diagnostics
                        break
                if alerts:
                    for alert in alerts:
                        st.write(f"`{alert}`")
                else:
                    st.success("No critical alerts in the last 24 hours.")
            except Exception:
                st.error("Could not read logs for alerts.")

    # ── 8. Trade History & Performance ────────────────────────────────────
    with tab_history:
        st.subheader("Trade History & Performance")
        st.caption("Detailed view of all closed positions.")
        
        hist_file = Path("trade_history.json")
        if hist_file.exists():
            try:
                with open(hist_file, "r") as f:
                    trades = json.load(f)
                
                if trades:
                    import pandas as pd
                    df = pd.DataFrame(trades)
                    
                    # Formatting
                    if 'closed_at' in df.columns:
                        df['closed_at'] = pd.to_datetime(df['closed_at'], errors='coerce', utc=True).dt.strftime('%Y-%m-%d %H:%M')
                    if 'opened_at' in df.columns:
                        df['opened_at'] = pd.to_datetime(df['opened_at'], errors='coerce', utc=True).dt.strftime('%Y-%m-%d %H:%M')
                    
                    # Ensure columns exist even if empty
                    for col in ['ticker', 'pnl', 'entry', 'exit', 'qty', 'reason', 'ai_win_prob', 'opened_at', 'closed_at']:
                        if col not in df.columns:
                            df[col] = None
                            
                    # Analytics
                    total_trades = len(df)
                    winning_trades = len(df[df['pnl'] > 0])
                    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
                    total_pnl = df['pnl'].sum()
                    
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Closed Trades", total_trades)
                    c2.metric("Win Rate", f"{win_rate:.1f}%")
                    c3.metric("Total Historical P&L", f"£{total_pnl:.2f}")
                    
                    st.markdown("---")
                    
                    # Display Table
                    st.dataframe(
                        df[['closed_at', 'ticker', 'pnl', 'reason', 'ai_win_prob', 'entry', 'exit', 'qty', 'opened_at']].sort_values(by='closed_at', ascending=False),
                        use_container_width=True,
                        column_config={
                            "closed_at": "Closed",
                            "ticker": "Ticker",
                            "pnl": st.column_config.NumberColumn("P&L (£)", format="£%.2f"),
                            "reason": "Exit Reason",
                            "ai_win_prob": st.column_config.NumberColumn("AI Rank", format="%.2f"),
                            "entry": st.column_config.NumberColumn("Entry", format="£%.2f"),
                            "exit": st.column_config.NumberColumn("Exit", format="£%.2f"),
                            "qty": "Qty",
                            "opened_at": "Opened"
                        },
                        hide_index=True
                    )
                    
                    # Exports
                    st.markdown("### Export Data")
                    e1, e2 = st.columns(2)
                    
                    csv = df.to_csv(index=False).encode('utf-8')
                    e1.download_button(
                        label="📥 Download CSV",
                        data=csv,
                        file_name="trade_history.csv",
                        mime="text/csv",
                        use_container_width=True
                    )
                    
                    e2.download_button(
                        label="📥 Download JSON",
                        data=json.dumps(trades, indent=4),
                        file_name="trade_history.json",
                        mime="application/json",
                        use_container_width=True
                    )
                    
                    st.markdown("---")
                    st.subheader("🗑️ Reset Data")
                    confirm = st.checkbox("I understand this will permanently delete all trade history.")
                    if st.button("Delete All Trade History", type="primary", disabled=not confirm, use_container_width=True):
                        try:
                            with open(hist_file, "w") as f:
                                json.dump([], f)
                            st.success("History cleared! Refresh the page to see changes.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to reset: {e}")
                    
                else:
                    st.info("No trade history available yet.")
            except Exception as e:
                st.error(f"Error loading trade history: {e}")
        else:
            st.info("No trade history file found. History will appear here once the bot closes a position.")

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

# AI Status determination
ai_ready = False
engine = get_ai_engine(config.get("ml_model_path", "trained_models/ai_brain_v1.pkl"))
if engine and engine.is_ai_active():
    ai_ready = True

kelly_enabled = config.get("quant_sizing_enabled", False)

if ai_ready and kelly_enabled:
    ai_status = "🤖 AI ACTIVE"
elif ai_ready:
    ai_status = "🤖 AI READY"
else:
    ai_status = "🔴 AI OFF"

st.markdown(
    f"## T212 Algo Dashboard &nbsp;&nbsp; {status_color} `{config.get('bot_status','UNKNOWN')}` "
    f"&nbsp;·&nbsp; `{api_mode}` mode &nbsp;·&nbsp; {ai_status}",
    unsafe_allow_html=True,
)

# ── Load bot state for SL / virtual TP data ──────────────────────────────
def load_bot_state():
    try:
        with open("bot_state.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}

bot_state        = load_bot_state()
open_trades_state = bot_state.get("open_trades", {})
realised_pnl_st  = bot_state.get("realised_pnl", [])

# ── Open Positions — Card View ────────────────────────────────────────────
st.markdown('<div class="section-title">Open Positions</div>', unsafe_allow_html=True)
if client:
    try:
        positions = client.get_open_positions()
        if positions and isinstance(positions, list):
            for p in positions:
                t212    = p.get("ticker", "")
                short   = t212.replace("_US_EQ", "").replace("_US_ETF", "")
                trade   = open_trades_state.get(short, open_trades_state.get(t212, {}))
                qty     = p.get("quantity", 0)
                entry   = float(p.get("averagePrice", 0))
                current = float(p.get("currentPrice", 0))
                ppl_val = float(p.get("ppl", 0))
                sl      = float(trade["sl_price"]) if trade.get("sl_price") else None
                tp      = float(trade["tp_price"]) if trade.get("tp_price") else None
                pcls    = "pnl-pos" if ppl_val >= 0 else "pnl-neg"
                sign    = "+" if ppl_val >= 0 else ""
                sl_html = f'<span class="pos-value pnl-neg">£{sl:.4f}</span>' if sl else '<span class="pos-value">—</span>'
                tp_html = f'<span class="pos-value pnl-pos">£{tp:.4f}</span>' if tp else '<span class="pos-value">—</span>'
                st.markdown(f"""<div class="pos-card">
                    <div class="pos-ticker">{t212}</div>
                    <div class="pos-sub">Qty: {qty}</div>
                    <div class="pos-row">
                        <div class="pos-item"><span class="pos-label">Entry</span><span class="pos-value">£{entry:.4f}</span></div>
                        <div class="pos-item"><span class="pos-label">Current</span><span class="pos-value">£{current:.4f}</span></div>
                        <div class="pos-item"><span class="pos-label">P / L</span><span class="pos-value {pcls}">{sign}£{ppl_val:.2f}</span></div>
                        <div class="pos-item"><span class="pos-label">🔴 Stop Loss</span>{sl_html}</div>
                        <div class="pos-item"><span class="pos-label">🎯 Take Profit</span>{tp_html}</div>
                    </div></div>""", unsafe_allow_html=True)
        else:
            st.info("No open positions found.")
    except Exception as e:
        st.error(f"Error fetching positions: {e}")

st.markdown("---")

# ── Metrics ───────────────────────────────────────────────────────────────
# Calculate Total Realised P&L first
total_realised = sum(float(r.get("pnl", 0)) for r in realised_pnl_st)

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("💰 Total Equity",   f"£{float(equity.get('total',    0)):.2f}")
m2.metric("💵 Free Cash",      f"£{float(equity.get('free',     0)):.2f}")
m3.metric("📈 Invested",       f"£{float(equity.get('invested', 0)):.2f}")
ppl_total = float(equity.get("ppl", 0))
m4.metric("🔄 Unrealised P/L", f"£{ppl_total:.2f}", delta=f"{ppl_total:.2f}")
m5.metric("✅ Realised P/L",   f"£{total_realised:.2f}")

st.markdown("---")

# ── Market Regime Indicator ───────────────────────────────────────────────
@st.cache_data(ttl=900)
def get_market_regime():
    try:
        import yfinance as yf
        import pandas as _pd
        import math as _math
        _spy = yf.download("SPY", period="90d", interval="1d", progress=False)
        if not _spy.empty:
            if isinstance(_spy.columns, _pd.MultiIndex):
                _spy.columns = _spy.columns.droplevel(1)
            _close = _spy['Close'] if 'Close' in _spy.columns else _spy.iloc[:, 0]
            _sma50 = float(_close.rolling(50).mean().iloc[-1])
            _cur   = float(_close.iloc[-1])
            if not _math.isnan(_cur) and not _math.isnan(_sma50):
                _bullish = _cur > _sma50
                regime_label  = "🟢 BULLISH Market" if _bullish else "🔴 BEARISH Market"
                regime_detail = f"SPY @ {_cur:.2f} vs 50-SMA {_sma50:.2f}"
                return regime_label, regime_detail
    except Exception:
        pass
    return "⚪ Regime Unknown", "Could not fetch SPY data"

regime_label, regime_detail = get_market_regime()
st.markdown(f"### {regime_label}")
st.caption(regime_detail)

st.markdown("---")

# ── Active Exchange Orders ────────────────────────────────────────────────
st.markdown('<div class="section-title">Active Exchange Orders</div>', unsafe_allow_html=True)
if client:
    try:
        orders = client.get_active_orders()
        if orders and isinstance(orders, list):
            import pandas as pd
            df_orders = pd.DataFrame(orders)
            rename_map = {"ticker": "Ticker", "quantity": "Qty", "stopPrice": "Stop Price",
                          "limitPrice": "Limit Price", "status": "Status", "type": "Type"}
            df_orders.rename(columns={k: v for k, v in rename_map.items() if k in df_orders.columns}, inplace=True)
            keep = [c for c in ["Ticker", "Type", "Qty", "Stop Price", "Limit Price", "Status"] if c in df_orders.columns]
            st.dataframe(df_orders[keep] if keep else df_orders, use_container_width=True, height=200, hide_index=True)
        else:
            st.info("No active orders on the exchange.")
    except Exception as e:
        st.warning(f"Could not load orders: {e}")


