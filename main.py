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
# SERVER KEEP-ALIVE SYSTEM (SELF PING)
# ==========================================
RENDER_URL = "https://onrender.com"

def self_ping():
    """सर्वर को स्लीप मोड (Idle) में जाने से रोकने के लिए हर 3 मिनट में पिंग करता है"""
    while True:
        try:
            if RENDER_URL:
                response = requests.get(RENDER_URL, timeout=10)
                print(f"Self-ping status code: {response.status_code}")
        except Exception as e:
            print(f"Self-ping temporary error: {e}")
        time.sleep(180)

# पिंग सर्विस को बैकग्राउंड थ्रेड में स्टार्ट करना
ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()

# प्रिंट बफरिंग को रोकने के लिए फ्लश ओवरराइड
def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# ==========================================
# FLASK & ENVIRONMENT INITIALIZATION
# ==========================================
app = Flask(__name__)

CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

# ==========================================
# CORE STATE AND MEMORY SYSTEM
# ==========================================
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
# ROBUST BLACK-SCHOLES OPTION GREEKS ENGINE
# ==========================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    """
    ब्लैक-स्कोल्स मॉडल का उपयोग करके IV और ग्रीक्स की गणना करता है।
    तार्किक विसंगतियों को रोकने के लिए असाधारण इनपुट को स्वचालित रूप से संभालता है।
    """
    # समय समाप्त या अमान्य इनपुट के लिए बेस-केस हैंडलिंग
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0.0, 'delta': round(delta, 3), 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}

    # आंतरिक मूल्य (Intrinsic Value) की गणना और सुरक्षा कुशन जोड़ना
    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic: 
        ltp = intrinsic + 0.05

    # IV खोजने के लिए ऑब्जेक्टिव फंक्शन (रूट फाइंडिंग)
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

    # Brentq पद्धति द्वारा वास्तविक समय में इम्प्लाइड वोलाटिलिटी खोजना
    try: 
        sigma = brentq(objective_function, 1e-4, 5.0, xtol=1e-6, maxiter=100)
    except Exception: 
        sigma = 0.20  # फॉलबैक बेस वोलाटिलिटी (20%) यदि सीमा से बाहर हो

    # अंतर मानों (d1, d2) की अंतिम गणना
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    
    # ग्रीक्स फॉर्मूला कार्यान्वयन
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
# SUPPORT & RESISTANCE DEEP MATRIX FILTER
# ==========================================
def extract_top_two(data_array):
    """
    ऑप्शन डेटा ऐरे में से सर्वोच्च और द्वितीय सर्वोच्च वैल्यूज को फ़िल्टर करता है
    और उनके बीच की कमजोरी का सटीक प्रतिशत (Percentile Weakness) निकालता है।
    """
    if not data_array or len(data_array) == 0:
        return {
            'highest': {'strike': 0, 'val': 0, 'row': 0}, 
            'second': {'strike': 0, 'val': 0, 'row': 0}, 
            'pct': 0.0
        }
        
    # शून्य से बड़ी वैल्यूज को फ़िल्टर करना
    filtered = [item for item in data_array if item['val'] > 0]
    if len(filtered) == 0:
        return {
            'highest': {'strike': 0, 'val': 0, 'row': 0}, 
            'second': {'strike': 0, 'val': 0, 'row': 0}, 
            'pct': 0.0
        }
        
    # वैल्यूज को अवरोही क्रम (Descending Order) में सॉर्ट करना
    filtered.sort(key=lambda x: x['val'], reverse=True)
    highest = filtered[0]
    
    # यदि दूसरी वैल्यू उपलब्ध है तो उसे लें, अन्यथा पहली को ही बेस मानकर शून्य वैल्यू दें
    if len(filtered) > 1:
        second = filtered[1]
    else:
        second = {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
        
    # कमजोरी के प्रतिशत की सटीक गणना
    pct = (second['val'] / highest['val']) * 100.0 if highest['val'] > 0 else 0.0
    
    return {'highest': highest, 'second': second, 'pct': round(pct, 2)}
# ==========================================
# SHIFTING DIRECTION DETECTOR ENGINE
# ==========================================
def detect_live_state_advanced(top_obj, is_put_side):
    """
    75% थ्रेशोल्ड के आधार पर सपोर्ट/रेजिस्टेंस की कमजोरी की दिशा तय करता है।
    is_put_side = False (CE Side): स्ट्राइक बढ़ने पर WTT, घटने पर WTB.
    is_put_side = True (PE Side): स्ट्राइक घटने पर WTB, बढ़ने पर WTT.
    """
    if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0:
        return "stable strong", round(top_obj['pct'], 1)
        
    highest_strike = top_obj['highest']['strike']
    second_strike = top_obj['second']['strike']
    
    if is_put_side:
        # पुट साइड (सपोर्ट): नीचे की स्ट्राइक पर कमजोरी = WTB, ऊपर की स्ट्राइक पर = WTT
        state = "wtb" if second_strike < highest_strike else "wtt"
    else:
        # कॉल साइड (रेजिस्टेंस): ऊपर की स्ट्राइक पर कमजोरी = WTT, नीचे की स्ट्राइक पर = WTB
        state = "wtt" if second_strike > highest_strike else "wtb"
        
    return state, round(top_obj['pct'], 1)

def get_full_status_string(top_obj, raw_state, cache_key):
    """
    स्टेट मेमोरी का उपयोग करके ट्रांजिशन स्टेट्स (जैसे WTT से Strong होना) को डिकोड करता है।
    """
    if not top_obj or not top_obj.get('highest') or top_obj['highest']['strike'] == 0:
        return "stable strong"
        
    current_weak_hist = get_memory("WEAK_HIST_" + cache_key, "")
    
    if raw_state == "stable strong":
        if current_weak_hist == "wtt": 
            return "wtt to strong"
        if current_weak_hist == "wtb": 
            return "wtb to strong"
        return "stable strong"
        
    return raw_state
# ==========================================
# CORE 27-SCENARIO CORE DECISION ENGINE
# ==========================================
def evaluate_27_scenarios(symbol_name, checkCe, checkPe):
    """
    सपोर्ट और रेजिस्टेंस की 9x9 अवस्थाओं को प्रोसेस करके
    LTP Calculator के वास्तविक 27 सिनेरियो और मार्केट सेंटीमेंट को डिकोड करता है।
    """
    # बेस डिफॉल्ट्स
    scenario_no = "Neutral"
    sentiment = "Neutral"
    description = "Market is in a range-bound or consolidating state."
    
    # ----------------------------------------------------------------
    # CASE A: RESISTANCE IS STRONG (9 Scenarios based on Support State)
    # ----------------------------------------------------------------
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
        elif checkPe == "wtt to strong":
            scenario_no, sentiment = "Scenario 4", "Slightly Bearish"
            description = "Support was WTT but became Strong. Reversal state."
        elif checkPe == "wtb to strong":
            scenario_no, sentiment = "Scenario 5", "Slightly Bullish"
            description = "Support was WTB but became Strong. Short-covering bounce."
            
    # ----------------------------------------------------------------
    # CASE B: RESISTANCE IS WTT (9 Scenarios based on Support State)
    # ----------------------------------------------------------------
    elif checkCe == "wtt":
        if checkPe == "stable strong":
            scenario_no, sentiment = "Scenario 6", "Bullish"
            description = "Resistance WTT & Support Strong. Highly Bullish. Look for extensions."
        elif checkPe == "wtt":
            scenario_no, sentiment = "Scenario 8", "Highly Bullish / Both Side Risky"
            description = "Both Resistance & Support are WTT. Double pressure upwards."
        elif checkPe == "wtb":
            scenario_no, sentiment = "Scenario 9", "Extremely Volatile"
            description = "Resistance WTT & Support WTB. Cross-currents. High risk zone."
        elif checkPe == "wtb to strong":
            scenario_no, sentiment = "Scenario 7", "Bullish"
            description = "Resistance WTT & Support recovering to strong."

    # ----------------------------------------------------------------
    # CASE C: RESISTANCE IS WTB (9 Scenarios based on Support State)
    # ----------------------------------------------------------------
    elif checkCe == "wtb":
        if checkPe == "stable strong":
            scenario_no, sentiment = "Scenario 10", "Bearish"
            description = "Resistance WTB & Support Strong. Selling pressure increasing."
        elif checkPe == "wtb":
            scenario_no, sentiment = "Scenario 11", "Highly Bearish"
            description = "Both Resistance & Support are WTB. Bloodbath or sharp breakdown."
        elif checkPe == "wtt":
            scenario_no, sentiment = "Scenario 12", "Confused / Both Side Risky"
            description = "Resistance WTB & Support WTT. Divergent paths. Avoid major positions."

    # ----------------------------------------------------------------
    # CASE D: RESISTANCE TRANSITION STATES (WTT/WTB to Strong)
    # ----------------------------------------------------------------
    elif "strong" in checkCe:
        if "strong" in checkPe:
            scenario_no, sentiment = "Scenario 13", "Neutral"
            description = "Both sides stabilizing after dynamic shifting."
        elif "wtt" in checkPe:
            scenario_no, sentiment = "Scenario 14", "Slightly Bullish"
        elif "wtb" in checkPe:
            scenario_no, sentiment = "Scenario 15", "Slightly Bearish"

    # फाइनल डिस्प्ले स्ट्रिंग जेनरेशन
    k3Text = f"{symbol_name}: {scenario_no} ({sentiment}) - {description}"
    return k3Text
# ==========================================
# MASTER SYMBOL SCENARIO PROCESSOR
# ==========================================
def process_symbol_scenario(symbol_name, strikes_data, spot_price, lot_size, time_to_expiry_years):
    """
    प्रत्येक सिंबल के ऑप्शन चेन डेटा को प्रोसेस करता है, सपोर्ट/रेजिस्टेंस
    का चयन करता है, ग्रीक्स की गणना करता है और स्टेट्स को अपडेट करता है।
    """
    global global_run_date, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        current_time_str = now_dt.strftime("%H:%M")
        today_str = now_dt.strftime("%Y-%m-%d")

        # प्रत्येक नए दिन मेमोरी रिफ्रेश करना
        if global_run_date != today_str:
            script_memory.clear()
            global_run_date = today_str

        # सिंबल के लिए रनटाइम स्टेट इनिशियलाइज़ेशन
        if symbol_name not in multi_symbol_runtime_states:
            multi_symbol_runtime_states[symbol_name] = {
                "D5_TEXT": "(-----)", "H5_TEXT": "(-----)", "U5_TEXT": "(-----)", "Q5_TEXT": "(-----)",
                "K3_TEXT": f"{symbol_name} is Initializing...",
                "grid_rows": [],
                "spot_price": spot_price
            }

        sym_vault = multi_symbol_runtime_states[symbol_name]

        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
        raw_rows = []

        # अधिकतम वैल्यूज खोजना (परसेंटाइल बेस नॉर्मलाइजेशन के लिए)
        max_ce_oi = max([r['ce_oi'] for r in strikes_data], default=1) or 1
        max_ce_vol = max([r['ce_vol'] for r in strikes_data], default=1) or 1
        max_pe_vol = max([r['pe_vol'] for r in strikes_data], default=1) or 1
        max_pe_oi = max([r['pe_oi'] for r in strikes_data], default=1) or 1

        # प्रत्येक स्ट्राइक प्राइस के लिए डेटा प्रोसेसिंग
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

            # ग्रीक्स की लाइव गणना (Risk Free Rate = 7% मानकर)
            ce_greeks = calculate_iv_and_greeks('CE', spot_price, strike, time_to_expiry_years, 0.07, ce_ltp)
            pe_greeks = calculate_iv_and_greeks('PE', spot_price, strike, time_to_expiry_years, 0.07, pe_ltp)

            # परसेंटाइल रेशियो निकालना
            ce_oi_pct = round((ce_oi / max_ce_oi) * 100, 1) if max_ce_oi > 0 else 0.0
            ce_vol_pct = round((ce_vol / max_ce_vol) * 100, 1) if max_ce_vol > 0 else 0.0
            pe_vol_pct = round((pe_vol / max_pe_vol) * 100, 1) if max_pe_vol > 0 else 0.0
            pe_oi_pct = round((pe_oi / max_pe_oi) * 100, 1) if max_pe_oi > 0 else 0.0

            # एरेज़ में अपेंड करना
            ceOiArray.append({'strike': strike, 'val': ce_oi, 'row': row_idx})
            ceVolArray.append({'strike': strike, 'val': ce_vol, 'row': row_idx})
            peVolArray.append({'strike': strike, 'val': pe_vol, 'row': row_idx})
            peOiArray.append({'strike': strike, 'val': pe_oi, 'row': row_idx})

            raw_rows.append({
                'strike': strike,
                'ce_vega': ce_greeks['vega'], 'ce_theta': ce_greeks['theta'], 'ce_gamma': ce_greeks['gamma'], 'ce_delta': ce_greeks['delta'], 'ce_iv': ce_greeks['iv'],
                'ce_oi_chg': ce_oi_chg, 'ce_oi': ce_oi, 'ce_oi_pct': ce_oi_pct, 'ce_vol': ce_vol, 'ce_vol_pct': ce_vol_pct, 'ce_change': ce_change, 'ce_ltp': ce_ltp,
                'pe_ltp': pe_ltp, 'pe_change': pe_change, 'pe_vol': pe_vol, 'pe_vol_pct': pe_vol_pct, 'pe_oi': pe_oi, 'pe_oi_pct': pe_oi_pct, 'pe_oi_chg': pe_oi_chg,
                'pe_iv': pe_greeks['iv'], 'pe_delta': pe_greeks['delta'], 'pe_gamma': pe_greeks['gamma'], 'pe_theta': pe_greeks['theta'], 'pe_vega': pe_greeks['vega'],
                'is_itm_ce': bool(strike < spot_price),
                'is_itm_pe': bool(strike > spot_price)
            })

        # टॉप 2 ओआई और वॉल्यूम स्ट्राइक्स एक्सट्रैक्ट करना
        ceVolTop = extract_top_two(ceVolArray)
        ceOiTop = extract_top_two(ceOiArray)
        peVolTop = extract_top_two(peVolArray)
        peOiTop = extract_top_two(peOiArray)

        if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike'] or not peVolTop['highest']['strike'] or not peOiTop['highest']['strike']: 
            return

        # लाइव कमजोरी स्टेट्स का विश्लेषण (कॉल साइड = False, पुट साइड = True)
        ceVolRawState, ceVolPctVal = detect_live_state_advanced(ceVolTop, False)
        ceOiRawState, ceOiPctVal = detect_live_state_advanced(ceOiTop, False)
        peVolRawState, peVolPctVal = detect_live_state_advanced(peVolTop, True)
        peOiRawState, peOiPctVal = detect_live_state_advanced(peOiTop, True)

        # विनर लॉजिक (Winner Selection Framework)
        ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"
        peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        # पूर्ण स्टेटस टेक्स्ट प्राप्त करना (हिस्ट्री चेकिंग के साथ)
        ceVolStatusText = get_full_status_string(ceVolTop, ceVolRawState, "CE_VOL")
        ceOiStatusText = get_full_status_string(ceOiTop, ceOiRawState, "CE_OI")
        peVolStatusText = get_full_status_string(peVolTop, peVolRawState, "PE_VOL")
        peOiStatusText = get_full_status_string(peOiTop, peOiRawState, "PE_OI")

        # कमजोरी की ऐतिहासिक यादें (Memory) सहेजें
        if ceOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_VOL", peVolRawState)

        # स्वतंत्र डैशबोर्ड कार्ड टेक्स्ट जेनरेशन
        def calculate_independent_cell(top_obj, cache_key, side_label, final_status_text):
            strike = top_obj['highest']['strike']
            s_val = int(strike) if strike.is_integer() else strike
            display_time = get_memory("TIME_" + cache_key)
            if not display_time:
                display_time = current_time_str
                set_memory("TIME_" + cache_key, display_time)
            output_text = f"({display_time} - {side_label} - {s_val})\n({final_status_text.upper()})"
            return {'text': output_text, 'status': final_status_text}

        ceOiResult  = calculate_independent_cell(ceOiTop, "CE_OI", "Eor OI", ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop, "PE_OI", "Eos OI", peOiStatusText)

        # विनर के आधार पर अंतिम चेकिंग वेरिएबल्स का चयन (Bugs Fixed)
        checkCe = ceOiResult['status'] if ceWinner == "OI" else ceVolResult['status']
        checkPe = peOiResult['status'] if peWinner == "OI" else peVolResult['status']

        # 27 परिदृश्यों के डिसीजन मैट्रिक्स को फायर करना
        k3Text = evaluate_27_scenarios(symbol_name, checkCe, checkPe)

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
        # HTML रंग कोडिंग और ग्रिड रोज़ का निर्माण
        for r in raw_rows:
            st = r['strike']
            r['ce_oi_color'] = 'green' if st == highest_ce_oi_strike else ('yellow' if st == second_ce_oi_strike else '')
            r['ce_vol_color'] = 'green' if st == highest_ce_vol_strike else ('yellow' if st == second_ce_vol_strike else '')
            r['pe_vol_color'] = 'green' if st == highest_pe_vol_strike else ('yellow' if st == second_pe_vol_strike else '')
            r['pe_oi_color'] = 'green' if st == highest_pe_oi_strike else ('yellow' if st == second_pe_oi_strike else '')

            # स्पॉट मूल्य के पार होने पर मध्य रेड लाइन रो को इंजेक्ट करना
            if not spot_inserted and spot_price > 0 and st >= spot_price:
                grid_rows_built.append({
                    'is_spot_row': True,
                    'spot_price': spot_price,
                    'ce_oi_badge': f"{ceOiRawState.upper()} {ceOiPctVal}%",
                    'ce_vol_badge': f"{ceVolRawState.upper()} {ceVolPctVal}%",
                    'pe_vol_badge': f"{peVolRawState.upper()} {peVolPctVal}%",
                    'pe_oi_badge': f"{peOiRawState.upper()} {peOiPctVal}%"
                })
                spot_inserted = True

            r['is_spot_line'] = False
            grid_rows_built.append(r)

        # यदि स्पॉट मूल्य अंत तक इंजेक्ट नहीं हुआ हो
        if not spot_inserted and spot_price > 0:
            grid_rows_built.append({
                'is_spot_row': True,
                'spot_price': spot_price,
                'ce_oi_badge': f"{ceOiRawState.upper()} {ceOiPctVal}%",
                'ce_vol_badge': f"{ceVolRawState.upper()} {ceVolPctVal}%",
                'pe_vol_badge': f"{peVolRawState.upper()} {peVolPctVal}%",
                'pe_oi_badge': f"{peOiRawState.upper()} {peOiPctVal}%"
            })

        # सिंबल वॉल्ट में अंतिम परिष्कृत डेटा सहेजना (All Bug Fixes Applied)
        sym_vault["D5_TEXT"] = ceOiResult['text'] if ceWinner == "OI" else "(-----)"
        sym_vault["H5_TEXT"] = ceVolResult['text'] if ceWinner == "VOL" else "(-----)"
        sym_vault["Q5_TEXT"] = peVolResult['text'] if peWinner == "VOL" else "(-----)"
        sym_vault["U5_TEXT"] = peOiResult['text'] if peWinner == "OI" else "(-----)"
        sym_vault["K3_TEXT"] = k3Text
        sym_vault["grid_rows"] = grid_rows_built
        sym_vault["spot_price"] = spot_price

    except Exception as e:
        print(f"Error in scenario engine for {symbol_name}: {e}")
# ==========================================
# INTELLIGENT SCRIP MASTER & INDEX MAPPING
# ==========================================
def initialize_scrip_master(api_client):
    """
    ऑल-इन-वन एंजेल वन स्क्रिप मास्टर डाउनलोडर।
    स्पॉट डेटा एरर्स को रोकने के लिए इंडेक्स और FNO ऑप्शंस को अलग-अलग कैश करता है।
    """
    global full_scrip_df, eq_cache, options_cache
    try:
        print("📥 Downloading Angel One Scrip Master JSON...")
        url = "https://angelbroking.com"
        resp = requests.get(url, timeout=25)
        
        if resp.status_code == 200:
            scrip_data = resp.json()
            full_scrip_df = pd.DataFrame(scrip_data)
            
            # कॉलम नेम नॉर्मलाइजेशन
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns: 
                full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns: 
                full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            if 'strike' in full_scrip_df.columns: 
                full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0) / 100.0
            
            # 💡 इंटेलिजेंट इंडेक्स स्पॉट मैपिंग (Fixes the missing index spot price issue)
            # निफ्टी, बैंकनिफ्टी और फिननिफ्टी जैसे प्रमुख सूचकांकों के लिए हार्डकोडेड सटीक स्पॉट टोकन
            eq_cache["NIFTY"] = ("99926000", "NSE")        # Nifty 50 Index Token
            eq_cache["BANKNIFTY"] = ("99926009", "NSE")    # Nifty Bank Index Token
            eq_cache["FINNIFTY"] = ("99926037", "NSE")     # Nifty Fin Services Index Token
            
            # सामान्य NSE इक्विटी शेयर्स को कैश में लोड करना
            nse_eq = full_scrip_df[(full_scrip_df['exch_seg'] == 'NSE') & (~full_scrip_df['instrumenttype'].str.contains('OPT|FUT', na=False))]
            for _, row in nse_eq.iterrows():
                sym = str(row['symbol']).strip().upper()
                name = str(row['name']).strip().upper()
                tok = str(row['token']).strip()
                
                # यदि इंडेक्स नाम पहले से मैप न हो, तो ही इक्विटी टोकन भरें
                if sym not in eq_cache: 
                    eq_cache[sym] = (tok, 'NSE')
                if name not in eq_cache: 
                    eq_cache[name] = (tok, 'NSE')
            
            # डेरिवेटिव (NFO) ऑप्शंस को कैश में स्ट्रक्चर करना
            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce').fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()): 
                options_cache[name.strip()] = group
                
            print(f"✅ Scrip Master Loaded Successfully for {len(options_cache)} FNO symbols!")
    except Exception as e: 
        print(f"⚠ Scrip Master Initialization Error: {e}")
# ==========================================
# REAL-TIME MULTI-BATCH OPTION CHAIN SCANNER
# ==========================================
def background_market_scanner():
    """
    पृष्ठभूमि (Background) स्कैनर जो लगातार चुनिंदा FNO सिंबल्स को स्कैन करता है,
    सटीक स्पॉट प्राइस लाता है, 31 एक्टिव स्ट्राइक्स लॉक करता है और बैच डेटा फेच करता है।
    """
    while True:
        try:
            api = app_state["smart_api"]
            if not api or not options_cache:
                time.sleep(3)
                continue

            # टॉप FNO सिंबल्स को प्राथमिकता के आधार पर स्कैन करना (जैसे NIFTY, BANKNIFTY आदि)
            for symbol_name in list(options_cache.keys())[:30]:
                try:
                    sym_options = options_cache[symbol_name]
                    now_dt = datetime.now()
                    
                    # केवल वही ऑप्शंस फ़िल्टर करें जिनकी एक्सपायरी आज या आज के बाद की है
                    valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
                    if valid_opts.empty: 
                        continue

                    # निकटतम (Current Weekly/Monthly) एक्सपायरी खोजना
                    unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
                    current_expiry_dt = unique_expiries_dt[0]
                    current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()
                    
                    # एक्सपायरी के दिनों को वर्षों में बदलना (Option Greeks के फॉर्मूले के लिए)
                    time_to_expiry_days = max((current_expiry_dt - pd.Timestamp(now_dt.date())).days, 1)
                    time_to_expiry_years = time_to_expiry_days / 365.0
                    lot_size = int(float(current_expiry_options['lotsize'].iloc[0])) if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty else 1

                    # 💡 लाइव स्पॉट प्राइस फेचिंग (Using fixed token mapping from Part 8)
                    spot_token, spot_exchange = eq_cache.get(symbol_name, ("", "NSE"))
                    spot_price = 0.0
                    
                    if spot_token:
                        # सूचकांकों या शेयरों का रीयल-टाइम फुल मार्केट टिकर खींचना
                        spot_quote = api.getMarketData("FULL", {spot_exchange: [spot_token]})
                        if spot_quote and spot_quote.get('status') and spot_quote.get('data') and spot_quote['data'].get('fetched'):
                            spot_price = float(spot_quote['data']['fetched'][0].get('ltp', 0))

                    # यदि लाइव API से स्पॉट 0 आता है (जैसे मार्केट बंद होने पर), तो मिडल स्ट्राइक को बेस मानें
                    if spot_price <= 0:
                        all_strikes_list = sorted(current_expiry_options['strike'].unique())
                        spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                    # 🎯 ATM के ऊपर की 15 और नीचे की 15 (कुल 31) सक्रिय स्ट्राइक प्राइस को लॉक करना
                    all_master_strikes = sorted(current_expiry_options['strike'].unique())
                    closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
                    strikes = all_master_strikes[max(0, closest_idx - 15):min(len(all_master_strikes), closest_idx + 16)]

                    # सिलेक्टेड स्ट्राइक्स के टोकन्स का एक्सचेंज मैप बनाना
                    filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
                    exch_map = {}
                    for _, row in filtered_opts.iterrows():
                        exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
                        if exch not in exch_map: 
                            exch_map[exch] = []
                        exch_map[exch].append(tok)

                    # ⚡ API बैचिंग: रेट-लिमिट से बचने के लिए 40-40 टोकन्स के टुकड़ों में कॉल फेच करना
                    all_dash_quotes = []
                    for exch, t_list in exch_map.items():
                        for i in range(0, len(t_list), 40):
                            res = api.getMarketData("FULL", {exch: t_list[i:i+40]})
                            if res and res.get('status') and res.get('data') and res['data'].get('fetched'): 
                                all_dash_quotes.extend(res['data']['fetched'])
                    # लाइव कोड्स को डिक्शनरी फॉर्मेट में व्यवस्थित और कैश करना
                    dash_quotes_dict = {}
                    for item in all_dash_quotes:
                        tok = str(item.get('symbolToken', item.get('token', ''))).strip()
                        ltp = float(item.get('ltp', 0))
                        volume = float(item.get('tradeVolume', 0))
                        oi = float(item.get('opnInterest', 0))
                        net_change = float(item.get('netChange', item.get('change', 0)))
                        
                        # यदि डेटा लाइव आ रहा है, तो इसे परसिस्टेंट कैशे में बैकअप करें
                        if ltp > 0 or oi > 0: 
                            persistent_quotes_cache[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi, 'net_change': net_change}

                    # सुबह का बेस OI सेट करना और नेट चेंज निकालना
                    for tok, q_data in persistent_quotes_cache.items():
                        if tok not in app_state["morning_oi"] and q_data['oi'] > 0: 
                            app_state["morning_oi"][tok] = q_data['oi']
                        
                        base_oi = app_state["morning_oi"].get(tok, q_data['oi'])
                        dash_quotes_dict[tok] = {
                            'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 
                            'oi_chg': q_data['oi'] - base_oi, 'net_change': q_data['net_change']
                        }

                    # कॉल (CE) और पुट (PE) पक्षों के डेटा पेलोड का मिलान करना
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

                    # मास्टर सिनेरियो डिसीजन इंजन को फायर करना
                    process_symbol_scenario(symbol_name, strikes_payload, spot_price, lot_size, time_to_expiry_years)
                except Exception as inner_e:
                    print(f"Error scanning {symbol_name}: {inner_e}")
            time.sleep(1) # रेट लिमिट रोकने के लिए 1 सेकंड का रेस्ट
        except Exception as e:
            print(f"Scanner loop error: {e}")
            time.sleep(5)
# ==========================================
# WEB DASHBOARD INTERFACE (HTML & CSS)
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
        select { padding: 8px; font-size: 16px; background: #ffffff; color: #000000; border: 1px solid #ced4da; border-radius: 5px; cursor: pointer; }
        
        .status-box { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 10px; }
        .card { 
            background: #ffffff; 
            padding: 12px; 
            border-radius: 8px; 
            border: 2px solid #dee2e6; 
            box-shadow: 0 2px 4px rgba(0,0,0,0.05); 
            text-align: center; 
            font-size: 12px; 
            font-weight: bold; 
            white-space: pre-line; 
            color: #212529; 
        }
        .card h3 { margin: 0 0 5px 0; font-size: 13px; color: #495057; text-transform: uppercase; }
        
        .k3-banner { background: #e6fcf5; padding: 14px; margin-top: 10px; text-align: center; font-size: 18px; font-weight: bold; border-radius: 8px; color: #0ca678; border: 1px solid #20c997; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
        
        .table-container { margin-top: 15px; overflow-x: auto; max-height: 700px; border: 1px solid #dee2e6; border-radius: 6px; }
        table { width: 100%; border-collapse: collapse; background: #ffffff; font-size: 11px; text-align: center; }
        th, td { border: 1px solid #dee2e6; padding: 6px 7px; white-space: nowrap; }
        th { background: #f8f9fa; color: #212529; position: sticky; top: 0; z-index: 10; font-size: 11px; font-weight: 600; }
        
        .strike-col { background: #fff3bf !important; color: #000000 !important; font-weight: bold; font-size: 12px; border-left: 2px solid #fab005 !important; border-right: 2px solid #fab005 !important; }
        
        /* 🎯 Spot Row Middle Red Line Implementation */
        .spot-row td { 
            background: #ffffff !important; 
            border-top: 1px solid #dee2e6 !important; 
            border-bottom: 1px solid #dee2e6 !important; 
            position: relative;
            padding: 4px; 
        }
        .spot-row td::after {
            content: "";
            position: absolute;
            left: 0;
            right: 0;
            top: 50%;
            height: 3px;
            background-color: #f03e3e;
            z-index: 2;
        }
        .spot-badge {
            background: #ffffff;
            color: #000000;
            border: 2px solid #495057;
            border-radius: 6px;
            padding: 3px 8px;
            font-weight: bold;
            font-size: 10px;
            display: inline-block;
            position: relative;
            z-index: 3;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .spot-price-cell {
            background: #e03131 !important;
            color: #ffffff !important;
            font-weight: bold;
            font-size: 13px;
            text-align: center;
            z-index: 4;
        }
        .spot-tag-small {
            background: #ffffff;
            color: #e03131;
            font-size: 8px;
            font-weight: bold;
            padding: 1px 4px;
            border-radius: 3px;
            display: block;
            margin-bottom: 2px;
        }

        /* ITM Option Chain Background Colors */
        .itm-ce { background: rgba(240, 62, 62, 0.06); }
        .itm-pe { background: rgba(43, 138, 62, 0.06); }

        /* 👑 Core 75% Theory Shifting Highlight Colors */
        .highlight-green { background: #2b8a3e !important; color: #ffffff !important; font-weight: bold; }
        .highlight-yellow { background: #fab005 !important; color: #000000 !important; font-weight: bold; }
        
        .chg-pos { color: #2b8a3e; font-weight: bold; }
        .chg-neg { color: #c92a2a; font-weight: bold; }
        .pct-sub { font-size: 9px; color: #666; display: block; margin-top: 2px; font-weight: normal; }
    </style>
</head>
<body>
    <div class="header">
        <h2>⚡ LTP Calculator (27 Scenarios Tracking Engine)</h2>
        <div>
            <label style="font-weight: bold;">Select Symbol: </label>
            <select id="symbolSelect" onchange="fetchData()">
                {% for sym in symbols %}
                <option value="{{ sym }}">{{ sym }}</option>
                {% endfor %}
            </select>
        </div>
    </div>

    <!-- 27 Scenarios Display Banner -->
    <div class="k3-banner" id="k3Display">Connecting to Angel One Scanner Server...</div>

    <!-- Live Decision Winners Matrix Cards -->
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
"""
# ==========================================
# CLIENT SIDE REAL-TIME DOM RUNTIME (JS)
# ==========================================
HTML_TEMPLATE += """
    <script>
        function fetchData() {
            let sym = document.getElementById("symbolSelect").value;
            fetch('/api/data/' + sym + '?t=' + new Date().getTime())
                .then(res => res.json())
                .then(data => {
                    // शीर्ष डिसीजन और विनर कार्ड्स को अपडेट करना
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
                                // 🎯 मध्य रेड लाइन रो जनरेशन
                                tr.classList.add("spot-row");
                                tr.innerHTML = `
                                    <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                                    <td><span class="spot-badge">${row.ce_oi_badge}</span></td>
                                    <td><span class="spot-badge">${row.ce_vol_badge}</span></td>
                                    <td>-</td><td>-</td>
                                    <td class="strike-col spot-price-cell"><span class="spot-tag-small">SPOT</span>${row.spot_price}</td>
                                    <td>-</td><td>-</td>
                                    <td><span class="spot-badge">${row.pe_vol_badge}</span></td>
                                    <td><span class="spot-badge">${row.pe_oi_badge}</span></td>
                                    <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                                `;
                            } else {
                                // 👑 75% थ्योरी के अनुसार कलर क्लास असाइनमेंट
                                let ceOiClass = row.ce_oi_color === 'green' ? 'highlight-green' : (row.ce_oi_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_ce ? 'itm-ce' : ''));
                                let ceVolClass = row.ce_vol_color === 'green' ? 'highlight-green' : (row.ce_vol_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_ce ? 'itm-ce' : ''));
                                let peVolClass = row.pe_vol_color === 'green' ? 'highlight-green' : (row.pe_vol_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_pe ? 'itm-pe' : ''));
                                let peOiClass = row.pe_oi_color === 'green' ? 'highlight-green' : (row.pe_oi_color === 'yellow' ? 'highlight-yellow' : (row.is_itm_pe ? 'itm-pe' : ''));

                                let ceOiChgClass = row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg';
                                let peOiChgClass = row.pe_oi_chg >= 0 ? 'chg-pos' : 'chg-neg';
                                let ceChgClass = row.ce_change >= 0 ? 'chg-pos' : 'chg-neg';
                                let peChgClass = row.pe_change >= 0 ? 'chg-pos' : 'chg-neg';

                                // 21-कॉलम डेटा मैपिंग (Bugs Fixed: PE Theta & PE OI Chg Classes)
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
                                    <td class="${peOiChgClass} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_oi_chg}</td>
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

        // 1 सेकंड का रीयल-टाइम रीफ़्रेश लूप
        setInterval(fetchData, 1000);
        window.onload = fetchData;
    </script>
</body>
</html>
"""

# ==========================================
# FLASK FLUID ENDPOINTS & ROUTING
# ==========================================
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

# ==========================================
# DRIVER MAIN WORKFLOW APPLICATION EXECUTION
# ==========================================
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
            print("❌ Session Error during startup. Check credentials.")
    except Exception as e:
        print(f"Init Exception: {e}")

    # Flask सर्वर को बैकग्राउंड थ्रेड में स्टार्ट करना
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # मुख्य थ्रेड पर लाइव स्कैनर को चलाना
    background_market_scanner()
