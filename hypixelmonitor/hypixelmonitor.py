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
                "skyblock enhanced", "sb enhanced"
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
                "enhancement", "tweak", "utility", "tool", "helper"
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

    def cog_unload(self):
        """Clean up when cog is unloaded."""
        # Cancel all monitoring tasks
        for task in self.monitor_tasks.values():
            task.cancel()

        # Close aiohttp session
        if self.session:
            asyncio.create_task(self.session.close())

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session

    async def is_mod_question(self, title: str, content: str = "", guild_id: int = None) -> bool:
        """
        Improved detection using scoring system with updated keywords.
        Based on the proven old detection algorithm.
        """
        if guild_id:
            config = await self.config.guild_from_id(guild_id).all()
        else:
            config = await self.config.get_raw()

        title_lower = title.lower() if title else ""
        content_lower = content.lower() if content else ""

        # Initialize score
        score = 0

        # HIGH PRIORITY: SB Enhanced / Skyblock Enhanced - immediate match
        high_priority_keywords = config.get("high_priority_keywords", [])
        for keyword in high_priority_keywords:
            if keyword in title_lower or (content_lower and keyword in content_lower):
                log.info(f"High priority keyword '{keyword}' found in post: {title}")
                return True

        # Check if the post explicitly asks what mod something is from
        explicit_mod_question = re.search(r'what\s+mod\s+is', title_lower) or re.search(r'what\s+mod\s+is', content_lower)
        if explicit_mod_question:
            log.info(f"Explicit mod question detected in post: '{title}'")
            return True

        # Primary mod keywords with word boundaries (updated list)
        primary_mod_keywords = [
            # Core terms
            r'\bmod\b', r'\bmods\b', r'\bmodpack\b', r'\bmodpacks\b',
            r'\bforge\b', r'\bfabric\b', r'\bconfigs?\b',
            
            # 1.21.5 Skyblock Mods
            r'\bfirmament\b', r'skyblock tweaks', r'modern warp menu', r'skyblockaddons unofficial',
            r'\bskyhanni\b', r'hypixel mod api', r'\bskyocean\b', r'skyblock profile viewer', r'bazaar utils',
            r'\bskyblocker\b', r'cookies-mod', r"aaron's mod", r'custom scoreboard', r'\bskycubed\b',
            r'\bnofrills\b', r'\bnobaaddons\b', r'sky cubed', r'\bdulkirmod\b', r'skyblock 21', r'\bskycofl\b',
            
            # 1.8.9 Skyblock Mods
            r'\bnotenoughupdates\b', r'\bneu\b', r'\bpolysprint\b', r'\bskyblockaddons\b', r'\bsba\b', 
            r'\bpolypatcher\b', r'hypixel plus', r'\bfurfsky\b', r'dungeons guide', r'\bskyguide\b', 
            r'partly sane skies', r'secret routes mod',
            
            # Performance Mods
            r'more culling', r'\bbadoptimizations\b', r'concurrent chunk management', r'very many players',
            r'\bthreadtweak\b', r'\bscalablelux\b', r'particle core', r'\bsodium\b', r'\blithium\b', r'\biris\b',
            r'entity culling', r'\bferritecore\b', r'\bimmediatelyfast\b',
            
            # QoL Mods
            r'scrollable tooltips', r'fzzy config', r'no chat reports', r'no resource pack warnings',
            r'auth me', r'\bbetterf3\b', r'scale me', r'\bpackcore\b', r'no double sneak', r'centered crosshair',
            r'\bcontinuity\b', r'3d skin layers', r'wavey capes', r'sound controller', r'cubes without borders',
            r'sodium shadowy path blocks',
            
            # Popular Clients/Launchers
            r'\bladymod\b', r'\blaby\b', r'\bskytils\b', r'\bbadlion\b', r'\blunar\b', 
            r'\bessential\b', r'\blunarclient\b', r'\bclient\b', r'\bfeather\b'
        ]

        # Secondary mod keywords (less certain but still relevant)
        secondary_mod_keywords = [
            r'modification', r'skyblock addons', r'not enough updates',
            r'fps boost', r'performance', r'lag', r'frames', r'frame rate',
            r'configs', r'settings', r'texture pack', r'resource pack',
            r'shader', r'shaders', r'optifine', r'optimization', r'optimize',
            r'stuttering', r'freezing', r'crash', r'crashing', r'memory', r'ram', r'cpu', r'gpu',
            r'low fps', r'bad performance', r'slow', r'choppy', r'frame drops',
            r'pc problem', r'computer issue', r'technical issue', r'troubleshoot', r'fix',
            r'error', r'bug', r'glitch', r'not working', r'broken', r'install', r'installation',
            r'setup', r'configure', r'configuration', r'compatibility', r'java', r'minecraft',
            r'modding', r'modded', r'loader', r'api', r'addon', r'plugin',
            r'enhancement', r'tweak', r'utility', r'tool', r'helper'
        ]

        # Mod question patterns with stronger contextual indicators
        mod_question_patterns = [
            r'(?:recommend|suggest)(?:ed)?\s+(?:.*?)\s+(?:mod|mods|modpack)',
            r'(?:what|which|best)\s+(?:.*?)\s+(?:mod|mods|modpack)',
            r'(?:help|issue|problem)\s+(?:with|using)\s+(?:.*?)\s+(?:mod|mods)',
            r'(?:how\s+to\s+(?:install|setup|configure|use))\s+(?:.*?)\s+(?:mod|mods)',
            r'(?:can\'?t\s+get)\s+(?:.*?)\s+(?:mod|mods)\s+(?:to\s+work)',
            r'(?:looking\s+for)\s+(?:.*?)\s+(?:mod|mods)',
            r'(?:need|want)\s+(?:.*?)\s+(?:mod|mods)',
            r'(?:low|bad)\s+(?:fps|frames|performance)',
            r'performance\s+(?:issue|problem|boost)',
            r'increase\s+(?:fps|performance)',
            r'fixing\s+(?:lag|stutter|freeze)',
            r'(?:fps|performance)\s+(?:boost|increase|improve)',
            r'(?:lag|stutter|freeze)\s+(?:fix|help|issue)',
            r'(?:crash|crashing)\s+(?:with|when\s+using)\s+(?:mod|mods)',
            r'(?:java|minecraft)\s+(?:error|crash|issue)',
            r'(?:config|configuration)\s+(?:help|issue|problem)',
            r'(?:texture|resource)\s+pack\s+(?:not\s+working|issue|help)',
            r'(?:pc|computer)\s+(?:problem|issue|trouble)',
            r'(?:technical|tech)\s+(?:issue|problem|help)'
        ]

        # Negative keywords that suggest the post is NOT about mods
        negative_keywords = [
            r'\bminion\b', r'\bcoins?\b', r'\bdungeon\b', r'\bf[0-9]\b',
            r'\bnetherstar\b', r'\bweapon\b', r'\barmor\b', r'\bitems?\b', 
            r'\bpets?\b', r'\btalismans?\b', r'\bslayer\b', r'\bdragon\b',
            r'\bfarm\b', r'\bmining\b', r'\bauction\b', r'\bbazaar\b',
            r'\bworth\b', r'\bsell\b', r'\bbuy\b', r'\btrade\b', r'\btrading\b',
            r'\bmoney\b', r'\bprofile\b', r'\bskills?\b', r'\bcollections?\b',
            r'\brecipe\b', r'\benchant\b', r'\benchanting\b', r'\breforge\b',
            r'\bgems?\b', r'\bcrystals?\b', r'\bmagic\b', r'\bspell\b', 
            r'\bmana\b', r'\bintelligence\b'
        ]

        # Check primary mod keywords in title (highest confidence)
        for keyword in primary_mod_keywords:
            if re.search(keyword, title_lower):
                log.debug(f"Primary keyword match in title: {keyword} in '{title}'")
                score += 3

        # Check mod question patterns in title
        for pattern in mod_question_patterns:
            if re.search(pattern, title_lower):
                log.debug(f"Question pattern matched in title: {pattern} in '{title}'")
                score += 4

        # Check secondary keywords in title
        for keyword in secondary_mod_keywords:
            if keyword in title_lower:
                log.debug(f"Secondary keyword match in title: {keyword} in '{title}'")
                score += 2

        # Check patterns in content
        if content_lower:
            # Primary keywords in content
            for keyword in primary_mod_keywords:
                if re.search(keyword, content_lower):
                    log.debug(f"Primary keyword match in content: {keyword}")
                    score += 2

            # Question patterns in content
            for pattern in mod_question_patterns:
                if re.search(pattern, content_lower):
                    log.debug(f"Question pattern matched in content: {pattern}")
                    score += 3

            # Secondary keywords in content
            for keyword in secondary_mod_keywords:
                if keyword in content_lower:
                    log.debug(f"Secondary keyword match in content: {keyword}")
                    score += 1

        # Check for negative keywords that suggest the post is NOT about mods
        negative_score = 0
        for keyword in negative_keywords:
            if re.search(keyword, title_lower):
                negative_score += 1
            if content_lower and re.search(keyword, content_lower):
                negative_score += 0.5

        # Specific pattern checks for false positives
        common_false_positive_patterns = [
            r'sword', r'bow', r'armor', r'helmet', r'chestplate', r'leggings', r'boots',
            r'hypixel\s+should', r'admins?\s+should', r'staff\s+should',
            r'rank\s+(?:all|the|these)', r'(?:texture|skin)\s+(?:pack|review|showcase)',
            r'(?:store|payment|purchase|buy)', r'(?:boosting|profile)\s+(?:\?|question)',
            r'what\s+can\s+be\s+done\s+about', r'(?:campfire|trial|badge|npc)',
            r'(?:hyperion|valkyrie|astrea|scylla)\s+(?:texture|review|comparison)'
        ]

        for pattern in common_false_positive_patterns:
            if re.search(pattern, title_lower):
                negative_score += 1.5

        # Apply negative score to reduce false positives
        final_score = score - (negative_score * 1.5)

        # Log the scoring results for debugging
        log.debug(f"Hypixel mod detection scores for '{title}' - Positive: {score}, Negative: {negative_score}, Final: {final_score}")

        # Return True if the final score exceeds the threshold
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

    async def send_notification(self, thread: Dict[str, str], guild_id: int):
        """Send a notification about a mod question to the configured channel."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel_id = await self.config.guild(guild).channel()
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        # Create embed
        embed = discord.Embed(
            title=thread['title'],
            url=thread['url'],
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )

        if thread['content']:
            description = thread['content'][:300] + "..." if len(thread['content']) > 300 else thread['content']
            embed.description = description

        embed.set_footer(text=f"Posted by {thread['author']} in {thread['category']}")

        try:
            await channel.send(f"New mod question in {thread['category']}:", embed=embed)
        except discord.HTTPException as e:
            log.error(f"Failed to send notification to {channel.id}: {e}")
        except Exception as e:
            log.error(f"Error sending notification: {e}")

    async def monitor_forums(self, guild_id: int):
        """Monitor forums for mod questions."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        config = await self.config.guild(guild).all()
        if not config['enabled']:
            return

        try:
            # Get processed posts
            processed_posts = set(config['processed_posts'])

            # Get all threads from all categories
            all_threads = []
            for category in config['forum_categories']:
                threads = await self.get_recent_threads(category)
                all_threads.extend(threads)

            # Process threads
            for thread in all_threads:
                if thread['id'] in processed_posts:
                    continue

                # Add to processed posts
                processed_posts.add(thread['id'])

                # Check if it's a mod question (first check title only)
                if await self.is_mod_question(thread['title'], guild_id=guild_id):
                    thread['content'] = await self.get_thread_content(thread['url'])
                    await self.send_notification(thread, guild_id)
                else:
                    # Check with content
                    thread['content'] = await self.get_thread_content(thread['url'])
                    if await self.is_mod_question(thread['title'], thread['content'], guild_id=guild_id):
                        await self.send_notification(thread, guild_id)

            # Update processed posts (keep only recent ones)
            processed_posts_list = list(processed_posts)
            if len(processed_posts_list) > config['max_processed_posts']:
                processed_posts_list = processed_posts_list[-config['max_processed_posts']:]

            await self.config.guild(guild).processed_posts.set(processed_posts_list)

        except Exception as e:
            log.error(f"Error monitoring forums for guild {guild_id}: {e}")

    async def monitor_task(self, guild_id: int):
        """Background task for monitoring forums."""
        while True:
            try:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    break

                config = await self.config.guild(guild).all()
                if not config['enabled']:
                    break

                await self.monitor_forums(guild_id)
                await asyncio.sleep(config['check_interval'])

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in monitor task for guild {guild_id}: {e}")
                await asyncio.sleep(60)  # Wait a minute before retrying

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
            if ctx.guild.id in self.monitor_tasks:
                self.monitor_tasks[ctx.guild.id].cancel()
                del self.monitor_tasks[ctx.guild.id]
            await ctx.send("✅ Monitoring disabled")
        else:
            # Start monitoring
            if not config['channel']:
                await ctx.send("❌ Please set a notification channel first using `hypixelmonitor channel`")
                return

            await self.config.guild(ctx.guild).enabled.set(True)

            # Start monitoring task
            task = asyncio.create_task(self.monitor_task(ctx.guild.id))
            self.monitor_tasks[ctx.guild.id] = task

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