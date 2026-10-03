import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
import gspread
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime, timedelta
from SmartApi import SmartConnect
from flask import Flask
import threading
import builtins

# ==========================================
# FORCE FLUSH LOGS IN RENDER
# ==========================================
def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# ==========================================
# FLASK WEB SERVER FOR RENDER PORT
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Angel One Option Chain Background State Engine is active and running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ==========================================
# CONFIGURATION & GOOGLE SHEETS
# ==========================================
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"
SYMBOL_SHEET_NAME = "Option chain index new"

def get_gspread_client():
    if os.path.exists("/etc/secrets/Credentials.json"):
        return gspread.service_account(filename="/etc/secrets/Credentials.json")
    if os.path.exists("Credentials.json"):
        return gspread.service_account(filename="Credentials.json")
    env_creds = os.getenv("GOOGLE_CREDENTIALS")
    if env_creds:
        creds_dict = json.loads(env_creds)
        return gspread.service_account_from_dict(creds_dict)
    raise Exception("No Credentials found for Google Sheets!")

try:
    gc = get_gspread_client()
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    data_sheet = spreadsheet.worksheet(MAIN_SHEET_NAME)
    dashboard_sheet = spreadsheet.worksheet(SYMBOL_SHEET_NAME)
    print("✅ SUCCESS: Google Sheets Connected Successfully!")
except Exception as e:
    print(f"❌ Google Sheet Connection Error: {e}")
    exit()

# ==========================================
# ROBUST IV & GREEKS
# ==========================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        if flag == 'CE':
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}

    if flag == 'CE':
        intrinsic = max(0, S - K * np.exp(-r * t))
    else:
        intrinsic = max(0, K * np.exp(-r * t) - S)
        
    if ltp < intrinsic:
        ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0:
            sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        if flag == 'CE':
            price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2)
        else:
            price = K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
        return price - ltp

    try:
        sigma = brentq(objective_function, 1e-4, 5.0, xtol=1e-6, maxiter=100)
    except Exception:
        sigma = 0.20

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(t))
    vega_val = S * norm.pdf(d1) * np.sqrt(t) / 100

    if flag == 'CE':
        delta = norm.cdf(d1)
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) - r * K * np.exp(-r * t) * norm.cdf(d2)) / 365
    else:
        delta = norm.cdf(d1) - 1
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) + r * K * np.exp(-r * t) * norm.cdf(-d2)) / 365

    return {
        'iv': round(sigma * 100, 2),
        'delta': round(delta, 3),
        'gamma': round(gamma, 4),
        'theta': round(theta, 2),
        'vega': round(vega_val, 2)
    }

# ==========================================
# GLOBAL CACHES & STATE ENGINE MEMORY (265+ SYMBOLS 9:15 AM - 3:30 PM)
# ==========================================
full_scrip_df = pd.DataFrame()
eq_cache = {}          
options_cache = {}     
morning_oi_dict = {}
baseline_date = None
watchlist_symbols = []
symbol_states_memory = {}

def update_fno_watchlist_alphabetical():
    global full_scrip_df, watchlist_symbols
    try:
        if full_scrip_df.empty:
            return
            
        fixed_indices_mcx = [
            "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX",
            "CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER", "ZINC", "ALUMINI", "LEAD"
        ]
        
        fno_df = full_scrip_df[
            (full_scrip_df['exch_seg'] == 'NFO') & 
            (full_scrip_df['instrumenttype'].isin(['OPTSTK', 'FUTSTK']))
        ]
        
        symbols = fno_df['name'].dropna().astype(str).str.upper().unique().tolist()
        
        formatted_fno = []
        for sym in symbols:
            clean_s = sym.strip()
            if "NSETEST" in clean_s or "TEST" in clean_s:
                continue
            if clean_s and clean_s not in fixed_indices_mcx:
                if clean_s in ["TATAMOTORS", "TATA", "TATA MOTORS LIMITED"]:
                    clean_s = "TMCV"
                formatted_fno.append(f"{clean_s}-EQ")
                
        sorted_fno_symbols = sorted(list(dict.fromkeys(formatted_fno)))
        final_watchlist_full = fixed_indices_mcx + sorted_fno_symbols
        watchlist_output = [[sym] for sym in final_watchlist_full]
        
        if watchlist_output:
            data_sheet.batch_clear(["Z10:Z700"])
            data_sheet.update(range_name=f"Z10:Z{9 + len(watchlist_output)}", values=watchlist_output)
            print(f"✅ Watchlist Updated Successfully! Total Symbols: {len(watchlist_output)}")
    except Exception as wl_err:
        print(f"⚠ Watchlist update error: {wl_err}")

def initialize_scrip_master(smart_api):
    global full_scrip_df, eq_cache, options_cache
    try:
        print("📥 Downloading Angel One Scrip Master JSON...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=25)
        if resp.status_code == 200:
            scrip_data = resp.json()
            full_scrip_df = pd.DataFrame(scrip_data)
            
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns:
                full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns:
                full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            
            if 'strike' in full_scrip_df.columns:
                full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0) / 100.0

            print("⚡ Pre-indexing EQ and Options for instant lookup...")
            
            nse_eq = full_scrip_df[(full_scrip_df['exch_seg'] == 'NSE') & (~full_scrip_df['instrumenttype'].str.contains('OPT|FUT', na=False))]
            for _, row in nse_eq.iterrows():
                sym = str(row['symbol']).strip().upper()
                name = str(row['name']).strip().upper()
                tok = str(row['token']).strip()
                eq_cache[sym] = (tok, 'NSE')
                eq_cache[name] = (tok, 'NSE')
                if sym.endswith("-EQ"):
                    eq_cache[sym.replace("-EQ", "")] = (tok, 'NSE')

            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce')
            fno_opts['parsed_expiry'] = fno_opts['parsed_expiry'].fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()):
                options_cache[name.strip()] = group
            
            if "TMCV" in options_cache:
                options_cache["TATAMOTORS"] = options_cache["TMCV"]
                options_cache["TATA"] = options_cache["TMCV"]
            elif "TATAMOTORS" in options_cache:
                options_cache["TMCV"] = options_cache["TATAMOTORS"]

            print(f"✅ Scrip Master Cached Successfully! Total Records: {len(full_scrip_df)}")
            update_fno_watchlist_alphabetical()
    except Exception as e:
        print(f"⚠ Scrip Master Download Error: {e}")

# ==========================================
# 🎯 EXACT PROVEN MATRIX ENGINE (V67 SHIFTED OPTIMIZED)
# ==========================================
def process_strict_independent_matrix(symbol, range_values, now_dt):
    global symbol_states_memory
    
    today_str = now_dt.strftime('%Y-%m-%d')
    if symbol not in symbol_states_memory:
        symbol_states_memory[symbol] = {
            'run_date': today_str,
            'cache': {}
        }
    
    mem = symbol_states_memory[symbol]
    if mem['run_date'] != today_str:
        mem['run_date'] = today_str
        mem['cache'] = {}
        
    cache = mem['cache']
    
    MARKET_ROW = 18 
    ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
    
    # STRICT SCANNING FOR CE SIDE ONLY: Row 15 to Row 28
    for r in range(15, 29):
        if r == MARKET_ROW: continue
        let_array_index = r - 8
        if let_array_index < 0 or let_array_index >= len(range_values): continue
        
        strike_val = range_values[let_array_index][13] # Column N is index 13
        strike = strike_val if strike_val not in [None, ""] else 0
        try:
            strike = float(strike)
        except:
            strike = 0
        if not strike: continue
        
        ce_oi_val  = range_values[let_array_index][7]  # Column H is index 7
        ce_vol_val = range_values[let_array_index][8]  # Column I is index 8
        
        ce_oi_raw  = str(ce_oi_val).replace("%", "").strip() if ce_oi_val not in [None, ""] else "0"
        ce_vol_raw = str(ce_vol_val).replace("%", "").strip() if ce_vol_val not in [None, ""] else "0"
        
        if "strong" in ce_oi_raw.lower(): ce_oi_raw = "100"
        if "strong" in ce_vol_raw.lower(): ce_vol_raw = "100"
        
        ceOiArray.append({'strike': strike, 'val': float(pd.to_numeric(pd.Series([ce_oi_raw]), errors='coerce').iloc[0]) or 0, 'row': r})
        ceVolArray.append({'strike': strike, 'val': float(pd.to_numeric(pd.Series([ce_vol_raw]), errors='coerce').iloc[0]) or 0, 'row': r})

    # STRICT SCANNING FOR PE SIDE ONLY: Row 8 to Row 21
    for r in range(8, 22):
        if r == MARKET_ROW: continue
        let_array_index = r - 8
        if let_array_index < 0 or let_array_index >= len(range_values): continue
        
        strike_val = range_values[let_array_index][13] # Column N is index 13
        strike = strike_val if strike_val not in [None, ""] else 0
        try:
            strike = float(strike)
        except:
            strike = 0
        if not strike: continue
        
        pe_vol_val = range_values[let_array_index][18] # Column S is index 18
        pe_oi_val  = range_values[let_array_index][19] # Column T is index 19
        
        pe_vol_raw = str(pe_vol_val).replace("%", "").strip() if pe_vol_val not in [None, ""] else "0"
        pe_oi_raw  = str(pe_oi_val).replace("%", "").strip() if pe_oi_val not in [None, ""] else "0"
        
        if "strong" in pe_vol_raw.lower(): pe_vol_raw = "100"
        if "strong" in pe_oi_raw.lower(): pe_oi_raw = "100"
        
        peVolArray.append({'strike': strike, 'val': float(pd.to_numeric(pd.Series([pe_vol_raw]), errors='coerce').iloc[0]) or 0, 'row': r})
        peOiArray.append({'strike': strike, 'val': float(pd.to_numeric(pd.Series([pe_oi_raw]), errors='coerce').iloc[0]) or 0, 'row': r})

    def extractTopTwo(dataArray):
        if not dataArray: return { 'highest': {'strike':0, 'val':0, 'row':0}, 'second': {'strike':0, 'val':0, 'row':0}, 'pct': 0 }
        filtered = [item for item in dataArray if item['val'] > 0]
        if not filtered: return { 'highest': {'strike':0, 'val':0, 'row':0}, 'second': {'strike':0, 'val':0, 'row':0}, 'pct': 0 }
        filtered.sort(key=lambda x: x['val'], reverse=True)
        
        highest = filtered[0]
        second = filtered[1] if len(filtered) > 1 else {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
        pct = (second['val'] / highest['val']) * 100 if highest['val'] > 0 else 0
        return { 'highest': highest, 'second': second, 'pct': pct }

    ceVolTop = extractTopTwo(ceVolArray)
    ceOiTop  = extractTopTwo(ceOiArray)
    peVolTop = extractTopTwo(peVolArray)
    peOiTop  = extractTopTwo(peOiArray)

    if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike'] or not peVolTop['highest']['strike'] or not peOiTop['highest']['strike']:
        return "(-----)", "(-----)", "(-----)", "(-----)", f"{symbol} is Neutral"

    def detectLiveStateAdvanced(topObj, isPutSide):
        if not topObj or topObj['highest']['val'] == 0 or topObj['pct'] < 75.0: return "STRONG"
        if isPutSide:
            return "wtb" if topObj['second']['strike'] < topObj['highest']['strike'] else "wtt"
        else:
            return "wtt" if topObj['second']['strike'] > topObj['highest']['strike'] else "wtb"

    ceVolRawState = detectLiveStateAdvanced(ceVolTop, False)
    ceOiRawState  = detectLiveStateAdvanced(ceOiTop, False)
    peVolRawState = detectLiveStateAdvanced(peVolTop, True)  
    peOiRawState  = detectLiveStateAdvanced(peOiTop, True)

    def getFullStatusString(topObj, rawState, cacheKey):
        if not topObj or not topObj['highest']['strike']: return "stable strong"
        strike = topObj['highest']['strike']
        prevStrike = cache.get("PREV_STRK_" + cacheKey, strike)
        currentWeakHist = cache.get("WEAK_HIST_" + cacheKey, "")
        
        if rawState == "STRONG":
            if currentWeakHist == "wtt": return "wtt to strong"
            if currentWeakHist == "wtb": return "wtb to strong"
            if strike != prevStrike: return "strong"
            return "stable strong"
        return rawState

    ceVolStatusText = getFullStatusString(ceVolTop, ceVolRawState, "CE_VOL")
    ceOiStatusText  = getFullStatusString(ceOiTop, ceOiRawState, "CE_OI")
    peVolStatusText = getFullStatusString(peVolTop, peVolRawState, "PE_VOL")
    peOiStatusText  = getFullStatusString(peOiTop, peOiRawState, "PE_OI")

    ceOiIsStable100  = ceOiRawState == "STRONG" or "to strong" in ceOiStatusText
    ceVolIsStable100 = ceVolRawState == "STRONG" or "to strong" in ceVolStatusText
    peOiIsStable100  = peOiRawState == "STRONG" or "to strong" in peOiStatusText
    peVolIsStable100 = peVolRawState == "STRONG" or "to strong" in peVolStatusText

    # CE Winner Logic
    ceWinner = "VOL"
    if ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiRawState == "wtb" and ceVolRawState == "wtb":
        ceWinner = "BOTH"
    elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and not ceOiIsStable100 and not ceVolIsStable100 and ceOiTop['second']['strike'] > ceOiTop['highest']['strike'] and ceVolTop['second']['strike'] > ceOiTop['highest']['strike']:
        ceWinner = "BOTH"
    elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and ceVolIsStable100:
        ceWinner = "BOTH"
    elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and not ceVolIsStable100 and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']:
        ceWinner = "OI"
    elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceVolIsStable100 and not ceOiIsStable100 and ceOiTop['second']['strike'] > ceOiTop['highest']['strike']:
        ceWinner = "VOL"
    elif ceOiIsStable100 and not ceVolIsStable100 and ceVolTop['second']['strike'] > ceOiTop['highest']['strike'] and ceOiTop['highest']['strike'] < ceVolTop['highest']['strike']:
        ceWinner = "OI"
    elif ceVolIsStable100 and not ceOiIsStable100 and ceOiTop['second']['strike'] > ceOiTop['highest']['strike'] and ceVolTop['highest']['strike'] < ceOiTop['highest']['strike']:
        ceWinner = "VOL"
    else:
        ceOiActiveRow = ceOiTop['highest']['row']
        if ceOiTop['second']['row'] > 0 and ceOiTop['pct'] >= 75.0: ceOiActiveRow = min(ceOiActiveRow, ceOiTop['second']['row'])
        ceVolActiveRow = ceVolTop['highest']['row']
        if ceVolTop['second']['row'] > 0 and ceVolTop['pct'] >= 75.0: ceVolActiveRow = min(ceVolActiveRow, ceVolTop['second']['row'])
        if ceVolActiveRow != ceOiActiveRow:
            ceWinner = "VOL" if ceVolActiveRow < ceOiActiveRow else "OI"
        else:
            ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"

    # PE Winner Logic
    peWinner = "VOL"
    if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt":
        peWinner = "BOTH"
    elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and not peOiIsStable100 and not peVolIsStable100 and peOiTop['second']['strike'] < peOiTop['highest']['strike'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']:
        peWinner = "BOTH"
    elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and peVolIsStable100:
        peWinner = "BOTH"
    elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and not peVolIsStable100 and peVolTop['second']['strike'] < peOiTop['highest']['strike']:
        peWinner = "OI"
    elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peVolIsStable100 and not peOiIsStable100 and peOiTop['second']['strike'] < peOiTop['highest']['strike']:
        peWinner = "VOL"
    elif peOiIsStable100 and not peVolIsStable100 and peVolTop['second']['strike'] < peOiTop['highest']['strike'] and peOiTop['highest']['strike'] > peVolTop['highest']['strike']:
        peWinner = "OI"
    elif peVolIsStable100 and not peOiIsStable100 and peOiTop['second']['strike'] < peOiTop['highest']['strike'] and peVolTop['highest']['strike'] > peOiTop['highest']['strike']:
        peWinner = "VOL"
    else:
        peOiActiveRow = peOiTop['highest']['row']
        if peOiTop['second']['row'] > 0 and peOiTop['pct'] >= 75.0: peOiActiveRow = max(peOiActiveRow, peOiTop['second']['row'])
        peVolActiveRow = peVolTop['highest']['row']
        if peVolTop['second']['row'] > 0 and peVolTop['pct'] >= 75.0: peVolActiveRow = max(peVolActiveRow, peVolTop['second']['row'])
        if peVolActiveRow != peOiActiveRow:
            peWinner = "VOL" if peVolActiveRow > peOiActiveRow else "OI"
        else:
            peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

    if ceOiRawState in ["wtt", "wtb"]: cache["WEAK_HIST_CE_OI"] = ceOiRawState
    if ceVolRawState in ["wtt", "wtb"]: cache["WEAK_HIST_CE_VOL"] = ceVolRawState
    if peOiRawState in ["wtt", "wtb"]: cache["WEAK_HIST_PE_OI"] = peOiRawState
    if peVolRawState in ["wtt", "wtb"]: cache["WEAK_HIST_PE_VOL"] = peVolRawState

    currentTimeStr = now_dt.strftime('%I:%M %p').lower()

    def calculateIndependentCell(topObj, cacheKey, sideLabel, isWinner, rawState, finalStatusText):
        if not topObj or not topObj['highest']['strike']:
            return {'text': "(-----)", 'status': "stable strong", 'scenarioStatus': "stable strong"}

        strike = topObj['highest']['strike']
        secondStrike = topObj['second']['strike'] if topObj['second']['val'] > 0 else strike
        prevStrike = cache.get("PREV_STRK_" + cacheKey, strike)
        if "PREV_STRK_" + cacheKey not in cache: cache["PREV_STRK_" + cacheKey] = strike

        displayStatus = finalStatusText
        scenarioStatus = finalStatusText
        isShifting = False
        shiftType = cache.get("SHIFT_TYPE_" + cacheKey, "")
        shiftFromStrike = cache.get("SHIFT_FROM_" + cacheKey, 0)
        lastTargetStrike = cache.get("SHIFT_TARGET_" + cacheKey, 0)

        if cache.get("IS_SHIFTED_" + cacheKey) == "true":
            if strike == shiftFromStrike or (lastTargetStrike > 0 and secondStrike != lastTargetStrike):
                cache["IS_SHIFTED_" + cacheKey] = "false"
                cache["PREV_STRK_" + cacheKey] = strike
                cache.pop("SHIFT_TARGET_" + cacheKey, None)

        if strike != prevStrike and prevStrike > 0 and finalStatusText in ["wtb", "wtt"]:
            if strike < prevStrike:
                shiftType = "top to bottom"
                scenarioStatus = "wtb"
            else:
                shiftType = "bottom to top"
                scenarioStatus = "wtt"
            cache["IS_SHIFTED_" + cacheKey] = "true"
            cache["SHIFT_TYPE_" + cacheKey] = shiftType
            cache["SHIFT_FROM_" + cacheKey] = prevStrike
            cache["SHIFT_TARGET_" + cacheKey] = secondStrike

        if cache.get("IS_SHIFTED_" + cacheKey) == "true":
            if rawState == "STRONG" or finalStatusText in ["stable strong", "wtt to strong", "wtb to strong"]:
                cache["IS_SHIFTED_" + cacheKey] = "false"
                cache["PREV_STRK_" + cacheKey] = strike
                isShifting = False
                scenarioStatus = finalStatusText
            else:
                isShifting = True
                shiftType = cache.get("SHIFT_TYPE_" + cacheKey)
                scenarioStatus = "wtb" if shiftType == "top to bottom" else "wtt"
        else:
            cache["PREV_STRK_" + cacheKey] = strike
            scenarioStatus = finalStatusText

        if not isWinner and rawState != "STRONG":
            displayStatus = "stable strong"
            scenarioStatus = "stable strong"

        displayTime = cache.get("TIME_" + cacheKey)
        lastRecordedStatus = cache.get("LAST_STATUS_" + cacheKey, "")
        isTriggerStatus = displayStatus in ["wtb", "wtt", "wtb to strong", "wtt to strong"] or isShifting

        if isTriggerStatus and displayStatus != lastRecordedStatus:
            displayTime = currentTimeStr
            cache["TIME_" + cacheKey] = displayTime
            cache["LAST_STATUS_" + cacheKey] = displayStatus
        elif not displayTime:
            displayTime = currentTimeStr
            cache["TIME_" + cacheKey] = displayTime
            cache["LAST_STATUS_" + cacheKey] = displayStatus

        if isShifting:
            outputText = f"({displayTime} - {sideLabel}-{strike}-{displayStatus}-{secondStrike})\n   (shifted {shiftType})"
        else:
            if displayStatus == "stable strong":
                outputText = f"({displayTime} - {sideLabel} - {strike})\n    (stable strong)"
            elif "to strong" in displayStatus:
                outputText = f"({displayTime} - {sideLabel} - {strike})\n   ({displayStatus})"
            else:
                weakDir = "weak towards top" if displayStatus == "wtt" else "weak towards bottom"
                outputText = f"({displayTime} - {sideLabel}-{strike}-{displayStatus}-{secondStrike})\n({weakDir})"

        return {'text': outputText, 'status': displayStatus, 'scenarioStatus': scenarioStatus}

    ceOiResult  = calculateIndependentCell(ceOiTop, "CE_OI", "Eor OI", ceWinner in ["OI", "BOTH"], ceOiRawState, ceOiStatusText)
    ceVolResult = calculateIndependentCell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner in ["VOL", "BOTH"], ceVolRawState, ceVolStatusText)
    peVolResult = calculateIndependentCell(peVolTop, "PE_VOL", "Eos VOL", peWinner in ["VOL", "BOTH"], peVolRawState, peVolStatusText)
    peOiResult  = calculateIndependentCell(peOiTop, "PE_OI", "Eos OI", peWinner in ["OI", "BOTH"], peOiRawState, peOiStatusText)

    finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
    finalPeState = peOiResult['scenarioStatus'] if peWinner == "OI" else peVolResult['scenarioStatus']

    if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
    if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

    checkCe = finalCeState
    checkPe = finalPeState

    # 27 Core Scenarios Mapping exactly matching your rules
    k3Text = f"{symbol} is Neutral"

    if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{symbol} is 8th scenario both side Risky"
    elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{symbol} is 9th scenario both side Risky"
    elif checkCe == "stable strong" and checkPe == "stable strong": k3Text = f"{symbol} is Neutral"
    elif checkCe == "wtb" and checkPe == "stable strong": k3Text = f"{symbol} is slightly Bearish"
    elif checkCe == "stable strong" and checkPe == "wtb": k3Text = f"{symbol} is slightly Bearish"
    elif checkCe == "wtt" and checkPe == "stable strong": k3Text = f"{symbol} is slightly bullish"
    elif checkCe == "stable strong" and checkPe == "wtt": k3Text = f"{symbol} is slightly bullish"
    elif checkCe == "wtt" and checkPe == "wtt": k3Text = f"{symbol} is Bullrun"
    elif checkCe == "wtb" and checkPe == "wtb": k3Text = f"{symbol} is bloodbath"
    elif checkCe == "wtb" and checkPe == "wtt to strong": k3Text = f"{symbol} is confirm Bloodbath"
    elif checkCe == "wtt to strong" and checkPe == "wtb": k3Text = f"{symbol} is confirm Bloodbath"
    elif checkCe == "wtt" and checkPe == "wtt to strong": k3Text = f"{symbol} is slightly bullish (Both side trade)"
    elif checkCe == "wtt to strong" and checkPe == "wtt": k3Text = f"{symbol} is slightly bullish (Both side trade)"
    elif checkCe == "wtt" and checkPe == "wtb to strong": k3Text = f"{symbol} is confirm Bull Run"
    elif checkCe == "wtb to strong" and checkPe == "wtt": k3Text = f"{symbol} is confirm Bull Run"
    elif checkCe == "wtb to strong" and checkPe == "wtb": k3Text = f"{symbol} is slightly Bearish (Both side trade)"
    elif checkCe == "wtb" and checkPe == "wtb to strong": k3Text = f"{symbol} is slightly Bearish (both side trade)"
    elif checkCe == "wtb to strong" and checkPe == "stable strong": k3Text = f"{symbol} is slightly Bullish"
    elif checkCe == "stable strong" and checkPe == "wtb to strong": k3Text = f"{symbol} is slightly Bullish"
    elif checkCe == "wtt to strong" and checkPe == "stable strong": k3Text = f"{symbol} is slightly Bearish"
    elif checkCe == "stable strong" and checkPe == "wtt to strong": k3Text = f"{symbol} is slightly Bearish"
    elif checkCe == "wtt to strong" and checkPe == "wtb to strong": k3Text = f"{symbol} is 8th scenario both side Risky trade"
    elif checkCe == "wtb to strong" and checkPe == "wtt to strong": k3Text = f"{symbol} is 9th scenario both side Risky trade"
    elif checkCe == "wtb to strong" and checkPe == "wtb to strong": k3Text = f"{symbol} is confirm Bull Run"
    elif checkCe == "wtt to strong" and checkPe == "wtt to strong": k3Text = f"{symbol} is confirm bloodbath"

    finalD5 = ceOiResult['text'] if ceWinner == "BOTH" else ("(-----)" if ceWinner == "VOL" else ceOiResult['text'])
    finalH5 = ceVolResult['text'] if ceWinner == "BOTH" else (ceVolResult['text'] if ceWinner == "VOL" else "(-----)")
    finalU5 = peOiResult['text'] if ceWinner == "BOTH" else ("(-----)" if peWinner == "VOL" else peOiResult['text'])
    finalQ5 = peVolResult['text'] if ceWinner == "BOTH" else (peVolResult['text'] if peWinner == "VOL" else "(-----)")

    if ceWinner == "VOL":
        finalD5 = "(-----)"
        finalH5 = ceVolResult['text']
    elif ceWinner == "OI":
        finalD5 = ceOiResult['text']
        finalH5 = "(-----)"
    elif ceWinner == "BOTH":
        finalD5 = ceOiResult['text']
        finalH5 = ceVolResult['text']

    if peWinner == "VOL":
        finalU5 = "(-----)"
        finalQ5 = peVolResult['text']
    elif peWinner == "OI":
        finalU5 = peOiResult['text']
        finalQ5 = "(-----)"
    elif peWinner == "BOTH":
        finalU5 = peOiResult['text']
        finalQ5 = peVolResult['text']

    return finalD5, finalH5, finalQ5, finalU5, k3Text

# ==========================================
# FETCH & BACKGROUND STATE ENGINE LOOP (9:15 AM TO 3:30 PM)
# ==========================================
def fetch_and_update_option_chain(smart_api):
    global morning_oi_dict, baseline_date, options_cache, eq_cache, watchlist_symbols
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')

        if baseline_date != current_date_str:
            morning_oi_dict = {}
            baseline_date = current_date_str

        if not options_cache:
            initialize_scrip_master(smart_api)
            if not options_cache:
                return

        selected_symbol = "RELIANCE"
        try:
            val = str(dashboard_sheet.cell(4, 10).value).strip().upper()
            if val and val not in ["NONE", "NAN", "0", ""]:
                selected_symbol = val
        except Exception:
            pass

        clean_symbol = selected_symbol.replace("-EQ", "").replace(".NS", "").strip()
        if clean_symbol in ["TATAMOTORS", "TATA", "TATA MOTORS LIMITED"]:
            clean_symbol = "TMCV"

        spot_token, spot_exchange = "", "NSE"
        if clean_symbol in eq_cache:
            spot_token, spot_exchange = eq_cache[clean_symbol]
        elif clean_symbol + "-EQ" in eq_cache:
            spot_token, spot_exchange = eq_cache[clean_symbol + "-EQ"]

        spot_price = 0.0
        if spot_token:
            try:
                spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                if spot_quote and spot_quote.get('status') and spot_quote.get('data') and spot_quote['data'].get('fetched'):
                    item = spot_quote['data']['fetched'][0]
                    spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            except Exception:
                pass

        sym_options = options_cache.get(clean_symbol, pd.DataFrame())
        if sym_options.empty:
            for key in options_cache:
                if clean_symbol in key:
                    sym_options = options_cache[key]
                    break

        if sym_options.empty:
            return

        today_date = pd.Timestamp(now_dt.date())
        valid_opts = sym_options[sym_options['parsed_expiry'] >= today_date].copy()
        if valid_opts.empty:
            return

        unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
        expiry_strings_sorted = [pd.Timestamp(dt).strftime('%d%b%Y').upper() for dt in unique_expiries_dt]

        active_expiries = expiry_strings_sorted[:4]
        while len(active_expiries) < 4:
            active_expiries.append(active_expiries[-1] if active_expiries else "N/A")

        current_expiry = active_expiries[0]
        current_expiry_dt = unique_expiries_dt[0]
        current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()

        lot_size = 1
        if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty:
            try:
                lot_size = int(float(current_expiry_options['lotsize'].iloc[0]))
            except:
                lot_size = 1

        if spot_price <= 0:
            all_strikes_list = sorted(current_expiry_options['strike'].unique())
            spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

        all_master_strikes = sorted(current_expiry_options['strike'].unique())
        if all_master_strikes:
            closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
            start_idx = max(0, closest_idx - 20)
            end_idx = min(len(all_master_strikes), closest_idx + 21)
            strikes = all_master_strikes[start_idx:end_idx]
            if len(all_master_strikes) < 41:
                strikes = all_master_strikes
        else:
            strikes = []

        filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
        
        exchange_tokens_map = {}
        for _, row in filtered_opts.iterrows():
            exch = str(row['exch_seg']).strip()
            tok = str(row['token']).strip()
            if exch not in exchange_tokens_map:
                exchange_tokens_map[exch] = []
            if tok not in exchange_tokens_map[exch]:
                exchange_tokens_map[exch].append(tok)

        all_fetched_quotes = []
        for exch, tokens_list in exchange_tokens_map.items():
            for i in range(0, len(tokens_list), 40):
                batch_tokens = tokens_list[i:i+40]
                if not batch_tokens: continue
                try:
                    res = smart_api.getMarketData("FULL", {exch: batch_tokens})
                    if res and res.get('status') and res.get('data'):
                        all_fetched_quotes.extend(res['data'].get('fetched', []))
                except Exception:
                    pass

        market_open_time = datetime.strptime("09:15:00", "%H:%M:%S").time()
        is_market_open_passed = now_dt.time() >= market_open_time

        quotes_dict = {}
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('symboltoken', item.get('token', '')))).strip()
            ltp = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            volume = float(item.get('tradeVolume', item.get('volume', 0)))
            oi = float(item.get('opnInterest', item.get('oi', 0)))
            net_change = float(item.get('netChange', item.get('change', 0)))
            
            if is_market_open_passed and tok not in morning_oi_dict:
                morning_oi_dict[tok] = oi

            base_oi = morning_oi_dict.get(tok, oi)
            quotes_dict[tok] = {
                'ltp': ltp, 'volume': volume, 'oi': oi, 'oi_chg': oi - base_oi, 'net_change': net_change
            }

        try:
            exp_dt = datetime.strptime(str(current_expiry).title(), "%d%b%Y")
            exp_date = exp_dt.replace(hour=15, minute=30, second=0)
        except:
            exp_date = datetime.now() + timedelta(days=5)

        t = max((exp_date - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)
        r = 0.07

        output_rows = [[
            "CE Vega", "CE Theta", "CE Gamma", "CE Delta", "CE IV", "CE OI Chg", "CE OI", "CE Vol", "CE Change", "CE LTP",
            "Strike Price",
            "PE LTP", "PE Change", "PE Vol", "PE OI", "PE OI Chg", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"
        ]]

        for st in strikes:
            ce_sub = current_expiry_options[
                (current_expiry_options['strike'] == st) & 
                (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('CE'))
            ]
            pe_sub = current_expiry_options[
                (current_expiry_options['strike'] == st) & 
                (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('PE'))
            ]

            ce_token = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
            pe_token = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""

            ce_q = quotes_dict.get(ce_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})
            pe_q = quotes_dict.get(pe_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, r, ce_q['ltp'])
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, r, pe_q['ltp'])

            ce_iv_val = ce_gks['iv'] if ce_gks['iv'] > 1.0 else 0
            pe_iv_val = pe_gks['iv'] if pe_gks['iv'] > 1.0 else 0

            output_rows.append([
                ce_gks['vega'], ce_gks['theta'], ce_gks['gamma'], ce_gks['delta'], ce_iv_val, ce_q['oi_chg'], ce_q['oi'], ce_q['volume'], ce_q['net_change'], ce_q['ltp'],
                st,
                pe_q['ltp'], pe_q['net_change'], pe_q['volume'], pe_q['oi'], pe_q['oi_chg'], pe_iv_val, pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        try:
            dashboard_sheet.update(range_name="K9", values=[[spot_price]])
            dashboard_sheet.update(range_name="L9", values=[[str(current_expiry)]])
            dashboard_sheet.update(range_name="A7", values=[[lot_size]])
            expiry_cells = [
                ['A3', active_expiries[0]], ['A4', active_expiries[1]], 
                ['A5', active_expiries[2]], ['A6', active_expiries[3]]
            ]
            dashboard_sheet.batch_update([{'range': cell, 'values': [[val]]} for cell, val in expiry_cells])
            
            if len(output_rows) > 1:
                last_row_num = 10 + len(output_rows) - 1
                dashboard_sheet.update(range_name=f"A10:U{last_row_num}", values=output_rows)
                dashboard_sheet.batch_clear([f"A{last_row_num + 1}:U100"])

            # Read A8:T20 or A8:T28 range values for exact matrix calculation
            range_values = dashboard_sheet.get("A8:T28")

            # Run Matrix Engine
            finalD5, finalH5, finalQ5, finalU5, k3Text = process_strict_independent_matrix(clean_symbol, range_values, now_dt)

            dashboard_sheet.update(range_name="D5", values=[[finalD5]])
            dashboard_sheet.update(range_name="H5", values=[[finalH5]])
            dashboard_sheet.update(range_name="Q5", values=[[finalQ5]])
            dashboard_sheet.update(range_name="U5", values=[[finalU5]])
            dashboard_sheet.update(range_name="K3", values=[[k3Text]])
            
            # K3 Color Formatting (Blue for Bullish, Red for Bearish, Black for Neutral)
            k3_lower = k3Text.lower()
            if "bullrun" in k3_lower or "bull run" in k3_lower or "bullish" in k3_lower:
                color_fmt = {"foregroundColor": {"red": 0.0, "green": 0.0, "blue": 1.0}}
            elif "bloodbath" in k3_lower or "bearish" in k3_lower:
                color_fmt = {"foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}
            else:
                color_fmt = {"foregroundColor": {"red": 0.0, "green": 0.0, "blue": 0.0}}

            dashboard_sheet.format("K3", {"textFormat": color_fmt, "textFormat": {"bold": True}})
            print(f"⚡ Refreshed & All 27 Scenarios Synchronized Successfully for {clean_symbol}!")
        except Exception as sheet_err:
            print(f"⚠ Google Sheet Batch Write Error: {sheet_err}")

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

# ==========================================
# MAIN EXECUTION THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Optimized Angel One Background State Engine...")
    try:
        smart_api = SmartConnect(api_key=API_KEY)
    except Exception as e:
        print(f"❌ SmartConnect Init Error: {e}")
        return
        
    while True:
        try:
            print("🔄 Attempting to generate Angel One session...")
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data and data.get('status'):
                print("✅ Angel One Session Generated Successfully!")
                initialize_scrip_master(smart_api)
                break
            else:
                print(f"⚠ Session Failed Response: {data}")
                time.sleep(5)
        except Exception as e:
            print(f"❌ Session Generation Exception: {e}")
            time.sleep(5)

    while True:
        try:
            fetch_and_update_option_chain(smart_api)
        except Exception as _e:
            print(f"❌ Loop Error: {_e}")
        time.sleep(1)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
