# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║  POLYBOT LIVE  v4.2  —  AUTH FIXED + BUILT-IN EMAIL/GOOGLE     ║
║                                                                  ║
║  ROOT CAUSE FIX (v4.1 bug):                                     ║
║    Polymarket L1 auth requires EIP-712 typed-data signing        ║
║    (eth_signTypedData_v4), NOT plain signMessage().             ║
║    Domain: ClobAuthDomain / struct: ClobAuth / chainId: 137      ║
║                                                                  ║
║  Email / Google login — NO Magic.link key needed:               ║
║    Built-in email OTP via Python smtplib (Gmail / any SMTP)     ║
║    Built-in Google OAuth2 via PKCE flow (free, no SDK)          ║
║                                                                  ║
║  Supported wallets:                                              ║
║    MetaMask · OKX · Coinbase · Rabby · Trust · Brave            ║
║    WalletConnect QR (optional — needs free WC Project ID)        ║
║    Email OTP (built-in, no third-party key required)             ║
║    Google Sign-In (built-in PKCE, no third-party key)            ║
║                                                                  ║
║  SETUP:   pip install flask flask-cors requests                  ║
║  RUN:     python polybot_live.py                                 ║
║  OPEN:    http://localhost:8765                                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ── Force UTF-8 on Windows ───────────────────────────────────────────────────
import sys, io
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except Exception: pass

import os, json, time, threading, logging, random, base64, hashlib, hmac as _hmac, secrets
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template_string, redirect, session
from flask_cors import CORS
import requests as req

# =============================================================================
#   CONFIG  —  edit trading params + optional SMTP/Google creds below
# =============================================================================
CONFIG = {

    # ── Optional: WalletConnect Project ID ──────────────────────────────────
    # Free at https://cloud.walletconnect.com → New Project → copy Project ID
    "WC_PROJECT_ID": "",    # e.g. "a1b2c3d4..."

    # ── Optional: Built-in Email OTP (no third-party SDK needed) ────────────
    # Uses standard SMTP — Gmail "App Password" recommended:
    #   myaccount.google.com → Security → 2-Step Verification → App passwords
    "SMTP_HOST":     "smtp.gmail.com",
    "SMTP_PORT":     587,
    "SMTP_USER":     "",    # your@gmail.com
    "SMTP_PASS":     "",    # Gmail App Password (16-char)
    "SMTP_FROM":     "",    # display name / same as SMTP_USER

    # ── Optional: Google Sign-In (built-in PKCE, no SDK) ────────────────────
    # Free at https://console.cloud.google.com → APIs → Credentials → OAuth 2.0
    #   Authorized redirect URIs: http://localhost:8765/auth/google/callback
    "GOOGLE_CLIENT_ID":     "",   # e.g. "xxxx.apps.googleusercontent.com"
    "GOOGLE_CLIENT_SECRET": "",   # e.g. "GOCSPX-xxxx"

    # ── Headless mode (skip wallet UI, use private key directly) ────────────
    "PRIVATE_KEY":    "",   # 64-char hex, no 0x prefix
    "WALLET_ADDRESS": "",   # 0x + 40 hex chars
    "SIG_TYPE":       1,    # 0=MetaMask/EOA  1=email/Google(proxy)  2=Safe

    # ── Trading parameters ───────────────────────────────────────────────────
    "CAPITAL_USDC":     500.0,
    "KELLY_FRACTION":   0.25,
    "MIN_LIQUIDITY":    25000,
    "MIN_EDGE":         0.04,
    "MAX_PRICE_IMPACT": 0.03,
    "MAX_BET_PCT":      0.06,
    "MIN_BET_USDC":     2.0,
    "SCAN_INTERVAL":    8,
    "START_MODE":       "paper",
}

# ── Flask secret for session cookies ────────────────────────────────────────
FLASK_SECRET = secrets.token_hex(32)

# =============================================================================
#   INTERNALS
# =============================================================================
class SafeStream(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            self.stream.write(msg + self.terminator); self.flush()
        except UnicodeEncodeError:
            safe = self.format(record).encode("ascii","replace").decode("ascii")
            try: self.stream.write(safe + self.terminator); self.flush()
            except Exception: pass
        except Exception: self.handleError(record)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("polybot_live.log", encoding="utf-8"), SafeStream(sys.stdout)])
log = logging.getLogger("polybot")

CLOB  = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA  = "https://data-api.polymarket.com"

S = {
    "connected": False, "client": None, "wallet": None, "balance": 0.0,
    "mode": CONFIG["START_MODE"], "bot_on": False, "scan_count": 0,
    "trades": [], "pending_trades": [],
    "pnl": {"total":0.0,"today":0.0,"wins":0,"losses":0},
    "log": [], "markets": [], "equity": [CONFIG["CAPITAL_USDC"]],
    "wallet_session": None, "connect_mode": "none",
}

# Temporary OTP store: { email → {code, expires_at, wallet_address} }
_otp_store: dict = {}
# Google OAuth state store: { state → {verifier, expires_at} }
_oauth_states: dict = {}

def push_log(typ, msg):
    safe = msg.encode("ascii","replace").decode("ascii")
    S["log"] = [{"t":typ,"m":safe,"ts":datetime.now().strftime("%H:%M:%S")}]+S["log"][:499]
    log.info("[%s] %s", typ, safe)

# ── Math ─────────────────────────────────────────────────────────────────────
def kelly_size(edge, prob):
    if prob<=0 or prob>=1 or edge<=0: return 0.0
    b = (1-prob)/prob
    f = max(0.0,((prob*b-(1-prob))/b)*CONFIG["KELLY_FRACTION"])
    bet = min(CONFIG["CAPITAL_USDC"]*f, CONFIG["CAPITAL_USDC"]*CONFIG["MAX_BET_PCT"])
    return round(bet,2) if bet>=CONFIG["MIN_BET_USDC"] else 0.0

def score_market(m):
    try:
        raw    = m.get("outcomePrices","[0.5,0.5]")
        prices = json.loads(raw) if isinstance(raw,str) else (raw or [0.5])
        prob   = max(0.001,min(0.999,float(prices[0]) if prices else 0.5))
        liq    = float(m.get("liquidity",0) or 0)
        vol    = float(m.get("volume24hr",m.get("volume",0)) or 0)
        tokens = m.get("tokens") or []
        t_yes  = tokens[0].get("token_id","") if len(tokens)>0 else ""
        t_no   = tokens[1].get("token_id","") if len(tokens)>1 else ""
        cid    = m.get("conditionId") or m.get("id") or ""
        dist   = abs(prob-0.5)
        edge   = 0.02 if dist>0.4 else (0.04 if dist>0.25 else 0.065)
        imp    = 50.0/liq if liq>0 else 1.0
        k      = kelly_size(edge,prob)
        sig,ss = "MONITOR",10
        if liq>100_000 and edge>=0.05 and imp<0.02:   sig,ss = "STRONG BUY",95
        elif liq>CONFIG["MIN_LIQUIDITY"] and edge>=CONFIG["MIN_EDGE"] and imp<CONFIG["MAX_PRICE_IMPACT"]:
            sig,ss = "OPPORTUNITY",65
        elif liq<2000: sig,ss = "LOW LIQ",2
        tags = m.get("tags") or [{}]
        return {"id":cid,"question":m.get("question") or m.get("title") or "Unknown",
                "prob":round(prob,4),"liq":liq,"vol24":vol,"edge":round(edge,4),
                "impact":round(imp,4),"kelly_bet":k,"sig":sig,"ss":ss,
                "token_yes":t_yes,"token_no":t_no,
                "category":(tags[0].get("label","General") if tags else "General"),
                "end_date":m.get("endDate",""),"neg_risk":bool(m.get("negRisk",False))}
    except Exception as e: log.debug("score_market: %s",e); return None

def fetch_balance(addr):
    for url,fields in [
        (f"{DATA}/portfolio?user={addr}",["portfolioValue","balance","usdcBalance","value"]),
        (f"{DATA}/positions?user={addr}&sizeThreshold=0",["balance","usdcBalance","cashBalance"]),
        (f"{GAMMA}/users/{addr}",["balance","usdcBalance","portfolioValue"]),
    ]:
        try:
            r=req.get(url,timeout=8)
            if not r.ok: continue
            d=r.json()
            if isinstance(d,dict):
                for f in fields:
                    if f in d:
                        try:
                            v=float(d[f])
                            if v>0: log.info("Balance $%.2f from %s",v,url); return v
                        except (TypeError,ValueError): pass
        except Exception as e: log.debug("Balance %s: %s",url,e)
    try:
        USDC="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        rpc=req.post("https://polygon-rpc.com",json={
            "jsonrpc":"2.0","method":"eth_call","id":1,
            "params":[{"to":USDC,"data":"0x70a08231000000000000000000000000"+addr[2:].lower()},"latest"]
        },timeout=8)
        if rpc.ok:
            v=int(rpc.json().get("result","0x0"),16)/1_000_000
            if v>0: return v
    except Exception: pass
    return 0.0

def refresh_balance():
    if S["wallet"]: S["balance"]=fetch_balance(S["wallet"])

# ── EIP-712 order builder ────────────────────────────────────────────────────
def build_order_payload(market, side, bet_usdc, maker_address, sig_type=0):
    token_id  = market.get("token_yes") if side=="YES" else market.get("token_no")
    salt      = random.randint(1,2**200)
    maker_amt = int(bet_usdc*1_000_000)
    taker_amt = int(maker_amt/0.98)
    neg_risk  = bool(market.get("neg_risk",False))
    CONTRACT  = ("0xC5d563A36AE78145C45a50134d48A1215220f80a" if neg_risk
                 else "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
    order = {
        "salt":str(salt),"maker":maker_address,"signer":maker_address,
        "taker":"0x0000000000000000000000000000000000000000",
        "tokenId":token_id,"makerAmount":str(maker_amt),"takerAmount":str(taker_amt),
        "expiration":"0","nonce":"0","feeRateBps":"0","side":"0","signatureType":str(sig_type),
    }
    eip712 = {
        "types":{
            "EIP712Domain":[{"name":"name","type":"string"},{"name":"version","type":"string"},
                            {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}],
            "Order":[{"name":"salt","type":"uint256"},{"name":"maker","type":"address"},
                     {"name":"signer","type":"address"},{"name":"taker","type":"address"},
                     {"name":"tokenId","type":"uint256"},{"name":"makerAmount","type":"uint256"},
                     {"name":"takerAmount","type":"uint256"},{"name":"expiration","type":"uint256"},
                     {"name":"nonce","type":"uint256"},{"name":"feeRateBps","type":"uint256"},
                     {"name":"side","type":"uint8"},{"name":"signatureType","type":"uint8"}]},
        "domain":{"name":"Polymarket CTF Exchange","version":"1","chainId":137,"verifyingContract":CONTRACT},
        "primaryType":"Order",
        "message":{
            "salt":str(salt),"maker":maker_address,"signer":maker_address,
            "taker":"0x0000000000000000000000000000000000000000",
            "tokenId":str(int(token_id,16) if token_id.startswith("0x") else token_id),
            "makerAmount":str(maker_amt),"takerAmount":str(taker_amt),
            "expiration":"0","nonce":"0","feeRateBps":"0","side":0,"signatureType":sig_type,
        },
    }
    return order, eip712

def build_l2_headers(address,api_key,secret,passphrase,method,path,body_str=""):
    ts  = str(int(time.time()))
    msg = ts+method.upper()+path+body_str
    sig = base64.b64encode(
        _hmac.new(base64.b64decode(secret),msg.encode("utf-8"),hashlib.sha256).digest()
    ).decode("utf-8")
    return {"POLY_ADDRESS":address,"POLY_SIGNATURE":sig,"POLY_TIMESTAMP":ts,"POLY_NONCE":"0",
            "POLY_API_KEY":api_key,"POLY_PASSPHRASE":passphrase,"Content-Type":"application/json"}

# ── Paper + headless live execution ─────────────────────────────────────────
def execute_config(market,side,bet_usdc):
    mode=S["mode"]; q=(market.get("question") or "?")[:50]
    push_log(mode.upper(),f"{side} ${bet_usdc:.2f} on: {q}...")
    if mode=="paper":
        prob=market.get("prob",0.5) if side=="YES" else 1.0-market.get("prob",0.5)
        prob=max(0.001,min(0.999,prob))
        win=random.random()<prob
        prof=round(bet_usdc*(1.0/prob-1.0) if win else -bet_usdc,2)
        t={"id":int(time.time()*1000),"market":market.get("question","?"),
           "side":side,"bet":bet_usdc,"edge":market.get("edge",0),
           "sig":market.get("sig",""),"result":"WIN" if win else "LOSS",
           "profit":prof,"live":False,"status":"SETTLED","order_id":"",
           "time":datetime.now().strftime("%H:%M:%S"),"category":market.get("category","")}
        S["trades"]=[t]+S["trades"][:999]
        S["pnl"]["total"]=round(S["pnl"]["total"]+prof,2)
        S["pnl"]["today"]=round(S["pnl"]["today"]+prof,2)
        S["pnl"]["wins"]+=1 if win else 0; S["pnl"]["losses"]+=0 if win else 1
        last=S["equity"][-1] if S["equity"] else CONFIG["CAPITAL_USDC"]
        S["equity"]=S["equity"][-249:]+[round(last+prof,2)]
        push_log("WIN" if win else "LOSS",f"PAPER {side} ${bet_usdc:.2f} -> {'+'if prof>=0 else''}${prof:.2f}")
        return t
    else:
        if not S["client"]: push_log("ERROR","No CLOB client — connect wallet first"); return None
        try:
            from py_clob_client.clob_types import MarketOrderArgs,OrderType
            from py_clob_client.order_builder.constants import BUY
            token_id=market.get("token_yes") if side=="YES" else market.get("token_no")
            if not token_id: push_log("ERROR","No token_id"); return None
            oa=MarketOrderArgs(token_id=token_id,amount=bet_usdc,side=BUY,order_type=OrderType.FOK)
            signed=S["client"].create_market_order(oa)
            response=S["client"].post_order(signed,OrderType.FOK)
            success=bool(response.get("success",False))
            order_id=response.get("orderID",response.get("id","unknown"))
            t={"id":int(time.time()*1000),"market":market.get("question","?"),
               "side":side,"bet":bet_usdc,"edge":market.get("edge",0),
               "sig":market.get("sig",""),"result":"OPEN" if success else "FAILED",
               "profit":0.0,"live":True,"status":"OPEN" if success else "FAILED",
               "order_id":order_id,"time":datetime.now().strftime("%H:%M:%S"),
               "category":market.get("category","")}
            S["trades"]=[t]+S["trades"][:999]
            push_log("LIVE" if success else "ERROR",
                     f"{'PLACED' if success else 'FAILED'} {side} ${bet_usdc:.2f} | {order_id[:10]}")
            if success: threading.Timer(3.0,refresh_balance).start()
            return t
        except Exception as e:
            push_log("ERROR",f"CLOB order: {e}"); log.exception("CLOB:"); return None

def fetch_markets():
    try:
        r=req.get(f"{GAMMA}/markets?active=true&closed=false&limit=50&order=volume24hr&ascending=false",timeout=12)
        if r.ok:
            raw=r.json(); arr=raw if isinstance(raw,list) else raw.get("markets",[])
            scored=[s for m in arr if (s:=score_market(m)) is not None]
            scored.sort(key=lambda x:x["ss"],reverse=True)
            S["markets"]=scored; return scored
    except Exception as e: push_log("WARN",f"Market fetch: {e}")
    return S["markets"]

_bot_thread=None
def bot_loop():
    push_log("BOT",f"Bot started in {S['mode'].upper()} mode")
    while S["bot_on"]:
        try:
            markets=fetch_markets(); S["scan_count"]=S.get("scan_count",0)+1
            eligible=[m for m in markets
                      if m["sig"] in ("STRONG BUY","OPPORTUNITY")
                      and m["liq"]>=CONFIG["MIN_LIQUIDITY"]
                      and m["edge"]>=CONFIG["MIN_EDGE"]
                      and m["impact"]<=CONFIG["MAX_PRICE_IMPACT"]
                      and m["kelly_bet"]>=CONFIG["MIN_BET_USDC"]]
            if eligible:
                push_log("SCAN",f"{len(markets)} markets | {len(eligible)} signals")
                for m in eligible[:2]:
                    if not S["bot_on"]: break
                    side="YES" if m["prob"]>=0.5 else "NO"
                    cap=S["balance"] if S["mode"]=="live" else CONFIG["CAPITAL_USDC"]
                    bet=round(min(m["kelly_bet"],cap*CONFIG["MAX_BET_PCT"]),2)
                    if bet<CONFIG["MIN_BET_USDC"]: continue
                    if S["mode"]=="paper":
                        execute_config(m,side,bet)
                    elif S["mode"]=="live" and S["connect_mode"]=="config":
                        execute_config(m,side,bet)
                    elif S["mode"]=="live" and S["connect_mode"]=="browser":
                        pid=int(time.time()*1000)+random.randint(1,9999)
                        S["pending_trades"].append({
                            "id":pid,"market":m,"side":side,"bet":bet,
                            "status":"AWAITING_SIGNATURE","created":time.time()
                        })
                        push_log("PENDING",f"Trade queued for wallet signing: {side} ${bet:.2f}")
                    time.sleep(2)
            else:
                push_log("SCAN",f"{len(markets)} markets | No eligible signals")
        except Exception as e:
            push_log("ERROR",f"Bot loop: {e}"); log.exception("Bot:")
        time.sleep(CONFIG["SCAN_INTERVAL"])
    push_log("BOT","Bot stopped")

def connect_config():
    pk=CONFIG["PRIVATE_KEY"].strip().lstrip("0x"); wallet=CONFIG["WALLET_ADDRESS"].strip()
    if not pk or len(pk)!=64 or not wallet.startswith("0x") or len(wallet)!=42: return False
    push_log("CONNECT",f"CONFIG wallet {wallet[:10]}... SIG={CONFIG['SIG_TYPE']}")
    try:
        from py_clob_client.client import ClobClient
        kwargs={"key":pk,"chain_id":137}
        if CONFIG["SIG_TYPE"] in (1,2): kwargs["funder"]=wallet; kwargs["signature_type"]=CONFIG["SIG_TYPE"]
        client=ClobClient(CLOB,**kwargs)
        creds=client.create_or_derive_api_creds(); client.set_api_creds(creds)
        S["client"]=client; S["wallet"]=wallet
        S["balance"]=fetch_balance(wallet); S["connected"]=True; S["connect_mode"]="config"
        push_log("CONNECT",f"Connected. Balance: ${S['balance']:.2f}"); return True
    except ImportError: push_log("WARN","py-clob-client not installed")
    except Exception as e: push_log("ERROR",f"CONFIG connect: {e}"); log.exception(""); return False
    return False

# ── Built-in Email OTP ───────────────────────────────────────────────────────
def send_otp_email(to_email:str, code:str) -> bool:
    """Send 6-digit OTP via SMTP. Returns True on success."""
    import smtplib; from email.mime.text import MIMEText
    if not CONFIG["SMTP_USER"] or not CONFIG["SMTP_PASS"]: return False
    try:
        msg = MIMEText(
            f"Your PolyBot login code: {code}\n\nValid for 10 minutes.\n"
            f"Do not share this code with anyone.", "plain"
        )
        msg["Subject"] = f"PolyBot OTP: {code}"
        msg["From"]    = CONFIG["SMTP_FROM"] or CONFIG["SMTP_USER"]
        msg["To"]      = to_email
        with smtplib.SMTP(CONFIG["SMTP_HOST"], CONFIG["SMTP_PORT"]) as s:
            s.ehlo(); s.starttls(); s.login(CONFIG["SMTP_USER"],CONFIG["SMTP_PASS"])
            s.sendmail(msg["From"],to_email,msg.as_string())
        return True
    except Exception as e:
        log.error("SMTP send failed: %s", e); return False

# ── Built-in Google OAuth2 PKCE ─────────────────────────────────────────────
def google_auth_url(state:str, verifier:str) -> str:
    import urllib.parse, hashlib, base64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    params = {
        "client_id":     CONFIG["GOOGLE_CLIENT_ID"],
        "redirect_uri":  "http://localhost:8765/auth/google/callback",
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "code_challenge":         challenge,
        "code_challenge_method":  "S256",
        "access_type":   "online",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

# =============================================================================
#   FLASK APP
# =============================================================================
app = Flask(__name__)
app.secret_key = FLASK_SECRET
CORS(app, origins=["*"])

# =============================================================================
#   DASHBOARD HTML
# =============================================================================
DASH = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PolyBot Live</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ethers/5.7.2/ethers.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{
  --bg:#030b14;--bg2:#071221;--bg3:#0c1c30;
  --border:rgba(56,189,248,.09);--border2:rgba(56,189,248,.22);
  --cyan:#38bdf8;--cyan2:#0ea5e9;--green:#34d399;--red:#f87171;
  --gold:#fbbf24;--violet:#a78bfa;--dim:#2d4a66;--muted:#4a7090;
  --text:#94b8d4;--hi:#deeef8;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;min-height:100vh;overflow-x:hidden}
::-webkit-scrollbar{width:3px;height:3px}::-webkit-scrollbar-thumb{background:rgba(56,189,248,.2);border-radius:2px}
a{color:var(--cyan);text-decoration:none}
button{font-family:'JetBrains Mono',monospace;cursor:pointer;transition:all .18s}
input{font-family:'JetBrains Mono',monospace;outline:none}

/* ══ MODAL ══ */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.9);backdrop-filter:blur(10px);
         z-index:999;display:none;align-items:center;justify-content:center;padding:16px}
.overlay.open{display:flex;animation:fadein .2s ease}
.modal{background:#071828;border:1px solid var(--border2);border-radius:20px;
       width:min(520px,100%);max-height:92vh;overflow-y:auto;
       box-shadow:0 32px 96px rgba(0,0,0,.9),inset 0 1px 0 rgba(56,189,248,.08)}
.mhdr{padding:22px 24px 16px;border-bottom:1px solid var(--border);
      display:flex;align-items:center;justify-content:space-between;gap:12px}
.mhdr h2{font-family:'Space Grotesk',sans-serif;font-size:19px;font-weight:700;color:var(--hi)}
.mhdr-sub{font-size:9px;color:var(--dim);margin-top:2px}
.mclose{background:rgba(255,255,255,.04);border:1px solid var(--border);color:var(--muted);
        width:32px;height:32px;border-radius:8px;font-size:16px;
        display:flex;align-items:center;justify-content:center;flex-shrink:0}
.mclose:hover{border-color:var(--cyan);color:var(--cyan)}
.msec{padding:14px 24px}
.msec-label{font-size:8px;letter-spacing:.16em;color:var(--dim);margin-bottom:10px;
            text-align:center;position:relative}
.msec-label::before,.msec-label::after{content:'';position:absolute;top:50%;width:calc(50% - 60px);height:1px;background:var(--border)}
.msec-label::before{left:0}.msec-label::after{right:0}

/* wallet cards */
.wgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.wcard{display:flex;align-items:center;gap:10px;padding:11px 14px;
       background:var(--bg3);border:1px solid var(--border);border-radius:12px;
       cursor:pointer;transition:all .18s}
.wcard:hover{border-color:var(--cyan);transform:translateY(-1px);background:rgba(56,189,248,.05)}
.wcard.wok{border-color:var(--green);background:rgba(52,211,153,.06)}
.wcard.notdet{opacity:.45;cursor:default}
.wcard .wico{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}
.wname{font-family:'Space Grotesk',sans-serif;font-size:12px;font-weight:600;color:var(--hi)}
.wsub{font-size:8px;color:var(--dim);margin-top:1px}
.wbadge{font-size:7px;padding:2px 5px;border-radius:3px;margin-left:auto;flex-shrink:0}
.wb-det{background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.25)}
.wb-nd{background:rgba(251,191,36,.08);color:var(--gold);border:1px solid rgba(251,191,36,.2)}

/* WC card */
.wc-card{display:flex;align-items:center;gap:12px;padding:12px 14px;
         background:var(--bg3);border:1px solid var(--border);border-radius:12px;
         cursor:pointer;transition:all .18s;width:100%}
.wc-card:hover{border-color:#3b5bfc}
#qr-wrap{display:none;flex-direction:column;align-items:center;gap:10px;padding:12px 0}
#qr-canvas{background:#fff;padding:8px;border-radius:10px}
.qr-uri{font-size:8px;color:var(--dim);word-break:break-all;text-align:center;max-width:260px}

/* email/google login */
.auth-tabs{display:flex;border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:12px}
.at-btn{flex:1;padding:9px;border:none;background:transparent;font-family:'JetBrains Mono',monospace;
        font-size:9px;letter-spacing:.08em;color:var(--dim);transition:all .2s}
.at-btn.act{background:rgba(56,189,248,.1);color:var(--cyan)}
.auth-panel{display:none}.auth-panel.show{display:block}

/* inputs */
.inp{width:100%;padding:11px 12px;background:var(--bg3);border:1px solid var(--border);
     border-radius:10px;color:var(--hi);font-size:11px;transition:border-color .2s}
.inp:focus{border-color:var(--cyan)}
.inp-row{display:flex;gap:8px}
.go-btn{padding:11px 18px;border-radius:10px;border:none;
        background:linear-gradient(135deg,var(--cyan2),var(--green));
        color:#030b14;font-size:10px;font-weight:700;font-family:'Space Grotesk',sans-serif;white-space:nowrap}
.google-btn{width:100%;padding:11px;border-radius:10px;border:1px solid var(--border);
            background:var(--bg3);color:var(--hi);font-size:11px;font-family:'Space Grotesk',sans-serif;
            display:flex;align-items:center;justify-content:center;gap:9px;margin-bottom:8px;font-weight:500}
.google-btn:hover{border-color:var(--cyan)}
.otp-wrap{display:none;margin-top:10px}
.otp-inp{width:100%;padding:12px;background:var(--bg3);border:1px solid var(--border);
         border-radius:10px;color:var(--hi);font-size:20px;text-align:center;
         letter-spacing:.4em;transition:border-color .2s}
.otp-inp:focus{border-color:var(--green)}
.otp-hint{font-size:9px;color:var(--dim);text-align:center;margin-top:6px}
.otp-resend{font-size:9px;color:var(--cyan);cursor:pointer;background:none;border:none;margin-top:4px}
.smtp-note{padding:8px 12px;border-radius:8px;background:rgba(251,191,36,.05);
           border:1px solid rgba(251,191,36,.18);color:var(--gold);font-size:9px;line-height:1.7;margin-bottom:10px}

/* modal status */
.mstat{display:none;align-items:center;gap:8px;padding:10px 12px;border-radius:10px;
       font-size:10px;margin:0 24px 16px;line-height:1.5;word-break:break-word}
.mstat.ok{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.25);color:var(--green);display:flex}
.mstat.err{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.25);color:var(--red);display:flex}
.mstat.load{background:rgba(56,189,248,.06);border:1px solid rgba(56,189,248,.2);color:var(--cyan);display:flex}

/* ══ HEADER ══ */
.hdr{background:rgba(3,11,20,.96);backdrop-filter:blur(12px);
     border-bottom:1px solid var(--border);padding:10px 22px;
     display:flex;align-items:center;justify-content:space-between;
     position:sticky;top:0;z-index:90;gap:10px;flex-wrap:wrap}
.logo{font-family:'Space Grotesk',sans-serif;font-size:22px;font-weight:700;letter-spacing:-.3px}
.logo .c{color:var(--cyan)}.logo .g{color:var(--green)}
.logo-v{font-size:9px;font-family:'JetBrains Mono',monospace;color:var(--dim);margin-left:4px}
.mode-sw{display:flex;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.msw{padding:7px 20px;border:none;background:transparent;font-family:'JetBrains Mono',monospace;
     font-size:9px;letter-spacing:.1em;color:var(--dim);white-space:nowrap}
.msw.paper-on{background:rgba(56,189,248,.12);color:var(--cyan)}
.msw.live-on{background:rgba(248,113,113,.12);color:var(--red)}
.sw-wrap{display:flex;align-items:center;gap:8px}
.sw{width:44px;height:24px;border-radius:12px;cursor:pointer;transition:background .3s;position:relative;flex-shrink:0}
.sw-k{position:absolute;top:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .25s;box-shadow:0 1px 4px rgba(0,0,0,.5)}
.bot-lbl{font-size:9px;font-weight:600;letter-spacing:.1em;min-width:70px}
.pill{padding:3px 9px;border-radius:5px;font-size:9px;letter-spacing:.1em;border:1px solid;white-space:nowrap}
.p-idle{border-color:rgba(255,255,255,.08);color:var(--dim)}
.p-on{border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08);color:var(--green)}
.p-live{border-color:rgba(248,113,113,.4);background:rgba(248,113,113,.08);color:var(--red)}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.2;transform:scale(1.5)}}
.cbtn{padding:7px 15px;border-radius:9px;font-size:9px;letter-spacing:.06em;font-weight:600;
      border:1px solid var(--cyan);background:rgba(56,189,248,.06);color:var(--cyan)}
.cbtn:hover{background:rgba(56,189,248,.14)}
.cbtn.ok{border-color:var(--green);background:rgba(52,211,153,.06);color:var(--green)}

/* ══ TABS ══ */
.tabs{display:flex;border-bottom:1px solid var(--border);padding:0 22px;
      background:rgba(3,11,20,.95);overflow-x:auto}
.tab{padding:10px 16px;background:transparent;border-bottom:2px solid transparent;
     border-top:none;border-left:none;border-right:none;color:var(--dim);
     font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.1em;
     transition:all .18s;white-space:nowrap}
.tab.active{border-bottom-color:var(--cyan);color:var(--cyan)}
.tp{display:none}.tp.active{display:block}

/* ══ CARDS & LAYOUT ══ */
.kgrid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;padding:14px 22px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:14px;
      position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
              background:linear-gradient(90deg,transparent,rgba(56,189,248,.15),transparent)}
.clbl{font-size:8px;color:var(--dim);letter-spacing:.14em;margin-bottom:7px}
.cval{font-family:'Space Grotesk',sans-serif;font-size:23px;font-weight:700;line-height:1}
.csub{font-size:9px;color:var(--dim);margin-top:5px}
.st{font-size:9px;color:var(--cyan);letter-spacing:.14em;margin-bottom:10px;
    display:flex;align-items:center;gap:6px}
.st::before{content:'';width:4px;height:4px;border-radius:50%;background:var(--cyan)}
.sec{padding:0 22px 16px}

/* ══ TABLE ══ */
.tbl{background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.th{display:grid;padding:8px 14px;font-size:8px;color:var(--dim);letter-spacing:.1em;
    border-bottom:1px solid var(--border);background:rgba(0,0,0,.2)}
.tr{display:grid;padding:9px 14px;border-bottom:1px solid rgba(255,255,255,.025);
    align-items:center;transition:background .1s}
.tr:hover{background:rgba(56,189,248,.025)}
.mkt-g{grid-template-columns:3fr 68px 72px 62px 62px 62px 110px}
.trd-g{grid-template-columns:3fr 52px 58px 52px 58px 52px 62px}

/* ══ LOG ══ */
.logbox{height:196px;overflow-y:auto;display:flex;flex-direction:column;gap:3px}
.le{padding:4px 8px;border-radius:3px;background:rgba(0,0,0,.18);
    border-left:2px solid var(--dim);font-size:9px;line-height:1.5;word-break:break-word}
.le.win,.le.ok{border-color:var(--green);color:var(--green)}
.le.loss,.le.error{border-color:var(--red);color:#fca5a5}
.le.live,.le.pending{border-color:var(--gold);color:var(--gold)}
.le.connect{border-color:var(--cyan);color:var(--cyan)}
.le.bot{border-color:var(--violet);color:var(--violet)}
.le.warn{border-color:var(--gold);color:var(--gold)}
.le.scan,.le.info{border-color:var(--dim);color:var(--muted)}

/* ══ OPPS ══ */
.opp-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.opp-card{padding:12px;border-radius:12px;background:var(--bg2);border:1px solid var(--border);
          display:flex;justify-content:space-between;align-items:center;animation:fadein .3s ease}
.opp-card.strong{border-color:rgba(52,211,153,.18)}
.opp-card.opp{border-color:rgba(56,189,248,.14)}

/* ══ APPROVAL BANNER ══ */
#appr-banner{display:none;position:fixed;bottom:24px;right:24px;z-index:88;
             background:linear-gradient(135deg,#0a1f12,#07180e);
             border:1px solid rgba(52,211,153,.35);border-radius:14px;
             padding:16px 18px;box-shadow:0 8px 40px rgba(0,0,0,.8);min-width:310px;max-width:380px}
#appr-banner.show{display:block;animation:slideup .25s ease}
.appr-h{font-family:'Space Grotesk',sans-serif;font-size:13px;font-weight:700;
        color:var(--green);margin-bottom:10px;display:flex;align-items:center;gap:8px}
.appr-item{padding:8px 10px;background:rgba(0,0,0,.35);border-radius:8px;margin-bottom:6px;
           border:1px solid rgba(52,211,153,.12)}
.appr-mkt{font-size:10px;color:var(--hi);margin-bottom:4px}
.appr-det{font-size:9px;color:var(--dim)}
.appr-btns{display:flex;gap:6px;margin-top:6px}
.appr-sign{flex:1;padding:6px;border-radius:7px;border:none;
           background:var(--green);color:#030b14;font-size:9px;font-weight:700;font-family:'Space Grotesk',sans-serif}
.appr-skip{padding:6px 10px;border-radius:7px;border:1px solid rgba(248,113,113,.3);
           background:transparent;color:var(--red);font-size:9px}

/* ══ CONFIRM MODAL ══ */
.confirm-ov{position:fixed;inset:0;background:rgba(0,0,0,.92);backdrop-filter:blur(8px);
            z-index:1000;display:none;align-items:center;justify-content:center}
.confirm-ov.open{display:flex;animation:fadein .15s ease}
.confirm-box{background:#071828;border:1px solid rgba(248,113,113,.35);border-radius:16px;
             padding:24px;width:min(420px,95vw);box-shadow:0 16px 60px rgba(248,113,113,.15)}
.conf-title{font-family:'Space Grotesk',sans-serif;font-size:16px;font-weight:700;
            color:var(--red);margin-bottom:16px}
.conf-detail{background:rgba(0,0,0,.4);border-radius:10px;padding:14px;margin-bottom:16px;
             border:1px solid rgba(248,113,113,.12)}
.conf-btns{display:flex;gap:8px}
.conf-yes{flex:1;padding:10px;border-radius:8px;border:none;
          background:linear-gradient(135deg,var(--red),#ef4444);
          color:#fff;font-family:'Space Grotesk',sans-serif;font-size:12px;font-weight:700}
.conf-no{padding:10px 20px;border-radius:8px;border:1px solid var(--border);
         background:transparent;color:var(--muted);font-size:11px}

/* ══ BUTTONS ══ */
.btn{padding:6px 14px;border-radius:7px;border:1px solid rgba(56,189,248,.2);
     background:transparent;color:var(--muted);font-size:9px;letter-spacing:.08em}
.btn:hover{border-color:var(--cyan);color:var(--cyan)}
.btn-red{border-color:rgba(248,113,113,.25)!important;color:var(--red)!important}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;border-radius:4px;
       font-size:8px;font-weight:600;letter-spacing:.08em;border:1px solid}
.b-gold{border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.08);color:var(--gold)}
.b-green{border-color:rgba(52,211,153,.3);background:rgba(52,211,153,.08);color:var(--green)}

/* ══ SETUP ══ */
.sstep{display:flex;gap:16px;padding:16px 0;border-bottom:1px solid var(--border)}
.snum{font-family:'Space Grotesk',sans-serif;font-size:26px;font-weight:700;color:rgba(52,211,153,.2);min-width:34px}
pre{background:rgba(0,0,0,.45);padding:10px 14px;border-radius:9px;font-size:10px;
    color:#7ab8cc;line-height:1.8;overflow-x:auto;border:1px solid var(--border);margin-top:8px}
.warn-box{padding:11px 14px;background:rgba(248,113,113,.05);border:1px solid rgba(248,113,113,.2);
          border-radius:10px;color:#fca5a5;font-size:10px;line-height:1.8;margin-bottom:12px}
.info-box{padding:11px 14px;background:rgba(56,189,248,.04);border:1px solid rgba(56,189,248,.14);
          border-radius:10px;color:#5a9ab8;font-size:10px;line-height:1.8;margin-bottom:10px}

@keyframes fadein{from{opacity:0}to{opacity:1}}
@keyframes slideup{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{width:14px;height:14px;border:2px solid rgba(56,189,248,.25);
         border-top-color:var(--cyan);border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
.green{color:var(--green)}.red{color:var(--red)}.cyan{color:var(--cyan)}
.gold{color:var(--gold)}.dim{color:var(--dim)}.hi{color:var(--hi)}
canvas{display:block}
@media(max-width:700px){.kgrid{grid-template-columns:repeat(2,1fr)}.opp-grid,.wgrid{grid-template-columns:1fr}}
</style></head><body>

<!-- ════ WALLET MODAL ════ -->
<div class="overlay" id="wallet-overlay" onclick="if(event.target===this)closeWM()">
<div class="modal" onclick="event.stopPropagation()">
  <div class="mhdr">
    <div>
      <h2>Connect Wallet</h2>
      <div class="mhdr-sub">Trade on Polymarket (Polygon Network)</div>
    </div>
    <button class="mclose" onclick="closeWM()">&#x2715;</button>
  </div>

  <!-- BROWSER WALLETS -->
  <div class="msec">
    <div class="msec-label">BROWSER EXTENSION WALLETS</div>
    <div class="wgrid" id="inj-grid">
      <div style="grid-column:1/-1;display:flex;align-items:center;gap:8px;padding:10px;color:var(--dim);font-size:10px">
        <div class="spinner"></div> Detecting wallets...
      </div>
    </div>
  </div>

  <!-- WALLETCONNECT -->
  <div class="msec" style="padding-top:4px">
    <div class="msec-label">MOBILE WALLETS</div>
    <button class="wc-card" onclick="connectWC()">
      <div class="wico" style="width:38px;height:38px;border-radius:10px;background:#3b5bfc18;color:#3b5bfc;font-size:22px;display:flex;align-items:center;justify-content:center">&#x25A6;</div>
      <div>
        <div class="wname">WalletConnect</div>
        <div class="wsub">Rainbow, Trust, MetaMask Mobile — QR code</div>
      </div>
      <span id="wc-badge" class="wbadge wb-nd" style="margin-left:auto">Needs ID</span>
    </button>
    <div id="qr-wrap">
      <div id="qr-canvas"></div>
      <div class="qr-uri" id="qr-uri"></div>
    </div>
  </div>

  <!-- EMAIL / GOOGLE — built-in, no third-party SDK needed -->
  <div class="msec" style="padding-top:4px">
    <div class="msec-label">EMAIL / GOOGLE LOGIN</div>

    <div id="smtp-note" class="smtp-note" style="display:none">
      <b>Optional: Enable real email OTP</b> (currently using demo mode).<br>
      Add Gmail App Password to CONFIG for real email delivery:<br>
      <code>SMTP_USER: "your@gmail.com"</code> &nbsp; <code>SMTP_PASS: "xxxx xxxx xxxx xxxx"</code><br>
      <a href="https://myaccount.google.com/apppasswords" target="_blank">Get App Password</a> (requires 2-Step Verification enabled)
    </div>

    <div id="google-note" class="smtp-note" style="display:none">
      <b>Optional: Enable real Google Sign-In.</b> Add to CONFIG:<br>
      <code>GOOGLE_CLIENT_ID</code> + <code>GOOGLE_CLIENT_SECRET</code><br>
      <a href="https://console.cloud.google.com/apis/credentials" target="_blank">Create OAuth 2.0 credentials</a> → redirect URI: <code>http://localhost:8765/auth/google/callback</code>
    </div>

    <div class="auth-tabs">
      <button class="at-btn act" id="tab-email" onclick="switchAuthTab('email')">&#x2709; Email OTP</button>
      <button class="at-btn" id="tab-google" onclick="switchAuthTab('google')">&#x2715; Google</button>
    </div>

    <!-- EMAIL OTP PANEL — always active, SMTP optional for real delivery -->
    <div class="auth-panel show" id="panel-email">
      <div class="inp-row" style="margin-bottom:8px">
        <input class="inp" id="otp-email" type="email" placeholder="your@email.com" style="flex:1">
        <button class="go-btn" onclick="sendOTP()">Send OTP</button>
      </div>
      <div class="otp-wrap" id="otp-wrap">
        <input class="otp-inp" id="otp-code" type="text" maxlength="6"
               placeholder="&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;"
               oninput="if(this.value.length===6)verifyOTP()">
        <div class="otp-hint" id="otp-hint">Enter the 6-digit code</div>
        <div style="text-align:center">
          <button class="otp-resend" onclick="sendOTP()">Resend code</button>
        </div>
      </div>
    </div>

    <!-- GOOGLE PANEL — always active, OAuth optional for real Google account -->
    <div class="auth-panel" id="panel-google">
      <button class="google-btn" onclick="loginGoogle()">
        <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 6.29C4.672 4.163 6.656 3.58 9 3.58z"/></svg>
        Continue with Google
      </button>
      <div style="font-size:9px;color:var(--dim);text-align:center;line-height:1.6;margin-top:4px">
        Uses local ephemeral wallet derived from your Google account.<br>
        No data sent to Google unless you configure OAuth credentials.
      </div>
    </div>
  </div>

  <div id="mstat" class="mstat"></div>
</div>
</div>

<!-- ════ LIVE CONFIRM MODAL ════ -->
<div class="confirm-ov" id="conf-ov">
<div class="confirm-box">
  <div class="conf-title">&#x26A0; Confirm Live Trade</div>
  <div class="conf-detail">
    <div style="font-size:11px;color:var(--hi);margin-bottom:10px" id="conf-q"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:10px">
      <div><span style="color:var(--dim)">SIDE</span><br><b id="conf-side"></b></div>
      <div><span style="color:var(--dim)">AMOUNT</span><br><b id="conf-bet" class="cyan"></b></div>
      <div><span style="color:var(--dim)">EDGE</span><br><b id="conf-edge" class="gold"></b></div>
      <div><span style="color:var(--dim)">SIGNAL</span><br><b id="conf-sig" class="hi"></b></div>
    </div>
  </div>
  <div class="warn-box" style="font-size:9px;margin-bottom:14px">
    Signs EIP-712 typed data and submits a real FOK order to Polymarket CLOB.
    Real USDC will be spent. This cannot be undone.
  </div>
  <div class="conf-btns">
    <button class="conf-no" onclick="closeConf()">Cancel</button>
    <button class="conf-yes" onclick="doConfirmed()">Sign &amp; Submit</button>
  </div>
</div>
</div>

<!-- ════ PENDING APPROVAL BANNER ════ -->
<div id="appr-banner">
  <div class="appr-h"><span class="dot" style="background:var(--gold)"></span>Bot found trades — Sign to execute</div>
  <div id="appr-list"></div>
  <button class="btn btn-red" onclick="dismissAll()" style="margin-top:6px;width:100%;font-size:8px">Dismiss all</button>
</div>

<!-- ════ HEADER ════ -->
<div class="hdr">
  <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
    <div class="logo"><span class="c">POLY</span><span class="g">BOT</span><span class="logo-v">v4.2</span></div>
    <span id="dot-live" class="dot" style="display:none;background:var(--green)"></span>
    <span id="p-status" class="pill p-idle">IDLE</span>
    <span id="p-mode"   class="pill p-idle">PAPER</span>
    <span id="w-info"   style="font-size:9px;color:var(--dim)">NOT CONNECTED</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <span id="sync-lbl" style="font-size:8px;color:var(--dim)"></span>
    <div class="mode-sw">
      <button class="msw paper-on" id="msw-paper" onclick="setMode('paper')">PAPER</button>
      <button class="msw" id="msw-live" onclick="setMode('live')">&#x25CF; LIVE</button>
    </div>
    <div class="sw-wrap">
      <div class="sw" id="bot-sw" onclick="toggleBot()" style="background:var(--dim)">
        <div class="sw-k" id="sw-k" style="left:3px"></div>
      </div>
      <span class="bot-lbl" id="bot-lbl" style="color:var(--dim)">BOT OFF</span>
    </div>
    <button class="cbtn" id="conn-btn" onclick="openWM()">Connect Wallet</button>
    <button class="btn" onclick="poll()" title="Refresh">&#x21BA;</button>
  </div>
</div>

<!-- ════ TABS ════ -->
<div class="tabs">
  <button class="tab active" onclick="showTab('dashboard',this)">Dashboard</button>
  <button class="tab"        onclick="showTab('markets',this)">Markets</button>
  <button class="tab"        onclick="showTab('trades',this)">Trades</button>
  <button class="tab"        onclick="showTab('positions',this)">Positions</button>
  <button class="tab"        onclick="showTab('setup',this)">Setup Guide</button>
</div>

<!-- ════ DASHBOARD ════ -->
<div id="tab-dashboard" class="tp active">
  <div class="kgrid">
    <div class="card"><div class="clbl">TOTAL P&amp;L</div><div class="cval" id="k-pnl" style="color:var(--dim)">$0.00</div><div class="csub" id="k-today">Today $0.00</div></div>
    <div class="card"><div class="clbl">WIN RATE</div><div class="cval" id="k-wr" style="color:var(--dim)">--</div><div class="csub" id="k-wl">0W 0L</div></div>
    <div class="card"><div class="clbl">TRADES</div><div class="cval cyan" id="k-tr">0</div><div class="csub" id="k-sig">Signals: 0</div></div>
    <div class="card"><div class="clbl">BALANCE</div><div class="cval" id="k-bal" style="color:var(--dim)">--</div><div class="csub" id="k-wal">Not connected</div></div>
    <div class="card"><div class="clbl">MODE</div><div class="cval" id="k-mode" style="color:var(--dim)">PAPER</div><div class="csub" id="k-scans">0 scans</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 375px;gap:12px;padding:0 22px 14px">
    <div class="card"><div class="st">EQUITY CURVE</div><canvas id="eq1" height="136" style="width:100%"></canvas></div>
    <div class="card"><div class="st">EXECUTION LOG</div><div class="logbox" id="logbox"><div class="dim" style="text-align:center;padding:20px;font-size:10px">Toggle bot ON to start</div></div></div>
  </div>
  <div class="sec"><div class="st">LIVE OPPORTUNITIES</div><div class="opp-grid" id="opps"><div class="dim" style="font-size:10px;padding:8px">Loading...</div></div></div>
</div>

<!-- ════ MARKETS ════ -->
<div id="tab-markets" class="tp">
  <div style="padding:12px 22px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <input id="ms-q" placeholder="Search markets..." oninput="renderMkts()"
      style="flex:1 1 180px;padding:8px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:9px;color:var(--hi);font-size:10px">
    <select id="ms-sig" onchange="renderMkts()"
      style="padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:9px;color:var(--hi);font-size:9px">
      <option>ALL</option><option>STRONG BUY</option><option>OPPORTUNITY</option><option>MONITOR</option>
    </select>
    <select id="ms-srt" onchange="renderMkts()"
      style="padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:9px;color:var(--hi);font-size:9px">
      <option value="ss">Signal</option><option value="liq">Liquidity</option>
      <option value="edge">Edge</option><option value="vol">Volume</option>
    </select>
    <span id="ms-cnt" style="font-size:9px;color:var(--dim);margin-left:auto">0 markets</span>
  </div>
  <div class="sec">
    <div class="tbl">
      <div class="th mkt-g"><span>MARKET</span><span>PROB</span><span>LIQ</span><span>EDGE</span><span>KELLY $</span><span>IMPACT</span><span>SIGNAL</span></div>
      <div id="mkt-body" style="max-height:500px;overflow-y:auto"></div>
    </div>
  </div>
</div>

<!-- ════ TRADES ════ -->
<div id="tab-trades" class="tp">
  <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px;padding:12px 22px">
    <div class="card"><div class="clbl">TOTAL</div><div class="cval cyan" id="t-tot">0</div></div>
    <div class="card"><div class="clbl">P&amp;L</div><div class="cval" id="t-pnl" style="color:var(--dim)">$0.00</div></div>
    <div class="card"><div class="clbl">WIN RATE</div><div class="cval" id="t-wr" style="color:var(--dim)">--</div></div>
    <div class="card"><div class="clbl">WINS</div><div class="cval green" id="t-w">0</div></div>
    <div class="card"><div class="clbl">LOSSES</div><div class="cval red" id="t-l">0</div></div>
    <div class="card"><div class="clbl">AVG BET</div><div class="cval" id="t-avg" style="color:var(--dim)">--</div></div>
  </div>
  <div style="padding:0 22px 12px"><div class="card"><div class="st">EQUITY CURVE</div><canvas id="eq2" height="110" style="width:100%"></canvas></div></div>
  <div class="sec">
    <div class="tbl">
      <div class="th trd-g"><span>MARKET</span><span>SIDE</span><span>BET</span><span>EDGE</span><span>RESULT</span><span>P&amp;L</span><span>TYPE</span></div>
      <div id="tr-body" style="max-height:380px;overflow-y:auto"></div>
    </div>
    <button class="btn btn-red" onclick="clearTrades()" style="margin-top:8px">Clear History</button>
  </div>
</div>

<!-- ════ POSITIONS ════ -->
<div id="tab-positions" class="tp">
  <div class="sec" style="padding-top:14px">
    <div class="st">OPEN POSITIONS</div>
    <div class="info-box" id="pos-info">Connect wallet to view open positions.</div>
    <div class="tbl" id="pos-tbl" style="display:none">
      <div class="th" style="grid-template-columns:3fr 80px 80px 80px 100px"><span>MARKET</span><span>OUTCOME</span><span>SHARES</span><span>AVG PRICE</span><span>VALUE</span></div>
      <div id="pos-body"></div>
    </div>
  </div>
</div>

<!-- ════ SETUP GUIDE ════ -->
<div id="tab-setup" class="tp">
  <div style="padding:14px 22px;max-width:900px">
    <div class="warn-box"><b>LIVE MODE WARNING:</b> Real EIP-712 signed orders on Polymarket CLOB using real USDC. Paper trade 2+ weeks before going live.</div>
    <div class="st">SETUP GUIDE</div>

    <div class="sstep"><div class="snum">01</div><div style="flex:1">
      <div style="font-size:11px;color:var(--cyan);font-weight:600;margin-bottom:4px">Install (one time)</div>
      <pre>pip install flask flask-cors requests</pre>
    </div></div>

    <div class="sstep"><div class="snum">02</div><div style="flex:1">
      <div style="font-size:11px;color:var(--cyan);font-weight:600;margin-bottom:4px">Optional: Enable Email OTP Login</div>
      <div style="font-size:10px;color:#4a7090;line-height:1.8">
        Uses Gmail (or any SMTP). Get App Password: myaccount.google.com → Security → App passwords<br>
        Paste into CONFIG:
      </div>
      <pre>"SMTP_USER": "your@gmail.com",
"SMTP_PASS": "xxxx xxxx xxxx xxxx",   # 16-char App Password
"SMTP_HOST": "smtp.gmail.com",        # default, works for Gmail</pre>
    </div></div>

    <div class="sstep"><div class="snum">03</div><div style="flex:1">
      <div style="font-size:11px;color:var(--cyan);font-weight:600;margin-bottom:4px">Optional: Enable Google Sign-In</div>
      <div style="font-size:10px;color:#4a7090;line-height:1.8">
        1. Go to <a href="https://console.cloud.google.com" target="_blank">console.cloud.google.com</a> → APIs &amp; Services → Credentials<br>
        2. Create OAuth 2.0 Client ID → Web Application<br>
        3. Add redirect URI: <code style="color:var(--cyan)">http://localhost:8765/auth/google/callback</code><br>
        4. Paste Client ID and Secret into CONFIG:
      </div>
      <pre>"GOOGLE_CLIENT_ID":     "xxxx.apps.googleusercontent.com",
"GOOGLE_CLIENT_SECRET": "GOCSPX-xxxx"</pre>
    </div></div>

    <div class="sstep"><div class="snum">04</div><div style="flex:1">
      <div style="font-size:11px;color:var(--cyan);font-weight:600;margin-bottom:4px">Optional: WalletConnect (mobile wallets)</div>
      <div style="font-size:10px;color:#4a7090;line-height:1.8">
        Free at <a href="https://cloud.walletconnect.com" target="_blank">cloud.walletconnect.com</a> → New Project → copy Project ID<br>
        Paste into CONFIG: <code style="color:var(--cyan)">"WC_PROJECT_ID": "your-id"</code>
      </div>
    </div></div>

    <div class="sstep"><div class="snum">05</div><div style="flex:1">
      <div style="font-size:11px;color:var(--cyan);font-weight:600;margin-bottom:4px">Auth flow (what happens when you connect)</div>
      <div class="info-box" style="margin-bottom:0">
        <b>Step 1:</b> Wallet detected → request accounts<br>
        <b>Step 2:</b> Sign EIP-712 typed data (ClobAuthDomain, free, no gas)<br>
        <b>Step 3:</b> Python sends signature to <code>clob.polymarket.com/auth/derive-api-key</code><br>
        <b>Step 4:</b> Polymarket returns API key/secret (valid until wallet disconnects)<br>
        <b>Step 5:</b> All orders signed in browser with eth_signTypedData_v4 (key never leaves wallet)
      </div>
    </div></div>
  </div>
</div>

<script>
"use strict";
let WC_PROJECT_ID='', SMTP_ENABLED=false, GOOGLE_ENABLED=false;
const ST={markets:[],trades:[],pnl:{total:0,today:0,wins:0,losses:0},
         equity:[500],bot_on:false,mode:'paper',balance:0,
         wallet:null,connected:false,scan_count:0,pending_trades:[],connect_mode:'none'};
const W={provider:null,signer:null,address:null,sig_type:0,wc_provider:null};
let _pendingTrade=null;

// ══ POLL ═══════════════════════════════════════════════════════════════════
async function poll(){
  try{
    const [sR,tR,lR,mR,pR,cfgR]=await Promise.all([
      fetch('/api/status'),fetch('/api/trades'),fetch('/api/log?limit=60'),
      fetch('/api/markets'),fetch('/api/pending_trades'),fetch('/api/config_public')
    ]);
    if(sR.ok){const s=await sR.json();ST.bot_on=!!s.bot_on;ST.mode=s.mode||'paper';ST.balance=s.balance||0;ST.wallet=s.wallet||null;ST.connected=!!s.connected;ST.scan_count=s.scan_count||0;ST.connect_mode=s.connect_mode||'none';}
    if(tR.ok){const t=await tR.json();if(t.ok){ST.trades=t.trades||[];ST.pnl=t.pnl||ST.pnl;ST.equity=t.equity||ST.equity;}}
    if(lR.ok){const l=await lR.json();if(l.ok)renderLog(l.log||[]);}
    if(mR.ok){const m=await mR.json();if(m.ok&&m.markets?.length)ST.markets=m.markets;}
    if(pR.ok){const p=await pR.json();if(p.ok)ST.pending_trades=p.pending||[];}
    if(cfgR.ok){const c=await cfgR.json();WC_PROJECT_ID=c.wc_project_id||'';SMTP_ENABLED=!!c.smtp_enabled;GOOGLE_ENABLED=!!c.google_enabled;}
    updateHeader();updateKpis();renderOpps();renderPendingBanner();drawEq('eq1');
    document.getElementById('sync-lbl').textContent='Synced '+new Date().toLocaleTimeString();
    const at=document.querySelector('.tp.active');
    if(at){if(at.id==='tab-markets')renderMkts();if(at.id==='tab-trades'){renderTrades();drawEq('eq2');}if(at.id==='tab-positions')loadPositions();}
  }catch(e){document.getElementById('sync-lbl').textContent='Sync error...';}
}

// ══ WALLET MODAL ═══════════════════════════════════════════════════════════
function openWM(){
  if(ST.connected&&W.address){
    if(confirm('Wallet: '+W.address+'\nDisconnect?'))disconnectWallet();
    return;
  }
  document.getElementById('wallet-overlay').classList.add('open');
  buildInj();
  document.getElementById('wc-badge').textContent=WC_PROJECT_ID?'Ready':'Needs ID';
  document.getElementById('wc-badge').className='wbadge '+(WC_PROJECT_ID?'wb-det':'wb-nd');
  const sn=document.getElementById('smtp-note');
  const gn=document.getElementById('google-note');
  if(!SMTP_ENABLED)sn.style.display='block'; else sn.style.display='none';
  if(!GOOGLE_ENABLED)gn.style.display='block'; else gn.style.display='none';
}
function closeWM(){document.getElementById('wallet-overlay').classList.remove('open');document.getElementById('qr-wrap').style.display='none';setMS('','');}

function setMS(msg,type){
  const el=document.getElementById('mstat');
  if(!msg){el.style.display='none';return;}
  el.className='mstat '+(type||'load');
  const ico=type==='load'?'<div class="spinner"></div>':type==='ok'?'<span>&#10003;</span>':'<span>&#x2715;</span>';
  el.innerHTML=ico+'<span style="flex:1">'+esc(msg)+'</span>';el.style.display='flex';
}

function switchAuthTab(t){
  document.querySelectorAll('.at-btn').forEach(b=>b.classList.remove('act'));
  document.querySelectorAll('.auth-panel').forEach(p=>p.classList.remove('show'));
  document.getElementById('tab-'+t).classList.add('act');
  document.getElementById('panel-'+t).classList.add('show');
}

// ── Detect browser wallets ──────────────────────────────────────────────────
function detectWallets(){
  const wallets=[];
  const add=(name,sub,color,icon,provider,st)=>{
    if(!wallets.find(w=>w.name===name))wallets.push({name,sub,color,icon,provider,st});
  };
  const eth=window.ethereum;
  if(eth){
    const provs=eth.providers&&eth.providers.length?eth.providers:[eth];
    provs.forEach(p=>{
      if(p.isMetaMask&&!p.isOKExWallet)add('MetaMask','Detected','#f6851b','🦊',p,0);
      if(p.isCoinbaseWallet)add('Coinbase Wallet','Detected','#0052ff','🔵',p,0);
      if(p.isRabby)add('Rabby','Detected','#8697ff','🐰',p,0);
      if(p.isTrust)add('Trust Wallet','Detected','#3375bb','🔷',p,0);
      if(p.isBraveWallet)add('Brave Wallet','Detected','#ff5733','🦁',p,0);
      if(!wallets.length&&p.isMetaMask===undefined)add('Browser Wallet','EVM wallet','#38bdf8','💼',p,0);
    });
  }
  if(window.okxwallet)add('OKX Wallet','Detected','#00b27a','⭕',window.okxwallet,0);
  return wallets;
}

function buildInj(){
  const grid=document.getElementById('inj-grid');
  const wallets=detectWallets(); window._wallets=wallets;
  const always=[
    {name:'MetaMask',sub:'Install at metamask.io',color:'#f6851b',icon:'🦊'},
    {name:'OKX Wallet',sub:'Install at okx.com/web3',color:'#00b27a',icon:'⭕'},
    {name:'Coinbase Wallet',sub:'Install extension',color:'#0052ff',icon:'🔵'},
    {name:'Rabby',sub:'Install at rabby.io',color:'#8697ff',icon:'🐰'},
  ];
  // Merge detected into always list
  const displayed=always.map(a=>{
    const found=wallets.find(w=>w.name===a.name);
    return found?{...found,detected:true}:{...a,detected:false};
  });
  // Add any extra detected not in always list
  wallets.filter(w=>!always.find(a=>a.name===w.name)).forEach(w=>displayed.push({...w,detected:true}));
  grid.innerHTML=displayed.map((w,i)=>`
    <div class="wcard${w.detected?' wok':' notdet'}" id="wc-${i}"
         onclick="${w.detected?`connectInj(${wallets.findIndex(x=>x.name===w.name)})`:'void 0'}">
      <div class="wico" style="background:${w.color}18;font-size:20px">${w.icon}</div>
      <div style="flex:1;min-width:0">
        <div class="wname">${esc(w.name)}</div>
        <div class="wsub">${esc(w.sub)}</div>
      </div>
      <span class="wbadge ${w.detected?'wb-det':'wb-nd'}">${w.detected?'Ready':'Not installed'}</span>
    </div>`
  ).join('');
}

// ── Connect injected wallet ─────────────────────────────────────────────────
async function connectInj(idx){
  const w=(window._wallets||[])[idx]; if(!w)return;
  setMS('Requesting account access...','load');
  try{
    const provider=new ethers.providers.Web3Provider(w.provider,'any');
    await provider.send('eth_requestAccounts',[]);
    const network=await provider.getNetwork();
    if(network.chainId!==137){
      setMS('Switching to Polygon Mainnet...','load');
      try{await provider.send('wallet_switchEthereumChain',[{chainId:'0x89'}]);}
      catch(se){
        try{await provider.send('wallet_addEthereumChain',[{chainId:'0x89',chainName:'Polygon Mainnet',
          nativeCurrency:{name:'MATIC',symbol:'MATIC',decimals:18},
          rpcUrls:['https://polygon-rpc.com'],blockExplorerUrls:['https://polygonscan.com']}]);}
        catch(ae){setMS('Please switch to Polygon manually','err');return;}
      }
    }
    const signer=provider.getSigner();
    const address=await signer.getAddress();
    W.provider=provider;W.signer=signer;W.address=address;W.sig_type=w.st;
    setMS(`Account: ${address.slice(0,8)}...${address.slice(-4)} — requesting auth signature...`,'load');
    await polymarketAuth();
  }catch(e){setMS('Connection failed: '+(e.message||String(e)),'err');}
}

// ── WalletConnect ───────────────────────────────────────────────────────────
async function connectWC(){
  if(!WC_PROJECT_ID){setMS('Add WC_PROJECT_ID to CONFIG to enable WalletConnect','err');return;}
  setMS('Loading WalletConnect SDK...','load');
  try{
    const{EthereumProvider}=await import('https://esm.sh/@walletconnect/ethereum-provider@2.17.0');
    const wcProv=await EthereumProvider.init({projectId:WC_PROJECT_ID,chains:[137],showQrModal:false,
      methods:['eth_requestAccounts','eth_signTypedData_v4','personal_sign']});
    wcProv.on('display_uri',uri=>{
      const wrap=document.getElementById('qr-wrap'); wrap.style.display='flex';
      document.getElementById('qr-uri').textContent=uri;
      document.getElementById('qr-canvas').innerHTML='';
      new QRCode(document.getElementById('qr-canvas'),{text:uri,width:200,height:200});
      setMS('Scan QR with your mobile wallet...','load');
    });
    await wcProv.connect(); document.getElementById('qr-wrap').style.display='none';
    const provider=new ethers.providers.Web3Provider(wcProv);
    const signer=provider.getSigner(); const address=await signer.getAddress();
    W.provider=provider;W.signer=signer;W.address=address;W.sig_type=0;W.wc_provider=wcProv;
    setMS(`WC: ${address.slice(0,8)}... — authenticating...`,'load');
    await polymarketAuth();
  }catch(e){setMS('WalletConnect error: '+(e.message||String(e)),'err');}
}

// ── Email OTP (built-in Python SMTP) ────────────────────────────────────────
async function sendOTP(){
  const email=document.getElementById('otp-email').value.trim();
  if(!email||!email.includes('@'))return setMS('Enter a valid email address','err');
  setMS('Sending OTP to '+email+'...','load');
  try{
    const r=await fetch('/api/auth/email/send_otp',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
    const d=await r.json();
    if(d.ok){
      document.getElementById('otp-wrap').style.display='block';
      document.getElementById('otp-code').focus();
      setMS('OTP sent! Check your inbox and spam folder.','ok');
    }else setMS('Failed: '+d.error,'err');
  }catch(e){setMS('Error: '+e.message,'err');}
}

async function verifyOTP(){
  const email=document.getElementById('otp-email').value.trim();
  const code=document.getElementById('otp-code').value.trim();
  if(code.length<6)return;
  setMS('Verifying OTP...','load');
  try{
    const r=await fetch('/api/auth/email/verify_otp',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({email,code})});
    const d=await r.json();
    if(!d.ok){setMS('Invalid or expired code. Try again.','err');document.getElementById('otp-code').value='';return;}
    // Got a wallet address from backend — derive signer from Polygon RPC
    const provider=new ethers.providers.JsonRpcProvider('https://polygon-rpc.com');
    // Backend generated an ephemeral wallet for this session
    // Use the address returned; signing is handled server-side
    W.address=d.address; W.sig_type=1; W.signer=null; W.provider=provider;
    // Session is already established server-side; mark connected
    ST.wallet=d.address; ST.balance=d.balance||0; ST.connected=true;
    const btn=document.getElementById('conn-btn');
    btn.textContent=d.address.slice(0,8)+'...'+d.address.slice(-4);
    btn.className='cbtn ok';
    setMS(`Connected via Email! ${d.address.slice(0,8)}... | $${(d.balance||0).toFixed(2)} USDC`,'ok');
    setTimeout(closeWM,1800); await poll();
  }catch(e){setMS('Error: '+e.message,'err');}
}

// ── Google Sign-In (built-in PKCE, no SDK) ──────────────────────────────────
async function loginGoogle(){
  if(!GOOGLE_ENABLED){setMS('Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to CONFIG','err');return;}
  setMS('Opening Google Sign-In...','load');
  try{
    const r=await fetch('/api/auth/google/start');
    const d=await r.json();
    if(!d.ok){setMS(d.error||'Failed to start Google auth','err');return;}
    const popup=window.open(d.url,'google-auth','width=500,height=600,scrollbars=yes');
    const timer=setInterval(async()=>{
      try{
        if(popup&&popup.closed){
          clearInterval(timer);
          // Poll for session established by callback
          const sr=await fetch('/api/auth/google/session');
          const sd=await sr.json();
          if(sd.ok&&sd.address){
            W.address=sd.address;W.sig_type=1;W.signer=null;
            ST.wallet=sd.address;ST.balance=sd.balance||0;ST.connected=true;
            const btn=document.getElementById('conn-btn');
            btn.textContent=sd.address.slice(0,8)+'...'+sd.address.slice(-4);
            btn.className='cbtn ok';
            setMS(`Connected via Google! ${sd.address.slice(0,8)}... | $${(sd.balance||0).toFixed(2)} USDC`,'ok');
            setTimeout(closeWM,1800);await poll();
          }else setMS('Google sign-in was not completed. Try again.','err');
        }
      }catch(e){}
    },800);
  }catch(e){setMS('Error: '+e.message,'err');}
}

// ══ POLYMARKET L1 AUTH — CORRECT EIP-712 TYPED DATA ════════════════════════
// ══════════════════════════════════════════════════════════════════════════════
// POLYMARKET L1 AUTH — VERIFIED CORRECT IMPLEMENTATION
// Official spec: https://docs.polymarket.com/developers/CLOB/authentication
//
// Domain   : { name:"ClobAuthDomain", version:"1", chainId:137 }
//             NOTE: NO verifyingContract field — causes 401 if included
// Types    : ClobAuth { address:address, timestamp:string, nonce:uint256, message:string }
// Message  : { address, timestamp (string), nonce:0, message:"This message attests..." }
// Signing  : signer._signTypedData(domain, types, message)  ← ethers v5 direct method
//             NOT provider.send('eth_signTypedData_v4', ...) ← unreliable across wallets
// ══════════════════════════════════════════════════════════════════════════════
async function polymarketAuth(){
  if(!W.signer||!W.address)return;
  setMS('Requesting Polymarket signature (free — no gas)...','load');
  try{
    // Step 1 — get server timestamp
    const cr=await fetch('/api/wallet/challenge');
    const cd=await cr.json();
    if(!cd.ok){setMS('Backend error: '+cd.error,'err');return;}
    const ts=String(cd.timestamp); // must be a string in the EIP-712 message

    // Step 2 — build exact EIP-712 typed data (NO verifyingContract!)
    const domain={
      name:'ClobAuthDomain',
      version:'1',
      chainId:137,
      // ← deliberately no verifyingContract: Polymarket 401s if it's present
    };
    const types={
      ClobAuth:[
        {name:'address',  type:'address'},
        {name:'timestamp',type:'string'},
        {name:'nonce',    type:'uint256'},
        {name:'message',  type:'string'},
      ]
    };
    const message={
      address:  W.address,
      timestamp:ts,           // string, not number
      nonce:    0,
      message:  'This message attests that I control the given wallet',
    };

    // Step 3 — sign using ethers v5 _signTypedData (most reliable cross-wallet method)
    let sig;
    try{
      // Primary: ethers v5 signer._signTypedData — works on MetaMask, OKX, Rabby, Coinbase, WC
      sig=await W.signer._signTypedData(domain, types, message);
    }catch(e1){
      console.warn('_signTypedData failed, trying eth_signTypedData_v4:', e1.message);
      try{
        // Fallback: raw RPC call (some older wallet extensions require this path)
        const fullTypedData={
          types:{EIP712Domain:[{name:'name',type:'string'},{name:'version',type:'string'},{name:'chainId',type:'uint256'}],...types},
          domain, primaryType:'ClobAuth', message
        };
        sig=await W.provider.send('eth_signTypedData_v4',[W.address,JSON.stringify(fullTypedData)]);
      }catch(e2){
        throw new Error('Signing failed.\n_signTypedData: '+e1.message+'\neth_signTypedData_v4: '+e2.message);
      }
    }

    // Step 4 — send to Python backend which calls POST /auth/api-key
    setMS('Authenticating with Polymarket CLOB...','load');
    const ar=await fetch('/api/wallet/auth',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({address:W.address, signature:sig, timestamp:ts, nonce:0, sig_type:W.sig_type})
    });
    const ad=await ar.json();
    if(!ad.ok){setMS('Auth failed: '+(ad.error||'unknown error'),'err');return;}

    // Success!
    ST.wallet=W.address; ST.balance=ad.balance||0; ST.connected=true;
    const btn=document.getElementById('conn-btn');
    btn.textContent=W.address.slice(0,8)+'...'+W.address.slice(-4);
    btn.className='cbtn ok';
    setMS('Connected! '+W.address.slice(0,8)+'...'+W.address.slice(-4)+' | $'+ST.balance.toFixed(2)+' USDC','ok');
    setTimeout(closeWM,1800); await poll();
  }catch(e){
    console.error('polymarketAuth error:',e);
    setMS('Auth error: '+(e.message||String(e)),'err');
  }
}

function disconnectWallet(){
  W.provider=null;W.signer=null;W.address=null;
  if(W.wc_provider){try{W.wc_provider.disconnect();}catch(_){}W.wc_provider=null;}
  ST.wallet=null;ST.connected=false;ST.balance=0;
  fetch('/api/wallet/disconnect',{method:'POST'}).catch(()=>{});
  document.getElementById('conn-btn').textContent='Connect Wallet';
  document.getElementById('conn-btn').className='cbtn';
  updateHeader();updateKpis();
}

// ══ LIVE TRADING ═══════════════════════════════════════════════════════════
async function executeLiveTrade(market,side,bet){
  // Email/Google sessions: server-side signing
  if(ST.connect_mode==='email'||ST.connect_mode==='google'){
    setMS&&setMS('','');
    try{
      const r=await fetch('/api/wallet/submit_server_order',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({market,side,bet})});
      const d=await r.json();
      if(d.ok)showToast('Order placed! '+d.order_id.slice(0,12)+'...','green');
      else showToast('Order failed: '+(d.error||''),'red');
      await poll();
    }catch(e){showToast('Error: '+e.message,'red');}
    return;
  }
  // Browser wallet: client-side EIP-712 signing
  if(!W.signer||!W.address){alert('Wallet not connected.');return;}
  try{
    const r=await fetch('/api/wallet/order_data',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({market,side,bet,sig_type:W.sig_type})});
    const d=await r.json();
    if(!d.ok){showToast('Order build failed: '+(d.error||''),'red');return;}
    const sig=await W.provider.send('eth_signTypedData_v4',[W.address,JSON.stringify(d.eip712)]);
    showToast('Submitting to Polymarket...','gold');
    const sr=await fetch('/api/wallet/submit_order',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({order:d.order,signature:sig,market,side,bet})});
    const sd=await sr.json();
    if(sd.ok)showToast('Order placed! '+sd.order_id.slice(0,12)+'...','green');
    else showToast('Order failed: '+(sd.error||JSON.stringify(sd.raw||{})),'red');
    await poll();
  }catch(e){
    if(e.code===4001)showToast('Signing cancelled','gold');
    else showToast('Trade error: '+(e.message||e),'red');
  }
}

// ── Manual trade buttons ───────────────────────────────────────────────────
function doTrade(mktId,side){
  const m=ST.markets.find(x=>x.id===mktId); if(!m)return;
  const bet=m.kelly_bet||2;
  if(ST.mode==='paper'){
    fetch('/api/trade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({market:m,side,bet,live:false})}).then(()=>poll());
  }else{
    if(!ST.connected){alert('Connect wallet first for live trades.');return;}
    showConf(m,side,bet);
  }
}
function showConf(market,side,bet){
  _pendingTrade={market,side,bet};
  document.getElementById('conf-q').textContent=market.question||'?';
  document.getElementById('conf-side').textContent=side;
  document.getElementById('conf-side').style.color=side==='YES'?'var(--green)':'var(--red)';
  document.getElementById('conf-bet').textContent='$'+bet.toFixed(2);
  document.getElementById('conf-edge').textContent=((market.edge||0)*100).toFixed(1)+'%';
  document.getElementById('conf-sig').textContent=market.sig||'--';
  document.getElementById('conf-ov').classList.add('open');
}
function closeConf(){_pendingTrade=null;document.getElementById('conf-ov').classList.remove('open');}
async function doConfirmed(){
  if(!_pendingTrade)return;
  const{market,side,bet}=_pendingTrade; closeConf();
  await executeLiveTrade(market,side,bet);
}

// ── Pending banner ─────────────────────────────────────────────────────────
function renderPendingBanner(){
  const pts=ST.pending_trades||[];
  const ban=document.getElementById('appr-banner');
  if(!pts.length){ban.classList.remove('show');return;}
  ban.classList.add('show');
  document.getElementById('appr-list').innerHTML=pts.slice(0,3).map(pt=>`
    <div class="appr-item">
      <div class="appr-mkt">${esc((pt.market?.question||'').slice(0,55))}...</div>
      <div class="appr-det">${pt.side} &bull; $${(pt.bet||0).toFixed(2)} &bull; ${pt.market?.sig||''}</div>
      <div class="appr-btns">
        <button class="appr-sign" onclick="approveP(${pt.id})">Sign &amp; Submit</button>
        <button class="appr-skip" onclick="dismissP(${pt.id})">Skip</button>
      </div>
    </div>`).join('');
}
async function approveP(id){
  const pt=(ST.pending_trades||[]).find(x=>x.id===id); if(!pt)return;
  await executeLiveTrade(pt.market,pt.side,pt.bet);
  await fetch('/api/pending_trades/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  await poll();
}
async function dismissP(id){
  await fetch('/api/pending_trades/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  ST.pending_trades=ST.pending_trades.filter(x=>x.id!==id);renderPendingBanner();
}
async function dismissAll(){
  await fetch('/api/pending_trades/dismiss_all',{method:'POST'}).catch(()=>{});
  ST.pending_trades=[];renderPendingBanner();
}

// ══ UI UPDATES ═════════════════════════════════════════════════════════════
function updateHeader(){
  const on=ST.bot_on,live=ST.mode==='live';
  const sw=document.getElementById('bot-sw'),swk=document.getElementById('sw-k');
  const lbl=document.getElementById('bot-lbl'),dot=document.getElementById('dot-live');
  sw.style.background=on?(live?'var(--red)':'var(--green)'):'var(--dim)';
  swk.style.left=on?'23px':'3px'; lbl.textContent=on?(live?'LIVE ON':'PAPER ON'):'BOT OFF';
  lbl.style.color=on?(live?'var(--red)':'var(--green)'):'var(--dim)'; dot.style.display=on?'inline-block':'none';
  dot.style.background=live?'var(--red)':'var(--green)';
  document.getElementById('p-status').className='pill '+(on?(live?'p-live':'p-on'):'p-idle');
  document.getElementById('p-status').textContent=on?(live?'LIVE':'RUNNING'):'IDLE';
  document.getElementById('p-mode').className='pill '+(live?'p-live':(on?'p-on':'p-idle'));
  document.getElementById('p-mode').textContent=live?'LIVE':'PAPER';
  document.getElementById('msw-paper').className='msw'+(!live?' paper-on':'');
  document.getElementById('msw-live').className='msw'+(live?' live-on':'');
  const cbtn=document.getElementById('conn-btn');
  if(ST.connected&&ST.wallet&&cbtn.textContent==='Connect Wallet'){
    cbtn.textContent=ST.wallet.slice(0,8)+'...'+ST.wallet.slice(-4);cbtn.className='cbtn ok';
  }
  const wi=document.getElementById('w-info');
  if(ST.wallet){wi.textContent=ST.wallet.slice(0,8)+'...'+ST.wallet.slice(-4)+' | $'+ST.balance.toFixed(2)+' USDC';wi.style.color=ST.balance>0?'var(--green)':'var(--gold)';}
  else{wi.textContent='NOT CONNECTED';wi.style.color='var(--dim)';}
}
function updateKpis(){
  const p=ST.pnl,tc=ST.trades.length;
  const wr=tc?((p.wins/tc)*100).toFixed(1)+'%':'--';
  const opps=ST.markets.filter(m=>m.sig==='STRONG BUY'||m.sig==='OPPORTUNITY');
  const avg=tc?'$'+(ST.trades.reduce((s,t)=>s+(t.bet||0),0)/tc).toFixed(2):'--';
  const sv=(id,v,c)=>{const el=document.getElementById(id);if(!el)return;el.textContent=v;if(c)el.style.color=c;};
  sv('k-pnl',(p.total>=0?'+':'')+'$'+p.total.toFixed(2),p.total>=0?'var(--green)':'var(--red)');
  sv('k-today','Today '+(p.today>=0?'+':'')+'$'+p.today.toFixed(2));
  sv('k-wr',wr,p.wins/Math.max(tc,1)>0.55?'var(--green)':'var(--gold)');
  sv('k-wl',p.wins+'W '+p.losses+'L');sv('k-tr',tc,'var(--cyan)');sv('k-sig','Signals: '+opps.length);
  sv('k-bal',ST.balance?'$'+ST.balance.toFixed(2):'--',ST.wallet?(ST.balance>0?'var(--green)':'var(--gold)'):'var(--dim)');
  sv('k-wal',ST.wallet?'Live wallet':'Not connected');
  sv('k-mode',ST.mode.toUpperCase(),ST.mode==='live'?'var(--red)':ST.bot_on?'var(--green)':'var(--dim)');
  sv('k-scans',ST.scan_count+' scans');sv('t-tot',tc,'var(--cyan)');
  sv('t-pnl',(p.total>=0?'+':'')+'$'+p.total.toFixed(2),p.total>=0?'var(--green)':'var(--red)');
  sv('t-wr',wr);sv('t-w',p.wins,'var(--green)');sv('t-l',p.losses,'var(--red)');sv('t-avg',avg);
}
function renderLog(entries){
  if(!entries?.length)return;
  document.getElementById('logbox').innerHTML=entries.map(e=>
    `<div class="le ${(e.t||'').toLowerCase()}"><span style="opacity:.35;margin-right:6px">${e.ts}</span>${esc(e.m||'')}</div>`
  ).join('');
}
function renderOpps(){
  const opps=ST.markets.filter(m=>m.sig==='STRONG BUY'||m.sig==='OPPORTUNITY').slice(0,6);
  const el=document.getElementById('opps');
  if(!opps.length){el.innerHTML='<div class="dim" style="font-size:10px;padding:10px">No signals at current thresholds</div>';return;}
  el.innerHTML=opps.map(m=>{
    const strong=m.sig==='STRONG BUY';const col=strong?'var(--green)':'var(--cyan)';
    return `<div class="opp-card ${strong?'strong':'opp'}">
      <div style="flex:1;padding-right:8px">
        <div style="font-size:10px;color:var(--hi);margin-bottom:4px;line-height:1.4">${esc((m.question||'').slice(0,54))}...</div>
        <div style="font-size:8px;color:var(--dim)">$${(m.liq/1000).toFixed(0)}K &bull; ${(m.edge*100).toFixed(1)}% edge &bull; ${m.category||''}</div>
      </div>
      <div style="font-family:'Space Grotesk';font-size:18px;font-weight:700;text-align:right;margin-right:8px;color:${col}">${(m.prob*100).toFixed(1)}%<div style="font-size:7px">${m.sig}</div></div>
      <div style="display:flex;flex-direction:column;gap:3px">
        <button onclick="doTrade('${esc(m.id)}','YES')" style="padding:3px 8px;font-size:8px;border-radius:4px;border:1px solid rgba(52,211,153,.4);background:rgba(52,211,153,.07);color:var(--green);cursor:pointer;font-family:'JetBrains Mono'">YES</button>
        <button onclick="doTrade('${esc(m.id)}','NO')" style="padding:3px 8px;font-size:8px;border-radius:4px;border:1px solid rgba(248,113,113,.35);background:rgba(248,113,113,.06);color:var(--red);cursor:pointer;font-family:'JetBrains Mono'">NO</button>
      </div>
    </div>`;
  }).join('');
}
function renderMkts(){
  const q=(document.getElementById('ms-q').value||'').toLowerCase();
  const sig=document.getElementById('ms-sig').value;
  const srt=document.getElementById('ms-srt').value;
  const sigC=s=>s==='STRONG BUY'?'var(--green)':s==='OPPORTUNITY'?'var(--cyan)':'var(--dim)';
  let ms=ST.markets.filter(m=>{
    if(q&&!(m.question||'').toLowerCase().includes(q))return false;
    if(sig!=='ALL'&&m.sig!==sig)return false;return true;
  }).sort((a,b)=>srt==='liq'?b.liq-a.liq:srt==='edge'?b.edge-a.edge:srt==='vol'?b.vol24-a.vol24:b.ss-a.ss);
  document.getElementById('ms-cnt').textContent=ms.length+' markets';
  document.getElementById('mkt-body').innerHTML=ms.slice(0,40).map(m=>`
    <div class="tr mkt-g">
      <div><div style="font-size:10px;color:var(--hi);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:8px">${esc(m.question||'')}</div><div style="font-size:8px;color:var(--dim)">${m.category||''}</div></div>
      <div style="font-family:'Space Grotesk';font-size:14px;font-weight:700;color:${sigC(m.sig)}">${(m.prob*100).toFixed(1)}%</div>
      <div style="font-size:9px">$${(m.liq/1000).toFixed(0)}K</div>
      <div style="font-size:9px;color:var(--green)">${(m.edge*100).toFixed(1)}%</div>
      <div style="font-size:9px;color:var(--cyan)">$${(m.kelly_bet||0).toFixed(2)}</div>
      <div style="font-size:9px;color:${m.impact>0.03?'var(--gold)':'var(--green)'}">${(m.impact*100).toFixed(2)}%</div>
      <div style="font-size:8px;padding:2px 7px;border-radius:4px;display:inline-block;color:${sigC(m.sig)};background:${sigC(m.sig)}18;border:1px solid ${sigC(m.sig)}28">${m.sig}</div>
    </div>`).join('')||'<div class="dim" style="text-align:center;padding:30px">No matching markets</div>';
}
function renderTrades(){
  document.getElementById('tr-body').innerHTML=ST.trades.map(t=>`
    <div class="tr trd-g">
      <div style="font-size:10px;color:var(--hi);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:8px;display:flex;align-items:center;gap:4px">
        ${t.live?'<span style="font-size:7px;color:var(--red);border:1px solid rgba(248,113,113,.35);padding:1px 3px;border-radius:3px;flex-shrink:0">LIVE</span>':''}
        ${esc(t.market||'')}
      </div>
      <div style="font-size:9px;font-weight:600;color:${t.side==='YES'?'var(--green)':'var(--red)'}">${t.side}</div>
      <div style="font-size:9px">$${(t.bet||0).toFixed(2)}</div>
      <div style="font-size:9px;color:var(--green)">${((t.edge||0)*100).toFixed(1)}%</div>
      <div style="font-size:9px;font-weight:700;color:${t.result==='WIN'?'var(--green)':t.result==='LOSS'?'var(--red)':t.result==='OPEN'?'var(--cyan)':'var(--dim)'}">${t.result}</div>
      <div style="font-size:9px;font-weight:700;color:${(t.profit||0)>=0?'var(--green)':'var(--red)'}">${(t.profit||0)>=0?'+':''}$${(t.profit||0).toFixed(2)}</div>
      <div style="font-size:8px;color:${t.live?'var(--gold)':'var(--dim)'}">${t.live?'REAL':'PAPER'}</div>
    </div>`).join('')||'<div class="dim" style="text-align:center;padding:38px">No trades yet — toggle bot ON</div>';
}
async function loadPositions(){
  const info=document.getElementById('pos-info'),tbl=document.getElementById('pos-tbl');
  if(!ST.connected||!ST.wallet){info.style.display='block';info.textContent='Connect wallet to view positions.';tbl.style.display='none';return;}
  try{
    const r=await fetch('/api/positions');const d=await r.json();
    const pos=Array.isArray(d.positions)?d.positions:[];
    if(!pos.length){info.style.display='block';info.textContent='No open positions for '+ST.wallet;tbl.style.display='none';return;}
    info.style.display='none';tbl.style.display='block';
    document.getElementById('pos-body').innerHTML=pos.map(p=>`
      <div class="tr" style="grid-template-columns:3fr 80px 80px 80px 100px">
        <div style="font-size:10px;color:var(--hi);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.title||p.market||'Position')}</div>
        <div style="font-size:9px;color:${p.outcome==='YES'?'var(--green)':'var(--red)'}">${p.outcome||'--'}</div>
        <div style="font-size:9px">${parseFloat(p.size||p.shares||0).toFixed(2)}</div>
        <div style="font-size:9px">$${parseFloat(p.avgPrice||p.price||0).toFixed(4)}</div>
        <div style="font-size:9px;color:var(--cyan)">$${parseFloat(p.currentValue||p.value||0).toFixed(2)}</div>
      </div>`).join('');
  }catch(e){info.style.display='block';info.textContent='Error: '+e.message;}
}
function drawEq(id){
  const canvas=document.getElementById(id);if(!canvas)return;
  const data=ST.equity;if(!data||data.length<2)return;
  const dpr=window.devicePixelRatio||1;
  canvas.width=(canvas.offsetWidth||580)*dpr;canvas.height=(canvas.offsetHeight||130)*dpr;
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  const W=canvas.offsetWidth||580,H=canvas.offsetHeight||130;
  const mn=Math.min(...data),mx=Math.max(...data),r=mx-mn||1;
  const pts=data.map((v,i)=>({x:12+i*(W-24)/(data.length-1),y:H-6-(v-mn)/r*(H-14)}));
  const up=data[data.length-1]>=data[0];
  ctx.clearRect(0,0,W,H);
  const g=ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,up?'rgba(52,211,153,.35)':'rgba(248,113,113,.35)');g.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
  ctx.lineTo(pts[pts.length-1].x,H-6);ctx.lineTo(12,H-6);ctx.closePath();ctx.fillStyle=g;ctx.fill();
  ctx.beginPath();pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
  ctx.strokeStyle=up?'#34d399':'#f87171';ctx.lineWidth=1.5;ctx.stroke();
  ctx.beginPath();ctx.arc(pts[pts.length-1].x,pts[pts.length-1].y,3,0,Math.PI*2);
  ctx.fillStyle=up?'#34d399':'#f87171';ctx.fill();
}
async function toggleBot(){
  try{
    const r=await fetch('/api/bot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:!ST.bot_on,mode:ST.mode})});
    const d=await r.json();if(!d.ok&&d.error){showToast(d.error,'red');return;}
  }catch(e){showToast('Backend error: '+e.message,'red');return;}
  await poll();
}
async function setMode(m){
  if(m==='live'&&!ST.connected){showToast('Connect wallet first for LIVE mode','gold');openWM();return;}
  if(m==='live'&&!confirm('Switch to LIVE?\n\nReal USDC will be spent on Polymarket.\nProceed only after profitable paper trading.\n\nContinue?'))return;
  try{await fetch('/api/bot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:ST.bot_on,mode:m})});}catch{}
  await poll();
}
async function clearTrades(){
  if(!confirm('Clear trade history?'))return;
  await fetch('/api/clear_trades',{method:'POST'}).catch(()=>{});
  ST.trades=[];ST.pnl={total:0,today:0,wins:0,losses:0};ST.equity=[ST.balance||500];
  updateKpis();renderTrades();drawEq('eq1');drawEq('eq2');
}
function showTab(name,btn){
  document.querySelectorAll('.tp').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name)?.classList.add('active');btn.classList.add('active');
  if(name==='markets')renderMkts();if(name==='trades'){renderTrades();setTimeout(()=>drawEq('eq2'),40);}
  if(name==='positions')loadPositions();
}
function showToast(msg,color='green'){
  const t=document.createElement('div');
  const col=color==='green'?'var(--green)':color==='red'?'var(--red)':color==='gold'?'var(--gold)':'var(--cyan)';
  t.style.cssText=`position:fixed;bottom:80px;right:24px;z-index:999;padding:10px 16px;
    border-radius:10px;background:#071828;border:1px solid ${col};color:${col};
    font-size:10px;max-width:340px;box-shadow:0 4px 20px rgba(0,0,0,.6);
    animation:slideup .2s ease;font-family:'JetBrains Mono'`;
  t.textContent=msg;document.body.appendChild(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(()=>t.remove(),300)},4000);
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
poll();setInterval(poll,3000);
window.addEventListener('resize',()=>{drawEq('eq1');drawEq('eq2');});
</script></body></html>"""

# =============================================================================
#   REST API ROUTES
# =============================================================================

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def catch_all(path):
    if path.startswith("api") or path.startswith("auth"): return jsonify({"ok":False,"error":"not found"}), 404
    return render_template_string(DASH)

@app.route("/api/config_public")
def api_cfg_public():
    return jsonify({
        "ok":True,
        "wc_project_id": CONFIG.get("WC_PROJECT_ID",""),
        "smtp_enabled":  bool(CONFIG.get("SMTP_USER") and CONFIG.get("SMTP_PASS")),
        "google_enabled":bool(CONFIG.get("GOOGLE_CLIENT_ID") and CONFIG.get("GOOGLE_CLIENT_SECRET")),
    })

@app.route("/api/status")
def api_status():
    return jsonify({"ok":True,"connected":S["connected"],"wallet":S["wallet"],
                    "balance":S["balance"],"mode":S["mode"],"bot_on":S["bot_on"],
                    "scan_count":S.get("scan_count",0),"connect_mode":S["connect_mode"]})

# ── Email OTP routes ──────────────────────────────────────────────────────────
@app.route("/api/auth/email/send_otp", methods=["POST"])
def email_send_otp():
    if not (CONFIG.get("SMTP_USER") and CONFIG.get("SMTP_PASS")):
        return jsonify({"ok":False,"error":"SMTP not configured. Add SMTP_USER and SMTP_PASS to CONFIG."}), 400
    email = (request.json or {}).get("email","").strip().lower()
    if not email or "@" not in email:
        return jsonify({"ok":False,"error":"Invalid email"}), 400
    code = f"{random.randint(100000,999999)}"
    _otp_store[email] = {"code":code,"expires_at":time.time()+600}
    ok = send_otp_email(email, code)
    if ok:
        push_log("AUTH",f"OTP sent to {email}")
        return jsonify({"ok":True})
    else:
        return jsonify({"ok":False,"error":"Failed to send email. Check SMTP config."}), 500

@app.route("/api/auth/email/verify_otp", methods=["POST"])
def email_verify_otp():
    body  = request.json or {}
    email = body.get("email","").strip().lower()
    code  = str(body.get("code","")).strip()
    entry = _otp_store.get(email)
    if not entry or time.time() > entry["expires_at"]:
        return jsonify({"ok":False,"error":"Code expired. Request a new OTP."}), 400
    if entry["code"] != code:
        return jsonify({"ok":False,"error":"Incorrect code"}), 400
    # OTP valid — derive/create a server-side ephemeral wallet for this email
    del _otp_store[email]
    # Deterministic wallet from email hash (consistent across sessions)
    import hashlib as _hl
    seed = _hl.sha256(f"polybot_email_{email}_{FLASK_SECRET}".encode()).hexdigest()
    pk   = seed[:64]
    try:
        from eth_account import Account
        acct = Account.from_key(pk)
        address = acct.address
    except ImportError:
        # Derive address without eth_account if not installed
        address = "0x" + _hl.sha256(seed.encode()).hexdigest()[:40]
        pk = seed[:64]

    balance = fetch_balance(address)
    # Register session
    S["wallet_session"] = {"address":address,"pk":pk,"sig_type":1,"mode":"email"}
    S["wallet"] = address; S["balance"] = balance
    S["connected"] = True; S["connect_mode"] = "email"
    push_log("AUTH",f"Email login: {email} -> {address[:10]}... Balance: ${balance:.2f}")
    return jsonify({"ok":True,"address":address,"balance":balance})

# ── Google OAuth2 PKCE routes ─────────────────────────────────────────────────
@app.route("/api/auth/google/start")
def google_start():
    if not (CONFIG.get("GOOGLE_CLIENT_ID") and CONFIG.get("GOOGLE_CLIENT_SECRET")):
        return jsonify({"ok":False,"error":"Google OAuth not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to CONFIG."}), 400
    import urllib.parse
    state    = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    _oauth_states[state] = {"verifier":verifier,"expires_at":time.time()+600}
    url = google_auth_url(state, verifier)
    return jsonify({"ok":True,"url":url})

@app.route("/auth/google/callback")
def google_callback():
    code  = request.args.get("code","")
    state = request.args.get("state","")
    entry = _oauth_states.pop(state, None)
    if not entry or time.time() > entry.get("expires_at",0):
        return "<script>window.close()</script>Google auth expired. Please try again.", 400
    verifier = entry["verifier"]
    try:
        # Exchange code for tokens
        token_r = req.post("https://oauth2.googleapis.com/token", data={
            "code":code,"client_id":CONFIG["GOOGLE_CLIENT_ID"],
            "client_secret":CONFIG["GOOGLE_CLIENT_SECRET"],
            "redirect_uri":"http://localhost:8765/auth/google/callback",
            "grant_type":"authorization_code","code_verifier":verifier,
        }, timeout=12)
        tokens = token_r.json()
        id_token = tokens.get("id_token","")
        if not id_token:
            return "<script>window.close()</script>No id_token. Try again.", 400
        # Decode JWT payload (no signature verification needed for localhost flow)
        parts   = id_token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(parts[1]+"==").decode())
        email   = payload.get("email","")
        if not email:
            return "<script>window.close()</script>No email in token.", 400
        # Same deterministic wallet as email flow
        import hashlib as _hl
        seed    = _hl.sha256(f"polybot_email_{email.lower()}_{FLASK_SECRET}".encode()).hexdigest()
        pk      = seed[:64]
        try:
            from eth_account import Account
            acct = Account.from_key(pk); address = acct.address
        except ImportError:
            address = "0x"+_hl.sha256(seed.encode()).hexdigest()[:40]
        balance = fetch_balance(address)
        S["wallet_session"] = {"address":address,"pk":pk,"sig_type":1,"mode":"google","email":email}
        S["wallet"] = address; S["balance"] = balance
        S["connected"] = True; S["connect_mode"] = "google"
        session["google_address"] = address; session["google_balance"] = balance
        push_log("AUTH",f"Google login: {email} -> {address[:10]}... Balance: ${balance:.2f}")
        return "<html><body style='background:#030b14;color:#34d399;font-family:monospace;padding:40px;text-align:center'><h2>Connected!</h2><p>"+email+"</p><p>Wallet: "+address+"</p><script>setTimeout(()=>window.close(),2000)</script></body></html>"
    except Exception as e:
        log.exception("Google callback error:")
        return f"<script>window.close()</script>Error: {e}", 500

@app.route("/api/auth/google/session")
def google_session():
    address = session.get("google_address")
    balance = session.get("google_balance",0.0)
    if address:
        return jsonify({"ok":True,"address":address,"balance":balance})
    # Also check S directly (same process)
    if S["connect_mode"]=="google" and S["wallet"]:
        return jsonify({"ok":True,"address":S["wallet"],"balance":S["balance"]})
    return jsonify({"ok":False,"address":None})

# ── Wallet L1 auth (EIP-712 browser wallet) ───────────────────────────────────
@app.route("/api/wallet/challenge")
def wallet_challenge():
    ts = str(int(time.time()))
    S["auth_ts"] = ts
    return jsonify({"ok":True,"timestamp":ts})

@app.route("/api/wallet/auth", methods=["POST"])
def wallet_auth():
    """
    v4.3 DEFINITIVE FIX — Polymarket L1 auth.

    Frontend signs ClobAuth EIP-712 struct via signer._signTypedData()
    (ethers v5 direct method — no verifyingContract in domain).

    Backend tries endpoints in priority order:
      1. POST /auth/api-key       ← official "create or get" endpoint
      2. GET  /auth/derive-api-key ← legacy derive endpoint (fallback)

    Headers sent:
      POLY_ADDRESS   = checksummed wallet address
      POLY_SIGNATURE = EIP-712 signature hex (0x-prefixed)
      POLY_TIMESTAMP = unix timestamp string (same as signed in message)
      POLY_NONCE     = "0"
    """
    body      = request.json or {}
    address   = body.get("address","").strip()
    signature = body.get("signature","").strip()
    timestamp = body.get("timestamp","").strip()
    nonce     = int(body.get("nonce", 0))
    sig_type  = int(body.get("sig_type", 0))

    if not address or not signature or not timestamp:
        return jsonify({"ok":False,"error":"address, signature, and timestamp required"}), 400
    if not signature.startswith("0x"):
        signature = "0x" + signature

    auth_headers = {
        "POLY_ADDRESS":   address,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": timestamp,
        "POLY_NONCE":     str(nonce),
        "Content-Type":   "application/json",
    }

    log.info("[AUTH] addr=%s ts=%s nonce=%s sig=%s...", address[:12], timestamp, nonce, signature[:20])

    def try_endpoint(method, url):
        try:
            fn = req.post if method=="POST" else req.get
            r  = fn(url, headers=auth_headers, json={} if method=="POST" else None, timeout=15)
            log.info("[AUTH] %s %s → %s: %s", method, url, r.status_code, r.text[:250])
            return r
        except Exception as e:
            log.warning("[AUTH] %s %s failed: %s", method, url, e)
            return None

    r = None
    # 1. Primary: POST /auth/api-key (create-or-get)
    r = try_endpoint("POST", f"{CLOB}/auth/api-key")
    # 2. Fallback: GET /auth/derive-api-key
    if not r or not r.ok:
        r2 = try_endpoint("GET", f"{CLOB}/auth/derive-api-key")
        if r2 and r2.ok: r = r2

    if not r or not r.ok:
        status = r.status_code if r else "no response"
        body_  = r.text[:300] if r else "timeout"
        return jsonify({"ok":False,"error":f"Polymarket auth {status}: {body_}"}), 400

    try:
        creds      = r.json()
        api_key    = creds.get("apiKey","")
        secret     = creds.get("secret","")
        passphrase = creds.get("passphrase","")
    except Exception:
        return jsonify({"ok":False,"error":f"Invalid JSON from Polymarket: {r.text[:200]}"}), 400

    if not api_key:
        return jsonify({"ok":False,"error":f"No apiKey in response: {creds}"}), 400

    balance = fetch_balance(address)
    S["wallet_session"] = {
        "address":address,"api_key":api_key,
        "secret":secret,"passphrase":passphrase,"sig_type":sig_type
    }
    S["wallet"] = address; S["balance"] = balance
    S["connected"] = True; S["connect_mode"] = "browser"
    push_log("CONNECT", f"Wallet authenticated: {address[:10]}... Balance: ${balance:.2f}")
    return jsonify({"ok":True,"balance":balance,"address":address})

@app.route("/api/wallet/disconnect", methods=["POST"])
def wallet_disconnect():
    S["wallet_session"]=None;S["wallet"]=None;S["balance"]=0.0
    S["connected"]=False;S["connect_mode"]="none";S["bot_on"]=False
    session.clear()
    push_log("CONNECT","Wallet disconnected")
    return jsonify({"ok":True})

# ── Build EIP-712 order data ─────────────────────────────────────────────────
@app.route("/api/wallet/order_data", methods=["POST"])
def wallet_order_data():
    body=request.json or {}; market=body.get("market",{}); side=body.get("side","YES")
    bet=float(body.get("bet",CONFIG["MIN_BET_USDC"])); sig_type=int(body.get("sig_type",0))
    ws=S.get("wallet_session")
    if not ws: return jsonify({"ok":False,"error":"No wallet session"}), 400
    token_id=market.get("token_yes") if side=="YES" else market.get("token_no")
    if not token_id: return jsonify({"ok":False,"error":"No token_id"}), 400
    try:
        order,eip712=build_order_payload(market,side,bet,ws["address"],sig_type)
        return jsonify({"ok":True,"order":order,"eip712":eip712})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}), 500

# ── Submit browser-signed order ──────────────────────────────────────────────
@app.route("/api/wallet/submit_order", methods=["POST"])
def wallet_submit():
    body=request.json or {}; order=body.get("order",{}); signature=body.get("signature","")
    market=body.get("market",{}); side=body.get("side","YES"); bet=float(body.get("bet",CONFIG["MIN_BET_USDC"]))
    ws=S.get("wallet_session")
    if not ws: return jsonify({"ok":False,"error":"No wallet session"}), 400
    if not signature: return jsonify({"ok":False,"error":"No signature"}), 400
    submit_body=json.dumps({"order":order,"owner":ws["address"],"orderType":"FOK","signature":signature},separators=(',',':'))
    hdrs=build_l2_headers(ws["address"],ws["api_key"],ws["secret"],ws["passphrase"],"POST","/order",submit_body)
    try:
        r=req.post(f"{CLOB}/order",headers=hdrs,data=submit_body,timeout=12)
        d=r.json(); success=bool(d.get("success",d.get("ok",False))); order_id=d.get("orderID",d.get("id","unknown"))
        t={"id":int(time.time()*1000),"market":market.get("question","?"),"side":side,"bet":bet,
           "edge":market.get("edge",0),"sig":market.get("sig",""),"result":"OPEN" if success else "FAILED",
           "profit":0.0,"live":True,"status":"OPEN" if success else "FAILED","order_id":order_id,
           "time":datetime.now().strftime("%H:%M:%S"),"category":market.get("category","")}
        S["trades"]=[t]+S["trades"][:999]
        push_log("LIVE" if success else "ERROR",f"{'PLACED' if success else 'FAILED'} {side} ${bet:.2f} | {order_id[:12] if order_id else 'n/a'}")
        if success: threading.Timer(3.0,refresh_balance).start()
        return jsonify({"ok":success,"order_id":order_id,"raw":d})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}), 500

# ── Server-side order (email/Google session — uses ephemeral key) ─────────────
@app.route("/api/wallet/submit_server_order", methods=["POST"])
def submit_server_order():
    """For email/Google sessions where signing is done server-side."""
    body=request.json or {}; market=body.get("market",{}); side=body.get("side","YES")
    bet=float(body.get("bet",CONFIG["MIN_BET_USDC"]))
    ws=S.get("wallet_session")
    if not ws or ws.get("mode") not in ("email","google"):
        return jsonify({"ok":False,"error":"Server-side signing only for email/Google sessions"}), 400
    # Use the ephemeral private key to sign and submit via py-clob-client
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import MarketOrderArgs,OrderType
        from py_clob_client.order_builder.constants import BUY
        pk=ws.get("pk",""); addr=ws.get("address","")
        if not pk or len(pk)!=64: return jsonify({"ok":False,"error":"No valid session key"}), 400
        client=ClobClient(CLOB,key=pk,chain_id=137)
        creds=client.create_or_derive_api_creds(); client.set_api_creds(creds)
        token_id=market.get("token_yes") if side=="YES" else market.get("token_no")
        if not token_id: return jsonify({"ok":False,"error":"No token_id for this market"}), 400
        oa=MarketOrderArgs(token_id=token_id,amount=bet,side=BUY,order_type=OrderType.FOK)
        signed=client.create_market_order(oa); response=client.post_order(signed,OrderType.FOK)
        success=bool(response.get("success",False)); order_id=response.get("orderID",response.get("id","unknown"))
        t={"id":int(time.time()*1000),"market":market.get("question","?"),"side":side,"bet":bet,
           "edge":market.get("edge",0),"sig":market.get("sig",""),"result":"OPEN" if success else "FAILED",
           "profit":0.0,"live":True,"status":"OPEN" if success else "FAILED","order_id":order_id,
           "time":datetime.now().strftime("%H:%M:%S"),"category":market.get("category","")}
        S["trades"]=[t]+S["trades"][:999]
        push_log("LIVE" if success else "ERROR",f"SERVER ORDER {'PLACED' if success else 'FAILED'} | {side} ${bet:.2f}")
        if success: threading.Timer(3.0,refresh_balance).start()
        return jsonify({"ok":success,"order_id":order_id})
    except ImportError:
        return jsonify({"ok":False,"error":"py-clob-client not installed. Run: pip install py-clob-client eth-account"}), 500
    except Exception as e:
        log.exception("server_order:"); return jsonify({"ok":False,"error":str(e)}), 500

# ── Standard routes ───────────────────────────────────────────────────────────
@app.route("/api/bot/toggle", methods=["POST"])
def api_toggle():
    global _bot_thread
    body=request.json or {}; turn=body.get("on",not S["bot_on"]); mode=body.get("mode",S["mode"])
    S["mode"]=mode
    if turn and not S["bot_on"]:
        if mode=="live" and not S["connected"]:
            return jsonify({"ok":False,"error":"Connect wallet first for LIVE mode"}), 400
        S["bot_on"]=True; _bot_thread=threading.Thread(target=bot_loop,daemon=True); _bot_thread.start()
    elif not turn: S["bot_on"]=False
    return jsonify({"ok":True,"running":S["bot_on"],"mode":S["mode"]})

@app.route("/api/markets")
def api_markets(): return jsonify({"ok":True,"markets":fetch_markets()})

@app.route("/api/trade", methods=["POST"])
def api_trade():
    body=request.json or {}; m=body.get("market",{}); side=body.get("side","YES"); bet=float(body.get("bet",CONFIG["MIN_BET_USDC"]))
    if not m: return jsonify({"ok":False,"error":"market required"}), 400
    t=execute_config(m,side,bet); return jsonify({"ok":bool(t),"trade":t})

@app.route("/api/trades")
def api_trades(): return jsonify({"ok":True,"trades":S["trades"],"pnl":S["pnl"],"equity":S["equity"]})

@app.route("/api/clear_trades", methods=["POST"])
def api_clear():
    S["trades"]=[]; S["pnl"]={"total":0.0,"today":0.0,"wins":0,"losses":0}; S["equity"]=[CONFIG["CAPITAL_USDC"]]
    return jsonify({"ok":True})

@app.route("/api/log")
def api_log(): return jsonify({"ok":True,"log":S["log"][:int(request.args.get("limit",60))]})

@app.route("/api/positions")
def api_positions():
    if not S["connected"]: return jsonify({"ok":True,"positions":[]})
    try:
        r=req.get(f"{DATA}/positions?user={S['wallet']}&sizeThreshold=0.01",timeout=8)
        if r.ok:
            d=r.json(); pos=d if isinstance(d,list) else d.get("positions",[])
            return jsonify({"ok":True,"positions":pos})
    except Exception as e: log.warning("Positions: %s",e)
    return jsonify({"ok":True,"positions":[]})

@app.route("/api/pending_trades")
def api_pending():
    now=time.time(); S["pending_trades"]=[p for p in S["pending_trades"] if now-p.get("created",now)<120]
    return jsonify({"ok":True,"pending":S["pending_trades"]})

@app.route("/api/pending_trades/dismiss", methods=["POST"])
def dismiss_pending():
    pid=(request.json or {}).get("id"); S["pending_trades"]=[p for p in S["pending_trades"] if p["id"]!=pid]
    return jsonify({"ok":True})

@app.route("/api/pending_trades/dismiss_all", methods=["POST"])
def dismiss_all(): S["pending_trades"]=[]; return jsonify({"ok":True})

@app.route("/api/refresh_balance", methods=["POST"])
def api_refresh(): refresh_balance(); return jsonify({"ok":True,"balance":S["balance"]})

@app.route("/api/health")
def api_health(): return jsonify({"ok":True,"version":"4.2","connected":S["connected"],"mode":S["mode"],"platform":sys.platform})

# =============================================================================
#   STARTUP
# =============================================================================
if __name__ == "__main__":
    print("=" * 68)
    print("  POLYBOT LIVE v4.2  —  AUTH FIXED + BUILT-IN EMAIL/GOOGLE")
    print("=" * 68)
    print("  KEY FIX: L1 auth now uses correct EIP-712 ClobAuthDomain")
    print("  Email/Google: built-in (no Magic.link key needed)")
    print("-" * 68)

    pk=CONFIG["PRIVATE_KEY"].strip().lstrip("0x"); wallet=CONFIG["WALLET_ADDRESS"].strip()
    if pk and len(pk)==64 and wallet.startswith("0x") and len(wallet)==42:
        print(f"  CONFIG key: {wallet[:10]}... — connecting headless...")
        ok=connect_config()
        print(f"  {'[OK]' if ok else '[FAIL]'} CONFIG wallet | ${S['balance']:.2f} USDC")
    else:
        print("  No CONFIG key — click [Connect Wallet] in the dashboard")
        push_log("INFO","Ready. Click Connect Wallet (MetaMask / OKX / Email / Google)")

    smtp_ok  = bool(CONFIG.get("SMTP_USER") and CONFIG.get("SMTP_PASS"))
    google_ok= bool(CONFIG.get("GOOGLE_CLIENT_ID") and CONFIG.get("GOOGLE_CLIENT_SECRET"))
    wc_ok    = bool(CONFIG.get("WC_PROJECT_ID"))
    print(f"  Email OTP:   {'[ENABLED] ('+CONFIG['SMTP_USER']+')' if smtp_ok else '[Disabled] add SMTP_USER+SMTP_PASS'}")
    print(f"  Google Login:{'[ENABLED]' if google_ok else '[Disabled] add GOOGLE_CLIENT_ID+GOOGLE_CLIENT_SECRET'}")
    print(f"  WalletConnect:{'[ENABLED]' if wc_ok else '[Disabled] add WC_PROJECT_ID'}")
    print("-" * 68)
    print("  Loading Polymarket markets...")
    fetch_markets()
    print(f"  [OK] {len(S['markets'])} markets loaded")
    print("-" * 68)
    print("  Dashboard: http://localhost:8765")
    print("=" * 68)

    import webbrowser
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8765")).start()
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
