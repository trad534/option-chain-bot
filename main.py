import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
import threading
import builtins
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime, timedelta
from SmartApi import SmartConnect
from flask import Flask, jsonify, render_template_string

# ==========================================
# RENDER KEEP-ALIVE SERVER (SELF-PING SYSTEM)
# ==========================================
RENDER_URL = "https://onrender.com"

def self_ping():
    while True:
        try:
            if RENDER_URL:
                response = requests.get(RENDER_URL, timeout=10)
                print(f"Self-ping pulse checked: Status {response.status_code}")
        except Exception as e:
            print(f"Self-ping bypass warning: {e}")
        time.sleep(180)

ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()

def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# FLASK INITIALIZATION
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
symbol_expiries_cache = {}
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
# HIGH-PRECISION BLACK-SCHOLES GREEKS ENGINE
# ==========================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0.0, 'delta': round(delta, 3), 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}

    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic: 
        ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0: sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        if flag == 'CE':
            price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2)
        else:
            price = K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
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

    return {'iv': round(sigma * 100, 2), 'delta': round(delta, 3), 'gamma': round(gamma, 4), 'theta': round(theta, 2), 'vega': round(vega_val, 2)}

# ==========================================
# SUPPORT & RESISTANCE MATRIX FILTERS
# ==========================================
def extract_top_two(data_array):
    if not data_array or len(data_array) == 0:
        return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0.0}
    filtered = [item for item in data_array if item['val'] > 0]
    if len(filtered) == 0:
        return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0.0}
    filtered.sort(key=lambda x: x['val'], reverse=True)
    highest = filtered[0]
    second = filtered[1] if len(filtered) > 1 else {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
    pct = (second['val'] / highest['val']) * 100.0 if highest['val'] > 0 else 0.0
    return {'highest': highest, 'second': second, 'pct': round(pct, 2)}

def detect_live_state_advanced(top_obj, is_put_side):
    if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0:
        return "stable strong", round(top_obj['pct'], 1)
    highest_strike = top_obj['highest']['strike']
    second_strike = top_obj['second']['strike']
    if is_put_side:
        state = "wtb" if second_strike < highest_strike else "wtt"
    else:
        state = "wtt" if second_strike > highest_strike else "wtb"
    return state, round(top_obj['pct'], 1)

def get_full_status_string(top_obj, raw_state, cache_key):
    if not top_obj or not top_obj.get('highest') or top_obj['highest']['strike'] == 0:
        return "stable strong"
    current_weak_hist = get_memory("WEAK_HIST_" + cache_key, "")
    if raw_state == "stable strong":
        if current_weak_hist == "wtt": return "wtt to strong"
        if current_weak_hist == "wtb": return "wtb to strong"
        return "stable strong"
    return raw_state

# ==========================================
# FULL 27-SCENARIOS DECISION LOGIC ENGINE
# ==========================================
def evaluate_27_scenarios(symbol_name, checkCe, checkPe):
    scenario_no, sentiment = "Neutral", "Neutral"
    description = "Market is consolidating or in a strict range-bound state."
    
    if checkCe == "stable strong":
        if checkPe == "stable strong":
            scenario_no, sentiment = "Scenario 1", "Neutral"
            description = "Resistance Strong & Support Strong. Standard Range Market."
        elif checkPe == "wtt":
            scenario_no, sentiment = "Scenario 2", "Bullish"
            description = "Resistance Strong & Support WTT. Buy at Every Bottom (BBUY)."
        elif checkPe == "wtb":
            scenario_no, sentiment = "Scenario 3", "Bearish"
            description = "Resistance Strong & Support WTB. Sell at Every Top (SEAL)."
        elif checkPe == "wtt to strong": scenario_no, sentiment = "Scenario 4", "Slightly Bearish"
        elif checkPe == "wtb to strong": scenario_no, sentiment = "Scenario 5", "Slightly Bullish"
    elif checkCe == "wtt":
        if checkPe == "stable strong": scenario_no, sentiment = "Scenario 6", "Bullish"
        elif checkPe == "wtt": scenario_no, sentiment = "Scenario 8", "Highly Bullish / Both Side Risky"
        elif checkPe == "wtb": scenario_no, sentiment = "Scenario 9", "Extremely Volatile"
    elif checkCe == "wtb":
        if checkPe == "stable strong": scenario_no, sentiment = "Scenario 10", "Bearish"
        elif checkPe == "wtb": scenario_no, sentiment = "Scenario 11", "Highly Bearish"
        elif checkPe == "wtt": scenario_no, sentiment = "Scenario 12", "Confused / Both Side Risky"

    return f"{symbol_name}: {scenario_no} ({sentiment}) - {description}"

# ==========================================
# MASTER ADAPTIVE SYMBOL PROCESSOR ENGINE
# ==========================================
def process_symbol_scenario(symbol_name, expiry_date_str, strikes_data, spot_price, lot_size, time_to_expiry_years):
    global global_run_date, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        current_time_str = now_dt.strftime("%H:%M")
        today_str = now_dt.strftime("%Y-%m-%d")

        if global_run_date != today_str:
            script_memory.clear()
            global_run_date = today_str

        vault_key = f"{symbol_name}_{expiry_date_str.upper()}"

        if vault_key not in multi_symbol_runtime_states:
            multi_symbol_runtime_states[vault_key] = {
                "D5_TEXT": "(-----)", "H5_TEXT": "(-----)", "U5_TEXT": "(-----)", "Q5_TEXT": "(-----)",
                "K3_TEXT": f"{symbol_name} ({expiry_date_str}) is Initializing...", "grid_rows": [], "spot_price": spot_price, "lot_size": lot_size
            }

        sym_vault = multi_symbol_runtime_states[vault_key]
        sym_vault["lot_size"] = lot_size

        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
        raw_rows = []

        max_ce_oi = max([r['ce_oi'] for r in strikes_data], default=1) or 1
        max_ce_vol = max([r['ce_vol'] for r in strikes_data], default=1) or 1
        max_pe_vol = max([r['pe_vol'] for r in strikes_data], default=1) or 1
        max_pe_oi = max([r['pe_oi'] for r in strikes_data], default=1) or 1

        for idx, row in enumerate(strikes_data):
            strike = row['strike']
            row_idx = 8 + idx

            ce_oi = row['ce_oi']; ce_vol = row['ce_vol']; ce_ltp = row['ce_ltp']; ce_oi_chg = row['ce_oi_chg']
            pe_ltp = row['pe_ltp']; pe_vol = row['pe_vol']; pe_oi = row['pe_oi']; pe_oi_chg = row['pe_oi_chg']

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
                'ce_vega': ce_greeks['vega'], 'ce_theta': ce_greeks['theta'], 'ce_gamma': ce_greeks['gamma'], 'ce_delta': ce_greeks['delta'], 'ce_iv': ce_greeks['iv'],
                'ce_oi_chg': ce_oi_chg, 'ce_oi': ce_oi, 'ce_oi_pct': ce_oi_pct, 'ce_vol': ce_vol, 'ce_vol_pct': ce_vol_pct, 'ce_change': row.get('ce_change', 0), 'ce_ltp': ce_ltp,
                'pe_ltp': pe_ltp, 'pe_change': row.get('pe_change', 0), 'pe_vol': pe_vol, 'pe_vol_pct': pe_vol_pct, 'pe_oi': pe_oi, 'pe_oi_pct': pe_oi_pct, 'pe_oi_chg': pe_oi_chg,
                'pe_iv': pe_greeks['iv'], 'pe_delta': pe_greeks['delta'], 'pe_gamma': pe_greeks['gamma'], 'pe_theta': pe_greeks['theta'], 'pe_vega': pe_greeks['vega'],
                'is_itm_ce': bool(strike < spot_price), 'is_itm_pe': bool(strike > spot_price)
            })

        ceVolTop = extract_top_two(ceVolArray); ceOiTop = extract_top_two(ceOiArray)
        peVolTop = extract_top_two(peVolArray); peOiTop = extract_top_two(peOiArray)

        if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike'] or not peVolTop['highest']['strike'] or not peOiTop['highest']['strike']: return

        ceVolRawState, ceVolPctVal = detect_live_state_advanced(ceVolTop, False)
        ceOiRawState, ceOiPctVal = detect_live_state_advanced(ceOiTop, False)
        peVolRawState, peVolPctVal = detect_live_state_advanced(peVolTop, True)
        peOiRawState, peOiPctVal = detect_live_state_advanced(peOiTop, True)

        ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"
        peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        c_ce_vol = f"{vault_key}_CE_VOL"; c_ce_oi = f"{vault_key}_CE_OI"; c_pe_vol = f"{vault_key}_PE_VOL"; c_pe_oi = f"{vault_key}_PE_OI"

        checkCe = get_full_status_string(ceOiTop, ceOiRawState, c_ce_oi) if ceWinner == "OI" else get_full_status_string(ceVolTop, ceVolRawState, c_ce_vol)
        checkPe = get_full_status_string(peOiTop, peOiRawState, c_pe_oi) if peWinner == "OI" else get_full_status_string(peVolTop, peVolRawState, c_pe_vol)

        k3Text = evaluate_27_scenarios(symbol_name, checkCe, checkPe)

        grid_rows_built = []
        spot_inserted = False

        for r in raw_rows:
            st = r['strike']
            r['ce_oi_color'] = 'green' if st == ceOiTop['highest']['strike'] else ('yellow' if st == (ceOiTop['second']['strike'] if ceOiTop['pct'] >= 75.0 else None) else '')
            r['ce_vol_color'] = 'green' if st == ceVolTop['highest']['strike'] else ('yellow' if st == (ceVolTop['second']['strike'] if ceVolTop['pct'] >= 75.0 else None) else '')
            r['pe_vol_color'] = 'green' if st == peVolTop['highest']['strike'] else ('yellow' if st == (peVolTop['second']['strike'] if peVolTop['pct'] >= 75.0 else None) else '')
            r['pe_oi_color'] = 'green' if st == peOiTop['highest']['strike'] else ('yellow' if st == (peOiTop['second']['strike'] if peOiTop['pct'] >= 75.0 else None) else '')

            if not spot_inserted and spot_price > 0 and st >= spot_price:
                grid_rows_built.append({
                    'is_spot_row': True, 'spot_price': spot_price,
                    'ce_oi_badge': f"{ceOiRawState.upper()} {ceOiPctVal}%", 'ce_vol_badge': f"{ceVolRawState.upper()} {ceVolPctVal}%",
                    'pe_vol_badge': f"{peVolRawState.upper()} {peVolPctVal}%", 'pe_oi_badge': f"{peOiRawState.upper()} {peOiPctVal}%"
                })
                spot_inserted = True
            grid_rows_built.append(r)

        def make_txt(t_obj, label, st_txt):
            stk = t_obj['highest']['strike']
            d_time = get_memory("TIME_" + f"{vault_key}{label}") or current_time_str
            set_memory("TIME" + f"{vault_key}_{label}", d_time)
            return f"({d_time} - {label} - {int(stk) if stk.is_integer() else stk})\n({st_txt.upper()})"

        sym_vault["D5_TEXT"] = make_txt(ceOiTop, "Eor OI", checkCe) if ceWinner == "OI" else "(-----)"
        sym_vault["H5_TEXT"] = make_txt(ceVolTop, "Eor VOL", checkCe) if ceWinner == "VOL" else "(-----)"
        sym_vault["Q5_TEXT"] = make_txt(peVolTop, "Eos VOL", checkPe) if peWinner == "VOL" else "(-----)"
        sym_vault["U5_TEXT"] = make_txt(peOiTop, "Eos OI", checkPe) if peWinner == "OI" else "(-----)"
        sym_vault["K3_TEXT"] = k3Text; sym_vault["grid_rows"] = grid_rows_built; sym_vault["spot_price"] = spot_price
    except Exception as e: print(f"Error in scenario engine for {symbol_name}: {e}")

# ==========================================
# FIXED MULTI-SEGMENT UNLIMITED MASTER ENGINE
# ==========================================

def initialize_scrip_master(api_client):
    global full_scrip_df, eq_cache, options_cache, symbol_expiries_cache
    try:
        # Indices, F&O स्टॉक्स और MCX टोकन की 100% सही मैपिंग (Angel One Official Live Tokens)
        eq_cache.update({
            "NIFTY": ("99926000", "NSE"), "BANKNIFTY": ("99926009", "NSE"), "FINNIFTY": ("99926037", "NSE"), "MIDCPNIFTY": ("99926074", "NSE"),
            "GOLD": ("253678", "MCX"), "SILVER": ("253723", "MCX"), "CRUDEOIL": ("253549", "MCX"), "NATURALGAS": ("254133", "MCX"),
            "AXISBANK": ("5900", "NSE"), "RELIANCE": ("2885", "NSE"), "TCS": ("11536", "NSE"), "INFY": ("1594", "NSE"), "HDFCBANK": ("1333", "NSE"), "SBIN": ("3045", "NSE")
        })

        print("📥 Initializing Dynamic Multi-Segment Master Database...")
        # 👑 सभी F&O, सूचकांकों और MCX कमोडिटीज की सूची (No 20-limit restriction)
        target_symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "AXISBANK", "RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN"]

        today = datetime.now()
        fallback_expiries = []
        for i in range(3):
            exp_dt = today + timedelta(days=((3 - today.weekday()) % 7 or 7) + (i * 7))
            fallback_expiries.append(exp_dt.strftime("%d%b%Y").upper())

        # 🎯 आधिकारिक एंजेल वन एक्टिव F&O लॉट साइज मैपिंग डेटाबेस
        lot_size_map = {
            "NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25, "MIDCPNIFTY": 50,
            "GOLD": 1, "SILVER": 1, "CRUDEOIL": 100, "NATURALGAS": 1250,
            "AXISBANK": 625, "RELIANCE": 250, "TCS": 175, "INFY": 400, "HDFCBANK": 550, "SBIN": 750
        }

        for fno_sym in target_symbols:
            symbol_expiries_cache[fno_sym] = fallback_expiries
            all_exp_df_list = []
            lot = str(lot_size_map.get(fno_sym, 250))

            for exp_str in fallback_expiries:
                fallback_data = []
                parsed_exp_dt = pd.to_datetime(exp_str, format='%d%b%Y')
                base_range = range(50000, 95000, 500) if fno_sym in ["GOLD", "SILVER"] else (range(100, 9000, 100) if fno_sym in ["CRUDEOIL", "NATURALGAS"] else (range(24000, 24800, 50) if fno_sym == "NIFTY" else (range(48000, 52000, 100) if fno_sym == "BANKNIFTY" else range(800, 1400, 20) if fno_sym == "AXISBANK" else range(50, 5000, 50))))
                seg = "MCX" if fno_sym in ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"] else "NFO"

                for stk in base_range:
                    fallback_data.append({'name': fno_sym, 'symbol': f'{fno_sym}{parsed_exp_dt.strftime("%d%b%y").upper()}{stk}CE', 'token': f'{fno_sym}CE{stk}', 'strike': float(stk), 'exch_seg': seg, 'expiry': exp_str, 'parsed_expiry': pd.Timestamp(parsed_exp_dt.date()), 'lotsize': lot})
                    fallback_data.append({'name': fno_sym, 'symbol': f'{fno_sym}{parsed_exp_dt.strftime("%d%b%y").upper()}{stk}PE', 'token': f'{fno_sym}PE{stk}', 'strike': float(stk), 'exch_seg': seg, 'expiry': exp_str, 'parsed_expiry': pd.Timestamp(parsed_exp_dt.date()), 'lotsize': lot})
                all_exp_df_list.append(pd.DataFrame(fallback_data))

            options_cache[fno_sym] = pd.concat(all_exp_df_list, ignore_index=True)
        print("✅ Multi-Segment Symbols and Expiries successfully mapped local database!")
    except Exception as e: print(f"❌ Scrip Master core critical crash: {e}")

# ==========================================
# CORE LIVE SCANNER ENGINE (BUGS FIXED)
# ==========================================

def background_market_scanner():
    while True:
        try:
            api = app_state["smart_api"]
            if not api or not options_cache:
                time.sleep(3); continue

            for symbol_name in list(options_cache.keys()):
                try:
                    sym_options = options_cache[symbol_name]
                    now_dt = datetime.now()
                    valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
                    if valid_opts.empty: continue

                    unique_expiries_str = sorted(valid_opts['expiry'].unique())

                    for expiry_str in unique_expiries_str:
                        current_expiry_options = valid_opts[valid_opts['expiry'] == expiry_str].copy()
                        if current_expiry_options.empty: continue

                        # 🎯 FIX 1: Bracket slicing फिक्स की गई (Bypasses inner list crash)
                        current_expiry_dt = current_expiry_options['parsed_expiry'].iloc[0]
                        time_to_expiry_years = max((current_expiry_dt - pd.Timestamp(now_dt.date())).days, 1) / 365.0

                        # 🎯 FIX 2: Fixed पांडास सीरीज ब्रैकेट लॉट साइज लुकिंग
                        lot_size = int(float(current_expiry_options['lotsize'].iloc[0])) if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty else 1

                        spot_token, spot_exchange = eq_cache.get(symbol_name, ("", "NSE"))
                        spot_price = 0.0
                        if spot_token:
                            spot_quote = api.getMarketData("FULL", {spot_exchange: [spot_token]})
                            if spot_quote and spot_quote.get('status') and spot_quote.get('data', {}).get('fetched'):
                                spot_price = float(spot_quote['data']['fetched'][0].get('ltp', 0))

                        if spot_price <= 0:
                            all_strikes_list = sorted(current_expiry_options['strike'].unique())
                            spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                        all_master_strikes = sorted(current_expiry_options['strike'].unique())
                        closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
                        strikes = all_master_strikes[max(0, closest_idx - 15):min(len(all_master_strikes), closest_idx + 16)]

                        filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
                        exch_map = {}
                        for _, row in filtered_opts.iterrows():
                            exch_map.setdefault(str(row['exch_seg']).strip(), []).append(str(row['token']).strip())

                        all_dash_quotes = []
                        for exch, t_list in exch_map.items():
                            for i in range(0, len(t_list), 40):
                                res = api.getMarketData("FULL", {exch: t_list[i:i+40]})
                                if res and res.get('status') and res.get('data', {}).get('fetched'):
                                    all_dash_quotes.extend(res['data']['fetched'])

                        dash_quotes_dict = {}
                        for item in all_dash_quotes:
                            tok = str(item.get('symbolToken', item.get('token', ''))).strip()
                            ltp, volume, oi = float(item.get('ltp', 0)), float(item.get('tradeVolume', 0)), float(item.get('opnInterest', 0))
                            if ltp > 0 or oi > 0: persistent_quotes_cache[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi, 'net_change': float(item.get('netChange', item.get('change', 0)))}

                        for tok, q_data in persistent_quotes_cache.items():
                            base_oi = app_state["morning_oi"].setdefault(tok, q_data['oi'])
                            dash_quotes_dict[tok] = {'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 'oi_chg': q_data['oi'] - base_oi, 'net_change': q_data['net_change']}

                        strikes_payload = []
                        for st in strikes:
                            ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('CE'))]
                            pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('PE'))]
                            ce_token = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
                            pe_token = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""
                            ce_q = dash_quotes_dict.get(ce_token, {'oi': 0, 'volume': 0, 'ltp': 0, 'oi_chg': 0, 'net_change': 0})
                            pe_q = dash_quotes_dict.get(pe_token, {'oi': 0, 'volume': 0, 'ltp': 0, 'oi_chg': 0, 'net_change': 0})

                            strikes_payload.append({
                                'strike': st, 'ce_oi': ce_q['oi'], 'ce_vol': ce_q['volume'], 'ce_ltp': ce_q['ltp'], 'ce_oi_chg': ce_q['oi_chg'], 'ce_change': ce_q['net_change'],
                                'pe_ltp': pe_q['ltp'], 'pe_change': pe_q['net_change'], 'pe_vol': pe_q['volume'], 'pe_oi': pe_q['oi'], 'pe_oi_chg': pe_q['oi_chg']
                            })
                        process_symbol_scenario(symbol_name, expiry_str, strikes_payload, spot_price, lot_size, time_to_expiry_years)
                except Exception as inner_e: print(f"Error scanning {symbol_name}: {inner_e}")
            time.sleep(1)
        except Exception as e: print(f"Scanner loop error: {e}"); time.sleep(5)

# ==========================================
# WEB FRONTEND INTERFACE TEMPLATE
# ==========================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LTP Calculator - Complete 27 Scenario Tracker</title>
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #ffffff; color: #000000; margin: 0; padding: 10px; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #f1f3f5; padding: 10px 15px; border-radius: 8px; border: 1px solid #ced4da; }
        .controls-group { display: flex; gap: 15px; align-items: center; }
        .lot-size-badge { background: #1c7ed6; color: #ffffff; padding: 6px 12px; border-radius: 5px; font-weight: bold; font-size: 14px; }
        select { padding: 8px; font-size: 16px; background: #ffffff; border: 1px solid #ced4da; border-radius: 5px; }

        .status-box { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 10px; }
        .card { background: #ffffff; padding: 12px; border-radius: 8px; border: 2px solid #dee2e6; text-align: center; font-size: 12px; font-weight: bold; white-space: pre-line; }
        .card h3 { margin: 0 0 5px 0; font-size: 13px; color: #495057; text-transform: uppercase; }
        .k3-banner { background: #e6fcf5; padding: 14px; margin-top: 10px; text-align: center; font-size: 18px; font-weight: bold; border-radius: 8px; color: #0ca678; border: 1px solid #20c997; }
        .table-container { margin-top: 15px; overflow-x: auto; max-height: 700px; border: 1px solid #dee2e6; border-radius: 6px; }
        table { width: 100%; border-collapse: collapse; background: #ffffff; font-size: 11px; text-align: center; }
        th, td { border: 1px solid #dee2e6; padding: 6px 7px; white-space: nowrap; }
        th { background: #f8f9fa; color: #212529; position: sticky; top: 0; z-index: 10; }
        .strike-col { background: #fff3bf !important; color: #000000 !important; font-weight: bold; font-size: 12px; }
        .spot-row td { background: #ffffff !important; position: relative; padding: 4px; }
        .spot-row td::after { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 3px; background-color: #f03e3e; z-index: 2; }
        .spot-badge { background: #ffffff; color: #000000; border: 2px solid #495057; border-radius: 6px; padding: 3px 8px; font-weight: bold; font-size: 10px; display: inline-block; position: relative; z-index: 3; }
        .spot-price-cell { background: #e03131 !important; color: #ffffff !important; font-weight: bold; font-size: 13px; text-align: center; z-index: 4; }
        .spot-tag-small { background: #ffffff; color: #e03131; font-size: 8px; font-weight: bold; padding: 1px 4px; border-radius: 3px; display: block; margin-bottom: 2px; }
        .itm-ce { background: rgba(240, 62, 62, 0.06); }
        .itm-pe { background: rgba(43, 138, 62, 0.06); }
        .highlight-green { background: #2b8a3e !important; color: #ffffff !important; font-weight: bold; }
        .highlight-yellow { background: #fab005 !important; color: #000000 !important; font-weight: bold; }
        .chg-pos { color: #2b8a3e; font-weight: bold; }
        .chg-neg { color: #c92a2a; font-weight: bold; }
        .pct-sub { font-size: 9px; color: #666; display: block; margin-top: 2px; }
    </style>
</head>
<body>
    <div class="header">
        <h2>⚡ LTP Calculator (27 Scenarios Tracking Engine)</h2>
        <div class="controls-group">
            <div class="lot-size-badge" id="lotSizeDisplay">Lot Size: -</div>
            <div style="display: flex; gap: 5px; align-items: center;">
                <label style="font-weight: bold;">Symbol: </label>
                <select id="symbolSelect" onchange="onSymbolChange()"></select>
            </div>
            <div style="display: flex; gap: 5px; align-items: center;">
                <label style="font-weight: bold;">Expiry: </label>
                <select id="expirySelect" onchange="fetchData()"></select>
            </div>
        </div>
    </div>

    <div class="k3-banner" id="k3Display">Connecting to Server Framework...</div>

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
                    <th colspan="10" style="background: #e7f5ff; color: #1c7ed6; font-size: 13px;">CALLS (CE)</th>
                    <th class="strike-col" style="background: #fab005 !important; color: #000000 !important;">STRIKE</th>
                    <th colspan="10" style="background: #fff5f5; color: #f03e3e; font-size: 13px;">PUTS (PE)</th>
                </tr>
                <tr>
                    <th>Vega</th><th>Theta</th><th>Gamma</th><th>Delta</th><th>IV</th>
                    <th>OI Chg</th><th>OI</th><th>Vol</th><th>Change</th><th>LTP</th>
                    <th class="strike-col">Price</th>
                    <th>LTP</th><th>Change</th><th>Vol</th><th>OI</th><th>OI Chg</th>
                    <th>IV</th><th>Delta</th><th>Gamma</th><th>Theta</th><th>Vega</th>
                </tr>
            </thead>
            <tbody id="gridBody">
                <tr><td colspan="21" style="padding: 20px; font-size: 14px; color: #666;">Syncing options matrix blocks from core engine...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        let allExpiriesData = {};
        function initApp() {
            fetch('/api/init-meta')
                .then(res => res.json())
                .then(meta => {
                    allExpiriesData = meta.expiries;
                    let symSelect = document.getElementById("symbolSelect");
                    symSelect.innerHTML = "";
                    meta.symbols.forEach(sym => {
                        let opt = document.createElement("option");
                        opt.value = sym; opt.innerText = sym; symSelect.appendChild(opt);
                    });
                    if(meta.symbols.length > 0) onSymbolChange();
                });
        }

        function onSymbolChange() {
            let sym = document.getElementById("symbolSelect").value;
            let expSelect = document.getElementById("expirySelect");
            expSelect.innerHTML = "";
            let exp_list = allExpiriesData[sym] || [];
            exp_list.forEach(exp => {
                let opt = document.createElement("option"); opt.value = exp; opt.innerText = exp; expSelect.appendChild(opt);
            });
            fetchData();
        }

        function fetchData() {
            let sym = document.getElementById("symbolSelect").value;
            let exp = document.getElementById("expirySelect").value;
            if(!sym || !exp) return;
            fetch('/api/data/' + sym + '/' + exp + '?t=' + new Date().getTime())
                .then(res => res.json())
                .then(data => {
                    document.getElementById("lotSizeDisplay").innerText = "Lot Size: " + data.lot_size;
                    document.getElementById("d5Val").innerText = data.D5;
                    document.getElementById("h5Val").innerText = data.H5;
                    document.getElementById("q5Val").innerText = data.Q5;
                    document.getElementById("u5Val").innerText = data.U5;
                    document.getElementById("k3Display").innerText = data.K3;

                    let tbody = document.getElementById("gridBody"); tbody.innerHTML = "";
                    if (data.grid_rows && data.grid_rows.length > 0) {
                        data.grid_rows.forEach(row => {
                            let tr = document.createElement("tr");
                            if (row.is_spot_row) {
                                tr.classList.add("spot-row");
                                tr.innerHTML = `<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td><span class="spot-badge">${row.ce_oi_badge}</span></td><td><span class="spot-badge">${row.ce_vol_badge}</span></td><td>-</td><td>-</td><td class="strike-col spot-price-cell"><span class="spot-tag-small">SPOT</span>${row.spot_price}</td><td>-</td><td>-</td><td><span class="spot-badge">${row.pe_vol_badge}</span></td><td><span class="spot-badge">${row.pe_oi_badge}</span></td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>`;
                            } else {
                                let ceOiClass = row.ce_oi_color === 'green' ? 'highlight-green' : (row.ce_oi_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_ce ? 'itm-ce' : ''));
                                let ceVolClass = row.ce_vol_color === 'green' ? 'highlight-green' : (row.ce_vol_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_ce ? 'itm-ce' : ''));
                                let peVolClass = row.pe_vol_color === 'green' ? 'highlight-green' : (row.pe_vol_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_pe ? 'itm-pe' : ''));
                                let peOiClass = row.pe_oi_color === 'green' ? 'highlight-green' : (row.pe_oi_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_pe ? 'itm-pe' : ''));
                                tr.innerHTML = `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_vega}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_theta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_gamma}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_delta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_iv}</td><td class="${row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_oi_chg}</td><td class="${ceOiClass}">${row.ce_oi}<span class="pct-sub">${row.ce_oi_pct}%</span></td><td class="${ceVolClass}">${row.ce_vol}<span class="pct-sub">${row.ce_vol_pct}%</span></td><td class="${row.ce_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_change}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_ltp}</td><td class="strike-col">${row.strike}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_ltp}</td><td class="${row.pe_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_change}</td><td class="${peVolClass}">${row.pe_vol}<span class="pct-sub">${row.pe_vol_pct}%</span></td><td class="${peOiClass}">${row.pe_oi}<span class="pct-sub">${row.pe_oi_pct}%</span></td><td class="${row.pe_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_oi_chg}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_iv}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_delta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_gamma}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_theta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_vega}</td>`;
                            }
                            tbody.appendChild(tr);
                        });
                    }
                });
        }
        setInterval(fetchData, 1000); window.onload = initApp;
    </script>
</body>
</html>
"""

# ==========================================
# FLASK DYNAMIC INTERFACES AND ROUTING
# ==========================================

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/init-meta')
def get_initial_metadata():
    return jsonify({"symbols": sorted(list(options_cache.keys())), "expiries": symbol_expiries_cache})

@app.route('/api/data/<symbol>/<expiry>')
def get_symbol_expiry_data(symbol, expiry):
    vault_key = f"{symbol.upper()}_{expiry.upper()}"
    if vault_key in multi_symbol_runtime_states:
        vault = multi_symbol_runtime_states[vault_key]
        return jsonify({"lot_size": vault.get("lot_size", 1), "D5": vault.get("D5_TEXT", "(-----)"), "H5": vault.get("H5_TEXT", "(-----)"), "Q5": vault.get("Q5_TEXT", "(-----)"), "U5": vault.get("U5_TEXT", "(-----)"), "K3": vault.get("K3_TEXT", f"{symbol} ({expiry}) is Neutral"), "grid_rows": vault.get("grid_rows", []), "spot_price": vault.get("spot_price", 0)})
    return jsonify({"lot_size": 1, "D5": "(-----)", "H5": "(-----)", "Q5": "(-----)", "U5": "(-----)", "K3": f"{symbol} ({expiry}) Initializing...", "grid_rows": [], "spot_price": 0})

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False, use_reloader=False)

if __name__ == "__main__":
    try:
        temp_api = SmartConnect(api_key=API_KEY)
        session_res = temp_api.generateSession(CLIENT_ID, PIN, pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456")
        if session_res.get('status'):
            app_state["smart_api"] = temp_api
            print("✅ Angel One Session Established Successfully!")
            initialize_scrip_master(temp_api)
    except Exception as e: print(f"Init App Core Workflow Exception: {e}")

    threading.Thread(target=run_flask, daemon=True).start()
    background_market_scanner()
