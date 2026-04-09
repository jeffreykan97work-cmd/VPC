"""
VCP Scanner v5.2 — 最終穩定版
=========================================================
修復項目：
1. 解決 ValueError (Cannot set a DataFrame with multiple columns)
2. 確保 yfinance 數據列名攤平處理
3. 保持 J Law META 戰法與 20MA 防守邏輯
4. 修復 NumPy JSON 序列化與郵件發送環境變數
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
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        sp500_table = pd.read_html(io.StringIO(response.text))[0]
        tickers.extend(sp500_table['Symbol'].str.replace('.', '-', regex=False).tolist())
    except: pass
    tickers.extend(["PLTR","CELH","ELF","APP","MSTR","SMCI","HOOD","COIN","NVDA","TSLA","META","ARM"])
    return list(dict.fromkeys(tickers))

def fetch(ticker: str) -> Optional[pd.DataFrame]:
    try:
        time.sleep(random.uniform(0.1, 0.3))
        # 下載單個 ticker 確保結構簡單
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty or len(df) < 150: return None
        
        # 關鍵修復：處理 MultiIndex 或多重列名
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # 確保只有一個 Open/High/Low/Close/Volume 列
        df = df.loc[:, ~df.columns.duplicated()]
        
        return df.dropna(subset=["Close", "Volume"])
    except Exception as e:
        log.debug(f"{ticker} fetch error: {e}")
        return None

def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame) -> Optional[VCPResult]:
    # 確保 df 是副本以避免 SettingWithCopyWarning
    df = df.copy()
    c = df["Close"]
    
    # 計算均線
    for p in [10, 20, 50, 150, 200]:
        df[f"MA{p}"] = c.rolling(window=p).mean()
    
    r = df.iloc[-1]
    p_val, ma10, ma20, ma50, ma150, ma200 = r["Close"], r["MA10"], r["MA20"], r["MA50"], r["MA150"], r["MA200"]
    
    # J Law META 排列 (Price > 10 > 20 > 50 > 200)
    meta = (p_val > ma10 > ma20 > ma50 > ma200) if not pd.isna([ma10, ma20, ma50, ma200]).any() else False
    
    # 趨勢模板
    h52 = c.rolling(252).max().iloc[-1]
    l52 = c.rolling(252).min().iloc[-1]
    uptrend = ((c.tail(120).max() - c.tail(200).head(80).min()) / c.tail(200).head(80).min()) * 100
    
    # 相對強度 (RS)
    rs_val = ((p_val / c.iloc[-252]) - (spy["Close"].iloc[-1] / spy["Close"].iloc[-252])) * 100 + 50
    
    dq = []
    if p_val < ma150 or p_val < ma200: dq.append("Below MA")
    if uptrend < 25: dq.append("Uptrend")
    
    score = (60 if not dq else 40) + (20 if meta else 0) + (min(20, rs_val/5))
    grade = "A" if score >= 80 else ("B" if score >= 65 else "C")

    return VCPResult(
        ticker=ticker, company=ticker, score=round(score, 1), grade=grade,
        price=p_val, pivot=round(df["High"].tail(30).max(), 2), stop=round(df["Low"].tail(15).min(), 2),
        trailing_stop_20ma=round(ma20, 2), risk_pct=round((p_val-ma20)/p_val*100, 2), reward_ratio=1.5,
        jlaw_meta_aligned=meta, prior_uptrend_pct=round(uptrend, 1), num_contractions=2,
        rs_rating=round(rs_val, 1), disqualified=len(dq)>0, dq_reasons=dq
    )

def send_email(results: list):
    sender = os.environ.get("EMAIL_SENDER")
    pwd = os.environ.get("EMAIL_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT")
    if not all([sender, pwd, recipient]): 
        log.warning("Email secrets missing, skipping email.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    html = f"<h3>📈 VCP x J Law META 掃描報告 ({today})</h3><table border='1' style='border-collapse:collapse;'><tr><th>Ticker</th><th>Grade</th><th>Price</th><th>Pivot</th><th>20MA 防守</th><th>META</th></tr>"
    for r in sorted(results, key=lambda x: x.score, reverse=True):
        html += f"<tr><td><b>{r.ticker}</b></td><td>{r.grade}</td><td>${r.price:.2f}</td><td>${r.pivot:.2f}</td><td>${r.trailing_stop_20ma:.2f}</td><td align='center'>{'✅' if r.jlaw_meta_aligned else '-'}</td></tr>"
    html += "</table>"

    msg = MIMEMultipart(); msg["Subject"] = f"🚀 VCP Alert: {len(results)} Setups ({today})"; msg["From"] = sender; msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(sender, pwd); s.sendmail(sender, recipient, msg.as_string())
        log.info("✅ 郵件已發送")
    except Exception as e: log.error(f"郵件失敗: {e}")

def main():
    spy_df = fetch("SPY")
    if spy_df is None: return
    tickers = get_tickers()
    qualifying = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticker = {executor.submit(fetch, t): t for t in tickers}
        for future in concurrent.futures.as_completed(future_to_ticker):
            df = future.result()
            if df is not None:
                r = analyze(future_to_ticker[future], df, spy_df)
                if r and not r.disqualified and r.score >= 65: qualifying.append(r)

    with open("vcp_results.json", "w", encoding="utf-8") as f:
        json.dump([vars(r) for r in qualifying], f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    if qualifying: 
        send_email(qualifying)
    log.info(f"掃描完成，符合標的: {len(qualifying)}")

if __name__ == "__main__":
    main()
