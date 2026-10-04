import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
import gspread
import gspread.utils
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
    return "Angel One Option Chain Bot & Direct Cell-Read Scenarios Engine is active!"

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
# PYTHON MEMORY STORE & QUOTA OPTIMIZATION
# ==========================================
script_memory = {}
global_run_date = ""
last_written_values = {}

def get_memory(key, default=""):
    return script_memory.get(key, default)

def set_memory(key, value):
    script_memory[key] = str(value)

def remove_memory(key):
    if key in script_memory:
        del script_memory[key]

def safe_update_cell(sheet, cell_name, new_value):
    global last_written_values
    cache_key = f"{sheet.title}_{cell_name}"
    str_val = str(new_value)
    if last_written_values.get(cache_key) != str_val:
        try:
            sheet.update_acell(cell_name, str_val)
            last_written_values[cache_key] = str_val
            time.sleep(0.3)
        except Exception as e:
            print(f"⚠ Cell Update Error on {cell_name}: {e}")

# ==========================================
# COLOR FORMATTING FOR K3 (PRESERVES FONT SIZE & BOLD)
# ==========================================
def update_k3_with_color(spreadsheet_obj, sheet, cell_name, text_value):
    try:
        sheet.update_acell(cell_name, text_value)
        text_lower = text_value.lower()
        if "bull" in text_lower:
            color_dict = {"red": 0.0, "green": 0.0, "blue": 1.0}  # Blue
        elif "bear" in text_lower or "bloodbath" in text_lower:
            color_dict = {"red": 1.0, "green": 0.0, "blue": 0.0}  # Red
        else:
            color_dict = {"red": 0.0, "green": 0.0, "blue": 0.0}  # Black

        row, col = gspread.utils.a1_to_rowcol(cell_name)
        
        body = {
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet.id,
                            "startRowIndex": row - 1,
                            "endRowIndex": row,
                            "startColumnIndex": col - 1,
                            "endColumnIndex": col
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "foregroundColor": color_dict
                                }
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.foregroundColor"
                    }
                }
            ]
        }
        spreadsheet_obj.batch_update(body)
    except Exception as e:
        print(f"⚠ K3 Color Formatting Error: {e}")

# ==========================================
# ROBUST IV & GREEKS
# ==========================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}

    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic:
        ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0: sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2) if flag == 'CE' else K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
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
        'iv': round(sigma * 100, 2), 'delta': round(delta, 3),
        'gamma': round(gamma, 4), 'theta': round(theta, 2), 'vega': round(vega_val, 2)
    }

# ==========================================
# MASTER SCENARIO ENGINE (FIXED STATE PRIORITY)
# ==========================================
# ... (rest of your scenario engine code remains same, with duplicate condition fixed:
# "elif checkCe == 'wtt to strong' and checkPe == 'wtt to strong':")
