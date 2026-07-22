import ccxt
import math
import requests 
from src.config import MAX_MARGIN_ALLOWED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 

class BinanceExecutor:
    def __init__(self, exchange_client):
        self.client = exchange_client
        self.MAX_MARGIN_ALLOWED = MAX_MARGIN_ALLOWED 

    def setup_account(self, symbol, entry_price, stop_loss):
        """Set margin mode ke ISOLATED dan kembalikan ACTUAL LEVERAGE dari Binance"""
        clean_symbol = symbol.replace('/', '').split(':')[0]
        try:
            self.client.fapiPrivatePostMarginType({
                'symbol': clean_symbol,
                'marginType': 'ISOLATED'
            })
        except Exception:
            pass 

        try:
            price_diff = abs(entry_price - stop_loss)
            if entry_price == 0 or price_diff == 0: return 20 # Default aman

            sl_percentage = price_diff / entry_price
            
            # 🪄 FIX: Tambahkan 35% Safety Buffer
            safe_sl_percentage = sl_percentage * 1.35
            calculated_leverage = 1.0 / safe_sl_percentage
            final_leverage = max(1, min(125, math.floor(calculated_leverage)))
            
            try:
                # 🚨 FIX: Tangkap response dari Binance untuk melihat leverage aktual yang disetujui!
                res = self.client.fapiPrivatePostLeverage({
                    'symbol': clean_symbol,
                    'leverage': final_leverage
                })
                actual_leverage = int(res['leverage'])
                print(f"⚙️ System: {symbol} Leverage accepted by Binance: {actual_leverage}x")
                return actual_leverage
                
            except Exception as lev_err:
                print(f"⚠️ [CRITICAL] Binance rejected {final_leverage}x for {symbol}: {lev_err}. Forcing default 20x.")
                return 20 # Kalau kena limit API, paksa hitungan pakai 20x biar margin ga jebol
                
        except Exception as e:
            print(f"⚠️ System: Failed to adjust dynamic leverage: {e}")
            return 20

    # 🚨 FIX: Tambahin parameter actual_leverage hasil konfirmasi Binance
    def calculate_position_size(self, symbol, entry_price, stop_loss, risk_amount, actual_leverage):
        """Menghitung qty dengan algoritma The Margin Presser anti-jebol"""
        try:
            price_diff = abs(entry_price - stop_loss)
            if entry_price == 0 or price_diff == 0: return 0, "Invalid Price"
            
            sl_percentage = price_diff / entry_price
            
            # 1. PURE NOTIONAL (Size) CALCULATION - Risk fix $100
            ideal_notional = risk_amount / sl_percentage
            
            # 2. MARGIN CHECK (Pakai ACTUAL LEVERAGE dari Binance, bukan tebak-tebakan lagi!)
            expected_margin = ideal_notional / actual_leverage
            
            if expected_margin > self.MAX_MARGIN_ALLOWED:
                print(f"   [⚠️ MARGIN PRESSER] {symbol} Butuh margin ${expected_margin:.2f} (Limit: ${self.MAX_MARGIN_ALLOWED}). Melakukan Auto-Scale Down...")
                notional_value = self.MAX_MARGIN_ALLOWED * actual_leverage
                actual_risk = notional_value * sl_percentage
                print(f"   [📉 SCALED DOWN] Notional dipress ke ${notional_value:.2f} | Risk tersesuaikan jadi ~${actual_risk:.2f}")
            else:
                notional_value = ideal_notional
            
            if notional_value < 6.0:
                notional_value = 6.0
                
            quantity = notional_value / entry_price
            qty_precision = self.client.amount_to_precision(symbol, quantity)
            
            return qty_precision, ""
        except Exception as e:
            print(f"Gagal hitung size: {e}")
            return 0, str(e)

    def send_telegram_alert(self, symbol, side, entry, sl, tp):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Telegram Token/ID belum diset di config. Skip notifikasi.")
            return
            
        pesan = f"""
🚨 <b>SMC SNIPER ALERT</b> 🚨
=========================
🪙 <b>Pair:</b> {symbol}
🛒 <b>Action:</b> LIMIT {side.upper()}
🎯 <b>Entry:</b> {entry}

⚠️ <b>MANUAL ACTION REQUIRED:</b>
🛡️ <b>Set SL:</b> {sl}
💰 <b>Set TP:</b> {tp}
=========================
<i>Buka aplikasi Binance lu sekarang!</i>
"""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": pesan,
            "parse_mode": "HTML"
        }
        
        try:
            requests.post(url, json=payload, timeout=5)
            print(f"📲 Telegram Alert berhasil dikirim ke HP lu!")
        except Exception as e:
            print(f"⚠️ Gagal ngirim Telegram: {e}")

    def place_order_pair(self, symbol, side, entry_price, stop_loss, take_profit, risk_amount):
        """Eksekusi Semi-Auto: Hanya pasang Entry Limit, SL & TP di-log manual"""
        
        # 🚨 FIX: Nangkep leverage asli dari Binance
        actual_leverage = self.setup_account(symbol, entry_price, stop_loss)
        
        # 🚨 FIX: Kirim leverage asli itu ke kalkulator size
        qty, err_msg = self.calculate_position_size(symbol, entry_price, stop_loss, risk_amount, actual_leverage)
        
        if float(qty) <= 0: 
            return False, f"Failed Size: {err_msg}"
        
        clean_symbol = symbol.replace('/', '')

        print(f"📦 Executing {side.upper()} Sniper for {symbol} @ {entry_price} (Qty: {qty} | Lev: {actual_leverage}x)")

        # 1. TEMBAK ENTRY LIMIT SAJA
        entry_params = {
            'symbol': clean_symbol,
            'side': side.upper(),
            'type': 'LIMIT',
            'quantity': qty,
            'price': self.client.price_to_precision(symbol, entry_price),
            'timeInForce': 'GTC'
        }
        
        try:
            self.client.fapiPrivatePostOrder(entry_params)
            print(f"✅ LIMIT ENTRY PLACED: {side.upper()} {qty} {symbol} @ {entry_price}")
            
            print("="*60)
            print("🚨 MANUAL ACTION REQUIRED PENDING ORDER 🚨")
            print(f" ➤ SET SL DI APLIKASI: {stop_loss}")
            print(f" ➤ SET TP DI APLIKASI: {take_profit}")
            print("="*60)
            
            self.send_telegram_alert(symbol, side, entry_price, stop_loss, take_profit)
            
            return True, f"LIMIT SET | SL: {stop_loss} | TP: {take_profit}"
            
        except Exception as e:
            print(f"❌ ENTRY GAGAL: {e}")
            return False, f"API Error - {str(e)[:30]}..."