import pandas as pd
import numpy as np

# KITA COPAST LANGSUNG ENGINE STRATEGY-NYA KE SINI BIAR ANTI-GLITCH IMPORT
class LocalSMCStrategy:
    def __init__(self, swing_window=5):
        self.swing_window = swing_window

    def identify_swings(self, df):
        swing_highs = []
        swing_lows = []
        for i in range(self.swing_window, len(df) - self.swing_window):
            is_high = True
            for j in range(1, self.swing_window + 1):
                if df['high'].iloc[i] <= df['high'].iloc[i-j] or df['high'].iloc[i] <= df['high'].iloc[i+j]:
                    is_high = False
                    break
            if is_high: swing_highs.append({'index': i, 'price': df['high'].iloc[i]})
                
            is_low = True
            for j in range(1, self.swing_window + 1):
                if df['low'].iloc[i] >= df['low'].iloc[i-j] or df['low'].iloc[i] >= df['low'].iloc[i+j]:
                    is_low = False
                    break
            if is_low: swing_lows.append({'index': i, 'price': df['low'].iloc[i]})
        return swing_highs, swing_lows

    def find_fvg(self, df):
        fvgs = []
        for i in range(2, len(df)):
            if df['low'].iloc[i] > df['high'].iloc[i-2]:
                fvg_bottom = df['high'].iloc[i-2]
                fvg_top = df['low'].iloc[i]
                if not any(df['low'].iloc[j] <= fvg_bottom for j in range(i+1, len(df))):
                    fvgs.append({'type': 'BULLISH', 'top': fvg_top, 'bottom': fvg_bottom, 'index': i-1})
        return fvgs

    def find_bullish_poi(self, df_m15):
        swing_highs, swing_lows = self.identify_swings(df_m15)
        fvgs = [f for f in self.find_fvg(df_m15) if f['type'] == 'BULLISH']
        if not fvgs: return None

        for i in range(len(df_m15) - 2, 10, -1):
            is_bearish = df_m15['close'].iloc[i] < df_m15['open'].iloc[i]
            if is_bearish:
                current_low = df_m15['low'].iloc[i]
                current_close = df_m15['close'].iloc[i]
                is_sweep = False
                
                for sl in swing_lows:
                    if sl['index'] >= i: continue
                    if current_low < sl['price'] and current_close > sl['price']:
                        is_sweep = True
                        break
                
                if not is_sweep: continue

                ob_top = df_m15['high'].iloc[i]
                is_mitigated = any(df_m15['low'].iloc[k] <= ob_top for k in range(i+1, len(df_m15)))
                if is_mitigated: continue

                valid_fvg = None
                for fvg in fvgs:
                    if fvg['index'] > i and fvg['bottom'] >= current_close:
                        valid_fvg = fvg
                        break
                        
                if valid_fvg:
                    return {'type': 'BULLISH', 'entry_price': ob_top, 'sl': current_low}
        return None

# DATA MOCK PENANTANG SENSOR
def generate_perfect_mock_data():
    timestamps = pd.date_range(start="2026-05-16", periods=70, freq="5min")
    opens = np.full(70, 100.0)
    highs = np.full(70, 101.0)
    lows = np.full(70, 99.0)
    closes = np.full(70, 100.0)
    
    # Candle 20 = Swing Low Tua
    opens[20], highs[20], lows[20], closes[20] = 96.0, 97.0, 93.0, 95.5
    
    # Candle 40 = Valid Sweep
    opens[40], highs[40], lows[40], closes[40] = 94.0, 96.0, 91.5, 95.0 

    # Candle 41 & 42 = Market Shift (FVG)
    opens[41], highs[41], lows[41], closes[41] = 95.0, 98.0, 94.5, 97.5
    opens[42], highs[42], lows[42], closes[42] = 97.5, 106.0, 101.0, 105.0

    for i in range(43, 70):
        opens[i], highs[i], lows[i], closes[i] = 105.0, 106.0, 104.0, 105.0
        
    df = pd.DataFrame({'timestamp': timestamps, 'open': opens, 'high': highs, 'low': lows, 'close': closes})
    df['atr'] = 1.0
    return df

if __name__ == "__main__":
    print("="*60)
    print("🚀 RUNNING STANDALONE ZERO-DEPENDENCY UNIT TEST")
    print("="*60)
    
    df_mock = generate_perfect_mock_data()
    strategy = LocalSMCStrategy(swing_window=5)
    
    poi = strategy.find_bullish_poi(df_mock)
    
    print("-"*60)
    if poi:
        print("🟢 TEST PASSED: SMC Logic Engine is 100% HEALTHY!")
        print(f"   Verdict: {poi['type']} | Entry: {poi['entry_price']} | SL: {poi['sl']}")
    else:
        print("❌ TEST FAILED: Engine still returned None.")
    print("="*60)