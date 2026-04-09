"""
VCP Scanner v5.4 — 優化增強版
=========================================================
優化項目：
1. 實作真正的 VCP 收縮區間計數（原版硬編碼為 2）。
2. RS Rating 改用全市場百分位排名，並限制在 1–99。
3. 動態 reward_ratio：根據 pivot/stop 實際計算。
4. 修正 Score 公式，RS 貢獻以 clamp 取代，避免負值拉垮結果。
5. analyze() 加 try/catch，單股例外不中斷整體掃描。
6. SPY DataFrame 傳入前先 .copy()，保障執行緒安全。
7. Email 加入簡單重試邏輯。
8. 移除硬編碼魔術數字，集中至 CONFIG。
"""

import os, json, smtplib, logging, time, random
import requests, io, concurrent.futures
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
import yfinance as yf

# ── 全域設定 ───────────────────────────────────────────────────────────────
CONFIG = {
    "min_data_bars":        50,
    "rs_period":            252,    # 52週 RS 計算基準
    "uptrend_lookback":     120,    # 升勢高點回溯天數
    "uptrend_base_lookback":200,    # 升勢低點回溯天數
    "min_uptrend_pct":      25,
    "contraction_window":   5,      # 收縮區間辨識：每段最小 bar 數
    "min_contraction_count":2,
    "score_threshold":      65,
    "max_workers":          10,
    "fetch_sleep_range":    (0.1, 0.4),
    "email_retries":        3,
    "email_retry_delay":    5,
}

# ── NumPy JSON 編碼器 ──────────────────────────────────────────────────────
class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray):  return obj.tolist()
        return super().default(obj)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ── 資料結構 ───────────────────────────────────────────────────────────────
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


# ── 取得股票清單 ───────────────────────────────────────────────────────────
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

    extras = ["PLTR","CELH","ELF","APP","MSTR","SMCI","HOOD","COIN",
              "NVDA","TSLA","META","ARM","AVGO","AMD"]
    tickers.extend(extras)
    return list(dict.fromkeys(tickers))  # 去重、保序


# ── 資料下載 ───────────────────────────────────────────────────────────────
def fetch(ticker: str) -> Optional[pd.DataFrame]:
    try:
        time.sleep(random.uniform(*CONFIG["fetch_sleep_range"]))
        df = yf.download(ticker, period="2y", auto_adjust=True,
                         progress=False, threads=False)
        if df is None or df.empty or len(df) < CONFIG["min_data_bars"]:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.loc[:, ~df.columns.duplicated()]

        return df.dropna(subset=["Close", "Volume"])
    except Exception as e:
        log.debug(f"{ticker} fetch error: {e}")
        return None


# ── VCP 收縮計數 ───────────────────────────────────────────────────────────
def count_vcp_contractions(close: pd.Series, window: int = 5) -> int:
    """
    將近期走勢切成等長區段，計算振幅逐段收縮的次數。
    振幅定義：(max - min) / min * 100
    """
    n = len(close)
    if n < window * 3:
        return 0

    # 取最近 ~90 根 bar，切成若干 window 大小的區段
    lookback = min(n, 90)
    segment_data = close.iloc[-lookback:].values
    segments = [segment_data[i:i+window]
                for i in range(0, len(segment_data) - window + 1, window)]

    if len(segments) < 2:
        return 0

    amplitudes = [(s.max() - s.min()) / s.min() * 100 if s.min() > 0 else np.nan
                  for s in segments]
    amplitudes = [a for a in amplitudes if not np.isnan(a)]

    contractions = sum(1 for i in range(1, len(amplitudes))
                       if amplitudes[i] < amplitudes[i - 1])
    return contractions


# ── RS Rating（百分位排名） ────────────────────────────────────────────────
def compute_rs_raw(close: pd.Series, period: int) -> float:
    """計算單一股票的原始 RS 值（相對於本身歷史，用於後續全市場排名）。"""
    actual_period = min(len(close), period)
    return (close.iloc[-1] / close.iloc[-actual_period]) - 1


def percentile_rank(value: float, all_values: list) -> float:
    """將個股 RS 值換算為 1–99 百分位。"""
    if len(all_values) < 2:
        return 50.0
    arr = np.array(all_values, dtype=float)
    rank = np.sum(arr < value) / len(arr) * 98 + 1  # 線性映射到 1–99
    return float(np.clip(rank, 1, 99))


# ── 單股分析 ───────────────────────────────────────────────────────────────
def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame,
            all_rs_raw: Optional[list] = None) -> Optional[VCPResult]:
    try:
        df = df.copy()
        c = df["Close"]
        data_len = len(df)

        # 均線
        for p in [10, 20, 50, 150, 200]:
            df[f"MA{p}"] = c.rolling(window=p).mean() if data_len >= p else np.nan

        r   = df.iloc[-1]
        p_val = float(r["Close"])

        def ma(n):
            val = r.get(f"MA{n}")
            return float(val) if val is not None and not pd.isna(val) else None

        ma10, ma20, ma50, ma150, ma200 = ma(10), ma(20), ma(50), ma(150), ma(200)

        # J Law META 排列：Price > MA10 > MA20 > MA50 > MA200
        meta = (
            all(v is not None for v in [ma10, ma20, ma50, ma200])
            and p_val > ma10 > ma20 > ma50 > ma200
        )

        # 升勢幅度
        uptrend = 0.0
        if data_len > CONFIG["uptrend_lookback"]:
            base_high   = c.tail(CONFIG["uptrend_lookback"]).max()
            pre_slice   = c.iloc[max(0, data_len - CONFIG["uptrend_base_lookback"]):
                                  max(1, data_len - CONFIG["uptrend_lookback"])]
            pre_base_low = pre_slice.min() if len(pre_slice) > 0 else 0
            if pre_base_low > 0:
                uptrend = (base_high - pre_base_low) / pre_base_low * 100

        # RS Rating（全市場百分位，若無全量資料則回退至相對 SPY 估算）
        stock_rs_raw = compute_rs_raw(c, CONFIG["rs_period"])
        if all_rs_raw and len(all_rs_raw) >= 10:
            rs_val = percentile_rank(stock_rs_raw, all_rs_raw)
        else:
            spy_period   = min(len(spy), CONFIG["rs_period"])
            spy_rs_raw   = (float(spy["Close"].iloc[-1]) /
                            float(spy["Close"].iloc[-spy_period])) - 1
            # 粗估：以 SPY 為中位線 (50)，±20% 差距對應 ±25 分
            rs_val = float(np.clip(50 + (stock_rs_raw - spy_rs_raw) * 125, 1, 99))

        # VCP 收縮計數
        num_contractions = count_vcp_contractions(c, window=CONFIG["contraction_window"])

        # 淘汰條件
        dq = []
        if ma200 is None or p_val < ma200:
            dq.append("Below MA200")
        if uptrend < CONFIG["min_uptrend_pct"]:
            dq.append(f"Uptrend < {CONFIG['min_uptrend_pct']}%")
        if num_contractions < CONFIG["min_contraction_count"]:
            dq.append(f"Contractions < {CONFIG['min_contraction_count']}")

        # 評分（各項最多 20 分，滿分 100）
        base_score   = 60 if not dq else 30
        meta_score   = 20 if meta else 0
        rs_score     = float(np.clip((rs_val - 50) / 50 * 20, -10, 20))  # RS>50 加分，<50 扣分上限 -10
        score        = base_score + meta_score + rs_score
        grade        = "A" if score >= 80 else ("B" if score >= 65 else "C")

        # Pivot / Stop / 動態 reward_ratio
        pivot = round(float(df["High"].tail(30).max()), 2)
        stop  = round(float(df["Low"].tail(15).min()), 2)
        risk  = p_val - stop
        reward_ratio = round((pivot - p_val) / risk, 2) if risk > 0 else 0.0

        risk_pct = round((p_val - (ma20 or p_val)) / p_val * 100, 2) if ma20 and p_val > 0 else 0.0

        return VCPResult(
            ticker=ticker, company=ticker,
            score=round(score, 1), grade=grade,
            price=round(p_val, 2), pivot=pivot, stop=stop,
            trailing_stop_20ma=round(ma20, 2) if ma20 else 0.0,
            risk_pct=risk_pct, reward_ratio=reward_ratio,
            jlaw_meta_aligned=meta, prior_uptrend_pct=round(uptrend, 1),
            num_contractions=num_contractions,
            rs_rating=round(rs_val, 1),
            disqualified=len(dq) > 0, dq_reasons=dq,
        )

    except Exception as e:
        log.warning(f"{ticker} analyze error: {e}")
        return None


# ── Email 發送（含重試） ───────────────────────────────────────────────────
def send_email(results: list) -> None:
    sender    = os.environ.get("EMAIL_SENDER")
    pwd       = os.environ.get("EMAIL_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT")
    if not all([sender, pwd, recipient]):
        log.warning("郵件環境變數缺失，跳過發送。")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    rows = "".join(
        f"<tr>"
        f"<td><b>{r.ticker}</b></td>"
        f"<td>{r.grade}</td>"
        f"<td>${r.price:.2f}</td>"
        f"<td>${r.pivot:.2f}</td>"
        f"<td>${r.stop:.2f}</td>"
        f"<td>${r.trailing_stop_20ma:.2f}</td>"
        f"<td>{r.rs_rating:.0f}</td>"
        f"<td>{r.num_contractions}</td>"
        f"<td>{'✅' if r.jlaw_meta_aligned else '-'}</td>"
        f"<td>{r.reward_ratio:.2f}</td>"
        f"</tr>"
        for r in sorted(results, key=lambda x: x.score, reverse=True)
    )

    html = f"""
    <h3>📈 VCP x J Law META 掃描報告 ({today})</h3>
    <table border='1' style='border-collapse:collapse;width:100%;text-align:center;font-size:13px;'>
      <tr style='background:#f2f2f2;'>
        <th>Ticker</th><th>Grade</th><th>Price</th><th>Pivot</th>
        <th>Stop</th><th>20MA 防守</th><th>RS</th>
        <th>收縮數</th><th>META</th><th>R/R</th>
      </tr>
      {rows}
    </table>
    """

    msg = MIMEMultipart()
    msg["Subject"] = f"🚀 VCP Alert: {len(results)} Setups ({today})"
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(html, "html"))

    for attempt in range(1, CONFIG["email_retries"] + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(sender, pwd)
                s.sendmail(sender, recipient, msg.as_string())
            log.info("✅ 郵件已發送")
            return
        except Exception as e:
            log.error(f"郵件發送失敗 (第 {attempt} 次): {e}")
            if attempt < CONFIG["email_retries"]:
                time.sleep(CONFIG["email_retry_delay"])

    log.error("郵件多次重試後仍失敗，放棄。")


# ── 主程式 ─────────────────────────────────────────────────────────────────
def main() -> None:
    spy_df = fetch("SPY")
    if spy_df is None:
        log.error("無法獲取 SPY 數據，中止。")
        return

    tickers = get_tickers()
    log.info(f"開始掃描 {len(tickers)} 檔股票...")

    # Phase 1：平行下載所有數據
    raw_data: dict[str, pd.DataFrame] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as ex:
        futures = {ex.submit(fetch, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            t  = futures[fut]
            df = fut.result()
            if df is not None:
                raw_data[t] = df

    log.info(f"成功下載 {len(raw_data)} 檔，開始計算 RS 百分位...")

    # Phase 2：預先計算全市場 RS raw，供百分位排名
    rs_period = CONFIG["rs_period"]
    all_rs_raw = []
    for t, df in raw_data.items():
        try:
            all_rs_raw.append(compute_rs_raw(df["Close"], rs_period))
        except Exception:
            pass

    # Phase 3：逐股分析（傳入全市場 RS 列表）
    qualifying: list[VCPResult] = []
    spy_copy = spy_df.copy()  # 執行緒安全：各 analyze 呼叫共享唯讀副本即可

    for ticker, df in raw_data.items():
        result = analyze(ticker, df, spy_copy, all_rs_raw)
        if result and not result.disqualified and result.score >= CONFIG["score_threshold"]:
            qualifying.append(result)

    # 輸出結果
    out_path = "vcp_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([vars(r) for r in qualifying], f,
                  indent=2, ensure_ascii=False, cls=NpEncoder)
    log.info(f"結果已儲存至 {out_path}")

    if qualifying:
        send_email(qualifying)

    log.info(f"✅ 掃描完成。合格標的: {len(qualifying)} / {len(raw_data)}")


if __name__ == "__main__":
    main()
