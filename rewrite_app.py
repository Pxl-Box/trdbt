import sys, os

new_settings_code = """@st.dialog("⚙️ Settings", width="large")
def show_settings():
    tab_api, tab_strategy, tab_risk, tab_ai, tab_watchlist, tab_discovery, tab_diag = st.tabs([
        "📡 System & API",
        "📈 Core Strategy",
        "🛡️ Risk & Exits",
        "🤖 AI Brain",
        "📋 Watchlist",
        "🌎 Discovery",
        "🛠️ Diagnostics",
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
        paused_tickers = {k: v for k, v in health_data.items() if v.get("is_paused")}
        
        if paused_tickers:
            for ticker, info in paused_tickers.items():
                with st.container(border=True):
                    hc1, hc2 = st.columns([3, 1])
                    hc1.markdown(f"**{ticker}** (Errors: `{info.get('error_count')}`)")
                    hc1.caption(f"Last error: `{info.get('last_error')}`")
                    if hc2.button("♻️ Resume", key=f"resume_{ticker}", use_container_width=True):
                        info["is_paused"] = False
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
        st.caption("Filter historical logs by date/time (scans all bot.log archives).")

        today = datetime.date.today()
        d1, d2 = st.columns(2)
        start_date = d1.date_input("Start Date", value=today)
        end_date   = d2.date_input("End Date",   value=today)
        t1c, t2c = st.columns(2)
        start_time = t1c.time_input("Start Time", value=datetime.time(0, 0))
        end_time   = t2c.time_input("End Time",   value=datetime.time(23, 59, 59))

        if st.button("🔍 Filter & Prepare Download", use_container_width=True):
            log_dir = Path("logs").absolute()
            start_dt = datetime.datetime.combine(start_date, start_time)
            end_dt   = datetime.datetime.combine(end_date,   end_time)
            matched  = []
            
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
                fname = f"bot_logs_{start_date}_{start_time.strftime('%H%M')}_to_{end_date}_{end_time.strftime('%H%M')}.txt"
                st.success(f"Found **{len(matched)}** lines securely.")
                st.download_button("⬇️ Download Logs", data="".join(matched), file_name=fname, mime="text/plain", use_container_width=True)
            else:
                st.warning("No entries in the selected range.")

        st.markdown("---")
        log_path = Path("logs/bot.log").absolute()
        if log_path.exists():
            size_kb = log_path.stat().st_size / 1024
            mtime   = datetime.datetime.fromtimestamp(log_path.stat().st_mtime)
            st.info(f"📄 `bot.log` — **{size_kb:.1f} KB** | Mod: **{mtime:%Y-%m-%d %H:%M:%S}**")
            
            with st.expander("Live Bot Trading Feed"):
                try:
                    with log_path.open("r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                        log_content = "".join(reversed(lines[-30:]))
                        st.text_area("Recent Logs", log_content, height=300, label_visibility="collapsed")
                        if st.button("Refresh Feed"): st.rerun()
                except Exception as e:
                    st.error(f"Error reading live feed: {e}")
        else:
            st.info("Log file not found yet.")
"""

target_file = 'c:/Users/Conor/Documents/GitHub/trdbt/app.py'

with open(target_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if line.startswith('@st.dialog("⚙️ Settings", width="large")'):
        start_idx = i
    if line.startswith('# ──────────────────────────────────────────────────────────────────────────'):
        if start_idx != -1 and i > start_idx and i + 1 < len(lines) and '⚙ Cog button' in lines[i+1]:
            end_idx = i
            break

if start_idx != -1 and end_idx != -1:
    new_lines = lines[:start_idx] + [new_settings_code + "\n"] + lines[end_idx:]
    with open(target_file, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print("SUCCESS: Rewrote show_settings in app.py")
else:
    print(f"FAILED: start_idx={start_idx}, end_idx={end_idx}")
