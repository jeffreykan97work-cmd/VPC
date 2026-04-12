import os, json, smtplib, logging, time, random
import requests, concurrent.futures
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
from tvDatafeed import TvDatafeed, Interval

# ── 全域設定 ───────────────────────────────────────────────────────────────
CONFIG = {
    "min_data_bars":        150,
    "rs_period":            252,
    "uptrend_lookback":     120,
    "uptrend_base_lookback":200,
    "min_uptrend_pct":      25,
    "contraction_window":   5,
    "min_contraction_count":2,
    "score_threshold":      65,
    "max_workers":          2,
    "fetch_sleep_range":    (2.0, 4.0),

    # ── 郵件設定（可選，留空則跳過）──
    "email_sender":   os.getenv("EMAIL_SENDER", ""),
    "email_password": os.getenv("EMAIL_PASSWORD", ""),
    "email_receiver": os.getenv("EMAIL_RECEIVER", ""),
    "smtp_host":      "smtp.gmail.com",
    "smtp_port":      587,
}

tv = TvDatafeed()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ── Bug Fix #1: NpEncoder 原本被引用但從未定義 ────────────────────────────
class NpEncoder(json.JSONEncoder):
    """將 numpy 型別序列化為 JSON 相容格式"""
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, bool):          # bool 必須在 int 之前檢查
            return bool(obj)
        return super().default(obj)


@dataclass
class VCPResult:
    ticker:              str
    company:             str
    score:               float
    grade:               str
    price:               float
    pivot:               float
    stop:                float
    trailing_stop_20ma:  float
    risk_pct:            float
    reward_ratio:        float
    jlaw_meta_aligned:   bool
    prior_uptrend_pct:   float
    num_contractions:    int
    rs_rating:           float
    disqualified:        bool
    dq_reasons:          list = field(default_factory=list)


# ── 數據抓取 ──────────────────────────────────────────────────────────────
def fetch(ticker: str) -> Optional[pd.DataFrame]:
    """從 TradingView 抓取日線數據"""
    for exchange in ["NASDAQ", "NYSE", "AMEX"]:
        try:
            time.sleep(random.uniform(*CONFIG["fetch_sleep_range"]))
            df = tv.get_hist(
                symbol=ticker,
                exchange=exchange,
                interval=Interval.in_daily,
                n_bars=300,
            )
            if df is not None and not df.empty:
                df.columns = [c.capitalize() for c in df.columns]
                df = df.dropna(subset=["Close"])

                # Bug Fix #2: min_data_bars 在 CONFIG 中定義但從未檢查
                if len(df) < CONFIG["min_data_bars"]:
                    log.debug(f"{ticker}@{exchange}: 數據不足（{len(df)} < {CONFIG['min_data_bars']}），略過")
                    continue

                return df

        # Bug Fix #3: 裸 except 會吞掉 KeyboardInterrupt 等系統例外
        except Exception as e:
            log.debug(f"{ticker}@{exchange} 取得失敗：{e}")
            continue

    log.warning(f"{ticker}: 所有交易所均無法取得數據")
    return None


# ── VCP 核心邏輯 ──────────────────────────────────────────────────────────
def count_vcp_contractions(close: pd.Series, window: int = 5) -> int:
    n = len(close)
    if n < window * 3:
        return 0
    segment_data = close.iloc[-90:].values
    segments = [
        segment_data[i : i + window]
        for i in range(0, len(segment_data) - window + 1, window)
    ]
    if len(segments) < 2:
        return 0
    amps = [
        (s.max() - s.min()) / s.min() * 100
        for s in segments
        if s.min() > 0
    ]
    if len(amps) < 2:
        return 0
    return sum(1 for i in range(1, len(amps)) if amps[i] < amps[i - 1])


def compute_rs_raw(close: pd.Series, period: int) -> float:
    p = min(len(close), period)
    if p < 2:
        return 0.0
    base = close.iloc[-p]
    if base == 0:
        return 0.0
    return (close.iloc[-1] / base) - 1


def percentile_rank(value: float, all_values: list) -> float:
    if not all_values:
        return 50.0
    arr = np.array(all_values)
    rank = np.sum(arr < value) / len(arr) * 98 + 1
    return float(np.clip(rank, 1, 99))


# Bug Fix #4: prior_uptrend_pct 原本硬編碼為 30.0；uptrend_lookback 設定從未使用
def compute_prior_uptrend(close: pd.Series) -> float:
    """計算過去 uptrend_lookback 根 K 棒相對於 uptrend_base_lookback 起點的漲幅"""
    lb  = CONFIG["uptrend_lookback"]
    blb = CONFIG["uptrend_base_lookback"]
    if len(close) < blb:
        return 0.0
    base  = close.iloc[-blb]
    peak  = close.iloc[-lb : -1].max() if lb > 1 else close.iloc[-1]
    if base <= 0:
        return 0.0
    return round((peak / base - 1) * 100, 2)


# ── 核心分析函數 ───────────────────────────────────────────────────────────
def analyze(
    ticker: str,
    df: pd.DataFrame,
    spy: pd.DataFrame,          # Bug Fix #5: 原本傳入但完全未使用
    all_rs_raw: list,
) -> Optional[VCPResult]:
    try:
        df = df.copy()
        c  = df["Close"]

        for p in [10, 20, 50, 150, 200]:
            df[f"MA{p}"] = c.rolling(window=p).mean()

        r    = df.iloc[-1]
        p_val = float(r["Close"])

        # Bug Fix #6: ma20 可能為 NaN，後續計算 risk_pct 會產生 NaN
        ma10  = r["MA10"]
        ma20  = r["MA20"]
        ma50  = r["MA50"]
        ma150 = r["MA150"]
        ma200 = r["MA200"]

        has_ma200 = not pd.isna(ma200)
        has_ma20  = not pd.isna(ma20)

        # META 排列
        meta = (
            not pd.isna(ma10)
            and not pd.isna(ma20)
            and not pd.isna(ma50)
            and has_ma200
            and p_val > ma10 > ma20 > ma50 > ma200
        )

        # RS Rating
        raw_rs = compute_rs_raw(c, CONFIG["rs_period"])
        rs_val = percentile_rank(raw_rs, all_rs_raw)

        # Bug Fix #5 (cont.): 計算相對 SPY 強度
        spy_rs_raw = compute_rs_raw(spy["Close"], CONFIG["rs_period"])
        relative_strength_vs_spy = raw_rs - spy_rs_raw

        # VCP 收縮次數
        contractions = count_vcp_contractions(c, CONFIG["contraction_window"])

        # Prior uptrend
        prior_uptrend = compute_prior_uptrend(c)

        # ── 淘汰邏輯 ──
        dq = []
        if not has_ma200 or p_val < ma200:
            dq.append("Below MA200")
        if not has_ma20 or p_val < ma20:
            dq.append("Below MA20")
        if contractions < CONFIG["min_contraction_count"]:
            dq.append(f"Low Contractions ({contractions})")

        # Bug Fix #7: min_uptrend_pct 設定從未使用
        if prior_uptrend < CONFIG["min_uptrend_pct"]:
            dq.append(f"Weak Prior Uptrend ({prior_uptrend:.1f}%)")

        # ── 評分 ──
        base_score   = 60 if not dq else 30
        meta_bonus   = 20 if meta else 0
        rs_bonus     = (rs_val - 50) / 50 * 20
        spy_bonus    = min(max(relative_strength_vs_spy * 100, -10), 10)
        score        = base_score + meta_bonus + rs_bonus + spy_bonus

        # Bug Fix #8: reward_ratio 原本硬編碼為 1.5；應由 pivot / stop 計算
        pivot     = round(df["High"].tail(30).max(), 2)
        stop      = round(df["Low"].tail(15).min(), 2)
        risk_amt  = p_val - stop
        reward    = pivot - p_val
        reward_ratio = round(reward / risk_amt, 2) if risk_amt > 0 else 0.0

        # Bug Fix #6 (cont.): risk_pct 需防範 NaN 及除以零
        risk_pct = round((p_val - ma20) / p_val * 100, 2) if has_ma20 and p_val > 0 else 0.0

        grade = "A" if score >= 80 else ("B" if score >= 65 else "C")

        return VCPResult(
            ticker=ticker,
            company=ticker,
            score=round(score, 1),
            grade=grade,
            price=p_val,
            pivot=pivot,
            stop=stop,
            trailing_stop_20ma=round(ma20, 2) if has_ma20 else 0.0,
            risk_pct=risk_pct,
            reward_ratio=reward_ratio,
            jlaw_meta_aligned=meta,
            prior_uptrend_pct=prior_uptrend,
            num_contractions=contractions,
            rs_rating=round(rs_val, 1),
            disqualified=len(dq) > 0,
            dq_reasons=dq,
        )

    except Exception as e:
        log.warning(f"{ticker} 分析失敗：{e}", exc_info=True)
        return None


# ── 輸出 ───────────────────────────────────────────────────────────────────
def results_to_df(results: list[VCPResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append({
            "Ticker":          r.ticker,
            "Score":           r.score,
            "Grade":           r.grade,
            "Price":           r.price,
            "Pivot":           r.pivot,
            "Stop":            r.stop,
            "Risk%":           r.risk_pct,
            "Reward:Risk":     r.reward_ratio,
            "RS Rating":       r.rs_rating,
            "Contractions":    r.num_contractions,
            "Prior Uptrend%":  r.prior_uptrend_pct,
            "META Aligned":    r.jlaw_meta_aligned,
            "Disqualified":    r.disqualified,
            "DQ Reasons":      "; ".join(r.dq_reasons),
        })
    return pd.DataFrame(rows).sort_values("Score", ascending=False)


# Bug Fix #9: 郵件功能被 import 但從未實作
def send_email(subject: str, html_body: str) -> None:
    sender   = CONFIG["email_sender"]
    receiver = CONFIG["email_receiver"]
    password = CONFIG["email_password"]

    if not all([sender, receiver, password]):
        log.info("郵件設定未完整，略過發送。")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = receiver
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as s:
            s.ehlo()
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, receiver, msg.as_string())
        log.info(f"✉️  郵件已發送至 {receiver}")
    except Exception as e:
        log.error(f"郵件發送失敗：{e}")


def build_email_html(df: pd.DataFrame) -> str:
    table_html = df.to_html(index=False, border=1, justify="center")
    return f"""
    <html><body>
    <h2>VCP 掃描報告 – {datetime.now().strftime('%Y-%m-%d %H:%M')}</h2>
    {table_html}
    </body></html>
    """


# ── 主程式 ─────────────────────────────────────────────────────────────────
def get_sp500_tickers() -> list[str]:
    """從 Wikipedia 抓取 S&P 500 成分股清單"""
    try:
        url   = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        table = pd.read_html(url)[0]
        tickers = table["Symbol"].str.replace(".", "-", regex=False).tolist()
        log.info(f"✅ 取得 {len(tickers)} 個 S&P 500 標的")
        return tickers
    except Exception as e:
        log.warning(f"無法抓取 S&P 500 清單：{e}，改用預設清單")
        return ["AAPL", "NVDA", "TSLA", "MSFT", "GOOGL", "META", "AMZN", "PLTR", "MSTR"]


def main():
    spy_df = fetch("SPY")
    if spy_df is None:
        log.error("無法取得 SPY 數據，程式終止。")
        return

    tickers = get_sp500_tickers()

    # 並行抓取
    raw_data: dict[str, pd.DataFrame] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as executor:
        future_to_ticker = {executor.submit(fetch, t): t for t in tickers}
        for future in concurrent.futures.as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                df = future.result()
                if df is not None:
                    raw_data[ticker] = df
            except Exception as e:
                log.warning(f"{ticker} 執行失敗：{e}")

    if not raw_data:
        log.error("沒有成功取得任何數據，程式終止。")
        return

    all_rs_raw = [
        compute_rs_raw(df["Close"], CONFIG["rs_period"])
        for df in raw_data.values()
    ]

    # 分析所有標的
    all_results: list[VCPResult] = []
    qualifying:  list[VCPResult] = []

    for t, df in raw_data.items():
        res = analyze(t, df, spy_df, all_rs_raw)
        if res is None:
            continue
        all_results.append(res)
        if not res.disqualified and res.score >= CONFIG["score_threshold"]:
            qualifying.append(res)

    log.info(f"✅ 掃描完成：共 {len(all_results)} 個標的分析完畢，{len(qualifying)} 個符合條件。")

    # 輸出結果
    if qualifying:
        df_out = results_to_df(qualifying)
        print("\n" + "=" * 80)
        print(df_out.to_string(index=False))
        print("=" * 80 + "\n")

        # 存為 CSV
        out_path = f"vcp_results_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df_out.to_csv(out_path, index=False)
        log.info(f"📄 結果已儲存至 {out_path}")

        # 存為 JSON（使用修復後的 NpEncoder）
        json_path = out_path.replace(".csv", ".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                [r.__dict__ for r in qualifying],
                f,
                cls=NpEncoder,
                ensure_ascii=False,
                indent=2,
            )

        # 發送郵件
        send_email(
            subject=f"VCP 掃描結果 {datetime.now().strftime('%Y-%m-%d')} – {len(qualifying)} 個標的",
            html_body=build_email_html(df_out),
        )
    else:
        log.info("本次掃描沒有符合條件的標的。")


if __name__ == "__main__":
    main()
