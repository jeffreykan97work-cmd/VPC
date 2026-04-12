import os, json, smtplib, logging, time, random
import requests, io, concurrent.futures
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
# --- 引入 TradingView 套件 ---
from tvDatafeed import TvDatafeed, Interval

# ── 全域設定 ───────────────────────────────────────────────────────────────
CONFIG = {
    "min_data_bars":        150,    # 為了計算 MA200，抓取量需大於 150
    "rs_period":            252,    
    "uptrend_lookback":     120,    
    "uptrend_base_lookback":200,    
    "min_uptrend_pct":      25,
    "contraction_window":   5,      
    "min_contraction_count":2,
    "score_threshold":      65,
    "max_workers":          4,      # TV 接口較敏感，建議並行數調低
    "fetch_sleep_range":    (0.5, 1.5),
}

# 初始化 TradingView (不需帳號密碼可做基礎抓取)
tv = TvDatafeed()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# [此處保留你原有的 NpEncoder 與 VCPResult 資料結構...]
class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray):  return obj.tolist()
        return super().default(obj)

@dataclass
class VCPResult:
    ticker:               str
    company:              str
    score:                float
    grade:                str
    price:                float
    pivot:                float
    stop:                 float
    trailing_stop_20ma:   float
    risk_pct:             float
    reward_ratio:         float
    jlaw_meta_aligned:    bool
    prior_uptrend_pct:    float
    num_contractions:     int
    rs_rating:            float
    disqualified:         bool
    dq_reasons:           list = field(default_factory=list)

# ── 取得股票清單 (與原版相同) ────────────────────────────────────────────────
def get_tickers() -> list:
    tickers = []
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=15)
        sp500 = pd.read_html(io.StringIO(resp.text))[0]
        tickers.extend(sp500["Symbol"].str.replace(".", "-", regex=False).tolist())
    except Exception as e:
        log.warning(f"Wikipedia 抓取失敗: {e}")
    extras = ["PLTR","CELH","ELF","APP","MSTR","SMCI","HOOD","COIN","NVDA","TSLA","META","ARM","AVGO","AMD"]
    tickers.extend(extras)
    return list(dict.fromkeys(tickers))

# ── 關鍵：TradingView 資料抓取函數 ──────────────────────────────────────────
def fetch_tv(ticker: str) -> Optional[pd.DataFrame]:
    """
    從 TradingView 抓取資料。因為 TV 需要知道交易所，
    我們採取：嘗試 NASDAQ -> 嘗試 NYSE -> 嘗試 AMEX 的策略。
    """
    exchanges = ['NASDAQ', 'NYSE', 'AMEX']
    df = None
    
    time.sleep(random.uniform(*CONFIG["fetch_sleep_range"]))
    
    for ex in exchanges:
        try:
            df = tv.get_hist(symbol=ticker, exchange=ex, interval=Interval.in_daily, n_bars=280)
            if df is not None and not df.empty:
                break
        except:
            continue
            
    if df is None or df.empty or len(df) < CONFIG["min_data_bars"]:
        return None

    # TV 的欄位是小寫 (open, close...)，需轉為首字母大寫以相容 analyze()
    df.columns = [c.capitalize() for c in df.columns]
    # 將索引（Datetime）移出變成普通欄位
    df = df.reset_index()
    return df

# [此處保留你原有的 count_vcp_contractions, compute_rs_raw, percentile_rank, analyze 邏輯...]
# 注意：analyze 函數內的 df["Close"] 索引不需要更動，因為我們已經在 fetch_tv 做過欄位大寫化處理。

def count_vcp_contractions(close: pd.Series, window: int = 5) -> int:
    n = len(close)
    if n < window * 3: return 0
    lookback = min(n, 90)
    segment_data = close.iloc[-lookback:].values
    segments = [segment_data[i:i+window] for i in range(0, len(segment_data) - window + 1, window)]
    if len(segments) < 2: return 0
    amplitudes = [(s.max() - s.min()) / s.min() * 100 if s.min() > 0 else np.nan for s in segments]
    amplitudes = [a for a in amplitudes if not np.isnan(a)]
    return sum(1 for i in range(1, len(amplitudes)) if amplitudes[i] < amplitudes[i - 1])

def compute_rs_raw(close: pd.Series, period: int) -> float:
    actual_period = min(len(close), period)
    return (close.iloc[-1] / close.iloc[-actual_period]) - 1

def percentile_rank(value: float, all_values: list) -> float:
    if len(all_values) < 2: return 50.0
    arr = np.array(all_values, dtype=float)
    rank = np.sum(arr < value) / len(arr) * 98 + 1
    return float(np.clip(rank, 1, 99))

def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame, all_rs_raw: Optional[list] = None) -> Optional[VCPResult]:
    # ... (此處與你原版 analyze 邏輯完全一致)
    try:
        df = df.copy()
        c = df["Close"]
        data_len = len(df)
        for p in [10, 20, 50, 150, 200]:
            df[f"MA{p}"] = c.rolling(window=p).mean() if data_len >= p else np.nan

        r = df.iloc[-1]
        p_val = float(r["Close"])
        def ma(n):
            val = r.get(f"MA{n}")
            return float(val) if val is not None and not pd.isna(val) else None
        ma10, ma20, ma50, ma150, ma200 = ma(10), ma(20), ma(50), ma(150), ma(200)

        meta = (all(v is not None for v in [ma10, ma20, ma50, ma200]) and p_val > ma10 > ma20 > ma50 > ma200)
        
        uptrend = 0.0
        if data_len > CONFIG["uptrend_lookback"]:
            base_high = c.tail(CONFIG["uptrend_lookback"]).max()
            pre_slice = c.iloc[max(0, data_len - CONFIG["uptrend_base_lookback"]):max(1, data_len - CONFIG["uptrend_lookback"])]
            pre_base_low = pre_slice.min() if len(pre_slice) > 0 else 0
            if pre_base_low > 0: uptrend = (base_high - pre_base_low) / pre_base_low * 100

        stock_rs_raw = compute_rs_raw(c, CONFIG["rs_period"])
        if all_rs_raw and len(all_rs_raw) >= 10:
            rs_val = percentile_rank(stock_rs_raw, all_rs_raw)
        else:
            spy_rs_raw = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[-min(len(spy), 252)])) - 1
            rs_val = float(np.clip(50 + (stock_rs_raw - spy_rs_raw) * 125, 1, 99))

        num_contractions = count_vcp_contractions(c, window=CONFIG["contraction_window"])
        dq = []
        if ma200 is None or p_val < ma200: dq.append("Below MA200")
        if uptrend < CONFIG["min_uptrend_pct"]: dq.append(f"Uptrend < {CONFIG['min_uptrend_pct']}%")
        if num_contractions < CONFIG["min_contraction_count"]: dq.append(f"Contractions < {CONFIG['min_contraction_count']}")

        base_score = 60 if not dq else 30
        meta_score = 20 if meta else 0
        rs_score = float(np.clip((rs_val - 50) / 50 * 20, -10, 20))
        score = base_score + meta_score + rs_score
        grade = "A" if score >= 80 else ("B" if score >= 65 else "C")
        
        pivot = round(float(df["High"].tail(30).max()), 2)
        stop = round(float(df["Low"].tail(15).min()), 2)
        risk = p_val - stop
        reward_ratio = round((pivot - p_val) / risk, 2) if risk > 0 else 0.0
        risk_pct = round((p_val - (ma20 or p_val)) / p_val * 100, 2) if ma20 and p_val > 0 else 0.0

        return VCPResult(
            ticker=ticker, company=ticker, score=round(score, 1), grade=grade,
            price=round(p_val, 2), pivot=pivot, stop=stop, trailing_stop_20ma=round(ma20, 2) if ma20 else 0.0,
            risk_pct=risk_pct, reward_ratio=reward_ratio, jlaw_meta_aligned=meta, prior_uptrend_pct=round(uptrend, 1),
            num_contractions=num_contractions, rs_rating=round(rs_val, 1), disqualified=len(dq) > 0, dq_reasons=dq,
        )
    except Exception as e:
        log.warning(f"{ticker} analyze error: {e}")
        return None

# [此處保留 send_email 函數...]

def main() -> None:
    # 1. 先抓取 SPY 作為基準
    spy_df = fetch_tv("SPY")
    if spy_df is None:
        log.error("無法獲取 SPY 數據，中止。")
        return

    tickers = get_tickers()
    log.info(f"開始從 TradingView 掃描 {len(tickers)} 檔股票...")

    # Phase 1：平行下載 (使用 tvDatafeed)
    raw_data: dict[str, pd.DataFrame] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as ex:
        futures = {ex.submit(fetch_tv, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            df = fut.result()
            if df is not None:
                raw_data[t] = df

    log.info(f"成功獲取 {len(raw_data)} 檔資料，進行分析...")

    # Phase 2 & 3: 計算 RS 與 進行 VCP 分析
    rs_period = CONFIG["rs_period"]
    all_rs_raw = [compute_rs_raw(df["Close"], rs_period) for t, df in raw_data.items()]
    
    qualifying = []
    spy_copy = spy_df.copy()
    for ticker, df in raw_data.items():
        result = analyze(ticker, df, spy_copy, all_rs_raw)
        if result and not result.disqualified and result.score >= CONFIG["score_threshold"]:
            qualifying.append(result)

    # 保存與發送
    with open("vcp_results.json", "w", encoding="utf-8") as f:
        json.dump([vars(r) for r in qualifying], f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    # if qualifying: send_email(qualifying)
    log.info(f"✅ 掃描完成。合格標的: {len(qualifying)}")

if __name__ == "__main__":
    main()
