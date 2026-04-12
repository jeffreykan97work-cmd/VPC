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

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class VCPScanner:
    def __init__(self, min_score=50):
        self.min_score = min_score
        self.results = []
        self.headers = {'User-Agent': 'Mozilla/5.0'}

    def get_sp500_tickers(self):
        """獲取更廣泛的股票池，避免樣本太少"""
        try:
            url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
            resp = requests.get(url, headers=self.headers)
            df = pd.read_html(StringIO(resp.text))[0]
            return [t.replace('.', '-') for t in df['Symbol'].tolist()]
        except:
            return ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'AMD', 'NFLX', 'AVGO']

    def check_trend_template(self, df):
        """Mark Minervini 趨勢模板 (基礎過濾)"""
        c = df['Close'].iloc[-1]
        ma50 = df['Close'].rolling(50).mean().iloc[-1]
        ma150 = df['Close'].rolling(150).mean().iloc[-1]
        ma200 = df['Close'].rolling(200).mean().iloc[-1]
        low_52w = df['Low'].rolling(252).min().iloc[-1]
        high_52w = df['High'].rolling(252).max().iloc[-1]
        
        # 條件：價格在均線之上，均線多頭排列
        cond1 = c > ma150 and c > ma200
        cond2 = ma150 > ma200
        cond3 = ma50 > ma150
        cond4 = c > low_52w * 1.25 # 脫離底部至少 25%
        cond5 = c > high_52w * 0.75 # 距離高點 25% 以內
        
        return all([cond1, cond2, cond3, cond4, cond5])

    def calculate_vcp_score(self, ticker):
        try:
            df = yf.download(ticker, period='1y', progress=False)
            if len(df) < 200 or not self.check_trend_template(df): return None

            # 1. 波動收縮 (Volatility Contraction) - 40分
            # 比較最近 10 天波幅與之前 20 天波幅
            recent_range = (df['High'].iloc[-10:].max() / df['Low'].iloc[-10:].min()) - 1
            prev_range = (df['High'].iloc[-30:-10].max() / df['Low'].iloc[-30:-10].min()) - 1
            tightness_score = 40 if recent_range < prev_range * 0.8 else 20
            if recent_range < 0.06: tightness_score += 10 # 極度緊湊額外加分

            # 2. 量縮 (Volume Dry-up) - 30分
            avg_vol = df['Volume'].rolling(20).mean().iloc[-1]
            current_vol = df['Volume'].iloc[-1]
            # VCP 關鍵：突破前成交量應低於平均
            vol_score = 30 if current_vol < avg_vol else 10

            # 3. 相對強度 (Relative Strength) - 30分
            rs_score = 30 if df['Close'].iloc[-1] > df['Close'].iloc[-50] else 0

            total_score = tightness_score + vol_score + rs_score
            
            return {
                'ticker': ticker,
                'score': total_score,
                'price': round(float(df['Close'].iloc[-1]), 2),
                'tightness': f"{round(recent_range*100, 2)}%",
                'vol_ratio': round(current_vol/avg_vol, 2)
            }
        except: return None

    def run(self):
        tickers = self.get_sp500_tickers()
        logger.info(f"掃描 {len(tickers)} 隻股票...")
        for i, t in enumerate(tickers):
            res = self.calculate_vcp_score(t)
            if res and res['score'] >= self.min_score:
                self.results.append(res)
            if i % 50 == 0: logger.info(f"進度: {i}/{len(tickers)}")
            time.sleep(0.1)
        return sorted(self.results, key=lambda x: x['score'], reverse=True)

def main():
    scanner = VCPScanner(min_score=int(os.getenv('MIN_SCORE', 50)))
    results = scanner.run()
    
    # 儲存與發送 (簡化邏輯)
    with open('vcp_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    # ... (此處保留你原本的 EmailNotifier 邏輯即可) ...
    logger.info(f"掃描結束，發現 {len(results)} 隻股票")

if __name__ == "__main__":
    main()
