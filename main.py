# ==========================================
# LOT SIZE & EXPIRY AUTO UPDATE
# ==========================================
def update_lot_and_expiry():
    global cached_options_df, earliest_expiry
    try:
        option_chain_sheet = spreadsheet.worksheet("Option chain index new")
        selected_symbol = option_chain_sheet.acell("J4").value.strip()

        if not selected_symbol:
            return

        # Lot size SmartAPI scrip master से निकालना
        lot_size = "NA"
        if not cached_options_df.empty and 'symbol' in cached_options_df.columns and 'lotsize' in cached_options_df.columns:
            match = cached_options_df[cached_options_df['symbol'].str.contains(selected_symbol, case=False, na=False)]
            if not match.empty:
                lot_size = int(match['lotsize'].values[0])

        # Update in upstox get data sheet
        data_sheet.update("A7", [[lot_size]])
        data_sheet.update("L9", [[str(earliest_expiry)]])
        print(f"✅ Lot Size & Expiry Updated → {selected_symbol}: Lot {lot_size}, Expiry {earliest_expiry}")
    except Exception as e:
        print(f"⚠️ Error updating Lot Size/Expiry: {e}")
