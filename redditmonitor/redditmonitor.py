import discord
import asyncio
import logging
import re
import traceback
from datetime import datetime
from typing import Optional, Dict, List, Set
import pytz

try:
    import praw
except ImportError:
    praw = None

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
                    "mod", "mods", "modpack", "modpacks", "forge", "fabric",
                    "ladymod", "laby", "skytils", "neu", "patcher", "sba",
                    "badlion", "lunar", "essential", "lunarclient", "client", "feather"
                ],
                "secondary": [
                    "modification", "skyblock addons", "not enough updates",
                    "fps boost", "performance", "lag", "frames", "frame rate",
                    "configs", "settings", "texture pack", "resource pack"
                ],
                "question_patterns": [
                    r"(?:recommend|suggest)(?:ed)?\s+(?:.*?)\s+(?:mod|mods|modpack)",
                    r"(?:what|which|best)\s+(?:.*?)\s+(?:mod|mods|modpack)",
                    r"(?:help|issue|problem)\s+(?:with|using)\s+(?:.*?)\s+(?:mod|mods)",
                    r"(?:how\s+to\s+(?:install|setup|configure|use))\s+(?:.*?)\s+(?:mod|mods)",
                    r"(?:can\'?t\s+get)\s+(?:.*?)\s+(?:mod|mods)\s+(?:to\s+work)",
                    r"(?:looking\s+for)\s+(?:.*?)\s+(?:mod|mods)",
                    r"(?:need|want)\s+(?:.*?)\s+(?:mod|mods)",
                    r"(?:low|bad)\s+(?:fps|frames|performance)",
                    r"performance\s+(?:issue|problem|boost)",
                    r"increase\s+(?:fps|performance)",
                    r"fixing\s+(?:lag|stutter|freeze)"
                ],
                "negative": [
                    "minion", "coins?", "dungeon", "f[0-9]", "hypixel", "skyblock",
                    "netherstar", "weapon", "armor", "items?", "pets?", "talismans?",
                    "slayer", "dragon", "farm", "mining"
                ]
            },
            "detection_threshold": 3.0,
            "processed_posts": []
        }

        self.config.register_guild(**default_guild)

        # Reddit client instances per guild
        self.reddit_clients: Dict[int, praw.Reddit] = {}

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

    async def create_reddit_client(self, guild: discord.Guild) -> Optional[praw.Reddit]:
        """Create a Reddit client for a guild"""
        if not praw:
            log.error("praw library not installed")
            return None

        guild_config = self.config.guild(guild)
        credentials = await guild_config.reddit_credentials()

        if not credentials["client_id"] or not credentials["client_secret"]:
            log.error(f"Reddit credentials not configured for guild {guild.id}")
            return None

        try:
            reddit_client = praw.Reddit(
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
        Determine if a Reddit post is about mods using improved detection logic
        """
        guild_config = self.config.guild(guild)
        keywords = await guild_config.keywords()
        threshold = await guild_config.detection_threshold()

        # Extract title and selftext
        title = post.title if hasattr(post, 'title') else ""
        content = post.selftext if hasattr(post, 'selftext') else ""

        title_lower = title.lower()
        content_lower = content.lower()

        score = 0

        # Check for explicit mod questions
        if re.search(r'what\s+mod\s+is', title_lower) or re.search(r'what\s+mod\s+is', content_lower):
            return True

        # Primary keywords in title (highest confidence)
        for keyword in keywords["primary"]:
            if re.search(rf'\b{keyword}\b', title_lower):
                score += 3

        # Question patterns in title
        for pattern in keywords["question_patterns"]:
            if re.search(pattern, title_lower):
                score += 4

        # Secondary keywords in title
        for keyword in keywords["secondary"]:
            if keyword in title_lower:
                score += 2

        # Content analysis
        if content_lower:
            for keyword in keywords["primary"]:
                if re.search(rf'\b{keyword}\b', content_lower):
                    score += 2

            for pattern in keywords["question_patterns"]:
                if re.search(pattern, content_lower):
                    score += 3

            for keyword in keywords["secondary"]:
                if keyword in content_lower:
                    score += 1

        # Negative keywords
        negative_score = 0
        for keyword in keywords["negative"]:
            if re.search(rf'\b{keyword}\b', title_lower):
                negative_score += 1
            if content_lower and re.search(rf'\b{keyword}\b', content_lower):
                negative_score += 0.5

        # Calculate final score
        final_score = score - (negative_score * 1.5)

        log.debug(f"Post '{title}' - Score: {score}, Negative: {negative_score}, Final: {final_score}")

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
        processed_posts = set(await guild_config.processed_posts())

        new_processed = []

        for subreddit_name in subreddits:
            try:
                subreddit = reddit_client.subreddit(subreddit_name)

                for post in subreddit.new(limit=post_limit):
                    if post.id in processed_posts:
                        continue

                    processed_posts.add(post.id)
                    new_processed.append(post.id)

                    if await self.is_mod_question(post, guild):
                        await self.send_notification(guild, post, subreddit_name)
                        log.info(f"Sent notification for post: {post.title}")

            except Exception as e:
                log.error(f"Error monitoring r/{subreddit_name}: {e}")

        # Save processed posts (keep only last 1000)
        if new_processed:
            all_processed = list(processed_posts)
            if len(all_processed) > 1000:
                all_processed = all_processed[-1000:]
            await guild_config.processed_posts.set(all_processed)

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
        if not praw:
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
            reddit_client = praw.Reddit(
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
    async def toggle_monitoring(self, ctx):
        """Toggle monitoring on/off"""
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