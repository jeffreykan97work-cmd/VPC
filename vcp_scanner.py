"""
VCP Scanner v4 — 結合 Minervini SEPA 與 J Law META 戰法
=========================================================
優化項目：
1. 修復 Wikipedia S&P500 抓取的 403 Forbidden 錯誤
2. 導入 J Law META 戰法 (10MA/20MA/50MA/200MA 多頭排列檢測)
3. 增加基於 20MA 的動態移動停損點 (Trailing Stop)
4. 多執行緒並發下載加速
5. 動態 VCP 容錯機制與真實升勢計算
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
    stop: float               # 靜態破底停損
    trailing_stop_20ma: float # J Law 動態 20MA 防守線
    risk_pct: float
    reward_ratio: float
    
    # 趨勢與 J Law META 狀態
    tt_passed: int
    tt_score: float
    ma200_slope_20d: float
    jlaw_meta_aligned: bool   # 是否符合 Price > 10 > 20 > 50 > 200
    
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

# ── 動態獲取股票宇宙 (修復 403 錯誤) ─────────────────────────────────────────
def get_tickers() -> list:
    tickers = []
    
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status() 
        
        sp500_table = pd.read_html(io.StringIO(response.text))[0]
        sp500_tickers = sp500_table['Symbol'].str.replace('.', '-', regex=False).tolist()
        tickers.extend(sp500_tickers)
        log.info(f"成功抓取 {len(sp500_tickers)} 檔 S&P 500 成分股")
        
    except Exception as e:
        log.warning(f"無法抓取 S&P 500，將使用備用清單: {e}")

    # 加入精選高成長科技/動能股作為補充
    growth_tickers = [
        "PLTR","CELH","ELF","APP","MSTR","SMCI","HOOD","COIN","DUOL","CRWD",
        "PANW","DDOG","MDB","NET","SNOW","SHOP","SE","MELI","ARM","IOT",
        "NVDA","TSLA","META","AVGO","AMD"
    ]
    tickers.extend(growth_tickers)
    
    return list(dict.fromkeys(tickers))

# ── Data fetch ──────────────────────────────────────────────────────────────
def fetch(ticker: str, period="2y", retries=2) -> Optional[pd.DataFrame]:
    for attempt in range(retries):
        try:
            time.sleep(random.uniform(0.1, 0.4)) # 避免觸發 429
            df = yf.download(ticker, period=period, auto_adjust=True, progress=False, threads=False)
            if df is None or df.empty or len(df) < 150:
                return None
            
            # yfinance>=0.2.40 有時會回傳 MultiIndex columns，將其攤平
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
                
            return df.dropna(subset=["Close","Volume"])
        except Exception as e:
            if "429" in str(e):
                time.sleep(2)
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

# ── Filter 1: Trend Template & J Law META ───────────────────────────────────
def check_trend_template(df: pd.DataFrame) -> dict:
    r = df.iloc[-1]
    p = float(r["Close"])
    ma10, ma20 = float(r.get("MA10", np.nan)), float(r.get("MA20", np.nan))
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

    # J Law META Alignment: 完美多頭排列 (強力攻擊姿態)
    meta_aligned = ok(ma10) and ok(ma20) and ok(ma50) and ok(ma200) and \
                   (p > ma10 > ma20 > ma50 > ma200)

    passed = sum(c.values())
    return {
        **c, 
        "passed": passed, 
        "score": passed / 8 * 100, 
        "ma200_slope": slope,
        "meta_aligned": meta_aligned,
        "ma20_val": ma20
    }

# ── Filter 2: 動態計算先前的升勢 ──────────────────────────────────────────
def check_prior_uptrend(df: pd.DataFrame) -> dict:
    if len(df) < 200:
        return {"valid": False, "pct": 0}

    base_window = df.tail(120)
    high_idx = base_window["Close"].idxmax()
    high_price = float(df.loc[high_idx, "Close"])
    
    abs_idx = df.index.get_loc(high_idx)
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
    
    rs = 50 + (rel * 1.5) 
    rs = min(99, max(1, rs))
    return {"rs": round(rs,1), "score": round(rs,1)}

# ── Filter 4: 動態 VCP 辨識 (加入容錯) ────────────────────────────────────
def detect_vcp(df: pd.DataFrame) -> dict:
    window = df.tail(150).copy()
    close = window["Close"].values.astype(float)
    vol   = window["Volume"].values.astype(float)

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
        if contractions[i]["pct"] > contractions[i-1]["pct"] * 1.10: # 10% 容錯
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

        dq_reasons = []
        if tt["passed"] < 6: dq_reasons.append(f"TrendTemplate {tt['passed']}/8")
        if pu["pct"] < 25.0: dq_reasons.append(f"Prior uptrend weak (+{pu['pct']:.0f}%)")
        if rs["rs"] < 60:    dq_reasons.append(f"RS weak ({rs['rs']})")
        if vol["avg"] < 300_000: dq_reasons.append("Illiquid")
        
        if not vcp["all_price_contracting"] and vcp.get("last_pct", 100) > 12:
            dq_reasons.append("VCP not shrinking & last > 12%")

        disqualified = len(dq_reasons) > 0

        # 計分加權 (若符合 J Law META，給予額外 10 分加權)
        composite = (vcp["score"]*0.35 + tt["score"]*0.25 + vol["score"]*0.20 + rs["score"]*0.20)
        if tt["meta_aligned"]:
            composite = min(100, composite + 10)

        grade = "A+" if composite >= 90 else ("A" if composite >= 80 else ("B" if composite >= 65 else "C"))

        # Pivot & Stop calculation
        recent = df.tail(30)
        pivot = float(recent["High"].max())
        stop = max(float(recent["Low"].tail(15).min()), price * 0.92) # 靜態波段停損
        
        # J Law 20MA 動態防守線
        ma20 = float(tt["ma20_val"])
        trailing_stop_20ma = round(ma20, 2)

        return VCPResult(
            ticker=ticker, company=ticker, score=round(composite,1), grade=grade,
            price=price, pivot=round(pivot,2), stop=round(stop,2), 
            trailing_stop_20ma=trailing_stop_20ma,
            risk_pct=round((price-stop)/price*100, 2), 
            reward_ratio=round(((pivot*1.2)-price)/(price-stop), 2) if (price-stop)>0 else 0,
            tt_passed=tt["passed"], tt_score=tt["score"], ma200_slope_20d=tt["ma200_slope"],
            jlaw_meta_aligned=tt["meta_aligned"],
            prior_uptrend_pct=pu["pct"], num_contractions=vcp.get("n",0),
            all_price_contracting=vcp.get("all_price_contracting",False), last_contraction_pct=vcp.get("last_pct",0),
            vol_dry_up=vol["dry_up"], vol_dry_up_ratio=vol["ratio"], avg_daily_vol=vol["avg"],
            rs_rating=rs["rs"], disqualified=disqualified, dq_reasons=dq_reasons
        )
    except Exception as e:
        log.debug(f"{ticker} 處理錯誤: {e}")
        return None

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log.info("="*65)
    log.info("🔍 VCP Scanner v4 — Minervini SEPA x J Law META")
    log.info("="*65)

    spy_df = fetch("SPY", retries=3)
    if spy_df is None:
        log.error("無法獲取基準指數 SPY，中止運行。")
        return
    spy_df = add_indicators(spy_df)

    tickers = get_tickers()
    log.info(f"掃描宇宙包含: {len(tickers)} 檔股票")

    qualifying = []
    dq_count = 0

    MAX_WORKERS = 10 
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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

            if r.score >= 65:
                qualifying.append(r)
                meta_flag = "🌟 META Align" if r.jlaw_meta_aligned else ""
                log.info(f"  ✅ {t} [{r.grade}] | VCP={r.num_contractions}x | "
                         f"RS={r.rs_rating:.0f} | 20MA防守=${r.trailing_stop_20ma} {meta_flag}")

    log.info("="*65)
    log.info(f"掃描完成。合格標的: {len(qualifying)} | 淘汰: {dq_count}")

    # 輸出結果
    out = [vars(r) for r in sorted(qualifying, key=lambda x: x.score, reverse=True)]
    with open("vcp_results.json","w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    log.info("結果已儲存至 → vcp_results.json")

if __name__ == "__main__":
    main()
