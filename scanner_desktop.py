import sys
import os
import json
import traceback
import logging
import warnings
from pathlib import Path
from datetime import datetime
import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf

# 1. FORCE THE MOST STABLE OPENGL MODE FOR WINDOWS
os.environ["QT_QUICK_BACKEND"] = "software"
os.environ["QT_OPENGL"] = "software"
os.environ["PYQTGRAPH_QT_LIB"] = "PyQt6"

import pyqtgraph as pg
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLineEdit, QLabel, QGroupBox, QDoubleSpinBox, 
                             QFormLayout, QMessageBox, QProgressBar, QListWidget, QListWidgetItem)
from PyQt6.QtCore import Qt, QTimer

from strategy import MeanReversionStrategy
from quant_inference import QuantInference

pg.setConfigOptions(useOpenGL=False, antialias=False)
warnings.filterwarnings('ignore')

# ── HELPER FUNCTIONS ───────────────────────────────────────────────────────────
def to_t212_ticker(ticker: str) -> str:
    if "_" in ticker: return ticker
    _US_ETFS = {"GDX", "GLD", "SLV", "ARKK", "VUSA", "QQQ", "SPY", "XLE", "XLK", "ICLN", "IBIT", "FBTC", "VUAG"}
    t = ticker.upper()
    if t.endswith(".L"): return f"{t.replace('.L', '')}_UK_EQ"
    if t in _US_ETFS: return f"{t}_US_ETF"
    return f"{t}_US_EQ"

def clean_ticker(ticker: str) -> str:
    return ticker.split("_")[0]

def load_bot_config():
    if Path("config.json").exists():
        with open("config.json", "r") as f:
            return json.load(f)
    return {}

def run_monte_carlo(df, current_price, steps=192, sims=5000):
    rets = np.log(df['Close'] / df['Close'].shift(1)).dropna()
    mu = rets.mean(); sigma = rets.std()
    paths = np.zeros((steps, sims))
    paths[0] = current_price
    rand_shocks = np.random.normal(mu, sigma, (steps, sims))
    price_multipliers = np.exp(rand_shocks)
    for t in range(1, steps):
        paths[t] = paths[t-1] * price_multipliers[t]
    return paths

def evaluate_historic_outlier(df, rsi, bb_pct, atr, current_price):
    df = df.copy()
    if 'RSI' not in df.columns: return 0.0, 0
    df['RSI'] = df['RSI'].fillna(50.0)
    bb_col = next((c for c in df.columns if 'BBL' in c), None)
    if not bb_col: return 0.0, 0
    df['bb_pct_below_hist'] = ((df[bb_col] - df['Close']) / df[bb_col]) * 100
    df['bb_pct_below_hist'] = df['bb_pct_below_hist'].clip(lower=0)
    rsi_mask = (df['RSI'] > rsi - 5) & (df['RSI'] < rsi + 5)
    bb_mask = df['bb_pct_below_hist'] >= np.clip(bb_pct - 0.5, 0, None)
    matches = df[rsi_mask & bb_mask]
    if len(matches) == 0: return 0.0, 0
    wins = 0; total = 0
    for idx_label in matches.index:
        idx_pos = df.index.get_loc(idx_label)
        if idx_pos + 192 < len(df):
            future_price = df.iloc[idx_pos + 192]['Close']
            if future_price > df.iloc[idx_pos]['Close'] + (atr * 0.5): wins += 1
            total += 1
    return (wins / total * 100) if total > 0 else 0, total

# ── MAIN WINDOW (ADVANCED TICKER MANAGER) ─────────────────────────────────────
class NativeTerminal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Trading Terminal (Advanced Ticker Manager)")
        self.setGeometry(100, 100, 1300, 900)
        self.config = load_bot_config()
        self.strategy = MeanReversionStrategy()
        self.engine = QuantInference(model_path=self.config.get("ml_model_path", "trained_models/ai_brain_v1.pkl"))
        
        self.tickers = []
        self.load_ticker_database()
        
        self.current_analysis = None
        self._current_view = "Forecast"
        self._selected_ticker = "NVDA"
        
        self.setup_ui()
    
    def load_ticker_database(self):
        path = Path("trdbt_tickers.json")
        if path.exists():
            with open(path, "r") as f:
                data = json.load(f)
                self.tickers = sorted(list(set(data.get("combined_list", []))))
        else:
            self.tickers = ["NVDA_US_EQ", "AAPL_US_EQ", "TSLA_US_EQ"]

    def save_ticker_database(self):
        path = Path("trdbt_tickers.json")
        data = {"combined_list": self.tickers}
        if path.exists():
             with open(path, "r") as f:
                 old = json.load(f)
                 old["combined_list"] = self.tickers
                 data = old
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def setup_ui(self):
        main_widget = QWidget(); self.setCentralWidget(main_widget); main_layout = QHBoxLayout(main_widget)
        
        # Left Panel (Control & Tickers)
        left_panel = QWidget(); left_layout = QVBoxLayout(left_panel); left_panel.setMaximumWidth(380)
        left_layout.addWidget(QLabel("<h2 style='color:#00ffaa;'>🧠 Ticker Manager</h2>"))
        
        # 1. Search Bar
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search Tickers...")
        self.search_bar.setStyleSheet("padding: 8px; background: #222; color: #fff; font-size: 14px;")
        self.search_bar.textChanged.connect(self.filter_tickers)
        left_layout.addWidget(self.search_bar)
        
        # 2. Ticker List (Checkable)
        self.ticker_list = QListWidget()
        self.ticker_list.setStyleSheet("QListWidget::item { padding: 5px; color: #eee; } QListWidget::item:selected { background: #00ffaa; color: black; }")
        self.ticker_list.itemClicked.connect(self.on_ticker_clicked)
        self.populate_ticker_list()
        left_layout.addWidget(self.ticker_list)
        
        # 3. Batch Actions
        batch_layout = QHBoxLayout()
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.setStyleSheet("background: #ff4b4b; color: #fff; padding: 5px;")
        self.delete_btn.clicked.connect(self.delete_selected_tickers)
        batch_layout.addWidget(self.delete_btn); left_layout.addLayout(batch_layout)
        
        # 4. Add Ticker
        add_layout = QHBoxLayout()
        self.add_input = QLineEdit(); self.add_input.setPlaceholderText("AAPL")
        self.add_btn = QPushButton("Verify & Add")
        self.add_btn.setStyleSheet("background: #00ffaa; color: black; font-weight: bold;")
        self.add_btn.clicked.connect(self.verify_and_add_ticker)
        add_layout.addWidget(self.add_input); add_layout.addWidget(self.add_btn)
        left_layout.addLayout(add_layout)
        
        # 5. Run Button
        self.run_btn = QPushButton("RUN HEAVY ANALYSIS")
        self.run_btn.setStyleSheet("background-color: #00ffaa; color: black; font-weight: bold; font-size: 18px; padding: 15px; margin-top: 10px;")
        self.run_btn.clicked.connect(self.run_analysis)
        self.status_label = QLabel("Select a ticker to begin."); self.status_label.setWordWrap(True); self.status_label.setStyleSheet("color: #aaa; font-size: 14px;")
        
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.setVisible(False)
        left_layout.addWidget(self.run_btn); left_layout.addWidget(self.progress); left_layout.addWidget(self.status_label)
        main_layout.addWidget(left_panel)
        
        # Right Panel (Charting)
        right_panel = QWidget(); right_layout = QVBoxLayout(right_panel)
        btn_layout = QHBoxLayout()
        self.view_btns = {}
        for name in ["Forecast", "Trend", "MC Paths"]:
            btn = QPushButton(name)
            btn.clicked.connect(lambda checked, n=name: self.switch_view(n))
            btn_layout.addWidget(btn)
            self.view_btns[name] = btn
        
        self.plot_widget = pg.PlotWidget(title="Performance Forecast")
        self.plot_widget.setBackground('#1a1c1e')
        right_layout.addLayout(btn_layout); right_layout.addWidget(self.plot_widget)
        main_layout.addWidget(right_panel)
        
        self.switch_view("Forecast", redraw=False)

    def populate_ticker_list(self, filter_text=""):
        self.ticker_list.clear()
        for t in self.tickers:
            clean = clean_ticker(t)
            if filter_text.upper() in clean.upper():
                item = QListWidgetItem(clean)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, t) # Store the full T212 ticker
                self.ticker_list.addItem(item)

    def filter_tickers(self, text):
        self.populate_ticker_list(text)

    def on_ticker_clicked(self, item):
        self._selected_ticker = item.data(Qt.ItemDataRole.UserRole)
        self.status_label.setText(f"Ready to analyze: <b>{clean_ticker(self._selected_ticker)}</b>")

    def verify_and_add_ticker(self):
        symbol = self.add_input.text().upper().strip()
        if not symbol: return
        
        # Yahoo Verification
        self.add_btn.setEnabled(False); self.add_btn.setText("Verifying...")
        QApplication.processEvents()
        
        try:
            # SNDK Check
            if symbol == "SNDK":
                QMessageBox.warning(self, "Ticker Alert", "SNDK was acquired by Western Digital. Please use 'WDC' instead.")
                return
                
            test_data = yf.download(symbol, period="1d", progress=False, threads=False)
            if test_data.empty:
                raise ValueError(f"Ticker '{symbol}' not found on Yahoo Finance.")
            
            full_ticker = to_t212_ticker(symbol)
            if full_ticker not in self.tickers:
                self.tickers.append(full_ticker)
                self.tickers.sort()
                self.save_ticker_database()
                self.populate_ticker_list()
                self.add_input.clear()
                self.status_label.setText(f"Added: {symbol}")
            else:
                QMessageBox.information(self, "Info", f"{symbol} is already in your list.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
        finally:
            self.add_btn.setText("Verify & Add"); self.add_btn.setEnabled(True)

    def delete_selected_tickers(self):
        to_delete = []
        for i in range(self.ticker_list.count()):
            item = self.ticker_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                to_delete.append(item.data(Qt.ItemDataRole.UserRole))
        
        if not to_delete: return
        
        ans = QMessageBox.question(self, "Confirm", f"Delete {len(to_delete)} selected tickers?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.tickers = [t for t in self.tickers if t not in to_delete]
            self.save_ticker_database()
            self.populate_ticker_list()

    def switch_view(self, name, redraw=True):
        self._current_view = name
        for n, btn in self.view_btns.items():
            btn.setStyleSheet("background-color: #00ffaa; color: #000;" if n == name else "background-color: #333; color: #aaa;")
        if redraw: self.render_chart()

    def run_analysis(self):
        ticker = self._selected_ticker
        if not ticker: 
            QMessageBox.warning(self, "Select Ticker", "Please select a ticker from the list first.")
            return
        
        self.run_btn.setEnabled(False); self.progress.setVisible(True); self.status_label.setText(f"Fetching Data for {clean_ticker(ticker)}...")
        self.current_analysis = None; self.plot_widget.clear(); self.plot_widget.setTitle(f"Analyzing {clean_ticker(ticker)}...")
        self.plot_widget.enableAutoRange(); self.switch_view("Forecast", redraw=False)
        self.status_label.repaint(); self.plot_widget.repaint() 
        
        try:
            print(f"STEP 1: Fetching data for {ticker}...")
            benchmarks_15m = {}
            for bm in ["SPY", "QQQ", "IWM"]:
                benchmarks_15m[bm] = self.strategy.get_historical_data(bm, interval="15m", period="10d")
            
            df_ticker_15m = self.strategy.get_historical_data(ticker, interval="15m", period="15d")
            if df_ticker_15m.empty: raise ValueError("Insufficient ticker data.")

            print(f"STEP 2: AI Inference...")
            # We fetch 1d separately for ML
            df_ticker_1d = self.strategy.get_historical_data(ticker, interval="1d", period="4mo")
            bench_1d = {bm: self.strategy.get_historical_data(bm, interval="1d", period="4mo") for bm in ["SPY", "QQQ", "IWM"]}
            
            signal_data = self.strategy.analyze(ticker, quant_engine=self.engine, 
                                                benchmarks_15m=benchmarks_15m, benchmarks_1d=bench_1d)
            
            print(f"STEP 3: Indicators & Simulation...")
            close = df_ticker_15m['Close']; h = df_ticker_15m['High']; l = df_ticker_15m['Low']
            sma20 = close.rolling(20).mean(); std20 = close.rolling(20).std()
            bbl = sma20 - (2 * std20); bbu = sma20 + (2 * std20)
            delta = close.diff(); g = delta.where(delta > 0, 0).rolling(14).mean(); ls = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi_s = 100 - (100 / (1 + (g/(ls+1e-9))))
            atr_s = pd.concat([(h-l), (h-close.shift(1)).abs(), (l-close.shift(1)).abs()], axis=1).max(axis=1).rolling(14).mean()
            
            cp = float(close.iloc[-1]); rsi = float(rsi_s.iloc[-1]); atr = float(atr_s.iloc[-1])
            low_b = float(bbl.iloc[-1]); bb_pct = max(0.0, ((low_b - cp) / low_b) * 100)
            
            paths = run_monte_carlo(df_ticker_15m, cp)
            prob_profit = float(np.mean(paths[-1, :] > cp) * 100)
            
            hist_df = df_ticker_15m.copy(); hist_df['RSI'] = rsi_s; hist_df['BBL'] = bbl
            hist_w, _ = evaluate_historic_outlier(hist_df, rsi, bb_pct, atr, cp)

            self.current_analysis = {
                "ticker": clean_ticker(ticker), "cp": cp, "rsi": rsi, "atr": atr, "ai_prob": signal_data.get("ai_win_prob", 0.5),
                "signal": signal_data.get("signal", "WAIT"), "prob_profit": prob_profit, "hist_win_rate": hist_w,
                "hist_closes": close.tail(150).values.copy(), "hist_bbl": bbl.tail(150).values, "hist_bbu": bbu.tail(150).values, "paths": paths
            }
            
            ai_col = "#00ffaa" if self.current_analysis['ai_prob'] > 0.65 else "#ffaa00" if self.current_analysis['ai_prob'] > 0.50 else "#ff4b4b"
            self.status_label.setText(f"<b>{clean_ticker(ticker)}</b>: ${cp:.2f}<br>AI Prob: <span style='color:{ai_col};'>{self.current_analysis['ai_prob']*100:.1f}%</span><br>MC Win: {prob_profit:.1f}%<br>Hist WR: {hist_w:.1f}%")
            self.switch_view("Forecast")
            
        except Exception as e:
            traceback.print_exc()
            QMessageBox.critical(self, "Analysis Failed", str(e))
        finally:
            self.run_btn.setEnabled(True); self.progress.setVisible(False)

    def render_chart(self):
        if not self.current_analysis: return
        res = self.current_analysis; self.plot_widget.clear(); self.plot_widget.enableAutoRange()
        cp = res['cp']; paths = res['paths']
        
        if self._current_view == "Forecast":
            self.plot_widget.setTitle(f"48h Forecast Cone: {res['ticker']}")
            p95 = np.percentile(paths, 95, axis=1); p90 = np.percentile(paths, 90, axis=1); p50 = np.percentile(paths, 50, axis=1); p10 = np.percentile(paths, 10, axis=1); p5 = np.percentile(paths, 5, axis=1)
            x = np.arange(len(p50))
            self.plot_widget.plot(x, p95, pen=pg.mkPen('#004433', width=1)); self.plot_widget.plot(x, p5, pen=pg.mkPen('#004433', width=1))
            self.plot_widget.plot(x, p90, pen=pg.mkPen('#007755', width=1)); self.plot_widget.plot(x, p10, pen=pg.mkPen('#007755', width=1))
            self.plot_widget.plot(x, p50, pen=pg.mkPen('#00ffaa', width=3))
            self.plot_widget.plot(x, np.full(len(x), cp), pen=pg.mkPen('#888', width=1, style=Qt.PenStyle.DashLine))
            sl = cp - (1.5 * res['atr']); self.plot_widget.plot(x, np.full(len(x), sl), pen=pg.mkPen('#ff4b4b', width=2, style=Qt.PenStyle.DashLine))

        elif self._current_view == "Trend":
            self.plot_widget.setTitle(f"Historical Trend: {res['ticker']}")
            hist = res['hist_closes']; bbl = res['hist_bbl']; bbu = res['hist_bbu']; p50 = np.percentile(paths, 50, axis=1)
            h_x = np.arange(len(hist))
            self.plot_widget.plot(h_x, bbu, pen=pg.mkPen('#222', width=1)); self.plot_widget.plot(h_x, bbl, pen=pg.mkPen('#222', width=1))
            self.plot_widget.plot(h_x, hist, pen=pg.mkPen('#ccc', width=2))
            proj_x = np.arange(len(hist), len(hist)+len(p50))
            self.plot_widget.plot(proj_x, p50, pen=pg.mkPen('#00ffaa', width=3))

        elif self._current_view == "MC Paths":
            self.plot_widget.setTitle(f"MC: 5,000 Paths (Sample 100): {res['ticker']}")
            x = np.arange(paths.shape[0])
            for i in range(100): self.plot_widget.plot(x, paths[:, i], pen=pg.mkPen('#003322', width=1))
            self.plot_widget.plot(x, np.percentile(paths, 50, axis=1), pen=pg.mkPen('#00ffaa', width=2))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = NativeTerminal()
    window.show()
    sys.exit(app.exec())
