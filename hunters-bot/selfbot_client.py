"""
Pure custom Discord Selfbot REST client.
No third-party Discord libraries. Raw HTTP via aiohttp.
Handles user token authentication and Discord REST API communication.
"""
import aiohttp
import asyncio
import logging
import json
import time
from typing import Optional, Dict, List, Any

log = logging.getLogger('selfbot-client')

DISCORD_API = 'https://discord.com/api/v10'

class SelfbotRESTClient:
    """
    Pure HTTP client for Discord's REST API using a user token.
    No discord.py-self, no nextcord. Pure aiohttp.
    """
    
    def __init__(self, token: str):
        self.token = token
        self.session: Optional[aiohttp.ClientSession] = None
        self._headers = {
            'Authorization': token,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        self._rate_limits: Dict[str, float] = {}
        self._last_request_time = 0.0
    
    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self._headers)
    
    async def __aenter__(self):
        await self._ensure_session()
        return self
    
    async def __aexit__(self, *args):
        await self.close()
    
    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make a rate-limit-aware request to Discord API."""
        await self._ensure_session()
        
        url = f'{DISCORD_API}{endpoint}'
        
        # Respect rate limits
        now = time.time()
        for route, reset_at in list(self._rate_limits.items()):
            if now < reset_at and endpoint.startswith(route):
                wait = reset_at - now
                await asyncio.sleep(wait)
        
        # Global rate limit: max 1 req per 0.3s for safety
        elapsed = now - self._last_request_time
        if elapsed < 0.3:
            await asyncio.sleep(0.3 - elapsed)
        
        for attempt in range(3):
            try:
                async with self.session.request(method, url, **kwargs) as resp:
                    self._last_request_time = time.time()
                    
                    if resp.status == 429:
                        retry_after = float(resp.headers.get('Retry-After', 5))
                        log.warning(f"Rate limited (429) on {endpoint}, retry in {retry_after}s")
                        self._rate_limits[endpoint] = time.time() + retry_after
                        await asyncio.sleep(retry_after)
                        continue
                    
                    if resp.status == 401:
                        log.error(f"Unauthorized (401) on {endpoint} — invalid token")
                        return {'error': 'unauthorized', 'status': 401}
                    
                    if resp.status == 403:
                        log.error(f"Forbidden (403) on {endpoint}")
                        return {'error': 'forbidden', 'status': 403}
                    
                    if resp.status == 404:
                        return {'error': 'not_found', 'status': 404}
                    
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = {'raw': text}
                    
                    data['_status'] = resp.status
                    
                    # Track rate limit headers
                    remaining = resp.headers.get('X-RateLimit-Remaining')
                    if remaining is not None and int(remaining) == 0:
                        reset_after = float(resp.headers.get('X-RateLimit-Reset-After', 1))
                        self._rate_limits[endpoint] = time.time() + reset_after
                    
                    return data
                    
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.error(f"Request error on {endpoint}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return {'error': str(e), 'status': 0}
        
        return {'error': 'max_retries', 'status': 0}
    
    # ======== USER ENDPOINTS ========
    
    async def get_me(self) -> dict:
        """Get current user info."""
        return await self._request('GET', '/users/@me')
    
    async def get_guilds(self) -> list:
        """Get all guilds the user is in."""
        result = await self._request('GET', '/users/@me/guilds')
        return result if isinstance(result, list) else []
    
    async def get_dm_channels(self) -> list:
        """Get all DM channels."""
        result = await self._request('GET', '/users/@me/channels')
        return result if isinstance(result, list) else []
    
    async def create_dm(self, recipient_id: str) -> dict:
        """Create a DM channel with a user."""
        return await self._request('POST', '/users/@me/channels', json={'recipient_id': recipient_id})
    
    # ======== CHANNEL ENDPOINTS ========
    
    async def send_message(self, channel_id: str, content: str, embed: dict = None, tts: bool = False) -> dict:
        """Send a message to a channel."""
        payload = {'content': content, 'tts': tts}
        if embed:
            payload['embeds'] = [embed] if isinstance(embed, dict) else embed
        return await self._request('POST', f'/channels/{channel_id}/messages', json=payload)
    
    async def get_channel_messages(self, channel_id: str, limit: int = 50) -> list:
        """Get recent messages from a channel."""
        result = await self._request('GET', f'/channels/{channel_id}/messages?limit={limit}')
        return result if isinstance(result, list) else []
    
    async def get_channel(self, channel_id: str) -> dict:
        """Get channel info."""
        return await self._request('GET', f'/channels/{channel_id}')
    
    async def trigger_typing(self, channel_id: str) -> bool:
        """Trigger typing indicator."""
        result = await self._request('POST', f'/channels/{channel_id}/typing')
        return result.get('_status') == 204
    
    # ======== GUILD ENDPOINTS ========
    
    async def get_guild_channels(self, guild_id: str) -> list:
        """Get all channels in a guild."""
        result = await self._request('GET', f'/guilds/{guild_id}/channels')
        return result if isinstance(result, list) else []
    
    async def get_guild_members(self, guild_id: str, limit: int = 100) -> list:
        """Get members of a guild."""
        result = await self._request('GET', f'/guilds/{guild_id}/members?limit={limit}')
        return result if isinstance(result, list) else []
    
    # ======== SESSION MANAGEMENT ========
    
    async def close(self):
        """Close the aiohttp session."""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
    
    async def validate(self) -> tuple:
        """Validate that the token works. Returns (is_valid: bool, user_data: dict)."""
        me = await self.get_me()
        if me.get('_status') == 200:
            return True, me
        return False, me
