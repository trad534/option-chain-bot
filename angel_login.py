import os
import json
import numpy as np
import urllib.request
import pandas as pd
import pyotp
from scipy.stats import norm
from datetime import datetime
from SmartApi import SmartConnect

CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

# --- Black-Scholes Greeks Calculation Functions ---
def calculate_greeks(flag, S, K, t, r, sigma):
    if t <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(t))
    vega = S * norm.pdf(d1) * np.sqrt(t) / 100  # 1% change
    
    if flag.upper() == 'CE':
        delta = norm.cdf(d1)
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) - r * K * np.exp(-r * t) * norm.cdf(d2)) / 365
    else:
        delta = norm.cdf(d1) - 1
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) + r * K * np.exp(-r * t) * norm.cdf(-d2)) / 365
        
    return {'delta': round(delta, 4), 'gamma': round(gamma, 6), 'theta': round(theta, 4), 'vega': round(vega, 4)}

def get_live_option_chain():
    try:
        smart_api = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api.generateSession(CLIENT_ID, PIN, totp)
        
        if not data.get('status'):
            print(f"❌ Login Failed: {data.get('message')}")
            return
            
        print("✅ Angel One Login Successful!")

        # 1. Fetch Master Scrip Data
        url = "https://margincalculator.angelbroking.com/open-api/tokenMaster/OpenAPIScripMaster.json"
        response = urllib.request.urlopen(url)
        scrip_data = json.loads(response.read().decode())
        df = pd.DataFrame(scrip_data)

        # 2. Get NIFTY Spot Price
        nifty_spot = df[(df['name'] == 'NIFTY') & (df['instrumenttype'] == 'AMXIDX') & (df['exch_seg'] == 'NSE')].iloc[0]
        spot_quote = smart_api.getMarketData("FULL", {"NSE": [str(nifty_spot['token'])]})
        spot_price = float(spot_quote['data']['fetched'][0]['ltp'])
        print(f"📌 NIFTY Live Spot Price: {spot_price}")

        # 3. Filter Options (ATM +- 500 Points & Near Expiry)
        nifty_opts = df[(df['name'] == 'NIFTY') & (df['instrumenttype'] == 'OPTIDX') & (df['exch_seg'] == 'NFO')].copy()
        nifty_opts['strike'] = nifty_opts['strike'].astype(float) / 100
        
        earliest_expiry = nifty_opts['expiry'].min()
        expiry_opts = nifty_opts[nifty_opts['expiry'] == earliest_expiry].copy()

        atm_strike = round(spot_price / 50) * 50
        filtered_opts = expiry_opts[(expiry_opts['strike'] >= atm_strike - 500) & (expiry_opts['strike'] <= atm_strike + 500)].copy()

        # 4. Fetch Live Quotes
        tokens_nfo = list(filtered_opts['token'].unique())
        quotes = smart_api.getMarketData("FULL", {"NFO": tokens_nfo})
        
        if quotes and quotes.get('status') and quotes['data']['fetched']:
            quotes_df = pd.DataFrame(quotes['data']['fetched'])
            
            final_df = pd.merge(filtered_opts[['token', 'symbol', 'strike', 'symbolname', 'expiry']], 
                                quotes_df[['tradingSymbol', 'ltp', 'opnInterest', 'volume']], 
                                left_on='symbol', right_on='tradingSymbol', how='inner')

            # 5. Calculate Time to Expiry (t in years)
            exp_date = datetime.strptime(earliest_expiry, "%d%b%Y")
            time_to_expiry = max((exp_date - datetime.now()).days / 365.0, 0.001)

            # 6. Calculate Greeks for Each Row
            deltas, gammas, thetas, vegas = [], [], [], []
            r = 0.07  # Risk-free rate (7%)
            default_iv = 0.15  # 15% estimated IV

            for idx, row in final_df.iterrows():
                opt_type = 'CE' if row['symbol'].endswith('CE') else 'PE'
                greeks = calculate_greeks(opt_type, spot_price, row['strike'], time_to_expiry, r, default_iv)
                
                deltas.append(greeks['delta'])
                gammas.append(greeks['gamma'])
                thetas.append(greeks['theta'])
                vegas.append(greeks['vega'])

            final_df['Delta'] = deltas
            final_df['Gamma'] = gammas
            final_df['Theta'] = thetas
            final_df['Vega'] = vegas

            # Save to CSV
            output_file = "live_nifty_option_chain.csv"
            final_df.to_csv(output_file, index=False)
            print(f"🎉 Live Data with Greeks Saved Successfully to '{output_file}'!")

    except Exception as e:
        print(f"❌ Error during processing: {str(e)}")

if __name__ == "__main__":
    get_live_option_chain()
