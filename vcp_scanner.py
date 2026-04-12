#!/usr/bin/env python3
import os
import json
import logging
import smtplib
import time
import pandas as pd
import numpy as np
import yfinance as yf
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

    def get_stock_pool(self):
        """獲取 S&P 500 + NASDAQ 100 股票池"""
        try:
            logger.info("正在從維基百科獲取大型股清單...")
            sp500 = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
            nasdaq100 = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100')[4]
            tickers = set(sp500['Symbol'].tolist() + nasdaq100['Ticker'].tolist())
            return sorted([t.replace('.', '-') for t in tickers])
        except Exception as e:
            logger.error(f"獲取股票池失敗，使用預設清單: {e}")
            return ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'TSLA', 'META']

    def check_vcp_setup(self, df):
        """核心 VCP 形態過濾 (Minervini 趨勢模板)"""
        if len(df) < 200: return False
        
        c = df['Close'].iloc[-1]
        ma50 = df['Close'].rolling(50).mean().iloc[-1]
        ma150 = df['Close'].rolling(150).mean().iloc[-1]
        ma200 = df['Close'].rolling(200).mean().iloc[-1]
        low_52w = df['Low'].rolling(252).min().iloc[-1]
        high_52w = df['High'].rolling(252).max().iloc[-1]

        # 1. 股價在 150 和 200 日均線之上
        # 2. 150 MA 在 200 MA 之上
        # 3. 200 MA 向上轉向 (或至少不下跌)
        # 4. 股價比 52 週低點高出 25% (修正原本 30% 以增加機會)
        # 5. 股價距離 52 週高點 25% 以內
        conditions = [
            c > ma150,
            c > ma200,
            ma150 > ma200,
            c > low_52w * 1.25,
            c > high_52w * 0.75
        ]
        return all(conditions)

    def scan_ticker(self, ticker):
        """分析單隻股票並評分"""
        try:
            df = yf.download(ticker, period='1y', progress=False)
            if df.empty or not self.check_vcp_setup(df): return None

            # 計算收縮量 (VCP 的關鍵)
            recent_vol = df['Volume'].rolling(10).mean().iloc[-1]
            prev_vol = df['Volume'].rolling(50).mean().iloc[-1]
            vol_dry_up = 1.0 if recent_vol < prev_vol else 0.5

            # 計算價格緊湊度 (最近 20 天的波幅)
            price_tightness = (df['High'].iloc[-20:].max() / df['Low'].iloc[-20:].min()) - 1
            tightness_score = max(0, 50 * (1 - price_tightness * 5)) # 波幅越小分數越高

            total_score = (tightness_score) + (vol_dry_up * 50)
            
            return {
                'ticker': ticker,
                'score': round(total_score, 2),
                'price': round(df['Close'].iloc[-1], 2),
                'change_50d': round(((df['Close'].iloc[-1]/df['Close'].iloc[-50])-1)*100, 2)
            }
        except: return None

    def run(self):
        tickers = self.get_stock_pool()
        logger.info(f"開始掃描 {len(tickers)} 隻股票...")
        for i, t in enumerate(tickers):
            if i % 50 == 0: logger.info(f"掃描中: {i}/{len(tickers)}")
            res = self.scan_ticker(t)
            if res: self.results.append(res)
            time.sleep(0.1) # 避免 API 頻繁請求被封

        self.results = sorted(self.results, key=lambda x: x['score'], reverse=True)
        return self.results

def main():
    min_score = int(os.getenv('MIN_SCORE', 50))
    scanner = VCPScanner(min_score=min_score)
    results = scanner.run()
    
    # 儲存結果
    with open('vcp_results.json', 'w') as f:
        json.dump(results, f, indent=2)

    # 電郵通知
    sender = os.getenv('EMAIL_SENDER')
    pwd = os.getenv('EMAIL_PASSWORD')
    to = os.getenv('EMAIL_RECIPIENT')
    
    if sender and pwd and to and results:
        msg = MIMEMultipart()
        msg['Subject'] = f"VCP 掃描報告 - 發現 {len(results)} 隻潛力股"
        body = "<h2>符合 VCP 形態股票清單:</h2><table border='1'><tr><th>代碼</th><th>評分</th><th>股價</th><th>50天漲跌</th></tr>"
        for r in results[:15]: # 只取前 15 名
            body += f"<tr><td>{r['ticker']}</td><td>{r['score']}</td><td>{r['price']}</td><td>{r['change_50d']}%</td></tr>"
        body += "</table>"
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, pwd)
            server.send_message(msg)
            logger.info("郵件發送成功")

if __name__ == "__main__":
    main()
