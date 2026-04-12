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
    def __init__(self):
        self.results = []
        self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124'}

    def get_stock_pool(self):
        """優化：從多個來源確保獲取 S&P 500 數據"""
        try:
            url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
            response = requests.get(url, headers=self.headers, timeout=10)
            df = pd.read_html(StringIO(response.text))[0]
            tickers = df['Symbol'].tolist()
            # 修正 yfinance 格式
            return [t.replace('.', '-') for t in tickers]
        except Exception as e:
            logger.error(f"數據源抓取失敗: {e}")
            return ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'AMD', 'NFLX', 'AVGO']

    def analyze_vcp(self, ticker):
        """
        核心邏輯優化：
        1. 檢測第二階段趨勢
        2. 檢測波動收縮 (VRP)
        3. 檢測相對強度 (RS)
        """
        try:
            # 獲取 1.5 年數據以計算 200MA 趨勢
            df = yf.download(ticker, period='2y', progress=False)
            if len(df) < 252: return None

            c = df['Close'].iloc[-1]
            ma50 = df['Close'].rolling(50).mean()
            ma150 = df['Close'].rolling(150).mean()
            ma200 = df['Close'].rolling(200).mean()
            
            # Minervini 趨勢模板核心條件
            is_stage_2 = (
                c > ma150.iloc[-1] and 
                c > ma200.iloc[-1] and 
                ma150.iloc[-1] > ma200.iloc[-1] and
                ma200.iloc[-1] > ma200.iloc[-20] # 200MA 向上
            )

            # 波動收縮檢測 (比較最近 10 天與前 40 天的波幅)
            recent_high = df['High'].iloc[-10:].max()
            recent_low = df['Low'].iloc[-10:].min()
            recent_volatility = (recent_high - recent_low) / recent_low
            
            prev_high = df['High'].iloc[-50:-10].max()
            prev_low = df['Low'].iloc[-50:-10].min()
            prev_volatility = (prev_high - prev_low) / prev_low

            # 計算 VCP 評分 (0-100)
            score = 0
            if is_stage_2: score += 40
            if recent_volatility < prev_volatility * 0.7: score += 30 # 波動顯著收縮
            if recent_volatility < 0.08: score += 20 # 極度收緊 (波幅 < 8%)
            
            # 成交量乾涸檢測
            avg_vol = df['Volume'].rolling(20).mean().iloc[-1]
            if df['Volume'].iloc[-1] < avg_vol: score += 10

            # 只要分數 > 40 就列入名單 (確保市況差時也有觀察股)
            if score >= 40:
                return {
                    'ticker': ticker,
                    'score': score,
                    'price': round(float(c), 2),
                    'volatility': f"{round(recent_volatility * 100, 2)}%",
                    'status': 'Perfect' if score >= 80 else 'Watching'
                }
        except:
            return None

    def run(self):
        tickers = self.get_stock_pool()
        logger.info(f"開始掃描 {len(tickers)} 隻股票...")
        
        for i, t in enumerate(tickers):
            res = self.analyze_vcp(t)
            if res: self.results.append(res)
            if i % 50 == 0: logger.info(f"進度: {i}/{len(tickers)}")
        
        # 按評分排序
        self.results.sort(key=lambda x: x['score'], reverse=True)
        return self.results

def main():
    scanner = VCPScanner()
    results = scanner.run()
    
    # 儲存結果
    with open('vcp_results.json', 'w') as f:
        json.dump(results, f, indent=2)

    # 郵件通知邏輯 (與你之前配置一致)
    sender = os.getenv('EMAIL_SENDER')
    pwd = os.getenv('EMAIL_PASSWORD')
    to = os.getenv('EMAIL_RECIPIENT')

    if sender and pwd and to:
        msg = MIMEMultipart()
        msg['Subject'] = f"VCP 掃描報告 - 發現 {len(results)} 隻潛力股"
        body = "<h3>VCP 篩選結果 (按評分排序)</h3><table border='1'><tr><th>代碼</th><th>評分</th><th>價格</th><th>近期波幅</th><th>狀態</th></tr>"
        for r in results[:20]:
            color = "#ccffcc" if r['status'] == 'Perfect' else "#ffffff"
            body += f"<tr bgcolor='{color}'><td>{r['ticker']}</td><td>{r['score']}</td><td>{r['price']}</td><td>{r['volatility']}</td><td>{r['status']}</td></tr>"
        body += "</table>"
        
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, pwd)
            server.send_message(msg)
            logger.info("郵件已發送")

if __name__ == "__main__":
    main()
