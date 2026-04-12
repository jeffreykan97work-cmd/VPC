#!/usr/bin/env python3
"""
VCP Scanner - Daily US Stock Alert
A tool to scan US stocks using Value, Consolidation, and Potential (VCP) pattern analysis
"""

import os
import json
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import pandas as pd
import numpy as np
import yfinance as yf
from pandas_datareader import data as pdr

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vcp_scan.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class VCPScanner:
    """VCP (Value, Consolidation, Potential) Pattern Scanner"""
    
    def __init__(self, min_score=65, min_grade='B'):
        self.min_score = min_score
        self.min_grade = min_grade
        self.grade_order = {'A+': 5, 'A': 4, 'B': 3, 'C': 2, 'D': 1}
        self.results = []
        
    def calculate_vcp_score(self, ticker):
        """
        Calculate VCP score for a given ticker
        VCP = Value (30%) + Consolidation (40%) + Potential (30%)
        """
        try:
            # Download stock data
            df = yf.download(ticker, period='1y', progress=False)
            
            if len(df) < 100:
                logger.warning(f"Insufficient data for {ticker}")
                return None
            
            # Calculate technical indicators
            close = df['Close'].values
            volume = df['Volume'].values
            
            # 1. Value Component (30%) - Price relative to moving average
            ma50 = df['Close'].rolling(window=50).mean().iloc[-1]
            ma200 = df['Close'].rolling(window=200).mean().iloc[-1]
            current_price = close[-1]
            
            price_to_ma50 = (current_price / ma50 - 1) * 100
            value_score = max(0, min(100, 50 - abs(price_to_ma50)))
            
            # 2. Consolidation Component (40%) - Volume and volatility analysis
            recent_volatility = np.std(close[-20:] / close[-21:-1] - 1) * 100
            avg_volume = np.mean(volume[-20:])
            current_volume = volume[-1]
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
            
            consolidation_score = max(0, min(100, 
                100 - recent_volatility * 2 + (volume_ratio - 1) * 10
            ))
            
            # 3. Potential Component (30%) - Trend and momentum
            trend = (close[-1] - close[-50]) / close[-50] * 100
            momentum = (close[-1] - close[-10]) / close[-10] * 100
            
            potential_score = max(0, min(100, 
                50 + momentum + (trend * 0.5 if trend > 0 else trend * 0.2)
            ))
            
            # Calculate weighted VCP score
            vcp_score = (
                value_score * 0.3 + 
                consolidation_score * 0.4 + 
                potential_score * 0.3
            )
            
            # Determine grade
            grade = self.score_to_grade(vcp_score)
            
            return {
                'ticker': ticker,
                'vcp_score': round(vcp_score, 2),
                'grade': grade,
                'price': round(current_price, 2),
                'ma50': round(ma50, 2),
                'ma200': round(ma200, 2),
                'volume_ratio': round(volume_ratio, 2),
                'trend_50d': round(trend, 2),
                'momentum_10d': round(momentum, 2),
                'value_score': round(value_score, 2),
                'consolidation_score': round(consolidation_score, 2),
                'potential_score': round(potential_score, 2),
                'scan_time': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error scanning {ticker}: {str(e)}")
            return None
    
    def score_to_grade(self, score):
        """Convert VCP score to letter grade"""
        if score >= 90:
            return 'A+'
        elif score >= 80:
            return 'A'
        elif score >= 70:
            return 'B'
        elif score >= 60:
            return 'C'
        else:
            return 'D'
    
    def filter_results(self, results):
        """Filter results based on min_score and min_grade"""
        filtered = []
        min_grade_value = self.grade_order.get(self.min_grade, 0)
        
        for result in results:
            if result is None:
                continue
            
            score = result['vcp_score']
            grade = result['grade']
            grade_value = self.grade_order.get(grade, 0)
            
            if score >= self.min_score and grade_value >= min_grade_value:
                filtered.append(result)
        
        return sorted(filtered, key=lambda x: x['vcp_score'], reverse=True)
    
    def scan_tickers(self, tickers):
        """Scan multiple tickers"""
        logger.info(f"Starting VCP scan for {len(tickers)} tickers...")
        
        for i, ticker in enumerate(tickers, 1):
            logger.info(f"[{i}/{len(tickers)}] Scanning {ticker}...")
            result = self.calculate_vcp_score(ticker)
            if result:
                self.results.append(result)
        
        self.results = self.filter_results(self.results)
        logger.info(f"Found {len(self.results)} stocks matching criteria")
        return self.results
    
    def save_results(self, filename='vcp_results.json'):
        """Save results to JSON file"""
        with open(filename, 'w') as f:
            json.dump(self.results, f, indent=2)
        logger.info(f"Results saved to {filename}")
    
    def get_results_html(self):
        """Generate HTML report of results"""
        if not self.results:
            return "<p>No stocks matching the criteria were found.</p>"
        
        html = """
        <table border="1" cellpadding="10" cellspacing="0" style="border-collapse:collapse; width:100%;">
            <thead style="background-color:#4CAF50; color:white;">
                <tr>
                    <th>Ticker</th>
                    <th>VCP Score</th>
                    <th>Grade</th>
                    <th>Current Price</th>
                    <th>MA50</th>
                    <th>MA200</th>
                    <th>Volume Ratio</th>
                    <th>50D Trend</th>
                    <th>10D Momentum</th>
                </tr>
            </thead>
            <tbody>
        """
        
        for result in self.results:
            html += f"""
                <tr>
                    <td><strong>{result['ticker']}</strong></td>
                    <td>{result['vcp_score']}</td>
                    <td style="background-color:#{"#90EE90" if result['grade'] in ['A+', 'A'] else "#FFEB3B"}">{result['grade']}</td>
                    <td>${result['price']}</td>
                    <td>${result['ma50']}</td>
                    <td>${result['ma200']}</td>
                    <td>{result['volume_ratio']}</td>
                    <td>{result['trend_50d']:.2f}%</td>
                    <td>{result['momentum_10d']:.2f}%</td>
                </tr>
            """
        
        html += """
            </tbody>
        </table>
        """
        return html


class EmailNotifier:
    """Send email notifications"""
    
    def __init__(self, sender, password, recipient):
        self.sender = sender
        self.password = password
        self.recipient = recipient
    
    def send_alert(self, results, min_score, min_grade):
        """Send email alert with scan results"""
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = f"VCP Scanner Alert - {len(results)} Stock(s) Found"
            msg['From'] = self.sender
            msg['To'] = self.recipient
            
            # Create email body
            if results:
                html_results = self._generate_html_table(results)
                text = f"""
VCP Scanner Daily Alert
=====================

Scan Parameters:
- Minimum Score: {min_score}
- Minimum Grade: {min_grade}
- Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Found {len(results)} stock(s) matching criteria:

{self._generate_text_results(results)}
                """
                html = f"""
                <html>
                  <body>
                    <h2>VCP Scanner Daily Alert</h2>
                    <p><strong>Scan Parameters:</strong></p>
                    <ul>
                        <li>Minimum Score: {min_score}</li>
                        <li>Minimum Grade: {min_grade}</li>
                        <li>Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</li>
                    </ul>
                    <p><strong>Found {len(results)} stock(s) matching criteria:</strong></p>
                    {html_results}
                  </body>
                </html>
                """
            else:
                text = f"""
VCP Scanner Daily Alert
=====================

Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

No stocks found matching the criteria.
"""
                html = f"""
                <html>
                  <body>
                    <h2>VCP Scanner Daily Alert</h2>
                    <p>Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                    <p>No stocks found matching the criteria.</p>
                  </body>
                </html>
                """
            
            part1 = MIMEText(text, 'plain')
            part2 = MIMEText(html, 'html')
            msg.attach(part1)
            msg.attach(part2)
            
            # Send email
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(self.sender, self.password)
                server.send_message(msg)
            
            logger.info(f"Email sent to {self.recipient}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send email: {str(e)}")
            return False
    
    @staticmethod
    def _generate_text_results(results):
        """Generate text format results"""
        text = ""
        for result in results:
            text += f"""
{result['ticker']}
  VCP Score: {result['vcp_score']} ({result['grade']})
  Price: ${result['price']} (MA50: ${result['ma50']}, MA200: ${result['ma200']})
  Volume Ratio: {result['volume_ratio']}
  50D Trend: {result['trend_50d']:.2f}%
  10D Momentum: {result['momentum_10d']:.2f}%
"""
        return text
    
    @staticmethod
    def _generate_html_table(results):
        """Generate HTML table from results"""
        html = """
        <table border="1" cellpadding="10" cellspacing="0" style="border-collapse:collapse; width:100%;">
            <thead style="background-color:#4CAF50; color:white;">
                <tr>
                    <th>Ticker</th>
                    <th>VCP Score</th>
                    <th>Grade</th>
                    <th>Price</th>
                    <th>MA50</th>
                    <th>MA200</th>
                    <th>Volume</th>
                    <th>50D Trend</th>
                    <th>10D Mom</th>
                </tr>
            </thead>
            <tbody>
        """
        
        for result in results:
            grade_color = "#90EE90" if result['grade'] in ['A+', 'A'] else "#FFEB3B"
            html += f"""
                <tr>
                    <td><strong>{result['ticker']}</strong></td>
                    <td>{result['vcp_score']}</td>
                    <td style="background-color:{grade_color}"><strong>{result['grade']}</strong></td>
                    <td>${result['price']}</td>
                    <td>${result['ma50']}</td>
                    <td>${result['ma200']}</td>
                    <td>{result['volume_ratio']}</td>
                    <td>{result['trend_50d']:.2f}%</td>
                    <td>{result['momentum_10d']:.2f}%</td>
                </tr>
            """
        
        html += """
            </tbody>
        </table>
        """
        return html


def main():
    """Main function"""
    try:
        # Load environment variables
        email_sender = os.getenv('EMAIL_SENDER')
        email_password = os.getenv('EMAIL_PASSWORD')
        email_recipient = os.getenv('EMAIL_RECIPIENT')
        min_score = int(os.getenv('MIN_SCORE', 65))
        min_grade = os.getenv('MIN_GRADE', 'B')
        
        logger.info("VCP Scanner started")
        
        # List of major US stocks to scan
        tickers = [
            'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'TSLA', 'META', 'BERKB',
            'JPM', 'V', 'JNJ', 'WMT', 'PG', 'MA', 'HD', 'DIS', 'PYPL', 'ADBE',
            'CRM', 'CSCO', 'INTC', 'AMD', 'QCOM', 'IBM', 'ORCL', 'SAP', 'NFLX',
            'AMAT', 'MU', 'LRCX', 'ASML', 'ARCH', 'AVGO', 'BRCM', 'CDNS', 'CHKP',
        ]
        
        # Initialize scanner
        scanner = VCPScanner(min_score=min_score, min_grade=min_grade)
        
        # Scan stocks
        results = scanner.scan_tickers(tickers)
        
        # Save results
        scanner.save_results('vcp_results.json')
        
        # Send email notification
        if email_sender and email_password and email_recipient:
            notifier = EmailNotifier(email_sender, email_password, email_recipient)
            notifier.send_alert(results, min_score, min_grade)
        else:
            logger.warning("Email credentials not configured, skipping email notification")
        
        logger.info("VCP Scanner completed successfully")
        
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
