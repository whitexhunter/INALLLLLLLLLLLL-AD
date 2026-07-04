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

import aiohttp

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

import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

# Global asyncio loop reference
_loop: asyncio.AbstractEventLoop = None

# Pending ephemeral message queue: { interaction_token: response_data }
_pending_responses: Dict[str, dict] = {}

# Active user sessions
_user_sessions: Dict[str, dict] = {}

# ── FIX: Store the bot's application ID (fetched at startup) ──
APPLICATION_ID = ''

class InteractionHandler(BaseHTTPRequestHandler):
    """Handles Discord Interactions (slash commands, buttons, modals) directly.
    
    This replaces discord.py's entire interaction handling system.
    Discord sends POST requests to our public URL with interaction data.
    """
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'ok',
                'service': 'hunters-bot',
                'version': 'pure-selfbot-v2'
            }).encode())
        
        elif parsed.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            html = '''
            <html>
            <head><title>Hunters Auto Bot</title>
            <style>
                body { font-family: Arial; background: #1a1a2e; color: #eee; padding: 40px; text-align: center; }
                h1 { color: #5865F2; }
                .status { color: #57F287; }
            </style>
            </head>
            <body>
                <h1>🚀 Hunters Auto Bot</h1>
                <p class="status">● Running (Pure Selfbot Engine)</p>
                <p>This is a Discord bot. Use /panel in DMs to get started.</p>
            </body>
            </html>
            '''
            self.wfile.write(html.encode())
        
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        """Handle incoming Discord interactions."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != '/interactions':
            self.send_response(404)
            self.end_headers()
            return
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            interaction = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        
        # Handle Ping (Discord's verification)
        if interaction.get('type') == 1:  # PING
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'type': 1}).encode())  # PONG
            return
        
        # Schedule the interaction handling in the asyncio loop
        asyncio.run_coroutine_threadsafe(
            handle_interaction(interaction),
            _loop
        )
        
        # Acknowledge immediately
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'type': 5}).encode())  # DEFERRED_UPDATE_MESSAGE
    
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def run_http_server():
    """Run the HTTP server in a background thread."""
    port = int(os.environ.get('PORT', 10000))
    server = socketserver.TCPServer(('0.0.0.0', port), InteractionHandler)
    log.info(f'HTTP+Interaction server running on port {port}')
    server.serve_forever()


# ============================================================
# ── DISCORD INTERACTION HANDLER (replaces discord.py slash commands)
# ============================================================

# ── FIX: Use APPLICATION_ID global instead of BOT_TOKEN.split(".")[0] ──

async def send_interaction_response(token: str, response_data: dict, is_ephemeral: bool = False):
    """Send a followup to a Discord interaction using the webhook."""
    global APPLICATION_ID
    if is_ephemeral:
        if 'flags' not in response_data:
            response_data['flags'] = 64  # EPHEMERAL
    
    async with aiohttp.ClientSession() as session:
        # FIX: Use APPLICATION_ID instead of BOT_TOKEN.split(".")[0]
        url = f'https://discord.com/api/v10/webhooks/{APPLICATION_ID}/{token}/messages/@original'
        async with session.patch(url, json=response_data) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"Failed to send interaction response: {resp.status} {text}")


async def send_interaction_followup(token: str, response_data: dict, is_ephemeral: bool = False):
    """Send a followup message to a Discord interaction."""
    global APPLICATION_ID
    if is_ephemeral:
        if 'flags' not in response_data:
            response_data['flags'] = 64
    
    async with aiohttp.ClientSession() as session:
        # FIX: Use APPLICATION_ID instead of BOT_TOKEN.split(".")[0]
        url = f'https://discord.com/api/v10/webhooks/{APPLICATION_ID}/{token}'
        async with session.post(url, json=response_data) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"Failed to send followup: {resp.status} {text}")


async def edit_original_response(token: str, embed: dict = None, content: str = None, components: list = None):
    """Edit the original interaction response."""
    global APPLICATION_ID
    payload = {}
    if content:
        payload['content'] = content
    if embed:
        payload['embeds'] = [embed] if isinstance(embed, dict) else embed
    if components:
        payload['components'] = components
    
    async with aiohttp.ClientSession() as session:
        # FIX: Use APPLICATION_ID instead of BOT_TOKEN.split(".")[0]
        url = f'https://discord.com/api/v10/webhooks/{APPLICATION_ID}/{token}/messages/@original'
        async with session.patch(url, json=payload) as resp:
            return resp.status


async def handle_interaction(interaction: dict):
    """Main interaction dispatcher — replaces all discord.py @tree.command decorators."""
    try:
        interaction_type = interaction.get('type', 0)
        data = interaction.get('data', {})
        token = interaction.get('token', '')
        member = interaction.get('member', {})
        user = member.get('user', interaction.get('user', {}))
        user_id = str(user.get('id', ''))
        username = user.get('username', 'Unknown')
        
        # Make sure user exists in our system
        await ensure_user_raw(user_id, username)
        
        if interaction_type == 2:  # APPLICATION_COMMAND
            command_name = data.get('name', '')
            
            if command_name == 'panel':
                await handle_panel_command(interaction, user_id, username, token)
            
            elif command_name == 'admin':
                await handle_admin_command(interaction, user_id, username, token)
            
            else:
                await send_interaction_response(token, {
                    'content': f'❌ Unknown command: {command_name}'
                }, is_ephemeral=True)
        
        elif interaction_type == 3:  # MESSAGE_COMPONENT (button clicks)
            custom_id = data.get('custom_id', '')
            await handle_component_interaction(interaction, user_id, username, token, custom_id)
        
        elif interaction_type == 5:  # MODAL_SUBMIT
            custom_id = data.get('custom_id', '')
            await handle_modal_submit(interaction, user_id, username, token, custom_id)
    
    except Exception as e:
        log.error(f"Error handling interaction: {e}", exc_info=True)
        try:
            await send_interaction_response(interaction.get('token', ''), {
                'content': f'❌ An error occurred: {str(e)}'
            }, is_ephemeral=True)
        except:
            pass


# ============================================================
# ── USER MANAGEMENT (same as original)
# ============================================================

async def ensure_user_raw(user_id: str, username: str) -> dict:
    """Create user if not exists, return user dict."""
    users = get_users()
    if user_id not in users:
        users[user_id] = {
            'discord_id': user_id,
            'username': username,
            'plan': 'free',
            'max_accounts': 1,
            'is_trial_used': False,
            'trial_expires_at': None,
            'subscription_expires_at': None,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
        }
        save_users(users)
    else:
        if users[user_id].get('username') != username:
            users[user_id]['username'] = username
            users[user_id]['updated_at'] = datetime.utcnow().isoformat()
            save_users(users)
    return users.get(user_id, {})


async def check_plan_expiry(user_id: str):
    """Check and downgrade expired plans."""
    users = get_users()
    if user_id not in users:
        return
    user = users[user_id]
    now = datetime.utcnow()
    
    # Trial expiry
    if user.get('trial_expires_at'):
        try:
            if datetime.fromisoformat(user['trial_expires_at']) < now:
                if user['plan'] == 'v3' and user.get('is_trial_used') and not user.get('subscription_expires_at'):
                    user['plan'] = 'free'
                    user['max_accounts'] = 1
                    log.info(f"User {user_id} trial expired, downgraded to free")
        except:
            pass
    
    # Subscription expiry
    if user.get('subscription_expires_at'):
        try:
            if datetime.fromisoformat(user['subscription_expires_at']) < now:
                if user['plan'] != 'free' and user['plan'] != 'lifetime':
                    user['plan'] = 'free'
                    user['max_accounts'] = 1
                    user['subscription_expires_at'] = None
                    log.info(f"User {user_id} subscription expired, downgraded to free")
        except:
            pass
    
    save_users(users)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_user_accounts(user_id: str) -> Dict[str, dict]:
    accounts = get_accounts()
    return {k: v for k, v in accounts.items() if v.get('user_id') == user_id}


def get_user_campaigns(user_id: str) -> Dict[str, dict]:
    campaigns = get_campaigns()
    return {k: v for k, v in campaigns.items() if v.get('user_id') == user_id}


# ============================================================
# ── EMBED BUILDER (works without discord.py)
# ============================================================

def make_embed_dict(title: str, description: str = '', color: int = 0x5865F2, fields: list = None, footer: str = None) -> dict:
    """Build a Discord embed dict (no discord.py needed)."""
    embed = {
        'title': title,
        'description': description,
        'color': color,
        'timestamp': datetime.utcnow().isoformat()
    }
    if fields:
        embed['fields'] = []
        for name, value, inline in fields:
            embed['fields'].append({
                'name': str(name),
                'value': str(value),
                'inline': inline
            })
    if footer:
        embed['footer'] = {'text': footer}
    else:
        embed['footer'] = {'text': 'Hunters Auto | DM Only'}
    return embed


# ============================================================
# ── COMPONENT BUILDERS (replaces discord.ui components)
# ============================================================

def make_button(custom_id: str, label: str, style: int = 2, disabled: bool = False, emoji: dict = None) -> dict:
    """Create a button component.
    Styles: 1=primary(blurple), 2=secondary(gray), 3=success(green), 4=danger(red)
    """
    btn = {
        'type': 2,  # Button
        'style': style,
        'label': label,
        'custom_id': custom_id,
        'disabled': disabled
    }
    if emoji:
        btn['emoji'] = emoji
    return btn


def make_action_row(*components) -> dict:
    """Create an action row containing components."""
    return {
        'type': 1,  # Action Row
        'components': list(components)
    }


def make_text_input(custom_id: str, label: str, style: int = 1, placeholder: str = '',
                    required: bool = True, value: str = '', max_length: int = 4000,
                    min_length: int = 0) -> dict:
    """Create a text input for modals.
    style: 1=short, 2=paragraph
    """
    return {
        'type': 4,  # Text Input
        'custom_id': custom_id,
        'label': label,
        'style': style,
        'placeholder': placeholder,
        'required': required,
        'value': value,
        'max_length': max_length,
        'min_length': min_length
    }


# ============================================================
# ── PANEL COMMAND HANDLER (/panel)
# ============================================================

async def handle_panel_command(interaction: dict, user_id: str, username: str, token: str):
    """Handle /panel command — same layout as original."""
    await check_plan_expiry(user_id)
    user = get_users().get(user_id, {})
    user_accounts = get_user_accounts(user_id)
    user_campaigns = get_user_campaigns(user_id)
    running = sum(1 for c in user_campaigns.values() if c.get('status') == 'running')
    
    embed = make_embed_dict(
        '🏠 Hunters Auto - Panel',
        f'Welcome **{username}**!\nSelect an option below.',
        color=0x5865F2,
        fields=[
            ('👤 Your Plan', f'`{user.get("plan", "free").upper()}`', True),
            ('📊 Accounts', f'{len(user_accounts)}/{user.get("max_accounts", 1)}', True),
            ('📨 Campaigns', f'{len(user_campaigns)} (🟢 {running} running)', True),
        ]
    )
    
    components = [
        make_action_row(
            make_button('panel_dashboard', '📊 Dashboard', style=1, emoji={'name': '📊'}),
            make_button('panel_accounts', '👤 My Accounts', style=2, emoji={'name': '👤'}),
            make_button('panel_campaigns', '📨 My Campaigns', style=2, emoji={'name': '📨'}),
        ),
        make_action_row(
            make_button('panel_add_account', '➕ Add Account', style=3, emoji={'name': '➕'}),
            make_button('panel_new_campaign', '🚀 New Campaign', style=3, emoji={'name': '🚀'}),
            make_button('panel_plans', '💰 Plans & Buy', style=4, emoji={'name': '💰'}),
        ),
        make_action_row(
            make_button('panel_refresh', '🔄 Refresh', style=2, emoji={'name': '🔄'}),
        )
    ]
    
    await send_interaction_response(token, {
        'embeds': [embed],
        'components': components
    })


async def handle_admin_command(interaction: dict, user_id: str, username: str, token: str):
    """Handle /admin command — same layout as original."""
    if not is_admin(int(user_id)):
        await send_interaction_response(token, {
            'content': '❌ You are not authorized.'
        }, is_ephemeral=True)
        return
    
    users = get_users()
    accounts = get_accounts()
    campaigns = get_campaigns()
    running = sum(1 for c in campaigns.values() if c.get('status') == 'running')
    revenue = sum(s.get('amount', 0) for s in get_subscriptions().values() if s.get('status') == 'confirmed')
    
    embed = make_embed_dict(
        '🛡️ Admin Panel',
        'Full control over the Hunters Auto system.',
        color=0xED4245,
        fields=[
            ('👥 Users', f'{len(users)}', True),
            ('👤 Accounts', f'{len(accounts)}', True),
            ('📨 Campaigns', f'{len(campaigns)} (🟢 {running})', True),
            ('💰 Revenue', f'{revenue} LTC', True),
        ],
        footer=f'Admin: {username}'
    )
    
    components = [
        make_action_row(
            make_button('admin_overview', '📊 Overview', style=1),
            make_button('admin_users', '👥 Users', style=2),
            make_button('admin_genkey', '🔑 Generate Key', style=3),
        ),
        make_action_row(
            make_button('admin_campaigns', '📨 All Campaigns', style=2),
            make_button('admin_revenue', '💰 Revenue', style=4),
            make_button('admin_system', '⚙️ System', style=2),
        ),
        make_action_row(
            make_button('admin_refresh', '🔄 Refresh', style=2),
        )
    ]
    
    await send_interaction_response(token, {
        'embeds': [embed],
        'components': components
    })


# ============================================================
# ── COMPONENT INTERACTION HANDLER (button clicks)
# ============================================================

async def handle_component_interaction(interaction: dict, user_id: str, username: str, 
                                        token: str, custom_id: str):
    """Handle button clicks — replaces all @discord.ui.button decorators."""
    
    # ── Panel buttons ──
    if custom_id == 'panel_refresh':
        await handle_panel_command(interaction, user_id, username, token)
    
    elif custom_id == 'panel_dashboard':
        await show_dashboard(user_id, username, token)
    
    elif custom_id == 'panel_accounts':
        await show_accounts(user_id, token)
    
    elif custom_id == 'panel_campaigns':
        await show_campaigns(user_id, token)
    
    elif custom_id == 'panel_add_account':
        await show_add_account_modal(user_id, token)
    
    elif custom_id == 'panel_new_campaign':
        await show_new_campaign_modal(user_id, token)
    
    elif custom_id == 'panel_plans':
        await show_plans(user_id, token)
    
    # ── Account management ──
    elif custom_id.startswith('account_delete_'):
        acc_id = custom_id.replace('account_delete_', '')
        await delete_account(user_id, acc_id, token)
    
    elif custom_id.startswith('account_view_'):
        acc_id = custom_id.replace('account_view_', '')
        await view_account(user_id, acc_id, token)
    
    # ── Campaign management ──
    elif custom_id.startswith('campaign_start_'):
        cid = custom_id.replace('campaign_start_', '')
        await toggle_campaign(user_id, cid, 'running', token)
    
    elif custom_id.startswith('campaign_pause_'):
        cid = custom_id.replace('campaign_pause_', '')
        await toggle_campaign(user_id, cid, 'paused', token)
    
    elif custom_id.startswith('campaign_delete_'):
        cid = custom_id.replace('campaign_delete_', '')
        await delete_campaign(user_id, cid, token)
    
    elif custom_id.startswith('campaign_view_'):
        cid = custom_id.replace('campaign_view_', '')
        await view_campaign(user_id, cid, token)
    
    # ── Plans ──
    elif custom_id.startswith('plan_buy_'):
        plan = custom_id.replace('plan_buy_', '')
        await generate_payment(user_id, plan, token)
    
    elif custom_id.startswith('pay_confirm_'):
        sub_id = custom_id.replace('pay_confirm_', '')
        await confirm_payment(user_id, sub_id, token)
    
    elif custom_id == 'trial_start':
        await start_trial(user_id, token)
    
    # ── Redeem key ──
    elif custom_id == 'redeem_key':
        await show_redeem_modal(user_id, token)
    
    # ── Admin buttons ──
    elif custom_id == 'admin_overview':
        await show_admin_overview(token)
    elif custom_id == 'admin_users':
        await show_admin_users(token)
    elif custom_id == 'admin_genkey':
        await show_genkey_modal_admin(token)
    elif custom_id == 'admin_campaigns':
        await show_admin_campaigns(token)
    elif custom_id == 'admin_revenue':
        await show_admin_revenue(token)
    elif custom_id == 'admin_system':
        await show_admin_system(token)
    elif custom_id == 'admin_refresh':
        await handle_admin_command(interaction, user_id, username, token)
    
    # ── Back buttons ──
    elif custom_id == 'back_to_panel':
        await handle_panel_command(interaction, user_id, username, token)
    elif custom_id == 'back_to_admin':
        await handle_admin_command(interaction, user_id, username, token)
    
    else:
        log.warning(f"Unknown custom_id: {custom_id}")
        await send_interaction_response(token, {
            'content': '❌ Unknown button.'
        }, is_ephemeral=True)


# ============================================================
# ── MODAL SUBMIT HANDLER
# ============================================================

async def handle_modal_submit(interaction: dict, user_id: str, username: str,
                               token: str, custom_id: str):
    """Handle modal submissions — replaces discord.ui.Modal.on_submit."""
    components = interaction.get('data', {}).get('components', [])
    
    def get_modal_value(components_list, target_custom_id: str) -> str:
        """Extract a value from modal components by custom_id."""
        for row in components_list:
            for comp in row.get('components', []):
                if comp.get('custom_id') == target_custom_id:
                    return comp.get('value', '')
        return ''
    
    if custom_id == 'modal_add_account':
        token_value = get_modal_value(components, 'account_token')
        if token_value:
            await add_account_submit(user_id, token_value, token)
        else:
            await send_interaction_response(token, {
                'content': '❌ Token is required.'
            }, is_ephemeral=True)
    
    elif custom_id == 'modal_new_campaign':
        name = get_modal_value(components, 'campaign_name')
        ctype = get_modal_value(components, 'campaign_type')
        acc_id = get_modal_value(components, 'campaign_account')
        channels = get_modal_value(components, 'campaign_channels')
        messages = get_modal_value(components, 'campaign_messages')
        await create_campaign_submit(user_id, name, ctype, acc_id, channels, messages, token)
    
    elif custom_id == 'modal_redeem_key':
        key = get_modal_value(components, 'license_key')
        await redeem_key_submit(user_id, key, token)
    
    elif custom_id == 'modal_genkey':
        plan = get_modal_value(components, 'genkey_plan')
        count_str = get_modal_value(components, 'genkey_count')
        await genkey_submit(user_id, plan, count_str, token)


# ============================================================
# ── PANEL VIEW FUNCTIONS (same as original, but HTTP API based)
# ============================================================

async def show_dashboard(user_id: str, username: str, token: str):
    """Show dashboard — same as original."""
    await check_plan_expiry(user_id)
    user = get_users().get(user_id, {})
    user_accounts = get_user_accounts(user_id)
    user_campaigns = get_user_campaigns(user_id)
    
    total_sent = sum(c.get('stats', {}).get('sent', 0) for c in user_campaigns.values())
    total_failed = sum(c.get('stats', {}).get('failed', 0) for c in user_campaigns.values())
    total_replied = sum(c.get('stats', {}).get('replied', 0) for c in user_campaigns.values())
    running = sum(1 for c in user_campaigns.values() if c.get('status') == 'running')
    
    embed = make_embed_dict(
        f'📊 Dashboard - {username}',
        'Your complete statistics at a glance.',
        color=0x5865F2,
        fields=[
            ('Plan', f'`{user.get("plan", "free").upper()}`', True),
            ('Accounts', f'{len(user_accounts)} / {user.get("max_accounts", 1)}', True),
            ('Campaigns', f'{len(user_campaigns)}', True),
            ('🟢 Running', f'{running}', True),
            ('✅ Sent', f'{total_sent}', True),
            ('❌ Failed', f'{total_failed}', True),
            ('💬 Replied', f'{total_replied}', True),
            ('Trial Used', '✅ Yes' if user.get('is_trial_used') else '❌ No', False),
        ]
    )
    
    components = [make_action_row(
        make_button('back_to_panel', '🔙 Back to Panel', style=2)
    )]
    
    await edit_original_response(token, embed=embed, components=components)


async def show_accounts(user_id: str, token: str):
    """Show accounts list — same as original."""
    user_accounts = get_user_accounts(user_id)
    
    if not user_accounts:
        embed = make_embed_dict('👤 My Accounts', 'No accounts added yet.', color=0xED4245)
        components = [
            make_action_row(
                make_button('panel_add_account', '➕ Add Account', style=3),
                make_button('back_to_panel', '🔙 Back', style=2),
            )
        ]
        await edit_original_response(token, embed=embed, components=components)
        return
    
    desc_lines = []
    buttons_row = []
    for aid, acc in list(user_accounts.items())[:10]:
        status = '🟢 Active' if acc.get('status') == 'active' else '🔴 Inactive'
        desc_lines.append(f'**`{aid}`** {status} — {acc.get("username", "Unknown")}')
        buttons_row.append(
            make_button(f'account_view_{aid}', f'{aid[:4]}..', style=2)
        )
    
    embed = make_embed_dict(
        f'👤 My Accounts ({len(user_accounts)})',
        '\n'.join(desc_lines) if desc_lines else 'No accounts.',
        color=0x5865F2
    )
    
    components = [
        make_action_row(*buttons_row[:5]) if buttons_row else make_action_row(),
        make_action_row(
            make_button('panel_add_account', '➕ Add Account', style=3),
            make_button('back_to_panel', '🔙 Back', style=2),
        )
    ]
    
    await edit_original_response(token, embed=embed, components=components)


async def view_account(user_id: str, acc_id: str, token: str):
    """View a single account's details."""
    accounts = get_accounts()
    acc = accounts.get(acc_id)
    if not acc or acc.get('user_id') != user_id:
        await edit_original_response(token, content='❌ Account not found.', components=[])
        return
    
    embed = make_embed_dict(
        f'👤 Account: {acc.get("username", "Unknown")}',
        f'**ID:** `{acc_id}`\n**Email:** {acc.get("email", "N/A")}\n**Status:** {acc.get("status", "active")}\n**Created:** {acc.get("created_at", "N/A")}',
        color=0x5865F2
    )
    
    components = [
        make_action_row(
            make_button(f'account_delete_{acc_id}', '🗑 Delete', style=4),
            make_button('panel_accounts', '🔙 Back', style=2),
        )
    ]
    
    await edit_original_response(token, embed=embed, components=components)


async def delete_account(user_id: str, acc_id: str, token: str):
    """Delete an account."""
    accounts = get_accounts()
    if acc_id in accounts and accounts[acc_id].get('user_id') == user_id:
        del accounts[acc_id]
        save_accounts(accounts)
        await edit_original_response(token, content='✅ Account deleted.', components=[])
    else:
        await edit_original_response(token, content='❌ Account not found.', components=[])


async def show_campaigns(user_id: str, token: str):
    """Show campaigns list — same as original."""
    user_campaigns = get_user_campaigns(user_id)
    
    if not user_campaigns:
        embed = make_embed_dict('📨 My Campaigns', 'No campaigns yet. Create one!', color=0xED4245)
        components = [
            make_action_row(
                make_button('panel_new_campaign', '🚀 New Campaign', style=3),
                make_button('back_to_panel', '🔙 Back', style=2),
            )
        ]
        await edit_original_response(token, embed=embed, components=components)
        return
    
    desc_lines = []
    buttons_row = []
    for cid, camp in list(user_campaigns.items())[:10]:
        status_map = {'running': '🟢', 'paused': '⏸', 'completed': '✅', 'failed': '❌'}
        s = status_map.get(camp.get('status', 'paused'), '⏸')
        st = camp.get('stats', {})
        desc_lines.append(f'**`{cid}`** {s} **{camp.get("name", "Unnamed")}** — {camp.get("type", "?")} | Sent: {st.get("sent", 0)}')
        buttons_row.append(
            make_button(f'campaign_view_{cid}', f'{cid[:4]}..', style=2)
        )
    
    embed = make_embed_dict(
        f'📨 My Campaigns ({len(user_campaigns)})',
        '\n'.join(desc_lines) if desc_lines else 'No campaigns.',
        color=0x5865F2
    )
    
    components = [
        make_action_row(*buttons_row[:5]) if buttons_row else make_action_row(),
        make_action_row(
            make_button('panel_new_campaign', '🚀 New Campaign', style=3),
            make_button('back_to_panel', '🔙 Back', style=2),
        )
    ]
    
    await edit_original_response(token, embed=embed, components=components)


async def view_campaign(user_id: str, cid: str, token: str):
    """View a single campaign's details with start/pause/delete."""
    campaigns = get_campaigns()
    camp = campaigns.get(cid)
    if not camp or camp.get('user_id') != user_id:
        await edit_original_response(token, content='❌ Campaign not found.', components=[])
        return
    
    st = camp.get('stats', {})
    status = camp.get('status', 'paused')
    status_emoji = {'running': '🟢', 'paused': '⏸', 'completed': '✅'}.get(status, '⏸')
    
    embed = make_embed_dict(
        f'{status_emoji} Campaign: {camp.get("name", "Unnamed")}',
        f'**Type:** {camp.get("type", "?")}\n**Status:** {status}\n**Account ID:** {camp.get("account_id", "N/A")}\n**Channels:** {len(camp.get("channels", []))}\n**Messages:** {len(camp.get("messages", []))}',
        color=0x5865F2,
        fields=[
            ('✅ Sent', str(st.get('sent', 0)), True),
            ('❌ Failed', str(st.get('failed', 0)), True),
            ('💬 Replied', str(st.get('replied', 0)), True),
        ]
    )
    
    components = [
        make_action_row(
            make_button(f'campaign_start_{cid}', '▶ Start', style=3),
            make_button(f'campaign_pause_{cid}', '⏸ Pause', style=2),
            make_button(f'campaign_delete_{cid}', '🗑 Delete', style=4),
        ),
        make_action_row(
            make_button('panel_campaigns', '🔙 Back', style=2),
        )
    ]
    
    await edit_original_response(token, embed=embed, components=components)


async def toggle_campaign(user_id: str, cid: str, new_status: str, token: str):
    """Start/pause a campaign."""
    campaigns = get_campaigns()
    if cid not in campaigns or campaigns[cid].get('user_id') != user_id:
        await edit_original_response(token, content='❌ Campaign not found.', components=[])
        return
    
    campaigns[cid]['status'] = new_status
    save_campaigns(campaigns)
    
    status_emoji = {'running': '▶', 'paused': '⏸'}.get(new_status, '⏸')
    await edit_original_response(token, content=f'{status_emoji} Campaign **{new_status.title()}**!', components=[])


async def delete_campaign(user_id: str, cid: str, token: str):
    """Delete a campaign."""
    campaigns = get_campaigns()
    if cid in campaigns and campaigns[cid].get('user_id') == user_id:
        del campaigns[cid]
        save_campaigns(campaigns)
        await edit_original_response(token, content='✅ Campaign deleted.', components=[])
    else:
        await edit_original_response(token, content='❌ Campaign not found.', components=[])


# ============================================================
# ── MODAL TRIGGER FUNCTIONS
# ============================================================

async def show_add_account_modal(user_id: str, token: str):
    """Show add account modal — same as original."""
    await send_interaction_followup(token, {
        'content': '📝 **Add a Discord Account**\n\nPaste your Discord user token below. It will be encrypted and stored securely.',
        'components': [make_action_row(
            make_button('_trigger_add_account', '➕ Open Form', style=3)
        )]
    }, is_ephemeral=True)


async def show_new_campaign_modal(user_id: str, token: str):
    """Show new campaign modal — same fields as original."""
    user = get_users().get(user_id, {})
    user_accounts = get_user_accounts(user_id)
    
    if not user_accounts:
        await send_interaction_followup(token, {
            'content': '❌ You need to add an account first! Use the **Add Account** button.'
        }, is_ephemeral=True)
        return
    
    account_list = '\n'.join([f'`{aid}` - {acc.get("username", "Unknown")}' for aid, acc in user_accounts.items()])
    
    await send_interaction_followup(token, {
        'content': f'📝 **Create New Campaign**\n\nYour accounts:\n{account_list}\n\nUse:\n`/campaign_create <name> <type> <account_id> <channels> <messages>`\n\nExample:\n`/campaign_create MyCamp channel_messaging abc123 123456,789012 Hello!|How are you?`'
    }, is_ephemeral=True)


async def show_plans(user_id: str, token: str):
    """Show plans and pricing — same as original."""
    user = get_users().get(user_id, {})
    
    embed = make_embed_dict(
        '💰 Plans & Pricing',
        f'Your current plan: **{user.get("plan", "free").upper()}**\n\nChoose a plan below:',
        color=0x5865F2,
        fields=[
            ('💎 V1 - $1/month', '1 account\nSend all at once', True),
            ('💎 V2 - $2/month', '3 accounts\nImage attachments', True),
            ('💎 V3 - $3/month', '5 accounts\nDM Auto-Reply\nImage attachments', True),
            ('🔥 Lifetime - $30', 'All features forever\nNo monthly payment', True),
        ]
    )
    
    components = [
        make_action_row(
            make_button('plan_buy_v1', '💎 V1 - $1/mo', style=1),
            make_button('plan_buy_v2', '💎 V2 - $2/mo', style=1),
            make_button('plan_buy_v3', '💎 V3 - $3/mo', style=1),
        ),
        make_action_row(
            make_button('plan_buy_lifetime', '🔥 Lifetime - $30', style=4),
            make_button('trial_start', '🎁 Free Trial (10min V3)', style=3),
        ),
        make_action_row(
            make_button('redeem_key', '🎟 Redeem Key', style=2),
            make_button('back_to_panel', '🔙 Back', style=2),
        )
    ]
    
    await edit_original_response(token, embed=embed, components=components)


async def generate_payment(user_id: str, plan: str, token: str):
    """Generate payment request — same LTC flow as original."""
    price = PRICES.get(plan, {})
    amount = price.get('lifetime') if plan == 'lifetime' else price.get('monthly')
    
    sub_id = next_id()
    subs = get_subscriptions()
    subs[sub_id] = {
        'id': sub_id,
        'user_id': user_id,
        'plan': plan,
        'amount': amount,
        'ltc_address': LTC_ADDRESS,
        'status': 'pending',
        'created_at': datetime.utcnow().isoformat(),
        'expires_at': (datetime.utcnow() + timedelta(hours=2)).isoformat(),
    }
    save_subscriptions(subs)
    
    embed = make_embed_dict(
        '💳 Payment Required',
        f'Plan: **{plan.upper()}**\nAmount: **{amount} LTC**\n\nSend exactly **{amount} LTC** to:\n`{LTC_ADDRESS}`\n\nThen click **I Have Paid** below.',
        color=0xFEE75C,
        fields=[('Subscription ID', f'`{sub_id}`', False)]
    )
    
    components = [
        make_action_row(
            make_button(f'pay_confirm_{sub_id}', '✅ I Have Paid', style=3),
            make_button('panel_plans', '🔙 Back', style=2),
        )
    ]
    
    await edit_original_response(token, embed=embed, components=components)


async def confirm_payment(user_id: str, sub_id: str, token: str):
    """Confirm payment and upgrade user."""
    subs = get_subscriptions()
    sub = subs.get(sub_id)
    if not sub:
        await edit_original_response(token, content='❌ Payment session expired. Use /panel to try again.', components=[])
        return
    
    plan = sub.get('plan', '')
    sub['status'] = 'confirmed'
    sub['confirmed_at'] = datetime.utcnow().isoformat()
    save_subscriptions(subs)
    
    # Upgrade user
    users = get_users()
    user = users.get(user_id)
    if user:
        if plan == 'lifetime':
            user['plan'] = 'lifetime'
            user['max_accounts'] = 5
            user['subscription_expires_at'] = None
        else:
            user['plan'] = plan
            user['max_accounts'] = PLAN_CONFIG[plan]['max_accounts']
            user['subscription_expires_at'] = (datetime.utcnow() + timedelta(days=30)).isoformat()
        user['updated_at'] = datetime.utcnow().isoformat()
        save_users(users)
    
    embed = make_embed_dict(
        '✅ Payment Verified!',
        f'Your plan has been upgraded to **{plan.upper()}**!',
        color=0x57F287
    )
    await edit_original_response(token, embed=embed, components=[])


async def start_trial(user_id: str, token: str):
    """Start free trial — same as original (10min V3)."""
    users = get_users()
    user = users.get(user_id)
    
    if not user:
        await edit_original_response(token, content='❌ Register first with /panel', components=[])
        return
    
    if user.get('is_trial_used'):
        await edit_original_response(token, content='❌ You already used your free trial!', components=[])
        return
    
    user['is_trial_used'] = True
    user['plan'] = 'v3'
    user['max_accounts'] = 5
    user['trial_expires_at'] = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
    user['updated_at'] = datetime.utcnow().isoformat()
    save_users(users)
    
    sub_id = next_id()
    subs = get_subscriptions()
    subs[sub_id] = {
        'id': sub_id,
        'user_id': user_id,
        'plan': 'v3',
        'type': 'free_trial',
        'amount': 0,
        'status': 'confirmed',
        'created_at': datetime.utcnow().isoformat(),
    }
    save_subscriptions(subs)
    
    embed = make_embed_dict(
        '🎁 Trial Activated!',
        'You now have **V3** plan for **10 minutes**!\nEnjoy all features: 5 accounts, DM auto-reply, image attachments.',
        color=0x57F287
    )
    await edit_original_response(token, embed=embed, components=[])


async def show_redeem_modal(user_id: str, token: str):
    """Show redeem key modal."""
    await send_interaction_followup(token, {
        'content': '🎟 **Redeem License Key**\n\nSend your key using:\n`/redeem HUNTER-XXXX-XXXX-XXXX`'
    }, is_ephemeral=True)


# ============================================================
# ── ACCOUNT & CAMPAIGN SUBMISSIONS
# ============================================================

async def add_account_submit(user_id: str, token_value: str, response_token: str):
    """Process account addition — validates token, stores encrypted."""
    users = get_users()
    user = users.get(user_id, {})
    user_accounts = get_user_accounts(user_id)
    max_acc = user.get('max_accounts', 1)
    
    if len(user_accounts) >= max_acc:
        await edit_original_response(response_token,
            content=f'❌ You reached your plan limit ({max_acc} accounts). Upgrade to add more!',
            components=[])
        return
    
    client = SelfbotRESTClient(token_value)
    valid, me = await client.validate()
    await client.close()
    
    if not valid:
        await edit_original_response(response_token,
            content='❌ Invalid Discord token. Please check and try again.',
            components=[])
        return
    
    email = ''
    username = 'Token Account'
    try:
        parts = token_value.split('.')
        if len(parts) == 3:
            payload_b64 = parts[1]
            payload_b64 += '=' * (4 - len(payload_b64) % 4) if len(payload_b64) % 4 else ''
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            email = payload.get('email', '')
            username_from_token = payload.get('username', '') or email.split('@')[0] if email else ''
            if username_from_token:
                username = username_from_token
    except Exception:
        username = me.get('username', 'Token Account')
    
    if me.get('username'):
        username = me.get('username', username)
    
    encrypted = encrypt_token(token_value)
    aid = next_id()
    accounts = get_accounts()
    accounts[aid] = {
        'id': aid,
        'user_id': user_id,
        'token_encrypted': encrypted,
        'email': email,
        'username': username,
        'is_online': True,
        'status': 'active',
        'created_at': datetime.utcnow().isoformat(),
    }
    save_accounts(accounts)
    
    embed = make_embed_dict(
        '✅ Account Added!',
        f'**Username:** {username}\n**Email:** {email or "N/A"}\n**ID:** `{aid}`',
        color=0x57F287
    )
    await edit_original_response(response_token, embed=embed, components=[])


async def create_campaign_submit(user_id: str, name: str, ctype: str, acc_id: str,
                                  channels_str: str, msgs_str: str, response_token: str):
    ctype = ctype.strip().lower()
    
    if ctype not in ('channel_messaging', 'dm_auto_reply'):
        await edit_original_response(response_token,
            content='❌ Type must be `channel_messaging` or `dm_auto_reply`',
            components=[])
        return
    
    accounts = get_accounts()
    if acc_id not in accounts or accounts[acc_id].get('user_id') != user_id:
        await edit_original_response(response_token,
            content='❌ Account not found or not yours!',
            components=[])
        return
    
    users = get_users()
    user = users.get(user_id, {})
    if ctype == 'dm_auto_reply' and 'auto_reply_dm' not in PLAN_CONFIG.get(user.get('plan', 'free'), {}).get('features', []):
        await edit_original_response(response_token,
            content='❌ DM Auto-Reply requires V3 or Lifetime plan!',
            components=[])
        return
    
    channels = [c.strip() for c in channels_str.split(',') if c.strip()] if channels_str else []
    messages = [{'content': m.strip(), 'delay': 0} for m in msgs_str.split('|') if m.strip()]
    
    if not messages:
        await edit_original_response(response_token,
            content='❌ At least one message is required.',
            components=[])
        return
    
    cid = next_id()
    campaigns = get_campaigns()
    campaigns[cid] = {
        'id': cid,
        'user_id': user_id,
        'account_id': acc_id,
        'name': name,
        'type': ctype,
        'status': 'paused',
        'channels': channels,
        'messages': messages,
        'reply_trigger': '',
        'schedule': {'type': 'immediate'},
        'send_all_at_once': False,
        'stats': {'sent': 0, 'failed': 0, 'replied': 0},
        'created_at': datetime.utcnow().isoformat(),
    }
    save_campaigns(campaigns)
    
    embed = make_embed_dict(
        '✅ Campaign Created!',
        f'**Name:** {name}\n**Type:** {ctype}\n**Account:** `{acc_id}`\n**Messages:** {len(messages)}\n**Status:** Paused\n\nUse campaign view to start it!',
        color=0x57F287
    )
    await edit_original_response(response_token, embed=embed, components=[])


async def redeem_key_submit(user_id: str, key: str, response_token: str):
    key = key.strip().upper()
    keys_data = get_keys()
    
    if key not in keys_data:
        await edit_original_response(response_token,
            content='❌ Invalid key.',
            components=[])
        return
    
    key_data = keys_data[key]
    if key_data.get('used'):
        await edit_original_response(response_token,
            content='❌ This key has already been used.',
            components=[])
        return
    
    plan = key_data['plan']
    users = get_users()
    
    if user_id in users:
        user = users[user_id]
        if plan == 'lifetime':
            user['plan'] = 'lifetime'
            user['max_accounts'] = 5
            user['subscription_expires_at'] = None
        else:
            user['plan'] = plan
            user['max_accounts'] = PLAN_CONFIG[plan]['max_accounts']
            user['subscription_expires_at'] = (datetime.utcnow() + timedelta(days=30)).isoformat()
        user['updated_at'] = datetime.utcnow().isoformat()
        save_users(users)
    
    keys_data[key]['used'] = True
    keys_data[key]['used_by'] = user_id
    keys_data[key]['used_at'] = datetime.utcnow().isoformat()
    save_keys(keys_data)
    
    embed = make_embed_dict(
        '✅ Key Redeemed!',
        f'Your plan has been upgraded to **{plan.upper()}**!\nEnjoy the premium features.',
        color=0x57F287
    )
    await edit_original_response(response_token, embed=embed, components=[])


# ============================================================
# ── ADMIN VIEW FUNCTIONS
# ============================================================

async def show_admin_overview(token: str):
    users = get_users()
    accounts = get_accounts()
    campaigns = get_campaigns()
    subs = get_subscriptions()
    
    running = sum(1 for c in campaigns.values() if c.get('status') == 'running')
    total_sent = sum(c.get('stats', {}).get('sent', 0) for c in campaigns.values())
    total_failed = sum(c.get('stats', {}).get('failed', 0) for c in campaigns.values())
    confirmed_subs = sum(1 for s in subs.values() if s.get('status') == 'confirmed')
    total_revenue = sum(s.get('amount', 0) for s in subs.values() if s.get('status') == 'confirmed')
    
    plan_counts = {}
    for u in users.values():
        p = u.get('plan', 'free')
        plan_counts[p] = plan_counts.get(p, 0) + 1
    
    embed = make_embed_dict(
        '📊 Admin Overview',
        'System-wide statistics.',
        color=0x5865F2,
        fields=[
            ('👥 Total Users', f'{len(users)}', True),
            ('👤 Accounts', f'{len(accounts)}', True),
            ('📨 Campaigns', f'{len(campaigns)} (🟢 {running})', True),
            ('✅ Messages Sent', f'{total_sent}', True),
            ('❌ Failed', f'{total_failed}', True),
            ('💰 Revenue', f'{total_revenue} LTC', True),
            ('📋 Subscriptions', f'{confirmed_subs}', True),
            ('Plan Distribution', ', '.join([f'{p}: {c}' for p, c in plan_counts.items()]), False),
        ]
    )
    
    components = [make_action_row(
        make_button('back_to_admin', '🔙 Back to Admin', style=2)
    )]
    await edit_original_response(token, embed=embed, components=components)


async def show_admin_users(token: str):
    users = get_users()
    
    embed = make_embed_dict(f'👥 Users ({len(users)})', '', color=0x5865F2)
    
    for uid, user in list(users.items())[:15]:
        name = user.get('username', 'Unknown')
        plan = user.get('plan', 'free').upper()
        accounts_count = len(get_user_accounts(uid))
        embed['fields'] = embed.get('fields', [])
        if len(embed['fields']) < 25:
            embed['fields'].append({
                'name': f'`{uid[:8]}...` {name}',
                'value': f'Plan: **{plan}** | Accounts: {accounts_count}',
                'inline': False
            })
    
    if len(users) > 15:
        embed['footer'] = {'text': f'Showing 15 of {len(users)} users'}
    
    components = [make_action_row(
        make_button('back_to_admin', '🔙 Back to Admin', style=2)
    )]
    await edit_original_response(token, embed=embed, components=components)


async def show_admin_campaigns(token: str):
    campaigns = get_campaigns()
    
    embed = make_embed_dict(f'📨 All Campaigns ({len(campaigns)})', '', color=0x5865F2)
    
    for cid, camp in list(campaigns.items())[:15]:
        status = camp.get('status', '?')
        status_e = '🟢' if status == 'running' else '⏸' if status == 'paused' else '✅' if status == 'completed' else '❌'
        stats = camp.get('stats', {})
        embed['fields'] = embed.get('fields', [])
        if len(embed['fields']) < 25:
            embed['fields'].append({
                'name': f'{status_e} `{cid}` {camp.get("name", "Unnamed")}',
                'value': f'Type: {camp.get("type", "?")} | Sent: {stats.get("sent", 0)} | Failed: {stats.get("failed", 0)}',
                'inline': False
            })
    
    components = [make_action_row(
        make_button('back_to_admin', '🔙 Back to Admin', style=2)
    )]
    await edit_original_response(token, embed=embed, components=components)


async def show_admin_revenue(token: str):
    subs = get_subscriptions()
    confirmed = [s for s in subs.values() if s.get('status') == 'confirmed']
    total = sum(s.get('amount', 0) for s in confirmed)
    
    embed = make_embed_dict(
        '💰 Revenue Overview',
        f'Total Revenue: **{total} LTC**',
        color=0xFEE75C,
        fields=[
            ('Total Subscriptions', f'{len(confirmed)}', True),
            ('Pending', f'{sum(1 for s in subs.values() if s.get("status") == "pending")}', True),
            ('Free Trials', f'{sum(1 for s in subs.values() if s.get("plan") == "v3" and s.get("type") == "free_trial")}', False),
        ]
    )
    
    components = [make_action_row(
        make_button('back_to_admin', '🔙 Back to Admin', style=2)
    )]
    await edit_original_response(token, embed=embed, components=components)


async def show_admin_system(token: str):
    import psutil
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    uptime_seconds = time.time() - process.create_time()
    
    data_files = {
        'users.json': USERS_FILE,
        'accounts.json': ACCOUNTS_FILE,
        'campaigns.json': CAMPAIGNS_FILE,
        'subscriptions.json': SUBSCRIPTIONS_FILE,
        'keys.json': KEYS_FILE,
    }
    
    file_info = '\n'.join([f'{name}: {path.stat().st_size / 1024:.1f} KB' if path.exists() else f'{name}: 0 B' for name, path in data_files.items()])
    
    embed = make_embed_dict(
        '⚙️ System Status',
        'Bot health and resource usage.',
        color=0x5865F2,
        fields=[
            ('Uptime', f'{uptime_seconds / 60:.1f} minutes', True),
            ('Memory', f'{memory_mb:.1f} MB', True),
            ('Data Files', file_info, False),
            ('Admins', ', '.join([str(a) for a in ADMIN_IDS]), False),
        ]
    )
    
    components = [make_action_row(
        make_button('back_to_admin', '🔙 Back to Admin', style=2)
    )]
    await edit_original_response(token, embed=embed, components=components)


async def show_genkey_modal_admin(token: str):
    await edit_original_response(token, content=
        '🔑 **Generate License Keys**\n\n'
        'Use the command:\n'
        '`/genkey <plan> <count>`\n\n'
        'Plans: v1, v2, v3, lifetime\n'
        'Count: 1-50\n\n'
        'Example: `/genkey v3 5`',
        components=[make_action_row(
            make_button('back_to_admin', '🔙 Back to Admin', style=2)
        )]
    )


async def genkey_submit(user_id: str, plan: str, count_str: str, token: str):
    if not is_admin(int(user_id)):
        await edit_original_response(token, content='❌ Unauthorized.', components=[])
        return
    
    plan = plan.strip().lower()
    if plan not in ('v1', 'v2', 'v3', 'lifetime'):
        await edit_original_response(token, content='❌ Invalid plan. Use: v1, v2, v3, lifetime', components=[])
        return
    
    try:
        count = int(count_str.strip())
        if count < 1 or count > 50:
            raise ValueError
    except ValueError:
        await edit_original_response(token, content='❌ Count must be 1-50', components=[])
        return
    
    keys_data = get_keys()
    created = []
    for _ in range(count):
        key = f'HUNTER-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}'
        keys_data[key] = {
            'key': key,
            'plan': plan,
            'used': False,
            'used_by': None,
            'created_at': datetime.utcnow().isoformat(),
        }
        created.append(key)
    save_keys(keys_data)
    
    embed = make_embed_dict(
        f'✅ Generated {count} Key(s) for {plan.upper()}',
        '\n'.join([f'`{k}`' for k in created]),
        color=0x57F287
    )
    await edit_original_response(token, embed=embed, components=[])


# ============================================================
# ── SELF-BOT MESSAGING ENGINE (Pure REST based)
# ============================================================

class SelfbotManager:
    """
    Pure selfbot engine using SelfbotRESTClient.
    No discord.py/discord.py-self — communicates directly with Discord REST API.
    """
    
    def __init__(self):
        self.clients: Dict[str, SelfbotRESTClient] = {}
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.dm_reply_clients: Dict[str, dict] = {}  # account_id -> {trigger, messages, campaign_id}
    
    async def get_client(self, account_id: str) -> Optional[SelfbotRESTClient]:
        """Get or create a REST client for an account."""
        if account_id in self.clients:
            return self.clients[account_id]
        
        accounts = get_accounts()
        acc = accounts.get(account_id)
        if not acc:
            return None
        
        encrypted = acc.get('token_encrypted', '')
        token = decrypt_token(encrypted)
        if not token:
            return None
        
        client = SelfbotRESTClient(token)
        valid, _ = await client.validate()
        if not valid:
            return None
        
        self.clients[account_id] = client
        return client
    
    async def logout_account(self, account_id: str):
        """Remove a selfbot client."""
        if account_id in self.clients:
            await self.clients[account_id].close()
            del self.clients[account_id]
        if account_id in self.dm_reply_clients:
            del self.dm_reply_clients[account_id]
    
async def send_messages(self, campaign_id: str, campaign: dict):
        """Send campaign messages using pure REST API."""
        account_id = campaign.get('account_id', '')
        client = await self.get_client(account_id)
        if not client:
            log.error(f"[{campaign_id}] Cannot get client for account {account_id}")
            return
        
        channels = campaign.get('channels', [])
        messages = campaign.get('messages', [{'content': 'Hello!'}])
        
        for ch_id in channels:
            if not self._is_running(campaign_id):
                break
            
            try:
                for msg_data in messages:
                    if not self._is_running(campaign_id):
                        break
                    
                    content = msg_data.get('content', '')
                    delay = msg_data.get('delay', 0)
                    
                    if delay > 0:
                        await asyncio.sleep(delay)
                    
                    if content:
                        result = await client.send_message(ch_id, content)
                        status = result.get('_status', 0)
                        
                        camps = get_campaigns()
                        if campaign_id in camps:
                            if status == 200:
                                camps[campaign_id]['stats']['sent'] = camps[campaign_id]['stats'].get('sent', 0) + 1
                                log.info(f"[{campaign_id}] Sent to {ch_id}: {content[:50]}")
                            else:
                                camps[campaign_id]['stats']['failed'] = camps[campaign_id]['stats'].get('failed', 0) + 1
                                log.error(f"[{campaign_id}] Failed to send to {ch_id}: {result}")
                            save_campaigns(camps)
                    
                    await asyncio.sleep(1)  # Rate limit protection
                    
            except Exception as e:
                log.error(f"[{campaign_id}] Error processing channel {ch_id}: {e}")
                camps = get_campaigns()
                if campaign_id in camps:
                    camps[campaign_id]['stats']['failed'] = camps[campaign_id]['stats'].get('failed', 0) + 1
                    save_campaigns(camps)
    
async def setup_dm_reply(self, campaign_id: str, campaign: dict):
        """
        Setup DM auto-reply.
        Since we can't use WebSocket events without discord.py-self,
        we poll for new DMs periodically. This is a REST-based alternative
        to WebSocket event listeners.
        """
        account_id = campaign.get('account_id', '')
        client = await self.get_client(account_id)
        if not client:
            return
        
        trigger = campaign.get('reply_trigger', '').lower() or None
        messages = campaign.get('messages', [{'content': 'Hello!'}])
        
        self.dm_reply_clients[account_id] = {
            'client': client,
            'campaign_id': campaign_id,
            'trigger': trigger,
            'messages': messages,
            'last_message_id': None
        }
        
        log.info(f"[{campaign_id}] DM auto-reply listener configured (polling mode)")
    
    async def check_dm_replies(self):
        """Poll for new DMs and auto-reply."""
        for account_id, config in list(self.dm_reply_clients.items()):
            try:
                client = config['client']
                campaign_id = config['campaign_id']
                trigger = config['trigger']
                reply_messages = config['messages']
                
                camps = get_campaigns()
                camp = camps.get(campaign_id)
                if not camp or camp.get('status') != 'running':
                    continue
                
                dm_channels = await client.get_dm_channels()
                
                for dm in dm_channels:
                    channel_id = dm.get('id', '')
                    recipient = dm.get('recipients', [{}])[0] if dm.get('recipients') else {}
                    recipient_id = recipient.get('id', '')
                    
                    messages = await client.get_channel_messages(channel_id, limit=5)
                    
                    for msg in messages:
                        msg_id = msg.get('id', '')
                        msg_author_id = msg.get('author', {}).get('id', '')
                        
                        if msg_author_id == recipient_id:
                            msg_content = msg.get('content', '').lower()
                            
                            if trigger and trigger not in msg_content:
                                continue
                            
                            last_id = config.get('last_message_id')
                            if msg_id == last_id:
                                continue
                            
                            for reply_msg in reply_messages:
                                content = reply_msg.get('content', '')
                                if content:
                                    await client.send_message(channel_id, content)
                                    camp = get_campaigns().get(campaign_id)
                                    if camp:
                                        camp['stats']['replied'] = camp['stats'].get('replied', 0) + 1
                                        save_campaigns(get_campaigns())
                                        log.info(f"[{campaign_id}] Auto-replied to DM from {recipient_id}")
                                    await asyncio.sleep(1)
                                    break
                            
                            config['last_message_id'] = msg_id
                            
            except Exception as e:
                log.error(f"DM reply check error for {account_id}: {e}")
        
        await asyncio.sleep(5)
    
    def _is_running(self, campaign_id: str) -> bool:
        camps = get_campaigns()
        camp = camps.get(campaign_id)
        return camp is not None and camp.get('status') == 'running'
    
    async def process_campaign(self, campaign_id: str):
        """Process a single campaign."""
        camps = get_campaigns()
        campaign = camps.get(campaign_id)
        
        if not campaign or campaign.get('status') != 'running':
            return
        
        log.info(f"Processing campaign {campaign_id}: {campaign.get('name')}")
        
        try:
            if campaign.get('type') == 'channel_messaging':
                await self.send_messages(campaign_id, campaign)
                camps = get_campaigns()
                if campaign_id in camps:
                    camps[campaign_id]['status'] = 'completed'
                    save_campaigns(camps)
            
            elif campaign.get('type') == 'dm_auto_reply':
                await self.setup_dm_reply(campaign_id, campaign)
                
        except Exception as e:
            log.error(f"[{campaign_id}] Error: {e}")
            camps = get_campaigns()
            if campaign_id in camps:
                camps[campaign_id]['status'] = 'failed'
                save_campaigns(camps)
    
    async def run_pending_campaigns(self):
        """Poll for running campaigns."""
        campaigns = get_campaigns()
        running = {k: v for k, v in campaigns.items() if v.get('status') == 'running'}
        
        for cid in list(running.keys()):
            if cid not in self.active_tasks or self.active_tasks[cid].done():
                task = asyncio.create_task(self.process_campaign(cid))
                self.active_tasks[cid] = task
        
        for cid in list(self.active_tasks.keys()):
            if self.active_tasks[cid].done():
                try:
                    self.active_tasks[cid].result()
                except:
                    pass
                del self.active_tasks[cid]


# ============================================================
# GLOBALS
# ============================================================
sm = SelfbotManager()


# ============================================================
# ── BACKGROUND POLLING LOOP
# ============================================================

async def campaign_polling_loop():
    """Background loop polling for campaigns and DM replies."""
    log.info("Campaign polling loop started")
    
    while True:
        try:
            await sm.run_pending_campaigns()
            await sm.check_dm_replies()
        except Exception as e:
            log.error(f"Polling error: {e}")
        await asyncio.sleep(15)


# ============================================================
# ── SLASH COMMAND REGISTRATION (via Discord API)
# ============================================================

async def register_commands():
    """Register slash commands with Discord API directly."""
    global APPLICATION_ID
    async with aiohttp.ClientSession() as session:
        headers = {
            'Authorization': f'Bot {BOT_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        async with session.get('https://discord.com/api/v10/applications/@me', headers=headers) as resp:
            if resp.status != 200:
                log.error(f"Failed to get application info: {resp.status}")
                return
            app_data = await resp.json()
            APPLICATION_ID = app_data.get('id')
            log.info(f"Got application ID: {APPLICATION_ID}")
        
        url = f'https://discord.com/api/v10/applications/{APPLICATION_ID}/commands'
        
        commands = [
            {
                'name': 'panel',
                'description': 'Open your user panel (DM only)',
                'type': 1
            },
            {
                'name': 'admin',
                'description': 'Open admin panel (DM only, admins only)',
                'type': 1
            },
            {
                'name': 'redeem',
                'description': 'Redeem a license key',
                'type': 1,
                'options': [{
                    'type': 3,
                    'name': 'key',
                    'description': 'Your license key (HUNTER-XXXX-XXXX-XXXX)',
                    'required': True
                }]
            },
            {
                'name': 'campaign_create',
                'description': 'Create a new campaign',
                'type': 1,
                'options': [
                    {'type': 3, 'name': 'name', 'description': 'Campaign name', 'required': True},
                    {'type': 3, 'name': 'type', 'description': 'channel_messaging or dm_auto_reply', 'required': True},
                    {'type': 3, 'name': 'account_id', 'description': 'Account ID', 'required': True},
                    {'type': 3, 'name': 'channels', 'description': 'Channel IDs (comma-separated)', 'required': False},
                    {'type': 3, 'name': 'messages', 'description': 'Messages separated by |', 'required': True},
                ]
            },
            {
                'name': 'genkey',
                'description': 'Generate license keys (admin only)',
                'type': 1,
                'options': [
                    {'type': 3, 'name': 'plan', 'description': 'v1, v2, v3, or lifetime', 'required': True},
                    {'type': 4, 'name': 'count', 'description': 'Number of keys (1-50)', 'required': True},
                ]
            }
        ]
        
        for cmd in commands:
            async with session.post(url, json=cmd, headers=headers) as resp:
                if resp.status == 201:
                    log.info(f"✅ Registered command: /{cmd['name']}")
                elif resp.status == 200:
                    log.info(f"✅ Updated existing command: /{cmd['name']}")
                else:
                    text = await resp.text()
                    log.error(f"Failed to register /{cmd['name']}: {resp.status} {text}")


# ============================================================
# ── TEXT COMMAND PARSER (for DMs)
# ============================================================

async def handle_dm_text(user_id: str, username: str, content: str):
    """Handle text commands sent in DMs."""
    content = content.strip()
    content_lower = content.lower()
    
    if content_lower.startswith('/redeem '):
        key = content[8:].strip().upper()
        keys_data = get_keys()
        
        if key not in keys_data:
            return {'content': '❌ Invalid key.', 'ephemeral': True}
        
        key_data = keys_data[key]
        if key_data.get('used'):
            return {'content': '❌ This key has already been used.', 'ephemeral': True}
        
        plan = key_data['plan']
        users = get_users()
        
        if user_id in users:
            user = users[user_id]
            if plan == 'lifetime':
                user['plan'] = 'lifetime'
                user['max_accounts'] = 5
                user['subscription_expires_at'] = None
            else:
                user['plan'] = plan
                user['max_accounts'] = PLAN_CONFIG[plan]['max_accounts']
                user['subscription_expires_at'] = (datetime.utcnow() + timedelta(days=30)).isoformat()
            user['updated_at'] = datetime.utcnow().isoformat()
            save_users(users)
        
        keys_data[key]['used'] = True
        keys_data[key]['used_by'] = user_id
        keys_data[key]['used_at'] = datetime.utcnow().isoformat()
        save_keys(keys_data)
        
        return {'embeds': [make_embed_dict('✅ Key Redeemed!', f'Plan upgraded to **{plan.upper()}**!', color=0x57F287)]}
    
    elif content_lower == '/panel':
        user = get_users().get(user_id, {})
        user_accounts = get_user_accounts(user_id)
        user_campaigns = get_user_campaigns(user_id)
        running = sum(1 for c in user_campaigns.values() if c.get('status') == 'running')
        
        return {
            'embeds': [make_embed_dict(
                '🏠 Hunters Auto - Panel',
                f'Welcome **{username}**!',
                color=0x5865F2,
                fields=[
                    ('👤 Your Plan', f'`{user.get("plan", "free").upper()}`', True),
                    ('📊 Accounts', f'{len(user_accounts)}/{user.get("max_accounts", 1)}', True),
                    ('📨 Campaigns', f'{len(user_campaigns)} (🟢 {running} running)', True),
                ]
            )]
        }
    
    return None


# ============================================================
# ── DISCORD BOT GATEWAY CONNECTION (WebSocket)
# ============================================================

async def connect_discord_bot():
    """
    Connect to Discord Gateway as a bot.
    Makes the bot appear online and receive slash command interactions.
    """
    import aiohttp
    
    async with aiohttp.ClientSession() as session:
        headers = {
            'Authorization': f'Bot {BOT_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'DiscordBot (hunters-bot, 1.0)'
        }
        
        async with session.get('https://discord.com/api/v10/gateway/bot', headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"Failed to get gateway URL: {resp.status} {text}")
                return
            
            data = await resp.json()
            gateway_url = data.get('url', 'wss://gateway.discord.gg') + '/?v=10&encoding=json'
        
        log.info(f"Connecting to Discord Gateway...")
        
        async with session.ws_connect(gateway_url) as ws:
            hello = await ws.receive_json()
            heartbeat_interval = hello.get('d', {}).get('heartbeat_interval', 41250) / 1000.0
            
            identify_payload = {
                'op': 2,
                'd': {
                    'token': BOT_TOKEN,
                    'intents': 513,
                    'properties': {
                        '$os': 'linux',
                        '$browser': 'hunters-bot',
                        '$device': 'hunters-bot'
                    }
                }
            }
            await ws.send_json(identify_payload)
            
            async def heartbeat():
                while True:
                    await asyncio.sleep(heartbeat_interval)
                    try:
                        await ws.send_json({'op': 1, 'd': None})
                    except:
                        break
            
            heartbeat_task = asyncio.create_task(heartbeat())
            
            log.info(f"✅ Bot connected to Discord Gateway!")
            
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        op = data.get('op', 0)
                        
                        if op == 0:
                            t = data.get('t', '')
                            if t == 'READY':
                                user = data.get('d', {}).get('user', {})
                                log.info(f"Bot logged in as: {user.get('username')}#{user.get('discriminator', '0')} (ID: {user.get('id')})")
                                
                                try:
                                    await register_commands()
                                except Exception as e:
                                    log.warning(f"Command registration after ready: {e}")
                            
                            elif t == 'INTERACTION_CREATE':
                                log.info(f"Received interaction via Gateway: {data.get('d', {}).get('data', {}).get('name', 'unknown')}")
                                asyncio.create_task(handle_interaction(data.get('d', {})))
                        
                        elif op == 7:
                            log.warning("Gateway requested reconnect")
                            break
                        
                        elif op == 9:
                            log.warning("Invalid session, reconnecting...")
                            break
                    
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.error(f"WebSocket error: {ws.exception()}")
                        break
            
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error(f"Gateway error: {e}")
            finally:
                heartbeat_task.cancel()
                log.warning("Disconnected from Gateway, reconnecting in 5s...")
                await asyncio.sleep(5)


# ============================================================
# ── MAIN ENTRY POINT
# ============================================================

async def main():
    """Main entry point."""
    global _loop
    _loop = asyncio.get_event_loop()
    
    log.info('=== Hunters Auto Bot (Pure Selfbot Engine) ===')
    log.info(f'Admin IDs: {ADMIN_IDS}')
    log.info(f'Data directory: {DATA_DIR.absolute()}')
    
    for path in [USERS_FILE, ACCOUNTS_FILE, CAMPAIGNS_FILE, SUBSCRIPTIONS_FILE, KEYS_FILE]:
        if not path.exists():
            _save_json(path, {})
    
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    
    gateway_task = asyncio.create_task(connect_discord_bot())
    
    log.info("Starting campaign polling loop...")
    
    await asyncio.gather(
        campaign_polling_loop(),
        gateway_task
    )


if __name__ == '__main__':
    if not BOT_TOKEN:
        log.error('BOT_TOKEN environment variable required')
        exit(1)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Shutting down...')
