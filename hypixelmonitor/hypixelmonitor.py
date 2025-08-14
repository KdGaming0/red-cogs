import asyncio
import re
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup
from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import pagify
import discord

LOGGER = logging.getLogger("red.hypixelmonitor")

# Unique identifier for Config.get_conf. Change if you fork the cog.
CONF_ID = 0x5b4c3d2e

# Default limits
MIN_INTERVAL = 60
DEFAULT_INTERVAL = 900
DEFAULT_THRESHOLD = 3.0
DEFAULT_MAX_PROCESSED = 1000

DEFAULT_KEYWORDS = {
    "higher": [
        # Special high-priority keywords that should trigger immediately
        "skyblock enhanced", "sb enhanced", "kd_gaming1", "kdgaming1", "kdgaming", "packcore", "scale me", "scaleme"
    ],
    "normal": [
        # Core mod terms
        "mod", "mods", "modpack", "modpacks", "forge", "fabric", "configs", "config", "1.21.5", "1.21.8",

        # 1.21+ Skyblock Mods
        "firmament", "skyblock tweaks", "modern warp menu", "skyblockaddons unofficial",
        "skyhanni", "hypixel mod api", "skyocean", "skyblock profile viewer", "bazaar utils",
        "skyblocker", "cookies-mod", "aaron's mod", "custom scoreboard", "skycubed",
        "nofrills", "nobaaddons", "sky cubed", "dulkirmod", "skyblock 21", "skycofl",

        # 1.8.9 Skyblock Mods
        "notenoughupdates", "neu", "polysprint", "skyblockaddons", "sba", "polypatcher",
        "hypixel plus", "furfsky", "dungeons guide", "skyguide", "partly sane skies",
        "secret routes mod", "skytils",

        # Performance Mods
        "more culling", "badoptimizations", "concurrent chunk management", "very many players",
        "threadtweak", "scalablelux", "particle core", "sodium", "lithium", "iris",
        "entity culling", "ferritecore", "immediatelyfast",

        # QoL Mods
        "scrollable tooltips", "fzzy config", "no chat reports", "no resource pack warnings",
        "auth me", "betterf3", "scale me", "packcore", "no double sneak", "centered crosshair",
        "continuity", "3d skin layers", "wavey capes", "sound controller", "cubes without borders",
        "sodium shadowy path blocks",

        # Popular Clients/Launchers
        "ladymod", "laby", "badlion", "lunar", "essential", "lunarclient", "client", "feather",

        # Performance issues
        "fps boost", "performance", "lag", "frames", "frame rate", "fps", "stuttering",
        "freezing", "crash", "crashing", "memory", "ram", "cpu", "gpu", "graphics",
        "low fps", "bad performance", "slow", "choppy", "frame drops",

        # PC/Technical problems
        "pc problem", "computer issue", "technical issue", "troubleshoot", "fix",
        "error", "bug", "glitch", "not working", "broken", "install", "installation",
        "setup", "configure", "configuration", "compatibility", "java", "minecraft", "windows", "linux",

        # Installation/setup
        "install mod", "mod installation", "how to install", "mod setup"
    ],
    "lower": [
        # Technical terms
        "modification", "skyblock addons", "not enough updates", "texture pack", "resource pack",
        "shader", "shaders", "optifine", "optimization", "optimize",

        # Modding terms
        "modding", "modded", "loader", "api", "addon", "plugin",
        "enhancement", "tweak", "utility", "tool", "helper"
    ],
    "negative": [
        # Strong game content indicators
        "minion", "coins", "dungeon master", "catacombs", "slayer", "dragon",
        "auction house", "bazaar", "trading", "selling", "buying", "worth",
        "price", "collection", "skill", "enchanting", "reforge", "talisman",
        "accessory", "weapon", "armor", "pet", "farming coins", "money making"
    ]
}

DEFAULT_FORUM_CATEGORIES = [
    {
        "url": "https://hypixel.net/forums/skyblock.157/",
        "name": "SkyBlock General"
    },
    {
        "url": "https://hypixel.net/forums/skyblock-community-help.196/",
        "name": "SkyBlock Community Help"
    }
]


class HypixelMonitor(commands.Cog):
    """Monitor Hypixel Forums for mod-related questions and technical help requests.

    Detection uses keyword lists divided into higher (immediate), normal, lower, and negative.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_ID, force_registration=True)

        # Guild defaults
        default_guild = {
            "enabled": False,
            "notify_channel_id": None,
            "forum_categories": DEFAULT_FORUM_CATEGORIES,
            "interval": DEFAULT_INTERVAL,
            "threshold": DEFAULT_THRESHOLD,
            "keywords": {
                "higher": [],
                "normal": [],
                "lower": [],
                "negative": [],
            },
            "processed_ids": [],
            "max_processed": DEFAULT_MAX_PROCESSED,
            "default_debug": False,
        }

        self.config.register_guild(**default_guild)

        # runtime state - improved task management
        self._tasks: Dict[int, asyncio.Task] = {}  # guild_id -> task
        self._sessions: Dict[int, aiohttp.ClientSession] = {}  # guild_id -> session
        self._task_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> lock for task creation
        self._global_lock = asyncio.Lock()

        # User agent for web requests
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

    async def cog_load(self) -> None:
        """Start monitoring tasks for all enabled guilds when the cog loads."""
        await self._startup_tasks()

    async def _startup_tasks(self):
        """Start monitoring tasks for all guilds that have monitoring enabled."""
        try:
            all_guilds = await self.config.all_guilds()
            for guild_id, guild_config in all_guilds.items():
                if guild_config.get("enabled", False):
                    guild = self.bot.get_guild(guild_id)
                    if guild:
                        await self._ensure_task(guild)
                        LOGGER.info("Started monitoring task for guild %s on cog load", guild_id)
        except Exception:
            LOGGER.exception("Error during startup task creation")

    async def cog_unload(self) -> None:
        """Clean shutdown of all monitoring tasks."""
        LOGGER.info("Shutting down HypixelMonitor cog...")

        # Cancel all monitoring tasks
        tasks_to_cancel = list(self._tasks.values())
        for task in tasks_to_cancel:
            if not task.cancelled():
                task.cancel()

        # Wait for tasks to finish cancelling
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self._tasks.clear()

        # Cleanup aiohttp sessions
        sessions_to_close = list(self._sessions.values())
        for session in sessions_to_close:
            try:
                await session.close()
            except Exception:
                LOGGER.exception("Error closing aiohttp session")

        self._sessions.clear()
        self._task_locks.clear()
        LOGGER.info("HypixelMonitor cog shutdown complete")

    # ------------------------- Helpers -------------------------
    async def _get_session(self, guild: discord.Guild) -> aiohttp.ClientSession:
        """Get or create an aiohttp session for the guild."""
        if guild.id in self._sessions and not self._sessions[guild.id].closed:
            return self._sessions[guild.id]

        try:
            session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=30)
            )
            self._sessions[guild.id] = session
            return session
        except Exception as e:
            LOGGER.exception("Failed to create aiohttp session: %s", e)
            raise

    def _match_score(self, title: str, body: str, keywords: dict) -> Dict:
        """Enhanced detection with context scoring and phrase matching."""
        score = 0.0
        matches = {"higher": [], "normal": [], "lower": [], "negative": []}

        # Preprocess text
        title_lower = title.lower()
        body_lower = body.lower()
        combined_text = f"{title_lower}\n{body_lower}"

        # Context indicators that boost confidence
        tech_context_patterns = [
            r'\b(help|issue|problem|error|crash|fix|install|setup|configure)\b',
            r'\b(not working|broken|won\'t work|can\'t get|having trouble)\b',
            r'\b(fps|performance|lag|optimization|memory|ram)\b'
        ]

        context_boost = 0
        for pattern in tech_context_patterns:
            if re.search(pattern, combined_text):
                context_boost += 0.5

        # Enhanced matching with phrase detection
        for level in ["higher", "normal", "lower", "negative"]:
            level_keywords = keywords.get(level, [])

            for keyword in level_keywords:
                keyword_lower = keyword.lower()

                # Exact phrase matching for multi-word keywords
                if ' ' in keyword_lower:
                    if keyword_lower in combined_text:
                        matches[level].append(keyword)
                        if level == "normal":
                            score += 3.0  # Higher score for exact phrases
                        elif level == "lower":
                            score += 1.5
                        elif level == "negative":
                            score -= 2.5
                else:
                    # Word boundary matching for single words
                    pattern = rf'\b{re.escape(keyword_lower)}\b'
                    if re.search(pattern, combined_text):
                        matches[level].append(keyword)
                        if level == "normal":
                            score += 2.0
                        elif level == "lower":
                            score += 1.0
                        elif level == "negative":
                            score -= 2.0

        # Apply context boost only if we have positive matches
        if matches["normal"] or matches["lower"]:
            score += context_boost

        # Title vs body weight (title matches are more important)
        title_matches = sum(len(matches[lvl]) for lvl in ["normal", "lower"]
                            if any(kw.lower() in title_lower for kw in matches[lvl]))
        if title_matches > 0:
            score += 1.0  # Bonus for title matches

        return {
            "immediate": bool(matches["higher"]),
            "score": score,
            "matches": matches,
            "context_boost": context_boost
        }

    async def _should_notify(self, thread_data: dict, detect_info: dict,
                             guild: discord.Guild) -> bool:
        """Advanced filtering to reduce false positives."""

        # Always notify for immediate matches
        if detect_info["immediate"]:
            return True

        # Check basic score threshold
        threshold = await self.config.guild(guild).threshold()
        if detect_info["score"] < threshold:
            return False

        # Additional filters
        title = thread_data.get('title', '').lower()
        body = thread_data.get('content', '').lower()

        # Skip if too many negative indicators
        negative_count = len(detect_info["matches"]["negative"])
        positive_count = len(detect_info["matches"]["normal"]) + len(detect_info["matches"]["lower"])

        if negative_count >= positive_count and negative_count > 2:
            return False

        # Skip common false positive patterns
        false_positive_patterns = [
            r'\b(selling|buying|trade|auction|price check|worth)\b',
            r'\b(looking for|want to buy|WTB|WTS)\b',
            r'\b(collection|skill|level|exp|xp)\b.*\b(boost|farm)\b',
            r'\b(what.*worth|how much|value)\b'
        ]

        combined = f"{title} {body}"
        for pattern in false_positive_patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                return False

        # Require stronger signals for borderline scores
        if detect_info["score"] < threshold + 1.0:
            # Need at least one strong keyword or good context
            if not detect_info["matches"]["normal"] and detect_info.get("context_boost", 0) < 1.0:
                return False

        return True

    async def _notify(self, guild: discord.Guild, thread_data: dict, detect_info: dict):
        """Enhanced notification with confidence indicators."""
        channel_id = await self.config.guild(guild).notify_channel_id()
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        title = thread_data.get('title', 'Unknown Title')
        url = thread_data.get('url', '')
        author = thread_data.get('author', 'Unknown')
        category = thread_data.get('category', 'Unknown Category')
        content = thread_data.get('content', '')

        # Determine confidence level
        score = detect_info.get("score", 0.0)
        if detect_info["immediate"]:
            confidence = "🔴 HIGH (Immediate)"
            color = discord.Color.red()
        elif score >= 5.0:
            confidence = "🟠 HIGH"
            color = discord.Color.orange()
        elif score >= 3.0:
            confidence = "🟡 MEDIUM"
            color = discord.Color.gold()
        else:
            confidence = "🟢 LOW"
            color = discord.Color.green()

        embed = discord.Embed(
            title=title[:256],
            url=url,
            description=(content[:500] + "..." if len(content) > 500 else content) or "No content preview available",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="Confidence", value=confidence, inline=True)
        embed.add_field(name="Score", value=f"{score:.1f}", inline=True)
        embed.add_field(name="Category", value=category, inline=True)

        # Show only significant matches to reduce noise
        matches = detect_info.get("matches", {})
        for lvl in ("higher", "normal"):
            vals = matches.get(lvl, [])
            if vals:
                embed.add_field(
                    name=f"{lvl.title()} Keywords",
                    value=", ".join(vals[:5]) + ("..." if len(vals) > 5 else ""),
                    inline=False
                )

        # Show negative matches if they exist (for debugging)
        if matches.get("negative"):
            embed.add_field(
                name="⚠️ Negative Indicators",
                value=", ".join(matches["negative"][:3]),
                inline=False
            )

        embed.set_footer(text=f"by {author} • Hypixel Forums")

        try:
            await channel.send(embed=embed)
        except Exception:
            LOGGER.exception("Failed to send notification")

    async def _add_processed(self, guild: discord.Guild, thread_id: str):
        async with self._global_lock:
            processed = await self.config.guild(guild).processed_ids()
            maxp = await self.config.guild(guild).max_processed()
            if processed is None:
                processed = []
            processed.append(thread_id)
            # keep most recent N
            if len(processed) > maxp:
                processed = processed[-maxp:]
            await self.config.guild(guild).processed_ids.set(processed)

    async def _is_processed(self, guild: discord.Guild, thread_id: str) -> bool:
        processed = await self.config.guild(guild).processed_ids()
        return processed and thread_id in processed

    async def _send_debug_message(self, guild: discord.Guild, message: str):
        """Send debug message to the notification channel."""
        debug_enabled = await self.config.guild(guild).default_debug()
        if not debug_enabled:
            return

        channel_id = await self.config.guild(guild).notify_channel_id()
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        try:
            await channel.send(message)
        except Exception:
            LOGGER.exception("Failed to send debug message")

    # ------------------------- Forum Parsing -------------------------
    def _extract_thread_id_from_class(self, class_str: str) -> Optional[str]:
        """Extract thread ID from class attribute."""
        if not class_str:
            return None

        match = re.search(r'js-threadListItem-(\d+)', class_str)
        if match:
            return match.group(1)
        return None

    async def _get_thread_content(self, session: aiohttp.ClientSession, thread_url: str) -> str:
        """Get the content of a thread."""
        try:
            async with session.get(thread_url) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')

                    content_element = soup.select_one('.message-body .message-userContent')
                    if not content_element:
                        content_element = soup.select_one('.message--post .message-body')

                    if content_element:
                        content = content_element.get_text(strip=True, separator=' ')
                        content = re.sub(r'\s+', ' ', content)
                        return content

        except Exception as e:
            LOGGER.warning("Error fetching thread content from %s: %s", thread_url, e)

        return ""

    async def _get_recent_threads(self, session: aiohttp.ClientSession, category: Dict[str, str]) -> List[
        Dict[str, str]]:
        """Get recent threads from a forum category."""
        threads = []

        try:
            async with session.get(category['url']) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')

                    thread_items = soup.select('.structItem--thread')

                    for item in thread_items:
                        try:
                            # Extract thread ID
                            class_attr = item.get('class', [])
                            class_str = ' '.join(class_attr)
                            thread_id = self._extract_thread_id_from_class(class_str)

                            if not thread_id:
                                continue

                            # Extract title and URL
                            title_element = item.select_one('.structItem-title')
                            if not title_element:
                                continue

                            title = title_element.get_text(strip=True)
                            url_element = title_element.select_one('a')
                            if not url_element:
                                continue

                            relative_url = url_element.get('href', '')
                            full_url = urljoin("https://hypixel.net", relative_url)

                            # Extract author
                            author_element = item.select_one('.structItem-minor .username')
                            if not author_element:
                                author_element = item.select_one('.username')

                            author = author_element.get_text(strip=True) if author_element else "Unknown"

                            threads.append({
                                'id': thread_id,
                                'title': title,
                                'url': full_url,
                                'author': author,
                                'category': category['name'],
                                'content': ''  # Will be fetched if needed
                            })
                        except Exception as e:
                            LOGGER.warning("Error parsing thread item: %s", e)
                            continue

        except Exception as e:
            LOGGER.error("Error fetching threads from %s: %s", category['name'], e)

        return threads

    # ------------------------- Monitoring Task -------------------------
    async def _monitor_guild(self, guild: discord.Guild):
        """Main monitoring loop for a guild."""
        LOGGER.info("Starting monitor for guild %s", guild.id)

        try:
            while True:
                try:
                    # Check if monitoring is still enabled
                    enabled = await self.config.guild(guild).enabled()
                    if not enabled:
                        LOGGER.info("Monitoring disabled for guild %s; stopping task", guild.id)
                        break

                    # Get configuration
                    categories = await self.config.guild(guild).forum_categories()
                    if not categories:
                        LOGGER.debug("No forum categories configured for guild %s", guild.id)
                        await self._send_debug_message(guild,
                                                       "⚠️ Hypixel monitor is alive but no forum categories are configured.")
                    else:
                        await self._monitor_categories(guild, categories)

                    # Wait for next check
                    interval = await self.config.guild(guild).interval()
                    if not isinstance(interval, int) or interval < MIN_INTERVAL:
                        interval = MIN_INTERVAL

                    LOGGER.debug("Guild %s sleeping for %d seconds", guild.id, interval)
                    await asyncio.sleep(interval)

                except asyncio.CancelledError:
                    LOGGER.info("Monitor task cancelled for guild %s", guild.id)
                    break
                except Exception:
                    LOGGER.exception("Error in monitoring loop for guild %s", guild.id)
                    await self._send_debug_message(guild, "❌ Hypixel monitor encountered an error. Retrying in 60s...")
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            LOGGER.info("Monitor task cancelled for guild %s", guild.id)
        except Exception:
            LOGGER.exception("Fatal error in monitor task for guild %s", guild.id)
        finally:
            # Cleanup
            await self._cleanup_guild_task(guild.id)

    async def _monitor_categories(self, guild: discord.Guild, categories: List[Dict[str, str]]):
        """Monitor all configured forum categories for a guild."""
        keywords = await self.config.guild(guild).keywords()
        session = await self._get_session(guild)

        found_any_match = False
        total_threads_checked = 0

        for category in categories:
            try:
                threads = await self._get_recent_threads(session, category)
                threads_checked = 0

                for thread in threads:
                    threads_checked += 1
                    total_threads_checked += 1

                    # Skip if already processed
                    if await self._is_processed(guild, thread['id']):
                        continue

                    # Get thread content for better analysis
                    if not thread['content']:
                        thread['content'] = await self._get_thread_content(session, thread['url'])

                    # Analyze thread content
                    title = thread['title'] or ""
                    body = thread['content'] or ""
                    detect = self._match_score(title, body, keywords)

                    # Check if we should notify
                    should_notify = await self._should_notify(thread, detect, guild)

                    if should_notify:
                        found_any_match = True
                        await self._notify(guild, thread, detect)
                        LOGGER.info("Notified for thread %s in %s for guild %s", thread['id'], category['name'],
                                    guild.id)

                    # Mark as processed regardless
                    await self._add_processed(guild, thread['id'])

                LOGGER.debug("Checked %d threads in %s for guild %s", threads_checked, category['name'], guild.id)

            except Exception:
                LOGGER.exception("Error processing category %s for guild %s", category['name'], guild.id)

        # Send debug message if no matches found and debug is enabled
        if not found_any_match:
            await self._send_debug_message(
                guild,
                f"✅ Hypixel monitor is alive. Checked {total_threads_checked} threads across {len(categories)} categor(y/ies). No matching threads found this cycle."
            )

    async def _cleanup_guild_task(self, guild_id: int):
        """Clean up resources for a guild's monitoring task."""
        # Remove task from tracking
        self._tasks.pop(guild_id, None)

        # Close aiohttp session
        session = self._sessions.pop(guild_id, None)
        if session:
            try:
                await session.close()
            except Exception:
                LOGGER.exception("Error closing aiohttp session for guild %s", guild_id)

        # Remove task lock
        self._task_locks.pop(guild_id, None)

        LOGGER.info("Cleanup completed for guild %s", guild_id)

    async def _ensure_task(self, guild: discord.Guild):
        """Ensure a monitoring task is running for a guild (thread-safe)."""
        if guild.id not in self._task_locks:
            self._task_locks[guild.id] = asyncio.Lock()

        async with self._task_locks[guild.id]:
            # Check if task already exists and is healthy
            existing_task = self._tasks.get(guild.id)
            if existing_task and not existing_task.done():
                LOGGER.debug("Task already running for guild %s", guild.id)
                return

            # Clean up any done task
            if existing_task:
                LOGGER.info("Cleaning up completed task for guild %s", guild.id)
                await self._cleanup_guild_task(guild.id)

            # Check if monitoring is enabled
            enabled = await self.config.guild(guild).enabled()
            if not enabled:
                LOGGER.debug("Monitoring disabled for guild %s, not starting task", guild.id)
                return

            # Create new task
            LOGGER.info("Creating new monitoring task for guild %s", guild.id)
            task = self.bot.loop.create_task(self._monitor_guild(guild))
            self._tasks[guild.id] = task

    async def _stop_task(self, guild: discord.Guild):
        """Stop monitoring task for a guild (thread-safe)."""
        if guild.id not in self._task_locks:
            return

        async with self._task_locks[guild.id]:
            task = self._tasks.get(guild.id)
            if task and not task.cancelled():
                LOGGER.info("Stopping monitoring task for guild %s", guild.id)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self._cleanup_guild_task(guild.id)

    # ------------------------- Commands -------------------------
    @commands.group()
    @commands.guild_only()
    async def hmonitor(self, ctx: commands.Context):
        """Hypixel monitor commands. Use 'quicksetup' or 'loaddefaults' to get started quickly."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @hmonitor.command(name="quicksetup")
    @commands.admin_or_permissions(manage_guild=True)
    async def quicksetup(self, ctx: commands.Context, channel: discord.TextChannel):
        """Quick setup: set channel and load default keywords."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await self.config.guild(ctx.guild).keywords.set(deepcopy(DEFAULT_KEYWORDS))
        await self.config.guild(ctx.guild).forum_categories.set(deepcopy(DEFAULT_FORUM_CATEGORIES))

        await ctx.send(f"✅ Quick setup complete!\n"
                       f"📢 Notification channel: {channel.mention}\n"
                       f"🔑 Default keywords loaded\n"
                       f"📂 Default forum categories loaded\n"
                       f"⚙️ Next step: Enable monitoring with `{ctx.prefix}hmonitor enable`")

    # Channel management
    @hmonitor.command(name="setchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where Hypixel forum notifications will be posted."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await ctx.send(f"Notification channel set to {channel.mention}")

    # Forum category management
    @hmonitor.command(name="addcategory")
    @commands.admin_or_permissions(manage_guild=True)
    async def addcategory(self, ctx: commands.Context, url: str, *, name: str):
        """Add a forum category to monitor."""
        async with self.config.guild(ctx.guild).forum_categories() as categories:
            if any(cat['url'] == url or cat['name'] == name for cat in categories):
                await ctx.send("A category with that URL or name already exists.")
                return
            categories.append({"url": url, "name": name})
        await ctx.send(f"Added forum category: {name}")

    @hmonitor.command(name="remcategory")
    @commands.admin_or_permissions(manage_guild=True)
    async def remcategory(self, ctx: commands.Context, *, name: str):
        """Remove a forum category from monitoring."""
        async with self.config.guild(ctx.guild).forum_categories() as categories:
            original_length = len(categories)
            categories[:] = [cat for cat in categories if cat['name'] != name]
            if len(categories) == original_length:
                await ctx.send("That category is not in the monitored list.")
                return
        await ctx.send(f"Removed forum category: {name}")

    @hmonitor.command(name="listcategories")
    @commands.admin_or_permissions(manage_guild=True)
    async def listcategories(self, ctx: commands.Context):
        """List all monitored forum categories."""
        categories = await self.config.guild(ctx.guild).forum_categories()
        if not categories:
            await ctx.send("No forum categories configured.")
            return

        msg_lines = ["Monitored forum categories:"]
        for cat in categories:
            msg_lines.append(f"- **{cat['name']}**: {cat['url']}")

        msg = "\n".join(msg_lines)
        for page in pagify(msg):
            await ctx.send(page)

    # Enable / disable
    @hmonitor.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def enable(self, ctx: commands.Context):
        """Enable monitoring for this guild."""
        enabled = await self.config.guild(ctx.guild).enabled()
        if enabled:
            await ctx.send("Monitoring is already enabled. Use `!hmonitor disable` to turn off.")
            return
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("Monitoring enabled for this guild.")
        await self._ensure_task(ctx.guild)

    @hmonitor.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context):
        """Disable monitoring for this guild."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await self._stop_task(ctx.guild)
        await ctx.send("Monitoring disabled for this guild.")

    # Interval and threshold
    @hmonitor.command(name="setinterval")
    @commands.admin_or_permissions(manage_guild=True)
    async def setinterval(self, ctx: commands.Context, seconds: int):
        """Set check interval in seconds (minimum 60)."""
        if seconds < MIN_INTERVAL:
            await ctx.send(f"Interval must be at least {MIN_INTERVAL} seconds.")
            return
        await self.config.guild(ctx.guild).interval.set(seconds)
        await ctx.send(f"Check interval set to {seconds} seconds.")

    @hmonitor.command(name="setthreshold")
    @commands.admin_or_permissions(manage_guild=True)
    async def setthreshold(self, ctx: commands.Context, threshold: float):
        """Set detection threshold (float between 1.0 and 10.0)."""
        if threshold < 1.0 or threshold > 10.0:
            await ctx.send("Threshold must be between 1.0 and 10.0")
            return
        await self.config.guild(ctx.guild).threshold.set(threshold)
        await ctx.send(f"Detection threshold set to {threshold}")

    # Keywords management
    @hmonitor.group(name="keyword")
    @commands.admin_or_permissions(manage_guild=True)
    async def keyword(self, ctx: commands.Context):
        """Manage detection keywords. Subcommands: add/remove/list"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @keyword.command(name="add")
    async def keyword_add(self, ctx: commands.Context, level: str, *, pattern: str):
        """Add a keyword/pattern to a level. Levels: higher, normal, lower, negative"""
        level = level.lower()
        if level not in ("higher", "normal", "lower", "negative"):
            await ctx.send("Invalid level. Use higher, normal, lower, or negative.")
            return
        async with self.config.guild(ctx.guild).keywords() as kw:
            kw[level].append(pattern)
        await ctx.send(f"Added pattern to {level}: `{pattern}`")

    @keyword.command(name="remove")
    async def keyword_remove(self, ctx: commands.Context, level: str, *, pattern: str):
        level = level.lower()
        if level not in ("higher", "normal", "lower", "negative"):
            await ctx.send("Invalid level. Use higher, normal, lower, or negative.")
            return
        async with self.config.guild(ctx.guild).keywords() as kw:
            if pattern not in kw[level]:
                await ctx.send("Pattern not found in that level.")
                return
            kw[level].remove(pattern)
        await ctx.send(f"Removed pattern from {level}: `{pattern}`")

    @keyword.command(name="list")
    async def keyword_list(self, ctx: commands.Context):
        kw = await self.config.guild(ctx.guild).keywords()
        msg_lines = []
        for lvl in ("higher", "normal", "lower", "negative"):
            vals = kw.get(lvl, []) or []
            msg_lines.append(f"{lvl.title()} ({len(vals)}):")
            for v in vals:
                msg_lines.append(f"  - {v}")
        for page in pagify("\n".join(msg_lines)):
            await ctx.send(page)

    @hmonitor.command(name="loaddefaults")
    @commands.admin_or_permissions(manage_guild=True)
    async def loaddefaults(self, ctx: commands.Context, merge: bool = False):
        """Load default keyword sets. Use 'true' as second argument to merge with existing keywords instead of replacing."""
        if merge:
            async with self.config.guild(ctx.guild).keywords() as kw:
                for level, defaults in DEFAULT_KEYWORDS.items():
                    existing = set(kw.get(level, []))
                    new_keywords = existing.union(set(defaults))
                    kw[level] = list(new_keywords)
            await ctx.send("Default keywords merged with existing keywords.")
        else:
            await self.config.guild(ctx.guild).keywords.set(DEFAULT_KEYWORDS.copy())
            await ctx.send("Default keywords loaded (existing keywords replaced).")

        # Show summary
        kw = await self.config.guild(ctx.guild).keywords()
        summary = []
        for level in ("higher", "normal", "lower", "negative"):
            count = len(kw.get(level, []))
            summary.append(f"{level}: {count}")

        await ctx.send(f"Keyword counts: {', '.join(summary)}")

    # Processed IDs / storage
    @hmonitor.command(name="setmaxprocessed")
    @commands.admin_or_permissions(manage_guild=True)
    async def setmaxprocessed(self, ctx: commands.Context, max_items: int):
        """Set maximum number of processed forum thread IDs stored to control storage usage."""
        if max_items < 10:
            await ctx.send("max_processed must be at least 10")
            return
        await self.config.guild(ctx.guild).max_processed.set(max_items)
        await ctx.send(f"max_processed set to {max_items}")

    @hmonitor.command(name="processedcount")
    @commands.admin_or_permissions(manage_guild=True)
    async def processedcount(self, ctx: commands.Context):
        processed = await self.config.guild(ctx.guild).processed_ids()
        cnt = len(processed) if processed else 0
        await ctx.send(f"Stored processed thread IDs: {cnt}")

    # Manual checks and status
    @hmonitor.command(name="checknow")
    @commands.admin_or_permissions(manage_guild=True)
    async def checknow(self, ctx: commands.Context):
        """Run a manual check now in this guild."""
        await ctx.send("Running manual check...")

        try:
            # Get configuration
            categories = await self.config.guild(ctx.guild).forum_categories()
            if not categories:
                await ctx.send("❌ No forum categories configured.")
                return

            # Run one monitoring cycle
            await self._monitor_categories(ctx.guild, categories)
            await ctx.send("✅ Manual check completed.")

        except Exception as e:
            LOGGER.exception("Error during manual check")
            await ctx.send(f"❌ Error during manual check: {str(e)}")

    @hmonitor.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        """Show current monitoring status and configuration for this guild."""
        enabled = await self.config.guild(ctx.guild).enabled()
        categories = await self.config.guild(ctx.guild).forum_categories()
        channel_id = await self.config.guild(ctx.guild).notify_channel_id()
        interval = await self.config.guild(ctx.guild).interval()
        threshold = await self.config.guild(ctx.guild).threshold()
        maxp = await self.config.guild(ctx.guild).max_processed()
        kw = await self.config.guild(ctx.guild).keywords()
        debug = await self.config.guild(ctx.guild).default_debug()

        # Check task status
        task = self._tasks.get(ctx.guild.id)
        if task and not task.done():
            task_status = "🟢 Running"
        elif task and task.done():
            task_status = "🔴 Stopped (task completed/failed)"
        else:
            task_status = "🔴 Not running"

        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        lines = [
            f"**Hypixel Monitor Status**",
            f"Enabled: {enabled}",
            f"Task Status: {task_status}",
            f"Channel: {channel.mention if channel else 'Not set'}",
            f"Forum Categories: {len(categories)}",
            f"Interval: {interval}s",
            f"Threshold: {threshold}",
            f"Debug Mode: {debug}",
            f"Max processed stored: {maxp}",
            f"Keywords: higher={len(kw.get('higher') or [])}, normal={len(kw.get('normal') or [])}, lower={len(kw.get('lower') or [])}, negative={len(kw.get('negative') or [])}",
        ]

        await ctx.send("\n".join(lines))

    @hmonitor.command(name="restart")
    @commands.admin_or_permissions(manage_guild=True)
    async def restart(self, ctx: commands.Context):
        """Restart the monitoring task for this guild."""
        await ctx.send("Restarting monitoring task...")
        await self._stop_task(ctx.guild)
        await asyncio.sleep(1)  # Give it a moment to clean up
        await self._ensure_task(ctx.guild)
        await ctx.send("✅ Monitoring task restarted.")

    # Test detection
    @hmonitor.command(name="testdetect")
    @commands.admin_or_permissions(manage_guild=True)
    async def testdetect(self, ctx: commands.Context, *, title_and_body: str):
        """Test the detection algorithm with a sample title (and optional body separated by '\\n')."""
        if "\n" in title_and_body:
            title, body = title_and_body.split("\n", 1)
        else:
            title, body = title_and_body, ""
        keywords = await self.config.guild(ctx.guild).keywords()
        detect = self._match_score(title, body, keywords)
        lines = [f"Immediate match: {detect['immediate']}", f"Score: {detect['score']}", "Matches:"]
        for lvl, vals in detect["matches"].items():
            lines.append(f"  {lvl}: {', '.join(vals) if vals else 'None'}")
        await ctx.send("\n".join(lines))

    @hmonitor.command(name="debugmode")
    @commands.admin_or_permissions(manage_guild=True)
    async def debugmode(self, ctx: commands.Context, enabled: bool):
        """Enable or disable debug mode (sends 'alive' messages when no matches found)."""
        await self.config.guild(ctx.guild).default_debug.set(enabled)
        await ctx.send(f"Debug mode set to: {enabled}")

    @hmonitor.command(name="tune")
    @commands.admin_or_permissions(manage_guild=True)
    async def tune_detection(self, ctx: commands.Context, category_name: str = None, limit: int = 10):
        """Test detection on recent threads from a forum category to tune accuracy."""
        categories = await self.config.guild(ctx.guild).forum_categories()

        if category_name:
            # Find specific category
            category = next((cat for cat in categories if cat['name'].lower() == category_name.lower()), None)
            if not category:
                await ctx.send(
                    f"Category '{category_name}' not found. Available categories: {', '.join(cat['name'] for cat in categories)}")
                return
            test_categories = [category]
        else:
            # Use first category if none specified
            if not categories:
                await ctx.send("No forum categories configured.")
                return
            test_categories = [categories[0]]

        keywords = await self.config.guild(ctx.guild).keywords()
        threshold = await self.config.guild(ctx.guild).threshold()
        session = await self._get_session(ctx.guild)

        try:
            results = []

            for category in test_categories:
                threads = await self._get_recent_threads(session, category)

                for i, thread in enumerate(threads[:limit]):
                    if not thread['content']:
                        thread['content'] = await self._get_thread_content(session, thread['url'])

                    title = thread['title'] or ""
                    body = thread['content'] or ""

                    detect = self._match_score(title, body, keywords)
                    would_notify = await self._should_notify(thread, detect, ctx.guild)

                    results.append({
                        "title": title[:50] + ("..." if len(title) > 50 else ""),
                        "score": detect["score"],
                        "notify": would_notify,
                        "matches": sum(len(v) for v in detect["matches"].values())
                    })

            # Format results
            msg = f"**Detection Test Results**\n```\n"
            msg += f"{'Title':<52} {'Score':<6} {'Notify':<6} {'Matches'}\n"
            msg += "-" * 75 + "\n"

            for r in results:
                notify_icon = "✓" if r["notify"] else "✗"
                msg += f"{r['title']:<52} {r['score']:<6.1f} {notify_icon:<6} {r['matches']}\n"

            msg += "```"

            for page in pagify(msg):
                await ctx.send(page)

        except Exception as e:
            await ctx.send(f"Error testing detection: {e}")

    # Additional debugging commands
    @hmonitor.command(name="taskinfo")
    @commands.admin_or_permissions(manage_guild=True)
    async def taskinfo(self, ctx: commands.Context):
        """Show detailed information about the monitoring task."""
        task = self._tasks.get(ctx.guild.id)

        if not task:
            await ctx.send("❌ No monitoring task exists for this guild.")
            return

        lines = [
            f"**Task Information for Guild {ctx.guild.id}**",
            f"Task exists: ✅ Yes",
            f"Task done: {'✅ Yes' if task.done() else '❌ No'}",
            f"Task cancelled: {'✅ Yes' if task.cancelled() else '❌ No'}",
        ]

        if task.done():
            try:
                exception = task.exception()
                if exception:
                    lines.append(f"Exception: {type(exception).__name__}: {exception}")
                else:
                    lines.append("Completed normally")
            except asyncio.InvalidStateError:
                lines.append("Task state unknown")

        # Show lock status
        has_lock = ctx.guild.id in self._task_locks
        lines.append(f"Has task lock: {'✅ Yes' if has_lock else '❌ No'}")

        # Show session status
        has_session = ctx.guild.id in self._sessions
        lines.append(f"Has HTTP session: {'✅ Yes' if has_session else '❌ No'}")

        await ctx.send("\n".join(lines))

    @hmonitor.command(name="cleartasks")
    @commands.is_owner()
    async def cleartasks(self, ctx: commands.Context):
        """[Owner Only] Clear all monitoring tasks and restart them."""
        await ctx.send("🔄 Clearing all monitoring tasks...")

        # Cancel all tasks
        tasks_cancelled = 0
        for guild_id, task in list(self._tasks.items()):
            if not task.cancelled():
                task.cancel()
                tasks_cancelled += 1

        # Wait for cancellation
        if tasks_cancelled > 0:
            await asyncio.sleep(2)

        # Clean up all guild tasks
        guilds_cleaned = len(self._tasks)
        for guild_id in list(self._tasks.keys()):
            await self._cleanup_guild_task(guild_id)

        await ctx.send(f"✅ Cleared {tasks_cancelled} tasks and cleaned up {guilds_cleaned} guilds.")

        # Restart tasks for enabled guilds
        await self._startup_tasks()
        await ctx.send("✅ Restarted monitoring tasks for enabled guilds.")