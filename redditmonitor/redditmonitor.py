"""
RedditMonitor — RedBot cog
Monitors configured subreddits for mod / tech-support posts and posts
Discord embeds to a configured channel.

Detection tiers
  higher   → immediate notify regardless of threshold (VIP keywords, exact names)
  normal   → +3.0 per phrase, +1.5 per single word  (title doubles these)
  lower    → +1.5 per phrase, +0.5 per single word   (title doubles these)
  negative → -2.5 per phrase, -1.0 per single word   (economy / trading terms)

Context boost (+0.5 each, capped at +2.0): help-seeking language, tech terms, question marks.
"""

import asyncio
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional

import asyncpraw
import asyncpraw.models
from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import pagify
import discord

LOGGER = logging.getLogger("red.redditmonitor")

# ── Config identifier (change if you fork) ──────────────────────────────────
CONF_ID = 0x4A3B2C1D

# ── Limits ───────────────────────────────────────────────────────────────────
MIN_INTERVAL = 60
DEFAULT_INTERVAL = 900       # 15 minutes
DEFAULT_THRESHOLD = 3.0
DEFAULT_MAX_PROCESSED = 1000

# ── Default keyword lists ─────────────────────────────────────────────────────
#
# TIER PHILOSOPHY
# ───────────────
# higher  → bypass threshold entirely (exact mod/tool names you specifically support)
# normal  → must be mod-specific; generic words like "bug" or "lag" do NOT belong here
# lower   → generic technical words that are weak signals on their own
# negative → game content / economy terms that indicate a non-mod post
DEFAULT_KEYWORDS: Dict[str, List[str]] = {
    # Immediate-trigger keywords (bypass threshold check entirely)
    "higher": [
        "skyblock enhanced", "sb enhanced",
        "kd_gaming1", "kdgaming1", "kdgaming",
        "packcore", "scale me", "scaleme",
    ],

    # Normal keywords — mod names, loaders, and general tech-help vocabulary.
    # The expanded negative list handles false positives; don't over-restrict here.
    "normal": [
        # Mod loaders / build tools
        "forge", "fabric", "modpack", "modpacks",
        "configs", "config", "configuration",
        "modrinth",
        "1.21.5", "1.21.8", "1.21.10", "1.21.11", "26.1", "26.2",

        # Generic modding terms
        "mod", "mods", "modded", "modding",
        "modification", "loader", "addon", "plugin",
        "skyblock addons", "not enough updates",
        "texture pack", "resource pack",
        "shader", "shaders", "optifine",
        "optimization", "optimize", "tweak", "utility",

        # 1.21+ SkyBlock mods
        "firmament", "skyblock tweaks", "modern warp menu",
        "skyblockaddons unofficial", "skyhanni", "hypixel mod api",
        "skyocean", "skyblock profile viewer", "bazaar utils",
        "skyblocker", "cookies-mod", "aaron's mod",
        "custom scoreboard", "skycubed", "nofrills",
        "nobaaddons", "sky cubed", "dulkirmod",
        "skyblock 21", "skycofl",

        # 1.8.9 SkyBlock mods
        "notenoughupdates", "neu", "polysprint",
        "skyblockaddons", "sba", "polypatcher",
        "hypixel plus", "furfsky", "dungeons guide",
        "skyguide", "partly sane skies",
        "secret routes mod", "skytils",

        # Performance mods
        "more culling", "badoptimizations",
        "concurrent chunk management", "very many players",
        "threadtweak", "scalablelux", "particle core",
        "sodium", "lithium", "iris",
        "entity culling", "ferritecore", "immediatelyfast",

        # QoL mods
        "scrollable tooltips", "fzzy config",
        "no chat reports", "no resource pack warnings",
        "auth me", "betterf3", "no double sneak",
        "centered crosshair", "continuity", "3d skin layers",
        "wavey capes", "sound controller",
        "cubes without borders", "sodium shadowy path blocks",

        # Popular clients / launchers
        "ladymod", "laby", "badlion", "lunar", "essential",
        "lunarclient", "feather",

        # Performance problems
        "fps boost", "fps drop", "frame drop", "low fps", "bad performance",
        "stuttering", "choppy", "frames", "frame rate",
        "performance", "fps", "lag",
        "memory", "ram", "cpu", "gpu", "graphics",

        # Technical problem words
        "bug", "error", "glitch", "crash", "crashing",
        "freezing", "not working", "broken",
        "fix", "troubleshoot",
        "install", "installation", "setup",
        "configure", "compatibility",

        # Mod-specific install / crash phrases
        "install mod", "mod installation", "how to install mod",
        "mod not loading", "mod not working", "mods not loading",
        "mod crashing", "mod crash", "client crash",
        "mod conflict", "mod incompatible",
        "java crash", "java error", "memory leak",

        # Platform / runtime
        "java", "minecraft", "windows", "linux",
    ],

    # Lower tier — intentionally empty; add very weak signals here if needed
    "lower": [],

    # Penalise game-content posts — these are almost never mod-related
    "negative": [
        # Economy / trading
        "auction house", "bazaar", "trading",
        "selling", "buying", "worth", "price check",
        "price", "coins", "bits",
        "money making", "farming coins",

        # Game progression / gear
        "minion", "dungeon master", "catacombs", "slayer", "dragon",
        "collection", "skill", "enchanting", "reforge",
        "talisman", "accessory", "weapon", "armor", "pet",
        "bestiary", "crimson isle", "kuudra",

        # Farming / garden game content
        "crop", "crops", "crop fever", "farming",
        "greenhouse", "garden", "mutation", "mutations",
        "dicer", "melon dicer", "visitor", "compost",
        "plot", "plots", "jacob", "pest",

        # World / exploration content
        "foraging", "foraging island", "jungle island", "mining island",
        "rift", "living cave", "autocap", "autonull",
        "dwarven mines", "crystal hollows", "deep caverns",
        "spider's den", "blazing fortress",
        "new profile", "profile",

        # Fishing content
        "fishing", "trophy fish", "lava fishing",

        # Combat / boss content
        "dungeon", "floor", "boss", "mob", "monster",
        "damage", "effective hp", "ehp", "dps",
    ],
}

# Patterns that strongly indicate game-content (not mod-related) posts
FALSE_POSITIVE_PATTERNS = [
    # Economy / trading language
    re.compile(r'\b(selling|buying|trade|auction|price\s*check|worth)\b', re.I),
    re.compile(r'\b(looking\s*for|want\s*to\s*buy|WTB|WTS)\b', re.I),
    re.compile(r'\b(what.{0,20}worth|how\s+much|value)\b', re.I),
    # Farming / crop game content
    re.compile(r'\b(crop|crops|greenhouse|mutation|mutations|farming|harvest|garden|dicer|compost|visitor|jacob)\b', re.I),
    # World / area exploration
    re.compile(r'\b(foraging\s+island|jungle\s+island|rift\s+(?!client)|living\s+cave|dwarven|crystal\s+hollow)\b', re.I),
    # Profile / game mechanic talk
    re.compile(r'\b(new\s+profile|fresh\s+profile|my\s+profile|profile\s+reset)\b', re.I),
    # Boss / dungeon game content
    re.compile(r'\b(dungeon\s+(?:run|floor|room)|slayer\s+(?:quest|boss)|dragon\s+(?:eye|armor|fight))\b', re.I),
    # Skill / level game content
    re.compile(r'\b(skill\s+(?:level|cap|xp|exp)|collection\s+(?:level|req))\b', re.I),
]

# Context patterns that raise confidence (+0.5 each, capped at +2.0)
CONTEXT_PATTERNS = [
    re.compile(r'\b(help|issue|problem|crash|fix|install|setup|configure)\b', re.I),
    re.compile(r"\b(not\s+working|broken|won'?t\s+work|can'?t\s+get|having\s+trouble)\b", re.I),
    re.compile(r'\b(fps|performance|lag|optimization|memory|ram|java)\b', re.I),
    re.compile(r'\b(how\s+do\s+i|how\s+to|anyone\s+know|can\s+someone|need\s+help|please\s+help)\b', re.I),
    re.compile(r'\?'),
]


# ─────────────────────────────────────────────────────────────────────────────
class RedditMonitor(commands.Cog):
    """Monitor subreddits for mod-related questions and technical help.

    Detection uses keyword tiers: higher (immediate), normal, lower, negative.
    Run ``[p]rmonitor quicksetup #channel`` to get started.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_ID, force_registration=True)

        default_guild = {
            "enabled": False,
            "reddit_client_id": None,
            "reddit_client_secret": None,
            "reddit_user_agent": None,
            "notify_channel_id": None,
            "subreddits": [],
            "interval": DEFAULT_INTERVAL,
            "threshold": DEFAULT_THRESHOLD,
            "keywords": {"higher": [], "normal": [], "lower": [], "negative": []},
            "flair_filter": None,
            "processed_ids": [],
            "max_processed": DEFAULT_MAX_PROCESSED,
            "debug": False,
        }
        self.config.register_guild(**default_guild)

        self._tasks:         Dict[int, asyncio.Task]      = {}
        self._reddit_clients: Dict[int, asyncpraw.Reddit] = {}
        self._task_locks:    Dict[int, asyncio.Lock]      = {}
        # Per-guild lock for processed-ID writes (avoids a single global bottleneck)
        self._proc_locks:    Dict[int, asyncio.Lock]      = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def cog_load(self) -> None:
        await self._startup_tasks()

    async def cog_unload(self) -> None:
        LOGGER.info("Shutting down RedditMonitor…")
        tasks = list(self._tasks.values())
        for t in tasks:
            if not t.cancelled():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for reddit in self._reddit_clients.values():
            try:
                await reddit.close()
            except Exception:
                pass
        self._reddit_clients.clear()
        self._task_locks.clear()
        self._proc_locks.clear()

    async def _startup_tasks(self):
        try:
            for guild_id, cfg in (await self.config.all_guilds()).items():
                if cfg.get("enabled"):
                    g = self.bot.get_guild(guild_id)
                    if g:
                        await self._ensure_task(g)
        except Exception:
            LOGGER.exception("Error during startup")

    # ── Reddit client helper ──────────────────────────────────────────────────
    async def _get_reddit(self, guild: discord.Guild) -> Optional[asyncpraw.Reddit]:
        """Return a cached asyncpraw Reddit client for this guild, or None if unconfigured."""
        if guild.id in self._reddit_clients:
            return self._reddit_clients[guild.id]

        cid    = await self.config.guild(guild).reddit_client_id()
        secret = await self.config.guild(guild).reddit_client_secret()
        ua     = await self.config.guild(guild).reddit_user_agent()

        if not (cid and secret and ua):
            return None

        try:
            reddit = asyncpraw.Reddit(
                client_id=cid, client_secret=secret, user_agent=ua
            )
            self._reddit_clients[guild.id] = reddit
            return reddit
        except Exception as e:
            LOGGER.exception("Reddit client creation failed: %s", e)
            return None

    # ── Detection ─────────────────────────────────────────────────────────────
    @staticmethod
    def _score_text(
        title: str,
        body: str,
        keywords: Dict[str, List[str]],
    ) -> Dict:
        """
        Score a post against keyword tiers.

        Title hits are worth 2× their normal value.

        Returns:
            immediate (bool): True if any "higher" keyword matched.
            score     (float): Aggregate relevance score.
            matches   (dict):  {tier: [matched keywords]}
            breakdown (dict):  keyword → (tier, points_awarded)
        """
        title_l  = title.lower()
        body_l   = body.lower()
        combined = f"{title_l}\n{body_l}"

        matches   = {"higher": [], "normal": [], "lower": [], "negative": []}
        breakdown = {}

        # Body score, phrase / single-word weights per tier
        # Title score = body score × 2.0
        BODY_PHRASE  = {"higher": 0,    "normal": 3.0,  "lower": 1.5,  "negative": -2.5}
        BODY_SINGLE  = {"higher": 0,    "normal": 1.5,  "lower": 0.5,  "negative": -1.0}
        TITLE_MULT   = 2.0

        score = 0.0

        for tier in ("higher", "normal", "lower", "negative"):
            for kw in keywords.get(tier, []):
                kw_l = kw.lower()
                if " " in kw_l:
                    in_title = kw_l in title_l
                    in_body  = kw_l in body_l and not in_title
                    if not (in_title or in_body):
                        continue
                    matches[tier].append(kw)
                    base = BODY_PHRASE[tier]
                    pts  = base * TITLE_MULT if in_title else base
                    score += pts
                    breakdown[kw] = (tier, pts)
                else:
                    pat      = rf'\b{re.escape(kw_l)}\b'
                    in_title = bool(re.search(pat, title_l))
                    in_body  = bool(re.search(pat, body_l)) and not in_title
                    if not (in_title or in_body):
                        continue
                    matches[tier].append(kw)
                    base = BODY_SINGLE[tier]
                    pts  = base * TITLE_MULT if in_title else base
                    score += pts
                    breakdown[kw] = (tier, pts)

        # Context boost (capped at +2.0)
        context_boost = 0.0
        if matches["normal"] or matches["lower"]:
            for cp in CONTEXT_PATTERNS:
                if cp.search(combined):
                    context_boost = min(context_boost + 0.5, 2.0)
            score += context_boost

        return {
            "immediate":     bool(matches["higher"]),
            "score":         round(score, 2),
            "matches":       matches,
            "context_boost": context_boost,
            "breakdown":     breakdown,
        }

    async def _should_notify(
        self,
        submission: asyncpraw.models.Submission,
        detect: dict,
        guild: discord.Guild,
    ) -> bool:
        if detect["immediate"]:
            return True

        threshold = await self.config.guild(guild).threshold()
        if detect["score"] < threshold:
            return False

        title    = submission.title.lower()
        body     = getattr(submission, "selftext", "").lower()
        combined = f"{title} {body}"

        # Too many negative indicators relative to positive
        neg = len(detect["matches"]["negative"])
        pos = len(detect["matches"]["normal"]) + len(detect["matches"]["lower"])
        if neg >= pos and neg > 1:
            return False

        # False-positive content patterns
        for pat in FALSE_POSITIVE_PATTERNS:
            if pat.search(combined):
                return False

        # Borderline score: require at least some normal-tier match + context signal
        if detect["score"] < threshold + 1.5:
            if not detect["matches"]["normal"] and detect["context_boost"] < 1.0:
                return False

        return True

    # ── Notification ──────────────────────────────────────────────────────────
    async def _notify(
        self,
        guild: discord.Guild,
        submission: asyncpraw.models.Submission,
        detect: dict,
    ):
        channel_id = await self.config.guild(guild).notify_channel_id()
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        score   = detect["score"]
        created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)

        if detect["immediate"]:
            confidence, color = "🔴 HIGH (Immediate)", discord.Color.red()
        elif score >= 6.0:
            confidence, color = "🟠 HIGH",   discord.Color.orange()
        elif score >= 3.0:
            confidence, color = "🟡 MEDIUM", discord.Color.gold()
        else:
            confidence, color = "🟢 LOW",    discord.Color.green()

        selftext = getattr(submission, "selftext", "") or ""
        embed = discord.Embed(
            title=(submission.title or "")[:256],
            url=f"https://reddit.com{submission.permalink}",
            description=(selftext[:500] + "…") if len(selftext) > 500 else selftext or "No text",
            color=color,
            timestamp=created,
        )
        embed.add_field(name="Confidence", value=confidence,                         inline=True)
        embed.add_field(name="Score",      value=f"{score:.1f}",                     inline=True)
        embed.add_field(name="Subreddit",  value=f"r/{submission.subreddit.display_name}", inline=True)

        for tier in ("higher", "normal"):
            vals = detect["matches"].get(tier, [])
            if vals:
                embed.add_field(
                    name=f"{tier.title()} Keywords",
                    value=", ".join(vals[:6]) + ("…" if len(vals) > 6 else ""),
                    inline=False,
                )
        if detect["matches"].get("negative"):
            embed.add_field(
                name="⚠️ Negative Indicators",
                value=", ".join(detect["matches"]["negative"][:4]),
                inline=False,
            )

        embed.set_footer(text=f"u/{submission.author} • {submission.id}")
        try:
            await channel.send(embed=embed)
        except Exception:
            LOGGER.exception("Failed to send notification")

    # ── Processed-ID helpers ─────────────────────────────────────────────────
    def _proc_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._proc_locks:
            self._proc_locks[guild_id] = asyncio.Lock()
        return self._proc_locks[guild_id]

    async def _add_processed(self, guild: discord.Guild, post_id: str):
        async with self._proc_lock(guild.id):
            processed = await self.config.guild(guild).processed_ids() or []
            maxp = await self.config.guild(guild).max_processed()
            if post_id not in processed:
                processed.append(post_id)
            if len(processed) > maxp:
                processed = processed[-maxp:]
            await self.config.guild(guild).processed_ids.set(processed)

    async def _is_processed(self, guild: discord.Guild, post_id: str) -> bool:
        processed = await self.config.guild(guild).processed_ids()
        return bool(processed) and post_id in processed

    # ── Debug helper ─────────────────────────────────────────────────────────
    async def _debug(self, guild: discord.Guild, msg: str):
        if not await self.config.guild(guild).debug():
            return
        ch_id = await self.config.guild(guild).notify_channel_id()
        if ch_id and (ch := guild.get_channel(ch_id)):
            try:
                await ch.send(msg)
            except Exception:
                pass

    # ── Monitoring loop ───────────────────────────────────────────────────────
    async def _monitor_guild(self, guild: discord.Guild):
        LOGGER.info("Monitor started: guild %s", guild.id)
        try:
            reddit = await self._get_reddit(guild)
            if reddit is None:
                LOGGER.warning("No Reddit credentials for guild %s — stopping", guild.id)
                return

            while True:
                try:
                    if not await self.config.guild(guild).enabled():
                        LOGGER.info("Monitoring disabled: guild %s", guild.id)
                        break

                    subs = await self.config.guild(guild).subreddits()
                    if not subs:
                        await self._debug(guild, "⚠️ Monitor alive — no subreddits configured.")
                    else:
                        await self._check_subreddits(guild, reddit, subs)

                    interval = max(await self.config.guild(guild).interval(), MIN_INTERVAL)
                    await asyncio.sleep(interval)

                except asyncio.CancelledError:
                    break
                except Exception:
                    LOGGER.exception("Loop error: guild %s", guild.id)
                    await self._debug(guild, "❌ Monitor error — retrying in 60 s…")
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("Fatal error: guild %s", guild.id)
        finally:
            await self._cleanup(guild.id)

    async def _check_subreddits(
        self,
        guild: discord.Guild,
        reddit: asyncpraw.Reddit,
        subreddits: List[str],
    ):
        keywords     = await self.config.guild(guild).keywords()
        flair_filter = await self.config.guild(guild).flair_filter()
        notified     = 0
        checked      = 0

        for sub_name in subreddits:
            try:
                sub = await reddit.subreddit(sub_name)
                async for submission in sub.new(limit=25):
                    checked += 1
                    if await self._is_processed(guild, submission.id):
                        continue

                    # Optional flair filter
                    if flair_filter:
                        flair = getattr(submission, "link_flair_text", None) or ""
                        if flair_filter.lower() not in flair.lower():
                            await self._add_processed(guild, submission.id)
                            continue

                    title  = submission.title or ""
                    body   = getattr(submission, "selftext", "") or ""
                    detect = self._score_text(title, body, keywords)

                    if await self._should_notify(submission, detect, guild):
                        await self._notify(guild, submission, detect)
                        notified += 1
                        LOGGER.info("Notified: %s in r/%s (guild %s)", submission.id, sub_name, guild.id)

                    await self._add_processed(guild, submission.id)

            except Exception:
                LOGGER.exception("Subreddit error (%s): guild %s", sub_name, guild.id)

        if notified == 0:
            await self._debug(
                guild,
                f"✅ Monitor alive — checked {checked} posts across "
                f"{len(subreddits)} subreddit(s). No matches this cycle.",
            )

    # ── Task management ───────────────────────────────────────────────────────
    async def _cleanup(self, guild_id: int):
        self._tasks.pop(guild_id, None)
        reddit = self._reddit_clients.pop(guild_id, None)
        if reddit:
            try:
                await reddit.close()
            except Exception:
                pass
        self._task_locks.pop(guild_id, None)
        self._proc_locks.pop(guild_id, None)

    def _get_task_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._task_locks:
            self._task_locks[guild_id] = asyncio.Lock()
        return self._task_locks[guild_id]

    async def _ensure_task(self, guild: discord.Guild):
        async with self._get_task_lock(guild.id):
            t = self._tasks.get(guild.id)
            if t and not t.done():
                return
            if t:
                await self._cleanup(guild.id)
            if not await self.config.guild(guild).enabled():
                return
            self._tasks[guild.id] = self.bot.loop.create_task(
                self._monitor_guild(guild)
            )

    async def _stop_task(self, guild: discord.Guild):
        async with self._get_task_lock(guild.id):
            t = self._tasks.get(guild.id)
            if t and not t.cancelled():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            await self._cleanup(guild.id)

    # ═════════════════════════════════════════════════════════════════════════
    # Commands
    # ═════════════════════════════════════════════════════════════════════════

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def rmonitor(self, ctx: commands.Context):
        """Reddit monitor. Start with ``quicksetup``, then ``setcreds``, then ``enable``.

        **Quick start**
        ```
        [p]rmonitor quicksetup #channel
        [p]rmonitor setcreds <id> <secret> MyBot/1.0
        [p]rmonitor addsub HypixelSkyblock
        [p]rmonitor enable
        ```
        """
        await ctx.send_help()

    # ── Setup ─────────────────────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def quicksetup(self, ctx: commands.Context, channel: discord.TextChannel):
        """One-shot setup: set channel and load default keywords."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await self.config.guild(ctx.guild).keywords.set(deepcopy(DEFAULT_KEYWORDS))
        await ctx.send(
            f"✅ Quick setup complete!\n"
            f"📢 Channel: {channel.mention}\n"
            f"🔑 Default keywords loaded\n"
            f"▶️  Next:\n"
            f"• Set credentials: `{ctx.prefix}rmonitor setcreds <id> <secret> <user_agent>`\n"
            f"• Add subreddits:  `{ctx.prefix}rmonitor addsub HypixelSkyblock`\n"
            f"• Start:           `{ctx.prefix}rmonitor enable`"
        )

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setcreds(
        self,
        ctx: commands.Context,
        client_id: str,
        client_secret: str,
        *,
        user_agent: str,
    ):
        """Set Reddit API credentials. Keep ``client_secret`` private."""
        await self.config.guild(ctx.guild).reddit_client_id.set(client_id)
        await self.config.guild(ctx.guild).reddit_client_secret.set(client_secret)
        await self.config.guild(ctx.guild).reddit_user_agent.set(user_agent)
        # Invalidate cached client so it's recreated with new creds
        old = self._reddit_clients.pop(ctx.guild.id, None)
        if old:
            try:
                await old.close()
            except Exception:
                pass
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.send("✅ Reddit credentials saved (message deleted for safety).")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the notification channel."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await ctx.send(f"Notification channel set to {channel.mention}.")

    # ── Enable / disable ──────────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def enable(self, ctx: commands.Context):
        """Start monitoring."""
        if await self.config.guild(ctx.guild).enabled():
            await ctx.send("Already enabled. Use `disable` to stop.")
            return
        await self.config.guild(ctx.guild).enabled.set(True)
        await self._ensure_task(ctx.guild)
        await ctx.send("✅ Monitoring enabled.")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context):
        """Stop monitoring."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await self._stop_task(ctx.guild)
        await ctx.send("⏹ Monitoring disabled.")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def restart(self, ctx: commands.Context):
        """Restart the monitoring task."""
        await self._stop_task(ctx.guild)
        await asyncio.sleep(1)
        await self._ensure_task(ctx.guild)
        await ctx.send("♻️ Monitoring task restarted.")

    # ── Interval / threshold ──────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setinterval(self, ctx: commands.Context, seconds: int):
        """Set check interval in seconds (minimum 60)."""
        if seconds < MIN_INTERVAL:
            await ctx.send(f"Minimum interval is {MIN_INTERVAL} s.")
            return
        await self.config.guild(ctx.guild).interval.set(seconds)
        await ctx.send(f"Interval set to {seconds} s.")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setthreshold(self, ctx: commands.Context, threshold: float):
        """Set detection threshold (1.0 – 10.0). Lower = more sensitive."""
        if not 1.0 <= threshold <= 10.0:
            await ctx.send("Threshold must be between 1.0 and 10.0.")
            return
        await self.config.guild(ctx.guild).threshold.set(threshold)
        await ctx.send(f"Threshold set to {threshold}.")

    # ── Subreddits ────────────────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def addsub(self, ctx: commands.Context, subreddit: str):
        """Add a subreddit to monitor (name only, e.g. ``HypixelSkyblock``)."""
        sub = subreddit.strip().lstrip("r/")
        async with self.config.guild(ctx.guild).subreddits() as subs:
            if sub in subs:
                await ctx.send("Already monitoring that subreddit.")
                return
            subs.append(sub)
        await ctx.send(f"Added: r/{sub}")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def remsub(self, ctx: commands.Context, subreddit: str):
        """Remove a subreddit from monitoring."""
        sub = subreddit.strip().lstrip("r/")
        async with self.config.guild(ctx.guild).subreddits() as subs:
            if sub not in subs:
                await ctx.send("That subreddit isn't in the list.")
                return
            subs.remove(sub)
        await ctx.send(f"Removed: r/{sub}")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def listsubs(self, ctx: commands.Context):
        """List all monitored subreddits."""
        subs = await self.config.guild(ctx.guild).subreddits()
        if not subs:
            await ctx.send("No subreddits configured.")
            return
        await ctx.send("**Monitored subreddits**\n" + "\n".join(f"• r/{s}" for s in subs))

    # ── Flair filter ──────────────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setflair(self, ctx: commands.Context, *, flair: Optional[str] = None):
        """Filter posts by flair text (case-insensitive substring). Leave blank to clear."""
        flair = flair.strip() if flair else None
        await self.config.guild(ctx.guild).flair_filter.set(flair)
        if flair:
            await ctx.send(f"Flair filter set to: `{flair}`")
        else:
            await ctx.send("Flair filter cleared — all flairs will be checked.")

    # ── Keywords ──────────────────────────────────────────────────────────────
    @rmonitor.group(name="keyword", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def keyword(self, ctx: commands.Context):
        """Manage detection keywords.

        Tiers: ``higher`` · ``normal`` · ``lower`` · ``negative``
        """
        await ctx.send_help()

    @keyword.command(name="add")
    async def keyword_add(self, ctx: commands.Context, tier: str, *, keyword: str):
        """Add one keyword to a tier.

        Example: ``[p]rmonitor keyword add normal skyhanni``
        """
        tier = tier.lower()
        if tier not in ("higher", "normal", "lower", "negative"):
            await ctx.send("Invalid tier. Use: `higher`, `normal`, `lower`, or `negative`.")
            return
        async with self.config.guild(ctx.guild).keywords() as kw:
            if keyword in kw[tier]:
                await ctx.send("That keyword is already in this tier.")
                return
            kw[tier].append(keyword)
        await ctx.send(f"Added to **{tier}**: `{keyword}`")

    @keyword.command(name="bulkadd")
    async def keyword_bulkadd(self, ctx: commands.Context, tier: str, *, keywords: str):
        """Add multiple comma-separated keywords at once.

        Example: ``[p]rmonitor keyword bulkadd normal skyhanni, skyblocker, sodium``
        """
        tier = tier.lower()
        if tier not in ("higher", "normal", "lower", "negative"):
            await ctx.send("Invalid tier. Use: `higher`, `normal`, `lower`, or `negative`.")
            return
        new_kws = [k.strip() for k in keywords.split(",") if k.strip()]
        if not new_kws:
            await ctx.send("No keywords found — separate them with commas.")
            return
        added, skipped = [], []
        async with self.config.guild(ctx.guild).keywords() as kw:
            for nk in new_kws:
                if nk in kw[tier]:
                    skipped.append(nk)
                else:
                    kw[tier].append(nk)
                    added.append(nk)
        parts = []
        if added:
            parts.append(f"✅ Added ({len(added)}): {', '.join(f'`{k}`' for k in added)}")
        if skipped:
            parts.append(f"⏭ Already present ({len(skipped)}): {', '.join(f'`{k}`' for k in skipped)}")
        await ctx.send("\n".join(parts))

    @keyword.command(name="remove")
    async def keyword_remove(self, ctx: commands.Context, tier: str, *, keyword: str):
        """Remove a keyword from a tier."""
        tier = tier.lower()
        if tier not in ("higher", "normal", "lower", "negative"):
            await ctx.send("Invalid tier. Use: `higher`, `normal`, `lower`, or `negative`.")
            return
        async with self.config.guild(ctx.guild).keywords() as kw:
            if keyword not in kw[tier]:
                await ctx.send("Keyword not found in that tier.")
                return
            kw[tier].remove(keyword)
        await ctx.send(f"Removed from **{tier}**: `{keyword}`")

    @keyword.command(name="list")
    async def keyword_list(self, ctx: commands.Context, tier: str = "all"):
        """List keywords. Optionally filter by tier.

        Example: ``[p]rmonitor keyword list normal``
        """
        kw = await self.config.guild(ctx.guild).keywords()
        tiers = ("higher", "normal", "lower", "negative") if tier == "all" \
                else (tier.lower(),)
        if any(t not in ("higher", "normal", "lower", "negative") for t in tiers):
            await ctx.send("Invalid tier. Use: `higher`, `normal`, `lower`, `negative`, or `all`.")
            return
        lines = []
        for t in tiers:
            vals = kw.get(t, [])
            lines.append(f"**{t.title()}** ({len(vals)})")
            for v in vals:
                lines.append(f"  • {v}")
        for page in pagify("\n".join(lines)):
            await ctx.send(page)

    @keyword.command(name="find")
    async def keyword_find(self, ctx: commands.Context, *, search: str):
        """Search for a keyword across all tiers.

        Example: ``[p]rmonitor keyword find sodium``
        """
        kw = await self.config.guild(ctx.guild).keywords()
        search_l = search.lower()
        found = [
            f"**{tier}**: `{k}`"
            for tier in ("higher", "normal", "lower", "negative")
            for k in kw.get(tier, [])
            if search_l in k.lower()
        ]
        await ctx.send("\n".join(found) if found else f"No keywords matching `{search}` found.")

    @keyword.command(name="export")
    async def keyword_export(self, ctx: commands.Context):
        """Export current keywords as a JSON file."""
        kw   = await self.config.guild(ctx.guild).keywords()
        data = json.dumps(kw, indent=2)
        fp   = discord.File(
            fp=__import__("io").BytesIO(data.encode()),
            filename="keywords.json",
        )
        await ctx.send("Current keywords:", file=fp)

    @keyword.command(name="import")
    async def keyword_import(self, ctx: commands.Context, merge: bool = False):
        """Import keywords from an attached JSON file.

        Pass ``true`` to merge instead of replace.
        """
        if not ctx.message.attachments:
            await ctx.send("Attach a `.json` file exported by `keyword export`.")
            return
        att = ctx.message.attachments[0]
        if not att.filename.endswith(".json"):
            await ctx.send("Attachment must be a `.json` file.")
            return
        try:
            raw  = await att.read()
            data = json.loads(raw)
        except Exception as e:
            await ctx.send(f"Failed to parse JSON: {e}")
            return

        valid = ("higher", "normal", "lower", "negative")
        if not all(k in valid for k in data):
            await ctx.send("JSON must only contain keys: higher, normal, lower, negative.")
            return

        if merge:
            async with self.config.guild(ctx.guild).keywords() as kw:
                for tier, vals in data.items():
                    kw[tier] = list(set(kw.get(tier, [])) | set(vals))
            await ctx.send("✅ Keywords merged from file.")
        else:
            await self.config.guild(ctx.guild).keywords.set(data)
            await ctx.send("✅ Keywords replaced from file.")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def loaddefaults(self, ctx: commands.Context, merge: bool = False):
        """(Re)load the built-in default keywords.

        Pass ``true`` to merge with existing keywords instead of replacing.
        """
        if merge:
            async with self.config.guild(ctx.guild).keywords() as kw:
                for tier, defaults in DEFAULT_KEYWORDS.items():
                    kw[tier] = list(set(kw.get(tier, [])) | set(defaults))
            await ctx.send("Default keywords merged.")
        else:
            await self.config.guild(ctx.guild).keywords.set(deepcopy(DEFAULT_KEYWORDS))
            await ctx.send("Default keywords loaded (previous keywords replaced).")

        kw = await self.config.guild(ctx.guild).keywords()
        counts = ", ".join(
            f"{t}: {len(kw.get(t,[]))}" for t in ("higher","normal","lower","negative")
        )
        await ctx.send(f"Keyword counts — {counts}")

    # ── Processed IDs ─────────────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def processedcount(self, ctx: commands.Context):
        """Show how many post IDs are in the processed-IDs list."""
        ids = await self.config.guild(ctx.guild).processed_ids()
        await ctx.send(f"Stored processed IDs: {len(ids) if ids else 0}")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def clearprocessed(self, ctx: commands.Context):
        """Clear the processed-IDs list (will re-check all visible posts)."""
        await self.config.guild(ctx.guild).processed_ids.set([])
        await ctx.send("✅ Processed IDs cleared.")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setmaxprocessed(self, ctx: commands.Context, max_items: int):
        """Cap the processed-ID list size (minimum 10)."""
        if max_items < 10:
            await ctx.send("Must be at least 10.")
            return
        await self.config.guild(ctx.guild).max_processed.set(max_items)
        await ctx.send(f"Max processed IDs set to {max_items}.")

    # ── Status / info ─────────────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        """Show current configuration and task status."""
        g     = ctx.guild
        cfg   = self.config.guild(g)
        en    = await cfg.enabled()
        subs  = await cfg.subreddits()
        ch_id = await cfg.notify_channel_id()
        iv    = await cfg.interval()
        thr   = await cfg.threshold()
        maxp  = await cfg.max_processed()
        kw    = await cfg.keywords()
        dbg   = await cfg.debug()
        ids   = await cfg.processed_ids()
        flair = await cfg.flair_filter()
        creds = await cfg.reddit_client_id()

        task = self._tasks.get(g.id)
        if task and not task.done():
            task_st = "🟢 Running"
        elif task:
            task_st = "🔴 Stopped (task ended)"
        else:
            task_st = "🔴 Not running"

        ch = g.get_channel(ch_id) if ch_id else None
        await ctx.send(
            f"**RedditMonitor Status**\n"
            f"Enabled: `{en}` | Task: {task_st}\n"
            f"Channel: {ch.mention if ch else '*(not set)*'}\n"
            f"Subreddits: {', '.join(subs) if subs else '*(none)*'}\n"
            f"Interval: {iv}s | Threshold: {thr} | Flair filter: `{flair or 'none'}`\n"
            f"Debug: `{dbg}` | Processed IDs: {len(ids) if ids else 0}/{maxp}\n"
            f"Reddit API: {'✅ Configured' if creds else '❌ Not configured'}\n"
            f"Keywords — higher: {len(kw.get('higher',[]))}, "
            f"normal: {len(kw.get('normal',[]))}, "
            f"lower: {len(kw.get('lower',[]))}, "
            f"negative: {len(kw.get('negative',[]))}"
        )

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def taskinfo(self, ctx: commands.Context):
        """Show detailed task / client state."""
        task = self._tasks.get(ctx.guild.id)
        if not task:
            await ctx.send("❌ No task exists for this guild.")
            return
        lines = [
            f"Task done: {'yes' if task.done() else 'no'}",
            f"Task cancelled: {'yes' if task.cancelled() else 'no'}",
        ]
        if task.done():
            try:
                exc = task.exception()
                lines.append(
                    f"Exception: {type(exc).__name__}: {exc}" if exc else "Completed normally"
                )
            except asyncio.InvalidStateError:
                lines.append("State unknown")
        lines.append(f"Has Reddit client: {'yes' if ctx.guild.id in self._reddit_clients else 'no'}")
        await ctx.send("\n".join(lines))

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def debugmode(self, ctx: commands.Context, enabled: bool):
        """Toggle debug mode (posts alive-pings when no matches are found)."""
        await self.config.guild(ctx.guild).debug.set(enabled)
        await ctx.send(f"Debug mode: `{enabled}`")

    # ── Manual check / tuning ─────────────────────────────────────────────────
    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def checknow(self, ctx: commands.Context):
        """Run one monitoring cycle immediately."""
        reddit = await self._get_reddit(ctx.guild)
        if reddit is None:
            await ctx.send("❌ Reddit credentials not configured.")
            return
        subs = await self.config.guild(ctx.guild).subreddits()
        if not subs:
            await ctx.send("❌ No subreddits configured.")
            return
        await ctx.send("🔍 Running check…")
        try:
            await self._check_subreddits(ctx.guild, reddit, subs)
            await ctx.send("✅ Manual check done.")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def testdetect(self, ctx: commands.Context, *, text: str):
        """Test detection on a title (and optional body after a newline).

        Example:
        ```
        [p]rmonitor testdetect My sodium mod keeps crashing
        java error in logs
        ```
        """
        title, _, body = text.partition("\n")
        kw     = await self.config.guild(ctx.guild).keywords()
        detect = self._score_text(title.strip(), body.strip(), kw)
        lines  = [
            f"**Immediate**: {detect['immediate']}",
            f"**Score**: {detect['score']}  (context boost: +{detect['context_boost']})",
            "**Matches by tier:**",
        ]
        for tier, vals in detect["matches"].items():
            lines.append(f"  {tier}: {', '.join(vals) if vals else '*(none)*'}")
        if detect["breakdown"]:
            lines.append("**Scoring breakdown (first 15):**")
            for kw_name, (tier, pts) in list(detect["breakdown"].items())[:15]:
                lines.append(f"  `{kw_name}` [{tier}] → {pts:+.1f}")
        await ctx.send("\n".join(lines))

    @rmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def tune(self, ctx: commands.Context, subreddit: str, limit: int = 10):
        """Run detection against recent posts to check accuracy.

        Example: ``[p]rmonitor tune HypixelSkyblock 15``
        """
        reddit = await self._get_reddit(ctx.guild)
        if not reddit:
            await ctx.send("Reddit credentials not configured.")
            return

        kw  = await self.config.guild(ctx.guild).keywords()
        sub = subreddit.strip().lstrip("r/")

        await ctx.send(f"🔍 Fetching up to {limit} posts from r/{sub}…")

        try:
            rows = []
            sr   = await reddit.subreddit(sub)
            async for submission in sr.new(limit=limit):
                title  = submission.title or ""
                body   = getattr(submission, "selftext", "") or ""
                detect = self._score_text(title, body, kw)
                would_notify = await self._should_notify(submission, detect, ctx.guild)
                top_kws = ", ".join(
                    (detect["matches"].get("higher") or [])[:2] +
                    (detect["matches"].get("normal") or [])[:3]
                ) or "—"
                rows.append((
                    title[:48],
                    detect["score"],
                    "✓" if would_notify else "✗",
                    top_kws[:30],
                ))

            header = f"{'Title':<50} {'Score':<6} {'Notify':<7} Top keywords\n" + "─" * 85
            body_t = "\n".join(
                f"{t:<50} {s:<6.1f} {n:<7} {k}" for t, s, n, k in rows
            )
            for page in pagify(f"```\n{header}\n{body_t}\n```"):
                await ctx.send(page)
        except Exception as e:
            await ctx.send(f"Error: {e}")

    # ── Owner utilities ───────────────────────────────────────────────────────
    @rmonitor.command()
    @commands.is_owner()
    async def cleartasks(self, ctx: commands.Context):
        """[Owner] Cancel all tasks globally and restart for enabled guilds."""
        cancelled = 0
        for t in self._tasks.values():
            if not t.cancelled():
                t.cancel()
                cancelled += 1
        await asyncio.sleep(2)
        for gid in list(self._tasks.keys()):
            await self._cleanup(gid)
        await ctx.send(f"Cancelled {cancelled} task(s). Restarting…")
        await self._startup_tasks()
        await ctx.send("✅ Tasks restarted for enabled guilds.")