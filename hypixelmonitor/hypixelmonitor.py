import traceback

import discord
import aiohttp
import asyncio
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify
from redbot.core.utils.predicates import MessagePredicate

log = logging.getLogger("red.hypixelmonitor")


class HypixelMonitor(commands.Cog):
    """Monitor Hypixel Forums for mod-related questions and technical help requests."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)

        # Default configuration
        default_guild = {
            "enabled": False,
            "channel": None,
            "check_interval": 300,  # 5 minutes in seconds
            "processed_posts": [],
            "max_processed_posts": 1000,
            "forum_categories": [
                {
                    "url": "https://hypixel.net/forums/skyblock.157/",
                    "name": "SkyBlock General"
                },
                {
                    "url": "https://hypixel.net/forums/skyblock-community-help.196/",
                    "name": "SkyBlock Community Help"
                }
            ],
            "primary_keywords": [
                # Core mod terms
                "mod", "mods", "modpack", "modpacks", "forge", "fabric", "configs", "config",
                
                # 1.21.5 Skyblock Mods
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
            ],
            "high_priority_keywords": [
                # Special high-priority keywords that should trigger immediately
                "skyblock enhanced", "sb enhanced", "kd_gaming1", "kdgaming1", "kdgaming", "packcore", "scale me", "scaleme"
            ],
            "secondary_keywords": [
                # Technical terms
                "modification", "skyblock addons", "not enough updates", "texture pack", "resource pack",
                "shader", "shaders", "optifine", "optimization", "optimize",
                
                # Performance issues
                "fps boost", "performance", "lag", "frames", "frame rate", "fps", "stuttering",
                "freezing", "crash", "crashing", "memory", "ram", "cpu", "gpu", "graphics",
                "low fps", "bad performance", "slow", "choppy", "frame drops",
                
                # PC/Technical problems
                "pc problem", "computer issue", "technical issue", "troubleshoot", "fix",
                "error", "bug", "glitch", "not working", "broken", "install", "installation",
                "setup", "configure", "configuration", "compatibility", "java", "minecraft",
                
                # Modding terms
                "modding", "modded", "forge", "fabric", "loader", "api", "addon", "plugin",
                "enhancement", "tweak", "utility", "tool", "helper",

                "modification", "addon", "plugin", "enhancement", "tweak", "utility", "tool", "helper",
                "fps boost", "performance", "lag", "frames", "frame rate", "fps", "stuttering",
                "freezing", "crash", "crashing", "memory", "ram", "cpu", "gpu", "graphics",
                "pc problem", "computer issue", "technical issue", "troubleshoot", "fix",
                "error", "bug", "glitch", "not working", "broken", "install", "installation",
                "setup", "configure", "configuration", "compatibility", "java", "minecraft"
            ],
            "negative_keywords": [
                # Game content (not technical)
                "minion", "coins", "coin", "dungeon master", "catacombs", "weapon", "armor", 
                "items", "item", "pets", "pet", "talismans", "talisman", "accessories",
                "slayer", "dragon", "farm", "farming", "mining", "netherstar", "auction",
                "bazaar price", "worth", "sell", "buy", "trade", "trading", "money",
                "profile", "skills", "skill", "collection", "collections", "recipe",
                "enchant", "enchanting", "reforge", "gem", "gems", "crystal", "crystals"
            ],
            "detection_threshold": 3.0
        }

        self.config.register_guild(**default_guild)

        # Task management
        self.monitor_tasks: Dict[int, asyncio.Task] = {}
        self.session: Optional[aiohttp.ClientSession] = None

        # User agent for web requests
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

        # Start monitoring for guilds that have it enabled
        self.bot.loop.create_task(self.initialize_monitoring())

    def cog_unload(self):
        """Clean up when cog is unloaded."""
        # Cancel all monitoring tasks
        for task in self.monitor_tasks.values():
            task.cancel()

        # Close aiohttp session
        if self.session:
            asyncio.create_task(self.session.close())

    async def initialize_monitoring(self):
        """Initialize monitoring for all guilds that have it enabled"""
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            if await self.config.guild(guild).enabled():
                await self.start_monitoring(guild)

    async def start_monitoring(self, guild):
        """Start monitoring for a guild"""
        if guild.id in self.monitor_tasks:
            return

        task = asyncio.create_task(self.monitor_task(guild.id))
        self.monitor_tasks[guild.id] = task
        log.info(f"Started monitoring for guild {guild.id}")

    async def stop_monitoring(self, guild):
        """Stop monitoring for a guild"""
        if guild.id in self.monitor_tasks:
            self.monitor_tasks[guild.id].cancel()
            del self.monitor_tasks[guild.id]
            log.info(f"Stopped monitoring for guild {guild.id}")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session

    async def is_mod_question(self, title: str, content: str = "", guild_id: int = None) -> bool:
        """Improved detection using scoring system with config-based keywords only."""
        if guild_id:
            config = await self.config.guild_from_id(guild_id).all()
        else:
            config = await self.config.get_raw()

        title_lower = title.lower() if title else ""
        content_lower = content.lower() if content else ""

        # Initialize score
        score = 0

        # Get keywords from config (not hardcoded)
        high_priority_keywords = config.get("high_priority_keywords", [])
        primary_keywords = config.get("primary_keywords", [])
        secondary_keywords = config.get("secondary_keywords", [])
        negative_keywords = config.get("negative_keywords", [])

        # HIGH PRIORITY: Check config-based high priority keywords
        for keyword in high_priority_keywords:
            if keyword in title_lower or (content_lower and keyword in content_lower):
                log.info(f"High priority keyword '{keyword}' found in post: {title}")
                return True

        # Use only config-based keywords for all checks
        for keyword in primary_keywords:
            if keyword in title_lower:
                score += 3
            if content_lower and keyword in content_lower:
                score += 2

        # Continue with secondary keywords from config...
        for keyword in secondary_keywords:
            if keyword in title_lower:
                score += 2
            if content_lower and keyword in content_lower:
                score += 1

        # Check negative keywords from config
        negative_score = 0
        for keyword in negative_keywords:
            if keyword in title_lower:
                negative_score += 1
            if content_lower and keyword in content_lower:
                negative_score += 0.5

        final_score = score - (negative_score * 1.5)
        threshold = config.get("detection_threshold", 3.0)

        return final_score >= threshold

    def extract_thread_id_from_class(self, class_str: str) -> Optional[str]:
        """Extract thread ID from class attribute."""
        if not class_str:
            return None

        match = re.search(r'js-threadListItem-(\d+)', class_str)
        if match:
            return match.group(1)
        return None

    async def get_thread_content(self, thread_url: str) -> str:
        """Get the content of a thread."""
        session = await self._get_session()
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
            log.warning(f"Error fetching thread content from {thread_url}: {e}")

        return ""

    async def get_recent_threads(self, category: Dict[str, str]) -> List[Dict[str, str]]:
        """Get recent threads from a forum category."""
        session = await self._get_session()
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
                            thread_id = self.extract_thread_id_from_class(class_str)

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
                                'content': ''
                            })
                        except Exception as e:
                            log.warning(f"Error parsing thread item: {e}")
                            continue

        except Exception as e:
            log.error(f"Error fetching threads from {category['name']}: {e}")

        return threads

    async def send_notification(self, guild: discord.Guild, thread_data: dict):
        """Send notification about a new mod question"""
        channel_id = await self.config.guild(guild).channel()
        if not channel_id:
            log.warning(f"No notification channel set for guild {guild.id}")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            log.error(f"Could not find channel {channel_id} for guild {guild.id}")
            return

        try:
            embed = discord.Embed(
                title=thread_data['title'],
                url=thread_data['url'],
                description=thread_data['content'] if thread_data['content'] else "No content preview available",
                color=discord.Color.orange(),
                timestamp=datetime.now()
            )

            embed.set_footer(text=f"Posted in {thread_data['category']} • Hypixel Forums")

            await channel.send(f"New mod question in **{thread_data['category']}**:", embed=embed)

        except discord.HTTPException as e:
            log.error(f"Failed to send notification to channel {channel_id}: {e}")
        except Exception as e:
            log.error(f"Unexpected error sending notification: {e}")

    async def check_forums(self, guild: discord.Guild):
        """Check Hypixel forums for new mod questions"""
        guild_config = self.config.guild(guild)
        categories = await guild_config.forum_categories()

        # Get processed posts as list and create set for fast lookup
        current_processed_list = await guild_config.processed_posts()
        processed_posts_set = set(str(pid) for pid in current_processed_list)

        newly_processed = []

        log.info(f"Monitoring {len(categories)} forum categories with {len(processed_posts_set)} processed posts")

        for category in categories:
            try:
                threads = await self.get_recent_threads(category)

                for thread in threads:
                    thread_id = str(thread['id'])

                    if thread_id in processed_posts_set:
                        log.debug(f"Skipping processed thread {thread_id}")
                        continue

                    log.info(f"Found new thread {thread_id}: '{thread['title'][:50]}'")
                    newly_processed.append(thread_id)

                    # Check if it's a mod question
                    if await self.is_mod_question(thread['title'], thread.get('content', ''), guild.id):
                        await self.send_notification(guild, thread)
                        log.info(f"Sent notification for thread: {thread['title']}")

            except Exception as e:
                log.error(f"Error checking category {category.get('name', 'Unknown')}: {e}")
                continue

        # Save processed posts with proper merging
        if newly_processed:
            # Merge and deduplicate
            all_processed = list(processed_posts_set) + newly_processed
            all_processed = list(dict.fromkeys(all_processed))  # Remove duplicates, preserve order

            # Keep only last max_processed_posts
            max_posts = await guild_config.max_processed_posts()
            if len(all_processed) > max_posts:
                all_processed = all_processed[-max_posts:]

            await guild_config.processed_posts.set(all_processed)
            log.info(f"Updated processed posts: added {len(newly_processed)}, total {len(all_processed)}")

    async def monitor_task(self, guild_id: int):
        """Main monitoring task for a guild"""
        while True:
            try:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    break

                guild_config = self.config.guild(guild)
                if not await guild_config.enabled():
                    break

                await self.check_forums(guild)  # This method needs to exist

                interval = await guild_config.check_interval()
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                log.info(f"Monitoring task cancelled for guild {guild_id}")
                break
            except Exception as e:
                log.error(f"Error in monitoring loop for guild {guild_id}: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
                await asyncio.sleep(60)  # Wait before retrying

    @commands.group(name="hypixelmonitor", aliases=["hm"])
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def hypixelmonitor(self, ctx):
        """Hypixel Forums monitoring commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @hypixelmonitor.command(name="setup")
    async def setup(self, ctx):
        """Set up the Hypixel Forums monitor."""
        embed = discord.Embed(
            title="Hypixel Forums Monitor Setup",
            description="The Hypixel Forums monitor will watch for mod-related questions and technical help requests.",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Next Steps",
            value="1. Set a notification channel: `[p]hypixelmonitor channel #channel`\n"
                  "2. Enable monitoring: `[p]hypixelmonitor toggle`\n"
                  "3. Check status: `[p]hypixelmonitor status`",
            inline=False
        )

        await ctx.send(embed=embed)

    @hypixelmonitor.command(name="channel")
    async def set_channel(self, ctx, channel: discord.TextChannel = None):
        """Set the notification channel for Hypixel Forums alerts."""
        if not channel:
            channel = ctx.channel

        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"✅ Notification channel set to {channel.mention}")

    @hypixelmonitor.command(name="category")
    async def category_manage(self, ctx, action: str, *, category_info: str = None):
        """Manage forum categories to monitor.

        Actions: add, remove, list
        For add: `[p]hypixelmonitor category add <url> <name>`
        For remove: `[p]hypixelmonitor category remove <name>`
        """
        if action.lower() == "list":
            categories = await self.config.guild(ctx.guild).forum_categories()
            if not categories:
                await ctx.send("No forum categories configured.")
                return

            embed = discord.Embed(title="Monitored Forum Categories", color=discord.Color.blue())
            for cat in categories:
                embed.add_field(name=cat['name'], value=cat['url'], inline=False)

            await ctx.send(embed=embed)

        elif action.lower() == "add":
            if not category_info:
                await ctx.send("Please provide URL and name: `[p]hypixelmonitor category add <url> <name>`")
                return

            parts = category_info.split(' ', 1)
            if len(parts) != 2:
                await ctx.send("Please provide both URL and name: `[p]hypixelmonitor category add <url> <name>`")
                return

            url, name = parts
            categories = await self.config.guild(ctx.guild).forum_categories()
            categories.append({"url": url, "name": name})
            await self.config.guild(ctx.guild).forum_categories.set(categories)
            await ctx.send(f"Added forum category: {name}")

        elif action.lower() == "remove":
            if not category_info:
                await ctx.send("Please provide the category name to remove.")
                return

            categories = await self.config.guild(ctx.guild).forum_categories()
            categories = [cat for cat in categories if cat['name'] != category_info]
            await self.config.guild(ctx.guild).forum_categories.set(categories)
            await ctx.send(f"Removed forum category: {category_info}")

        else:
            await ctx.send("Invalid action. Use: add, remove, or list")

    @hypixelmonitor.command(name="keywords")
    async def manage_keywords(self, ctx, keyword_type: str, action: str, *, keyword: str = None):
        """Manage detection keywords.

        Types: primary, secondary, negative
        Actions: add, remove, list
        """
        valid_types = ["primary", "secondary", "negative"]
        if keyword_type not in valid_types:
            await ctx.send(f"Invalid keyword type. Use: {', '.join(valid_types)}")
            return

        config_key = f"{keyword_type}_keywords"

        if action.lower() == "list":
            keywords = await self.config.guild(ctx.guild).get_raw(config_key)
            if not keywords:
                await ctx.send(f"No {keyword_type} keywords configured.")
                return

            embed = discord.Embed(title=f"{keyword_type.title()} Keywords", color=discord.Color.blue())
            embed.description = ", ".join(keywords)
            await ctx.send(embed=embed)

        elif action.lower() == "add":
            if not keyword:
                await ctx.send("Please provide a keyword to add.")
                return

            keywords = await self.config.guild(ctx.guild).get_raw(config_key)
            if keyword not in keywords:
                keywords.append(keyword)
                await self.config.guild(ctx.guild).set_raw(config_key, value=keywords)
                await ctx.send(f"Added {keyword_type} keyword: {keyword}")
            else:
                await ctx.send(f"Keyword '{keyword}' already exists in {keyword_type} keywords.")

        elif action.lower() == "remove":
            if not keyword:
                await ctx.send("Please provide a keyword to remove.")
                return

            keywords = await self.config.guild(ctx.guild).get_raw(config_key)
            if keyword in keywords:
                keywords.remove(keyword)
                await self.config.guild(ctx.guild).set_raw(config_key, value=keywords)
                await ctx.send(f"Removed {keyword_type} keyword: {keyword}")
            else:
                await ctx.send(f"Keyword '{keyword}' not found in {keyword_type} keywords.")

        else:
            await ctx.send("Invalid action. Use: add, remove, or list")

    @hypixelmonitor.command(name="status")
    async def status(self, ctx):
        """Show current monitoring status."""
        config = await self.config.guild(ctx.guild).all()

        embed = discord.Embed(
            title="Hypixel Forums Monitor Status",
            color=discord.Color.green() if config['enabled'] else discord.Color.red()
        )

        # Basic status
        status = "🟢 Enabled" if config['enabled'] else "🔴 Disabled"
        embed.add_field(name="Status", value=status, inline=True)

        # Channel
        channel_id = config['channel']
        if channel_id:
            channel = ctx.guild.get_channel(channel_id)
            channel_text = f"<#{channel_id}>" if channel else "Not set"
        else:
            channel_text = "Not set"
        embed.add_field(name="Channel", value=channel_text, inline=True)

        # Check interval
        embed.add_field(name="Interval", value=f"{config['check_interval']}s", inline=True)

        # Categories
        embed.add_field(name="Forum Categories", value=str(len(config['forum_categories'])), inline=True)

        # Keywords
        embed.add_field(name="Primary Keywords", value=str(len(config['primary_keywords'])), inline=True)
        embed.add_field(name="Detection Threshold", value=str(config['detection_threshold']), inline=True)

        # Processed posts
        embed.add_field(name="Processed Posts", value=str(len(config['processed_posts'])), inline=True)

        # Task status
        task_status = "Running" if ctx.guild.id in self.monitor_tasks else "Stopped"
        embed.add_field(name="Task Status", value=task_status, inline=True)

        await ctx.send(embed=embed)

    @hypixelmonitor.command(name="toggle")
    async def toggle(self, ctx):
        """Toggle monitoring on/off."""
        config = await self.config.guild(ctx.guild).all()

        if config['enabled']:
            # Stop monitoring
            await self.config.guild(ctx.guild).enabled.set(False)
            await self.stop_monitoring(ctx.guild)
            await ctx.send("✅ Monitoring disabled")
        else:
            # Start monitoring
            if not config['channel']:
                await ctx.send("❌ Please set a notification channel first using `hypixelmonitor channel`")
                return

            await self.config.guild(ctx.guild).enabled.set(True)
            await self.start_monitoring(ctx.guild)
            await ctx.send("✅ Monitoring enabled")

    @hypixelmonitor.command(name="check")
    async def manual_check(self, ctx):
        """Manually check for new mod questions."""
        config = await self.config.guild(ctx.guild).all()

        if not config['channel']:
            await ctx.send("❌ Please set a notification channel first using `hypixelmonitor channel`")
            return

        await ctx.send("🔍 Checking for new mod questions...")
        
        try:
            await self.monitor_forums(ctx.guild.id)
            await ctx.send("✅ Manual check completed!")
        except Exception as e:
            await ctx.send(f"❌ Error during manual check: {e}")
            log.error(f"Manual check error for guild {ctx.guild.id}: {e}")

    @hypixelmonitor.command(name="test")
    async def test_detection(self, ctx, *, post_title: str):
        """Test the mod detection algorithm on a post title."""
        is_mod = await self.is_mod_question(post_title, guild_id=ctx.guild.id)

        result = "✅ Would be detected" if is_mod else "❌ Would not be detected"
        await ctx.send(f"**Test Result:** {result}\n**Title:** {post_title}")

    @hypixelmonitor.command(name="interval")
    async def set_interval(self, ctx, seconds: int):
        """Set the check interval in seconds (minimum 60)."""
        if seconds < 60:
            await ctx.send("❌ Interval must be at least 60 seconds")
            return

        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(f"✅ Check interval set to {seconds} seconds")

    @hypixelmonitor.command(name="threshold")
    async def set_threshold(self, ctx, threshold: float):
        """Set the detection threshold (1.0-10.0)."""
        if not 1.0 <= threshold <= 10.0:
            await ctx.send("❌ Threshold must be between 1.0 and 10.0")
            return

        await self.config.guild(ctx.guild).detection_threshold.set(threshold)
        await ctx.send(f"✅ Detection threshold set to {threshold}")