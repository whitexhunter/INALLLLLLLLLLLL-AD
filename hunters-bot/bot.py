import os
import json
import asyncio
import logging
import uuid
import time
import secrets
import hashlib
import base64
import threading
import socketserver
import http.server
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

from cryptography.fernet import Fernet

# Our pure selfbot client — no discord.py/discord.py-self/nextcord
from selfbot_client import SelfbotRESTClient

# ============================================================
# This bot uses a minimal HTTP server for health checks.
# It does NOT use discord.py at all for the selfbot portion.
# Instead, SelfbotRESTClient communicates with Discord's REST API
# directly via aiohttp, using a user token.
# ============================================================

# ============================================================
# CONFIGURATION
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('hunters-bot')

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY', '')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]
LTC_ADDRESS = os.environ.get('LTC_ADDRESS', 'Lxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
FERNET_KEY = os.environ.get('FERNET_KEY', '')

DATA_DIR = Path('data')
DATA_DIR.mkdir(exist_ok=True)

# File paths
USERS_FILE = DATA_DIR / 'users.json'
ACCOUNTS_FILE = DATA_DIR / 'accounts.json'
CAMPAIGNS_FILE = DATA_DIR / 'campaigns.json'
SUBSCRIPTIONS_FILE = DATA_DIR / 'subscriptions.json'
KEYS_FILE = DATA_DIR / 'keys.json'

# Plan configuration
PLAN_CONFIG = {
    'free':     {'max_accounts': 1,  'features': ['send_all_at_once']},
    'v1':       {'max_accounts': 1,  'features': ['send_all_at_once']},
    'v2':       {'max_accounts': 3,  'features': ['send_all_at_once', 'image_attachments']},
    'v3':       {'max_accounts': 5,  'features': ['send_all_at_once', 'image_attachments', 'auto_reply_dm']},
    'lifetime': {'max_accounts': 5,  'features': ['send_all_at_once', 'image_attachments', 'auto_reply_dm']},
}

PRICES = {
    'v1':       {'monthly': 1,   'lifetime': None},
    'v2':       {'monthly': 2,   'lifetime': None},
    'v3':       {'monthly': 3,   'lifetime': None},
    'lifetime': {'monthly': 0,   'lifetime': 30},
}

# ============================================================
# FILE-BASED STORAGE
# ============================================================
def _load_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def _save_json(path: Path, data: Any):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)

def get_users() -> Dict[str, dict]:
    return _load_json(USERS_FILE)

def save_users(users: dict):
    _save_json(USERS_FILE, users)

def get_accounts() -> Dict[str, dict]:
    return _load_json(ACCOUNTS_FILE)

def save_accounts(accounts: dict):
    _save_json(ACCOUNTS_FILE, accounts)

def get_campaigns() -> Dict[str, dict]:
    return _load_json(CAMPAIGNS_FILE)

def save_campaigns(campaigns: dict):
    _save_json(CAMPAIGNS_FILE, campaigns)

def get_subscriptions() -> Dict[str, dict]:
    return _load_json(SUBSCRIPTIONS_FILE)

def save_subscriptions(subs: dict):
    _save_json(SUBSCRIPTIONS_FILE, subs)

def get_keys() -> Dict[str, dict]:
    return _load_json(KEYS_FILE)

def save_keys(keys: dict):
    _save_json(KEYS_FILE, keys)

def next_id() -> str:
    return str(uuid.uuid4())[:8]

# ============================================================
# ENCRYPTION (Fernet) — SAME AS ORIGINAL
# ============================================================
def get_cipher() -> Fernet:
    key = FERNET_KEY
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(ENCRYPTION_KEY.encode()).digest())
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key if isinstance(key, bytes) else key)

def encrypt_token(token: str) -> str:
    return get_cipher().encrypt(token.encode()).decode()

def decrypt_token(encrypted: str) -> str:
    try:
        return get_cipher().decrypt(encrypted.encode()).decode()
    except Exception:
        return ''

# ============================================================
# ── SIMPLE HTTP SERVER (replaces discord.py's bot.run)
# We serve:
#   /health          → JSON health check
#   /webhook         → incoming interaction endpoints
#   /               → HTML admin panel
# ============================================================

# We'll store pending interaction responses in a queue
# Discord sends interactions to our Interactions Endpoint URL
# This replaces discord.py's slash command handling entirely.

import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

# Global asyncio loop reference
_loop: asyncio.AbstractEventLoop = None

# Pending ephemeral message queue: { interaction_token: response_data }
_pending_responses: Dict[str, dict] = {}

# Active user
