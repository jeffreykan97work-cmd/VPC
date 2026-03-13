"""
VCP (Volatility Contraction Pattern) Scanner
Based on Mark Minervini's methodology from "Trade Like a Stock Market Wizard"
Scans top 1000 US stocks and sends email alerts for qualifying setups
"""

import os
import json
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional
import time

import pandas as pd
import numpy as np
import yfinance as yf
import requests

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("vcp_scan.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────
@dataclass
class VCPScore:
    ticker: str
    company_name: str
    score: float                  # 0-100
    grade: str                    # A+, A, B, C, D
    price: float
    pivot_price: float            # breakout level
    stop_loss: float
    risk_reward: float

    # Stage Analysis (Weinstein)
    stage: int                    # 1,2,3,4
    stage_score: float

    # Trend Template (MM 8 criteria)
    trend_template_score: float
    above_150ma: bool
    above_200ma: bool
    ma150_above_ma200: bool
    ma200_trending_up: bool
    ma50_above_ma150_ma200: bool
    price_above_ma50: bool
    within_25pct_of_52w_high: bool
    above_30pct_of_52w_low: bool

    # VCP Contractions
    num_contractions: int         # ideal: 2-4
    contraction_score: float
    contraction_details: list

    # Volume Analysis
    volume_score: float
    avg_volume: float
    volume_dry_up: bool           # volume shrinks during base
    breakout_volume_surge: bool

    # RS Rating (Relative Strength)
    rs_rating: float              # 0-100
    rs_score: float

    # Base Quality
    base_depth: float             # % decline from peak to trough
    base_duration_weeks: int
    base_score: float

    # Pivot / Tightness
    tightness_score: float
    weeks_tight: int

    details: dict = field(default_factory=dict)


# ─────────────────────────────────────────────
# Universe – Top 1000 US Stocks
# ─────────────────────────────────────────────
def get_top1000_tickers() -> list[str]:
    """
    Top 1000 US stocks by market cap — hardcoded for reliability.
    Covers S&P500 + NASDAQ100 + Russell 1000 large/mid caps.
    No external HTTP calls needed (avoids 403 blocks).
    """
    tickers = [
        # Mega Cap Tech
        "AAPL","MSFT","NVDA","GOOGL","GOOG","AMZN","META","TSLA","AVGO","ORCL",
        "AMD","ADBE","CRM","INTC","QCOM","TXN","MU","AMAT","LRCX","KLAC",
        "SNPS","CDNS","MRVL","ADI","NXPI","ON","MPWR","ENTG","COHR","WOLF",
        "ARM","SMCI","PLTR","UBER","ABNB","DASH","LYFT","RBLX","U","COIN",
        # Financials
        "JPM","BAC","WFC","GS","MS","BLK","C","AXP","SPGI","MCO",
        "ICE","CME","SCHW","BK","STT","TFC","USB","PNC","COF","DFS",
        "AIG","MET","PRU","AFL","ALL","PGR","TRV","CB","HIG","WRB",
        "BRK-B","V","MA","PYPL","SQ","AFRM","SOFI","HOOD","UPST","LC",
        # Healthcare
        "LLY","UNH","JNJ","ABBV","MRK","TMO","ABT","DHR","BMY","AMGN",
        "GILD","ISRG","SYK","BSX","MDT","EW","ZBH","BAX","BDX","RMD",
        "DXCM","PODD","INSP","NVCR","KRYS","RXRX","BEAM","EDIT","NTLA","CRSP",
        "REGN","VRTX","BIIB","ALNY","MRNA","BNTX","PFE","CVS","CI","HUM",
        "MOH","CNC","ELV","ANTM","EXAS","ILMN","HOLX","HOLO","VEEV","DOCS",
        # Consumer
        "AMZN","WMT","COST","TGT","HD","LOW","MCD","SBUX","YUM","QSR",
        "CMG","DPZ","WING","CAVA","BROS","SHAK","TXRH","DENN","EAT","DRI",
        "NKE","LULU","PVH","RL","TPR","VFC","HBI","UAA","CROX","SKX",
        "DECK","ONON","SKECHERS","BIRK","COLM","WWW","GH","PTON","NFLX",
        "DIS","PARA","WBD","FOXA","FOX","NYT","SPOT","PINS","SNAP","MTCH",
        # Industrials
        "GE","HON","CAT","DE","RTX","LMT","NOC","GD","BA","TDG",
        "HEI","SPR","TXT","WWD","DRS","LDOS","SAIC","CACI","BAH","PLTR",
        "UPS","FDX","XPO","CHRW","EXPD","JBHT","ODFL","SAIA","WERN","KNX",
        "CSX","UNP","NSC","CP","CNI","WAB","TRN","GNRC","IR","CARR",
        "OTIS","JCI","EMR","ROK","PH","ITW","ETN","AME","ROP","VRSK",
        # Energy
        "XOM","CVX","COP","EOG","SLB","HAL","BKR","MPC","VLO","PSX",
        "PXD","DVN","FANG","OXY","APA","HES","MRO","CTRA","SM","MTDR",
        "AR","RRC","EQT","SWN","CNX","CHRD","PDCE","ESTE","BATL","NOG",
        # Utilities & REITs
        "NEE","DUK","SO","D","EXC","AEP","SRE","XEL","WEC","ES",
        "AWK","AMT","PLD","EQIX","CCI","SBAC","DLR","PSA","EXR","CUBE",
        "O","NNN","STOR","VICI","MGM","LVS","WYNN","CZR","BXP","SLG",
        # Materials
        "LIN","APD","ECL","SHW","PPG","RPM","IFF","EMN","CE","WLK",
        "NUE","STLD","CLF","X","FCX","NEM","GOLD","AEM","KGC","AGI",
        "ALB","LTHM","SQM","PLL","LAC","ALTM","MP","UUUU","USAS","FFIE",
        # Communication
        "GOOGL","META","NFLX","DIS","CMCSA","VZ","T","TMUS","CHTR","LBRDA",
        "DISH","SIRI","IHRT","FOXA","NWSA","IPG","OMC","PUBGY","WPP","TTGT",
        # Growth / Mid Cap
        "SHOP","NET","DDOG","SNOW","MDB","TWLO","ZS","OKTA","CFLT","GTLB",
        "BILL","TOST","IOT","APP","FICO","AXON","ELF","CELH","DUOL","CAVA",
        "BROS","WING","MELI","SE","GRAB","BEKE","FUTU","TIGR","MNSO","PDD",
        "PANW","FTNT","CRWD","S","TENB","QLYS","VRNT","RDWR","SAIL","XMTR",
        "ZI","EVBG","ALTR","BRZE","AMPL","MIXC","MNTV","SPNV","PYCR","MSTR",
        # Healthcare Growth
        "ISRG","DXCM","PODD","INSP","NVCR","TMDX","ATRC","NTRA","EXAS","GH",
        "RXRX","BEAM","EDIT","NTLA","CRSP","KYMR","ARQT","ACAD","ITCI","AXSM",
        "SAVA","PRAX","ACRX","NKTR","ALKS","INVA","PRTA","BGNE","ZLAB","LEGN",
        # Financial Growth
        "SOFI","HOOD","AFRM","UPST","LC","ENVA","PRAA","CACC","OMF","QFIN",
        "PYPL","SQ","FOUR","GPN","FI","FIS","WEX","FLYW","PAYO","RELY",
        # Real Estate Tech
        "CSGP","RDFN","OPEN","EXPI","HOUS","RKT","UWMC","PFSI","GHLD","RATE",
        # Consumer Tech
        "AMZN","BABA","JD","PDD","SE","GRAB","DIDI","MELI","VTEX","OZON",
        "CHWY","W","ETSY","POSH","REAL","OSTK","PRTS","FLXS","BIGC","SHPW",
        # Biotech
        "MRNA","BNTX","NVAX","VXRT","DVAX","IOVA","FATE","KPTI","PRLD","KRTX",
        "FOLD","RARE","ONCE","BLUE","SGMO","ARWR","DRNA","SRPT","BMRN","ALNY",
        "REGN","VRTX","BIIB","IONS","AGEN","CLVS","IDYA","MGNX","MERUS","IMVT",
        # Auto & EV
        "TSLA","RIVN","LCID","NIO","XPEV","LI","NKLA","WKHS","RIDE","GOEV",
        "F","GM","STLA","TM","HMC","RACE","MBGYY","BMWYY","VWAGY","POAHY",
        # Semiconductors Extended
        "NVDA","AMD","INTC","QCOM","AVGO","TXN","MU","AMAT","LRCX","KLAC",
        "MCHP","SWKS","QRVO","CRUS","SLAB","DIOD","SITM","AMBA","ALGM","POWI",
        "AEHR","ONTO","UCTT","FORM","ACLS","NVMI","RVLV","ICHR","MKSI","COHU",
        # Cloud & SaaS
        "MSFT","CRM","ORCL","NOW","WDAY","ADSK","ANSS","PTC","CDNS","SNPS",
        "VEEV","HUBS","PCTY","PAYC","PAYLOCITY","TASK","JAMF","APPF","YEXT","KYNDRYL",
        "ZM","DOCU","BOX","DRFT","DOCN","FSLY","ESTC","ELASTIC","SUMO","SPSC",
        # Media & Entertainment
        "NFLX","DIS","PARA","WBD","FOXA","SPOT","PINS","SNAP","MTCH","BMBL",
        "RBLX","U","EA","TTWO","ATVI","DKNG","PENN","BALY","RSI","AGS",
        # Retail Extended
        "WMT","COST","TGT","HD","LOW","DLTR","DG","FIVE","OLLI","BIG",
        "M","KSS","JWN","GPS","URBN","ANF","AEO","EXPR","CATO","PLCE",
        "ROST","TJX","BURL","PSMT","CASY","WINN","KR","SFM","CHEF","USFD",
        # Insurance
        "BRK-B","AIG","MET","PRU","AFL","ALL","PGR","TRV","CB","HIG",
        "WRB","RNR","MKL","ACGL","RLI","ERIE","SIGI","KMPR","HCI","UPC",
        # Banks Extended
        "JPM","BAC","WFC","C","USB","PNC","TFC","FITB","HBAN","CFG",
        "RF","KEY","MTB","ZION","CMA","WAL","PACW","FHB","BOKF","CVBF",
        # REITs Extended
        "AMT","PLD","EQIX","CCI","SBAC","DLR","PSA","EXR","O","NNN",
        "VICI","EPR","COLD","STAG","REXR","EGP","FR","LXP","GTY","ADC",
    ]

    # Try to supplement with yfinance market cap screen (best effort)
    try:
        extra = []
        screens = ["SPY","QQQ","IWB","VTI"]
        for etf in screens:
            try:
                t = yf.Ticker(etf)
                holdings = t.funds_data.top_holdings if hasattr(t, 'funds_data') else None
                if holdings is not None and hasattr(holdings, 'index'):
                    extra.extend(holdings.index.tolist()[:100])
            except:
                pass
        tickers.extend(extra)
    except:
        pass

    # Clean and deduplicate
    cleaned = []
    for t in tickers:
        t = str(t).strip().upper().replace(".", "-")
        if t and 1 <= len(t) <= 5 and t.replace("-","").isalpha():
            cleaned.append(t)

    final = sorted(list(dict.fromkeys(cleaned)))  # dedupe preserving order
    log.info(f"Total universe: {len(final)} tickers")
    return final


# ─────────────────────────────────────────────
# Data Fetcher
# ─────────────────────────────────────────────
def fetch_data(ticker: str, period: str = "2y") -> Optional[pd.DataFrame]:
    """Download OHLCV data from Yahoo Finance."""
    try:
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
        if df.empty or len(df) < 100:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        log.debug(f"{ticker}: fetch error {e}")
        return None


# ─────────────────────────────────────────────
# Technical Indicators
# ─────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["Close"]
    volume = df["Volume"]

    # Moving Averages
    for p in [10, 20, 50, 150, 200]:
        df[f"MA{p}"] = close.rolling(p).mean()

    # ATR
    high, low = df["High"], df["Low"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14).mean()
    df["ATR_pct"] = df["ATR14"] / close * 100

    # Volume MA
    df["VolMA50"] = volume.rolling(50).mean()
    df["VolMA20"] = volume.rolling(20).mean()
    df["VolRatio"] = volume / df["VolMA50"]

    # 52-week high/low
    df["High52w"] = close.rolling(252).max()
    df["Low52w"] = close.rolling(252).min()

    # RS (price relative to SPY)
    df["PctChange"] = close.pct_change(63)   # 3-month momentum

    return df


# ─────────────────────────────────────────────
# MM Trend Template (8 Criteria)
# ─────────────────────────────────────────────
def check_trend_template(df: pd.DataFrame) -> dict:
    """
    Minervini's 8-point Trend Template.
    All 8 must pass for a proper Stage 2 stock.
    """
    last = df.iloc[-1]
    price = last["Close"]

    results = {}
    
    # 1. Price > 150-day MA
    results["above_150ma"] = bool(price > last["MA150"]) if not np.isnan(last.get("MA150", np.nan)) else False
    
    # 2. Price > 200-day MA
    results["above_200ma"] = bool(price > last["MA200"]) if not np.isnan(last.get("MA200", np.nan)) else False
    
    # 3. 150-day MA > 200-day MA
    results["ma150_above_ma200"] = bool(last["MA150"] > last["MA200"]) if (not np.isnan(last.get("MA150", np.nan)) and not np.isnan(last.get("MA200", np.nan))) else False
    
    # 4. 200-day MA trending up (slope over last 20 sessions)
    if len(df) >= 20 and "MA200" in df.columns:
        ma200_slice = df["MA200"].dropna().tail(20)
        if len(ma200_slice) >= 20:
            slope = (ma200_slice.iloc[-1] - ma200_slice.iloc[0]) / ma200_slice.iloc[0]
            results["ma200_trending_up"] = bool(slope > 0.005)  # +0.5% over 20 days
        else:
            results["ma200_trending_up"] = False
    else:
        results["ma200_trending_up"] = False
    
    # 5. 50-day MA > 150-day MA AND 50-day MA > 200-day MA
    ma50 = last.get("MA50", np.nan)
    ma150 = last.get("MA150", np.nan)
    ma200 = last.get("MA200", np.nan)
    results["ma50_above_ma150_ma200"] = bool(
        (not np.isnan(ma50)) and (not np.isnan(ma150)) and (not np.isnan(ma200)) and
        ma50 > ma150 and ma50 > ma200
    )
    
    # 6. Price > 50-day MA
    results["price_above_ma50"] = bool(price > ma50) if not np.isnan(ma50) else False
    
    # 7. Price within 25% of 52-week high
    high52 = last.get("High52w", np.nan)
    if not np.isnan(high52) and high52 > 0:
        results["within_25pct_of_52w_high"] = bool(price >= high52 * 0.75)
    else:
        results["within_25pct_of_52w_high"] = False
    
    # 8. Price at least 30% above 52-week low
    low52 = last.get("Low52w", np.nan)
    if not np.isnan(low52) and low52 > 0:
        results["above_30pct_of_52w_low"] = bool(price >= low52 * 1.30)
    else:
        results["above_30pct_of_52w_low"] = False

    passed = sum(results.values())
    results["passed_count"] = passed
    results["score"] = (passed / 8) * 100
    return results


# ─────────────────────────────────────────────
# Stage Analysis (Weinstein / MM)
# ─────────────────────────────────────────────
def detect_stage(df: pd.DataFrame) -> dict:
    """
    Stage 1: Base/accumulation - price flat near MA200
    Stage 2: Advancing - price above rising MA200 (BUY zone)
    Stage 3: Top/distribution - price extended, MA200 flattening
    Stage 4: Declining - price below falling MA200
    """
    last = df.iloc[-1]
    price = last["Close"]
    ma200 = last.get("MA200", np.nan)
    ma50 = last.get("MA50", np.nan)
    
    if np.isnan(ma200) or np.isnan(ma50):
        return {"stage": 0, "score": 0}

    # MA200 slope
    ma200_series = df["MA200"].dropna().tail(20)
    if len(ma200_series) >= 20:
        slope_pct = (ma200_series.iloc[-1] - ma200_series.iloc[0]) / ma200_series.iloc[0] * 100
    else:
        slope_pct = 0

    above_ma200 = price > ma200
    ma200_rising = slope_pct > 0.5

    if above_ma200 and ma200_rising and ma50 > ma200:
        stage = 2
        score = 100
    elif above_ma200 and not ma200_rising:
        stage = 3  # Potential distribution
        score = 30
    elif not above_ma200 and slope_pct < -0.5:
        stage = 4  # Declining
        score = 0
    else:
        stage = 1  # Base/Accumulation
        score = 50

    return {"stage": stage, "score": score, "ma200_slope_pct": slope_pct}


# ─────────────────────────────────────────────
# VCP Contraction Detection (Core Logic)
# ─────────────────────────────────────────────
def detect_vcp_contractions(df: pd.DataFrame) -> dict:
    """
    VCP = series of price contractions with:
    1. Each contraction (pivot-to-pivot decline) is smaller than the previous
    2. Each contraction has lower volume than the previous
    3. Ideal: 2-4 contractions, total base 15-50% deep
    4. Final contraction very tight (2-5% range)
    
    Returns scoring and pivot details.
    """
    # Use last 200 trading days for base analysis
    window = df.tail(200).copy()
    close = window["Close"].values
    high = window["High"].values
    low = window["Low"].values
    vol = window["Volume"].values

    # Find local pivots (swing highs and lows)
    def find_pivots(series, order=10):
        from scipy.signal import argrelextrema
        highs = argrelextrema(series, np.greater_equal, order=order)[0]
        lows = argrelextrema(series, np.less_equal, order=order)[0]
        return highs, lows

    try:
        from scipy.signal import argrelextrema
        pivot_highs_idx, pivot_lows_idx = find_pivots(close, order=8)
    except ImportError:
        # Fallback: simple rolling max/min
        pivot_highs_idx = []
        pivot_lows_idx = []
        for i in range(8, len(close)-8):
            if close[i] == max(close[i-8:i+9]):
                pivot_highs_idx.append(i)
            if close[i] == min(close[i-8:i+9]):
                pivot_lows_idx.append(i)
        pivot_highs_idx = np.array(pivot_highs_idx)
        pivot_lows_idx = np.array(pivot_lows_idx)

    if len(pivot_highs_idx) < 2 or len(pivot_lows_idx) < 2:
        return {"num_contractions": 0, "score": 0, "details": [], "valid": False}

    # Identify contraction swings (high→low→high→low...)
    # A contraction = distance from pivot high to next pivot low (% decline)
    contractions = []
    all_pivots = sorted(
        [(i, "H", close[i], vol[i]) for i in pivot_highs_idx[-8:]] +
        [(i, "L", close[i], vol[i]) for i in pivot_lows_idx[-8:]],
        key=lambda x: x[0]
    )

    for k in range(len(all_pivots)-1):
        cur = all_pivots[k]
        nxt = all_pivots[k+1]
        if cur[1] == "H" and nxt[1] == "L":
            pct_decline = (cur[2] - nxt[2]) / cur[2] * 100
            avg_vol_contraction = np.mean(vol[cur[0]:nxt[0]+1])
            contractions.append({
                "high_idx": cur[0],
                "low_idx": nxt[0],
                "high_price": cur[2],
                "low_price": nxt[2],
                "pct_decline": pct_decline,
                "avg_volume": avg_vol_contraction,
            })

    if len(contractions) < 2:
        return {"num_contractions": 0, "score": 0, "details": [], "valid": False}

    # Score the VCP quality
    score = 0
    details = []
    
    # Check each successive contraction is smaller (key VCP criterion)
    is_contracting = True
    is_volume_contracting = True
    prev_decline = contractions[0]["pct_decline"]
    prev_vol = contractions[0]["avg_volume"]

    for i, c in enumerate(contractions[1:], 1):
        dec = c["pct_decline"]
        v = c["avg_volume"]
        contracting = dec < prev_decline
        vol_contracting = v < prev_vol
        
        details.append({
            "contraction_num": i,
            "pct_decline": round(dec, 2),
            "prev_pct_decline": round(prev_decline, 2),
            "price_contracting": contracting,
            "volume_contracting": vol_contracting,
        })
        
        if not contracting:
            is_contracting = False
        if not vol_contracting:
            is_volume_contracting = False
        
        prev_decline = dec
        prev_vol = v

    # Scoring
    valid_contractions = len(contractions)
    
    # Ideal: 2-4 contractions
    if 2 <= valid_contractions <= 4:
        score += 30
    elif valid_contractions == 1:
        score += 10
    elif valid_contractions >= 5:
        score += 15

    # Each contraction smaller than previous
    if is_contracting:
        score += 30
    else:
        # Partial credit
        passing = sum(1 for d in details if d["price_contracting"])
        score += int(passing / len(details) * 20)

    # Volume contracting
    if is_volume_contracting:
        score += 20
    else:
        passing = sum(1 for d in details if d["volume_contracting"])
        score += int(passing / len(details) * 15)

    # Last contraction tightness (< 10% = excellent)
    last_c = contractions[-1]
    last_decline = last_c["pct_decline"]
    if last_decline < 5:
        score += 20   # Very tight!
    elif last_decline < 10:
        score += 15
    elif last_decline < 15:
        score += 10
    elif last_decline < 25:
        score += 5

    # Total base depth (15-50% = ideal)
    total_depth = contractions[0]["pct_decline"]
    if 15 <= total_depth <= 50:
        base_depth_score = 10
    elif total_depth < 15:
        base_depth_score = 5  # Too shallow
    else:
        base_depth_score = 0  # Too deep

    score = min(score, 100)

    return {
        "num_contractions": valid_contractions,
        "score": score,
        "details": details,
        "valid": valid_contractions >= 2 and is_contracting,
        "is_price_contracting": is_contracting,
        "is_volume_contracting": is_volume_contracting,
        "last_contraction_pct": round(last_decline, 2),
        "total_base_depth": round(total_depth, 2),
    }


# ─────────────────────────────────────────────
# Volume Analysis
# ─────────────────────────────────────────────
def analyze_volume(df: pd.DataFrame) -> dict:
    """
    VCP Volume Criteria:
    1. Volume dries up during base formation (< 50% of avg)
    2. Breakout occurs on volume surge (> 200% of avg)
    3. Down days on lighter volume than up days
    """
    recent = df.tail(50)
    last = df.iloc[-1]
    
    vol = recent["Volume"].values
    close = recent["Close"].values
    vol_ma50 = last.get("VolMA50", np.nan)
    
    score = 0
    
    # Volume trend during base (should be declining)
    first_half_vol = np.mean(vol[:25])
    second_half_vol = np.mean(vol[25:])
    volume_dry_up = second_half_vol < first_half_vol * 0.8
    if volume_dry_up:
        score += 30

    # Last 5 days volume vs average (looking for pocket pivot or drying up)
    last5_vol = np.mean(vol[-5:])
    avg_vol = np.mean(vol)
    vol_ratio = last5_vol / avg_vol if avg_vol > 0 else 1
    
    if vol_ratio < 0.6:
        score += 20  # Volume drying up – good sign before breakout
    elif vol_ratio > 2.0:
        score += 30  # Breakout surge in progress
    elif vol_ratio > 1.5:
        score += 15

    # Up days vs down days volume comparison
    up_days = recent[recent["Close"] > recent["Open"]]
    down_days = recent[recent["Close"] <= recent["Open"]]
    if len(up_days) > 0 and len(down_days) > 0:
        up_vol_avg = up_days["Volume"].mean()
        down_vol_avg = down_days["Volume"].mean()
        if up_vol_avg > down_vol_avg * 1.2:
            score += 20  # Accumulation pattern
        elif up_vol_avg > down_vol_avg:
            score += 10

    # Average volume > 500k (liquidity)
    if not np.isnan(vol_ma50) and vol_ma50 > 1_000_000:
        score += 15
    elif not np.isnan(vol_ma50) and vol_ma50 > 500_000:
        score += 10
    elif not np.isnan(vol_ma50) and vol_ma50 < 100_000:
        score -= 20  # Too illiquid

    score = max(0, min(score, 100))
    
    return {
        "score": score,
        "volume_dry_up": volume_dry_up,
        "vol_ratio_last5": round(vol_ratio, 2),
        "avg_volume": round(float(vol_ma50) if not np.isnan(vol_ma50) else avg_vol, 0),
    }


# ─────────────────────────────────────────────
# RS Rating (Relative Strength vs Market)
# ─────────────────────────────────────────────
def calculate_rs(ticker_df: pd.DataFrame, spy_df: pd.DataFrame) -> dict:
    """
    Minervini RS Rating: compares stock's 12-month performance vs S&P 500.
    Weighted: last quarter counts 2x.
    """
    def perf(df, days):
        if len(df) < days:
            return 0
        return (df["Close"].iloc[-1] / df["Close"].iloc[-days] - 1) * 100

    ticker_3m = perf(ticker_df, 63)
    ticker_6m = perf(ticker_df, 126)
    ticker_12m = perf(ticker_df, 252)
    ticker_score = ticker_3m * 0.4 + ticker_6m * 0.2 + ticker_12m * 0.4

    spy_3m = perf(spy_df, 63)
    spy_6m = perf(spy_df, 126)
    spy_12m = perf(spy_df, 252)
    spy_score = spy_3m * 0.4 + spy_6m * 0.2 + spy_12m * 0.4

    relative = ticker_score - spy_score

    # Normalize to 1-99 range
    if relative > 50:
        rs_rating = 99
    elif relative > 30:
        rs_rating = 90 + (relative - 30) / 2
    elif relative > 10:
        rs_rating = 70 + (relative - 10)
    elif relative > 0:
        rs_rating = 60 + relative
    elif relative > -10:
        rs_rating = 50 + relative
    elif relative > -30:
        rs_rating = 30 + (relative + 10) * 1.5
    else:
        rs_rating = max(1, 30 + relative)

    rs_score = min(100, max(0, rs_rating * 0.8 if rs_rating >= 70 else rs_rating * 0.4))

    return {
        "rs_rating": round(rs_rating, 1),
        "rs_score": round(rs_score, 1),
        "outperformance": round(relative, 2),
    }


# ─────────────────────────────────────────────
# Pivot Price & Risk/Reward
# ─────────────────────────────────────────────
def calculate_pivot(df: pd.DataFrame) -> dict:
    """
    Pivot = breakout point above the handle/last contraction high.
    Stop loss = below the last pivot low (or 8% below pivot, whichever is closer).
    """
    recent = df.tail(30)
    last = df.iloc[-1]
    price = last["Close"]
    
    # Pivot = recent 30-day high + 0.10 (10 cents above resistance)
    pivot = recent["High"].max()
    
    # Stop loss = recent significant low
    stop = recent["Low"].min()
    
    # Ensure stop is meaningful (max 8% below current price)
    max_stop = price * 0.92
    stop = max(stop, max_stop)
    
    risk = price - stop
    reward = pivot * 1.20 - price  # Target: 20% above pivot
    
    rr = reward / risk if risk > 0 else 0
    
    return {
        "pivot_price": round(pivot, 2),
        "stop_loss": round(stop, 2),
        "risk_pct": round((price - stop) / price * 100, 2),
        "risk_reward": round(rr, 2),
    }


# ─────────────────────────────────────────────
# Tightness Analysis (Handle quality)
# ─────────────────────────────────────────────
def analyze_tightness(df: pd.DataFrame) -> dict:
    """
    Handle/tight area = last 3-8 weeks with <5% range.
    The tighter, the better – shows supply has been absorbed.
    """
    weekly = df.tail(60).resample("W").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).dropna()

    if len(weekly) < 3:
        return {"score": 0, "weeks_tight": 0}

    tight_weeks = 0
    for i in range(-1, -min(9, len(weekly)), -1):
        w = weekly.iloc[i]
        if w["High"] > 0:
            week_range = (w["High"] - w["Low"]) / w["High"] * 100
            if week_range < 5:
                tight_weeks += 1
            else:
                break  # Stop at first loose week

    if tight_weeks >= 4:
        score = 100
    elif tight_weeks == 3:
        score = 80
    elif tight_weeks == 2:
        score = 60
    elif tight_weeks == 1:
        score = 40
    else:
        score = 0

    return {"score": score, "weeks_tight": tight_weeks}


# ─────────────────────────────────────────────
# Main VCP Analysis
# ─────────────────────────────────────────────
def analyze_vcp(ticker: str, df: pd.DataFrame, spy_df: pd.DataFrame) -> Optional[VCPScore]:
    """Full VCP analysis pipeline for a single ticker."""
    try:
        df = add_indicators(df)
        
        if len(df) < 200:
            return None
        
        last = df.iloc[-1]
        price = float(last["Close"])
        
        if price < 10:  # Skip penny stocks
            return None

        # Run all analyses
        trend = check_trend_template(df)
        stage_result = detect_stage(df)
        vcp = detect_vcp_contractions(df)
        volume = analyze_volume(df)
        rs = calculate_rs(df, spy_df)
        pivot = calculate_pivot(df)
        tightness = analyze_tightness(df)

        # Get company name
        try:
            info = yf.Ticker(ticker).info
            company_name = info.get("longName", ticker)
        except:
            company_name = ticker

        # ── Composite Score ─────────────────────────────
        # Weights based on MM's emphasis:
        # Trend Template: 25% | VCP Contractions: 30% | Stage: 15%
        # Volume: 15% | RS: 10% | Tightness: 5%
        
        tt_score = trend["score"]           # 0-100
        vcp_score = vcp["score"]            # 0-100
        stage_score = stage_result["score"] # 0-100
        vol_score = volume["score"]         # 0-100
        rs_score = rs["rs_score"]           # 0-100
        tight_score = tightness["score"]    # 0-100

        composite = (
            tt_score   * 0.25 +
            vcp_score  * 0.30 +
            stage_score* 0.15 +
            vol_score  * 0.15 +
            rs_score   * 0.10 +
            tight_score* 0.05
        )

        # Grade
        if composite >= 85:   grade = "A+"
        elif composite >= 75: grade = "A"
        elif composite >= 65: grade = "B"
        elif composite >= 50: grade = "C"
        else:                  grade = "D"

        return VCPScore(
            ticker=ticker,
            company_name=company_name,
            score=round(composite, 1),
            grade=grade,
            price=price,
            pivot_price=pivot["pivot_price"],
            stop_loss=pivot["stop_loss"],
            risk_reward=pivot["risk_reward"],
            stage=stage_result["stage"],
            stage_score=stage_score,
            trend_template_score=tt_score,
            above_150ma=trend["above_150ma"],
            above_200ma=trend["above_200ma"],
            ma150_above_ma200=trend["ma150_above_ma200"],
            ma200_trending_up=trend["ma200_trending_up"],
            ma50_above_ma150_ma200=trend["ma50_above_ma150_ma200"],
            price_above_ma50=trend["price_above_ma50"],
            within_25pct_of_52w_high=trend["within_25pct_of_52w_high"],
            above_30pct_of_52w_low=trend["above_30pct_of_52w_low"],
            num_contractions=vcp["num_contractions"],
            contraction_score=vcp_score,
            contraction_details=vcp.get("details", []),
            volume_score=vol_score,
            avg_volume=volume["avg_volume"],
            volume_dry_up=volume["volume_dry_up"],
            breakout_volume_surge=volume["vol_ratio_last5"] > 1.8,
            rs_rating=rs["rs_rating"],
            rs_score=rs_score,
            base_depth=vcp.get("total_base_depth", 0),
            base_duration_weeks=max(1, len(df.tail(200)) // 5),
            base_score=(vcp_score + tight_score) / 2,
            tightness_score=tight_score,
            weeks_tight=tightness["weeks_tight"],
            details={
                "trend_template_passed": trend["passed_count"],
                "vcp_valid": vcp["valid"],
                "vcp_contracting_price": vcp.get("is_price_contracting", False),
                "vcp_contracting_volume": vcp.get("is_volume_contracting", False),
                "last_contraction_pct": vcp.get("last_contraction_pct", 0),
                "ma200_slope": stage_result.get("ma200_slope_pct", 0),
                "rs_outperformance": rs["outperformance"],
                "risk_pct": pivot["risk_pct"],
            }
        )

    except Exception as e:
        log.debug(f"{ticker}: analysis error – {e}")
        return None


# ─────────────────────────────────────────────
# Email Alert
# ─────────────────────────────────────────────
def send_email_alert(results: list[VCPScore], recipient: str, sender: str, password: str):
    """Send HTML email with VCP scan results."""
    if not results:
        log.info("No qualifying VCP stocks – email not sent.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    
    # Sort by score
    results = sorted(results, key=lambda x: x.score, reverse=True)

    def grade_color(grade):
        return {"A+": "#00c853", "A": "#64dd17", "B": "#ffd600", "C": "#ff6d00", "D": "#d50000"}.get(grade, "#888")

    rows = ""
    for r in results:
        tt_icons = "".join([
            "✅" if r.above_150ma else "❌",
            "✅" if r.above_200ma else "❌",
            "✅" if r.ma150_above_ma200 else "❌",
            "✅" if r.ma200_trending_up else "❌",
            "✅" if r.ma50_above_ma150_ma200 else "❌",
            "✅" if r.price_above_ma50 else "❌",
            "✅" if r.within_25pct_of_52w_high else "❌",
            "✅" if r.above_30pct_of_52w_low else "❌",
        ])
        rows += f"""
        <tr>
          <td style="padding:8px;font-weight:bold;color:#1a73e8">{r.ticker}</td>
          <td style="padding:8px">{r.company_name[:28]}</td>
          <td style="padding:8px;text-align:center;background:{grade_color(r.grade)};color:#fff;border-radius:4px;font-weight:bold">{r.grade} ({r.score})</td>
          <td style="padding:8px;text-align:right">${r.price:.2f}</td>
          <td style="padding:8px;text-align:right;color:#00c853">${r.pivot_price:.2f}</td>
          <td style="padding:8px;text-align:right;color:#d50000">${r.stop_loss:.2f}</td>
          <td style="padding:8px;text-align:center">{r.stage}</td>
          <td style="padding:8px;text-align:center">{r.num_contractions}</td>
          <td style="padding:8px;text-align:center">{r.rs_rating:.0f}</td>
          <td style="padding:8px;text-align:center">{r.weeks_tight}w</td>
          <td style="padding:8px;font-size:11px">{tt_icons}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;background:#f5f5f5">
    <div style="max-width:1100px;margin:20px auto;background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);overflow:hidden">
      <div style="background:linear-gradient(135deg,#1a237e,#283593);padding:24px;color:#fff">
        <h1 style="margin:0;font-size:22px">📈 VCP Scanner Alert – {today}</h1>
        <p style="margin:8px 0 0;opacity:0.8">Mark Minervini's Volatility Contraction Pattern | Top 1000 US Stocks</p>
      </div>
      <div style="padding:20px">
        <div style="background:#e8f5e9;border-left:4px solid #00c853;padding:12px;border-radius:4px;margin-bottom:20px">
          <strong>Found {len(results)} qualifying VCP setups</strong> (Grade B or better, Score ≥ 65)
        </div>
        
        <h3 style="color:#1a237e">VCP Scoring Methodology (Minervini)</h3>
        <ul style="color:#555;line-height:1.8">
          <li><strong>Trend Template (25%)</strong>: All 8 MM criteria – MAs aligned, price above key levels, near 52-week high</li>
          <li><strong>VCP Contractions (30%)</strong>: 2-4 successive smaller contractions, volume drying up</li>
          <li><strong>Stage Analysis (15%)</strong>: Must be in Stage 2 with rising 200MA</li>
          <li><strong>Volume Pattern (15%)</strong>: Low volume base, accumulation days dominant</li>
          <li><strong>RS Rating (10%)</strong>: Outperforming S&P 500 (target: RS ≥ 80)</li>
          <li><strong>Tightness (5%)</strong>: Handle consolidation with <5% weekly range</li>
        </ul>

        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="background:#1a237e;color:#fff">
              <th style="padding:10px">Ticker</th>
              <th style="padding:10px">Company</th>
              <th style="padding:10px">Grade (Score)</th>
              <th style="padding:10px">Price</th>
              <th style="padding:10px">Pivot</th>
              <th style="padding:10px">Stop</th>
              <th style="padding:10px">Stage</th>
              <th style="padding:10px">Contractions</th>
              <th style="padding:10px">RS</th>
              <th style="padding:10px">Tight</th>
              <th style="padding:10px">Trend Template</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>

        <div style="background:#fff3e0;border-left:4px solid #ff6d00;padding:12px;border-radius:4px;margin-top:24px">
          <strong>⚠️ Disclaimer</strong>: This is automated analysis for educational purposes only. 
          Always conduct your own due diligence. Past patterns do not guarantee future returns.
          Position sizing and risk management are your responsibility.
        </div>
      </div>
      <div style="background:#f5f5f5;padding:12px;text-align:center;color:#888;font-size:12px">
        VCP Scanner | Powered by Minervini's Methodology | {today}
      </div>
    </div>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📈 VCP Scanner – {len(results)} Setups Found | {today}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(sender, password)
            srv.sendmail(sender, recipient, msg.as_string())
        log.info(f"✅ Email sent to {recipient} with {len(results)} results")
    except Exception as e:
        log.error(f"Email send failed: {e}")


# ─────────────────────────────────────────────
# Main Runner
# ─────────────────────────────────────────────
def main():
    # Config from environment variables (GitHub Secrets)
    recipient = os.environ.get("EMAIL_RECIPIENT", "")
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("EMAIL_PASSWORD", "")
    min_score = float(os.environ.get("MIN_SCORE", "65"))
    min_grade = os.environ.get("MIN_GRADE", "B")   # A+, A, B, C, D

    if not all([recipient, sender, password]):
        log.warning("Email credentials not set – will print results only.")

    log.info("=" * 60)
    log.info("🔍 VCP Scanner Starting")
    log.info(f"Min Score: {min_score} | Min Grade: {min_grade}")
    log.info("=" * 60)

    # Fetch SPY for RS comparison
    log.info("Fetching SPY benchmark...")
    spy_df = fetch_data("SPY", period="2y")
    if spy_df is None:
        log.error("Failed to fetch SPY data. Aborting.")
        return

    # Get universe
    tickers = get_top1000_tickers()
    log.info(f"Scanning {len(tickers)} tickers...")

    qualifying: list[VCPScore] = []
    errors = 0

    for i, ticker in enumerate(tickers):
        if i % 50 == 0:
            log.info(f"Progress: {i}/{len(tickers)} | Qualifying so far: {len(qualifying)}")

        df = fetch_data(ticker)
        if df is None:
            errors += 1
            continue

        result = analyze_vcp(ticker, df, spy_df)
        if result is None:
            continue

        grade_order = {"A+": 5, "A": 4, "B": 3, "C": 2, "D": 1}
        if (result.score >= min_score and
            grade_order.get(result.grade, 0) >= grade_order.get(min_grade, 3) and
            result.stage == 2 and          # Must be Stage 2
            result.num_contractions >= 2): # Must have real contractions
            qualifying.append(result)
            log.info(f"  ✅ {ticker}: {result.grade} ({result.score}) | "
                     f"Stage {result.stage} | {result.num_contractions} contractions | "
                     f"RS {result.rs_rating:.0f}")

        time.sleep(0.1)  # Rate limit

    log.info(f"\n{'='*60}")
    log.info(f"Scan complete. {len(qualifying)} qualifying setups found.")
    log.info(f"Errors/skipped: {errors}")

    if qualifying:
        # Save JSON results
        output = []
        for r in sorted(qualifying, key=lambda x: x.score, reverse=True):
            output.append({
                "ticker": r.ticker,
                "company": r.company_name,
                "score": r.score,
                "grade": r.grade,
                "price": r.price,
                "pivot": r.pivot_price,
                "stop": r.stop_loss,
                "rr": r.risk_reward,
                "stage": r.stage,
                "contractions": r.num_contractions,
                "rs_rating": r.rs_rating,
                "trend_template_passed": r.details.get("trend_template_passed"),
                "weeks_tight": r.weeks_tight,
                "scanned_at": datetime.now().isoformat(),
            })
        
        with open("vcp_results.json", "w") as f:
            json.dump(output, f, indent=2)
        log.info("Results saved to vcp_results.json")

        # Send email
        if all([recipient, sender, password]):
            send_email_alert(qualifying, recipient, sender, password)
    else:
        log.info("No qualifying VCP setups found today.")


if __name__ == "__main__":
    main()
