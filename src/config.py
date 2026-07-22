import os
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# API KEYS & ENVIRONMENT
# ==========================================
API_KEY = os.getenv('BINANCE_API_KEY')
SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
MODE = os.getenv('BINANCE_MODE', 'testnet')

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ==========================================
# RISK MANAGEMENT SOP
# ==========================================
MAX_OPEN_POSITIONS = 10
RISK_PER_TRADE = 100.0  
MAX_MARGIN_ALLOWED = 200.0  

# ==========================================
# TECHNICAL SETTINGS
# ==========================================
TOP_COINS_LIMIT = 30
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5  

# ==========================================
# MEMORY & CLEANUP SETTINGS
# ==========================================
ACTIVE_POIS_FILE = "active_pois.json"
OB_AUDIT_FILE = "retracement_ob_audit.jsonl"
POI_UNTAPPED_TTL_SECONDS = int(os.getenv("POI_UNTAPPED_TTL_SECONDS", str(24 * 60 * 60)))
POI_TAPPED_TTL_SECONDS = int(os.getenv("POI_TAPPED_TTL_SECONDS", str(6 * 60 * 60)))
POI_MAX_DISTANCE_ATR_MULTIPLIER = float(os.getenv("POI_MAX_DISTANCE_ATR_MULTIPLIER", "3.0"))
MANUAL_ACTION_TTL_SECONDS = int(os.getenv("MANUAL_ACTION_TTL_SECONDS", str(10 * 60 * 60)))