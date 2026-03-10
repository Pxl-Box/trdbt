import streamlit as st
import json
import logging
from trading212_client import Trading212Client

st.set_page_config(page_title="T212 Algo Dashboard", layout="wide", initial_sidebar_state="expanded")

CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config_data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=4)

config = load_config()

# ---- SIDEBAR SETTINGS ----
st.sidebar.title("Bot Settings")

api_key = st.sidebar.text_input("Trading 212 API Key", value=config.get("api_key", ""), type="password")
api_secret = st.sidebar.text_input("Trading 212 Secret Key", value=config.get("api_secret", ""), type="password")
api_mode = st.sidebar.selectbox("Mode", ["Practice", "Live"], index=0 if config.get("api_mode") == "Practice" else 1)

bot_status = st.sidebar.radio("Bot Status", ["RUNNING", "PAUSED", "LOCKED"], 
                              index=["RUNNING", "PAUSED", "LOCKED"].index(config.get("bot_status", "LOCKED")))

tickers_str = st.sidebar.text_input("Tickers (comma separated)", value=",".join(config.get("tickers", [])))
tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]

preset_mode = st.sidebar.selectbox(
    "Strategy Preset", 
    ["Conservative", "Aggressive", "Manual Custom"], 
    index=["Conservative", "Aggressive", "Manual Custom"].index(config.get("preset_mode", "Conservative"))
)

# Preset definitions
if preset_mode == "Conservative":
    bb_length, bb_std, rsi_length, rsi_threshold = 20, 2.5, 14, 25
elif preset_mode == "Aggressive":
    bb_length, bb_std, rsi_length, rsi_threshold = 20, 2.0, 14, 40
else:
    bb_length = st.sidebar.number_input("BB Length", value=config.get("bb_length", 20))
    bb_std = st.sidebar.number_input("BB StdDev", value=config.get("bb_std", 2.0), step=0.1)
    rsi_length = st.sidebar.number_input("RSI Length", value=config.get("rsi_length", 14))
    rsi_threshold = st.sidebar.number_input("RSI Buy Threshold", value=config.get("rsi_threshold", 30))

if st.sidebar.button("Save Configuration"):
    config["api_key"] = api_key
    config["api_secret"] = api_secret
    config["api_mode"] = api_mode
    config["bot_status"] = bot_status
    config["tickers"] = tickers
    config["preset_mode"] = preset_mode
    config["bb_length"] = bb_length
    config["bb_std"] = bb_std
    config["rsi_length"] = rsi_length
    config["rsi_threshold"] = rsi_threshold
    save_config(config)
    st.sidebar.success("Settings saved! The bot will pick them up on the next cycle.")

if bot_status == "LOCKED":
    st.sidebar.error("KILL SWITCH WAS TRIGGERED. Please review logs and manually set status back to RUNNING after investigation.")

# ---- MAIN DASHBOARD ----
st.title("Trading 212 LXC Trading Dashboard")
st.markdown("Monitor performance, check open positions, and view logs.")

col1, col2, col3 = st.columns(3)

# Test Connection and Fetch Stats
client = None
equity_data = {"free": 0.0, "total": 0.0}
if api_key and api_secret:
    client = Trading212Client(api_key, api_secret, api_mode)
    try:
        equity_data = client.get_account_cash()
    except Exception as e:
        st.error(f"API Error: {e}")

col1.metric("Account Equity", f"£{equity_data.get('total', 0.0):.2f}")
col2.metric("Free Cash", f"£{equity_data.get('free', 0.0):.2f}")
col3.metric("Status", bot_status)

st.subheader("Open Positions")
if client:
    try:
        positions = client.get_open_positions()
        if positions and isinstance(positions, list):
            import pandas as pd
            df = pd.DataFrame(positions)
            st.dataframe(df, use_container_width=True)
        else:
            st.info("No open positions found.")
    except Exception as e:
        st.warning(f"Could not load positions: {e}")

st.subheader("Bot Logs")
try:
    with open("bot.log", "r") as f:
        # Read the last 20 lines
        lines = f.readlines()
        log_content = "".join(lines[-20:])
    st.text_area("Recent Logs", log_content, height=300)
    st.button("Refresh Logs")
except FileNotFoundError:
    st.info("bot.log not found yet.")
