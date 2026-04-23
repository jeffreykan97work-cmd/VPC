 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/vcp_scanner.py b/vcp_scanner.py
index aa80305b8845eec3c672fe019708436fa2da7115..e5100c627f5df91bf057917c70533cf61c425dce 100644
--- a/vcp_scanner.py
+++ b/vcp_scanner.py
@@ -257,193 +257,339 @@ class ScoringEngine:
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
 
 
+class AnalystEngine:
+    """
+    華爾街分析師常用的技術面框架（簡化）：
+    - 趨勢：短中期均線多頭排列
+    - 動能：RSI 在強勢但不過熱區間
+    - 量價：接近/突破 pivot 時具量能支持
+    - 風報比：至少達到 1.8R
+    """
+
+    @staticmethod
+    def calc_rsi(close: pd.Series, period: int = 14) -> float:
+        delta = close.diff()
+        gain = delta.clip(lower=0).rolling(period).mean()
+        loss = (-delta.clip(upper=0)).rolling(period).mean()
+        # loss = 0 代表近期幾乎無下跌，RSI 應偏高而非回到中性
+        rs = gain / loss.replace(0, 1e-10)
+        rsi = 100 - (100 / (1 + rs))
+        if pd.isna(rsi.iloc[-1]):
+            return 50.0
+        return float(rsi.iloc[-1])
+
+    @staticmethod
+    def build_plan(df: pd.DataFrame, pivot: float) -> dict:
+        close = df["Close"]
+        high = df["High"]
+        low = df["Low"]
+        volume = df["Volume"]
+
+        last_close = float(close.iloc[-1])
+        ma20 = float(close.rolling(20).mean().iloc[-1])
+        ma50 = float(close.rolling(50).mean().iloc[-1])
+        rsi = AnalystEngine.calc_rsi(close)
+
+        avg_vol_20 = float(volume.rolling(20).mean().iloc[-1])
+        vol_ratio = float(volume.iloc[-1] / avg_vol_20) if avg_vol_20 > 0 else 1.0
+        support_20d = float(low.iloc[-20:].min())
+        resistance_60d = float(high.iloc[-60:].max())
+
+        # 入場：突破 pivot 後再加 0.5% 緩衝，減少假突破
+        entry = round(max(last_close, pivot * 1.005), 2)
+        # 停損以「較近」者為主，避免風險過寬；同時確保停損低於入場價
+        raw_stop = max(support_20d * 0.995, entry * 0.97)
+        stop_loss = round(min(raw_stop, entry * 0.995), 2)
+        risk = max(entry - stop_loss, 0.01)
+
+        # 目標價優先以壓力區推估，若壓力太近才退回固定 R 倍數
+        raw_take_profit = resistance_60d * 1.02
+        if raw_take_profit <= entry:
+            raw_take_profit = entry + 1.5 * risk
+        take_profit = round(raw_take_profit, 2)
+        risk_reward = round((take_profit - entry) / risk, 2)
+
+        momentum_ok = 50 <= rsi <= 75
+        trend_ok = last_close > ma20 > ma50
+        volume_ok = vol_ratio >= 1.2 or last_close >= pivot
+        recommendation = "BUY" if (momentum_ok and trend_ok and volume_ok and risk_reward >= 1.8) else "WATCH"
+
+        return {
+            "recommendation": recommendation,
+            "entry": entry,
+            "take_profit": take_profit,
+            "stop_loss": stop_loss,
+            "rsi": round(rsi, 1),
+            "risk_reward": risk_reward,
+            "analyst_flags": {
+                "momentum_ok": momentum_ok,
+                "trend_ok": trend_ok,
+                "volume_ok": volume_ok,
+            },
+        }
+
+
 # ── 掃描主體 ──────────────────────────────────────────────────────────────────
 
 class VCPScanner:
 
-    def __init__(self, min_score: int = 50):
+    def __init__(
+        self,
+        min_score: int = 50,
+        trend_min_passed: int = 8,
+        min_contractions: int = 1,
+        only_buy_recommendation: bool = False,
+    ):
         self.min_score = min_score
+        self.trend_min_passed = max(5, min(trend_min_passed, 9))
+        self.min_contractions = max(0, min_contractions)
+        self.only_buy_recommendation = only_buy_recommendation
         self.results: list[dict] = []
+        self.rejections: dict[str, int] = {
+            "data_unavailable": 0,
+            "trend_template": 0,
+            "insufficient_contractions": 0,
+            "score_below_min": 0,
+            "not_buy_signal": 0,
+        }
+        self.near_misses: list[dict] = []
 
     def analyse_ticker(self, ticker: str, spy_df: pd.DataFrame | None) -> dict | None:
         df = DataFetcher.download(ticker)
         if df is None:
+            self.rejections["data_unavailable"] += 1
             return None
 
         # ── 趨勢模板前置篩選 ─────────────────────────────
         passed_template, passed_count = TrendAnalyzer.check_trend_template(df)
-        if not passed_template:
-            return None   # 不符合趨勢模板，直接略過
+        if passed_count < self.trend_min_passed:
+            self.rejections["trend_template"] += 1
+            return None
 
         # ── VCP 形態分析 ──────────────────────────────────
         vcp_info = VCPAnalyzer.detect_contractions(df)
+        if vcp_info["contractions"] < self.min_contractions:
+            self.rejections["insufficient_contractions"] += 1
+            return None
+
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
+        plan = AnalystEngine.build_plan(df, pivot)
 
         avg_vol   = float(df["Volume"].rolling(20).mean().iloc[-1])
         last_vol  = float(df["Volume"].iloc[-1])
         last_close = float(df["Close"].iloc[-1])
 
         return {
             "ticker":         ticker,
             "score":          total,
             "grade":          score_to_grade(total),
+            "trend_template_passed": passed_template,
             "price":          round(last_close, 2),
             "pivot":          round(pivot, 2),
-            "stop_loss":      round(last_close * 0.92, 2),   # 簡易停損（-8%）
+            "stop_loss":      plan["stop_loss"],
             "contractions":   vcp_info["contractions"],
             "last_drawdown":  f"{round(vcp_info['last_drawdown']*100, 1)}%",
             "vol_ratio":      round(last_vol / avg_vol, 2) if avg_vol > 0 else None,
             "trend_passed":   f"{passed_count}/9",
+            "recommendation": plan["recommendation"],
+            "entry":          plan["entry"],
+            "take_profit":    plan["take_profit"],
+            "rsi":            plan["rsi"],
+            "risk_reward":    plan["risk_reward"],
+            "analyst_flags":  plan["analyst_flags"],
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
+                    if self.only_buy_recommendation and result["recommendation"] != "BUY":
+                        self.rejections["not_buy_signal"] += 1
+                        self.near_misses.append(result)
+                        continue
                     self.results.append(result)
                     logger.info(f"  ✅ {ticker} | 分數：{result['score']} | 等級：{result['grade']}")
+                elif result:
+                    self.rejections["score_below_min"] += 1
+                    self.near_misses.append(result)
             except Exception as e:
                 logger.debug(f"{ticker} 分析例外：{e}")
 
             if (i + 1) % 50 == 0:
                 logger.info(f"進度：{i+1}/{len(tickers)}，目前命中：{len(self.results)} 隻")
             time.sleep(0.1)
 
-        self.results.sort(key=lambda x: x["score"], reverse=True)
-        logger.info(f"掃描完成，共 {len(self.results)} 隻符合條件")
+        self.results.sort(
+            key=lambda x: (x["recommendation"] == "BUY", x["score"], x["risk_reward"]),
+            reverse=True,
+        )
+        self.near_misses.sort(key=lambda x: x["score"], reverse=True)
+
+        logger.info(
+            "掃描完成，共 %s 隻符合條件；淘汰統計：%s",
+            len(self.results),
+            self.rejections,
+        )
+        if not self.results and self.near_misses:
+            logger.info(
+                "目前無達標股票，最接近門檻前 5 名：%s",
+                [f"{x['ticker']}({x['score']})" for x in self.near_misses[:5]],
+            )
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
-            bd = r["score_breakdown"]
             rows += f"""
             <tr>
               <td><strong>{r['ticker']}</strong></td>
+              <td style="color:{'#16a085' if r['recommendation']=='BUY' else '#7f8c8d'}"><strong>{r['recommendation']}</strong></td>
               <td style="color:{'#27ae60' if r['grade']=='A+' else '#2980b9'}">{r['grade']}</td>
               <td>{r['score']}</td>
               <td>${r['price']}</td>
               <td>${r['pivot']}</td>
+              <td>${r['entry']}</td>
+              <td>${r['take_profit']}</td>
               <td>${r['stop_loss']}</td>
+              <td>{r['risk_reward']}R</td>
+              <td>{r['rsi']}</td>
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
-              <th>股票</th><th>等級</th><th>分數</th><th>現價</th>
-              <th>樞軸</th><th>停損</th><th>收縮次數</th>
+              <th>股票</th><th>建議</th><th>等級</th><th>分數</th><th>現價</th>
+              <th>樞軸</th><th>入場位</th><th>止賺位</th><th>止損位</th>
+              <th>風報比</th><th>RSI</th><th>收縮次數</th>
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
-    scanner   = VCPScanner(min_score=min_score)
-    results   = scanner.run()
+    trend_min_passed = int(os.getenv("TREND_MIN_PASSED", "8"))
+    min_contractions = int(os.getenv("MIN_CONTRACTIONS", "1"))
+    only_buy_recommendation = os.getenv("ONLY_BUY_RECOMMENDATION", "false").lower() == "true"
+
+    logger.info(
+        "目前設定：MIN_SCORE=%s, TREND_MIN_PASSED=%s/9, MIN_CONTRACTIONS=%s, ONLY_BUY_RECOMMENDATION=%s",
+        min_score,
+        trend_min_passed,
+        min_contractions,
+        only_buy_recommendation,
+    )
+
+    scanner = VCPScanner(
+        min_score=min_score,
+        trend_min_passed=trend_min_passed,
+        min_contractions=min_contractions,
+        only_buy_recommendation=only_buy_recommendation,
+    )
+    results = scanner.run()
 
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
 
EOF
)
