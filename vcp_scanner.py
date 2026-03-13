"""
VCP Scanner v2 — STRICT Minervini SEPA Methodology
====================================================
Based on Mark Minervini's complete framework from:
  - "Trade Like a Stock Market Wizard" (2013)
  - "Think & Trade Like a Champion" (2017)
  - SEPA (Specific Entry Point Analysis) methodology

STRICT FILTERS (all must pass):
  1. MM Trend Template — ALL 8 criteria (hard pass/fail)
  2. Stage 2 confirmed — MA200 rising slope ≥ 1% over 20 days
  3. Prior uptrend — stock up ≥ 30% before base formation
  4. RS Rating ≥ 70 (outperforming 70%+ of market)
  5. VCP contractions — EACH must be smaller AND on lower volume
  6. Last contraction ≤ 15% (tight base), ideally ≤ 8%
  7. Volume dry-up — last 10-day avg < 50% of 50-day avg
  8. Higher lows during base (institutional accumulation)
  9. Base duration 3–52 weeks (not too short, not too long)
 10. Liquidity — avg volume > 500k shares/day
"""

import os, json, smtplib, logging, time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
import yfinance as yf

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("vcp_scan.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class VCPResult:
    ticker: str
    company: str
    score: float          # 0–100 composite
    grade: str            # A+/A/B/C/D
    price: float
    pivot: float          # breakout level
    stop: float           # stop-loss level
    risk_pct: float       # % risk from current price to stop
    reward_ratio: float   # reward-to-risk

    # ── Trend Template (8 hard criteria) ─────────────────────────
    tt_passed: int        # how many of 8 passed (must be 8)
    tt_score: float
    above_ma50: bool
    above_ma150: bool
    above_ma200: bool
    ma50_gt_ma150: bool
    ma50_gt_ma200: bool
    ma150_gt_ma200: bool
    ma200_rising: bool
    near_52w_high: bool   # within 25%
    above_52w_low: bool   # 30%+ above

    # ── Stage ────────────────────────────────────────────────────
    stage: int
    ma200_slope_20d: float   # % slope over 20 days

    # ── Prior uptrend ────────────────────────────────────────────
    prior_uptrend_pct: float  # % gain before base

    # ── VCP contractions ─────────────────────────────────────────
    num_contractions: int
    contractions: list        # list of {pct, vol_ratio}
    all_price_contracting: bool
    all_volume_contracting: bool
    last_contraction_pct: float
    vcp_score: float

    # ── Volume ───────────────────────────────────────────────────
    vol_dry_up: bool
    vol_dry_up_ratio: float   # last10 / ma50 (want < 0.5)
    avg_daily_vol: float
    vol_score: float

    # ── Higher lows ──────────────────────────────────────────────
    higher_lows: bool

    # ── Base metrics ─────────────────────────────────────────────
    base_weeks: int
    base_depth: float

    # ── RS ───────────────────────────────────────────────────────
    rs_rating: float     # 1–99
    rs_score: float

    # ── Tightness ────────────────────────────────────────────────
    weeks_tight: int
    tight_score: float

    # ── Disqualification reasons ─────────────────────────────────
    disqualified: bool
    dq_reasons: list = field(default_factory=list)


# ── Stock universe ────────────────────────────────────────────────────────────
def get_tickers() -> list:
    return [
        # Mega Cap Tech
        "AAPL","MSFT","NVDA","GOOGL","GOOG","AMZN","META","TSLA","AVGO","ORCL",
        "AMD","ADBE","CRM","INTC","QCOM","TXN","MU","AMAT","LRCX","KLAC",
        "SNPS","CDNS","MRVL","ADI","NXPI","ON","MPWR","ENTG","COHR","ARM",
        "SMCI","PLTR","UBER","ABNB","DASH","RBLX","COIN","APP","FICO","AXON",
        # Financials
        "JPM","BAC","WFC","GS","MS","BLK","C","AXP","SPGI","MCO",
        "ICE","CME","SCHW","BK","STT","TFC","USB","PNC","COF","DFS",
        "AIG","MET","PRU","AFL","ALL","PGR","TRV","CB","HIG","WRB",
        "BRK-B","V","MA","PYPL","SQ","AFRM","SOFI","HOOD","UPST","LC",
        "FOUR","GPN","FI","FIS","WEX","ACGL","RNR","MKL","ERIE","RLI",
        # Healthcare
        "LLY","UNH","JNJ","ABBV","MRK","TMO","ABT","DHR","BMY","AMGN",
        "GILD","ISRG","SYK","BSX","MDT","EW","RMD","DXCM","PODD","INSP",
        "REGN","VRTX","BIIB","ALNY","MRNA","PFE","CVS","CI","HUM","ELV",
        "EXAS","ILMN","HOLX","VEEV","NVCR","RXRX","BEAM","CRSP","ALKS","ITCI",
        "AXSM","ACAD","BGNE","ZLAB","LEGN","KYMR","TMDX","ATRC","NTRA","GH",
        # Consumer Discretionary
        "WMT","COST","TGT","HD","LOW","MCD","SBUX","YUM","CMG","WING",
        "CAVA","BROS","TXRH","DRI","NKE","LULU","CROX","DECK","ONON","ELF",
        "CELH","DUOL","NFLX","DIS","SPOT","PINS","SNAP","MTCH","CHWY","W",
        "ETSY","ROST","TJX","BURL","DLTR","DG","FIVE","OLLI","PLCE","ANF",
        # Industrials
        "GE","HON","CAT","DE","RTX","LMT","NOC","GD","BA","TDG",
        "HEI","LDOS","SAIC","CACI","BAH","UPS","FDX","ODFL","SAIA","XPO",
        "CSX","UNP","NSC","CP","CNI","GNRC","IR","CARR","OTIS","ETN",
        "AME","ROP","VRSK","ITW","PH","EMR","ROK","JCI","MMM","HON",
        # Energy
        "XOM","CVX","COP","EOG","SLB","HAL","MPC","VLO","PSX","OXY",
        "DVN","HES","MRO","CTRA","SM","MTDR","AR","RRC","EQT","NOG",
        # Tech Growth / SaaS / Cybersecurity
        "NOW","WDAY","ADSK","ANSS","PTC","HUBS","PCTY","PAYC","VEEV","ZM",
        "DOCU","BOX","DOCN","SHOP","NET","DDOG","SNOW","MDB","TWLO","ZS",
        "OKTA","CFLT","GTLB","BILL","TOST","IOT","PANW","FTNT","CRWD","S",
        "TENB","QLYS","BRZE","ZI","MSTR","ESTC","SPSC","APPF","JAMF","TASK",
        # Semiconductors extended
        "MCHP","SWKS","QRVO","CRUS","SLAB","DIOD","SITM","AMBA","POWI",
        "AEHR","ONTO","UCTT","FORM","ACLS","NVMI","ICHR","MKSI","COHU","ALGM",
        # Consumer Tech / E-commerce
        "MELI","SE","GRAB","PDD","BABA","JD","BEKE","FUTU","MNSO",
        # REITs
        "AMT","PLD","EQIX","CCI","SBAC","DLR","PSA","EXR","O","VICI",
        "STAG","REXR","EGP","FR","COLD","ADC","GTY","EPR",
        # Utilities
        "NEE","DUK","SO","AEP","SRE","XEL","WEC","AWK","ES","D",
        # Materials
        "LIN","APD","ECL","SHW","PPG","FCX","NEM","GOLD","ALB","MP",
        "NUE","STLD","CLF","WLK","CE","IFF","EMN",
        # Auto & EV
        "TSLA","RIVN","F","GM","RACE",
        # Gaming & Media
        "EA","TTWO","DKNG","PENN","RBLX",
        # Banks extended
        "FITB","HBAN","CFG","RF","KEY","MTB","ZION","WAL","BOK","CVBF",
        # Insurance extended
        "CB","TRV","ALL","PGR","AFL","AIG","MET","PRU","HIG","WRB",
    ]


# ── Data fetch ────────────────────────────────────────────────────────────────
def fetch(ticker: str, period="2y") -> Optional[pd.DataFrame]:
    try:
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty or len(df) < 150:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return df.dropna(subset=["Close","Volume"])
    except:
        return None


# ── Indicators ────────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
    v = df["Volume"]
    for p in [10, 20, 50, 150, 200]:
        df[f"MA{p}"] = c.rolling(p).mean()
    df["Vol_MA50"] = v.rolling(50).mean()
    df["Vol_MA10"] = v.rolling(10).mean()
    df["High52"]   = c.rolling(252).max()
    df["Low52"]    = c.rolling(252).min()
    # Weekly ATR proxy
    df["ATR14"] = (df["High"] - df["Low"]).rolling(14).mean()
    return df


# ══════════════════════════════════════════════════════════════════════════════
# FILTER 1 — MM Trend Template (ALL 8 must pass — hard requirement)
# ══════════════════════════════════════════════════════════════════════════════
def check_trend_template(df: pd.DataFrame) -> dict:
    r = df.iloc[-1]
    p  = float(r["Close"])
    ma50  = float(r.get("MA50",  np.nan))
    ma150 = float(r.get("MA150", np.nan))
    ma200 = float(r.get("MA200", np.nan))
    h52   = float(r.get("High52", np.nan))
    l52   = float(r.get("Low52",  np.nan))

    def ok(v): return not np.isnan(v)

    c = {}
    c["above_ma50"]    = ok(ma50)  and p > ma50
    c["above_ma150"]   = ok(ma150) and p > ma150
    c["above_ma200"]   = ok(ma200) and p > ma200
    c["ma50_gt_ma150"] = ok(ma50) and ok(ma150) and ma50 > ma150
    c["ma50_gt_ma200"] = ok(ma50) and ok(ma200) and ma50 > ma200
    c["ma150_gt_ma200"]= ok(ma150) and ok(ma200) and ma150 > ma200

    # MA200 must be rising (slope > 1% over 20 days — strict)
    ma200_series = df["MA200"].dropna().tail(21)
    if len(ma200_series) >= 21:
        slope = (ma200_series.iloc[-1] - ma200_series.iloc[0]) / ma200_series.iloc[0] * 100
        c["ma200_rising"] = slope > 1.0   # STRICT: must be +1% over 20 days
    else:
        c["ma200_rising"] = False
        slope = 0.0

    c["near_52w_high"]  = ok(h52) and h52 > 0 and p >= h52 * 0.75
    c["above_52w_low"]  = ok(l52) and l52 > 0 and p >= l52 * 1.30

    passed = sum(c.values())
    return {**c, "passed": passed, "score": passed / 8 * 100, "ma200_slope": slope}


# ══════════════════════════════════════════════════════════════════════════════
# FILTER 2 — Prior Uptrend (stock must have a big move BEFORE the base)
# MM: great VCPs come from stocks that already ran 30–100%+ before base
# ══════════════════════════════════════════════════════════════════════════════
def check_prior_uptrend(df: pd.DataFrame) -> dict:
    """
    Look at the 6–12 months before the current base.
    The stock should have risen ≥ 30% in that period.
    """
    if len(df) < 200:
        return {"valid": False, "pct": 0}

    # Use price 200 days ago vs 100 days ago as proxy for pre-base move
    price_200 = float(df["Close"].iloc[-200])
    price_100 = float(df["Close"].iloc[-100])
    pct = (price_100 - price_200) / price_200 * 100

    return {"valid": pct >= 30.0, "pct": round(pct, 1)}


# ══════════════════════════════════════════════════════════════════════════════
# FILTER 3 — RS Rating vs SPY
# ══════════════════════════════════════════════════════════════════════════════
def calc_rs(df: pd.DataFrame, spy: pd.DataFrame) -> dict:
    def perf(d, n):
        if len(d) < n: return 0.0
        return (float(d["Close"].iloc[-1]) / float(d["Close"].iloc[-n]) - 1) * 100

    # Weighted: recent performance counts more
    s  = perf(df, 63)*0.40 + perf(df, 126)*0.20 + perf(df, 252)*0.40
    sp = perf(spy,63)*0.40 + perf(spy,126)*0.20 + perf(spy,252)*0.40
    rel = s - sp

    # Map to 1–99
    if   rel > 60:  rs = 99
    elif rel > 40:  rs = 90 + (rel-40)/2
    elif rel > 20:  rs = 75 + (rel-20)*0.75
    elif rel > 5:   rs = 60 + (rel-5)
    elif rel > 0:   rs = 55 + rel
    elif rel > -10: rs = 45 + rel
    elif rel > -25: rs = 30 + (rel+10)
    else:           rs = max(1, 20+rel)

    rs = min(99, max(1, rs))
    score = rs  # directly use as score (need ≥70 to qualify)
    return {"rs": round(rs,1), "score": round(score,1), "outperform": round(rel,2)}


# ══════════════════════════════════════════════════════════════════════════════
# FILTER 4 — VCP Contraction Analysis (STRICT)
# Rules:
#   a) EVERY price contraction must be smaller than the previous
#   b) EVERY contraction's volume must be lower than the previous
#   c) 2–4 contractions (5+ is too many, 1 is not enough)
#   d) Last contraction ≤ 15% (ideally ≤ 8%)
#   e) Each contraction forms HIGHER LOWS
# ══════════════════════════════════════════════════════════════════════════════
def detect_vcp(df: pd.DataFrame) -> dict:
    window = df.tail(200).copy()
    close = window["Close"].values.astype(float)
    vol   = window["Volume"].values.astype(float)

    # ── Find swing highs and lows ──────────────────────────────────────────
    order = 8  # look 8 bars each side
    highs_idx, lows_idx = [], []
    for i in range(order, len(close)-order):
        if close[i] == max(close[i-order:i+order+1]):
            highs_idx.append(i)
        if close[i] == min(close[i-order:i+order+1]):
            lows_idx.append(i)

    if len(highs_idx) < 2 or len(lows_idx) < 2:
        return {"valid": False, "score": 0, "reason": "Not enough pivots"}

    # Build high→low pairs (contractions)
    all_pts = sorted(
        [(i,"H",close[i],vol[i]) for i in highs_idx[-10:]] +
        [(i,"L",close[i],vol[i]) for i in lows_idx[-10:]],
        key=lambda x: x[0]
    )

    contractions = []
    for k in range(len(all_pts)-1):
        a, b = all_pts[k], all_pts[k+1]
        if a[1]=="H" and b[1]=="L":
            pct   = (a[2]-b[2])/a[2]*100
            avg_v = float(np.mean(vol[a[0]:b[0]+1]))
            contractions.append({
                "hi": a[2], "lo": b[2],
                "pct": round(pct,2),
                "vol": avg_v,
                "hi_idx": a[0], "lo_idx": b[0]
            })

    if len(contractions) < 2:
        return {"valid": False, "score": 0, "reason": "< 2 contractions"}

    # ── STRICT checks ──────────────────────────────────────────────────────
    score = 0
    all_price_ok = True
    all_vol_ok   = True
    higher_lows  = True
    detail       = []

    for i in range(1, len(contractions)):
        prev, cur = contractions[i-1], contractions[i]
        p_ok = cur["pct"] < prev["pct"]
        v_ok = cur["vol"] < prev["vol"]
        hl   = cur["lo"] > prev["lo"]    # higher low = institutional support

        if not p_ok: all_price_ok = False
        if not v_ok: all_vol_ok   = False
        if not hl:   higher_lows  = False

        detail.append({
            "n": i,
            "pct": cur["pct"], "prev_pct": prev["pct"],
            "price_contracting": p_ok,
            "vol_contracting": v_ok,
            "higher_low": hl
        })

    n = len(contractions)
    last_pct = contractions[-1]["pct"]

    # ── Scoring ────────────────────────────────────────────────────────────
    # Number of contractions (2–4 ideal)
    if   2 <= n <= 4: score += 25
    elif n == 5:      score += 10
    else:             score += 5

    # ALL price contractions smaller (strict)
    if all_price_ok:  score += 30
    else:
        passing = sum(1 for d in detail if d["price_contracting"])
        score += int(passing/len(detail)*15)

    # ALL volume contracting (strict)
    if all_vol_ok:    score += 20
    else:
        passing = sum(1 for d in detail if d["vol_contracting"])
        score += int(passing/len(detail)*10)

    # Higher lows (institutional accumulation)
    if higher_lows:   score += 15

    # Last contraction tightness
    if   last_pct <= 5:  score += 10   # extremely tight
    elif last_pct <= 8:  score += 8
    elif last_pct <= 12: score += 5
    elif last_pct <= 15: score += 2

    valid = (all_price_ok and all_vol_ok and 2 <= n <= 5 and last_pct <= 15)

    return {
        "valid": valid,
        "score": min(score, 100),
        "n": n,
        "all_price_contracting": all_price_ok,
        "all_vol_contracting": all_vol_ok,
        "higher_lows": higher_lows,
        "last_pct": last_pct,
        "total_depth": contractions[0]["pct"],
        "detail": detail,
    }


# ══════════════════════════════════════════════════════════════════════════════
# FILTER 5 — Volume Dry-Up (strict: last 10-day avg < 50% of MA50)
# ══════════════════════════════════════════════════════════════════════════════
def check_volume(df: pd.DataFrame) -> dict:
    last    = df.iloc[-1]
    vol10   = float(df["Volume"].tail(10).mean())
    vol_ma50= float(last.get("Vol_MA50", np.nan))
    if np.isnan(vol_ma50) or vol_ma50 == 0:
        return {"score": 0, "dry_up": False, "ratio": 1.0, "avg": 0}

    ratio    = vol10 / vol_ma50
    dry_up   = ratio < 0.60    # strict: must be < 60% of average

    # Up-day vs down-day volume (accumulation evidence)
    recent = df.tail(30)
    up   = recent[recent["Close"] > recent["Open"]]["Volume"].mean()
    dn   = recent[recent["Close"] <= recent["Open"]]["Volume"].mean()
    accum = up > dn * 1.2 if (not np.isnan(up) and not np.isnan(dn) and dn > 0) else False

    score = 0
    if ratio < 0.40: score += 40    # extreme dry-up
    elif ratio < 0.60: score += 25
    elif ratio < 0.80: score += 10
    if dry_up:   score += 20
    if accum:    score += 25
    if vol_ma50 > 1_000_000: score += 15   # good liquidity
    elif vol_ma50 > 500_000: score += 10
    elif vol_ma50 < 300_000: score -= 20   # illiquid

    return {
        "score": max(0, min(score, 100)),
        "dry_up": dry_up,
        "ratio": round(ratio, 3),
        "avg": round(vol_ma50, 0),
        "accum": accum
    }


# ══════════════════════════════════════════════════════════════════════════════
# FILTER 6 — Tightness (handle quality: consecutive weekly ranges < 5%)
# ══════════════════════════════════════════════════════════════════════════════
def check_tightness(df: pd.DataFrame) -> dict:
    try:
        weekly = df.tail(80).resample("W").agg(
            {"High":"max","Low":"min","Close":"last","Volume":"sum"}
        ).dropna()
    except:
        return {"score": 0, "weeks": 0}

    if len(weekly) < 2:
        return {"score": 0, "weeks": 0}

    tight = 0
    for i in range(-1, -min(10, len(weekly)), -1):
        w = weekly.iloc[i]
        if w["High"] > 0:
            rng = (w["High"]-w["Low"])/w["High"]*100
            if rng < 5:
                tight += 1
            else:
                break  # stop at first loose week

    score = {0:0, 1:30, 2:55, 3:75, 4:90}.get(tight, 100)
    return {"score": score, "weeks": tight}


# ══════════════════════════════════════════════════════════════════════════════
# PIVOT & STOP calculation
# ══════════════════════════════════════════════════════════════════════════════
def calc_pivot_stop(df: pd.DataFrame) -> dict:
    recent = df.tail(30)
    price  = float(df["Close"].iloc[-1])
    pivot  = float(recent["High"].max())        # breakout level
    stop   = float(recent["Low"].tail(15).min())# below handle low

    # MM: stop should not be more than 7-8% below entry
    max_stop = price * 0.93
    stop = max(stop, max_stop)

    risk   = max(price - stop, 0.01)
    target = pivot * 1.25   # conservative 25% target
    rr     = (target - price) / risk

    return {
        "pivot": round(pivot, 2),
        "stop":  round(stop,  2),
        "risk_pct": round((price-stop)/price*100, 2),
        "rr": round(rr, 2)
    }


# ══════════════════════════════════════════════════════════════════════════════
# MASTER ANALYSIS  — runs all filters, applies strict disqualification
# ══════════════════════════════════════════════════════════════════════════════
def analyze(ticker: str, df: pd.DataFrame, spy: pd.DataFrame) -> Optional[VCPResult]:
    try:
        df = add_indicators(df.copy())
        if len(df) < 200:
            return None

        price = float(df["Close"].iloc[-1])
        if price < 10:   # skip penny stocks
            return None

        tt   = check_trend_template(df)
        pu   = check_prior_uptrend(df)
        rs   = calc_rs(df, spy)
        vcp  = detect_vcp(df)
        vol  = check_volume(df)
        tght = check_tightness(df)
        piv  = calc_pivot_stop(df)

        # ── Company name (best effort) ──────────────────────────────────────
        try:
            info = yf.Ticker(ticker).fast_info
            company = getattr(info, "company_name", ticker) or ticker
        except:
            company = ticker

        # ── Disqualification — STRICT gates ────────────────────────────────
        dq_reasons = []

        # Gate 1: ALL 8 Trend Template criteria must pass
        if tt["passed"] < 8:
            failed = [k for k,v in tt.items()
                      if k not in ("passed","score","ma200_slope") and not v]
            dq_reasons.append(f"TrendTemplate {tt['passed']}/8 (failed: {', '.join(failed)})")

        # Gate 2: Stage 2 — MA200 slope must be positive (≥ 1%)
        if tt["ma200_slope"] < 1.0:
            dq_reasons.append(f"MA200 slope too flat ({tt['ma200_slope']:.1f}%)")

        # Gate 3: Prior uptrend ≥ 30%
        if not pu["valid"]:
            dq_reasons.append(f"No prior uptrend (only +{pu['pct']:.1f}%)")

        # Gate 4: RS ≥ 70
        if rs["rs"] < 70:
            dq_reasons.append(f"RS too low ({rs['rs']:.0f} < 70)")

        # Gate 5: VCP must be strictly valid
        if not vcp["valid"]:
            reason = vcp.get("reason", "")
            if not vcp.get("all_price_contracting"):
                reason += " prices not all contracting"
            if not vcp.get("all_vol_contracting"):
                reason += " volume not all contracting"
            if vcp.get("last_pct", 99) > 15:
                reason += f" last contraction too wide ({vcp.get('last_pct',0):.1f}%)"
            dq_reasons.append(f"VCP invalid: {reason.strip()}")

        # Gate 6: Volume must be drying up
        if not vol["dry_up"]:
            dq_reasons.append(f"Volume not drying up (ratio={vol['ratio']:.2f}, need <0.60)")

        # Gate 7: Liquidity
        if vol["avg"] < 300_000:
            dq_reasons.append(f"Illiquid (avg vol {vol['avg']:,.0f})")

        disqualified = len(dq_reasons) > 0

        # ── Composite score (only meaningful if not DQ'd) ───────────────────
        # Weights: VCP 35% | TrendTemplate 25% | Volume 15% | RS 12% | Tightness 8% | PriorUptrend 5%
        composite = (
            vcp["score"]  * 0.35 +
            tt["score"]   * 0.25 +
            vol["score"]  * 0.15 +
            rs["score"]   * 0.12 +
            tght["score"] * 0.08 +
            (100 if pu["valid"] else 0) * 0.05
        )

        if   composite >= 85: grade = "A+"
        elif composite >= 75: grade = "A"
        elif composite >= 65: grade = "B"
        elif composite >= 50: grade = "C"
        else:                  grade = "D"

        # ── Base duration ───────────────────────────────────────────────────
        base_weeks = max(1, len(df.tail(200)) // 5)

        return VCPResult(
            ticker=ticker,
            company=company[:30],
            score=round(composite,1),
            grade=grade,
            price=price,
            pivot=piv["pivot"],
            stop=piv["stop"],
            risk_pct=piv["risk_pct"],
            reward_ratio=piv["rr"],
            tt_passed=tt["passed"],
            tt_score=tt["score"],
            above_ma50=tt["above_ma50"],
            above_ma150=tt["above_ma150"],
            above_ma200=tt["above_ma200"],
            ma50_gt_ma150=tt["ma50_gt_ma150"],
            ma50_gt_ma200=tt["ma50_gt_ma200"],
            ma150_gt_ma200=tt["ma150_gt_ma200"],
            ma200_rising=tt["ma200_rising"],
            near_52w_high=tt["near_52w_high"],
            above_52w_low=tt["above_52w_low"],
            stage=2 if (tt["above_ma200"] and tt["ma200_rising"]) else 1,
            ma200_slope_20d=tt["ma200_slope"],
            prior_uptrend_pct=pu["pct"],
            num_contractions=vcp.get("n",0),
            contractions=vcp.get("detail",[]),
            all_price_contracting=vcp.get("all_price_contracting",False),
            all_volume_contracting=vcp.get("all_vol_contracting",False),
            last_contraction_pct=vcp.get("last_pct",0),
            vcp_score=vcp["score"],
            vol_dry_up=vol["dry_up"],
            vol_dry_up_ratio=vol["ratio"],
            avg_daily_vol=vol["avg"],
            vol_score=vol["score"],
            higher_lows=vcp.get("higher_lows",False),
            base_weeks=base_weeks,
            base_depth=vcp.get("total_depth",0),
            rs_rating=rs["rs"],
            rs_score=rs["score"],
            weeks_tight=tght["weeks"],
            tight_score=tght["score"],
            disqualified=disqualified,
            dq_reasons=dq_reasons,
        )

    except Exception as e:
        log.debug(f"{ticker} error: {e}")
        return None


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(results: list, recipient: str, sender: str, pwd: str):
    if not results:
        log.info("No qualifying setups — email not sent.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    results = sorted(results, key=lambda r: r.score, reverse=True)

    gc = {"A+":"#00c853","A":"#64dd17","B":"#ffd600","C":"#ff6d00","D":"#d50000"}

    rows = ""
    for r in results:
        tt = "".join([
            "✅" if r.above_ma50      else "❌",
            "✅" if r.above_ma150     else "❌",
            "✅" if r.above_ma200     else "❌",
            "✅" if r.ma50_gt_ma150   else "❌",
            "✅" if r.ma50_gt_ma200   else "❌",
            "✅" if r.ma150_gt_ma200  else "❌",
            "✅" if r.ma200_rising    else "❌",
            "✅" if r.near_52w_high   else "❌",
        ])
        vc = "✅" if r.all_price_contracting and r.all_volume_contracting else "⚠️"
        rows += f"""
        <tr style="border-bottom:1px solid #eee">
          <td style="padding:9px 8px;font-weight:700;color:#1565c0">{r.ticker}</td>
          <td style="padding:9px 8px;font-size:12px">{r.company}</td>
          <td style="padding:9px 8px;text-align:center;background:{gc.get(r.grade,'#888')};
              color:#fff;font-weight:700;border-radius:4px">{r.grade} ({r.score})</td>
          <td style="padding:9px 8px;text-align:right">${r.price:.2f}</td>
          <td style="padding:9px 8px;text-align:right;color:#00897b"><b>${r.pivot:.2f}</b></td>
          <td style="padding:9px 8px;text-align:right;color:#e53935">${r.stop:.2f}</td>
          <td style="padding:9px 8px;text-align:center">{r.risk_pct:.1f}%</td>
          <td style="padding:9px 8px;text-align:center">{r.reward_ratio:.1f}x</td>
          <td style="padding:9px 8px;text-align:center">{r.num_contractions} {vc}</td>
          <td style="padding:9px 8px;text-align:center">{r.last_contraction_pct:.1f}%</td>
          <td style="padding:9px 8px;text-align:center">{r.rs_rating:.0f}</td>
          <td style="padding:9px 8px;text-align:center">{r.weeks_tight}w</td>
          <td style="padding:9px 8px;text-align:center;font-size:11px">{r.ma200_slope_20d:.1f}%</td>
          <td style="padding:9px 8px;font-size:10px">{tt}</td>
        </tr>"""

    html = f"""<html><body style="font-family:Arial,sans-serif;background:#f0f2f5;margin:0;padding:20px">
<div style="max-width:1300px;margin:auto;background:#fff;border-radius:10px;
     box-shadow:0 4px 16px rgba(0,0,0,0.12);overflow:hidden">

  <div style="background:linear-gradient(135deg,#0d1b8e,#1565c0);padding:28px 32px;color:#fff">
    <h1 style="margin:0;font-size:24px;letter-spacing:-0.5px">
      📈 VCP Scanner — STRICT Minervini SEPA Mode
    </h1>
    <p style="margin:8px 0 0;opacity:0.85;font-size:14px">
      {today} &nbsp;|&nbsp; {len(results)} A/A+ setups passed ALL 7 strict gates
    </p>
  </div>

  <div style="padding:24px 28px">

    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">
      <div style="background:#e8f5e9;border-left:4px solid #43a047;padding:10px 16px;border-radius:6px;flex:1">
        <div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.5px">Qualifying Setups</div>
        <div style="font-size:28px;font-weight:700;color:#2e7d32">{len(results)}</div>
      </div>
      <div style="background:#e3f2fd;border-left:4px solid #1565c0;padding:10px 16px;border-radius:6px;flex:1">
        <div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.5px">Strict Gates Passed</div>
        <div style="font-size:28px;font-weight:700;color:#0d47a1">7 / 7</div>
      </div>
      <div style="background:#fff8e1;border-left:4px solid #f9a825;padding:10px 16px;border-radius:6px;flex:1">
        <div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.5px">Min RS Rating</div>
        <div style="font-size:28px;font-weight:700;color:#e65100">≥ 70</div>
      </div>
    </div>

    <h3 style="color:#0d1b8e;margin:0 0 6px">⚙️ Strict Qualification Gates (ALL must pass)</h3>
    <table style="font-size:12px;color:#444;margin-bottom:20px;border-collapse:collapse;width:100%">
      <tr>
        <td style="padding:4px 12px 4px 0">✅ <b>Gate 1</b></td>
        <td>MM Trend Template — ALL 8 criteria must pass (no exceptions)</td>
        <td style="padding:4px 12px 4px 0">✅ <b>Gate 2</b></td>
        <td>MA200 slope ≥ +1% over 20 days (confirmed Stage 2)</td>
      </tr>
      <tr>
        <td style="padding:4px 12px 4px 0">✅ <b>Gate 3</b></td>
        <td>Prior uptrend ≥ 30% before base formation</td>
        <td style="padding:4px 12px 4px 0">✅ <b>Gate 4</b></td>
        <td>RS Rating ≥ 70 (outperforming 70%+ of market)</td>
      </tr>
      <tr>
        <td style="padding:4px 12px 4px 0">✅ <b>Gate 5</b></td>
        <td>VCP: EVERY contraction smaller, EVERY volume lower, last ≤ 15%</td>
        <td style="padding:4px 12px 4px 0">✅ <b>Gate 6</b></td>
        <td>Volume dry-up: last 10-day avg &lt; 60% of MA50</td>
      </tr>
      <tr>
        <td style="padding:4px 12px 4px 0">✅ <b>Gate 7</b></td>
        <td>Liquidity: avg volume &gt; 300k shares/day</td>
        <td></td><td></td>
      </tr>
    </table>

    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead>
        <tr style="background:#0d1b8e;color:#fff;font-size:11px">
          <th style="padding:10px 8px;text-align:left">Ticker</th>
          <th style="padding:10px 8px;text-align:left">Company</th>
          <th style="padding:10px 8px">Grade</th>
          <th style="padding:10px 8px">Price</th>
          <th style="padding:10px 8px;background:#1b5e20">Pivot 🎯</th>
          <th style="padding:10px 8px;background:#b71c1c">Stop 🛑</th>
          <th style="padding:10px 8px">Risk%</th>
          <th style="padding:10px 8px">R:R</th>
          <th style="padding:10px 8px">VCP #</th>
          <th style="padding:10px 8px">Last C%</th>
          <th style="padding:10px 8px">RS</th>
          <th style="padding:10px 8px">Tight</th>
          <th style="padding:10px 8px">MA200↑</th>
          <th style="padding:10px 8px">TT 8pts</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    </div>

    <div style="margin-top:20px;background:#fce4ec;border-left:4px solid #c62828;
         padding:14px;border-radius:6px;font-size:13px">
      <b>⚠️ Important:</b> These stocks have passed strict algorithmic screening only.
      Always verify manually on TradingView charts before trading.
      Use stops at the indicated level. Risk ≤ 1–2% of account per trade.
      Past patterns do not guarantee future results.
    </div>
  </div>

  <div style="background:#f5f5f5;padding:12px 28px;font-size:11px;color:#888;text-align:center">
    VCP Scanner v2 · Minervini SEPA · {today}
  </div>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📈 STRICT VCP ({len(results)} setups) | {today}"
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(sender, pwd)
            s.sendmail(sender, recipient, msg.as_string())
        log.info(f"✅ Email sent → {recipient}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    recipient = os.environ.get("EMAIL_RECIPIENT","")
    sender    = os.environ.get("EMAIL_SENDER","")
    pwd       = os.environ.get("EMAIL_PASSWORD","")
    min_score = float(os.environ.get("MIN_SCORE","65"))

    log.info("="*60)
    log.info("🔍 VCP Scanner v2 — STRICT Minervini SEPA Mode")
    log.info(f"Min composite score: {min_score}")
    log.info("="*60)

    spy = fetch("SPY")
    if spy is None:
        log.error("Cannot fetch SPY — aborting")
        return
    spy = add_indicators(spy)

    tickers  = list(dict.fromkeys(get_tickers()))
    log.info(f"Universe: {len(tickers)} tickers")

    qualifying = []
    dq_count   = 0
    err_count  = 0

    for i, t in enumerate(tickers):
        if i % 50 == 0:
            log.info(f"[{i}/{len(tickers)}] qualifying={len(qualifying)} dq={dq_count}")

        df = fetch(t)
        if df is None:
            err_count += 1
            continue

        r = analyze(t, df, spy)
        if r is None:
            err_count += 1
            continue

        if r.disqualified:
            dq_count += 1
            log.debug(f"  ❌ {t}: {' | '.join(r.dq_reasons)}")
            continue

        if r.score >= min_score and r.grade in ("A+","A","B"):
            qualifying.append(r)
            log.info(f"  ✅ {t} {r.grade}({r.score}) | "
                     f"VCP={r.num_contractions}x last={r.last_contraction_pct:.1f}% | "
                     f"RS={r.rs_rating:.0f} | TT={r.tt_passed}/8 | "
                     f"VolDry={r.vol_dry_up_ratio:.2f}")

        time.sleep(0.08)

    log.info("="*60)
    log.info(f"Done. Qualifying: {len(qualifying)} | DQ'd: {dq_count} | Errors: {err_count}")

    # Save JSON
    out = []
    for r in sorted(qualifying, key=lambda x: x.score, reverse=True):
        out.append({
            "ticker": r.ticker, "company": r.company,
            "score": r.score, "grade": r.grade,
            "price": r.price, "pivot": r.pivot,
            "stop": r.stop, "risk_pct": r.risk_pct, "rr": r.reward_ratio,
            "tt_passed": r.tt_passed, "stage": r.stage,
            "ma200_slope": r.ma200_slope_20d,
            "prior_uptrend_pct": r.prior_uptrend_pct,
            "vcp_contractions": r.num_contractions,
            "last_contraction_pct": r.last_contraction_pct,
            "all_price_contracting": r.all_price_contracting,
            "all_vol_contracting": r.all_volume_contracting,
            "higher_lows": r.higher_lows,
            "vol_dry_up": r.vol_dry_up, "vol_ratio": r.vol_dry_up_ratio,
            "rs_rating": r.rs_rating,
            "weeks_tight": r.weeks_tight,
            "scanned_at": datetime.now().isoformat()
        })

    with open("vcp_results.json","w") as f:
        json.dump(out, f, indent=2)
    log.info("Saved → vcp_results.json")

    if all([recipient, sender, pwd]):
        send_email(qualifying, recipient, sender, pwd)
    else:
        log.warning("Email credentials not set — skipping email")


if __name__ == "__main__":
    main()
