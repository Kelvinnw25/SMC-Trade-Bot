import ccxt

API = 'Bkt9wgj8T2AzE79SUAiwwqRVO9eKSMWTXkxFJj9DRypfArTdhveuiEHNWiFYk2XS'
SEC = 'iIadgzlYhHSJdy824I2KELzprSS7SXh6KZpL1CGcAoIJwLMAdADiVS772r50b3Td'

def run_test():
    print(f"--- [TOTAL AMNESIA MODE - 15 MEI 2026] ---")
    
    exchange = ccxt.binance({
        'apiKey': API,
        'secret': SEC,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': False, # MATIIN! Biar gak nanya jam ke Mainnet
        }
    })

    # JURUS PAMUNGKAS: Hapus semua mapping URL bawaan CCXT
    # Kita ganti SEMUANYA ke domain Demo
    demo_url = 'https://demo-fapi.binance.com/fapi'
    
    # Kita paksa semua jenis API (public, private, sapi, dapi, dll) belok ke Demo
    for api_type in list(exchange.urls['api'].keys()):
        exchange.urls['api'][api_type] = demo_url

    try:
        print("Mencoba narik saldo tanpa nengok ke Mainnet sama sekali...")
        # Kita pake request manual ke v2/account biar ccxt gak pake logika internalnya
        res = exchange.request('v2/account', 'fapiPrivate', 'GET')
        
        if 'assets' in res:
            print(f"✅ AKHIRNYA TEMBUS, KELVIN!")
            for asset in res['assets']:
                if asset['asset'] == 'USDT':
                    print(f"💰 Saldo USDT Demo: {asset['walletBalance']}")
                    break
        else:
            print(f"⚠️ Respon diterima tapi format beda: {res}")

    except Exception as e:
        print(f"\n❌ ERROR: {e}")

if __name__ == "__main__":
    run_test()