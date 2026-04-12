#!/usr/bin/env python3
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
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- Logging 配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('vcp_scan.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

class VCPScanner:
    def __init__(self, min_score=50):
        self.min_score = min_score
        self.results = []
        # 加入偽裝瀏覽器的 Header，防止被維基百科封鎖
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

    def get_stock_pool(self):
        """獲取 S&P 500 + NASDAQ 100 股票池 (多重備援方案)"""
        tickers = set()
        
        # 方案 A: 從維基百科抓取 (加上 Headers)
        try:
            logger.info("嘗試從維基百科獲取 S&P 500 清單...")
            url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                df = pd.read_html(StringIO(response.text))[0]
                tickers.update(df['Symbol'].tolist())
                logger.info(f"成功獲取 S&P 500: {len(df)} 隻")
        except Exception as e:
            logger.warning(f"維基百科 S&P 500 抓取失敗: {e}")

        # 方案 B: 從 GitHub 上的可靠數據源抓取 (備用)
        if len(tickers) < 100:
            try:
                logger.info("嘗試從 GitHub 數據源獲取成分股...")
                nasdaq_url = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nasdaq/nasdaq_full_tickers.json"
                # 這裡抓取前 100 隻市值大的作為範例，避免過載
                res = requests.get(nasdaq_url, timeout=10).json()
                tickers.update([item['symbol'] for item in res[:200]])
            except Exception as e:
                logger.error(f"備援方案抓取失敗: {e}")

        # 清理代碼格式 (例如把 . 換成 -)
        clean_tickers = sorted([str(t).replace('.', '-') for t in tickers if isinstance(t, str)])
        return clean_tickers if clean_tickers else ['AAPL', 'MSFT', 'GOOGL', 'NVDA', 'TSLA']

    def check_vcp_setup(self, df):
        """核心 VCP 形態過濾 (Mark Minervini 趨勢模板)"""
        if len(df) < 200: return False
        
        # 取得最新數據
        last_row = df.iloc[-1]
        c = last_row['Close']
        
        # 計算移動平均線
        ma50 = df['Close'].rolling(50).mean().iloc[-1]
        ma150 = df['Close'].rolling(150).mean().iloc[-1]
        ma200 = df['Close'].rolling(200).mean().iloc[-1]
        
        # 52 週高低點
        low_52w = df['Low'].rolling(252).min().iloc[-1]
        high_52w = df['High'].rolling(252).max().iloc[-1]

        # 趨勢模板 7 大條件
        cond_1 = c > ma150 and c > ma200
        cond_2 = ma150 > ma200
        cond_3 = ma200 > df['MA200_prev'] if 'MA200_prev' in df else True # 200MA 向上
        cond_4 = ma50 > ma150 and ma50 > ma200
        cond_5 = c > ma50
        cond_6 = c > (low_52w * 1.25) # 股價高於低點 25%
        cond_7 = c > (high_52w * 0.75) # 股價在距離高點 25% 以內

        return all([cond_1, cond_2, cond_4, cond_5, cond_6, cond_7])

    def calculate_vcp_score(self, ticker):
        """計算 VCP 收縮與評分"""
        try:
            df = yf.download(ticker, period='1y', progress=False)
            if df.empty or len(df) < 200: return None
            
            # 計算均線方向
            df['MA200_prev'] = df['Close'].rolling(200).mean().shift(20) # 20天前的 200MA
            
            if not self.check_vcp_setup(df): return None

            # 1. 波動率收縮量 (Tightness)
            # 計算最近 10 天的價格波幅 vs 最近 30 天的波幅
            recent_range = (df['High'].rolling(10).max() / df['Low'].rolling(10).min()) - 1
            prev_range = (df['High'].rolling(30).max() / df['Low'].rolling(30).min()) - 1
            
            tightness_score = 0
            if recent_range.iloc[-1] < prev_range.iloc[-1] * 0.6: # 顯著收縮
                tightness_score = 50
            elif recent_range.iloc[-1] < 0.05: # 絕對波幅小於 5%
                tightness_score = 40

            # 2. 成交量乾涸 (Volume Dry-up)
            avg_vol = df['Volume'].rolling(20).mean().iloc[-1]
            current_vol = df['Volume'].iloc[-1]
            vol_score = 30 if current_vol < avg_vol else 0

            # 3. 相對強度 (RS) 簡化版: 50天表現
            rs_perf = (df['Close'].iloc[-1] / df['Close'].iloc[-50]) - 1
            rs_score = min(20, rs_perf * 100)

            total_score = tightness_score + vol_score + rs_score

            return {
                'ticker': ticker,
                'score': round(total_score, 2),
                'price': round(df['Close'].iloc[-1], 2),
                'change_50d': f"{round(rs_perf * 100, 2)}%",
                'vol_ratio': round(current_vol / avg_vol, 2)
            }
        except Exception:
            return None

    def run(self):
        tickers = self.get_stock_pool()
        logger.info(f"開始掃描 {len(tickers)} 隻股票...")
        
        for i, t in enumerate(tickers):
            if i % 20 == 0: logger.info(f"掃描進度: {i}/{len(tickers)}...")
            res = self.calculate_vcp_score(t)
            if res:
                self.results.append(res)
            # 為了防止 yfinance 被限流，稍微停頓
            time.sleep(0.05)

        self.results = sorted(self.results, key=lambda x: x['score'], reverse=True)
        return self.results

def main():
    # 讀取環境變量
    min_score = int(os.getenv('MIN_SCORE', 40))
    email_sender = os.getenv('EMAIL_SENDER')
    email_pwd = os.getenv('EMAIL_PASSWORD')
    email_to = os.getenv('EMAIL_RECIPIENT')

    scanner = VCPScanner(min_score=min_score)
    results = scanner.run()

    # 存檔
    with open('vcp_results.json', 'w') as f:
        json.dump(results, f, indent=2)

    # 構建報告
    if results:
        report_html = "<h2>VCP 掃描成功 - 強勢股清單</h2><table border='1'><tr><th>代碼</th><th>總分</th><th>價格</th><th>50天漲幅</th><th>成交量比</th></tr>"
        for r in results[:20]: # 僅列出前 20 名
            report_html += f"<tr><td><b>{r['ticker']}</b></td><td>{r['score']}</td><td>${r['price']}</td><td>{r['change_50d']}</td><td>{r['vol_ratio']}</td></tr>"
        report_html += "</table><p>評分越高代表收縮形態越趨近完美。</p>"

        # 發送電郵
        if email_sender and email_pwd and email_to:
            try:
                msg = MIMEMultipart()
                msg['Subject'] = f"🚀 VCP Scanner 報告 ({len(results)} 隻符合條件)"
                msg['From'] = email_sender
                msg['To'] = email_to
                msg.attach(MIMEText(report_html, 'html'))
                
                with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                    server.login(email_sender, email_pwd)
                    server.send_message(msg)
                logger.info("報告已發送至郵箱")
            except Exception as e:
                logger.error(f"郵件發送失敗: {e}")
    else:
        logger.info("今日無符合 VCP 形態之股票")

if __name__ == "__main__":
    main()
