"""
VCP Scanner v3 — 強化版 Minervini SEPA 掃描器
====================================================
優化項目：
1. 動態計算先前升勢 (Prior Uptrend)
2. 加入 VCP 收縮容錯機制 (Tolerance)
3. 動態抓取 S&P 500 成分股擴充掃描池
4. 使用 ThreadPoolExecutor 多執行緒加速下載並減少超時錯誤
"""

import os, json, smtplib, logging, time, random
import concurrent.futures
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
import yfinance as yf

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("vcp_scan.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class VCPResult:
    ticker: str
    company: str
    score: float
    grade: str
    price: float
    pivot: float
    stop: float
    risk_pct: float
    reward_ratio: float
    tt_passed: int
    tt_score: float
    ma200_slope_20d: float
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

# ── 動態獲取股票宇宙 ────────────────────────────────────────────────────────
def get_tickers() -> list:
    tickers = []
    # 嘗試動態抓取 S&P 500
    try:
        sp500_table = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
        # Yahoo Finance 使用 '-' 代替 '.' (例如 BRK.B -> BRK-B)
        sp500_tickers = sp500_table['Symbol'].str.replace('.', '-', regex=False).tolist()
        tickers.extend(sp500_tickers)
        log.info(f"成功抓取 {len(sp500_tickers)} 檔 S&P 500 成分股")
    except Exception as e:
        log.warning(f"無法抓取 S&P 500，將使用備用清單: {e}")

    # 加入精選高成長科技/動能股作為補充
    growth_tickers = [
        "PLTR","CELH","ELF","APP","MSTR","SMCI","HOOD","COIN","DUOL","CRWD",
        "PANW","DDOG","MDB","NET","SNOW","SHOP","SE","MELI","ARM","IOT"
    ]
    tickers.extend(growth_tickers)
    
    # 去除重複項
    return list(dict.fromkeys(tickers))

# ── Data fetch (加入重試機制) ───────────────────────────────────────────────
def fetch(ticker: str, period="2y", retries=2) -> Optional[pd.DataFrame]:
    for attempt in range(retries):
        try:
            # 加入隨機延遲避免觸發 HTTP 429 Too Many Requests
            time.sleep(random.uniform(0.1, 0.5))
            df = yf.download(ticker, period=period, auto_adjust=True, progress=False, threads=False)
            if df is None or df.empty or len(df) < 150:
                return None
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df.dropna(subset=["Close","Volume"])
        except Exception as e:
            if "429" in str(e):
                time.sleep(2) # 遇到 429 強制休息
            if attempt == retries - 1:
                return None

# ── Indicators ────────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
    v = df["Volume"]
    for p in [10, 20, 50, 150, 200]:
        df[f"MA{p}"] = c.rolling(p).mean()
    df["Vol_MA50"] = v.rolling(50).mean()
    df["Vol_MA10"] = v.rolling(10).mean()
    df["High52"]   = c.rolling(252).max()
    df["Low52"]    = c.rolling(252).min()
    return df

# ── Filter 1: Trend Template ────────────────────────────────────────────────
def check_trend_template(df: pd.DataFrame) -> dict:
    r = df.iloc[-1]
    p = float(r["Close"])
    ma50, ma150, ma200 = float(r.get("MA50", np.nan)), float(r.get("MA150", np.nan)), float(r.get("MA200", np.nan))
    h52, l52 = float(r.get("High52", np.nan)), float(r.get("Low52", np.nan))

    def ok(v): return not np.isnan(v)

    c = {
        "above_ma50": ok(ma50) and p > ma50,
        "above_ma150": ok(ma150) and p > ma150,
        "above_ma200": ok(ma200) and p > ma200,
        "ma50_gt_ma150": ok(ma50) and ok(ma150) and ma50 > ma150,
        "ma50_gt_ma200": ok(ma50) and ok(ma200) and ma50 > ma200,
        "ma150_gt_ma200": ok(ma150) and ok(ma200) and ma150 > ma200,
    }

    ma200_series = df["MA200"].dropna().tail(21)
    if len(ma200_series) >= 21:
        slope = (ma200_series.iloc[-1] - ma200_series.iloc[0]) / ma200_series.iloc[0] * 100
        c["ma200_rising"] = slope > 1.0
    else:
        c["ma200_rising"] = False
        slope = 0.0

    c["near_52w_high"] = ok(h52) and h52 > 0 and p >= h52 * 0.75
    c["above_52w_low"] = ok(l52) and l52 > 0 and p >= l52 * 1.30

    passed = sum(c.values())
    return {**c, "passed": passed, "score": passed / 8 * 100, "ma200_slope": slope}

# ── Filter 2: 動態計算先前的升勢 ──────────────────────────────────────────
def check_prior_uptrend(df: pd.DataFrame) -> dict:
    if len(df) < 200:
        return {"valid": False, "pct": 0}

    # 在最近 120 天內尋找潛在底基的高點 (Base High)
    base_window = df.tail(120)
    high_idx = base_window["Close"].idxmax()
    high_price = float(df.loc[high_idx, "Close"])
    
    # 獲取該高點在整個 dataframe 中的絕對索引
    abs_idx = df.index.get_loc(high_idx)
    
    # 往前回溯 60-150 天尋找起漲點 (Prior Low)
    if abs_idx < 60:
        return {"valid": False, "pct": 0}
        
    start_search = max(0, abs_idx - 150)
    prior_low = float(df["Close"].iloc[start_search:abs_idx].min())
    
    pct = (high_price - prior_low) / prior_low * 100
    return {"valid": pct >= 30.0, "pct": round(pct, 1)}

# ── Filter 3: Relative Strength ─────────────────────────────────────────────
def calc_rs(df: pd.DataFrame, spy: pd.DataFrame) -> dict:
    def perf(d, n):
        if len(d) < n: return 0.0
        return (float(d["Close"].iloc[-1]) / float(d["Close"].iloc[-n]) - 1) * 100

    s  = perf(df, 63)*0.40 + perf(df, 126)*0.20 + perf(df, 252)*0.40
    sp = perf(spy,63)*0.40 + perf(spy,126)*0.20 + perf(spy,252)*0.40
    rel = s - sp
    
    rs = 50 + (rel * 1.5) # 簡化版映射
    rs = min(99, max(1, rs))
    return {"rs": round(rs,1), "score": round(rs,1)}

# ── Filter 4: 動態 VCP 辨識 (加入容錯) ────────────────────────────────────
def detect_vcp(df: pd.DataFrame) -> dict:
    window = df.tail(150).copy()
    close = window["Close"].values.astype(float)
    vol   = window["Volume"].values.astype(float)

    # 稍微放寬波段判斷區間
    order = 5 
    highs_idx, lows_idx = [], []
    for i in range(order, len(close)-order):
        if close[i] == max(close[i-order:i+order+1]): highs_idx.append(i)
        if close[i] == min(close[i-order:i+order+1]): lows_idx.append(i)

    if len(highs_idx) < 2 or len(lows_idx) < 2:
        return {"valid": False, "score": 0, "n": 0}

    all_pts = sorted(
        [(i,"H",close[i],vol[i]) for i in highs_idx[-8:]] +
        [(i,"L",close[i],vol[i]) for i in lows_idx[-8:]],
        key=lambda x: x[0]
    )

    contractions = []
    for k in range(len(all_pts)-1):
        a, b = all_pts[k], all_pts[k+1]
        if a[1]=="H" and b[1]=="L":
            pct = (a[2]-b[2])/a[2]*100
            contractions.append({"pct": round(pct,2)})

    if len(contractions) < 2:
        return {"valid": False, "score": 0, "n": len(contractions)}

    all_price_ok = True
    for i in range(1, len(contractions)):
        # 容許 10% 的誤差，避免稍微突出就被判定無效
        if contractions[i]["pct"] > contractions[i-1]["pct"] * 1.10:
            all_price_ok = False
            
    last_pct = contractions[-1]["pct"]
    n = len(contractions)
    
    score = 50
    if all_price_ok: score += 30
    if last_pct <= 10: score += 20
    
    return {
        "valid": all_price_ok and (2 <= n <= 5) and last_pct <= 15,
        "score": min(score, 100),
        "n": n,
        "all_price_contracting": all_price_ok,
        "last_pct": last_pct
    }

# ── Filter 5: Volume Dry-Up ───────────────────────────────────────────────
def check_volume(df: pd.DataFrame) -> dict:
    vol10   = float(df["Volume"].tail(10).mean())
    vol_ma50= float(df.iloc[-1].get("Vol_MA50", np.nan))
    if np.isnan(vol_ma50) or vol_ma50 == 0:
        return {"score": 0, "dry_up": False, "ratio": 1.0, "avg": 0}

    ratio = vol10 / vol_ma50
    score = 100 if ratio < 0.5 else (80 if ratio < 0.7 else 0)
    return {"score": score, "dry_up": ratio < 0.60, "ratio": round(ratio, 2), "avg": round(vol_ma50, 0)}

# ── 主分析函數 ──────────────────────────────────────────────────────────────
def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame) -> Optional[VCPResult]:
    try:
        df = add_indicators(df.copy())
        if len(df) < 200: return None

        price = float(df["Close"].iloc[-1])
        if price < 10: return None

        tt  = check_trend_template(df)
        pu  = check_prior_uptrend(df)
        rs  = calc_rs(df, spy)
        vcp = detect_vcp(df)
        vol = check_volume(df)

        company = ticker # 簡化處理以加速

        dq_reasons = []
        if tt["passed"] < 6: dq_reasons.append(f"TrendTemplate {tt['passed']}/8")
        if pu["pct"] < 25.0: dq_reasons.append(f"Prior uptrend weak (+{pu['pct']}%)")
        if rs["rs"] < 60:    dq_reasons.append(f"RS weak ({rs['rs']})")
        if vol["avg"] < 300_000: dq_reasons.append("Illiquid")
        
        # 放寬條件：即使不是完美的遞減，只要最後一次收縮夠緊密也算過關
        if not vcp["all_price_contracting"] and vcp.get("last_pct", 100) > 12:
            dq_reasons.append("VCP not shrinking and last > 12%")

        disqualified = len(dq_reasons) > 0

        composite = (vcp["score"]*0.35 + tt["score"]*0.25 + vol["score"]*0.20 + rs["score"]*0.20)
        grade = "A" if composite >= 80 else ("B" if composite >= 65 else "C")

        # Pivot & Stop calculation
        recent = df.tail(30)
        pivot = float(recent["High"].max())
        stop = max(float(recent["Low"].tail(15).min()), price * 0.92)

        return VCPResult(
            ticker=ticker, company=company, score=round(composite,1), grade=grade,
            price=price, pivot=round(pivot,2), stop=round(stop,2),
            risk_pct=round((price-stop)/price*100, 2), reward_ratio=round(((pivot*1.2)-price)/(price-stop), 2) if (price-stop)>0 else 0,
            tt_passed=tt["passed"], tt_score=tt["score"], ma200_slope_20d=tt["ma200_slope"],
            prior_uptrend_pct=pu["pct"], num_contractions=vcp.get("n",0),
            all_price_contracting=vcp.get("all_price_contracting",False), last_contraction_pct=vcp.get("last_pct",0),
            vol_dry_up=vol["dry_up"], vol_dry_up_ratio=vol["ratio"], avg_daily_vol=vol["avg"],
            rs_rating=rs["rs"], disqualified=disqualified, dq_reasons=dq_reasons
        )
    except Exception as e:
        log.debug(f"{ticker} 處理錯誤: {e}")
        return None

# ── Main 多執行緒架構 ───────────────────────────────────────────────────────
def main():
    log.info("="*60)
    log.info("🔍 VCP Scanner v3 — 多執行緒優化版")
    log.info("="*60)

    spy_df = fetch("SPY", retries=3)
    if spy_df is None:
        log.error("無法獲取基準指數 SPY，中止運行。")
        return
    spy_df = add_indicators(spy_df)

    tickers = get_tickers()
    log.info(f"掃描宇宙包含: {len(tickers)} 檔股票")

    qualifying = []
    dq_count = 0

    # 使用 ThreadPoolExecutor 並發處理
    MAX_WORKERS = 10 # 若網絡不穩可調低至 5
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 建立 Future 字典
        future_to_ticker = {executor.submit(fetch, t): t for t in tickers}
        
        processed_count = 0
        for future in concurrent.futures.as_completed(future_to_ticker):
            t = future_to_ticker[future]
            processed_count += 1
            
            if processed_count % 50 == 0:
                log.info(f"進度: [{processed_count}/{len(tickers)}] | 合格: {len(qualifying)}")

            df = future.result()
            if df is None:
                continue

            r = analyze(t, df, spy_df)
            if r is None:
                continue

            if r.disqualified:
                dq_count += 1
                continue

            # 只有評分 >= 65 才記錄
            if r.score >= 65:
                qualifying.append(r)
                log.info(f"  ✅ {t} [{r.grade}] | VCP={r.num_contractions}x | "
                         f"Last_C={r.last_contraction_pct:.1f}% | Uptrend={r.prior_uptrend_pct:.1f}%")

    log.info("="*60)
    log.info(f"掃描完成。合格標的: {len(qualifying)} | 淘汰: {dq_count}")

    # 輸出結果
    out = [vars(r) for r in sorted(qualifying, key=lambda x: x.score, reverse=True)]
    with open("vcp_results.json","w") as f:
        json.dump(out, f, indent=2)
    log.info("結果已儲存至 → vcp_results.json")

if __name__ == "__main__":
    main()
