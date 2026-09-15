from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.message import EmailMessage

import os
import re
import math
import time
import sqlite3
import hashlib
import secrets
import smtplib
import threading
import requests
import pandas as pd

from dotenv import load_dotenv

load_dotenv(override=True)

APP_VERSION = "3.2.0"
DATABASE = os.getenv("COLLINS_DATABASE", "collins_ai.db")
FRONTEND_URL = os.getenv("COLLINS_FRONTEND_URL", "http://localhost:5173")
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
NEWS_FEEDS = {
    "this": ["https://nfs.faireconomy.media/ff_calendar_thisweek.json", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json"],
    "next": ["https://nfs.faireconomy.media/ff_calendar_nextweek.json", "https://cdn-nfs.faireconomy.media/ff_calendar_nextweek.json"],
}
NEWS_URLS = NEWS_FEEDS["this"]
API_REQUEST_INTERVAL = float(os.getenv("TWELVE_DATA_REQUEST_INTERVAL", "8"))
RATE_LIMIT_BACKOFF = float(os.getenv("TWELVE_DATA_RATE_BACKOFF", "65"))
MARKET_CACHE_SECONDS = int(os.getenv("MARKET_CACHE_SECONDS", "600"))
SIGNAL_CACHE_SECONDS = int(os.getenv("SIGNAL_CACHE_SECONDS", "90"))
SIGNAL_SCAN_INTERVAL_SECONDS = int(os.getenv("SIGNAL_SCAN_INTERVAL_SECONDS", "600"))

app = FastAPI(title="Collins AI Signals API", version=APP_VERSION)

cors_origins = [x.strip() for x in os.getenv(
    "COLLINS_CORS_ORIGINS",
    f"{FRONTEND_URL},http://localhost:5173,http://127.0.0.1:5173"
).split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# DATABASE
# =========================================================

def db():
    c = sqlite3.connect(DATABASE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_database():
    c = db(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS verification_tokens(
        token TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS paper_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        entry REAL NOT NULL,
        sl REAL NOT NULL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        tp4 REAL,
        tp5 REAL,
        rr1 REAL,
        rr2 REAL,
        rr3 REAL,
        rr4 REAL,
        rr5 REAL,
        score REAL,
        status TEXT NOT NULL,
        result TEXT,
        created_at TEXT NOT NULL,
        closed_at TEXT,
        proxy INTEGER DEFAULT 0
    )""")
    # Safe migrations for databases created by older CollinsAI versions.
    existing={row[1] for row in c.execute("PRAGMA table_info(paper_trades)").fetchall()}
    migrations={
        "signal_key":"TEXT", "tp1_hit":"INTEGER DEFAULT 0", "tp2_hit":"INTEGER DEFAULT 0",
        "tp3_hit":"INTEGER DEFAULT 0", "tp1_hit_at":"TEXT", "tp2_hit_at":"TEXT",
        "tp3_hit_at":"TEXT", "stage":"TEXT DEFAULT 'ENTRY'", "last_price":"REAL"
    }
    for name,typ in migrations.items():
        if name not in existing: c.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {typ}")
    c.commit(); c.close()

init_database()

# =========================================================
# AUTH
# =========================================================

class Register(BaseModel):
    email: str
    password: str

class Login(BaseModel):
    email: str
    password: str


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def send_verification_email(email: str, token: str):
    sender = os.getenv("COLLINS_GMAIL")
    app_password = os.getenv("COLLINS_GMAIL_APP_PASSWORD")
    if not sender or not app_password:
        raise RuntimeError("Email settings are missing.")
    link = f"{FRONTEND_URL.rstrip('/')}/?verify={token}"
    msg = EmailMessage()
    msg["Subject"] = "Verify your CollinsAI account"
    msg["From"] = sender
    msg["To"] = email
    msg.set_content(
        f"Welcome to CollinsAI.\n\nVerify your email here:\n{link}\n\n"
        "If you did not create this account, ignore this email."
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(msg)

@app.post("/api/register")
def register(body: Register):
    email = body.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Enter a valid email address.")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    c = db(); existing = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        c.close(); raise HTTPException(400, "An account with this email already exists.")
    now = datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO users(email,password,verified,created_at) VALUES(?,?,0,?)",
              (email, hash_password(body.password), now))
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO verification_tokens(token,email,created_at) VALUES(?,?,?)", (token,email,now))
    c.commit(); c.close()
    try:
        send_verification_email(email, token)
    except Exception:
        c = db(); c.execute("DELETE FROM verification_tokens WHERE token=?", (token,)); c.execute("DELETE FROM users WHERE email=?", (email,)); c.commit(); c.close()
        raise HTTPException(500, "Could not send verification email. Check your Gmail settings.")
    return {"message": "Verification email sent. Check your inbox."}

@app.get("/api/verify")
def verify_email(token: str):
    c = db(); row = c.execute("SELECT email FROM verification_tokens WHERE token=?", (token,)).fetchone()
    if not row:
        c.close(); raise HTTPException(400, "Invalid or expired verification link.")
    c.execute("UPDATE users SET verified=1 WHERE email=?", (row["email"],))
    c.execute("DELETE FROM verification_tokens WHERE token=?", (token,))
    c.commit(); c.close()
    return {"message":"Email verified successfully.", "email":row["email"]}

@app.post("/api/login")
def login(body: Login):
    email = body.email.strip().lower()
    c = db(); row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone(); c.close()
    if not row: raise HTTPException(401, "Account not found.")
    if row["password"] != hash_password(body.password): raise HTTPException(401, "Incorrect password.")
    if not row["verified"]: raise HTTPException(403, "Please verify your email before signing in.")
    return {"message":"Login successful.", "email":email, "token":secrets.token_urlsafe(24)}

# =========================================================
# MARKETS
# =========================================================

MARKETS = [
    {"symbol":"XAUUSD", "display":"XAU/USD", "provider":"XAU/USD", "asset_type":"metal", "proxy":False},
    {"symbol":"EURUSD", "display":"EUR/USD", "provider":"EUR/USD", "asset_type":"forex", "proxy":False},
    {"symbol":"GBPUSD", "display":"GBP/USD", "provider":"GBP/USD", "asset_type":"forex", "proxy":False},
    {"symbol":"USDJPY", "display":"USD/JPY", "provider":"USD/JPY", "asset_type":"forex", "proxy":False},
    {"symbol":"AUDUSD", "display":"AUD/USD", "provider":"AUD/USD", "asset_type":"forex", "proxy":False},
    {"symbol":"USDCAD", "display":"USD/CAD", "provider":"USD/CAD", "asset_type":"forex", "proxy":False},
    {"symbol":"USDCHF", "display":"USD/CHF", "provider":"USD/CHF", "asset_type":"forex", "proxy":False},
    {"symbol":"NAS100", "display":"NAS100", "provider":"QQQ", "asset_type":"index_proxy", "proxy":True},
    {"symbol":"US30", "display":"US30", "provider":"DIA", "asset_type":"index_proxy", "proxy":True},
    {"symbol":"BTCUSD", "display":"BTC/USD", "provider":"BTC/USD", "asset_type":"crypto", "proxy":False},
    {"symbol":"ETHUSD", "display":"ETH/USD", "provider":"ETH/USD", "asset_type":"crypto", "proxy":False},
]
MARKET_BY_SYMBOL = {m["symbol"]:m for m in MARKETS}

CURRENCY_MAP = {
    "XAUUSD":["USD"], "EURUSD":["EUR","USD"], "GBPUSD":["GBP","USD"],
    "USDJPY":["USD","JPY"], "AUDUSD":["AUD","USD"], "USDCAD":["USD","CAD"],
    "USDCHF":["USD","CHF"], "NAS100":["USD"], "US30":["USD"],
    "BTCUSD":["USD"], "ETHUSD":["USD"]
}

# =========================================================
# MARKET DATA CACHE / RATE LIMIT
# =========================================================

market_cache = {}
market_lock = threading.Lock()
last_api_request_time = 0.0
last_rate_limit_time = 0.0


def safe_provider_error(message: str) -> str:
    msg = str(message)
    msg = re.sub(r"https?://\S+", "provider request", msg)
    msg = re.sub(r"apikey=[^&\s]+", "apikey=REDACTED", msg, flags=re.I)
    return msg[:240]


def get_market_data(symbol, interval="15min", outputsize=150, cache_seconds=MARKET_CACHE_SECONDS):
    global last_api_request_time, last_rate_limit_time
    api_key = os.getenv("TWELVE_DATA_API_KEY")
    if not api_key:
        raise RuntimeError("Twelve Data API key is missing.")
    key = f"{symbol}|{interval}|{outputsize}"
    cached = market_cache.get(key)
    if cached and time.time() - cached[0] < cache_seconds:
        return cached[1].copy()
    if last_rate_limit_time and time.time() - last_rate_limit_time < RATE_LIMIT_BACKOFF:
        if cached: return cached[1].copy()
        raise RuntimeError("Twelve Data rate limit is active; waiting for cooldown.")
    with market_lock:
        cached = market_cache.get(key)
        if cached and time.time() - cached[0] < cache_seconds:
            return cached[1].copy()
        wait = API_REQUEST_INTERVAL - (time.time() - last_api_request_time)
        if wait > 0: time.sleep(wait)
        try:
            r = requests.get(TWELVE_DATA_URL, params={"symbol":symbol,"interval":interval,"outputsize":outputsize,"apikey":api_key}, timeout=20)
            last_api_request_time = time.time()
        except requests.RequestException as e:
            raise RuntimeError("Market-data provider connection failed.") from e
        if r.status_code == 429:
            last_rate_limit_time = time.time()
            if cached: return cached[1].copy()
            raise RuntimeError("Twelve Data rate limit reached; cooldown activated.")
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError("Market-data provider returned invalid data.") from e
        if data.get("status") == "error":
            message = str(data.get("message", "Market data request failed."))
            if any(x in message.lower() for x in ["rate limit","api credits","too many"]):
                last_rate_limit_time = time.time()
            if cached: return cached[1].copy()
            raise RuntimeError(safe_provider_error(message))
        values = data.get("values")
        if not values:
            raise RuntimeError("No market data returned for this provider symbol.")
        df = pd.DataFrame(values)
        if "datetime" not in df.columns:
            raise RuntimeError("Market data has no datetime field.")
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
        for col in ["open","high","low","close","volume"]:
            if col in df.columns: df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["datetime","open","high","low","close"]).sort_values("datetime").reset_index(drop=True)
        market_cache[key] = (time.time(), df.copy())
        return df

# =========================================================
# INDICATORS / STRUCTURE
# =========================================================

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d=s.diff(); up=d.clip(lower=0); down=-d.clip(upper=0)
    rs=up.ewm(alpha=1/n, adjust=False).mean()/down.ewm(alpha=1/n, adjust=False).mean().replace(0, float("nan"))
    return 100-(100/(1+rs))

def atr(df, n=14):
    prev=df["close"].shift(1)
    tr=pd.concat([(df.high-df.low),(df.high-prev).abs(),(df.low-prev).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def macd(s):
    m=ema(s,12)-ema(s,26); sig=ema(m,9)
    return m,sig,m-sig

def bollinger(s,n=20,k=2):
    mid=s.rolling(n).mean(); std=s.rolling(n).std()
    return mid,mid+k*std,mid-k*std

def trend(df):
    if df is None or len(df)<60: return "NEUTRAL"
    e20=ema(df.close,20).iloc[-1]; e50=ema(df.close,50).iloc[-1]; last=df.close.iloc[-1]
    if last>e20>e50: return "BULLISH"
    if last<e20<e50: return "BEARISH"
    return "NEUTRAL"

def structure(df):
    if len(df)<25: return "NEUTRAL", None
    h=df.high; l=df.low; c=df.close.iloc[-1]
    prev_high=h.iloc[-20:-2].max(); prev_low=l.iloc[-20:-2].min()
    if c>prev_high: return "BULLISH", float(prev_high)
    if c<prev_low: return "BEARISH", float(prev_low)
    return "NEUTRAL", None

def liquidity_levels(df, direction):
    # Recent swing pools; farther levels are used for extended targets.
    highs=[]; lows=[]
    for i in range(2,len(df)-2):
        if df.high.iloc[i]>=df.high.iloc[i-2:i+3].max(): highs.append(float(df.high.iloc[i]))
        if df.low.iloc[i]<=df.low.iloc[i-2:i+3].min(): lows.append(float(df.low.iloc[i]))
    price=float(df.close.iloc[-1])
    if direction=="BUY":
        return sorted(set(x for x in highs if x>price))
    return sorted(set(x for x in lows if x<price), reverse=True)

def detect_fvg(df):
    if len(df)<5: return "NONE"
    a,b,c=df.iloc[-3],df.iloc[-2],df.iloc[-1]
    if float(c.low)>float(a.high): return "BULLISH"
    if float(c.high)<float(a.low): return "BEARISH"
    return "NONE"

def detect_sweep(df):
    if len(df)<20: return "NONE"
    recent=df.iloc[-2]; prior=df.iloc[-20:-2]
    if recent.low < prior.low.min() and recent.close > prior.low.min(): return "BULLISH"
    if recent.high > prior.high.max() and recent.close < prior.high.max(): return "BEARISH"
    return "NONE"

def detect_crt(df):
    if len(df)<3: return "NEUTRAL"
    a,b=df.iloc[-2],df.iloc[-1]
    if b.close>a.high: return "BULLISH"
    if b.close<a.low: return "BEARISH"
    return "NEUTRAL"

# =========================================================
# NEWS / FUNDAMENTAL
# =========================================================

news_cache={"time":0.0,"items":[],"available":False,"source":None,"week":"this","weeks":{}}
news_lock=threading.Lock()

def fetch_news_week(week="this"):
    week = week if week in NEWS_FEEDS else "this"
    cached = news_cache.get("weeks",{}).get(week)
    if cached and time.time()-cached.get("time",0) < 300:
        return cached.get("items",[])
    with news_lock:
        cached = news_cache.get("weeks",{}).get(week)
        if cached and time.time()-cached.get("time",0) < 300:
            return cached.get("items",[])
        headers={"User-Agent":"CollinsAI-Signals/3.1 (economic-calendar)"}
        for url in NEWS_FEEDS[week]:
            try:
                r=requests.get(url,headers=headers,timeout=15)
                r.raise_for_status()
                data=r.json()
                if isinstance(data,list):
                    news_cache.setdefault("weeks",{})[week]={"time":time.time(),"items":data,"source":url,"available":True}
                    if week == "this":
                        news_cache.update({"time":time.time(),"items":data,"available":True,"source":url,"week":week})
                    return data
            except Exception:
                continue
        news_cache.setdefault("weeks",{})[week]={"time":time.time(),"items":[],"source":None,"available":False}
        if week == "this":
            news_cache.update({"time":time.time(),"items":[],"available":False,"source":None,"week":week})
        return []

def get_news():
    return fetch_news_week("this")

def news_for(symbol):
    currencies=CURRENCY_MAP.get(symbol,[]); now=datetime.now(timezone.utc)
    items=get_news(); out=[]; max_risk=0
    for item in items:
        cur=str(item.get("currency",item.get("country",""))).upper()
        impact=str(item.get("impact",item.get("importance",""))).upper()
        if cur not in currencies: continue
        raw=item.get("date",item.get("datetime",item.get("time")))
        try:
            dt=pd.to_datetime(raw,utc=True).to_pydatetime()
        except Exception:
            continue
        hours=(dt-now).total_seconds()/3600
        if -2<=hours<=24:
            base=80 if "HIGH" in impact else 45 if "MED" in impact else 15
            proximity=max(0.35,1-min(abs(hours)/24,0.65))
            risk=int(base*proximity); max_risk=max(max_risk,risk)
            out.append({"event":item.get("event",item.get("title",item.get("name","Economic event"))),"currency":cur,"impact":impact or "LOW","datetime":dt.isoformat(),"hours":round(hours,2)})
    level="HIGH" if max_risk>=70 else "MEDIUM" if max_risk>=35 else "LOW" if out else "NONE"
    return {"risk":max_risk,"level":level,"events":sorted(out,key=lambda x:x["hours"]),"available":bool(news_cache.get("available"))}

def fundamental_status(symbol):
    ctx=news_for(symbol)
    if not ctx.get("available"):
        return "UNAVAILABLE", "Economic-calendar feed unavailable; news safety mode is active."
    if not ctx["events"]:
        return "AVAILABLE", "Economic calendar is available; no relevant event is in the active window."
    return "AVAILABLE", f"{len(ctx['events'])} relevant economic event(s) detected."

# =========================================================
# DYNAMIC TARGET ENGINE
# =========================================================

def rr(entry, sl, target):
    risk=abs(entry-sl)
    return abs(target-entry)/risk if risk else 0

def make_targets(df, direction, entry, sl):
    """Build exactly three clean targets. TP3 is the final structural target.
    TP1/TP2 are intermediate R-multiples derived from that final target.
    """
    risk=abs(entry-sl)
    if risk<=0: return []
    levels=liquidity_levels(df,direction)
    if direction=="BUY":
        levels=[x for x in levels if x>entry+risk*1.2]
        levels.sort()
    else:
        levels=[x for x in levels if x<entry-risk*1.2]
        levels.sort(reverse=True)

    # Prefer the nearest meaningful liquidity/structure target. Do not invent
    # extreme R:R just to make the card look attractive.
    final=None
    for level in levels:
        r=rr(entry,sl,level)
        if 2.0<=r<=25.0:
            final=float(level); break
    if final is None:
        # Conservative fallback only when structure supplied no usable pool.
        a=float(atr(df).iloc[-1] or risk)
        max_r=max(2.0,min(8.0,(a*6.0)/risk))
        target=entry+max_r*risk if direction=="BUY" else entry-max_r*risk
        final=float(target)

    final_r=rr(entry,sl,final)
    # TP1 is the first clean 1R milestone whenever the final structure allows it.
    # TP2 remains an intermediate structural target; TP3 is always the final target.
    r1=min(1.0, final_r*0.50)
    r2=min(final_r*0.75, max(r1+0.5, final_r*0.60))
    if r2>=final_r: r2=final_r*0.75
    if r1>=r2: r1=min(1.0, final_r*0.45)
    t1=entry+r1*risk if direction=="BUY" else entry-r1*risk
    t2=entry+r2*risk if direction=="BUY" else entry-r2*risk
    return [(float(t1),round(r1,2)),(float(t2),round(r2,2)),(float(final),round(final_r,2))]

# =========================================================
# ANALYSIS ENGINE
# =========================================================

def post_news_reaction(df15, news_context):
    """Require actual price reaction after a major event before allowing a signal."""
    if not news_context: return {"regime":"NEWS_CLEAR","confirmed":True,"note":"No relevant event in the active window."}
    events=news_context.get("events",[])
    high=[e for e in events if "HIGH" in str(e.get("impact","")).upper()]
    if not high:
        return {"regime":"NEWS_CLEAR","confirmed":True,"note":"No high-impact event in the active window."}
    nearest=min(high,key=lambda e:abs(float(e.get("hours",999))))
    h=float(nearest.get("hours",999))
    if -0.75<=h<=0.75:
        return {"regime":"NEWS_LOCK","confirmed":False,"note":f"High-impact event {abs(h)*60:.0f} minutes from current time; waiting for clean reaction."}
    if h < -0.75 and h >= -2.0:
        if len(df15)<20: return {"regime":"POST_NEWS_WAIT","confirmed":False,"note":"Waiting for post-news candles."}
        a=float(atr(df15).iloc[-1] or 0)
        body=abs(float(df15.close.iloc[-1])-float(df15.open.iloc[-1]))
        recent_high=float(df15.high.iloc[-8:-1].max()); recent_low=float(df15.low.iloc[-8:-1].min())
        close=float(df15.close.iloc[-1]); high_now=float(df15.high.iloc[-1]); low_now=float(df15.low.iloc[-1])
        displacement=body >= max(a*0.55,1e-12)
        bos_up=close>recent_high; bos_dn=close<recent_low
        sweep=detect_sweep(df15); fvg=detect_fvg(df15); crt=detect_crt(df15)
        confirmed=displacement and (bos_up or bos_dn or sweep in ("BULLISH","BEARISH")) and (fvg in ("BULLISH","BEARISH") or crt in ("BULLISH","BEARISH"))
        direction="BULLISH" if bos_up or sweep=="BULLISH" else "BEARISH" if bos_dn or sweep=="BEARISH" else "NEUTRAL"
        note="Post-news displacement + structure reaction confirmed." if confirmed else "Post-news reaction is not confirmed yet."
        return {"regime":"POST_NEWS_CONFIRMED" if confirmed else "POST_NEWS_WAIT","confirmed":confirmed,"direction":direction,"note":note}
    return {"regime":"NEWS_CLEAR","confirmed":True,"note":"High-impact event is outside the immediate reaction window."}

def calculate_analysis(df15, df1h, df4h, news_context, fundamental_context, symbol_context=""):
    if len(df15)<60: raise RuntimeError("Not enough 15M candles for analysis.")
    close=df15.close
    e20=ema(close,20).iloc[-1]; e50=ema(close,50).iloc[-1]
    rv=float(rsi(close).iloc[-1]); _,_,mh=macd(close); bv,_,_=bollinger(close)
    macd_hist=float(mh.iloc[-1]); bbmid=float(bv.iloc[-1]); price=float(close.iloc[-1]); a=float(atr(df15).iloc[-1])
    st,_=structure(df15); sweep=detect_sweep(df15); fvg=detect_fvg(df15); crt=detect_crt(df15)
    t1=trend(df1h); t4=trend(df4h)
    reaction=post_news_reaction(df15,news_context)
    bull=0; bear=0; reasons=[]
    if price>e20>e50: bull+=22; reasons.append("EMA trend bullish")
    elif price<e20<e50: bear+=22; reasons.append("EMA trend bearish")
    if macd_hist>0: bull+=14; reasons.append("MACD bullish")
    elif macd_hist<0: bear+=14; reasons.append("MACD bearish")
    if 52<=rv<=72: bull+=7; reasons.append("RSI bullish zone")
    elif 28<=rv<=48: bear+=7; reasons.append("RSI bearish zone")
    if price>bbmid: bull+=5; reasons.append("Above Bollinger midline")
    elif price<bbmid: bear+=5; reasons.append("Below Bollinger midline")
    if st=="BULLISH": bull+=18; reasons.append("Bullish market structure/BOS")
    elif st=="BEARISH": bear+=18; reasons.append("Bearish market structure/BOS")
    if sweep=="BULLISH": bull+=14; reasons.append("Bullish liquidity sweep")
    elif sweep=="BEARISH": bear+=14; reasons.append("Bearish liquidity sweep")
    if fvg=="BULLISH": bull+=9; reasons.append("Bullish FVG")
    elif fvg=="BEARISH": bear+=9; reasons.append("Bearish FVG")
    if crt=="BULLISH": bull+=8; reasons.append("Bullish CRT confirmation")
    elif crt=="BEARISH": bear+=8; reasons.append("Bearish CRT confirmation")
    if t1=="BULLISH": bull+=14; reasons.append("1H structure bullish")
    elif t1=="BEARISH": bear+=14; reasons.append("1H structure bearish")
    if t4=="BULLISH": bull+=18; reasons.append("4H major trend bullish")
    elif t4=="BEARISH": bear+=18; reasons.append("4H major trend bearish")
    if t1==t4 and t1 in ("BULLISH","BEARISH"):
        if t1=="BULLISH": bull+=12
        else: bear+=12
        reasons.append("1H + 4H confluence")

    technical=min(100,max(bull,bear)); bull=min(100,bull); bear=min(100,bear); lead=abs(bull-bear)
    direction="WAIT"
    if bull>=82 and bull>=bear+24: direction="BUY"
    elif bear>=82 and bear>=bull+24: direction="SELL"
    elif bull>=70 and lead>=32 and t1=="BULLISH": direction="BUY"
    elif bear>=70 and lead>=32 and t1=="BEARISH": direction="SELL"

    news_level=(news_context or {}).get("level","NONE")
    if reaction["regime"] in ("NEWS_LOCK","POST_NEWS_WAIT"):
        direction="WAIT"; reasons.append(reaction["note"])
    elif reaction["regime"]=="POST_NEWS_CONFIRMED":
        if reaction.get("direction")=="BULLISH" and direction=="BUY": bull+=12; reasons.append("Post-news bullish reaction confirmed")
        elif reaction.get("direction")=="BEARISH" and direction=="SELL": bear+=12; reasons.append("Post-news bearish reaction confirmed")
        else: direction="WAIT"; reasons.append("Post-news reaction conflicts with setup")

    technical=min(100,max(bull,bear)); bull=min(100,bull); bear=min(100,bear); lead=abs(bull-bear)
    asset_symbol=symbol_context or ""
    confidence=round(42 + min(technical,100)*0.31 + (12 if t1==t4 and t1!="NEUTRAL" else 0) + min(lead*0.12,8))
    # Asset-specific quality gates: metals and crypto need stronger confirmation than major FX.
    if asset_symbol in ("XAUUSD","BTCUSD","ETHUSD") and direction in ("BUY","SELL"):
        required_score={"XAUUSD":86,"BTCUSD":88,"ETHUSD":90}[asset_symbol]
        aligned=(t1==t4 and t1 != "NEUTRAL")
        directional_structure=(st==("BULLISH" if direction=="BUY" else "BEARISH"))
        confirmation=(fvg==("BULLISH" if direction=="BUY" else "BEARISH") or crt==("BULLISH" if direction=="BUY" else "BEARISH") or reaction.get("regime")=="POST_NEWS_CONFIRMED")
        if not (aligned and directional_structure and confirmation):
            direction="WAIT"
            reasons.append("Asset-specific confirmation gate not satisfied")
        confidence=min(confidence,required_score-1) if direction=="WAIT" else max(confidence,required_score)
    if direction=="WAIT": confidence=min(confidence,68)
    if reaction["regime"]=="POST_NEWS_CONFIRMED" and direction!="WAIT": confidence=min(96,confidence+5)
    if news_level=="MEDIUM": confidence=min(confidence,90)
    if fundamental_context[0]=="UNAVAILABLE": confidence=min(confidence,84); reasons.append("Economic calendar unavailable; confidence capped")
    # 96 is reserved for exceptional post-news confirmation; crypto remains stricter.
    confidence=min(confidence,92)

    # Volatility-aware stops: Gold and crypto need more breathing room than major FX.
    stop_mult={"XAUUSD":1.80,"BTCUSD":2.10,"ETHUSD":2.20}.get(asset_symbol,1.35)
    if direction=="BUY": sl=price-max(a*stop_mult,a*0.8); targets=make_targets(df15,"BUY",price,sl)
    elif direction=="SELL": sl=price+max(a*stop_mult,a*0.8); targets=make_targets(df15,"SELL",price,sl)
    else: sl=None; targets=[]
    tps=[x[0] for x in targets]; rrs=[x[1] for x in targets]
    while len(tps)<3: tps.append(None); rrs.append(None)
    max_rr=rrs[2] if len(rrs)>=3 and rrs[2] is not None else 0
    if direction!="WAIT" and max_rr>=8: tier="EXTENDED HIGH-RR"
    elif direction!="WAIT" and max_rr>=5: tier="VERY STRONG"
    elif direction!="WAIT" and confidence>=80: tier="STRONG"
    elif direction!="WAIT": tier="GOOD"
    else: tier="WAIT"
    return {
        "direction":direction,"score":confidence,"confidence":confidence,"technical_score":technical,
        "bullish_score":bull,"bearish_score":bear,"tier":tier,
        "entry":round(price,5),"sl":round(sl,5) if sl is not None else None,
        "tp1":round(tps[0],5) if tps[0] is not None else None,
        "tp2":round(tps[1],5) if tps[1] is not None else None,
        "tp3":round(tps[2],5) if tps[2] is not None else None,
        "tp4":None,"tp5":None,"rr1":rrs[0] if len(rrs)>0 else None,"rr2":rrs[1] if len(rrs)>1 else None,"rr3":rrs[2] if len(rrs)>2 else None,
        "rr4":None,"rr5":None,"rr":f"1:{max_rr:g}" if max_rr else None,"max_rr":max_rr,
        "final_rr":max_rr,"rsi":round(rv,2),"atr":round(a,6),"trend_1h":t1,"major_trend":t4,
        "structure":st,"liquidity_sweep":sweep,"fvg":fvg,"crt":crt,
        "fundamental_status":fundamental_context[0],"fundamental_note":fundamental_context[1],
        "news_risk":(news_context or {}).get("risk",0),"news_level":news_level,"news_regime":reaction["regime"],
        "news_note":reaction["note"],"analysis":"; ".join(reasons[:10]),
        "strategies":["SMC","Market Structure","Liquidity","Order Block/FVG","CRT","EMA","RSI","MACD","Bollinger","MTF","News Reaction"]
    }

# =========================================================
# SIGNAL CACHE / BACKGROUND SCANNER
# =========================================================

signals_cache={"generated_at":None,"signals":[],"status":"warming_up","scan_started":None,"scan_completed":None,"last_market":"","markets_scanned":0}
signal_lock=threading.Lock()
scanner_running=True


def generate_one(m):
    provider=m["provider"]
    df15=get_market_data(provider,"15min",150)
    df1h=get_market_data(provider,"1h",150)
    df4h=get_market_data(provider,"4h",100)
    news=news_for(m["symbol"])
    fundamental=fundamental_status(m["symbol"])
    result=calculate_analysis(df15,df1h,df4h,news,fundamental,m["symbol"])
    return {"symbol":m["display"],"internal_symbol":m["symbol"],"provider_symbol":provider,
            "proxy":m["proxy"],"proxy_note":"ETF proxy used for index market data" if m["proxy"] else None,
            **result,"status":("ACTIVE" if result.get("direction") in ("BUY","SELL") and float(result.get("score",0)) >= 75 else "WAITING")}


def error_signal(m, exc):
    return {
        "symbol":m["display"], "internal_symbol":m["symbol"], "provider_symbol":m["provider"],
        "proxy":m["proxy"], "proxy_note":"ETF proxy used for index market data" if m["proxy"] else None,
        "direction":"DATA ERROR", "score":0, "tier":"ERROR", "entry":None, "sl":None,
        "tp1":None, "tp2":None, "tp3":None, "tp4":None, "tp5":None,
        "rr":None, "rr1":None, "rr2":None, "rr3":None, "rr4":None, "rr5":None,
        "max_rr":0, "trend_1h":"UNKNOWN", "major_trend":"UNKNOWN",
        "fundamental_status":"UNAVAILABLE", "news_level":"UNKNOWN", "news_risk":0,
        "analysis":safe_provider_error(exc), "status":"ERROR",
        "strategies":[]
    }


def scanner_loop():
    global signals_cache
    while scanner_running:
        cycle_start=time.time()
        scan_started=datetime.now(timezone.utc).isoformat()
        with signal_lock:
            previous={x.get("internal_symbol"):x for x in signals_cache.get("signals",[]) if x.get("status") not in ("ERROR","DATA ERROR")}
            signals_cache["status"]="scanning"
            signals_cache["scan_started"]=scan_started
            signals_cache["last_market"]=""
            signals_cache["markets_scanned"]=0

        # IMPORTANT: publish each market immediately after it finishes.
        # The API publishes each completed market immediately while the full scan continues.
        for m in MARKETS:
            try:
                result=generate_one(m)
                # Automatically track strong confirmed signals as PAPER trades only.
                try: insert_paper_trade(result)
                except Exception: pass
                with signal_lock:
                    current=[x for x in signals_cache.get("signals",[]) if x.get("internal_symbol") != m["symbol"]]
                    current.append(result)
                    order={x["symbol"]:i for i,x in enumerate(MARKETS)}
                    current.sort(key=lambda x: order.get(x.get("internal_symbol"),999))
                    signals_cache["signals"]=current
                    signals_cache["generated_at"]=datetime.now(timezone.utc).isoformat()
                    signals_cache["last_market"]=m["display"]
                    signals_cache["markets_scanned"]=len(current)
                    signals_cache["status"]="scanning"
            except Exception as exc:
                # Preserve the last good signal for this market when possible.
                with signal_lock:
                    old=previous.get(m["symbol"])
                    current=[x for x in signals_cache.get("signals",[]) if x.get("internal_symbol") != m["symbol"]]
                    current.append(old if old else error_signal(m,exc))
                    order={x["symbol"]:i for i,x in enumerate(MARKETS)}
                    current.sort(key=lambda x: order.get(x.get("internal_symbol"),999))
                    signals_cache["signals"]=current
                    signals_cache["generated_at"]=datetime.now(timezone.utc).isoformat()
                    signals_cache["last_market"]=m["display"]
                    signals_cache["markets_scanned"]=len(current)
                    signals_cache["status"]="scanning"

        with signal_lock:
            signals_cache["status"]="ready" if signals_cache.get("signals") else "warming_up"
            signals_cache["scan_completed"]=datetime.now(timezone.utc).isoformat()
            signals_cache["generated_at"]=signals_cache["scan_completed"]

        elapsed=time.time()-cycle_start
        time.sleep(max(5, SIGNAL_SCAN_INTERVAL_SECONDS-elapsed))

threading.Thread(target=scanner_loop,daemon=True,name="collins-signal-scanner").start()


# =========================================================
# FOREX-FACTORY-STYLE CALENDAR DATA
# =========================================================
CALENDAR_WAT = ZoneInfo("Africa/Lagos")
CALENDAR_CACHE = {}
CALENDAR_LOCK = threading.Lock()

def calendar_week_start(day=None, offset=0):
    if day is None:
        day = datetime.now(CALENDAR_WAT).date()
    # Sunday is the start used by the calendar week display.
    sunday = day - timedelta(days=(day.weekday() + 1) % 7)
    return sunday + timedelta(days=7 * offset)

def ff_week_url(sunday):
    return f"https://www.forexfactory.com/calendar?week={sunday.strftime('%b%d.%Y').lower()}"

def parse_ff_calendar(html, default_year=None):
    """Best-effort parser for the public calendar table. Falls back cleanly if FF changes markup."""
    out=[]
    try:
        tables=pd.read_html(html)
    except Exception:
        return out
    table=None
    for df in tables:
        cols=[str(c).strip().lower() for c in df.columns]
        joined=' '.join(cols)
        if 'currency' in joined and 'actual' in joined and 'forecast' in joined and 'previous' in joined:
            table=df.copy(); break
    if table is None: return out
    table.columns=[str(c).strip().lower() for c in table.columns]
    current_date=None
    for _, row in table.iterrows():
        vals={str(k).lower(): row.get(k) for k in table.columns}
        raw_date=vals.get('date') or vals.get('time')
        text=lambda x: '' if pd.isna(x) else str(x).strip()
        date_text=text(vals.get('date'))
        if date_text and re.search(r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b', date_text):
            current_date=date_text
        currency=text(vals.get('currency'))
        event=text(vals.get('event') or vals.get('detail') or vals.get('name'))
        if not currency or not event: continue
        time_text=text(vals.get('time'))
        impact=text(vals.get('impact')).upper() or 'LOW'
        actual=text(vals.get('actual'))
        forecast=text(vals.get('forecast'))
        previous=text(vals.get('previous'))
        combined=f"{date_text} {time_text}".strip()
        dt=None
        for fmt in ('%a %b %d %I:%M%p','%a %b %d %H:%M'):
            try:
                dt=datetime.strptime(combined,fmt).replace(year=default_year or datetime.now(CALENDAR_WAT).year,tzinfo=CALENDAR_WAT); break
            except Exception: pass
        if dt is None and current_date:
            for fmt in ('%a %b %d %Y %I:%M%p','%a %b %d %Y %H:%M'):
                try:
                    dt=datetime.strptime(f"{current_date} {time_text} {default_year or datetime.now(CALENDAR_WAT).year}",fmt).replace(tzinfo=CALENDAR_WAT); break
                except Exception: pass
        if dt is None: continue
        out.append({
            'event':event,'currency':currency.upper(),'impact':impact,'actual':actual or None,
            'forecast':forecast or None,'previous':previous or None,
            'wat_time':dt.isoformat(),'utc_time':dt.astimezone(timezone.utc).isoformat(),
            'date':dt.isoformat(),'country':currency.upper(),
            'tentative': 'TENTATIVE' in time_text.upper()
        })
    return out

def parse_ff_json_calendar(payload, default_tz=CALENDAR_WAT):
    """Parse the public weekly FF/FairEconomy JSON export."""
    out=[]
    if not isinstance(payload, list):
        return out
    for row in payload:
        try:
            raw_date=str(row.get('date') or '').strip()
            title=str(row.get('title') or row.get('event') or '').strip()
            currency=str(row.get('country') or row.get('currency') or '').strip().upper()
            impact=str(row.get('impact') or 'Low').strip().upper()
            if not raw_date or not title or not currency:
                continue
            dt=pd.to_datetime(raw_date, utc=True, errors='coerce')
            if pd.isna(dt):
                continue
            dt=dt.to_pydatetime().astimezone(default_tz)
            out.append({
                'event': title,
                'currency': currency,
                'impact': impact,
                'actual': row.get('actual') or None,
                'forecast': row.get('forecast') or None,
                'previous': row.get('previous') or None,
                'wat_time': dt.isoformat(),
                'utc_time': dt.astimezone(timezone.utc).isoformat(),
                'date': dt.isoformat(),
                'country': currency,
                'tentative': bool(row.get('tentative', False)) or 'TENTATIVE' in raw_date.upper(),
            })
        except Exception:
            continue
    return out

def fetch_ff_json_calendar(offset=0):
    """Reliable structured calendar feed; use sparingly because the public export is rate-limited."""
    if offset == 0:
        url='https://nfs.faireconomy.media/ff_calendar_thisweek.json'
    elif offset == 1:
        url='https://nfs.faireconomy.media/ff_calendar_nextweek.json'
    else:
        return []
    key=f'json:{offset}'
    cached=CALENDAR_CACHE.get(key)
    if cached and time.time()-cached.get('time',0)<1800:
        return cached.get('items',[])
    try:
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0 CollinsAI economic calendar'},timeout=20)
        r.raise_for_status()
        payload=r.json()
        items=parse_ff_json_calendar(payload)
        if items:
            CALENDAR_CACHE[key]={'time':time.time(),'items':items,'source':url}
            return items
    except Exception:
        pass
    return []

def fetch_ff_calendar(offset=0):
    # Structured weekly JSON is much more stable than scraping the rendered HTML table.
    if offset in (0, 1):
        structured=fetch_ff_json_calendar(offset)
        if structured:
            return structured
    sunday=calendar_week_start(offset=offset)
    key=sunday.isoformat()
    cached=CALENDAR_CACHE.get(key)
    if cached and time.time()-cached.get('time',0)<900: return cached.get('items',[])
    url=ff_week_url(sunday)
    try:
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0 CollinsAI economic calendar'},timeout=20)
        r.raise_for_status()
        items=parse_ff_calendar(r.text, sunday.year)
        CALENDAR_CACHE[key]={'time':time.time(),'items':items,'source':url}
        return items
    except Exception:
        CALENDAR_CACHE[key]={'time':time.time(),'items':[],'source':url}
        return []

def calendar_items(view='today',offset=0,date_value=None):
    today=datetime.now(CALENDAR_WAT).date()
    if view=='today':
        items=fetch_ff_calendar(0)
        start=end=today
        title=f"Today: {today.strftime('%a %b %d')}"
    elif view=='next':
        items=fetch_ff_calendar(1); start=calendar_week_start(offset=1); end=start+timedelta(days=6); title=f"Next Week: {start.strftime('%b %d')} - {end.strftime('%b %d')}"
    elif view=='previous':
        items=fetch_ff_calendar(-1); start=calendar_week_start(offset=-1); end=start+timedelta(days=6); title=f"Previous Week: {start.strftime('%b %d')} - {end.strftime('%b %d')}"
    elif view=='week':
        items=fetch_ff_calendar(offset); start=calendar_week_start(offset=offset); end=start+timedelta(days=6); title=("This Week" if offset==0 else ("Next Week" if offset>0 else "Previous Week"))+f": {start.strftime('%b %d')} - {end.strftime('%b %d')}"
    else:
        try: start=datetime.strptime(date_value,'%Y-%m-%d').date()
        except Exception: start=today
        week_start=start-timedelta(days=(start.weekday()+1)%7); items=fetch_ff_calendar((week_start-calendar_week_start()).days//7); end=start; title=f"{start.strftime('%A, %B %d, %Y')}"
    filtered=[]
    for x in items:
        try: d=pd.to_datetime(x.get('wat_time')).date()
        except Exception: continue
        if start<=d<=end: filtered.append(x)
    return filtered,title,start,end

# =========================================================
# HEALTH / SIGNALS
# =========================================================

@app.get("/api/health")
def health():
    return {"status":"ok","version":APP_VERSION,"engine":"MULTI-TIMEFRAME AI","database":"SQLITE","markets":len(MARKETS),"timeframes":{"entry":"15M","structure":"1H","major_trend":"4H"},"mode":"PAPER / NO REAL ORDERS"}

@app.get("/api/signals")
def signals():
    with signal_lock: snap=dict(signals_cache); snap["signals"]=list(signals_cache.get("signals",[]))
    return {"generated_at":snap.get("generated_at"),"mode":"LIVE MULTI-TIMEFRAME MARKET DATA","engine":"15M ENTRY + 1H STRUCTURE + 4H TREND + SMC + LIQUIDITY + FVG + CRT + EMA + RSI + MACD + BB + NEWS","timeframes":{"entry":"15min","structure":"1h","major_trend":"4h"},"markets":len(MARKETS),"scanner_status":snap.get("status"),"signals":snap.get("signals",[]),"markets_scanned":snap.get("markets_scanned",0),"last_market":snap.get("last_market",""),"note":"Index markets use ETF proxies where the data provider does not expose the exact CFD symbol. No real broker orders are placed."}

@app.get("/api/news")
def news_endpoint(symbol: str | None = None, view: str = "today", offset: int = 0, date: str | None = None, week: str | None = None):
    if symbol:
        internal=next((m["symbol"] for m in MARKETS if m["display"]==symbol or m["symbol"]==symbol),symbol)
        ctx=news_for(internal)
        return {"news":ctx.get("events",[]), "items":ctx.get("events",[]), **ctx}
    # Backward compatibility: old frontend used week=this/next.
    if week in ("this","next"):
        view=week
    if view not in ("today","week","next","previous","date"): view="today"
    items,title,start_date,end_date=calendar_items(view, int(offset), date)
    return {"news":items,"items":items,"available":bool(items),"source":"Forex Factory-style public calendar feed","view":view,"offset":int(offset),"title":title,"range_start":start_date.isoformat(),"range_end":end_date.isoformat(),"timezone":"Africa/Lagos (WAT, UTC+1)"}

# =========================================================
# PAPER TRADES
# =========================================================

def insert_paper_trade(sig):
    """Create one automatic paper-tracking record per active symbol/direction."""
    if sig.get("direction") not in ("BUY","SELL") or float(sig.get("score",0)) < 75 or sig.get("tp3") is None: return None
    now=datetime.now(timezone.utc).isoformat()
    key=f'{sig["internal_symbol"]}|{sig["direction"]}'
    c=db()
    active=c.execute("SELECT id FROM paper_trades WHERE signal_key=? AND status='ACTIVE'",(key,)).fetchone()
    if active:
        c.close(); return int(active["id"])
    c.execute("""INSERT INTO paper_trades(symbol,direction,entry,sl,tp1,tp2,tp3,tp4,tp5,rr1,rr2,rr3,rr4,rr5,score,status,created_at,proxy,signal_key,stage,last_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sig["symbol"],sig["direction"],sig["entry"],sig["sl"],sig["tp1"],sig["tp2"],sig["tp3"],None,None,sig["rr1"],sig["rr2"],sig["rr3"],None,None,sig["score"],"ACTIVE",now,int(sig.get("proxy",False)),key,"ENTRY",sig["entry"]))
    c.commit(); tid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.close(); return int(tid)

@app.post("/api/trades/paper")
def create_paper_trade(symbol: str):
    internal=next((m for m in MARKETS if m["display"]==symbol or m["symbol"]==symbol),None)
    if not internal: raise HTTPException(404,"Unknown market.")
    with signal_lock:
        sig=next((x for x in signals_cache.get("signals",[]) if x["internal_symbol"]==internal["symbol"]),None)
    if not sig or sig.get("direction") not in ("BUY","SELL"): raise HTTPException(400,"There is no qualifying BUY/SELL signal for this market.")
    tid=insert_paper_trade(sig)
    if tid is None: raise HTTPException(400,"This signal is below the paper-tracking threshold or has no valid TP3.")
    return {"message":"Paper trade tracking created. No real broker order was placed.","trade_id":tid,"trade":sig}

def row_trade(r): return dict(r)

@app.get("/api/trades")
def trades():
    c=db(); rows=c.execute("SELECT * FROM paper_trades ORDER BY id DESC").fetchall(); c.close(); return {"trades":[row_trade(r) for r in rows]}

@app.get("/api/trades/active")
def active_trades():
    c=db(); rows=c.execute("SELECT * FROM paper_trades WHERE status='ACTIVE' ORDER BY id DESC").fetchall(); c.close(); return {"trades":[row_trade(r) for r in rows]}

# =========================================================
# PAPER TRADE MONITOR
# =========================================================
def monitor_loop():
    while True:
        try:
            c=db(); rows=c.execute("SELECT * FROM paper_trades WHERE status='ACTIVE'").fetchall(); c.close()
            for r in rows:
                m=next((x for x in MARKETS if x["display"]==r["symbol"] or x["symbol"]==r["symbol"]),None)
                if not m: continue
                try:
                    df=get_market_data(m["provider"],"15min",5,cache_seconds=60)
                    candle=df.iloc[-1]; price=float(candle.close); high=float(candle.high); low=float(candle.low); direction=r["direction"]
                    hit_sl=(low<=r["sl"] if direction=="BUY" else high>=r["sl"])
                    now=datetime.now(timezone.utc).isoformat()
                    if hit_sl:
                        c=db(); c.execute("UPDATE paper_trades SET status='CLOSED',result='LOSS',closed_at=?,last_price=?,stage=? WHERE id=?",(now,price,"SL HIT",r["id"])); c.commit(); c.close(); continue
                    hits=[]
                    for n in (1,2,3):
                        tp=r[f"tp{n}"]
                        if tp is None: continue
                        touched=(high>=tp if direction=="BUY" else low<=tp)
                        if touched and not r[f"tp{n}_hit"]: hits.append(n)
                    if hits:
                        c=db()
                        sets=[]; vals=[]
                        for n in hits:
                            sets += [f"tp{n}_hit=1",f"tp{n}_hit_at=?"]; vals.append(now)
                        highest=max(hits); stage=f"TP{highest} HIT"
                        if highest==3:
                            sets += ["status='CLOSED'","result='PROFIT'","closed_at=?"]; vals.append(now)
                        sets += ["stage=?","last_price=?"]; vals += [stage,price,r["id"]]
                        c.execute(f"UPDATE paper_trades SET {', '.join(sets)} WHERE id=?",tuple(vals)); c.commit(); c.close()
                    else:
                        c=db(); c.execute("UPDATE paper_trades SET last_price=? WHERE id=?",(price,r["id"])); c.commit(); c.close()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(60)

threading.Thread(target=monitor_loop,daemon=True,name="collins-paper-monitor").start()

@app.get("/")
def root():
    return {"name":"Collins AI Signals API","version":APP_VERSION,"status":"online"}
