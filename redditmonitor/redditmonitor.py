import asyncio
import re
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional

import asyncpraw
from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import pagify
import discord

LOGGER = logging.getLogger("red.redditmonitor")

# Unique identifier for Config.get_conf. Change if you fork the cog.
CONF_ID = 0x4a3b2c1d

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


class RedditMonitor(commands.Cog):
    """Monitor configured subreddits and post detected modding / tech-support posts to a Discord channel.

    Detection uses keyword lists divided into higher (immediate), normal, lower, and negative.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_ID, force_registration=True)

        # Guild defaults
        default_guild = {
            "enabled": False,
            "reddit_client_id": None,
            "reddit_client_secret": None,
            "reddit_user_agent": None,
            "notify_channel_id": None,
            "subreddits": [],  # list of subreddit names
            "interval": DEFAULT_INTERVAL,
            "threshold": DEFAULT_THRESHOLD,
            "keywords": {
                "higher": [],
                "normal": [],
                "lower": [],
                "negative": [],
            },
            "flair_filter": None,
            "processed_ids": [],
            "max_processed": DEFAULT_MAX_PROCESSED,
            "timezone": None,
            "default_debug": False,
        }

        self.config.register_guild(**default_guild)

        # runtime state
        self._tasks: Dict[int, asyncio.Task] = {}  # guild_id -> task
        self._reddit_clients: Dict[int, asyncpraw.Reddit] = {}  # guild_id -> reddit instance
        self._lock = asyncio.Lock()

    async def cog_unload(self) -> None:
        # cancel monitoring tasks
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        # cleanup reddit clients
        for r in list(self._reddit_clients.values()):
            try:
                await r.close()
            except Exception:
                pass
        self._reddit_clients.clear()

    # ------------------------- Helpers -------------------------
    async def _get_reddit(self, guild: discord.Guild) -> Optional[asyncpraw.Reddit]:
        """Get or create an asyncpraw Reddit instance for the guild using stored creds."""
        creds = {
            "client_id": await self.config.guild(guild).reddit_client_id(),
            "client_secret": await self.config.guild(guild).reddit_client_secret(),
            "user_agent": await self.config.guild(guild).reddit_user_agent(),
        }
        if not (creds["client_id"] and creds["client_secret"] and creds["user_agent"]):
            return None

        # create a reddit instance per guild if not exists
        if guild.id in self._reddit_clients:
            return self._reddit_clients[guild.id]

        try:
            reddit = asyncpraw.Reddit(
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                user_agent=creds["user_agent"],
            )
        except Exception as e:
            LOGGER.exception("Failed to create asyncpraw Reddit instance: %s", e)
            return None

        self._reddit_clients[guild.id] = reddit
        return reddit

    def _compile_patterns(self, patterns: List[str]) -> List[re.Pattern]:
        compiled = []
        for p in patterns:
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error:
                # fallback: escape as literal
                compiled.append(re.compile(re.escape(p), re.IGNORECASE))
        return compiled

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

    async def _should_notify(self, submission: asyncpraw.models.Submission, detect_info: dict,
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
        title = submission.title.lower()
        body = getattr(submission, "selftext", "").lower()

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

    async def _notify(self, guild: discord.Guild, submission: asyncpraw.models.Submission, detect_info: dict):
        """Enhanced notification with confidence indicators."""
        channel_id = await self.config.guild(guild).notify_channel_id()
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        title = submission.title
        permalink = f"https://reddit.com{submission.permalink}"
        created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)

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
            url=permalink,
            description=(submission.selftext[:500] + "..." if len(submission.selftext) > 500 else submission.selftext),
            color=color,
            timestamp=created
        )

        embed.add_field(name="Confidence", value=confidence, inline=True)
        embed.add_field(name="Score", value=f"{score:.1f}", inline=True)
        embed.add_field(name="Subreddit", value=f"r/{submission.subreddit.display_name}", inline=True)

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

        embed.set_footer(text=f"u/{submission.author} • {submission.id}")

        try:
            await channel.send(embed=embed)
        except Exception:
            LOGGER.exception("Failed to send notification")

    async def _add_processed(self, guild: discord.Guild, post_id: str):
        async with self._lock:
            processed = await self.config.guild(guild).processed_ids()
            maxp = await self.config.guild(guild).max_processed()
            if processed is None:
                processed = []
            processed.append(post_id)
            # keep most recent N
            if len(processed) > maxp:
                processed = processed[-maxp:]
            await self.config.guild(guild).processed_ids.set(processed)

    async def _is_processed(self, guild: discord.Guild, post_id: str) -> bool:
        processed = await self.config.guild(guild).processed_ids()
        return processed and post_id in processed

    # ------------------------- Monitoring Task -------------------------
    async def _monitor_guild(self, guild: discord.Guild):
        LOGGER.info("Starting monitor for guild %s", guild.id)
        try:
            reddit = await self._get_reddit(guild)
            if reddit is None:
                LOGGER.warning("Guild %s has no reddit credentials configured; stopping monitor", guild.id)
                return

            while True:
                try:
                    enabled = await self.config.guild(guild).enabled()
                    if not enabled:
                        LOGGER.info("Monitoring disabled for guild %s; stopping task", guild.id)
                        return

                    subs = await self.config.guild(guild).subreddits()
                    if not subs:
                        LOGGER.debug("No subreddits configured for guild %s", guild.id)
                    else:
                        keywords = await self.config.guild(guild).keywords()
                        threshold = await self.config.guild(guild).threshold()
                        flair_filter = await self.config.guild(guild).flair_filter()

                        found_match = False  # <-- Add this line

                        for sub in list(subs):
                            try:
                                subreddit = await reddit.subreddit(sub)
                                async for submission in subreddit.new(limit=25):
                                    if await self._is_processed(guild, submission.id):
                                        continue

                                    if flair_filter:
                                        flair = getattr(submission, "link_flair_text", None)
                                        if not flair or flair_filter.lower() not in str(flair).lower():
                                            continue

                                    title = submission.title or ""
                                    body = getattr(submission, "selftext", "") or ""

                                    detect = self._match_score(title, body, keywords)

                                    detected = False
                                    if detect["immediate"]:
                                        detected = True
                                    elif detect["score"] >= threshold:
                                        detected = True

                                    if detect["matches"]["negative"]:
                                        if len(detect["matches"]["negative"]) > len(detect["matches"]["normal"]) + len(
                                                detect["matches"]["lower"]):
                                            detected = False

                                    if detected:
                                        found_match = True  # <-- Set to True if match found
                                        await self._notify(guild, submission, detect)

                                    await self._add_processed(guild, submission.id)

                            except Exception:
                                LOGGER.exception("Error processing subreddit %s for guild %s", sub, guild.id)

                        # Send debug message if no matches found and debug mode is enabled
                        debug_enabled = await self.config.guild(guild).default_debug()
                        if not found_match and debug_enabled:
                            channel_id = await self.config.guild(guild).notify_channel_id()
                            channel = guild.get_channel(channel_id) if channel_id else None
                            if channel:
                                await channel.send("✅ Reddit monitor is alive. No matching posts found this cycle.")

                    interval = await self.config.guild(guild).interval()
                    if not isinstance(interval, int) or interval < MIN_INTERVAL:
                        interval = MIN_INTERVAL
                    await asyncio.sleep(interval)

                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Unhandled error in monitoring loop for guild %s", guild.id)
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            LOGGER.info("Monitor task cancelled for guild %s", guild.id)
        finally:
            try:
                rc = self._reddit_clients.pop(guild.id, None)
                if rc:
                    await rc.close()
            except Exception:
                pass
            self._tasks.pop(guild.id, None)
            LOGGER.info("Monitor stopped for guild %s", guild.id)

    async def _ensure_task(self, guild: discord.Guild):
        """Start monitoring task for a guild if enabled and not already running."""
        if guild.id in self._tasks:
            return
        enabled = await self.config.guild(guild).enabled()
        if not enabled:
            return
        task = self.bot.loop.create_task(self._monitor_guild(guild))
        self._tasks[guild.id] = task

    # ------------------------- Commands -------------------------
    @commands.group()
    @commands.guild_only()
    async def rmonitor(self, ctx: commands.Context):
        """Reddit monitor commands. Use 'quicksetup' or 'loaddefaults' to get started quickly."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @rmonitor.command(name="quicksetup")
    @commands.admin_or_permissions(manage_guild=True)
    async def quicksetup(self, ctx: commands.Context, channel: discord.TextChannel):
        """Quick setup: set channel and load default keywords."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await self.config.guild(ctx.guild).keywords.set(deepcopy(DEFAULT_KEYWORDS))

        await ctx.send(f"✅ Quick setup complete!\n"
                       f"📢 Notification channel: {channel.mention}\n"
                       f"🔑 Default keywords loaded\n"
                       f"⚙️ Next steps:\n"
                       f"• Add subreddits: `{ctx.prefix}rmonitor addsub minecraft`\n"
                       f"• Set Reddit credentials: `{ctx.prefix}rmonitor setcreds <id> <secret> <user_agent>`\n"
                       f"• Enable monitoring: `{ctx.prefix}rmonitor enable`")

    # Credentials
    @rmonitor.command(name="setcreds")
    @commands.admin_or_permissions(manage_guild=True)
    async def setcreds(self, ctx: commands.Context, client_id: str, client_secret: str, *, user_agent: str):
        """Set Reddit API credentials for this guild. Keep these private."""
        await self.config.guild(ctx.guild).reddit_client_id.set(client_id)
        await self.config.guild(ctx.guild).reddit_client_secret.set(client_secret)
        await self.config.guild(ctx.guild).reddit_user_agent.set(user_agent)
        await ctx.send("Reddit credentials saved. Do not share these publicly.")

    @rmonitor.command(name="setchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where Reddit post notifications will be posted."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await ctx.send(f"Notification channel set to {channel.mention}")

    # Subreddit management
    @rmonitor.command(name="addsub")
    @commands.admin_or_permissions(manage_guild=True)
    async def addsub(self, ctx: commands.Context, subreddit: str):
        """Add a subreddit (name only, e.g. 'minecraft') to the monitored list."""
        subreddit = subreddit.strip().lstrip("r/")
        async with self.config.guild(ctx.guild).subreddits() as subs:
            if subreddit in subs:
                await ctx.send("That subreddit is already being monitored.")
                return
            subs.append(subreddit)
        await ctx.send(f"Added subreddit: {subreddit}")

    @rmonitor.command(name="remsub")
    @commands.admin_or_permissions(manage_guild=True)
    async def remsub(self, ctx: commands.Context, subreddit: str):
        subreddit = subreddit.strip().lstrip("r/")
        async with self.config.guild(ctx.guild).subreddits() as subs:
            if subreddit not in subs:
                await ctx.send("That subreddit is not in the monitored list.")
                return
            subs.remove(subreddit)
        await ctx.send(f"Removed subreddit: {subreddit}")

    @rmonitor.command(name="listsubs")
    @commands.admin_or_permissions(manage_guild=True)
    async def listsubs(self, ctx: commands.Context):
        subs = await self.config.guild(ctx.guild).subreddits()
        if not subs:
            await ctx.send("No subreddits configured.")
            return
        msg = "Monitored subreddits:\n" + "\n".join(f"- {s}" for s in subs)
        for page in pagify(msg):
            await ctx.send(page)

    # Enable / disable
    @rmonitor.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def enable(self, ctx: commands.Context):
        """Enable monitoring for this guild."""
        enabled = await self.config.guild(ctx.guild).enabled()
        if enabled:
            await ctx.send("Monitoring is already enabled. Use `!rmonitor disable` to turn off.")
            return
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("Monitoring enabled for this guild.")
        await self._ensure_task(ctx.guild)

    @rmonitor.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context):
        """Disable monitoring for this guild."""
        await self.config.guild(ctx.guild).enabled.set(False)
        # cancel running task if any
        task = self._tasks.pop(ctx.guild.id, None)
        if task:
            task.cancel()
        await ctx.send("Monitoring disabled for this guild.")

    # Interval and threshold
    @rmonitor.command(name="setinterval")
    @commands.admin_or_permissions(manage_guild=True)
    async def setinterval(self, ctx: commands.Context, seconds: int):
        """Set check interval in seconds (minimum 60)."""
        if seconds < MIN_INTERVAL:
            await ctx.send(f"Interval must be at least {MIN_INTERVAL} seconds.")
            return
        await self.config.guild(ctx.guild).interval.set(seconds)
        await ctx.send(f"Check interval set to {seconds} seconds.")

    @rmonitor.command(name="setthreshold")
    @commands.admin_or_permissions(manage_guild=True)
    async def setthreshold(self, ctx: commands.Context, threshold: float):
        """Set detection threshold (float between 1.0 and 10.0)."""
        if threshold < 1.0 or threshold > 10.0:
            await ctx.send("Threshold must be between 1.0 and 10.0")
            return
        await self.config.guild(ctx.guild).threshold.set(threshold)
        await ctx.send(f"Detection threshold set to {threshold}")

    # Keywords management
    @rmonitor.group(name="keyword")
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

    @rmonitor.command(name="loaddefaults")
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
    @rmonitor.command(name="setmaxprocessed")
    @commands.admin_or_permissions(manage_guild=True)
    async def setmaxprocessed(self, ctx: commands.Context, max_items: int):
        """Set maximum number of processed reddit post IDs stored to control storage usage."""
        if max_items < 10:
            await ctx.send("max_processed must be at least 10")
            return
        await self.config.guild(ctx.guild).max_processed.set(max_items)
        await ctx.send(f"max_processed set to {max_items}")

    @rmonitor.command(name="processedcount")
    @commands.admin_or_permissions(manage_guild=True)
    async def processedcount(self, ctx: commands.Context):
        processed = await self.config.guild(ctx.guild).processed_ids()
        cnt = len(processed) if processed else 0
        await ctx.send(f"Stored processed post IDs: {cnt}")

    # Manual checks and status
    @rmonitor.command(name="checknow")
    @commands.admin_or_permissions(manage_guild=True)
    async def checknow(self, ctx: commands.Context):
        """Run a manual check now in this guild."""
        await ctx.send("Running manual check...")
        # run monitor once
        # We create a short-lived task to run a single iteration by calling the monitor and cancelling after one loop.
        async def short_run():
            await self._monitor_guild(ctx.guild)

        # start the task and cancel after a small delay if it doesn't return (monitor_guild is long-running)
        task = self.bot.loop.create_task(short_run())
        await asyncio.sleep(5)
        if not task.done():
            task.cancel()
        await ctx.send("Manual check requested (background).")

    @rmonitor.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        """Show current monitoring status and configuration for this guild."""
        enabled = await self.config.guild(ctx.guild).enabled()
        subs = await self.config.guild(ctx.guild).subreddits()
        channel_id = await self.config.guild(ctx.guild).notify_channel_id()
        interval = await self.config.guild(ctx.guild).interval()
        threshold = await self.config.guild(ctx.guild).threshold()
        maxp = await self.config.guild(ctx.guild).max_processed()
        kw = await self.config.guild(ctx.guild).keywords()

        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        lines = [
            f"Enabled: {enabled}",
            f"Channel: {channel.mention if channel else 'Not set'}",
            f"Subreddits: {', '.join(subs) if subs else 'None'}",
            f"Interval: {interval}s",
            f"Threshold: {threshold}",
            f"Max processed stored: {maxp}",
            f"Keywords: higher={len(kw.get('higher') or [])}, normal={len(kw.get('normal') or [])}, lower={len(kw.get('lower') or [])}, negative={len(kw.get('negative') or [])}",
        ]
        await ctx.send("\n".join(lines))

    # Flair / category
    @rmonitor.command(name="setflair")
    @commands.admin_or_permissions(manage_guild=True)
    async def setflair(self, ctx: commands.Context, *, flair: Optional[str] = None):
        """Optionally set a flair text filter. Only posts with flair containing this text (case-insensitive) will be considered. Use blank to clear."""
        if flair is None or flair.strip() == "":
            await self.config.guild(ctx.guild).flair_filter.set(None)
            await ctx.send("Cleared flair filter. Monitoring all flairs.")
            return
        await self.config.guild(ctx.guild).flair_filter.set(flair.strip())
        await ctx.send(f"Set flair filter to: {flair.strip()}")

    # Test detection
    @rmonitor.command(name="testdetect")
    @commands.admin_or_permissions(manage_guild=True)
    async def testdetect(self, ctx: commands.Context, *, title_and_body: str):
        """Test the detection algorithm with a sample title (and optional body separated by '\n')."""
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

    @rmonitor.command(name="debugmode")
    @commands.admin_or_permissions(manage_guild=True)
    async def debugmode(self, ctx: commands.Context, enabled: bool):
        """Enable or disable debug mode (sends 'alive' messages when no matches found)."""
        await self.config.guild(ctx.guild).default_debug.set(enabled)
        await ctx.send(f"Debug mode set to: {enabled}")

    @rmonitor.command(name="tune")
    @commands.admin_or_permissions(manage_guild=True)
    async def tune_detection(self, ctx: commands.Context, subreddit: str, limit: int = 10):
        """Test detection on recent posts from a subreddit to tune accuracy."""
        reddit = await self._get_reddit(ctx.guild)
        if not reddit:
            await ctx.send("Reddit credentials not configured.")
            return

        keywords = await self.config.guild(ctx.guild).keywords()
        threshold = await self.config.guild(ctx.guild).threshold()

        try:
            sub = reddit.subreddit(subreddit.strip().lstrip("r/"))
            results = []

            async for submission in sub.new(limit=limit):
                title = submission.title or ""
                body = getattr(submission, "selftext", "") or ""

                detect = self._match_score(title, body, keywords)
                would_notify = await self._should_notify(submission, detect, ctx.guild)

                results.append({
                    "title": title[:50] + ("..." if len(title) > 50 else ""),
                    "score": detect["score"],
                    "notify": would_notify,
                    "matches": sum(len(v) for v in detect["matches"].values())
                })

            # Format results
            msg = f"**Detection Test Results for r/{subreddit}**\n```\n"
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