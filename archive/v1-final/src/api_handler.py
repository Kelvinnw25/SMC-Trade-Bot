import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import urllib3
from src.config import API_KEY, SECRET_KEY, MODE, ATR_PERIOD

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class LockedBinance(ccxt.binance):
    def sign(self, path, api='public', method='GET', params={}, headers=None, body=None):
        full_url_obj = super().sign(path, api, method, params, headers, body)
        if api in ['private', 'fapiPrivate', 'fapiPrivateV2']:
            if 'binance.com' in full_url_obj['url']:
                split_url = full_url_obj['url'].split('.com')
                full_url_obj['url'] = f"https://demo-fapi.binance.com{split_url[1]}"
        return full_url_obj

class BinanceHandler:
    def __init__(self):
        self.client = LockedBinance({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': False,
                'verify': False
            }
        })
        
        self.client.precisionMode = ccxt.TICK_SIZE
        self._load_exchange_info_manually()

    def _load_exchange_info_manually(self):
        try:
            print("⚙️ System: Injecting coin specifications (Bypassing CCXT)...")
            url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            data = response.json()
            
            market_data = {}
            for s in data['symbols']:
                if s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT':
                    tick_size = 0.001
                    step_size = 0.001
                    for f in s['filters']:
                        if f['filterType'] == 'PRICE_FILTER':
                            tick_size = float(f['tickSize'])
                        elif f['filterType'] == 'LOT_SIZE':
                            step_size = float(f['stepSize'])
                            
                    symbol_ccxt = f"{s['baseAsset']}/{s['quoteAsset']}"
                    market_data[symbol_ccxt] = {
                        'id': s['symbol'],
                        'symbol': symbol_ccxt,
                        'base': s['baseAsset'],
                        'quote': s['quoteAsset'],
                        'type': 'swap',
                        'swap': True,
                        'spot': False,
                        'active': s['status'] == 'TRADING',
                        'precision': {'amount': step_size, 'price': tick_size},
                        'limits': {'amount': {'min': step_size}}
                    }
            
            self.client.markets = market_data
            self.client.markets_by_id = {m['id']: m for m in market_data.values()}
            self.client.symbols = list(market_data.keys())
            self.client.markets_loaded = True
            print(f"✅ System: {len(market_data)} Pairs successfully loaded into memory!")
            
        except Exception as e:
            print(f"⚠️ System: Failed to bypass exchange info: {e}")

    def get_top_volume_coins(self, limit=30):
        try:
            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            tickers = response.json()
            
            if isinstance(tickers, dict) and 'code' in tickers:
                print(f"   [⚠️ API LIMIT] Binance: {tickers.get('msg', 'Too many requests')}")
                return []
            
            usdt_pairs = [t for t in tickers if t['symbol'].endswith('USDT') and '_' not in t['symbol']]
            usdt_pairs.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
            
            top_coins = []
            for t in usdt_pairs[:limit]:
                base = t['symbol'][:-4]
                volume = float(t['quoteVolume'])
                top_coins.append({'symbol': f"{base}/USDT", 'volume': volume})
                
            return top_coins
            
        except Exception as e:
            print(f"⚠️ System: Failed to fetch Top Volume Coins: {e}")
            return []

    def fetch_data_with_atr(self, symbol, timeframe='15m', limit=100):
        try:
            clean_symbol = symbol.replace('/', '')
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_symbol}&interval={timeframe}&limit={limit}"
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            if response.status_code != 200: return None
            bars = response.json()
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'v', 'ct', 'qv', 'nt', 'tb', 'tq', 'i'])
            
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'v']].astype(float)
            
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_PERIOD)
            
            return df
        except Exception as e:
            print(f"⚠️ System: Failed fetching {symbol} ({timeframe}): {e}")
            return None

    def get_active_positions(self):
        try:
            positions = self.client.fapiPrivateV2GetPositionRisk()
            active_symbols = []
            for pos in positions:
                if float(pos['positionAmt']) != 0:
                    symbol = pos['symbol']
                    if symbol.endswith('USDT'):
                        active_symbols.append(f"{symbol[:-4]}/USDT")
            return active_symbols
        except Exception as e:
            print(f"⚠️ System: Failed to fetch active positions: {e}")
            return []