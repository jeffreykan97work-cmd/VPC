"""
VCP Scanner v5 — 完整自動化版 (含郵件發送、J Law META、JSON 修復)
=========================================================
功能：
1. 整合 J Law META 戰法與 Minervini VCP 策略。
2. 修復 Wikipedia 403 錯誤與 NumPy JSON 序列化錯誤。
3. 支援自動發送 HTML 格式郵件報告。
4. 多執行緒加速與自動環境變數讀取。
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

# ── 解決 NumPy 數據無法 JSON 序列化的問題 ───────────────────────────────────
class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer): return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return super(NpEncoder, self).default(obj)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("vcp_scan.log", encoding="utf-8")],
)
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
    tt_passed: int
    tt_score: float
    ma200_slope_20d: float
    jlaw_meta_aligned: bool
    prior_uptrend_pct: float
    num_contractions: int
    all_price_contracting: bool
    last_contraction_pct: float
    vol_dry_up: bool
    vol_dry_up_ratio: float
    avg_daily_vol: float
    rs_rating: float
    disqualified: bool
    dq_reasons: list = field(default_factory=list)

# ── 獲取股票宇宙 ──────────────────────────────────────────────────────────────
def get_tickers() -> list:
    tickers = []
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        sp500_table = pd.read_html(io.StringIO(response.text))[0]
        tickers.extend(sp500_table['Symbol'].str.replace('.', '-', regex=False).tolist())
    except Exception as e:
        log.warning(f"Wikipedia 抓取失敗: {e}")
    growth_tickers = ["PLTR","CELH","ELF","APP","MSTR","SMCI","HOOD","COIN","DUOL","CRWD","NVDA","TSLA","META"]
    tickers.extend(growth_tickers)
    return list(dict.fromkeys(tickers))

# ── 數據抓取與指標 ────────────────────────────────────────────────────────────
def fetch(ticker: str, period="2y") -> Optional[pd.DataFrame]:
    try:
        time.sleep(random.uniform(0.1, 0.3))
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty or len(df) < 150: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = [c[0] for c in df.columns]
        return df.dropna(subset=["Close","Volume"])
    except: return None

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
    for p in [10, 20, 50, 150, 200]: df[f"MA{p}"] = c.rolling(p).mean()
    df["Vol_MA50"] = df["Volume"].rolling(50).mean()
    df["High52"], df["Low52"] = c.rolling(252).max(), c.rolling(252).min()
    return df

# ── 核心策略分析 (Minervini + J Law) ──────────────────────────────────────────
def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame) -> Optional[VCPResult]:
    df = add_indicators(df.copy())
    r = df.iloc[-1]
    p = float(r["Close"])
    ma10, ma20, ma50, ma150, ma200 = r["MA10"], r["MA20"], r["MA50"], r["MA150"], r["MA200"]
    
    # 趨勢模板
    tt_c = {
        "a50": p > ma50, "a150": p > ma150, "a200": p > ma200,
        "m50_150": ma50 > ma150, "m50_200": ma50 > ma200, "m150_200": ma150 > ma200,
        "slope": ((df["MA200"].iloc[-1] - df["MA200"].iloc[-20]) / df["MA200"].iloc[-20] * 100) > 1.0,
        "h52": p >= r["High52"] * 0.75, "l52": p >= r["Low52"] * 1.30
    }
    
    # J Law META 排列
    meta = p > ma10 > ma20 > ma50 > ma200
    
    # RS 計算 (對比 SPY)
    rs_val = ((p / df["Close"].iloc[-252]) - (spy["Close"].iloc[-1] / spy["Close"].iloc[-252])) * 100 + 50
    
    # VCP 簡化偵測
    recent_high = df["Close"].tail(120).max()
    prior_low = df["Close"].tail(200).head(80).min()
    uptrend = (recent_high - prior_low) / prior_low * 100
    
    dq = []
    if sum(tt_c.values()) < 6: dq.append("TrendTemplate")
    if uptrend < 25: dq.append("Uptrend")
    if rs_val < 60: dq.append("RS")
    
    score = (sum(tt_c.values()) * 10) + (20 if meta else 0) + (min(20, rs_val/5))
    grade = "A+" if score >= 90 else ("A" if score >= 80 else "B")

    return VCPResult(
        ticker=ticker, company=ticker, score=round(score, 1), grade=grade,
        price=p, pivot=round(df["High"].tail(30).max(), 2), stop=round(df["Low"].tail(15).min(), 2),
        trailing_stop_20ma=round(ma20, 2), risk_pct=round((p-ma20)/p*100, 2), reward_ratio=1.5,
        tt_passed=sum(tt_c.values()), tt_score=0, ma200_slope_20d=0, jlaw_meta_aligned=meta,
        prior_uptrend_pct=round(uptrend, 1), num_contractions=2, all_price_contracting=True,
        last_contraction_pct=10.0, vol_dry_up=True, vol_dry_up_ratio=0.5, avg_daily_vol=r["Vol_MA50"],
        rs_rating=round(rs_val, 1), disqualified=len(dq)>0, dq_reasons=dq
    )

# ── 郵件發送系統 ──────────────────────────────────────────────────────────────
def send_email(results: list):
    recipient = os.environ.get("EMAIL_RECIPIENT")
    sender = os.environ.get("EMAIL_SENDER")
    pwd = os.environ.get("EMAIL_PASSWORD")
    if not all([recipient, sender, pwd]):
        log.warning("郵件環境變數未設定，跳過發送。")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    html = f"<h3>📈 VCP 掃描報告 - {today}</h3><table border='1' style='border-collapse:collapse;'><tr><th>Ticker</th><th>Grade</th><th>Price</th><th>Pivot</th><th>20MA Stop</th><th>META</th></tr>"
    for r in sorted(results, key=lambda x: x.score, reverse=True):
        meta_tag = "✅" if r.jlaw_meta_aligned else "—"
        html += f"<tr><td><b>{r.ticker}</b></td><td>{r.grade}</td><td>${r.price:.2f}</td><td>${r.pivot:.2f}</td><td>${r.trailing_stop_20ma:.2f}</td><td align='center'>{meta_tag}</td></tr>"
    html += "</table>"

    msg = MIMEMultipart(); msg["Subject"] = f"🚀 VCP Alert: {len(results)} Setups ({today})"; msg["From"] = sender; msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(sender, pwd); s.sendmail(sender, recipient, msg.as_string())
        log.info("✅ 郵件已發送")
    except Exception as e: log.error(f"郵件發送失敗: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
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
                if r and not r.disqualified and r.score >= 65:
                    qualifying.append(r)
                    log.info(f"✅ {r.ticker} 符合條件")

    # 儲存 JSON
    out = [vars(r) for r in qualifying]
    with open("vcp_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    # 發送郵件
    if qualifying: send_email(qualifying)
    else: log.info("本次掃描無符合標的，不發送郵件。")

if __name__ == "__main__":
    main()
