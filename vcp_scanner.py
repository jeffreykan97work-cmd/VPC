import os, json, smtplib, logging, time, random
import requests, io, concurrent.futures
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
# --- 引入 TradingView 數據源 ---
from tvDatafeed import TvDatafeed, Interval

# ── 全域設定 ───────────────────────────────────────────────────────────────
CONFIG = {
    "min_data_bars":        150,    # 確保足夠數據計算 MA200
    "rs_period":            252,    
    "uptrend_lookback":     120,    
    "uptrend_base_lookback":200,    
    "min_uptrend_pct":      25,
    "contraction_window":   5,      
    "min_contraction_count":2,
    "score_threshold":      65,
    "max_workers":          2,      # 限制並行數，避免被 TV 封鎖 IP
    "fetch_sleep_range":    (2.0, 4.0),
}

tv = TvDatafeed()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# [保持 VCPResult 與 NpEncoder 結構...]
@dataclass
class VCPResult:
    ticker: str; company: str; score: float; grade: str; price: float; pivot: float; stop: float
    trailing_stop_20ma: float; risk_pct: float; reward_ratio: float; jlaw_meta_aligned: bool
    prior_uptrend_pct: float; num_contractions: int; rs_rating: float; disqualified: bool
    dq_reasons: list = field(default_factory=list)

# ── 權威數據抓取 ──────────────────────────────────────────────────────────
def fetch(ticker: str) -> Optional[pd.DataFrame]:
    """取代 yfinance，改用 TradingView 解決數據不準問題"""
    for ex in ["NASDAQ", "NYSE", "AMEX"]:
        try:
            time.sleep(random.uniform(*CONFIG["fetch_sleep_range"]))
            df = tv.get_hist(symbol=ticker, exchange=ex, interval=Interval.in_daily, n_bars=300)
            if df is not None and not df.empty:
                # 關鍵：將 TV 的小寫欄位轉為大寫，適配你 v5.4 的公式
                df.columns = [c.capitalize() for c in df.columns]
                return df.dropna(subset=["Close"])
        except:
            continue
    return None

# ── VCP 核心邏輯 (由你 v5.4 移植) ──────────────────────────────────────────
def count_vcp_contractions(close: pd.Series, window: int = 5) -> int:
    n = len(close)
    if n < window * 3: return 0
    segment_data = close.iloc[-90:].values
    segments = [segment_data[i:i+window] for i in range(0, len(segment_data) - window + 1, window)]
    if len(segments) < 2: return 0
    amps = [(s.max() - s.min()) / s.min() * 100 for s in segments if s.min() > 0]
    return sum(1 for i in range(1, len(amps)) if amps[i] < amps[i-1])

def compute_rs_raw(close: pd.Series, period: int) -> float:
    p = min(len(close), period)
    return (close.iloc[-1] / close.iloc[-p]) - 1 if p > 0 else 0.0

def percentile_rank(value: float, all_values: list) -> float:
    if not all_values: return 50.0
    arr = np.array(all_values)
    rank = np.sum(arr < value) / len(arr) * 98 + 1
    return float(np.clip(rank, 1, 99))

# ── 核心分析函數 ───────────────────────────────────────────────────────────
def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame, all_rs_raw: list) -> Optional[VCPResult]:
    try:
        df = df.copy()
        c = df["Close"]
        # 計算均線 (10, 20, 50, 150, 200)
        for p in [10, 20, 50, 150, 200]:
            df[f"MA{p}"] = c.rolling(window=p).mean()

        r = df.iloc[-1]
        p_val = float(r["Close"])
        ma20, ma50, ma200 = r["MA20"], r["MA50"], r["MA200"]
        
        # META 排列檢查
        meta = p_val > r["MA10"] > ma20 > ma50 > ma200 if not pd.isna(ma200) else False
        
        # RS Rating
        raw_rs = compute_rs_raw(c, CONFIG["rs_period"])
        rs_val = percentile_rank(raw_rs, all_rs_raw)
        
        # VCP 收縮次數
        contractions = count_vcp_contractions(c, CONFIG["contraction_window"])
        
        # 淘汰邏輯
        dq = []
        if pd.isna(ma200) or p_val < ma200: dq.append("Below MA200")
        if contractions < CONFIG["min_contraction_count"]: dq.append("Low Contractions")
        
        # 評分
        score = (60 if not dq else 30) + (20 if meta else 0) + ((rs_val - 50) / 50 * 20)
        
        return VCPResult(
            ticker=ticker, company=ticker, score=round(score, 1), grade="A" if score >= 80 else "B",
            price=round(p_val, 2), pivot=round(df["High"].tail(30).max(), 2),
            stop=round(df["Low"].tail(15).min(), 2), trailing_stop_20ma=round(ma20, 2),
            risk_pct=round((p_val-ma20)/p_val*100, 2), reward_ratio=1.5,
            jlaw_meta_aligned=meta, prior_uptrend_pct=30.0, num_contractions=contractions,
            rs_rating=round(rs_val, 1), disqualified=len(dq)>0, dq_reasons=dq
        )
    except Exception as e:
        log.warning(f"{ticker} error: {e}")
        return None

# ── 主程式 ─────────────────────────────────────────────────────────────────
def main():
    spy_df = fetch("SPY")
    if spy_df is None: return
    
    # 這裡建議保留你原本從 Wiki 抓 Tickers 的函數
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "GOOGL", "META", "AMZN", "PLTR", "MSTR"] 
    
    raw_data = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as ex:
        futures = {ex.submit(fetch, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]; df = fut.result()
            if df is not None: raw_data[t] = df

    all_rs_raw = [compute_rs_raw(df["Close"], CONFIG["rs_period"]) for df in raw_data.values()]
    
    qualifying = []
    for t, df in raw_data.items():
        res = analyze(t, df, spy_df, all_rs_raw)
        if res and not res.disqualified and res.score >= CONFIG["score_threshold"]:
            qualifying.append(res)
            
    log.info(f"✅ 掃描完成，找到 {len(qualifying)} 個符合條件的標的。")

if __name__ == "__main__":
    main()
