#!/usr/bin/env python3
"""
VCP Scanner — Mark Minervini 自動選股系統
基於《Trade Like a Stock Market Wizard》的 VCP 方法論
"""

import os
import json
import logging
import smtplib
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from io import StringIO
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── 日誌設定 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("vcp_scan.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── 評分常數 ──────────────────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "vcp_quality":    30,   # VCP 收縮品質
    "trend_template": 25,   # MM 趨勢模板（8 項）
    "stage":          15,   # Stage 2 確認
    "volume_pattern": 15,   # 量能模式
    "rs":             10,   # 相對強度 vs S&P 500
    "tightness":       5,   # 最後整理緊縮度
}
GRADE_MAP = [(85, "A+"), (75, "A"), (65, "B"), (50, "C")]


def score_to_grade(score: float) -> str:
    for threshold, grade in GRADE_MAP:
        if score >= threshold:
            return grade
    return "D"


# ── 資料取得 ──────────────────────────────────────────────────────────────────

class DataFetcher:
    HEADERS = {"User-Agent": "Mozilla/5.0"}

    @staticmethod
    def get_sp500_tickers() -> list[str]:
        """取得 S&P 500 成分股清單（Wikipedia）"""
        try:
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            resp = requests.get(url, headers=DataFetcher.HEADERS, timeout=10)
            resp.raise_for_status()
            df = pd.read_html(StringIO(resp.text))[0]
            tickers = [t.replace(".", "-") for t in df["Symbol"].tolist()]
            logger.info(f"成功取得 {len(tickers)} 隻 S&P 500 成分股")
            return tickers
        except Exception as e:
            logger.warning(f"無法取得 S&P 500 清單，使用備用清單：{e}")
            return [
                "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
                "META", "TSLA", "AMD", "NFLX", "AVGO",
            ]

    @staticmethod
    def download(ticker: str, period: str = "1y") -> pd.DataFrame | None:
        """下載股價資料，失敗時記錄具體錯誤"""
        try:
            df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
            if df.empty or len(df) < 200:
                return None
            # yfinance 新版回傳 MultiIndex columns，壓平成單層避免 FutureWarning
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        except Exception as e:
            logger.debug(f"{ticker} 下載失敗：{e}")
            return None

    @staticmethod
    def download_benchmark(period: str = "1y") -> pd.DataFrame | None:
        """下載 SPY 作為大盤基準"""
        return DataFetcher.download("SPY", period)


# ── 核心分析 ──────────────────────────────────────────────────────────────────

class TrendAnalyzer:

    @staticmethod
    def check_trend_template(df: pd.DataFrame) -> tuple[bool, int]:
        """
        MM 趨勢模板 — 8 項條件，全部通過才進入評分。
        回傳 (是否通過, 通過項目數)，供評分參考。
        """
        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]

        c      = float(close.iloc[-1])
        ma50   = float(close.rolling(50).mean().iloc[-1])
        ma150  = float(close.rolling(150).mean().iloc[-1])
        ma200  = float(close.rolling(200).mean().iloc[-1])

        # 200 日均線是否在過去 20 個交易日內上升
        ma200_20d_ago = float(close.rolling(200).mean().iloc[-21])
        ma200_rising  = ma200 > ma200_20d_ago

        low_52w  = float(low.rolling(252).min().iloc[-1])
        high_52w = float(high.rolling(252).max().iloc[-1])

        conditions = [
            c > ma150,                    # 1. 股價 > 150MA
            c > ma200,                    # 2. 股價 > 200MA
            ma150 > ma200,                # 3. 150MA > 200MA
            ma200_rising,                 # 4. 200MA 上升趨勢
            ma50 > ma150,                 # 5a. 50MA > 150MA
            ma50 > ma200,                 # 5b. 50MA > 200MA
            c > ma50,                     # 6. 股價 > 50MA
            c >= high_52w * 0.75,         # 7. 距 52 週高點 ≤25%
            c >= low_52w * 1.30,          # 8. 超越 52 週低點 ≥30%
        ]
        passed = sum(conditions)
        return all(conditions), passed


class VCPAnalyzer:

    @staticmethod
    def detect_contractions(df: pd.DataFrame) -> dict:
        """
        識別 VCP 的多次波動收縮。
        策略：在過去 6 個月中，以區間高低點識別每段整理的幅度，
        判斷幅度是否遞減。
        """
        prices = df["Close"].iloc[-126:]   # 約 6 個月
        volumes = df["Volume"].iloc[-126:]

        # 以 10 日滾動視窗切分「波段」
        window = 10
        drawdowns = []
        vol_ratios = []
        baseline_vol = float(volumes.mean())

        for start in range(0, len(prices) - window * 2, window):
            seg = prices.iloc[start: start + window]
            seg_vol = volumes.iloc[start: start + window]
            hi = float(seg.max())
            lo = float(seg.min())
            if hi > 0:
                drawdown = (hi - lo) / hi
                drawdowns.append(drawdown)
                vol_ratios.append(float(seg_vol.mean()) / baseline_vol if baseline_vol > 0 else 1.0)

        if len(drawdowns) < 2:
            return {"contractions": 0, "decreasing": False, "vol_decreasing": False, "last_drawdown": 0.0}

        # 判斷幅度是否整體遞減（用線性回歸斜率）
        x = np.arange(len(drawdowns))
        slope_dd  = float(np.polyfit(x, drawdowns, 1)[0])
        slope_vol = float(np.polyfit(x, vol_ratios, 1)[0])

        # 計算收縮次數（相鄰幅度遞減即計 +1）
        contractions = sum(
            1 for i in range(1, len(drawdowns)) if drawdowns[i] < drawdowns[i - 1]
        )

        return {
            "contractions":   contractions,
            "decreasing":     slope_dd < 0,     # 幅度整體遞減
            "vol_decreasing": slope_vol < 0,    # 量能整體遞減
            "last_drawdown":  drawdowns[-1],    # 最後一段收縮幅度
        }

    @staticmethod
    def calc_pivot(df: pd.DataFrame) -> float:
        """簡易樞軸價：最近整理高點（過去 10 日最高）"""
        return float(df["High"].iloc[-10:].max())


# ── 評分引擎 ──────────────────────────────────────────────────────────────────

class ScoringEngine:

    def __init__(self, spy_df: pd.DataFrame | None):
        self.spy_df = spy_df

    def score_vcp_quality(self, info: dict) -> float:
        """VCP 收縮品質（滿分 30）"""
        score = 0.0
        n = info["contractions"]
        # 理想收縮次數 2–4
        if   n >= 3: score += 15
        elif n == 2: score += 10
        elif n == 1: score += 5

        if info["decreasing"]:     score += 10   # 幅度遞減
        if info["vol_decreasing"]: score +=  5   # 量縮遞減
        return min(score, 30)

    def score_trend_template(self, passed: int, total: int = 9) -> float:
        """MM 趨勢模板（滿分 25，線性比例）"""
        return round(25 * passed / total, 1)

    def score_stage(self, df: pd.DataFrame) -> float:
        """
        Stage 2 判斷（滿分 15）：
        - 股價在 30/10/40 週均線之上且均線多頭排列
        """
        close = df["Close"]
        ma10w  = close.rolling(50).mean().iloc[-1]   # ≈10 週
        ma30w  = close.rolling(150).mean().iloc[-1]  # ≈30 週
        ma40w  = close.rolling(200).mean().iloc[-1]  # ≈40 週
        c = close.iloc[-1]

        if c > ma10w and ma10w > ma30w and ma30w > ma40w:
            return 15.0   # 典型 Stage 2
        elif c > ma30w:
            return 8.0    # 部分符合
        return 0.0

    def score_volume_pattern(self, df: pd.DataFrame) -> float:
        """
        量能模式（滿分 15）：
        - 基部整體量縮（收縮期均量 < 長期均量）
        - 最近一日量是否出現異常放量（可能突破前兆）
        """
        score = 0.0
        avg_vol_long   = float(df["Volume"].rolling(50).mean().iloc[-1])
        avg_vol_recent = float(df["Volume"].rolling(10).mean().iloc[-1])

        if avg_vol_recent < avg_vol_long:
            score += 10   # 基部量縮

        last_vol = float(df["Volume"].iloc[-1])
        if last_vol > avg_vol_long * 1.5:
            score += 5    # 潛在放量突破訊號

        return min(score, 15)

    def score_rs(self, df: pd.DataFrame) -> float:
        """
        相對強度 vs S&P 500（滿分 10）：
        比較過去 3 個月報酬率相對於 SPY
        """
        if self.spy_df is None or len(df) < 63:
            return 5.0    # 無基準時給平均分

        stock_ret = float(df["Close"].iloc[-1] / df["Close"].iloc[-63] - 1)

        # 對齊日期
        spy = self.spy_df["Close"].reindex(df.index, method="ffill")
        if len(spy) < 63:
            return 5.0
        spy_ret = float(spy.iloc[-1] / spy.iloc[-63] - 1)

        if   stock_ret > spy_ret + 0.10: return 10.0
        elif stock_ret > spy_ret + 0.05: return  8.0
        elif stock_ret > spy_ret:        return  5.0
        else:                            return  0.0

    def score_tightness(self, info: dict) -> float:
        """最後整理緊縮度（滿分 5）"""
        last_dd = info.get("last_drawdown", 1.0)
        if   last_dd < 0.04: return 5.0
        elif last_dd < 0.06: return 4.0
        elif last_dd < 0.08: return 3.0
        elif last_dd < 0.12: return 2.0
        return 0.0


# ── 掃描主體 ──────────────────────────────────────────────────────────────────

class VCPScanner:

    def __init__(self, min_score: int = 50):
        self.min_score = min_score
        self.results: list[dict] = []

    def analyse_ticker(self, ticker: str, spy_df: pd.DataFrame | None) -> dict | None:
        df = DataFetcher.download(ticker)
        if df is None:
            return None

        # ── 趨勢模板前置篩選 ─────────────────────────────
        passed_template, passed_count = TrendAnalyzer.check_trend_template(df)
        if not passed_template:
            return None   # 不符合趨勢模板，直接略過

        # ── VCP 形態分析 ──────────────────────────────────
        vcp_info = VCPAnalyzer.detect_contractions(df)
        pivot    = VCPAnalyzer.calc_pivot(df)

        # ── 評分 ──────────────────────────────────────────
        engine = ScoringEngine(spy_df)
        scores = {
            "vcp_quality":    engine.score_vcp_quality(vcp_info),
            "trend_template": engine.score_trend_template(passed_count),
            "stage":          engine.score_stage(df),
            "volume_pattern": engine.score_volume_pattern(df),
            "rs":             engine.score_rs(df),
            "tightness":      engine.score_tightness(vcp_info),
        }
        total = round(sum(scores.values()), 1)

        avg_vol   = float(df["Volume"].rolling(20).mean().iloc[-1])
        last_vol  = float(df["Volume"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])

        return {
            "ticker":         ticker,
            "score":          total,
            "grade":          score_to_grade(total),
            "price":          round(last_close, 2),
            "pivot":          round(pivot, 2),
            "stop_loss":      round(last_close * 0.92, 2),   # 簡易停損（-8%）
            "contractions":   vcp_info["contractions"],
            "last_drawdown":  f"{round(vcp_info['last_drawdown']*100, 1)}%",
            "vol_ratio":      round(last_vol / avg_vol, 2) if avg_vol > 0 else None,
            "trend_passed":   f"{passed_count}/9",
            "score_breakdown": scores,
            "scanned_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def run(self) -> list[dict]:
        tickers = DataFetcher.get_sp500_tickers()
        spy_df  = DataFetcher.download_benchmark()
        if spy_df is None:
            logger.warning("無法下載 SPY 基準，RS 評分將使用預設值")

        logger.info(f"開始掃描 {len(tickers)} 隻股票（門檻：{self.min_score} 分）")
        for i, ticker in enumerate(tickers):
            try:
                result = self.analyse_ticker(ticker, spy_df)
                if result and result["score"] >= self.min_score:
                    self.results.append(result)
                    logger.info(f"  ✅ {ticker} | 分數：{result['score']} | 等級：{result['grade']}")
            except Exception as e:
                logger.debug(f"{ticker} 分析例外：{e}")

            if (i + 1) % 50 == 0:
                logger.info(f"進度：{i+1}/{len(tickers)}，目前命中：{len(self.results)} 隻")
            time.sleep(0.1)

        self.results.sort(key=lambda x: x["score"], reverse=True)
        logger.info(f"掃描完成，共 {len(self.results)} 隻符合條件")
        return self.results


# ── Email 通知 ────────────────────────────────────────────────────────────────

class EmailNotifier:

    def __init__(self):
        self.sender    = os.environ["EMAIL_SENDER"]
        self.password  = os.environ["EMAIL_PASSWORD"]
        self.recipient = os.environ["EMAIL_RECIPIENT"]

    def _build_html(self, results: list[dict]) -> str:
        date_str = datetime.now().strftime("%Y-%m-%d")
        rows = ""
        for r in results:
            bd = r["score_breakdown"]
            rows += f"""
            <tr>
              <td><strong>{r['ticker']}</strong></td>
              <td style="color:{'#27ae60' if r['grade']=='A+' else '#2980b9'}">{r['grade']}</td>
              <td>{r['score']}</td>
              <td>${r['price']}</td>
              <td>${r['pivot']}</td>
              <td>${r['stop_loss']}</td>
              <td>{r['contractions']}</td>
              <td>{r['last_drawdown']}</td>
              <td>{r['vol_ratio']}</td>
              <td>{r['trend_passed']}</td>
            </tr>"""

        return f"""
        <html><body style="font-family:Arial,sans-serif">
        <h2>📈 VCP Scanner 每日報告 — {date_str}</h2>
        <p>共篩選出 <strong>{len(results)}</strong> 隻符合條件股票</p>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
          <thead style="background:#2c3e50;color:white">
            <tr>
              <th>股票</th><th>等級</th><th>分數</th><th>現價</th>
              <th>樞軸</th><th>停損</th><th>收縮次數</th>
              <th>末段收縮</th><th>量比</th><th>趨勢模板</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <hr>
        <p style="color:gray;font-size:12px">
          ⚠️ 本報告僅供學習研究，不構成投資建議。
        </p>
        </body></html>"""

    def send(self, results: list[dict]) -> None:
        if not results:
            logger.info("無符合條件股票，不發送 Email")
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📈 VCP Scanner — {datetime.now().strftime('%Y-%m-%d')} ({len(results)} 隻)"
        msg["From"]    = self.sender
        msg["To"]      = self.recipient
        msg.attach(MIMEText(self._build_html(results), "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(self.sender, self.password)
            server.sendmail(self.sender, self.recipient, msg.as_string())
        logger.info(f"Email 已寄出至 {self.recipient}")


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    min_score = int(os.getenv("MIN_SCORE", "50"))
    scanner   = VCPScanner(min_score=min_score)
    results   = scanner.run()

    # 儲存 JSON
    out_path = f"results/vcp_{datetime.now().strftime('%Y-%m-%d')}.json"
    os.makedirs("results", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    with open("vcp_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info(f"結果已儲存：{out_path}")

    # 發送 Email（環境變數存在時才發送）
    if all(os.getenv(k) for k in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECIPIENT")):
        EmailNotifier().send(results)
    else:
        logger.info("未設定 Email 環境變數，略過寄信")


if __name__ == "__main__":
    main()
