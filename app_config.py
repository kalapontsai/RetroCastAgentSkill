"""app_config.py — 路徑 / 設定常數"""
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / 'data'
PRICE_CACHE_DIR = DATA_DIR / 'price_cache'
REPORTS_DIR = DATA_DIR / 'reports'
USER_PROFILE_DIR = ROOT_DIR / 'user_profile'
LOGS_DIR = ROOT_DIR / 'logs'
STATIC_DIR = ROOT_DIR / 'static'
TEMPLATES_DIR = ROOT_DIR / 'templates'

# 確保目錄存在
for p in (DATA_DIR, PRICE_CACHE_DIR, REPORTS_DIR, LOGS_DIR):
    p.mkdir(parents=True, exist_ok=True)

# 預設分析範圍
DEFAULT_N_YEARS = 10
DEFAULT_PV = 10_000_000
DEFAULT_START_DATE = '2000-01-01'  # FinMind 抓價的歷史起點
FINMIND_REQUEST_TIMEOUT = 30
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB CSV 上限
