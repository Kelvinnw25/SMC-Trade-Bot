import ccxt
from src.config import API_KEY, SECRET_KEY

def test():
    # Inisialisasi
    exchange = ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    # Override URL Demo
    exchange.urls['api']['fapiPublic'] = 'https://demo-fapi.binance.com/fapi'
    exchange.urls['api']['fapiPrivate'] = 'https://demo-fapi.binance.com/fapi'

    print("--- TESTING AUTHENTICATION ---")
    try:
        # Panggil fungsi standar CCXT (bukan fungsi ajaib)
        balance = exchange.fetch_balance()
        print("✅ AUTH BERHASIL! Saldo lu terbaca.")
        print(f"Saldo USDT: {balance['total'].get('USDT', 0)}")
    except Exception as e:
        print(f"❌ AUTH GAGAL: {e}")

    print("\n--- TESTING PUBLIC DATA ---")
    try:
        # Panggil tickers standar
        tickers = exchange.fetch_tickers()
        print(f"✅ PUBLIC DATA BERHASIL! Dapet {len(tickers)} tickers.")
    except Exception as e:
        print(f"❌ PUBLIC DATA GAGAL: {e}")

if __name__ == "__main__":
    test()

    