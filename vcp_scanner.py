#!/usr/bin/env python3
"""
VCP Scanner - Pro Edition
優化說明：
1. 自動獲取 S&P 500 / NASDAQ 100 股票池
2. 引入 Minervini 趨勢模板過濾
3. 增加波動率收縮 (Volatility Contraction) 邏輯
"""

import os
import json
import logging
import smtplib
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
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
    def __init__(self, min_score=60):
        self.min_score = min_score
        self.results = []

    def get_stock_pool(self):
        """獲取更廣泛的股票池 (S&P 500 + NASDAQ 100)"""
        try:
            logger.info("正在獲取 S&P 500 和 NASDAQ 100 列表...")
            sp500 = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
            nasdaq100 = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100')[4]
            
            tickers = set(sp500['Symbol'].tolist() + nasdaq100['Ticker'].tolist())
            # 清洗標籤 (yfinance 格式)
            tickers = [t.replace('.', '-') for t in tickers]
            return sorted(list(tickers))
        except Exception as e:
            logger.error(f"獲取股票池失敗: {e}")
            return ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA'] # 備用

    def check_trend_template(self, df):
        """
        Mark Minervini 趨勢模板 (Trend Template)
        這能確保我們只挑選處於第二階段上升趨勢的股票
        """
        if len(df) < 200: return False
        
        close = df['Close'].iloc[-1]
        ma50 = df['MA50'].iloc[-1]
        ma150 = df['MA150'].iloc[-1]
        ma200 = df['MA200'].iloc[-1]
        low_52w = df['Close'].rolling(window=252).min().iloc[-1]
        high_52w = df['Close'].rolling(window=252).max().iloc[-1]
        
        # 條件 1: 價格在 150MA 和 200MA 之上
        cond1 = close > ma150 and close > ma200
        # 條件 2: 150MA 在 200MA 之上
        cond2 = ma150 > ma200
        # 條件 3: 200MA 正在上升 (至少一個月)
        ma200_prev = df['MA200'].iloc[-20]
        cond3 = ma200 > ma200_prev
        # 條件 4: 50MA 在 150MA 和 200MA 之上
        cond4 = ma50 > ma150 and ma50 > ma200
        # 條件 5: 價格在 50MA 之上
        cond5 = close > ma50
        # 條件 6: 價格比 52 週低點高出至少 30%
        cond6 = close >= (low_52w * 1.30)
        # 條件 7: 價格距離 52 週高點 25% 以內
        cond7 = close >= (high_52w * 0.75)

        return all([cond1, cond2, cond3, cond4, cond5, cond6, cond7])

    def calculate_vcp_score(self, ticker):
        """計算 VCP 評分"""
        try:
            df = yf.download(ticker, period='1y', progress=False)
            if len(df) < 200: return None

            # 計算均線
            df['MA50'] = df['Close'].rolling(window=50).mean()
            df['MA150'] = df['Close'].rolling(window=150).mean()
            df['MA200'] = df['Close'].rolling(window=200).mean()

            # 1. 趨勢過濾 (不符合則直接淘汰)
            if not self.check_trend_template(df):
                return None

            # 2. 波動率收縮 (Contraction) 檢測
            # 比較最近 10 天與前 40 天的波幅
            recent_range = (df['High'].rolling(10).max() - df['Low'].rolling(10).min()) / df['Close']
            prev_range = (df['High'].rolling(40).max() - df['Low'].rolling(40).min()) / df['Close']
            vol_contraction = prev_range.iloc[-10] / recent_range.iloc[-1] 
            
            # 3. 成交量檢測 (是否有縮量乾涸 VCP 特徵)
            avg_vol = df['Volume'].rolling(20).mean()
            vol_ratio = df['Volume'].iloc[-1] / avg_vol.iloc[-1]
            
            # 評分邏輯 (滿分 100)
            score = 0
            if vol_contraction > 1.5: score += 40 # 波動顯著收縮
            if vol_ratio < 1.0: score += 30      # 突破前縮量
            
            # 相對強度指標 (簡化版：對比標普500表現)
            # 這裡略過複雜計算，改用 50 天漲幅
            rs_score = (df['Close'].iloc[-1] / df['Close'].iloc[-50]) * 30
            score += min(rs_score, 30)

            return {
                'ticker': ticker,
                'vcp_score': round(score, 2),
                'price': round(df['Close'].iloc[-1], 2),
                'ma50': round(df['MA50'].iloc[-1], 2),
                'vol_ratio': round(vol_ratio, 2),
                'change_50d': round(((df['Close'].iloc[-1]/df['Close'].iloc[-50])-1)*100, 2)
            }
        except Exception as e:
            return None

    def scan(self):
        tickers = self.get_stock_pool()
        logger.info(f"開始掃描 {len(tickers)} 隻股票...")
        
        final_results = []
        for i, ticker in enumerate(tickers):
            if i % 50 == 0: logger.info(f"已掃描 {i}/{len(tickers)}...")
            res = self.calculate_vcp_score(ticker)
            if res and res['vcp_score'] >= self.min_score:
                final_results.append(res)
        
        # 按評分排序
        self.results = sorted(final_results, key=lambda x: x['vcp_score'], reverse=True)
        return self.results

    def save_and_report(self):
        with open('vcp_results.json', 'w') as f:
            json.dump(self.results, f, indent=2)
        
        # 生成 HTML 表格
        html = "<table border='1'><tr><th>Ticker</th><th>Score</th><th>Price</th><th>50D %</th></tr>"
        for r in self.results:
            html += f"<tr><td>{r['ticker']}</td><td>{r['vcp_score']}</td><td>{r['price']}</td><td>{r['change_50d']}%</td></tr>"
        html += "</table>"
        return html

# --- 電郵通知與主程序與原代碼類似，但更新了內容格式 ---

def main():
    # 讀取環境變量
    email_sender = os.getenv('EMAIL_SENDER')
    email_password = os.getenv('EMAIL_PASSWORD')
    email_recipient = os.getenv('EMAIL_RECIPIENT')
    
    scanner = VCPScanner(min_score=50) # 稍微調低分數門檻以獲取更多候選
    results = scanner.scan()
    html_content = scanner.save_and_report()
    
    logger.info(f"掃描結束，找到 {len(results)} 隻潛在 VCP 股票")
    
    if email_sender and email_password and results:
        # 發送郵件 (邏輯同你原有的 EmailNotifier)
        msg = MIMEMultipart()
        msg['Subject'] = f"VCP 掃描報告 - 找到 {len(results)} 隻股票"
        msg.attach(MIMEText(html_content, 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(email_sender, email_password)
            server.send_message(msg)
            logger.info("報告已發送")

if __name__ == "__main__":
    main()
