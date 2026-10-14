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

RENDER_URL = "https://option-chain-bot.onrender.com"

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

CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

app_state = {
    "smart_api": None,
    "baseline_date": None,
    "morning_oi": {}
}

full_scrip_df = pd.DataFrame()
eq_cache = {}
options_cache = {}
persistent_quotes_cache = {}
multi_symbol_runtime_states = {}
global_run_date = ""
script_memory = {}

def get_memory(key, default=""):
    return script_memory.get(key, default)

def set_memory(key, value):
    script_memory[key] = str(value)

def remove_memory(key):
    if key in script_memory:
        del script_memory[key]

# ==========================================
# ROBUST IV & GREEKS
# ==========================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0.0, 'delta': round(delta, 3), 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}

    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic: ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0: sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2) if flag == 'CE' else K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
        return price - ltp

    try: sigma = brentq(objective_function, 1e-4, 5.0, xtol=1e-6, maxiter=100)
    except Exception: sigma = 0.20

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
        'iv': round(sigma * 100, 2), 'delta': round(delta, 3),
        'gamma': round(gamma, 4), 'theta': round(theta, 2), 'vega': round(vega_val, 2)
    }

# ==========================================
# MASTER SCENARIO ENGINE
# ==========================================
def process_symbol_scenario(symbol_name, strikes_data, spot_price, lot_size, time_to_expiry_years):
    global global_run_date, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        current_time_str = now_dt.strftime("%H:%M")
        today_str = now_dt.strftime("%Y-%m-%d")

        if global_run_date != today_str:
            script_memory.clear()
            global_run_date = today_str

        if symbol_name not in multi_symbol_runtime_states:
            multi_symbol_runtime_states[symbol_name] = {
                "D5_TEXT": "(-----)", "H5_TEXT": "(-----)", "U5_TEXT": "(-----)", "Q5_TEXT": "(-----)",
                "K3_TEXT": f"{symbol_name} is Neutral",
                "grid_rows": [],
                "spot_price": spot_price
            }

        sym_vault = multi_symbol_runtime_states[symbol_name]

        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
        raw_rows = []

        max_ce_oi = max([r['ce_oi'] for r in strikes_data], default=1) or 1
        max_ce_vol = max([r['ce_vol'] for r in strikes_data], default=1) or 1
        max_pe_vol = max([r['pe_vol'] for r in strikes_data], default=1) or 1
        max_pe_oi = max([r['pe_oi'] for r in strikes_data], default=1) or 1

        for idx, row in enumerate(strikes_data):
            strike = row['strike']
            row_idx = 8 + idx

            ce_oi = row['ce_oi']
            ce_vol = row['ce_vol']
            ce_ltp = row['ce_ltp']
            ce_oi_chg = row['ce_oi_chg']
            ce_change = row.get('ce_change', 0)
            
            pe_ltp = row['pe_ltp']
            pe_vol = row['pe_vol']
            pe_oi = row['pe_oi']
            pe_oi_chg = row['pe_oi_chg']
            pe_change = row.get('pe_change', 0)

            ce_greeks = calculate_iv_and_greeks('CE', spot_price, strike, time_to_expiry_years, 0.07, ce_ltp)
            pe_greeks = calculate_iv_and_greeks('PE', spot_price, strike, time_to_expiry_years, 0.07, pe_ltp)

            ce_oi_pct = round((ce_oi / max_ce_oi) * 100, 1) if max_ce_oi > 0 else 0.0
            ce_vol_pct = round((ce_vol / max_ce_vol) * 100, 1) if max_ce_vol > 0 else 0.0
            pe_vol_pct = round((pe_vol / max_pe_vol) * 100, 1) if max_pe_vol > 0 else 0.0
            pe_oi_pct = round((pe_oi / max_pe_oi) * 100, 1) if max_pe_oi > 0 else 0.0

            ceOiArray.append({'strike': strike, 'val': ce_oi, 'row': row_idx})
            ceVolArray.append({'strike': strike, 'val': ce_vol, 'row': row_idx})
            peVolArray.append({'strike': strike, 'val': pe_vol, 'row': row_idx})
            peOiArray.append({'strike': strike, 'val': pe_oi, 'row': row_idx})

            raw_rows.append({
                'strike': strike,
                'ce_vega': ce_greeks['vega'],
                'ce_theta': ce_greeks['theta'],
                'ce_gamma': ce_greeks['gamma'],
                'ce_delta': ce_greeks['delta'],
                'ce_iv': ce_greeks['iv'],
                'ce_oi_chg': ce_oi_chg,
                'ce_oi': ce_oi,
                'ce_oi_pct': ce_oi_pct,
                'ce_vol': ce_vol,
                'ce_vol_pct': ce_vol_pct,
                'ce_change': ce_change,
                'ce_ltp': ce_ltp,
                'pe_ltp': pe_ltp,
                'pe_change': pe_change,
                'pe_vol': pe_vol,
                'pe_vol_pct': pe_vol_pct,
                'pe_oi': pe_oi,
                'pe_oi_pct': pe_oi_pct,
                'pe_oi_chg': pe_oi_chg,
                'pe_iv': pe_greeks['iv'],
                'pe_delta': pe_greeks['delta'],
                'pe_gamma': pe_greeks['gamma'],
                'pe_theta': pe_greeks['theta'],
                'pe_vega': pe_greeks['vega'],
                'is_itm_ce': bool(strike < spot_price),
                'is_itm_pe': bool(strike > spot_price)
            })

        def extract_top_two(data_array):
            if not data_array or len(data_array) == 0:
                return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered = [item for item in data_array if item['val'] > 0]
            if len(filtered) == 0:
                return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered.sort(key=lambda x: x['val'], reverse=True)
            highest = filtered[0]
            second = filtered[1] if len(filtered) > 1 else {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
            pct = (second['val'] / highest['val']) * 100 if highest['val'] > 0 else 0
            return {'highest': highest, 'second': second, 'pct': pct}

        ceVolTop = extract_top_two(ceVolArray)
        ceOiTop = extract_top_two(ceOiArray)
        peVolTop = extract_top_two(peVolArray)
        peOiTop = extract_top_two(peOiArray)

        if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike'] or not peVolTop['highest']['strike'] or not peOiTop['highest']['strike']: return

        def detect_live_state_advanced(top_obj, is_put_side):
            if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0:
                return "stable strong", round(top_obj['pct'], 1)
            if is_put_side:
                st = "wtb" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtt"
                return st, round(top_obj['pct'], 1)
            else:
                st = "wtt" if top_obj['second']['strike'] > top_obj['highest']['strike'] else "wtb"
                return st, round(top_obj['pct'], 1)

        ceVolRawState, ceVolPctVal = detect_live_state_advanced(ceVolTop, False)
        ceOiRawState, ceOiPctVal = detect_live_state_advanced(ceOiTop, True)
        peVolRawState, peVolPctVal = detect_live_state_advanced(peVolTop, True)
        peOiRawState, peOiPctVal = detect_live_state_advanced(peOiTop, True)

        ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"
        peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        def get_full_status_string(top_obj, raw_state, cache_key):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike') or top_obj['highest']['strike'] == 0:
                return "stable strong"
            current_weak_hist = get_memory("WEAK_HIST_" + cache_key, "")
            if raw_state == "stable strong":
                if current_weak_hist == "wtt": return "wtt to strong"
                if current_weak_hist == "wtb": return "wtb to strong"
                return "stable strong"
            return raw_state

        ceVolStatusText = get_full_status_string(ceVolTop, ceVolRawState, "CE_VOL")
        ceOiStatusText = get_full_status_string(ceOiTop, ceOiRawState, "CE_OI")
        peVolStatusText = get_full_status_string(peVolTop, peVolRawState, "PE_VOL")
        peOiStatusText = get_full_status_string(peOiTop, peOiRawState, "PE_OI")

        if ceOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_VOL", peVolRawState)

        def calculate_independent_cell(top_obj, cache_key, side_label, final_status_text):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike') or top_obj['highest']['strike'] == 0:
                return {'text': "(-----)", 'status': "stable strong"}
            strike = top_obj['highest']['strike']
            s_val = int(strike) if strike.is_integer() else strike
            display_time = get_memory("TIME_" + cache_key)
            if not display_time:
                display_time = current_time_str
                set_memory("TIME_" + cache_key, display_time)
            output_text = f"({display_time} - {side_label} - {s_val})\n    ({final_status_text})"
            return {'text': output_text, 'status': final_status_text}

        ceOiResult  = calculate_independent_cell(ceOiTop, "CE_OI", "Eor OI", ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop, "PE_OI", "Eos OI", peOiStatusText)

        checkCe = ceOiResult['status'] if ceWinner == "OI" else ceVolResult['status']
        checkPe = peOiResult['status'] if ceWinner == "OI" else peVolResult['status']

        k3Text = f"{symbol_name} is Neutral"
        if "wtt" in checkCe and "wtb" in checkPe: k3Text = f"{symbol_name} is 8th scenario both side Risky"
        elif "wtb" in checkCe and "wtt" in checkPe: k3Text = f"{symbol_name} is 9th scenario both side Risky"
        elif "stable strong" in checkCe and "stable strong" in checkPe: k3Text = f"{symbol_name} is Neutral"
        elif "wtb" in checkCe: k3Text = f"{symbol_name} is slightly Bearish"
        elif "wtb" in checkPe: k3Text = f"{symbol_name} is slightly Bearish"
        elif "wtt" in checkCe: k3Text = f"{symbol_name} is slightly bullish"
        elif "wtt" in checkPe: k3Text = f"{symbol_name} is slightly bullish"

        highest_ce_oi_strike = ceOiTop['highest']['strike']
        second_ce_oi_strike = ceOiTop['second']['strike'] if ceOiTop['pct'] >= 75.0 else None
        highest_ce_vol_strike = ceVolTop['highest']['strike']
        second_ce_vol_strike = ceVolTop['second']['strike'] if ceVolTop['pct'] >= 75.0 else None
        highest_pe_vol_strike = peVolTop['highest']['strike']
        second_pe_vol_strike = peVolTop['second']['strike'] if peVolTop['pct'] >= 75.0 else None
        highest_pe_oi_strike = peOiTop['highest']['strike']
        second_pe_oi_strike = peOiTop['second']['strike'] if peOiTop['pct'] >= 75.0 else None

        grid_rows_built = []
        spot_inserted = False

        for r in raw_rows:
            st = r['strike']
            r['ce_oi_color'] = 'green' if st == highest_ce_oi_strike else ('yellow' if st == second_ce_oi_strike else '')
            r['ce_vol_color'] = 'green' if st == highest_ce_vol_strike else ('yellow' if st == second_ce_vol_strike else '')
            r['pe_vol_color'] = 'green' if st == highest_pe_vol_strike else ('yellow' if st == second_pe_vol_strike else '')
            r['pe_oi_color'] = 'green' if st == highest_pe_oi_strike else ('yellow' if st == second_pe_oi_strike else '')

            if not spot_inserted and spot_price > 0 and st >= spot_price:
                grid_rows_built.append({
                    'is_spot_row': True,
                    'spot_price': round(spot_price, 2),
                    'ce_oi_badge': f"{ceOiRawState.upper()} {ceOiPctVal}%",
                    'ce_vol_badge': f"{ceVolRawState.upper()} {ceVolPctVal}%",
                    'pe_vol_badge': f"{peVolRawState.upper()} {peVolPctVal}%",
                    'pe_oi_badge': f"{peOiRawState.upper()} {peOiPctVal}%"
                })
                spot_inserted = True

            r['is_spot_line'] = False
            grid_rows_built.append(r)

        if not spot_inserted and spot_price > 0:
            grid_rows_built.append({
                'is_spot_row': True,
                'spot_price': round(spot_price, 2),
                'ce_oi_badge': f"{ceOiRawState.upper()} {ceOiPctVal}%",
                'ce_vol_badge': f"{ceVolRawState.upper()} {ceVolPctVal}%",
                'pe_vol_badge': f"{peVolRawState.upper()} {peVolPctVal}%",
                'pe_oi_badge': f"{peOiRawState.upper()} {peOiPctVal}%"
            })

        sym_vault["D5_TEXT"] = ceOiResult['text'] if ceWinner == "OI" else "(-----)"
        sym_vault["H5_TEXT"] = ceVolResult['text'] if ceWinner == "VOL" else "(-----)"
        sym_vault["Q5_TEXT"] = peVolResult['text'] if peWinner == "VOL" else "(-----)"
        sym_vault["U5_TEXT"] = peOiResult['text'] if ceWinner == "OI" else "(-----)"
        sym_vault["K3_TEXT"] = k3Text
        sym_vault["grid_rows"] = grid_rows_built
        sym_vault["spot_price"] = spot_price

    except Exception as e:
        print(f"Error in scenario engine for {symbol_name}: {e}")

# ==========================================
# SCRIP MASTER & BACKGROUND SCANNER
# ==========================================
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
            
            nse_eq = full_scrip_df[(full_scrip_df['exch_seg'].isin(['NSE', 'BSE'])) & (~full_scrip_df['instrumenttype'].str.contains('OPT|FUT', na=False))]
            for _, row in nse_eq.iterrows():
                sym, name, tok, exch = str(row['symbol']).strip().upper(), str(row['name']).strip().upper(), str(row['token']).strip(), str(row['exch_seg']).strip()
                eq_cache[sym] = (tok, exch)
                eq_cache[name] = (tok, exch)
            
            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce').fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()): options_cache[name.strip()] = group
            print(f"✅ Scrip Master Loaded Successfully for {len(options_cache)} symbols!")
    except Exception as e: print(f"⚠ Scrip Master Error: {e}")

def background_market_scanner():
    while True:
        try:
            api = app_state["smart_api"]
            if not api or not options_cache:
                time.sleep(3)
                continue

            for symbol_name in list(options_cache.keys())[:30]:
                try:
                    sym_options = options_cache[symbol_name]
                    now_dt = datetime.now()
                    valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
                    if valid_opts.empty: continue

                    unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
                    current_expiry_dt = unique_expiries_dt[0]
                    current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()
                    
                    time_to_expiry_days = max((current_expiry_dt - pd.Timestamp(now_dt.date())).days, 1)
                    time_to_expiry_years = time_to_expiry_days / 365.0
                    lot_size = int(float(current_expiry_options['lotsize'].iloc[0])) if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty else 1

                    spot_token, spot_exchange = eq_cache.get(symbol_name, ("", ""))
                    if not spot_token:
                        match_df = full_scrip_df[(full_scrip_df['exch_seg'].isin(['NSE', 'BSE'])) & (full_scrip_df['symbol'].str.upper() == symbol_name)]
                        if match_df.empty:
                            match_df = full_scrip_df[(full_scrip_df['exch_seg'].isin(['NSE', 'BSE'])) & (full_scrip_df['name'].str.upper() == symbol_name)]
                        if not match_df.empty:
                            spot_token = str(match_df['token'].iloc[0]).strip()
                            spot_exchange = str(match_df['exch_seg'].iloc[0]).strip()

                    spot_price = 0.0
                    if spot_token and spot_exchange:
                        spot_quote = api.getMarketData("FULL", {spot_exchange: [spot_token]})
                        if spot_quote and spot_quote.get('status') and spot_quote.get('data'):
                            fetched_list = spot_quote['data'].get('fetched', [])
                            if fetched_list:
                                spot_price = float(fetched_list[0].get('ltp', 0))

                    if spot_price <= 0:
                        all_strikes_list = sorted(current_expiry_options['strike'].unique())
                        spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                    all_master_strikes = sorted(current_expiry_options['strike'].unique())
                    closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
                    strikes = all_master_strikes[max(0, closest_idx - 15):min(len(all_master_strikes), closest_idx + 16)]

                    filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
                    exch_map = {}
                    for _, row in filtered_opts.iterrows():
                        exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
                        if exch not in exch_map: exch_map[exch] = []
                        exch_map[exch].append(tok)

                    all_dash_quotes = []
                    for exch, t_list in exch_map.items():
                        for i in range(0, len(t_list), 40):
                            res = api.getMarketData("FULL", {exch: t_list[i:i+40]})
                            if res and res.get('status') and res.get('data'): all_dash_quotes.extend(res['data']['fetched'])

                    dash_quotes_dict = {}
                    for item in all_dash_quotes:
                        tok = str(item.get('symbolToken', item.get('token', ''))).strip()
                        ltp = float(item.get('ltp', 0)); volume = float(item.get('tradeVolume', 0)); oi = float(item.get('opnInterest', 0))
                        net_change = float(item.get('netChange', item.get('change', 0)))
                        if ltp > 0 or oi > 0: persistent_quotes_cache[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi, 'net_change': net_change}

                    for tok, q_data in persistent_quotes_cache.items():
                        if tok not in app_state["morning_oi"] and q_data['oi'] > 0: app_state["morning_oi"][tok] = q_data['oi']
                        base_oi = app_state["morning_oi"].get(tok, q_data['oi'])
                        dash_quotes_dict[tok] = {
                            'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 
                            'oi_chg': q_data['oi'] - base_oi, 'net_change': q_data['net_change']
                        }

                    strikes_payload = []
                    for st in strikes:
                        ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('CE'))]
                        pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('PE'))]
                        ce_token = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
                        pe_token = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""
                        
                        ce_q = dash_quotes_dict.get(ce_token, {'oi': 0, 'volume': 0, 'ltp': 0, 'oi_chg': 0, 'net_change': 0})
                        pe_q = dash_quotes_dict.get(pe_token, {'oi': 0, 'volume': 0, 'ltp': 0, 'oi_chg': 0, 'net_change': 0})

                        strikes_payload.append({
                            'strike': st,
                            'ce_oi': ce_q['oi'], 'ce_vol': ce_q['volume'], 'ce_ltp': ce_q['ltp'],
                            'ce_oi_chg': ce_q['oi_chg'], 'ce_change': ce_q['net_change'],
                            'pe_ltp': pe_q['ltp'], 'pe_change': pe_q['net_change'], 'pe_vol': pe_q['volume'],
                            'pe_oi': pe_q['oi'], 'pe_oi_chg': pe_q['oi_chg']
                        })

                    process_symbol_scenario(symbol_name, strikes_payload, spot_price, lot_size, time_to_expiry_years)
                except Exception as inner_e:
                    print(f"Error scanning {symbol_name}: {inner_e}")
            time.sleep(1)
        except Exception as e:
            print(f"Scanner loop error: {e}")
            time.sleep(5)

# ==========================================
# WEB DASHBOARD INTERFACE
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>LTP Calculator - Spot Price Fixed</title>
    <style>
        body { font-family: Arial, sans-serif; background: #ffffff; color: #000000; margin: 0; padding: 10px; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #f1f3f5; padding: 10px 15px; border-radius: 8px; border: 1px solid #ced4da; }
        select { padding: 8px; font-size: 16px; background: #ffffff; color: #000000; border: 1px solid #ced4da; border-radius: 5px; }
        
        .status-box { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 10px; }
        .card { 
            background: #ffffff; 
            padding: 10px; 
            border-radius: 8px; 
            border: 2px solid #dee2e6; 
            box-shadow: 0 2px 4px rgba(0,0,0,0.05); 
            text-align: center; 
            font-size: 12px; 
            font-weight: bold; 
            white-space: pre-line; 
            color: #212529; 
        }
        .card h3 { margin: 0 0 5px 0; font-size: 13px; color: #495057; }
        
        .k3-banner { background: #e6fcf5; padding: 12px; margin-top: 10px; text-align: center; font-size: 18px; font-weight: bold; border-radius: 8px; color: #0ca678; border: 1px solid #20c997; }
        
        .table-container { margin-top: 15px; overflow-x: auto; max-height: 700px; }
        table { width: 100%; border-collapse: collapse; background: #ffffff; font-size: 11px; text-align: center; }
        th, td { border: none !important; padding: 5px 6px; white-space: nowrap; }
        th { background: #f8f9fa; color: #212529; position: sticky; top: 0; z-index: 10; font-size: 11px; border-bottom: 1px solid #dee2e6 !important; }
        
        .strike-col { background: #ff9800 !important; color: #000 !important; font-weight: bold; font-size: 12px; }
        
        /* Spot Row with Middle Red Line and NO cell grid borders */
        .spot-row td { 
            background: #ffffff !important; 
            border: none !important; 
            position: relative;
            padding: 4px 6px !important; 
        }
        .spot-row td::after {
            content: "";
            position: absolute;
            left: 0;
            right: 0;
            top: 50%;
            height: 2px;
            background-color: #f03e3e;
            z-index: 2;
        }
        .spot-badge {
            background: #ffffff;
            color: #000000;
            border: 2px solid #333333;
            border-radius: 6px;
            padding: 3px 8px;
            font-weight: bold;
            font-size: 10px;
            display: inline-block;
            position: relative;
            z-index: 3;
        }
        .spot-price-cell {
            background: #d32f2f !important;
            color: #ffffff !important;
            font-weight: bold;
            font-size: 12px;
            text-align: center;
        }
        .spot-tag-small {
            background: #ffffff;
            color: #d32f2f;
            font-size: 8px;
            font-weight: bold;
            padding: 1px 4px;
            border-radius: 3px;
            display: block;
            margin-bottom: 2px;
        }

        .itm-ce { background: rgba(255, 0, 0, 0.05); }
        .itm-pe { background: rgba(0, 128, 0, 0.05); }

        .highlight-green { background: #2e7d32 !important; color: #ffffff !important; font-weight: bold; }
        .highlight-yellow { background: #fbc02d !important; color: #000000 !important; font-weight: bold; }
        
        .chg-pos { color: #2b8a3e; font-weight: bold; }
        .chg-neg { color: #c92a2a; font-weight: bold; }
        .pct-sub { font-size: 10px; color: #555; display: block; }
    </style>
</head>
<body>
    <div class="header">
        <h2>⚡ LTP Calculator</h2>
        <div>
            <label>Symbol: </label>
            <select id="symbolSelect" onchange="fetchData()">
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

    <div class="table-container">
        <table id="optionChainTable">
            <thead>
                <tr>
                    <th colspan="10" style="background: #e7f5ff; color: #1864ab;">CALLS (CE)</th>
                    <th class="strike-col">STRIKE</th>
                    <th colspan="10" style="background: #fff5f5; color: #c92a2a;">PUTS (PE)</th>
                </tr>
                <tr>
                    <th>Vega</th>
                    <th>Theta</th>
                    <th>Gamma</th>
                    <th>Delta</th>
                    <th>IV</th>
                    <th>OI Chg</th>
                    <th>OI</th>
                    <th>Vol</th>
                    <th>Change</th>
                    <th>LTP</th>
                    <th class="strike-col">Price</th>
                    <th>LTP</th>
                    <th>Change</th>
                    <th>Vol</th>
                    <th>OI</th>
                    <th>OI Chg</th>
                    <th>IV</th>
                    <th>Delta</th>
                    <th>Gamma</th>
                    <th>Theta</th>
                    <th>Vega</th>
                </tr>
            </thead>
            <tbody id="gridBody">
                <tr><td colspan="21">Loading option chain data...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        function fetchData() {
            let sym = document.getElementById("symbolSelect").value;
            fetch('/api/data/' + sym + '?t=' + new Date().getTime())
                .then(res => res.json())
                .then(data => {
                    document.getElementById("d5Val").innerText = data.D5;
                    document.getElementById("h5Val").innerText = data.H5;
                    document.getElementById("q5Val").innerText = data.Q5;
                    document.getElementById("u5Val").innerText = data.U5;
                    document.getElementById("k3Display").innerText = data.K3;

                    let tbody = document.getElementById("gridBody");
                    tbody.innerHTML = "";

                    if (data.grid_rows && data.grid_rows.length > 0) {
                        data.grid_rows.forEach(row => {
                            let tr = document.createElement("tr");

                            if (row.is_spot_row) {
                                tr.classList.add("spot-row");
                                let activeSpotPrice = row.spot_price || data.spot_price || '-';
                                tr.innerHTML = `
                                    <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                                    <td><span class="spot-badge">${row.ce_oi_badge}</span></td>
                                    <td><span class="spot-badge">${row.ce_vol_badge}</span></td>
                                    <td>-</td><td>-</td>
                                    <td class="strike-col spot-price-cell"><span class="spot-tag-small">SPOT</span>${activeSpotPrice}</td>
                                    <td>-</td><td>-</td>
                                    <td><span class="spot-badge">${row.pe_vol_badge}</span></td>
                                    <td><span class="spot-badge">${row.pe_oi_badge}</span></td>
                                    <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                                `;
                            } else {
                                let ceOiClass = row.ce_oi_color === 'green' ? 'highlight-green' : (row.ce_oi_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_ce ? 'itm-ce' : ''));
                                let ceVolClass = row.ce_vol_color === 'green' ? 'highlight-green' : (row.ce_vol_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_ce ? 'itm-ce' : ''));
                                let peVolClass = row.pe_vol_color === 'green' ? 'highlight-green' : (row.pe_vol_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_pe ? 'itm-pe' : ''));
                                let peOiClass = row.pe_oi_color === 'green' ? 'highlight-green' : (row.pe_oi_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_pe ? 'itm-pe' : ''));

                                let ceOiChgClass = row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg';
                                let peOiChgClass = row.pe_oi_chg >= 0 ? 'chg-pos' : 'chg-neg';
                                let ceChgClass = row.ce_change >= 0 ? 'chg-pos' : 'chg-neg';
                                let peChgClass = row.pe_change >= 0 ? 'chg-pos' : 'chg-neg';

                                tr.innerHTML = `
                                    <td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_vega}</td>
                                    <td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_theta}</td>
                                    <td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_gamma}</td>
                                    <td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_delta}</td>
                                    <td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_iv}</td>
                                    <td class="${ceOiChgClass} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_oi_chg}</td>
                                    <td class="${ceOiClass}">${row.ce_oi}<span class="pct-sub">${row.ce_oi_pct}%</span></td>
                                    <td class="${ceVolClass}">${row.ce_vol}<span class="pct-sub">${row.ce_vol_pct}%</span></td>
                                    <td class="${ceChgClass} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_change}</td>
                                    <td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_ltp}</td>
                                    <td class="strike-col">${row.strike}</td>
                                    <td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_ltp}</td>
                                    <td class="${peChgClass} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_change}</td>
                                    <td class="${peVolClass}">${row.pe_vol}<span class="pct-sub">${row.pe_vol_pct}%</span></td>
                                    <td class="${peOiClass}">${row.pe_oi}<span class="pct-sub">${row.pe_oi_pct}%</span></td>
                                    <td class="${peOiClass}">${row.pe_oi_chg}</td>
                                    <td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_iv}</td>
                                    <td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_delta}</td>
                                    <td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_gamma}</td>
                                    <td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_theta}</td>
                                    <td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_vega}</td>
                                `;
                            }
                            tbody.appendChild(tr);
                        });
                    }
                });
        }

        setInterval(fetchData, 1000);
        window.onload = fetchData;
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
            "K3": vault.get("K3_TEXT", f"{sym} is Neutral"),
            "grid_rows": vault.get("grid_rows", []),
            "spot_price": vault.get("spot_price", 0)
        })
    return jsonify({"D5": "(-----)", "H5": "(-----)", "Q5": "(-----)", "U5": "(-----)", "K3": f"{sym} Initializing...", "grid_rows": [], "spot_price": 0})

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    try:
        temp_api = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
        session_res = temp_api.generateSession(CLIENT_ID, PIN, totp)
        if session_res.get('status'):
            app_state["smart_api"] = temp_api
            print("✅ Angel One Session Established Successfully!")
            initialize_scrip_master(temp_api)
        else:
            print("❌ Session Error during startup.")
    except Exception as e:
        print(f"Init Exception: {e}")

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    background_market_scanner()
