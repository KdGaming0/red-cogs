"""
HypixelMonitor — RedBot cog
Scrapes Hypixel forum categories for mod / tech-support threads and posts
Discord embeds to a configured channel.

Detection tiers
  higher   → immediate notify regardless of threshold (VIP keywords, exact names)
  normal   → +2.0 per single word, +3.0 per phrase
  lower    → +1.0 per single word, +1.5 per phrase
  negative → -2.0 per single word, -2.5 per phrase  (game-economy terms)

Context boost (+0.5 each, capped at +2.0): help-seeking language, tech terms, question patterns.
Title hits are worth 2× their normal score.
"""

import asyncio
import json
import logging
import re
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

# ── Config identifier (change if you fork) ──────────────────────────────────
CONF_ID = 0x5B4C3D2E

# ── Limits ───────────────────────────────────────────────────────────────────
MIN_INTERVAL = 60
DEFAULT_INTERVAL = 900       # 15 minutes
DEFAULT_THRESHOLD = 3.0      # minimum score to trigger a notify
DEFAULT_MAX_PROCESSED = 1000  # rolling window of seen thread IDs

# ── Default keyword lists ─────────────────────────────────────────────────────
# Edit freely — these are only applied when you run `loaddefaults` or `quicksetup`.
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

DEFAULT_FORUM_CATEGORIES = [
    {"url": "https://hypixel.net/forums/skyblock.157/",           "name": "SkyBlock General"},
    {"url": "https://hypixel.net/forums/skyblock-community-help.196/", "name": "SkyBlock Community Help"},
]

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
    # Profile / game mechanic talk (not technical)
    re.compile(r'\b(new\s+profile|fresh\s+profile|my\s+profile|profile\s+reset)\b', re.I),
    # Boss / dungeon game content
    re.compile(r'\b(dungeon\s+(?:run|floor|room)|slayer\s+(?:quest|boss)|dragon\s+(?:eye|armor|fight))\b', re.I),
    # Skill / level game content
    re.compile(r'\b(skill\s+(?:level|cap|xp|exp)|collection\s+(?:level|req))\b', re.I),
]

# Context patterns that raise confidence (each hit +0.5, capped at +2.0)
CONTEXT_PATTERNS = [
    re.compile(r'\b(help|issue|problem|crash|fix|install|setup|configure)\b', re.I),
    re.compile(r"\b(not\s+working|broken|won'?t\s+work|can'?t\s+get|having\s+trouble)\b", re.I),
    re.compile(r'\b(fps|performance|lag|optimization|memory|ram|java)\b', re.I),
    re.compile(r'\b(how\s+do\s+i|how\s+to|anyone\s+know|can\s+someone|need\s+help|please\s+help)\b', re.I),
    re.compile(r'\?', re.I),   # question marks are a strong signal
]


# ─────────────────────────────────────────────────────────────────────────────
class HypixelMonitor(commands.Cog):
    """Monitor Hypixel Forums for mod-related questions and technical help.

    Detection uses keyword tiers: higher (immediate), normal, lower, negative.
    Run ``[p]hmonitor quicksetup #channel`` to get started.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_ID, force_registration=True)

        default_guild = {
            "enabled": False,
            "notify_channel_id": None,
            "forum_categories": DEFAULT_FORUM_CATEGORIES,
            "interval": DEFAULT_INTERVAL,
            "threshold": DEFAULT_THRESHOLD,
            "keywords": {"higher": [], "normal": [], "lower": [], "negative": []},
            "processed_ids": [],
            "max_processed": DEFAULT_MAX_PROCESSED,
            "debug": False,
        }
        self.config.register_guild(**default_guild)

        # Per-guild async state
        self._tasks:       Dict[int, asyncio.Task]       = {}
        self._sessions:    Dict[int, aiohttp.ClientSession] = {}
        self._task_locks:  Dict[int, asyncio.Lock]       = {}
        # Per-guild lock for processed-ID writes (avoids a global bottleneck)
        self._proc_locks:  Dict[int, asyncio.Lock]       = {}

        self._ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def cog_load(self) -> None:
        await self._startup_tasks()

    async def cog_unload(self) -> None:
        LOGGER.info("Shutting down HypixelMonitor…")
        tasks = list(self._tasks.values())
        for t in tasks:
            if not t.cancelled():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for s in self._sessions.values():
            await s.close()
        self._sessions.clear()
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

    # ── Session helper ────────────────────────────────────────────────────────
    async def _get_session(self, guild: discord.Guild) -> aiohttp.ClientSession:
        s = self._sessions.get(guild.id)
        if s and not s.closed:
            return s
        s = aiohttp.ClientSession(
            headers={"User-Agent": self._ua},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._sessions[guild.id] = s
        return s

    # ── Detection ─────────────────────────────────────────────────────────────
    @staticmethod
    def _score_text(
        title: str,
        body: str,
        keywords: Dict[str, List[str]],
    ) -> Dict:
        """
        Score a post against keyword tiers.

        Returns:
            immediate (bool): True if any "higher" keyword matched.
            score     (float): Aggregate relevance score.
            matches   (dict):  {tier: [matched keywords]}
            breakdown (dict):  Scoring detail for debugging.
        """
        title_l = title.lower()
        body_l  = body.lower()
        combined = f"{title_l}\n{body_l}"

        matches   = {"higher": [], "normal": [], "lower": [], "negative": []}
        breakdown = {}   # keyword → (tier, points_awarded)

        # ── Tier weights ─────────────────────────────────────────────────────
        # (title_score, body_score)  for single-word / phrase
        TIER_WEIGHT = {
            "higher":   (0,    0),     # just flips `immediate` flag
            "normal":   (6.0,  3.0),   # phrase; single-word = half
            "lower":    (3.0,  1.5),
            "negative": (-4.0, -2.0),
        }
        # For single-word keywords, we use half the phrase weight
        SINGLE_DIVISOR = 2.0

        score = 0.0
        for tier in ("higher", "normal", "lower", "negative"):
            for kw in keywords.get(tier, []):
                kw_l = kw.lower()
                if " " in kw_l:
                    in_title = kw_l in title_l
                    in_body  = kw_l in body_l
                    if in_title or in_body:
                        matches[tier].append(kw)
                        tw, bw = TIER_WEIGHT[tier]
                        pts = (tw if in_title else 0) + (bw if (in_body and not in_title) else 0)
                        # if in both, use title weight (it's higher)
                        if in_title:
                            pts = tw
                        score += pts
                        breakdown[kw] = (tier, pts)
                else:
                    pattern = rf'\b{re.escape(kw_l)}\b'
                    in_title = bool(re.search(pattern, title_l))
                    in_body  = bool(re.search(pattern, body_l))
                    if in_title or in_body:
                        matches[tier].append(kw)
                        tw, bw = TIER_WEIGHT[tier]
                        tw /= SINGLE_DIVISOR
                        bw /= SINGLE_DIVISOR
                        pts = (tw if in_title else bw)
                        score += pts
                        breakdown[kw] = (tier, pts)

        # ── Context boost (capped at +2.0) ───────────────────────────────────
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
        thread_data: dict,
        detect: dict,
        guild: discord.Guild,
    ) -> bool:
        if detect["immediate"]:
            return True

        threshold = await self.config.guild(guild).threshold()
        if detect["score"] < threshold:
            return False

        title    = thread_data.get("title", "").lower()
        body     = thread_data.get("content", "").lower()
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
    async def _notify(self, guild: discord.Guild, thread: dict, detect: dict):
        channel_id = await self.config.guild(guild).notify_channel_id()
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        score = detect["score"]
        if detect["immediate"]:
            confidence, color = "🔴 HIGH (Immediate)", discord.Color.red()
        elif score >= 6.0:
            confidence, color = "🟠 HIGH",   discord.Color.orange()
        elif score >= 3.0:
            confidence, color = "🟡 MEDIUM", discord.Color.gold()
        else:
            confidence, color = "🟢 LOW",    discord.Color.green()

        content = thread.get("content", "") or ""
        embed = discord.Embed(
            title=thread.get("title", "Unknown")[:256],
            url=thread.get("url", ""),
            description=(content[:500] + "…") if len(content) > 500 else content or "No preview",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Confidence", value=confidence,       inline=True)
        embed.add_field(name="Score",      value=f"{score:.1f}",   inline=True)
        embed.add_field(name="Category",   value=thread.get("category", "?"), inline=True)

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

        embed.set_footer(text=f"by {thread.get('author','?')} • Hypixel Forums")
        try:
            await channel.send(embed=embed)
        except Exception:
            LOGGER.exception("Failed to send notification")

    # ── Processed-ID helpers ─────────────────────────────────────────────────
    def _proc_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._proc_locks:
            self._proc_locks[guild_id] = asyncio.Lock()
        return self._proc_locks[guild_id]

    async def _add_processed(self, guild: discord.Guild, thread_id: str):
        async with self._proc_lock(guild.id):
            processed = await self.config.guild(guild).processed_ids() or []
            maxp = await self.config.guild(guild).max_processed()
            if thread_id not in processed:
                processed.append(thread_id)
            if len(processed) > maxp:
                processed = processed[-maxp:]
            await self.config.guild(guild).processed_ids.set(processed)

    async def _is_processed(self, guild: discord.Guild, thread_id: str) -> bool:
        processed = await self.config.guild(guild).processed_ids()
        return bool(processed) and thread_id in processed

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

    # ── Forum scraping ────────────────────────────────────────────────────────
    async def _get_thread_content(
        self, session: aiohttp.ClientSession, url: str
    ) -> str:
        try:
            async with session.get(url) as r:
                if r.status == 200:
                    soup = BeautifulSoup(await r.text(), "html.parser")
                    el = soup.select_one(".message-body .message-userContent") or \
                         soup.select_one(".message--post .message-body")
                    if el:
                        return re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        except Exception as e:
            LOGGER.warning("Content fetch failed %s: %s", url, e)
        return ""

    async def _get_recent_threads(
        self, session: aiohttp.ClientSession, category: Dict[str, str]
    ) -> List[Dict]:
        threads = []
        try:
            async with session.get(category["url"]) as r:
                if r.status != 200:
                    return threads
                soup = BeautifulSoup(await r.text(), "html.parser")
                for item in soup.select(".structItem--thread"):
                    try:
                        cls   = " ".join(item.get("class", []))
                        m     = re.search(r"js-threadListItem-(\d+)", cls)
                        if not m:
                            continue
                        tid   = m.group(1)
                        title_el = item.select_one(".structItem-title")
                        if not title_el:
                            continue
                        title  = title_el.get_text(strip=True)
                        a      = title_el.select_one("a")
                        if not a:
                            continue
                        url    = urljoin("https://hypixel.net", a["href"])
                        author_el = item.select_one(".structItem-minor .username") or \
                                    item.select_one(".username")
                        author = author_el.get_text(strip=True) if author_el else "Unknown"
                        threads.append({
                            "id": tid, "title": title, "url": url,
                            "author": author, "category": category["name"],
                            "content": "",
                        })
                    except Exception as e:
                        LOGGER.warning("Thread parse error: %s", e)
        except Exception as e:
            LOGGER.error("Category fetch error (%s): %s", category["name"], e)
        return threads

    # ── Monitoring loop ───────────────────────────────────────────────────────
    async def _monitor_guild(self, guild: discord.Guild):
        LOGGER.info("Monitor started: guild %s", guild.id)
        try:
            while True:
                try:
                    if not await self.config.guild(guild).enabled():
                        LOGGER.info("Monitoring disabled, stopping: guild %s", guild.id)
                        break

                    cats = await self.config.guild(guild).forum_categories()
                    if not cats:
                        await self._debug(guild, "⚠️ Monitor alive — no forum categories configured.")
                    else:
                        await self._check_categories(guild, cats)

                    interval = await self.config.guild(guild).interval()
                    interval = max(interval, MIN_INTERVAL)
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

    async def _check_categories(self, guild: discord.Guild, cats: List[Dict]):
        keywords = await self.config.guild(guild).keywords()
        session  = await self._get_session(guild)
        notified = 0
        checked  = 0

        for cat in cats:
            try:
                threads = await self._get_recent_threads(session, cat)
                for thread in threads:
                    checked += 1
                    if await self._is_processed(guild, thread["id"]):
                        continue
                    if not thread["content"]:
                        thread["content"] = await self._get_thread_content(
                            session, thread["url"]
                        )
                    detect = self._score_text(
                        thread["title"], thread["content"], keywords
                    )
                    if await self._should_notify(thread, detect, guild):
                        await self._notify(guild, thread, detect)
                        notified += 1
                        LOGGER.info("Notified: %s in %s (guild %s)", thread["id"], cat["name"], guild.id)
                    await self._add_processed(guild, thread["id"])
            except Exception:
                LOGGER.exception("Category error (%s): guild %s", cat["name"], guild.id)

        if notified == 0:
            await self._debug(
                guild,
                f"✅ Monitor alive — checked {checked} threads across "
                f"{len(cats)} category/ies. No matches this cycle.",
            )

    # ── Task management ───────────────────────────────────────────────────────
    async def _cleanup(self, guild_id: int):
        self._tasks.pop(guild_id, None)
        s = self._sessions.pop(guild_id, None)
        if s:
            try:
                await s.close()
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
    async def hmonitor(self, ctx: commands.Context):
        """Hypixel forum monitor. Start with ``quicksetup``, then ``enable``.

        **Quick start**
        ```
        [p]hmonitor quicksetup #channel
        [p]hmonitor enable
        ```
        """
        await ctx.send_help()

    # ── Setup ─────────────────────────────────────────────────────────────────
    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def quicksetup(self, ctx: commands.Context, channel: discord.TextChannel):
        """One-shot setup: sets channel, loads default keywords & categories."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await self.config.guild(ctx.guild).keywords.set(deepcopy(DEFAULT_KEYWORDS))
        await self.config.guild(ctx.guild).forum_categories.set(deepcopy(DEFAULT_FORUM_CATEGORIES))
        await ctx.send(
            f"✅ Quick setup complete!\n"
            f"📢 Channel: {channel.mention}\n"
            f"🔑 Default keywords loaded\n"
            f"📂 Default forum categories loaded\n"
            f"▶️  Run `{ctx.prefix}hmonitor enable` to start."
        )

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the notification channel."""
        await self.config.guild(ctx.guild).notify_channel_id.set(channel.id)
        await ctx.send(f"Notification channel set to {channel.mention}.")

    # ── Enable / disable ──────────────────────────────────────────────────────
    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def enable(self, ctx: commands.Context):
        """Start monitoring."""
        if await self.config.guild(ctx.guild).enabled():
            await ctx.send("Already enabled. Use `disable` to stop.")
            return
        await self.config.guild(ctx.guild).enabled.set(True)
        await self._ensure_task(ctx.guild)
        await ctx.send("✅ Monitoring enabled.")

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context):
        """Stop monitoring."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await self._stop_task(ctx.guild)
        await ctx.send("⏹ Monitoring disabled.")

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def restart(self, ctx: commands.Context):
        """Restart the monitoring task."""
        await self._stop_task(ctx.guild)
        await asyncio.sleep(1)
        await self._ensure_task(ctx.guild)
        await ctx.send("♻️ Monitoring task restarted.")

    # ── Interval / threshold ──────────────────────────────────────────────────
    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setinterval(self, ctx: commands.Context, seconds: int):
        """Set check interval in seconds (minimum 60)."""
        if seconds < MIN_INTERVAL:
            await ctx.send(f"Minimum interval is {MIN_INTERVAL} s.")
            return
        await self.config.guild(ctx.guild).interval.set(seconds)
        await ctx.send(f"Interval set to {seconds} s.")

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setthreshold(self, ctx: commands.Context, threshold: float):
        """Set detection threshold (1.0 – 10.0). Lower = more sensitive."""
        if not 1.0 <= threshold <= 10.0:
            await ctx.send("Threshold must be between 1.0 and 10.0.")
            return
        await self.config.guild(ctx.guild).threshold.set(threshold)
        await ctx.send(f"Threshold set to {threshold}.")

    # ── Forum categories ──────────────────────────────────────────────────────
    @hmonitor.group(name="category", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def category(self, ctx: commands.Context):
        """Manage monitored forum categories."""
        await ctx.send_help()

    @category.command(name="add")
    async def category_add(self, ctx: commands.Context, url: str, *, name: str):
        """Add a forum category URL with a friendly name."""
        async with self.config.guild(ctx.guild).forum_categories() as cats:
            if any(c["url"] == url or c["name"] == name for c in cats):
                await ctx.send("A category with that URL or name already exists.")
                return
            cats.append({"url": url, "name": name})
        await ctx.send(f"Added category: **{name}**")

    @category.command(name="remove")
    async def category_remove(self, ctx: commands.Context, *, name: str):
        """Remove a forum category by name."""
        async with self.config.guild(ctx.guild).forum_categories() as cats:
            before = len(cats)
            cats[:] = [c for c in cats if c["name"] != name]
            if len(cats) == before:
                await ctx.send("No category with that name found.")
                return
        await ctx.send(f"Removed category: **{name}**")

    @category.command(name="list")
    async def category_list(self, ctx: commands.Context):
        """List all monitored forum categories."""
        cats = await self.config.guild(ctx.guild).forum_categories()
        if not cats:
            await ctx.send("No categories configured.")
            return
        lines = ["**Monitored categories**"]
        for c in cats:
            lines.append(f"• **{c['name']}** — {c['url']}")
        for page in pagify("\n".join(lines)):
            await ctx.send(page)

    # ── Keywords ──────────────────────────────────────────────────────────────
    @hmonitor.group(name="keyword", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def keyword(self, ctx: commands.Context):
        """Manage detection keywords.

        Tiers: ``higher`` · ``normal`` · ``lower`` · ``negative``
        """
        await ctx.send_help()

    @keyword.command(name="add")
    async def keyword_add(self, ctx: commands.Context, tier: str, *, keyword: str):
        """Add one keyword to a tier.

        Example: ``[p]hmonitor keyword add normal skyhanni``
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
        """Add multiple comma-separated keywords to a tier at once.

        Example: ``[p]hmonitor keyword bulkadd normal skyhanni, skyblocker, sodium``
        """
        tier = tier.lower()
        if tier not in ("higher", "normal", "lower", "negative"):
            await ctx.send("Invalid tier. Use: `higher`, `normal`, `lower`, or `negative`.")
            return
        new_kws = [k.strip() for k in keywords.split(",") if k.strip()]
        if not new_kws:
            await ctx.send("No keywords found. Separate them with commas.")
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

        Example: ``[p]hmonitor keyword list normal``
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

        Example: ``[p]hmonitor keyword find sodium``
        """
        kw = await self.config.guild(ctx.guild).keywords()
        search_l = search.lower()
        found = []
        for tier in ("higher", "normal", "lower", "negative"):
            for k in kw.get(tier, []):
                if search_l in k.lower():
                    found.append(f"**{tier}**: `{k}`")
        if found:
            await ctx.send("\n".join(found))
        else:
            await ctx.send(f"No keywords matching `{search}` found in any tier.")

    @keyword.command(name="export")
    async def keyword_export(self, ctx: commands.Context):
        """Export keywords as a JSON file you can re-import later."""
        kw = await self.config.guild(ctx.guild).keywords()
        data = json.dumps(kw, indent=2)
        fp = discord.File(
            fp=__import__("io").BytesIO(data.encode()),
            filename="keywords.json",
        )
        await ctx.send("Here are your current keywords:", file=fp)

    @keyword.command(name="import")
    async def keyword_import(self, ctx: commands.Context, merge: bool = False):
        """Import keywords from an attached JSON file.

        Pass ``true`` as the second argument to merge instead of replace.
        """
        if not ctx.message.attachments:
            await ctx.send("Please attach a JSON file exported by `keyword export`.")
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
            await ctx.send("JSON must have only keys: higher, normal, lower, negative.")
            return

        if merge:
            async with self.config.guild(ctx.guild).keywords() as kw:
                for tier, vals in data.items():
                    existing = set(kw.get(tier, []))
                    kw[tier] = list(existing | set(vals))
            await ctx.send("✅ Keywords merged from file.")
        else:
            await self.config.guild(ctx.guild).keywords.set(data)
            await ctx.send("✅ Keywords replaced from file.")

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def loaddefaults(self, ctx: commands.Context, merge: bool = False):
        """(Re)load the built-in default keywords.

        Pass ``true`` to merge with existing keywords instead of replacing.
        """
        if merge:
            async with self.config.guild(ctx.guild).keywords() as kw:
                for tier, defaults in DEFAULT_KEYWORDS.items():
                    existing = set(kw.get(tier, []))
                    kw[tier] = list(existing | set(defaults))
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
    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def processedcount(self, ctx: commands.Context):
        """Show how many thread IDs are stored in the processed-IDs list."""
        ids = await self.config.guild(ctx.guild).processed_ids()
        await ctx.send(f"Stored processed IDs: {len(ids) if ids else 0}")

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def clearprocessed(self, ctx: commands.Context):
        """Clear the processed-IDs list (will re-check all visible threads)."""
        await self.config.guild(ctx.guild).processed_ids.set([])
        await ctx.send("✅ Processed IDs cleared.")

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def setmaxprocessed(self, ctx: commands.Context, max_items: int):
        """Cap the processed-ID list size (minimum 10)."""
        if max_items < 10:
            await ctx.send("Must be at least 10.")
            return
        await self.config.guild(ctx.guild).max_processed.set(max_items)
        await ctx.send(f"Max processed IDs set to {max_items}.")

    # ── Status / info ─────────────────────────────────────────────────────────
    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        """Show current configuration and task status."""
        g     = ctx.guild
        cfg   = self.config.guild(g)
        en    = await cfg.enabled()
        cats  = await cfg.forum_categories()
        ch_id = await cfg.notify_channel_id()
        iv    = await cfg.interval()
        thr   = await cfg.threshold()
        maxp  = await cfg.max_processed()
        kw    = await cfg.keywords()
        dbg   = await cfg.debug()
        ids   = await cfg.processed_ids()

        task = self._tasks.get(g.id)
        if task and not task.done():
            task_st = "🟢 Running"
        elif task:
            task_st = "🔴 Stopped (task ended)"
        else:
            task_st = "🔴 Not running"

        ch = g.get_channel(ch_id) if ch_id else None
        await ctx.send(
            f"**HypixelMonitor Status**\n"
            f"Enabled: `{en}` | Task: {task_st}\n"
            f"Channel: {ch.mention if ch else '*(not set)*'}\n"
            f"Categories: {len(cats)} | Interval: {iv}s | Threshold: {thr}\n"
            f"Debug: `{dbg}` | Processed IDs stored: {len(ids) if ids else 0}/{maxp}\n"
            f"Keywords — higher: {len(kw.get('higher',[]))}, "
            f"normal: {len(kw.get('normal',[]))}, "
            f"lower: {len(kw.get('lower',[]))}, "
            f"negative: {len(kw.get('negative',[]))}"
        )

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def taskinfo(self, ctx: commands.Context):
        """Show detailed task / session state."""
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
                lines.append(f"Exception: {type(exc).__name__}: {exc}" if exc else "Completed normally")
            except asyncio.InvalidStateError:
                lines.append("State unknown")
        lines.append(f"Has session: {'yes' if ctx.guild.id in self._sessions else 'no'}")
        await ctx.send("\n".join(lines))

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def debugmode(self, ctx: commands.Context, enabled: bool):
        """Toggle debug mode (posts alive-pings when no matches are found)."""
        await self.config.guild(ctx.guild).debug.set(enabled)
        await ctx.send(f"Debug mode: `{enabled}`")

    # ── Manual check / tuning ─────────────────────────────────────────────────
    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def checknow(self, ctx: commands.Context):
        """Run one monitoring cycle immediately."""
        cats = await self.config.guild(ctx.guild).forum_categories()
        if not cats:
            await ctx.send("❌ No forum categories configured.")
            return
        await ctx.send("🔍 Running check…")
        try:
            await self._check_categories(ctx.guild, cats)
            await ctx.send("✅ Manual check done.")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def testdetect(self, ctx: commands.Context, *, text: str):
        """Test detection on a title (and optional body after a newline).

        Example:
        ```
        [p]hmonitor testdetect My sodium mod keeps crashing
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
            lines.append("**Scoring breakdown:**")
            for kw_name, (tier, pts) in list(detect["breakdown"].items())[:15]:
                lines.append(f"  `{kw_name}` [{tier}] → {pts:+.1f}")
        await ctx.send("\n".join(lines))

    @hmonitor.command()
    @commands.admin_or_permissions(manage_guild=True)
    async def tune(self, ctx: commands.Context, category_name: str = None, limit: int = 10):
        """Run detection against recent threads to check accuracy.

        Omit ``category_name`` to use the first configured category.
        """
        cats = await self.config.guild(ctx.guild).forum_categories()
        if not cats:
            await ctx.send("No categories configured.")
            return

        if category_name:
            cat = next((c for c in cats if c["name"].lower() == category_name.lower()), None)
            if not cat:
                names = ", ".join(c["name"] for c in cats)
                await ctx.send(f"Category not found. Available: {names}")
                return
            test_cats = [cat]
        else:
            test_cats = [cats[0]]

        kw      = await self.config.guild(ctx.guild).keywords()
        session = await self._get_session(ctx.guild)

        await ctx.send(f"🔍 Fetching up to {limit} threads from **{test_cats[0]['name']}**…")

        try:
            rows = []
            for cat in test_cats:
                threads = await self._get_recent_threads(session, cat)
                for thread in threads[:limit]:
                    if not thread["content"]:
                        thread["content"] = await self._get_thread_content(
                            session, thread["url"]
                        )
                    detect = self._score_text(
                        thread["title"], thread["content"], kw
                    )
                    would_notify = await self._should_notify(thread, detect, ctx.guild)
                    top_kws = ", ".join(
                        (detect["matches"].get("higher") or [])[:2] +
                        (detect["matches"].get("normal") or [])[:3]
                    ) or "—"
                    rows.append((
                        thread["title"][:48],
                        detect["score"],
                        "✓" if would_notify else "✗",
                        top_kws[:30],
                    ))

            header = f"{'Title':<50} {'Score':<6} {'Notify':<7} Top keywords\n" + "─" * 85
            body   = "\n".join(
                f"{t:<50} {s:<6.1f} {n:<7} {k}" for t, s, n, k in rows
            )
            for page in pagify(f"```\n{header}\n{body}\n```"):
                await ctx.send(page)
        except Exception as e:
            await ctx.send(f"Error: {e}")

    # ── Owner utilities ───────────────────────────────────────────────────────
    @hmonitor.command()
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