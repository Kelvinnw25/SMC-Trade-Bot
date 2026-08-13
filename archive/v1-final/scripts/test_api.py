from src.api_handler import BinanceHandler
from src.config import MODE

def main():
    print(f"--- MENCOBA KONEKSI KE BINANCE ({MODE.upper()}) ---")
    
    # Inisialisasi Handler
    bot_api = BinanceHandler()
    
    # 1. Tes tarik Top 30 Koin
    print("\n[1] Menarik Top Volume Coins...")
    top_coins = bot_api.get_top_volume_coins(limit=5)
    if top_coins:
        print(f"Berhasil! Top 5 Koin saat ini: {top_coins}")
    else:
        print("Gagal narik koin. Cek API Key atau koneksi internet lu.")
        return

    # 2. Tes tarik data OHLCV + ATR koin pertama
    test_symbol = top_coins[0]
    print(f"\n[2] Menarik data candle & ATR buat {test_symbol}...")
    df = bot_api.fetch_data_with_atr(test_symbol)
    
    if df is not None:
        print("Berhasil!")
        print(df[['timestamp', 'close', 'atr']].tail()) # Tampilkan 5 data terakhir
    else:
        print(f"Gagal narik data {test_symbol}")

if __name__ == "__main__":
    main()



    