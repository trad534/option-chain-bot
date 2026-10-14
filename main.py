import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime, timedelta
from SmartApi import SmartConnect
from flask import Flask, jsonify, render_template_string
import threading
import builtins

# ====================================================================
# 🛡️ SYSTEM TERMINAL LOGS & KEEP-ALIVE SERVER SHIELD
# ====================================================================
RENDER_URL = "https://onrender.com"

def self_ping():
    while True:
        try:
            if RENDER_URL:
                response = requests.get(RENDER_URL)
                print(f"Self-ping sent: Status {response.status_code}")
        except Exception as e:
            print(f"Self-ping error: {e}")
        time.sleep(180)

ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()

def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

app = Flask(__name__)

# ====================================================================
# 🛠️ GLOBAL ENVIRONMENT PARAMETERS & CREDENTIALS
# ====================================================================
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

smart_api = None
full_scrip_df = pd.DataFrame()
eq_cache = {}
options_cache = {}
morning_oi_dict = {}
baseline_date = None
persistent_quotes_cache = {}

# In-Memory Vault for All 265+ Symbols (Investing Daddy Architecture)
multi_symbol_runtime_states = {}
global_run_date = ""

# ====================================================================
# 🧮 BLACK-SCHOLES OPTIONS GREEKS DERIVATIONS
# ====================================================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}

    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic: ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0: sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        return (S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2) if flag == 'CE' else K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)) - ltp

    try: sigma = brentq(objective_function, 1e-4, 5.0, xtol=1e-6, maxiter=100)
    except Exception: sigma = 0.20

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(t))
    vega_val = S * norm.pdf(d1) * np.sqrt(t) / 100

    if flag == 'CE':
        delta = norm.cdf(d1)
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) - r * K * np.exp(-r * t) * norm.cdf(d1 - sigma * np.sqrt(t))) / 365
    else:
        delta = norm.cdf(d1) - 1
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) + r * K * np.exp(-r * t) * norm.cdf(-(d1 - sigma * np.sqrt(t)))) / 365

    return {'iv': round(sigma * 100, 2), 'delta': round(delta, 3), 'gamma': round(gamma, 4), 'theta': round(theta, 2), 'vega': round(vega_val, 2)}

# ====================================================================
# 🎛️ CORE SCENARIO ENGINE FOR ANY SYMBOL IN RAM
# ====================================================================
def process_symbol_scenario(symbol_name, strikes_data, spot_price, lot_size):
    global global_run_date, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        current_time_clock = now_dt.strftime("%H:%M:%S")
        today_str = now_dt.strftime("%Y-%m-%d")

        if global_run_date != today_str:
            global_run_date = today_str

        if symbol_name not in multi_symbol_runtime_states:
            multi_symbol_runtime_states[symbol_name] = {
                "memory": {},
                "D5_TEXT": "(-----)", "H5_TEXT": "(-----)", "U5_TEXT": "(-----)", "Q5_TEXT": "(-----)",
                "K3_TEXT": f"{symbol_name} is Neutral",
                "grid_rows": []
            }

        sym_vault = multi_symbol_runtime_states[symbol_name]
        sym_memory = sym_vault["memory"]

        def get_mem(key, default=""):
            return sym_memory.get(key, default)

        def set_mem(key, val):
            sym_memory[key] = str(val)

        def remove_mem(key):
            if key in sym_memory: del sym_memory[key]

        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
        
        # strikes_data contains rows corresponding to the 21 strikes (10 up, spot, 10 down)
        for idx, row in enumerate(strikes_data):
            strike = row['strike']
            ce_oi = row['ce_oi']
            ce_vol = row['ce_vol']
            pe_vol = row['pe_vol']
            pe_oi = row['pe_oi']

            # Row index mapping (approximate simulation of rows 8 to 28)
            row_idx = 8 + idx
            if strike == spot_price: row_idx = 18 # Imaginary line row index

            ceOiArray.append({'strike': strike, 'val': ce_oi, 'row': row_idx})
            ceVolArray.append({'strike': strike, 'val': ce_vol, 'row': row_idx})
            peVolArray.append({'strike': strike, 'val': pe_vol, 'row': row_idx})
            peOiArray.append({'strike': strike, 'val': pe_oi, 'row': row_idx})

        def extract_top_two(data_array):
            if not data_array: return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered = [item for item in data_array if item['val'] > 0]
            if not filtered: return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered.sort(key=lambda x: x['val'], reverse=True)
            highest = filtered[0]
            second = filtered[1] if len(filtered) > 1 else {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
            pct = (second['val'] / highest['val']) * 100 if highest['val'] > 0 else 0
            return {'highest': highest, 'second': second, 'pct': pct}

        ceVolTop = extract_top_two(ceVolArray)
        ceOiTop = extract_top_two(ceOiArray)
        peVolTop = extract_top_two(peVolArray)
        peOiTop = extract_top_two(peOiArray)

        if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike'] or not peVolTop['highest']['strike'] or not peOiTop['highest']['strike']:
            return

        def detect_live_state_advanced(top_obj, is_put_side):
            if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0: return "STRONG"
            if is_put_side: return "wtb" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtt"
            else: return "wtt" if top_obj['second']['strike'] > top_obj['highest']['strike'] else "wtb"

        ceVolRawState = detect_live_state_advanced(ceVolTop, False)
        ceOiRawState = detect_live_state_advanced(ceOiTop, False)
        peVolRawState = detect_live_state_advanced(peVolTop, True)
        peOiRawState = detect_live_state_advanced(peOiTop, True)

        def get_full_status_string(top_obj, raw_state, cache_key):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike'): return "stable strong"
            strike = top_obj['highest']['strike']
            prev_strike_str = get_mem("PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike
            current_weak_hist = get_mem("WEAK_HIST_" + cache_key, "")

            if raw_state == "STRONG":
                if current_weak_hist == "wtt": return "wtt to strong"
                if current_weak_hist == "wtb": return "wtb to strong"
                if strike != prev_strike: return "strong"
                return "stable strong"
            return raw_state

        ceVolStatusText = get_full_status_string(ceVolTop, ceVolRawState, "CE_VOL")
        ceOiStatusText = get_full_status_string(ceOiTop, ceOiRawState, "CE_OI")
        peVolStatusText = get_full_status_string(peVolTop, peVolRawState, "PE_VOL")
        peOiStatusText = get_full_status_string(peOiTop, peOiRawState, "PE_OI")

        ceOiIsStable100 = ceOiRawState == "STRONG" or "to strong" in ceOiStatusText
        ceVolIsStable100 = ceVolRawState == "STRONG" or "to strong" in ceVolStatusText
        peOiIsStable100 = peOiRawState == "STRONG" or "to strong" in peOiStatusText
        peVolIsStable100 = peVolRawState == "STRONG" or "to strong" in peVolStatusText

        ceWinner = "VOL"
        if ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiRawState == "wtb" and ceVolRawState == "wtb": ceWinner = "BOTH"
        elif ceOiIsStable100 and ceVolIsStable100: ceWinner = "BOTH"
        elif ceOiIsStable100 and not ceVolIsStable100: ceWinner = "OI"
        elif ceVolIsStable100 and not ceOiIsStable100: ceWinner = "VOL"
        else:
            ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"

        peWinner = "VOL"
        if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt": peWinner = "BOTH"
        elif peOiIsStable100 and peVolIsStable100: peWinner = "BOTH"
        elif peOiIsStable100 and not peVolIsStable100: ceWinner = "OI"
        elif peVolIsStable100 and not ceOiIsStable100: ceWinner = "VOL"
        else:
            peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        if ceOiRawState in ["wtt", "wtb"]: set_mem("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_mem("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_mem("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_mem("WEAK_HIST_PE_VOL", peVolRawState)

        def calculate_independent_cell(top_obj, cache_key, side_label, is_winner, raw_state, final_status_text):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike'):
                return {'text': "(-----)", 'scenarioStatus': "stable strong"}

            strike = top_obj['highest']['strike']
            second_strike = top_obj['second']['strike'] if top_obj.get('second') else strike

            prev_strike_str = get_mem("PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike
            if not prev_strike_str: set_mem("PREV_STRK_" + cache_key, str(strike)); prev_strike = strike

            display_status = final_status_text; scenario_status = final_status_text; is_shifting = False

            if get_mem("IS_SHIFTED_" + cache_key) == "true":
                shift_from = float(get_mem("SHIFT_FROM_" + cache_key, 0))
                if strike == shift_from:
                    set_mem("IS_SHIFTED_" + cache_key, "false"); set_mem("PREV_STRK_" + cache_key, str(strike))

            if strike != prev_strike and prev_strike > 0 and final_status_text in ["wtb", "wtt"]:
                shift_type = "top to bottom" if strike < prev_strike else "bottom to top"
                set_mem("IS_SHIFTED_" + cache_key, "true"); set_mem("SHIFT_TYPE_" + cache_key, shift_type)
                set_mem("SHIFT_FROM_" + cache_key, str(prev_strike))

            if get_mem("IS_SHIFTED_" + cache_key) == "true":
                if raw_state == "STRONG" or final_status_text == "stable strong" or "to strong" in final_status_text:
                    set_mem("IS_SHIFTED_" + cache_key, "false"); set_mem("PREV_STRK_" + cache_key, str(strike)); is_shifting = False
                else:
                    is_shifting = True
                    scenario_status = "wtb" if get_mem("SHIFT_TYPE_" + cache_key) == "top to bottom" else "wtt"
            else:
                set_mem("PREV_STRK_" + cache_key, str(strike))

            if not is_winner and raw_state != "STRONG": display_status = "stable strong"; scenario_status = "stable strong"

            display_time = get_mem("TIME_" + cache_key)
            if not display_time:
                display_time = current_time_clock
                set_mem("TIME_" + cache_key, display_time)

            s_val = int(strike) if strike.is_integer() else strike
            sec_val = int(second_strike) if second_strike.is_integer() else second_strike

            if is_shifting:
                output_text = f"({display_time} - {side_label}-{s_val}-{display_status}-{sec_val})\n   (shifted {get_mem('SHIFT_TYPE_' + cache_key)})"
            else:
                if display_status == "stable strong": output_text = f"({display_time} - {side_label} - {s_val})\n    (stable strong)"
                elif "to strong" in display_status: output_text = f"({display_time} - {side_label} - {s_val})\n   ({display_status})"
                else: output_text = f"({display_time} - {side_label}-{s_val}-{display_status}-{sec_val})"
            return {'text': output_text, 'scenarioStatus': scenario_status}

        ceOiResult  = calculate_independent_cell(ceOiTop, "CE_OI", "Eor OI", ceWinner in ["OI", "BOTH"], ceOiRawState, ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner in ["VOL", "BOTH"], ceVolRawState, ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", peWinner in ["VOL", "BOTH"], peVolRawState, peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop, "PE_OI", "Eos OI", peWinner in ["OI", "BOTH"], peOiRawState, peOiStatusText)

        checkCe = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        checkPe = peOiResult['scenarioStatus'] if peWinner == "OI" else peVolResult['scenarioStatus']

        k3Text = f"{symbol_name} is Neutral"
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{symbol_name} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{symbol_name} is 9th scenario both side Risky"
        elif checkCe == "stable strong" and checkPe == "stable strong": k3Text = f"{symbol_name} is Neutral"
        elif checkCe == "wtb" and checkPe == "stable strong": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "stable strong" and checkPe == "wtb": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "wtt" and checkPe == "stable strong": k3Text = f"{symbol_name} is slightly bullish"
        elif checkCe == "stable strong" and checkPe == "wtt": k3Text = f"{symbol_name} is slightly bullish"
        elif checkCe == "wtt" and checkPe == "wtt": k3Text = f"{symbol_name} is Bullrun"
        elif checkCe == "wtb" and checkPe == "wtb": k3Text = f"{symbol_name} is bloodbath"

        sym_vault["D5_TEXT"] = ceOiResult['text'] if ceWinner in ["OI", "BOTH"] else sym_vault["D5_TEXT"]
        sym_vault["H5_TEXT"] = ceVolResult['text'] if ceWinner in ["VOL", "BOTH"] else sym_vault["H5_TEXT"]
        sym_vault["U5_TEXT"] = peOiResult['text'] if peWinner in ["OI", "BOTH"] else sym_vault["U5_TEXT"]
        sym_vault["Q5_TEXT"] = peVolResult['text'] if peWinner in ["VOL", "BOTH"] else sym_vault["Q5_TEXT"]
        sym_vault["K3_TEXT"] = k3Text

    except Exception as e:
        print(f"Error in RAM scenario engine for {symbol_name}: {e}")

# ====================================================================
# 🚀 BACKGROUND SCRIP MASTER & LIVE DATA POLL INITIALIZER
# ====================================================================
def initialize_scrip_master(api_client):
    global full_scrip_df, eq_cache, options_cache
    try:
        print("📥 Downloading Angel One Scrip Master JSON...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=25)
        if resp.status_code == 200:
            scrip_data = resp.json()
            full_scrip_df = pd.DataFrame(scrip_data)
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns: full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns: full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            if 'strike' in full_scrip_df.columns: full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0) / 100.0
            
            nse_eq = full_scrip_df[(full_scrip_df['exch_seg'] == 'NSE') & (~full_scrip_df['instrumenttype'].str.contains('OPT|FUT', na=False))]
            for _, row in nse_eq.iterrows():
                sym, name, tok = str(row['symbol']).strip().upper(), str(row['name']).strip().upper(), str(row['token']).strip()
                eq_cache[sym] = (tok, 'NSE'); eq_cache[name] = (tok, 'NSE')
                if sym.endswith("-EQ"): eq_cache[sym.replace("-EQ", "")] = (tok, 'NSE')
            
            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce').fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()): options_cache[name.strip()] = group
            print(f"✅ Scrip Master Loaded Successfully for {len(options_cache)} symbols!")
    except Exception as e: print(f"⚠ Scrip Master Error: {e}")

def background_market_scanner():
    global smart_api, options_cache, eq_cache, persistent_quotes_cache, morning_oi_dict, baseline_date
    while True:
        try:
            if not smart_api:
                time.sleep(5)
                continue

            now_dt = datetime.now()
            current_date_str = now_dt.strftime('%Y-%m-%d')
            if baseline_date != current_date_str:
                morning_oi_dict = {}
                baseline_date = current_date_str

            if not options_cache:
                time.sleep(2)
                continue

            # Loop through active cached symbols in background RAM
            for symbol_name in list(options_cache.keys())[:30]: # Polling top symbols for speed
                try:
                    sym_options = options_cache[symbol_name]
                    valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
                    if valid_opts.empty: continue

                    unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
                    current_expiry_dt = unique_expiries_dt[0]
                    current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()
                    lot_size = int(float(current_expiry_options['lotsize'].iloc[0])) if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty else 1

                    spot_token, spot_exchange = eq_cache.get(symbol_name, ("", "NSE"))
                    spot_price = 0.0
                    if spot_token:
                        spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                        if spot_quote and spot_quote.get('status') and spot_quote.get('data'):
                            spot_price = float(spot_quote['data']['fetched'][0].get('ltp', 0))

                    if spot_price <= 0:
                        all_strikes_list = sorted(current_expiry_options['strike'].unique())
                        spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                    all_master_strikes = sorted(current_expiry_options['strike'].unique())
                    closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
                    strikes = all_master_strikes[max(0, closest_idx - 10):min(len(all_master_strikes), closest_idx + 11)]

                    filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
                    
                    exch_map = {}
                    for _, row in filtered_opts.iterrows():
                        exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
                        if exch not in exch_map: exch_map[exch] = []
                        exch_map[exch].append(tok)

                    all_dash_quotes = []
                    for exch, t_list in exch_map.items():
                        for i in range(0, len(t_list), 40):
                            res = smart_api.getMarketData("FULL", {exch: t_list[i:i+40]})
                            if res and res.get('status') and res.get('data'): all_dash_quotes.extend(res['data']['fetched'])

                    dash_quotes_dict = {}
                    for item in all_dash_quotes:
                        tok = str(item.get('symbolToken', item.get('token', ''))).strip()
                        ltp = float(item.get('ltp', 0)); volume = float(item.get('tradeVolume', 0)); oi = float(item.get('opnInterest', 0))
                        if ltp > 0 or oi > 0: persistent_quotes_cache[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi}

                    for tok, q_data in persistent_quotes_cache.items():
                        if tok not in morning_oi_dict and q_data['oi'] > 0: morning_oi_dict[tok] = q_data['oi']
                        base_oi = morning_oi_dict.get(tok, q_data['oi'])
                        dash_quotes_dict[tok] = {'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 'oi_chg': q_data['oi'] - base_oi}

                    strikes_payload = []
                    for st in strikes:
                        ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('CE'))]
                        pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('PE'))]
                        ce_token = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
                        pe_token = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""
                        
                        ce_q = dash_quotes_dict.get(ce_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0})
                        pe_q = dash_quotes_dict.get(pe_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0})

                        strikes_payload.append({
                            'strike': st,
                            'ce_oi': ce_q['oi'],
                            'ce_vol': ce_q['volume'],
                            'pe_vol': pe_q['volume'],
                            'pe_oi': pe_q['oi']
                        })

                    process_symbol_scenario(symbol_name, strikes_payload, spot_price, lot_size)
                except Exception as inner_e:
                    print(f"Error scanning {symbol_name}: {inner_e}")
            time.sleep(1)
        except Exception as e:
            print(f"Scanner loop error: {e}")
            time.sleep(5)

# ====================================================================
# 🌐 FAST WEB DASHBOARD INTERFACE (INVESTING DADDY STYLE)
# ====================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>LTP Calculator - Live RAM Engine</title>
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #fff; margin: 0; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #1e1e1e; padding: 15px; border-radius: 8px; }
        select { padding: 10px; font-size: 16px; background: #333; color: #fff; border: 1px solid #555; border-radius: 5px; }
        .status-box { display: flex; gap: 15px; margin-top: 20px; }
        .card { background: #1e1e1e; flex: 1; padding: 15px; border-radius: 8px; border: 1px solid #333; text-align: center; }
        .k3-banner { background: #2c2c2c; padding: 15px; margin-top: 20px; text-align: center; font-size: 20px; font-weight: bold; border-radius: 8px; color: #00ffcc; }
    </style>
</head>
<body>
    <div class="header">
        <h2>⚡ LTP Calculator (RAM Memory Engine)</h2>
        <div>
            <label>Select Symbol: </label>
            <select id="symbolSelect" onchange="switchSymbol()">
                {% for sym in symbols %}
                <option value="{{ sym }}">{{ sym }}</option>
                {% endfor %}
            </select>
        </div>
    </div>

    <div class="k3-banner" id="k3Display">Loading Scenario Status...</div>

    <div class="status-box">
        <div class="card"><h3>CE OI (D5)</h3><p id="d5Val">-</p></div>
        <div class="card"><h3>CE Vol (H5)</h3><p id="h5Val">-</p></div>
        <div class="card"><h3>PE Vol (Q5)</h3><p id="q5Val">-</p></div>
        <div class="card"><h3>PE OI (U5)</h3><p id="u5Val">-</p></div>
    </div>

    <script>
        function switchSymbol() {
            let sym = document.getElementById("symbolSelect").value;
            fetch('/api/data/' + sym)
                .then(res => res.json())
                .then(data => {
                    document.getElementById("d5Val").innerText = data.D5;
                    document.getElementById("h5Val").innerText = data.H5;
                    document.getElementById("q5Val").innerText = data.Q5;
                    document.getElementById("u5Val").innerText = data.U5;
                    document.getElementById("k3Display").innerText = data.K3;
                });
        }

        setInterval(() => {
            let sym = document.getElementById("symbolSelect").value;
            fetch('/api/data/' + sym)
                .then(res => res.json())
                .then(data => {
                    document.getElementById("d5Val").innerText = data.D5;
                    document.getElementById("h5Val").innerText = data.H5;
                    document.getElementById("q5Val").innerText = data.Q5;
                    document.getElementById("u5Val").innerText = data.U5;
                    document.getElementById("k3Display").innerText = data.K3;
                });
        }, 1000);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    syms = list(options_cache.keys()) if options_cache else ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    return render_template_string(HTML_TEMPLATE, symbols=syms)

@app.route('/api/data/<symbol>')
def get_symbol_data(symbol):
    sym = symbol.upper()
    if sym in multi_symbol_runtime_states:
        vault = multi_symbol_runtime_states[sym]
        return jsonify({
            "D5": vault.get("D5_TEXT", "(-----)"),
            "H5": vault.get("H5_TEXT", "(-----)"),
            "Q5": vault.get("Q5_TEXT", "(-----)"),
            "U5": vault.get("U5_TEXT", "(-----)"),
            "K3": vault.get("K3_TEXT", f"{sym} is Neutral")
        })
    return jsonify({"D5": "(-----)", "H5": "(-----)", "Q5": "(-----)", "U5": "(-----)", "K3": f"{sym} Initializing..."})

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ====================================================================
# 🏁 MAIN EXECUTION START
# ====================================================================
if __name__ == "__main__":
    global smart_api
    try:
        smart_api = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
        session_res = smart_api.generateSession(CLIENT_ID, PIN, totp)
        if session_res.get('status'):
            print("✅ Angel One Session Established Successfully!")
            initialize_scrip_master(smart_api)
        else:
            print("❌ Session Error during startup.")
    except Exception as e:
        print(f"Init Exception: {e}")

    # Start Flask Web Dashboard Thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Start Background RAM Market Polling Engine
    background_market_scanner()
