import discord
import asyncio
import logging
import re
import traceback
from datetime import datetime
from typing import Optional, Dict, List, Set
import pytz
import asyncpraw

from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS

log = logging.getLogger("red.redditmonitor")


class RedditMonitor(commands.Cog):
    """
    Monitor Reddit for mod-related questions and support requests.

    This cog monitors specified subreddits for posts asking about mods,
    modpacks, or PC performance issues and sends notifications to Discord.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)

        # Default configuration structure
        default_guild = {
            "enabled": False,
            "channel_id": None,
            "reddit_credentials": {
                "client_id": None,
                "client_secret": None,
                "user_agent": "RedditMonitor/1.0 by RedBot"
            },
            "subreddits": ["HypixelSkyblock"],
            "check_interval": 300,  # 5 minutes
            "post_limit": 20,
            "timezone": "Europe/Oslo",
            "keywords": {
                "primary": [
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
                    "ladymod", "laby", "badlion", "lunar", "essential", "lunarclient", "client", "feather"

                ],
                "high_priority": [
                    # Special high-priority keywords that should trigger immediately
                    "skyblock enhanced", "sb enhanced", "kd_gaming1", "kdgaming1", "kdgaming", "packcore", "scale me", "scaleme"
                ],
                "secondary": [
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
                "question_patterns": [
                    r"(?:recommend|suggest)(?:ed)?\s+(?:any|some|good)?\s*(?:mod|mods|modpack)",
                    r"(?:what|which|best)\s+(?:.*?)\s+(?:mod|mods|modpack)",
                    r"(?:help|issue|problem)\s+(?:with|using)\s+(?:.*?)\s+(?:mod|mods)",
                    r"(?:how\s+to\s+(?:install|setup|configure|use))\s+(?:.*?)\s+(?:mod|mods)",
                    r"(?:can\'?t\s+get)\s+(?:.*?)\s+(?:mod|mods)\s+(?:to\s+work|working)",
                    r"(?:looking\s+for)\s+(?:a\s+)?(?:mod|mods|modpack)",
                    r"(?:need|want)\s+(?:a\s+)?(?:mod|mods|modpack)",
                    r"(?:fps|performance)\s+(?:boost|increase|improve)",
                    r"(?:low|bad|poor)\s+(?:fps|performance)",
                    r"(?:lag|stutter|freeze)\s+(?:fix|help|issue)",
                    r"(?:crash|crashing)\s+(?:with|when\s+using)\s+(?:mod|mods)",
                    r"(?:java|minecraft)\s+(?:error|crash|issue)",
                    r"(?:config|configuration)\s+(?:help|issue|problem)",
                    r"(?:texture|resource)\s+pack\s+(?:not\s+working|issue|help)",
                    r"(?:pc|computer)\s+(?:problem|issue|trouble)",
                    r"(?:technical|tech)\s+(?:issue|problem|help)"
                ],
                "negative": [
                    # Game content (not technical) - less aggressive
                    "minion", "coins", "coin", "dungeon master", "catacombs", "weapon", "armor", 
                    "items", "item", "pets", "pet", "talismans", "talisman", "accessories",
                    "slayer", "dragon", "farm", "farming", "mining", "netherstar", "auction",
                    "bazaar price", "worth", "sell", "buy", "trade", "trading", "money",
                    "profile", "skills", "skill", "collection", "collections", "recipe",
                    "enchant", "enchanting", "reforge", "gem", "gems", "crystal", "crystals"
                ]
            },
            "detection_threshold": 3.0,
            "processed_posts": []
        }

        self.config.register_guild(**default_guild)

        # Reddit client instances per guild
        self.reddit_clients: Dict[int, asyncpraw.Reddit] = {}

        # Task management
        self.monitor_tasks: Dict[int, asyncio.Task] = {}

        # Start monitoring for guilds that have it enabled
        self.bot.loop.create_task(self.initialize_monitoring())

    def cog_unload(self):
        """Clean up when the cog is unloaded"""
        for task in self.monitor_tasks.values():
            task.cancel()

    async def initialize_monitoring(self):
        """Initialize monitoring for all guilds that have it enabled"""
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            if await self.config.guild(guild).enabled():
                await self.start_monitoring(guild)

    async def create_reddit_client(self, guild: discord.Guild) -> Optional[asyncpraw.Reddit]:
        """Create a Reddit client for a guild"""
        if not asyncpraw:
            log.error("praw library not installed")
            return None

        guild_config = self.config.guild(guild)
        credentials = await guild_config.reddit_credentials()

        if not credentials["client_id"] or not credentials["client_secret"]:
            log.error(f"Reddit credentials not configured for guild {guild.id}")
            return None

        try:
            reddit_client = asyncpraw.Reddit(
                client_id=credentials["client_id"],
                client_secret=credentials["client_secret"],
                user_agent=credentials["user_agent"]
            )
            return reddit_client
        except Exception as e:
            log.error(f"Failed to create Reddit client for guild {guild.id}: {e}")
            return None

    async def is_mod_question(self, post, guild: discord.Guild) -> bool:
        """
        Improved detection using scoring system with updated keywords.
        Based on the proven old detection algorithm.
        """
        guild_config = self.config.guild(guild)
        keywords = await guild_config.keywords()

        # Extract title and selftext
        title = post.title if hasattr(post, 'title') else ""
        content = post.selftext if hasattr(post, 'selftext') else ""

        title_lower = title.lower()
        content_lower = content.lower()

        # Initialize score
        score = 0

        # HIGH PRIORITY: SB Enhanced / Skyblock Enhanced - immediate match
        high_priority_keywords = keywords.get("high_priority", [])
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
        log.debug(f"Reddit mod detection scores for '{title}' - Positive: {score}, Negative: {negative_score}, Final: {final_score}")

        # Return True if the final score exceeds the threshold
        threshold = 3.0
        return final_score >= threshold

    async def send_notification(self, guild: discord.Guild, post, subreddit_name: str):
        """Send a notification about a new mod question"""
        channel_id = await self.config.guild(guild).channel_id()
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        # Create embed
        embed = discord.Embed(
            title=post.title,
            url=f"https://www.reddit.com{post.permalink}",
            description=(post.selftext[:200] + "...") if len(post.selftext) > 200 else post.selftext,
            color=discord.Color.orange(),
            timestamp=datetime.now(pytz.UTC)
        )

        embed.set_footer(text=f"Posted by u/{post.author.name} in r/{subreddit_name}")

        try:
            await channel.send(f"New mod question in r/{subreddit_name}:", embed=embed)
        except discord.HTTPException as e:
            log.error(f"Failed to send notification to {channel.id}: {e}")

    async def monitor_subreddit(self, guild: discord.Guild):
        """Monitor a subreddit for mod questions"""
        reddit_client = self.reddit_clients.get(guild.id)
        if not reddit_client:
            reddit_client = await self.create_reddit_client(guild)
            if not reddit_client:
                return
            self.reddit_clients[guild.id] = reddit_client

        guild_config = self.config.guild(guild)
        subreddits = await guild_config.subreddits()
        post_limit = await guild_config.post_limit()

        # Get processed posts as list and create set for fast lookup
        current_processed_list = await guild_config.processed_posts()
        processed_posts_set = set(str(pid) for pid in current_processed_list)  # Ensure all strings

        newly_processed = []

        log.info(f"Monitoring {len(subreddits)} subreddits with {len(processed_posts_set)} processed posts")

        for subreddit_name in subreddits:
            try:
                subreddit = reddit_client.subreddit(subreddit_name)
                log.debug(f"Checking r/{subreddit_name} for new posts")

                for post in subreddit.new(limit=post_limit):
                    post_id = str(post.id)  # Ensure string type

                    # Enhanced logging for debugging
                    if post_id in processed_posts_set:
                        log.debug(f"Skipping processed post {post_id}")
                        continue

                    log.info(f"Found new post {post_id}: '{post.title[:50]}'")
                    newly_processed.append(post_id)

                    if await self.is_mod_question(post, guild):
                        await self.send_notification(guild, post, subreddit_name)
                        log.info(f"Sent notification for post: {post.title}")

            except Exception as e:
                log.error(f"Error monitoring r/{subreddit_name}: {e}")

        # Save processed posts with proper merging
        if newly_processed:
            # Merge and deduplicate
            all_processed = list(processed_posts_set) + newly_processed
            all_processed = list(dict.fromkeys(all_processed))  # Remove duplicates, preserve order

            # Keep only last 1000
            if len(all_processed) > 1000:
                all_processed = all_processed[-1000:]

            await guild_config.processed_posts.set(all_processed)
            log.info(f"Updated processed posts: added {len(newly_processed)}, total {len(all_processed)}")

    async def monitoring_loop(self, guild: discord.Guild):
        """Main monitoring loop for a guild"""
        while True:
            try:
                if not await self.config.guild(guild).enabled():
                    break

                await self.monitor_subreddit(guild)

                interval = await self.config.guild(guild).check_interval()
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in monitoring loop for guild {guild.id}: {e}")
                await asyncio.sleep(60)  # Wait before retrying

    async def start_monitoring(self, guild: discord.Guild):
        """Start monitoring for a guild"""
        if guild.id in self.monitor_tasks:
            return

        task = asyncio.create_task(self.monitoring_loop(guild))
        self.monitor_tasks[guild.id] = task
        log.info(f"Started monitoring for guild {guild.id}")

    async def stop_monitoring(self, guild: discord.Guild):
        """Stop monitoring for a guild"""
        if guild.id in self.monitor_tasks:
            self.monitor_tasks[guild.id].cancel()
            del self.monitor_tasks[guild.id]
            log.info(f"Stopped monitoring for guild {guild.id}")

    # Commands
    @commands.group(name="redditmonitor", aliases=["rm"])
    @checks.admin_or_permissions(manage_guild=True)
    async def redditmonitor(self, ctx):
        """Reddit monitoring commands"""
        pass

    @redditmonitor.command(name="setup")
    async def setup_credentials(self, ctx, client_id: str, client_secret: str, user_agent: str = None):
        """Set up Reddit API credentials"""
        if not asyncpraw:
            await ctx.send("❌ The `praw` library is not installed. Please install it to use this cog.")
            return

        if not user_agent:
            user_agent = f"RedditMonitor/1.0 by {ctx.guild.name}"

        await self.config.guild(ctx.guild).reddit_credentials.set({
            "client_id": client_id,
            "client_secret": client_secret,
            "user_agent": user_agent
        })

        # Test the credentials
        try:
            reddit_client = asyncpraw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                user_agent=user_agent
            )
            # Test API access
            reddit_client.user.me()
            await ctx.send("✅ Reddit credentials set up successfully!")
        except Exception as e:
            await ctx.send(f"❌ Failed to verify Reddit credentials: {e}")

    @redditmonitor.command(name="channel")
    async def set_channel(self, ctx, channel: discord.TextChannel = None):
        """Set the notification channel"""
        if not channel:
            channel = ctx.channel

        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"✅ Notification channel set to {channel.mention}")

    @redditmonitor.command(name="toggle")
    async def toggle(self, ctx):
        """Toggle monitoring on/off."""
        guild_config = self.config.guild(ctx.guild)
        enabled = await guild_config.enabled()

        if enabled:
            await guild_config.enabled.set(False)
            await self.stop_monitoring(ctx.guild)
            await ctx.send("✅ Monitoring disabled")
        else:
            # Check if credentials are set
            credentials = await guild_config.reddit_credentials()
            if not credentials["client_id"] or not credentials["client_secret"]:
                await ctx.send("❌ Please set up Reddit credentials first using `redditmonitor setup`")
                return

            if not await guild_config.channel_id():
                await ctx.send("❌ Please set a notification channel first using `redditmonitor channel`")
                return

            await guild_config.enabled.set(True)
            await self.start_monitoring(ctx.guild)
            await ctx.send("✅ Monitoring enabled")

    @redditmonitor.command(name="subreddits")
    async def manage_subreddits(self, ctx, action: str = None, subreddit: str = None):
        """Manage monitored subreddits (add/remove/list)"""
        guild_config = self.config.guild(ctx.guild)

        if action is None or action.lower() == "list":
            subreddits = await guild_config.subreddits()
            if subreddits:
                subreddit_list = "\n".join(f"• r/{sr}" for sr in subreddits)
                await ctx.send(f"**Monitored Subreddits:**\n{subreddit_list}")
            else:
                await ctx.send("No subreddits are being monitored.")
            return

        if not subreddit:
            await ctx.send("Please specify a subreddit name.")
            return

        subreddits = await guild_config.subreddits()

        if action.lower() == "add":
            if subreddit not in subreddits:
                subreddits.append(subreddit)
                await guild_config.subreddits.set(subreddits)
                await ctx.send(f"✅ Added r/{subreddit} to monitored subreddits")
            else:
                await ctx.send(f"r/{subreddit} is already being monitored")

        elif action.lower() == "remove":
            if subreddit in subreddits:
                subreddits.remove(subreddit)
                await guild_config.subreddits.set(subreddits)
                await ctx.send(f"✅ Removed r/{subreddit} from monitored subreddits")
            else:
                await ctx.send(f"r/{subreddit} is not being monitored")

        else:
            await ctx.send("Invalid action. Use `add`, `remove`, or `list`")

    @redditmonitor.command(name="interval")
    async def set_interval(self, ctx, seconds: int):
        """Set the check interval in seconds (minimum 60)"""
        if seconds < 60:
            await ctx.send("❌ Interval must be at least 60 seconds")
            return

        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(f"✅ Check interval set to {seconds} seconds")

    @redditmonitor.command(name="threshold")
    async def set_threshold(self, ctx, threshold: float):
        """Set the detection threshold (1.0-10.0)"""
        if not 1.0 <= threshold <= 10.0:
            await ctx.send("❌ Threshold must be between 1.0 and 10.0")
            return

        await self.config.guild(ctx.guild).detection_threshold.set(threshold)
        await ctx.send(f"✅ Detection threshold set to {threshold}")

    @redditmonitor.command(name="status")
    async def show_status(self, ctx):
        """Show current monitoring status"""
        guild_config = self.config.guild(ctx.guild)

        enabled = await guild_config.enabled()
        channel_id = await guild_config.channel_id()
        subreddits = await guild_config.subreddits()
        interval = await guild_config.check_interval()
        threshold = await guild_config.detection_threshold()

        status = "🟢 Enabled" if enabled else "🔴 Disabled"
        channel = f"<#{channel_id}>" if channel_id else "Not set"

        embed = discord.Embed(
            title="Reddit Monitor Status",
            color=discord.Color.green() if enabled else discord.Color.red()
        )

        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Channel", value=channel, inline=True)
        embed.add_field(name="Interval", value=f"{interval}s", inline=True)
        embed.add_field(name="Threshold", value=str(threshold), inline=True)
        embed.add_field(name="Subreddits", value=f"{len(subreddits)} monitored", inline=True)
        embed.add_field(name="Task Status", value="Running" if ctx.guild.id in self.monitor_tasks else "Stopped",
                        inline=True)

        await ctx.send(embed=embed)

    @redditmonitor.command(name="test")
    async def test_detection(self, ctx, *, post_title: str):
        """Test the mod detection algorithm on a post title"""

        # Create a mock post object
        class MockPost:
            def __init__(self, title):
                self.title = title
                self.selftext = ""

        mock_post = MockPost(post_title)
        is_mod = await self.is_mod_question(mock_post, ctx.guild)

        result = "✅ Would be detected" if is_mod else "❌ Would not be detected"
        await ctx.send(f"**Test Result:** {result}\n**Title:** {post_title}")