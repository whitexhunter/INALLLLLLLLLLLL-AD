import os
import json
import asyncio
import logging
import uuid
import time
import secrets
import hashlib
import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands
from discord.ext import tasks
from cryptography.fernet import Fernet

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
# ENCRYPTION (Fernet)
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
# DISCORD BOT
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ============================================================
# AUTH / GUARDS
# ============================================================
async def ensure_user(interaction: discord.Interaction) -> dict:
    """Create user if not exists, return user dict."""
    users = get_users()
    uid = str(interaction.user.id)
    if uid not in users:
        users[uid] = {
            'discord_id': uid,
            'username': interaction.user.name,
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
        # Update username
        if users[uid].get('username') != interaction.user.name:
            users[uid]['username'] = interaction.user.name
            users[uid]['updated_at'] = datetime.utcnow().isoformat()
            save_users(users)
    return users[uid]

async def check_plan_expiry(user_id: str):
    """Check and downgrade expired plans."""
    users = get_users()
    if user_id not in users:
        return
    user = users[user_id]
    now = datetime.utcnow()
    
    # Trial expiry
    if user.get('trial_expires_at') and datetime.fromisoformat(user['trial_expires_at']) < now:
        if user['plan'] == 'v3' and user.get('is_trial_used') and not user.get('subscription_expires_at'):
            user['plan'] = 'free'
            user['max_accounts'] = 1
            log.info(f"User {user_id} trial expired, downgraded to free")
    
    # Subscription expiry
    if user.get('subscription_expires_at') and datetime.fromisoformat(user['subscription_expires_at']) < now:
        if user['plan'] != 'free' and user['plan'] != 'lifetime':
            user['plan'] = 'free'
            user['max_accounts'] = 1
            user['subscription_expires_at'] = None
            log.info(f"User {user_id} subscription expired, downgraded to free")
    
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
# EMBED BUILDERS
# ============================================================
def make_embed(title: str, description: str = '', color: int = 0x5865F2, fields: list = None, footer: str = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if footer:
        embed.set_footer(text=footer)
    else:
        embed.set_footer(text='Hunters Auto | DM Only')
    return embed

# ============================================================
# PANEL VIEWS
# ============================================================
class PanelView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=300)
        self.user_id = user_id
    
    @discord.ui.button(label='📊 Dashboard', style=discord.ButtonStyle.primary, row=0)
    async def dashboard_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_dashboard(interaction)
    
    @discord.ui.button(label='👤 My Accounts', style=discord.ButtonStyle.secondary, row=0)
    async def accounts_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_accounts(interaction)
    
    @discord.ui.button(label='📨 My Campaigns', style=discord.ButtonStyle.secondary, row=0)
    async def campaigns_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_campaigns(interaction)
    
    @discord.ui.button(label='➕ Add Account', style=discord.ButtonStyle.success, row=1)
    async def add_account_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_add_account_modal(interaction)
    
    @discord.ui.button(label='🚀 New Campaign', style=discord.ButtonStyle.success, row=1)
    async def new_campaign_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_new_campaign_modal(interaction)
    
    @discord.ui.button(label='💰 Plans & Buy', style=discord.ButtonStyle.danger, row=1)
    async def plans_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_plans(interaction)
    
    @discord.ui.button(label='🔄 Refresh', style=discord.ButtonStyle.gray, row=2)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_main_panel(interaction)

class AccountView(discord.ui.View):
    def __init__(self, user_id: str, account_id: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.account_id = account_id
    
    @discord.ui.button(label='🗑 Delete', style=discord.ButtonStyle.danger, row=0)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        accounts = get_accounts()
        if self.account_id in accounts:
            del accounts[self.account_id]
            save_accounts(accounts)
            await interaction.response.edit_message(content='✅ Account deleted.', embed=None, view=None)
        else:
            await interaction.response.edit_message(content='❌ Account not found.', view=None)
    
    @discord.ui.button(label='🔙 Back', style=discord.ButtonStyle.secondary, row=0)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_accounts(interaction)

class CampaignView(discord.ui.View):
    def __init__(self, campaign_id: str):
        super().__init__(timeout=120)
        self.campaign_id = campaign_id
    
    @discord.ui.button(label='▶ Start', style=discord.ButtonStyle.success, row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_campaign(interaction, self.campaign_id, 'running')
    
    @discord.ui.button(label='⏸ Pause', style=discord.ButtonStyle.warning, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_campaign(interaction, self.campaign_id, 'paused')
    
    @discord.ui.button(label='🗑 Delete', style=discord.ButtonStyle.danger, row=0)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        campaigns = get_campaigns()
        if self.campaign_id in campaigns:
            del campaigns[self.campaign_id]
            save_campaigns(campaigns)
            await interaction.response.edit_message(content='✅ Campaign deleted.', embed=None, view=None)
        else:
            await interaction.response.edit_message(content='❌ Campaign not found.', view=None)
    
    @discord.ui.button(label='🔙 Back', style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        campaigns = get_campaigns()
        c = campaigns.get(self.campaign_id, {})
        await show_campaigns(interaction)

class PlansView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=120)
        self.user_id = user_id
    
    @discord.ui.button(label='💎 V1 - $1/mo', style=discord.ButtonStyle.primary, row=0)
    async def v1_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await generate_payment(interaction, 'v1')
    
    @discord.ui.button(label='💎 V2 - $2/mo', style=discord.ButtonStyle.primary, row=0)
    async def v2_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await generate_payment(interaction, 'v2')
    
    @discord.ui.button(label='💎 V3 - $3/mo', style=discord.ButtonStyle.primary, row=0)
    async def v3_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await generate_payment(interaction, 'v3')
    
    @discord.ui.button(label='🔥 Lifetime - $30', style=discord.ButtonStyle.danger, row=1)
    async def lifetime_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await generate_payment(interaction, 'lifetime')
    
    @discord.ui.button(label='🎁 Free Trial (10min V3)', style=discord.ButtonStyle.success, row=1)
    async def trial_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await start_trial(interaction)
    
    @discord.ui.button(label='🔙 Back', style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_main_panel(interaction)

class PayConfirmView(discord.ui.View):
    def __init__(self, user_id: str, plan: str, sub_id: str):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.plan = plan
        self.sub_id = sub_id
    
    @discord.ui.button(label='✅ I Have Paid', style=discord.ButtonStyle.success, row=0)
    async def paid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        subs = get_subscriptions()
        sub = subs.get(self.sub_id)
        if not sub:
            await interaction.response.edit_message(content='❌ Payment session expired. Use /panel to try again.', view=None)
            return
        
        # Mark as confirmed
        sub['status'] = 'confirmed'
        sub['confirmed_at'] = datetime.utcnow().isoformat()
        save_subscriptions(subs)
        
        # Upgrade user
        users = get_users()
        user = users.get(self.user_id)
        if user:
            if self.plan == 'lifetime':
                user['plan'] = 'lifetime'
                user['max_accounts'] = 5
                user['subscription_expires_at'] = None
            else:
                user['plan'] = self.plan
                user['max_accounts'] = PLAN_CONFIG[self.plan]['max_accounts']
                user['subscription_expires_at'] = (datetime.utcnow() + timedelta(days=30)).isoformat()
            user['updated_at'] = datetime.utcnow().isoformat()
            save_users(users)
        
        embed = make_embed(
            '✅ Payment Verified!',
            f'Your plan has been upgraded to **{self.plan.upper()}**!',
            color=0x57F287
        )
        await interaction.response.edit_message(embed=embed, view=None)

# ============================================================
# MODALS
# ============================================================
class AddAccountModal(discord.ui.Modal, title='Add Discord Account'):
    token_input = discord.ui.TextInput(
        label='Discord Token',
        placeholder='Paste your Discord account token here...',
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        token = self.token_input.value.strip()
        uid = str(interaction.user.id)
        
        # Check account limit
        users = get_users()
        user = users.get(uid, {})
        user_accounts = get_user_accounts(uid)
        max_acc = user.get('max_accounts', 1)
        
        if len(user_accounts) >= max_acc:
            await interaction.response.send_message(
                f'❌ You reached your plan limit ({max_acc} accounts). Upgrade to add more!',
                ephemeral=True
            )
            return
        
        # Extract user info from token (JWT decode)
        email = ''
        username = 'Unknown'
        try:
            parts = token.split('.')
            if len(parts) == 3:
                # Fix padding for base64
                payload_b64 = parts[1]
                payload_b64 += '=' * (4 - len(payload_b64) % 4) if len(payload_b64) % 4 else ''
                import base64 as b64
                payload = json.loads(b64.urlsafe_b64decode(payload_b64))
                email = payload.get('email', '')
                username = payload.get('username', '') or email.split('@')[0] if email else 'Unknown'
        except Exception:
            username = 'Token Account'
        
        # Encrypt and save
        encrypted = encrypt_token(token)
        aid = next_id()
        accounts = get_accounts()
        accounts[aid] = {
            'id': aid,
            'user_id': uid,
            'token_encrypted': encrypted,
            'email': email,
            'username': username,
            'is_online': False,
            'status': 'active',
            'created_at': datetime.utcnow().isoformat(),
        }
        save_accounts(accounts)
        
        embed = make_embed(
            '✅ Account Added!',
            f'**Username:** {username}\n**Email:** {email or "N/A"}\n**ID:** `{aid}`',
            color=0x57F287
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class NewCampaignModal(discord.ui.Modal, title='Create New Campaign'):
    name_input = discord.ui.TextInput(
        label='Campaign Name',
        placeholder='My Campaign',
        required=True,
        max_length=50,
    )
    type_input = discord.ui.TextInput(
        label='Type: channel_messaging or dm_auto_reply',
        placeholder='channel_messaging',
        required=True,
        max_length=30,
    )
    account_id_input = discord.ui.TextInput(
        label='Account ID to use',
        placeholder='Paste Account ID from /panel',
        required=True,
        max_length=20,
    )
    channels_input = discord.ui.TextInput(
        label='Channel IDs (comma-separated)',
        placeholder='123456789,987654321 (or leave blank for DM)',
        required=False,
        max_length=200,
    )
    messages_input = discord.ui.TextInput(
        label='Messages (separate with | )',
        placeholder='Hello! | How are you? | Check this out!',
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph,
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        name = self.name_input.value.strip()
        ctype = self.type_input.value.strip().lower()
        acc_id = self.account_id_input.value.strip()
        channels_str = self.channels_input.value.strip()
        msgs_str = self.messages_input.value.strip()
        
        if ctype not in ('channel_messaging', 'dm_auto_reply'):
            await interaction.response.send_message('❌ Type must be `channel_messaging` or `dm_auto_reply`', ephemeral=True)
            return
        
        # Verify account exists and belongs to user
        accounts = get_accounts()
        if acc_id not in accounts or accounts[acc_id].get('user_id') != uid:
            await interaction.response.send_message('❌ Account not found or not yours!', ephemeral=True)
            return
        
        # Check plan features for DM auto-reply
        users = get_users()
        user = users.get(uid, {})
        if ctype == 'dm_auto_reply' and 'auto_reply_dm' not in PLAN_CONFIG.get(user.get('plan', 'free'), {}).get('features', []):
            await interaction.response.send_message('❌ DM Auto-Reply requires V3 or Lifetime plan!', ephemeral=True)
            return
        
        channels = [c.strip() for c in channels_str.split(',') if c.strip()] if channels_str else []
        messages = [m.strip() for m in msgs_str.split('|') if m.strip()]
        
        cid = next_id()
        campaigns = get_campaigns()
        campaigns[cid] = {
            'id': cid,
            'user_id': uid,
            'account_id': acc_id,
            'name': name,
            'type': ctype,
            'status': 'paused',
            'channels': channels,
            'messages': [{'content': m, 'delay': 0} for m in messages],
            'schedule': {'type': 'immediate'},
            'send_all_at_once': False,
            'stats': {'sent': 0, 'failed': 0, 'replied': 0},
            'created_at': datetime.utcnow().isoformat(),
        }
        save_campaigns(campaigns)
        
        embed = make_embed(
            '✅ Campaign Created!',
            f'**Name:** {name}\n**Type:** {ctype}\n**Account:** `{acc_id}`\n**Messages:** {len(messages)}\n**Status:** Paused\n\nUse the campaign view to start it!',
            color=0x57F287
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# VIEW FUNCTIONS
# ============================================================
async def show_main_panel(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    user = await ensure_user(interaction)
    await check_plan_expiry(uid)
    
    user_accounts = get_user_accounts(uid)
    user_campaigns = get_user_campaigns(uid)
    running = sum(1 for c in user_campaigns.values() if c.get('status') == 'running')
    
    embed = make_embed(
        '🏠 Hunters Auto - Panel',
        f'Welcome **{interaction.user.name}**!\nSelect an option below.',
        color=0x5865F2,
        fields=[
            ('👤 Your Plan', f'`{user.get("plan", "free").upper()}`', True),
            ('📊 Accounts', f'{len(user_accounts)}/{user.get("max_accounts", 1)}', True),
            ('📨 Campaigns', f'{len(user_campaigns)} (🟢 {running} running)', True),
            ('📅 Subscription', f'{user.get("subscription_expires_at", "N/A") or "Lifetime"}', False),
        ],
        footer='Hunters Auto | DM Only'
    )
    
    await interaction.response.edit_message(embed=embed, view=PanelView(uid))

async def show_dashboard(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    user = await ensure_user(interaction)
    await check_plan_expiry(uid)
    
    user_accounts = get_user_accounts(uid)
    user_campaigns = get_user_campaigns(uid)
    
    total_sent = sum(c.get('stats', {}).get('sent', 0) for c in user_campaigns.values())
    total_failed = sum(c.get('stats', {}).get('failed', 0) for c in user_campaigns.values())
    total_replied = sum(c.get('stats', {}).get('replied', 0) for c in user_campaigns.values())
    running = sum(1 for c in user_campaigns.values() if c.get('status') == 'running')
    
    embed = make_embed(
        f'📊 Dashboard - {interaction.user.name}',
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
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='🔙 Back to Panel', style=discord.ButtonStyle.secondary, custom_id='back_to_panel'))
    
    await interaction.response.edit_message(embed=embed, view=view)

async def show_accounts(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    user_accounts = get_user_accounts(uid)
    
    if not user_accounts:
        embed = make_embed('👤 My Accounts', 'No accounts added yet.', color=0xED4245)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='➕ Add Account', style=discord.ButtonStyle.success, custom_id='add_account'))
        view.add_item(discord.ui.Button(label='🔙 Back', style=discord.ButtonStyle.secondary, custom_id='back_to_panel'))
        await interaction.response.edit_message(embed=embed, view=view)
        return
    
    desc_lines = []
    for aid, acc in list(user_accounts.items())[:10]:
        status_emoji = '🟢' if acc.get('status') == 'active' else '🔴'
        desc_lines.append(f'**`{aid}`** {status_emoji} {acc.get("username", "Unknown")} — {acc.get("email", "No email")}')
    
    embed = make_embed(
        f'👤 My Accounts ({len(user_accounts)})',
        '\n'.join(desc_lines) if desc_lines else 'No accounts.',
        color=0x5865F2
    )
    
    await interaction.response.edit_message(embed=embed, view=PanelView(uid))

async def show_campaigns(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    user_campaigns = get_user_campaigns(uid)
    
    if not user_campaigns:
        embed = make_embed('📨 My Campaigns', 'No campaigns yet. Create one!', color=0xED4245)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='🚀 New Campaign', style=discord.ButtonStyle.success, custom_id='new_campaign'))
        view.add_item(discord.ui.Button(label='🔙 Back', style=discord.ButtonStyle.secondary, custom_id='back_to_panel'))
        await interaction.response.edit_message(embed=embed, view=view)
        return
    
    desc_lines = []
    for cid, camp in list(user_campaigns.items())[:10]:
        status_map = {'running': '🟢', 'paused': '⏸', 'completed': '✅', 'failed': '❌'}
        s = status_map.get(camp.get('status', 'paused'), '⏸')
        st = camp.get('stats', {})
        desc_lines.append(f'**`{cid}`** {s} **{camp.get("name", "Unnamed")}** — {camp.get("type", "?")} | Sent: {st.get("sent", 0)}')
    
    embed = make_embed(
        f'📨 My Campaigns ({len(user_campaigns)})',
        '\n'.join(desc_lines) if desc_lines else 'No campaigns.',
        color=0x5865F2
    )
    
    await interaction.response.edit_message(embed=embed, view=PanelView(uid))

async def show_add_account_modal(interaction: discord.Interaction):
    modal = AddAccountModal()
    await interaction.response.send_modal(modal)

async def show_new_campaign_modal(interaction: discord.Interaction):
    modal = NewCampaignModal()
    await interaction.response.send_modal(modal)

async def show_plans(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    user = get_users().get(uid, {})
    
    embed = make_embed(
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
    
    await interaction.response.edit_message(embed=embed, view=PlansView(uid))

async def generate_payment(interaction: discord.Interaction, plan: str):
    uid = str(interaction.user.id)
    price = PRICES.get(plan, {})
    amount = price.get('lifetime') if plan == 'lifetime' else price.get('monthly')
    
    sub_id = next_id()
    subs = get_subscriptions()
    subs[sub_id] = {
        'id': sub_id,
        'user_id': uid,
        'plan': plan,
        'amount': amount,
        'ltc_address': LTC_ADDRESS,
        'status': 'pending',
        'created_at': datetime.utcnow().isoformat(),
        'expires_at': (datetime.utcnow() + timedelta(hours=2)).isoformat(),
    }
    save_subscriptions(subs)
    
    embed = make_embed(
        '💳 Payment Required',
        f'Plan: **{plan.upper()}**\nAmount: **{amount} LTC**\n\nSend exactly **{amount} LTC** to:\n`{LTC_ADDRESS}`\n\nThen click **I Have Paid** below.',
        color=0xFEE75C,
        fields=[('Subscription ID', f'`{sub_id}`', False)]
    )
    
    await interaction.response.edit_message(embed=embed, view=PayConfirmView(uid, plan, sub_id))

async def start_trial(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    users = get_users()
    user = users.get(uid)
    
    if not user:
        await interaction.response.send_message('❌ Register first with /panel', ephemeral=True)
        return
    
    if user.get('is_trial_used'):
        await interaction.response.send_message('❌ You already used your free trial!', ephemeral=True)
        return
    
    user['is_trial_used'] = True
    user['plan'] = 'v3'
    user['max_accounts'] = 5
    user['trial_expires_at'] = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
    user['updated_at'] = datetime.utcnow().isoformat()
    save_users(users)
    
    # Log subscription
    sub_id = next_id()
    subs = get_subscriptions()
    subs[sub_id] = {
        'id': sub_id,
        'user_id': uid,
        'plan': 'v3',
        'type': 'free_trial',
        'amount': 0,
        'status': 'confirmed',
        'created_at': datetime.utcnow().isoformat(),
    }
    save_subscriptions(subs)
    
    embed = make_embed(
        '🎁 Trial Activated!',
        'You now have **V3** plan for **10 minutes**!\nEnjoy all features: 5 accounts, DM auto-reply, image attachments.',
        color=0x57F287
    )
    await interaction.response.edit_message(embed=embed, view=None)

async def toggle_campaign(interaction: discord.Interaction, campaign_id: str, new_status: str):
    campaigns = get_campaigns()
    if campaign_id not in campaigns:
        await interaction.response.edit_message(content='❌ Campaign not found.', view=None)
        return
    
    campaigns[campaign_id]['status'] = new_status
    save_campaigns(campaigns)
    
    embed = make_embed(
        f'{"▶" if new_status == "running" else "⏸"} Campaign {new_status.title()}!',
        f'Campaign `{campaign_id}` is now **{new_status}**.',
        color=0x57F287 if new_status == 'running' else 0xFEE75C
    )
    await interaction.response.edit_message(embed=embed, view=None)

# ============================================================
# ADMIN VIEW
# ============================================================
class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label='📊 Overview', style=discord.ButtonStyle.primary, row=0)
    async def overview_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_admin_overview(interaction)
    
    @discord.ui.button(label='👥 Users', style=discord.ButtonStyle.secondary, row=0)
    async def users_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_admin_users(interaction)
    
    @discord.ui.button(label='🔑 Generate Key', style=discord.ButtonStyle.success, row=0)
    async def genkey_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_genkey_modal(interaction)
    
    @discord.ui.button(label='📨 All Campaigns', style=discord.ButtonStyle.secondary, row=1)
    async def campaigns_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_admin_campaigns(interaction)
    
    @discord.ui.button(label='💰 Revenue', style=discord.ButtonStyle.danger, row=1)
    async def revenue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_admin_revenue(interaction)
    
    @discord.ui.button(label='⚙️ System', style=discord.ButtonStyle.gray, row=1)
    async def system_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_admin_system(interaction)
    
    @discord.ui.button(label='🔄 Refresh', style=discord.ButtonStyle.gray, row=2)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_admin_panel(interaction)

class GenKeyModal(discord.ui.Modal, title='Generate License Key'):
    plan_input = discord.ui.TextInput(
        label='Plan (v1, v2, v3, lifetime)',
        placeholder='v3',
        required=True,
        max_length=20,
    )
    count_input = discord.ui.TextInput(
        label='Number of keys to generate',
        placeholder='1',
        required=True,
        max_length=5,
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        plan = self.plan_input.value.strip().lower()
        if plan not in ('v1', 'v2', 'v3', 'lifetime'):
            await interaction.response.send_message('❌ Invalid plan. Use: v1, v2, v3, lifetime', ephemeral=True)
            return
        
        try:
            count = int(self.count_input.value.strip())
            if count < 1 or count > 50:
                raise ValueError
        except ValueError:
            await interaction.response.send_message('❌ Count must be 1-50', ephemeral=True)
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
        
        embed = make_embed(
            f'✅ Generated {count} Key(s) for {plan.upper()}',
            '\n'.join([f'`{k}`' for k in created]),
            color=0x57F287
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def show_admin_panel(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message('❌ You are not authorized to use this command.', ephemeral=True)
        return
    
    users = get_users()
    accounts = get_accounts()
    campaigns = get_campaigns()
    subs = get_subscriptions()
    
    running_camps = sum(1 for c in campaigns.values() if c.get('status') == 'running')
    confirmed_subs = sum(1 for s in subs.values() if s.get('status') == 'confirmed')
    total_revenue = sum(s.get('amount', 0) for s in subs.values() if s.get('status') == 'confirmed')
    
    embed = make_embed(
        '🛡️ Admin Panel',
        'Full control over the Hunters Auto system.',
        color=0xED4245,
        fields=[
            ('👥 Users', f'{len(users)}', True),
            ('👤 Accounts', f'{len(accounts)}', True),
            ('📨 Campaigns', f'{len(campaigns)} (🟢 {running_camps} running)', True),
            ('💰 Revenue', f'{total_revenue} LTC', True),
            ('📋 Subscriptions', f'{confirmed_subs} confirmed', False),
        ],
        footer=f'Admin: {interaction.user.name}'
    )
    
    await interaction.response.edit_message(embed=embed, view=AdminView())

async def show_admin_overview(interaction: discord.Interaction):
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
    
    embed = make_embed(
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
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='🔙 Back to Admin', style=discord.ButtonStyle.secondary, custom_id='back_to_admin'))
    await interaction.response.edit_message(embed=embed, view=view)

async def show_admin_users(interaction: discord.Interaction):
    users = get_users()
    
    embed = make_embed(f'👥 Users ({len(users)})', '', color=0x5865F2)
    
    for uid, user in list(users.items())[:15]:
        name = user.get('username', 'Unknown')
        plan = user.get('plan', 'free').upper()
        accounts_count = len(get_user_accounts(uid))
        embed.add_field(
            name=f'`{uid[:8]}...` {name}',
            value=f'Plan: **{plan}** | Accounts: {accounts_count}',
            inline=False
        )
    
    if len(users) > 15:
        embed.set_footer(text=f'Showing 15 of {len(users)} users')
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='🔙 Back to Admin', style=discord.ButtonStyle.secondary, custom_id='back_to_admin'))
    await interaction.response.edit_message(embed=embed, view=view)

async def show_admin_campaigns(interaction: discord.Interaction):
    campaigns = get_campaigns()
    
    embed = make_embed(f'📨 All Campaigns ({len(campaigns)})', '', color=0x5865F2)
    
    for cid, camp in list(campaigns.items())[:15]:
        status = camp.get('status', '?')
        status_e = '🟢' if status == 'running' else '⏸' if status == 'paused' else '✅' if status == 'completed' else '❌'
        stats = camp.get('stats', {})
        embed.add_field(
            name=f'{status_e} `{cid}` {camp.get("name", "Unnamed")}',
            value=f'Type: {camp.get("type", "?")} | Sent: {stats.get("sent", 0)} | Failed: {stats.get("failed", 0)}',
            inline=False
        )
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='🔙 Back to Admin', style=discord.ButtonStyle.secondary, custom_id='back_to_admin'))
    await interaction.response.edit_message(embed=embed, view=view)

async def show_admin_revenue(interaction: discord.Interaction):
    subs = get_subscriptions()
    confirmed = [s for s in subs.values() if s.get('status') == 'confirmed']
    total = sum(s.get('amount', 0) for s in confirmed)
    
    embed = make_embed(
        '💰 Revenue Overview',
        f'Total Revenue: **{total} LTC**',
        color=0xFEE75C,
        fields=[
            ('Total Subscriptions', f'{len(confirmed)}', True),
            ('Pending', f'{sum(1 for s in subs.values() if s.get("status") == "pending")}', True),
            ('Free Trials', f'{sum(1 for s in subs.values() if s.get("plan") == "v3" and s.get("type") == "free_trial")}', False),
        ]
    )
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='🔙 Back to Admin', style=discord.ButtonStyle.secondary, custom_id='back_to_admin'))
    await interaction.response.edit_message(embed=embed, view=view)

async def show_admin_system(interaction: discord.Interaction):
    import os, psutil
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    uptime_seconds = time.time() - process.create_time()
    
    # Data file sizes
    data_files = {
        'users.json': USERS_FILE,
        'accounts.json': ACCOUNTS_FILE, 
        'campaigns.json': CAMPAIGNS_FILE,
        'subscriptions.json': SUBSCRIPTIONS_FILE,
        'keys.json': KEYS_FILE,
    }
    
    file_info = []
    for name, path in data_files.items():
        size = path.stat().st_size if path.exists() else 0
        file_info.append(f'{name}: {size/1024:.1f} KB')
    
    embed = make_embed(
        '⚙️ System Status',
        'Bot health and resource usage.',
        color=0x5865F2,
        fields=[
            ('Uptime', f'{uptime_seconds/60:.1f} minutes', True),
            ('Memory', f'{memory_mb:.1f} MB', True),
            ('Data Files', '\n'.join(file_info), False),
            ('Admins', ', '.join([str(a) for a in ADMIN_IDS]), False),
        ]
    )
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='🔙 Back to Admin', style=discord.ButtonStyle.secondary, custom_id='back_to_admin'))
    await interaction.response.edit_message(embed=embed, view=view)

async def show_genkey_modal(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message('❌ Unauthorized.', ephemeral=True)
        return
    modal = GenKeyModal()
    await interaction.response.send_modal(modal)

# ============================================================
# REDEEM KEY (Button-based)
# ============================================================
class RedeemView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
    
    @discord.ui.button(label='🎟 Enter Key', style=discord.ButtonStyle.primary, custom_id='redeem_key')
    async def redeem_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        class RedeemModal(discord.ui.Modal, title='Redeem License Key'):
            key_input = discord.ui.TextInput(
                label='Your License Key',
                placeholder='HUNTER-XXXX-XXXX-XXXX',
                required=True,
                max_length=50,
            )
            
            async def on_submit(self, interaction: discord.Interaction):
                key = self.key_input.value.strip().upper()
                keys_data = get_keys()
                
                if key not in keys_data:
                    await interaction.response.send_message('❌ Invalid key.', ephemeral=True)
                    return
                
                key_data = keys_data[key]
                if key_data.get('used'):
                    await interaction.response.send_message('❌ This key has already been used.', ephemeral=True)
                    return
                
                plan = key_data['plan']
                uid = str(interaction.user.id)
                users = get_users()
                
                if uid in users:
                    user = users[uid]
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
                
                # Mark key as used
                keys_data[key]['used'] = True
                keys_data[key]['used_by'] = uid
                keys_data[key]['used_at'] = datetime.utcnow().isoformat()
                save_keys(keys_data)
                
                embed = make_embed(
                    '✅ Key Redeemed!',
                    f'Your plan has been upgraded to **{plan.upper()}**!\nEnjoy the premium features.',
                    color=0x57F287
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await interaction.response.send_modal(RedeemModal())

# ============================================================
# SLASH COMMANDS
# ============================================================
@tree.command(name='panel', description='Open your user panel (DM only)')
async def panel_command(interaction: discord.Interaction):
    if interaction.guild:
        await interaction.response.send_message('❌ This command only works in DMs with the bot.', ephemeral=True)
        return
    
    await ensure_user(interaction)
    user = get_users().get(str(interaction.user.id), {})
    
    user_accounts = get_user_accounts(str(interaction.user.id))
    user_campaigns = get_user_campaigns(str(interaction.user.id))
    running = sum(1 for c in user_campaigns.values() if c.get('status') == 'running')
    
    embed = make_embed(
        '🏠 Hunters Auto - Panel',
        f'Welcome **{interaction.user.name}**!\nSelect an option below.',
        color=0x5865F2,
        fields=[
            ('👤 Your Plan', f'`{user.get("plan", "free").upper()}`', True),
            ('📊 Accounts', f'{len(user_accounts)}/{user.get("max_accounts", 1)}', True),
            ('📨 Campaigns', f'{len(user_campaigns)} (🟢 {running} running)', True),
        ]
    )
    
    await interaction.response.send_message(embed=embed, view=PanelView(str(interaction.user.id)))

@tree.command(name='admin', description='Open admin panel (DM only, admins only)')
async def admin_command(interaction: discord.Interaction):
    if interaction.guild:
        await interaction.response.send_message('❌ This command only works in DMs with the bot.', ephemeral=True)
        return
    
    if not is_admin(interaction.user.id):
        await interaction.response.send_message('❌ You are not authorized.', ephemeral=True)
        return
    
    users = get_users()
    accounts = get_accounts()
    campaigns = get_campaigns()
    running = sum(1 for c in campaigns.values() if c.get('status') == 'running')
    revenue = sum(s.get('amount', 0) for s in get_subscriptions().values() if s.get('status') == 'confirmed')
    
    embed = make_embed(
        '🛡️ Admin Panel',
        'Full control over the Hunters Auto system.',
        color=0xED4245,
        fields=[
            ('👥 Users', f'{len(users)}', True),
            ('👤 Accounts', f'{len(accounts)}', True),
            ('📨 Campaigns', f'{len(campaigns)} (🟢 {running})', True),
            ('💰 Revenue', f'{revenue} LTC', True),
        ],
        footer=f'Admin: {interaction.user.name}'
    )
    
    await interaction.response.send_message(embed=embed, view=AdminView())

# ============================================================
# SELF-BOT MESSAGING ENGINE
# ============================================================
class SelfbotManager:
    def __init__(self):
        self.clients: Dict[str, discord.Client] = {}
        self.active_tasks: Dict[str, asyncio.Task] = {}
    
    async def login_account(self, account_id: str, token: str) -> Optional[discord.Client]:
        """Login a user token via discord.py-self."""
        if account_id in self.clients:
            return self.clients[account_id]
        
        try:
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)
            
            await client.login(token)
            self.clients[account_id] = client
            log.info(f"Logged in account {account_id}")
            return client
        except Exception as e:
            log.error(f"Failed to login account {account_id}: {e}")
            return None
    
    async def logout_account(self, account_id: str):
        """Logout a selfbot account."""
        if account_id in self.clients:
            try:
                await self.clients[account_id].logout()
            except:
                pass
            del self.clients[account_id]
        if account_id in self.active_tasks:
            self.active_tasks[account_id].cancel()
            del self.active_tasks[account_id]
    
    async def send_messages(self, campaign_id: str, campaign: dict):
        """Send campaign messages using the selfbot account."""
        account_id = campaign.get('account_id', '')
        accounts = get_accounts()
        acc = accounts.get(account_id)
        
        if not acc:
            log.error(f"Account {account_id} not found for campaign {campaign_id}")
            return
        
        encrypted = acc.get('token_encrypted', '')
        token = decrypt_token(encrypted)
        if not token:
            log.error(f"Failed to decrypt token for account {account_id}")
            return
        
        client = await self.login_account(account_id, token)
        if not client:
            return
        
        channels = campaign.get('channels', [])
        messages = campaign.get('messages', [{'content': 'Hello!'}])
        
        for ch_id in channels:
            try:
                channel = client.get_channel(int(ch_id))
                if not channel:
                    try:
                        channel = await client.fetch_channel(int(ch_id))
                    except:
                        log.error(f"Cannot fetch channel {ch_id}")
                        continue
                
                for msg_data in messages:
                    content = msg_data.get('content', '')
                    delay = msg_data.get('delay', 0)
                    
                    if delay > 0:
                        await asyncio.sleep(delay)
                    
                    try:
                        await channel.send(content)
                        # Update stats
                        camps = get_campaigns()
                        if campaign_id in camps:
                            camps[campaign_id]['stats']['sent'] = camps[campaign_id]['stats'].get('sent', 0) + 1
                            save_campaigns(camps)
                        log.info(f"[{campaign_id}] Sent to {ch_id}: {content[:50]}")
                    except Exception as e:
                        camps = get_campaigns()
                        if campaign_id in camps:
                            camps[campaign_id]['stats']['failed'] = camps[campaign_id]['stats'].get('failed', 0) + 1
                            save_campaigns(camps)
                        log.error(f"[{campaign_id}] Failed to send to {ch_id}: {e}")
                    
                    await asyncio.sleep(1)  # Rate limit protection
                    
            except Exception as e:
                log.error(f"[{campaign_id}] Error processing channel {ch_id}: {e}")
    
    async def process_campaign(self, campaign_id: str):
        """Process a single campaign."""
        campaigns = get_campaigns()
        campaign = campaigns.get(campaign_id)
        
        if not campaign or campaign.get('status') != 'running':
            return
        
        log.info(f"Processing campaign {campaign_id}: {campaign.get('name')}")
        
        if campaign.get('type') == 'channel_messaging':
            await self.send_messages(campaign_id, campaign)
        elif campaign.get('type') == 'dm_auto_reply':
            # DM auto-reply: set up listener
            await self.setup_dm_reply(campaign_id, campaign)
    
    async def setup_dm_reply(self, campaign_id: str, campaign: dict):
        """Setup DM auto-reply listener for a campaign."""
        account_id = campaign.get('account_id', '')
        accounts = get_accounts()
        acc = accounts.get(account_id)
        
        if not acc:
            return
        
        encrypted = acc.get('token_encrypted', '')
        token = decrypt_token(encrypted)
        if not token:
            return
        
        client = await self.login_account(account_id, token)
        if not client:
            return
        
        trigger = campaign.get('reply_trigger', '').lower() or None
        messages = campaign.get('messages', [{'content': 'Hello!'}])
        
        @client.event
        async def on_message(message):
            if message.author == client.user:
                return
            if not isinstance(message.channel, discord.DMChannel):
                return
            
            content = message.content.lower()
            if trigger and trigger not in content:
                return
            
            # Check campaign still running
            camps = get_campaigns()
            camp = camps.get(campaign_id)
            if not camp or camp.get('status') != 'running':
                return
            
            for msg_data in messages:
                try:
                    await message.channel.send(msg_data.get('content', ''))
                    camp['stats']['replied'] = camp['stats'].get('replied', 0) + 1
                    save_campaigns(camps)
                    log.info(f"[{campaign_id}] Auto-replied to DM from {message.author}")
                except Exception as e:
                    camp['stats']['failed'] = camp['stats'].get('failed', 0) + 1
                    save_campaigns(camps)
                    log.error(f"[{campaign_id}] DM reply failed: {e}")
                
                await asyncio.sleep(1)
                break  # Only send first message as reply
        
        log.info(f"[{campaign_id}] DM auto-reply listener active")
    
    async def run_pending_campaigns(self):
        """Main loop: find and process all running campaigns."""
        campaigns = get_campaigns()
        running = {k: v for k, v in campaigns.items() if v.get('status') == 'running'}
        
        for cid, camp in running.items():
            if cid not in self.active_tasks or self.active_tasks[cid].done():
                task = asyncio.create_task(self.process_campaign(cid))
                self.active_tasks[cid] = task
        
        await asyncio.sleep(15)  # Poll every 15 seconds

sm = SelfbotManager()

# ============================================================
# BACKGROUND POLLING LOOP
# ============================================================
async def campaign_polling_loop():
    """Background task that polls for running campaigns every 15s."""
    await bot.wait_until_ready()
    log.info("Campaign polling loop started")
    
    while not bot.is_closed():
        try:
            await sm.run_pending_campaigns()
        except Exception as e:
            log.error(f"Polling error: {e}")
        await asyncio.sleep(15)

# ============================================================
# BOT EVENTS
# ============================================================
@bot.event
async def on_ready():
    log.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    log.info(f'Admin IDs: {ADMIN_IDS}')
    log.info(f'Data directory: {DATA_DIR.absolute()}')
    
    # Ensure data files exist
    for path in [USERS_FILE, ACCOUNTS_FILE, CAMPAIGNS_FILE, SUBSCRIPTIONS_FILE, KEYS_FILE]:
        if not path.exists():
            _save_json(path, {})
    
    # Sync slash commands
    await tree.sync()
    log.info('Slash commands synced')
    
    # Start polling loop
    asyncio.create_task(campaign_polling_loop())

@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Handle button custom_id callbacks that aren't handled by views."""
    if interaction.type == discord.InteractionType.component:
        cid = interaction.data.get('custom_id', '')
        
        if cid == 'back_to_panel':
            await show_main_panel(interaction)
        elif cid == 'back_to_admin':
            await show_admin_panel(interaction)
        elif cid == 'add_account':
            await show_add_account_modal(interaction)
        elif cid == 'new_campaign':
            await show_new_campaign_modal(interaction)
        elif cid == 'redeem_key':
            # Already handled by RedeemView
            pass

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    if not BOT_TOKEN:
        log.error('BOT_TOKEN environment variable required')
        exit(1)
    
    log.info('Starting Hunters Auto Bot...')

# ============================================================
# HEALTH CHECK HTTP SERVER (for Render)
# ============================================================
import http.server
import socketserver
import threading

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"hunters-bot"}')
    
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs

def run_health_server():
    port = int(os.environ.get('PORT', 10000))
    server = socketserver.TCPServer(('0.0.0.0', port), HealthHandler)
    log.info(f'Health server running on port {port}')
    server.serve_forever()

# Start health server in a thread before bot.run
health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()
  
bot.run(BOT_TOKEN)
