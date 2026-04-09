"""
VCP Scanner v5.3 — 穩定增強版
=========================================================
修復項目：
1. 修復 IndexError (位置索引越界)，增加數據長度檢查。
2. 優化 RS 計算邏輯，支援上市未滿一年的股票。
3. 延續 v5.2 的 MultiIndex 數據結構修復。
4. 整合 J Law META 戰法與自動郵件發送。
"""

import os, json, smtplib, logging, time, random
import requests
import io
import concurrent.futures
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
import yfinance as yf

# ── NumPy JSON 編碼器 ──────────────────────────────────────────────────────
class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer): return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return super(NpEncoder, self).default(obj)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

@dataclass
class VCPResult:
    ticker: str
    company: str
    score: float
    grade: str
    price: float
    pivot: float
    stop: float
    trailing_stop_20ma: float
    risk_pct: float
    reward_ratio: float
    jlaw_meta_aligned: bool
    prior_uptrend_pct: float
    num_contractions: int
    rs_rating: float
    disqualified: bool
    dq_reasons: list = field(default_factory=list)

def get_tickers() -> list:
    tickers = []
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        response = requests.get(url, headers=headers, timeout=15)
        sp500_table = pd.read_html(io.StringIO(response.text))[0]
        tickers.extend(sp500_table['Symbol'].str.replace('.', '-', regex=False).tolist())
    except Exception as e:
        log.warning(f"Wikipedia 抓取失敗: {e}")
    tickers.extend(["PLTR","CELH","ELF","APP","MSTR","SMCI","HOOD","COIN","NVDA","TSLA","META","ARM","AVGO","AMD"])
    return list(dict.fromkeys(tickers))

def fetch(ticker: str) -> Optional[pd.DataFrame]:
    try:
        time.sleep(random.uniform(0.1, 0.4))
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty or len(df) < 50: return None
        
        # 處理 MultiIndex 結構
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.loc[:, ~df.columns.duplicated()]
        
        return df.dropna(subset=["Close", "Volume"])
    except Exception as e:
        log.debug(f"{ticker} fetch error: {e}")
        return None

def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame) -> Optional[VCPResult]:
    df = df.copy()
    c = df["Close"]
    data_len = len(df)
    
    # 計算均線
    for p in [10, 20, 50, 150, 200]:
        if data_len >= p:
            df[f"MA{p}"] = c.rolling(window=p).mean()
        else:
            df[f"MA{p}"] = np.nan
    
    r = df.iloc[-1]
    p_val = r["Close"]
    ma10, ma20, ma50, ma150, ma200 = r.get("MA10"), r.get("MA20"), r.get("MA50"), r.get("MA150"), r.get("MA200")
    
    # J Law META 排列 (Price > 10 > 20 > 50 > 200)
    meta = False
    if all(not pd.isna(x) for x in [ma10, ma20, ma50, ma200]):
        meta = (p_val > ma10 > ma20 > ma50 > ma200)
    
    # 趨勢模板與 VCP 升勢
    uptrend = 0
    if data_len > 120:
        base_high = c.tail(120).max()
        pre_base_low = c.iloc[max(0, data_len-200):max(1, data_len-120)].min()
        uptrend = ((base_high - pre_base_low) / pre_base_low) * 100 if pre_base_low > 0 else 0
    
    # 修復版 RS 計算 (處理數據不足 252 天的情況)
    rs_period = min(data_len, 252)
    spy_period = min(len(spy), 252)
    stock_perf = (p_val / c.iloc[-rs_period]) - 1
    spy_perf = (spy["Close"].iloc[-1] / spy["Close"].iloc[-spy_period]) - 1
    rs_val = (stock_perf - spy_perf) * 100 + 50
    
    dq = []
    if pd.isna(ma200) or p_val < ma200: dq.append("Below MA200")
    if uptrend < 25: dq.append("Uptrend < 25%")
    
    score = (60 if not dq else 30) + (20 if meta else 0) + (min(20, rs_val/5))
    grade = "A" if score >= 80 else ("B" if score >= 65 else "C")

    return VCPResult(
        ticker=ticker, company=ticker, score=round(score, 1), grade=grade,
        price=p_val, pivot=round(df["High"].tail(30).max(), 2), 
        stop=round(df["Low"].tail(15).min(), 2),
        trailing_stop_20ma=round(ma20, 2) if not pd.isna(ma20) else 0,
        risk_pct=round((p_val-ma20)/p_val*100, 2) if not pd.isna(ma20) and p_val > 0 else 0,
        reward_ratio=1.5, jlaw_meta_aligned=meta, prior_uptrend_pct=round(uptrend, 1),
        num_contractions=2, rs_rating=round(rs_val, 1), disqualified=len(dq)>0, dq_reasons=dq
    )

def send_email(results: list):
    sender = os.environ.get("EMAIL_SENDER")
    pwd = os.environ.get("EMAIL_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT")
    if not all([sender, pwd, recipient]): 
        log.warning("郵件環境變數缺失，跳過發送。")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    html = f"<h3>📈 VCP x J Law META 掃描報告 ({today})</h3><table border='1' style='border-collapse:collapse; width:100%; text-align:center;'> <tr style='background-color: #f2f2f2;'><th>Ticker</th><th>Grade</th><th>Price</th><th>Pivot</th><th>20MA 防守</th><th>META</th></tr>"
    for r in sorted(results, key=lambda x: x.score, reverse=True):
        html += f"<tr><td><b>{r.ticker}</b></td><td>{r.grade}</td><td>${r.price:.2f}</td><td>${r.pivot:.2f}</td><td>${r.trailing_stop_20ma:.2f}</td><td>{'✅' if r.jlaw_meta_aligned else '-'}</td></tr>"
    html += "</table>"

    msg = MIMEMultipart(); msg["Subject"] = f"🚀 VCP Alert: {len(results)} Setups ({today})"; msg["From"] = sender; msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(sender, pwd); s.sendmail(sender, recipient, msg.as_string())
        log.info("✅ 郵件已發送")
    except Exception as e: log.error(f"郵件發送失敗: {e}")

def main():
    spy_df = fetch("SPY")
    if spy_df is None: 
        log.error("無法獲取 SPY 數據，中止。")
        return
    
    tickers = get_tickers()
    log.info(f"開始掃描 {len(tickers)} 檔股票...")
    qualifying = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticker = {executor.submit(fetch, t): t for t in tickers}
        for future in concurrent.futures.as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            df = future.result()
            if df is not None:
                r = analyze(ticker, df, spy_df)
                if r and not r.disqualified and r.score >= 65:
                    qualifying.append(r)

    # 儲存結果
    with open("vcp_results.json", "w", encoding="utf-8") as f:
        json.dump([vars(r) for r in qualifying], f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    if qualifying: 
        send_email(qualifying)
    log.info(f"掃描完成。合格標的: {len(qualifying)}")

if __name__ == "__main__":
    main()
