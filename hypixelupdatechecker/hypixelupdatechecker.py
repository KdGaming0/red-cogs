"""
HypixelUpdateChecker — Red V3 Cog
Monitors Hypixel forums for new/updated SkyBlock posts and sends
formatted Discord embeds to a configured channel.

Sources:
  • skyblock-patch-notes.158   — all posts are SkyBlock, posted by Hypixel Team
  • news-and-announcements.4   — filtered: Hypixel Team only + "skyblock" in title
  • skyblock-alpha/            — filtered: Hypixel Team only (anyone can post here)

Pinned/sticky threads (like alpha changelogs) are re-checked every poll
for edits; an "Updated" embed is sent if the content changes.
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime
from typing import Optional

import aiohttp
import discord
from bs4 import BeautifulSoup
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.hypixelupdatechecker")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hypixel Team's member profile path — used to confirm a post is official.
# This is stable because it's a numeric member ID, not just a display name.
HYPIXEL_TEAM_MEMBER_PATH = "/members/hypixel-team.377696/"

SOURCES: dict[str, dict] = {
    "patch_notes": {
        "url": "https://hypixel.net/forums/skyblock-patch-notes.158/",
        "label": "SkyBlock Patch Notes",
        "emoji": "📋",
        "color": 0x55AAFF,
        # Every post in this forum is from Hypixel Team & is SkyBlock — no extra filter needed
        "require_hypixel_team": False,
        "require_skyblock_in_title": False,
    },
    "news": {
        "url": "https://hypixel.net/forums/news-and-announcements.4/",
        "label": "News & Announcements",
        "emoji": "📰",
        "color": 0xFFAA00,
        # Mixed forum — need both Hypixel Team author AND SkyBlock in title
        "require_hypixel_team": True,
        "require_skyblock_in_title": True,
    },
    "alpha": {
        "url": "https://hypixel.net/skyblock-alpha/",
        "label": "SkyBlock Alpha",
        "emoji": "🔬",
        "color": 0xAA55FF,
        # Anyone can post here — only Hypixel Team posts are relevant
        "require_hypixel_team": True,
        "require_skyblock_in_title": False,
    },
}

SKYBLOCK_KEYWORDS = ["skyblock", "sky block"]


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

def _is_skyblock_title(title: str) -> bool:
    lower = title.lower()
    return any(kw in lower for kw in SKYBLOCK_KEYWORDS)


def _truncate(text: str, limit: int = 450) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + " …"


def _content_hash(text: str) -> str:
    """SHA-1 fingerprint of post text for edit detection."""
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    headers = {"User-Agent": "HypixelUpdateChecker-RedBot/2.0 (compatible)"}
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status == 200:
                return await resp.text()
            log.warning("HTTP %s fetching %s", resp.status, url)
    except Exception as exc:
        log.error("Error fetching %s: %s", url, exc)
    return None


_THREAD_URL_RE = re.compile(r"^/threads/[^/]+\.(\d+)/?$")


def _find_container(tag):
    """
    Walk up the DOM from a tag to the XenForo thread row element.

    The correct container is <div class="structItem structItem--thread ...">
    which carries data-author and contains all member links for the thread.

    Must check for "structItem--thread" specifically — NOT just "structItem" —
    because child divs like "structItem-title" and "structItem-cell" are also
    prefixed with "structItem" and would cause an early stop.
    """
    node = tag
    for _ in range(12):
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if parent.name == "div":
            classes = " ".join(parent.get("class", []))
            if "structItem--thread" in classes:
                return parent
        node = parent
    return node


def _parse_thread_list(html: str, source_cfg: dict) -> list[dict]:
    """
    Parse a XenForo forum listing page (no-JS version).

    XenForo's JavaScript-disabled HTML does not include data-content attributes
    on thread rows. Instead we find all links matching /threads/slug.ID/ and
    climb up to their containing row element to gather metadata.

    Returns list of thread dicts:
        thread_id   str   — numeric ID from URL
        title       str
        url         str
        is_sticky   bool  — pinned threads are re-checked for edits each poll
        is_official bool  — has XenForo 'Official' label
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_ids: set[str] = set()

    for link in soup.find_all("a", href=_THREAD_URL_RE):
        href = link.get("href", "")
        m = _THREAD_URL_RE.match(href)
        if not m:
            continue
        thread_id = m.group(1)

        # Deduplicate (pagination controls repeat links)
        if thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)

        title = link.get_text(strip=True)
        if not title:
            continue

        full_url = "https://hypixel.net" + href

        # ── Walk up to the row container ──────────────────────────────────
        container = _find_container(link)
        container_text = container.get_text(separator=" ")

        # ── Author filter ─────────────────────────────────────────────────
        # XenForo puts data-author="Display Name" on the structItem row.
        # We check that first (fast), then fall back to scanning for a member link.
        container_data_author = container.get("data-author", "")
        posted_by_hypixel_team = (
            "hypixel team" in container_data_author.lower()
            or bool(container.find("a", href=lambda h: h and HYPIXEL_TEAM_MEMBER_PATH in h))
        )

        if source_cfg["require_hypixel_team"] and not posted_by_hypixel_team:
            continue

        # ── SkyBlock title filter ─────────────────────────────────────────
        if source_cfg["require_skyblock_in_title"] and not _is_skyblock_title(title):
            continue

        # ── Sticky / Official flags ───────────────────────────────────────
        is_sticky = "Sticky" in container_text
        is_official = "Official" in container_text

        results.append({
            "thread_id": thread_id,
            "title": title,
            "url": full_url,
            "is_sticky": is_sticky,
            "is_official": is_official,
        })

    return results


def _parse_post_content(html: str) -> dict:
    """
    Extract content from the first post of a thread page (no-JS XenForo HTML).

    Returns:
        preview   str        — short plain-text preview
        spoilers  list[str]  — spoiler section titles (e.g. "New Plot: Greenhouse")
        raw_hash  str        — SHA-1 of full text, used to detect edits
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Find the first post body ──────────────────────────────────────────
    # Priority order: try progressively broader selectors
    post_body = (
        # JS-rendered XenForo
        soup.find("div", class_="bbWrapper")
        # or the article tag
        or soup.find("article", class_=re.compile(r"message--post"))
        # no-JS fallback: first <div class="...message...">
        or soup.find("div", class_=re.compile(r"message-body|messageContent"))
        # last resort: the biggest block of text on the page
    )

    if not post_body:
        # Try finding the first big block of paragraph text
        candidates = soup.find_all(["div", "article"], class_=re.compile(r"block|content|post"))
        post_body = max(candidates, key=lambda t: len(t.get_text()), default=None)

    if not post_body:
        return {"preview": "", "spoilers": [], "raw_hash": ""}

    # ── Extract spoiler titles BEFORE removing content ────────────────────
    # JS version:  <span class="bbCodeSpoiler-button-title">Section Name</span>
    # no-JS version: <div class="bbCodeSpoiler"><span>Spoiler: Section Name</span>...</div>
    spoiler_titles = []

    # Try JS-rendered selector first
    for btn in post_body.find_all(class_=re.compile(r"bbCodeSpoiler-button-title")):
        label = btn.get_text(strip=True)
        label = re.sub(r"^spoiler\s*:?\s*", "", label, flags=re.IGNORECASE).strip()
        if label and label not in spoiler_titles:
            spoiler_titles.append(label)

    # no-JS fallback: look for text "Spoiler: Something" patterns in any tag
    if not spoiler_titles:
        for tag in post_body.find_all(string=re.compile(r"spoiler\s*:", re.IGNORECASE)):
            label = re.sub(r"^spoiler\s*:?\s*", "", str(tag), flags=re.IGNORECASE).strip()
            if label and label not in spoiler_titles:
                spoiler_titles.append(label)

    # ── Strip noise before extracting preview text ────────────────────────
    for tag in post_body.find_all(["blockquote", "img", "figure", "script", "style"]):
        tag.decompose()
    for tag in post_body.find_all(class_=re.compile(r"bbCodeSpoiler-content|js-spoilerTarget")):
        tag.decompose()

    full_text = post_body.get_text(separator="\n", strip=True)
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    clean_text = "\n".join(lines)

    return {
        "preview": _truncate(clean_text, 450),
        "spoilers": spoiler_titles[:10],
        "raw_hash": _content_hash(clean_text),
    }


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------

class HypixelUpdateChecker(commands.Cog):
    """
    Periodically checks the Hypixel forums for new (and updated) SkyBlock
    posts and sends nicely formatted Discord embeds to a configured channel.

    **Three monitored sources:**
    • `patch_notes` — SkyBlock Patch Notes forum (all official posts)
    • `news`        — News & Announcements (Hypixel Team + SkyBlock in title)
    • `alpha`       — SkyBlock Alpha forum (Hypixel Team only — anyone can post here)

    Pinned/sticky threads (e.g. alpha changelogs) are re-checked on every
    poll so edits adding new spoiler sections days later are also announced.
    """

    DEFAULT_INTERVAL = 900  # 30 minutes — respectful default for external scraping

    default_guild = {
        "channel_id": None,
        "post_previews": True,
        "check_interval": 900,  # seconds between polls
        "enabled_sources": {
            "patch_notes": True,
            "news": True,
            "alpha": True,
        },
        # Role ID to ping per source, or None for no ping
        "ping_roles": {
            "patch_notes": None,
            "news": None,
            "alpha": None,
        },
        # Stored as { thread_id: { "hash": str, "is_sticky": bool } }
        "seen_threads": {
            "patch_notes": {},
            "news": {},
            "alpha": {},
        },
    }

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x48595058454C32, force_registration=True
        )
        self.config.register_guild(**self.default_guild)
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def cog_load(self):
        self._task = self.bot.loop.create_task(self._update_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    # ── Background loop ──────────────────────────────────────────────────

    async def _update_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._check_all_guilds()
            except Exception:
                log.exception("Unhandled exception in update loop")
            # Use the shortest interval across all guilds (minimum 300s / 5 min)
            intervals = []
            for guild in self.bot.guilds:
                try:
                    iv = await self.config.guild(guild).check_interval()
                    intervals.append(iv)
                except Exception:
                    pass
            sleep_for = max(300, min(intervals)) if intervals else self.DEFAULT_INTERVAL
            await asyncio.sleep(sleep_for)

    async def _check_all_guilds(self):
        async with aiohttp.ClientSession() as session:
            for guild in self.bot.guilds:
                try:
                    await self._check_guild(session, guild)
                except Exception:
                    log.exception("Error checking guild %s", guild.id)

    async def _check_guild(self, session: aiohttp.ClientSession, guild: discord.Guild):
        conf = self.config.guild(guild)

        channel_id = await conf.channel_id()
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        enabled = await conf.enabled_sources()
        seen: dict = await conf.seen_threads()
        do_previews = await conf.post_previews()
        ping_roles: dict = await conf.ping_roles()

        for source_key, source_cfg in SOURCES.items():
            if not enabled.get(source_key, True):
                continue

            await asyncio.sleep(3)  # pause between sources
            listing_html = await _fetch_html(session, source_cfg["url"])
            if not listing_html:
                continue

            threads = _parse_thread_list(listing_html, source_cfg)
            source_seen: dict = seen.get(source_key, {})

            # Process oldest-first so Discord posts appear in chronological order
            for thread in reversed(threads):
                tid = thread["thread_id"]
                known = source_seen.get(tid)          # None = brand new

                # We always need post content for sticky threads (edit detection)
                # and optionally for previews
                need_content = do_previews or thread["is_sticky"]
                post_data: dict = {}

                if need_content:
                    await asyncio.sleep(2)  # brief pause between requests
                    thread_html = await _fetch_html(session, thread["url"])
                    if thread_html:
                        post_data = _parse_post_content(thread_html)

                new_hash = post_data.get("raw_hash", "")

                if known is None:
                    # ── Brand-new thread ─────────────────────────────────
                    embed = self._build_embed(
                        thread, source_cfg, post_data, is_update=False
                    )
                    await self._safe_send(channel, embed, ping_roles.get(source_key))
                    source_seen[tid] = {
                        "hash": new_hash,
                        "is_sticky": thread["is_sticky"],
                    }

                elif thread["is_sticky"] and new_hash and new_hash != known.get("hash", ""):
                    # ── Pinned thread was edited ─────────────────────────
                    embed = self._build_embed(
                        thread, source_cfg, post_data, is_update=True
                    )
                    await self._safe_send(channel, embed, ping_roles.get(source_key))
                    source_seen[tid]["hash"] = new_hash

            seen[source_key] = source_seen

        await conf.seen_threads.set(seen)

    # ── Embed builder ────────────────────────────────────────────────────

    def _build_embed(
        self,
        thread: dict,
        source_cfg: dict,
        post_data: dict,
        is_update: bool,
    ) -> discord.Embed:

        if is_update:
            author_text = f"📝 Updated — {source_cfg['label']}"
        elif thread.get("is_sticky"):
            author_text = f"📌 Pinned — {source_cfg['label']}"
        else:
            author_text = f"{source_cfg['emoji']} New Post — {source_cfg['label']}"

        embed = discord.Embed(
            title=thread["title"],
            url=thread["url"],
            color=source_cfg["color"],
        )
        embed.set_author(
            name=author_text,
            icon_url="https://hypixel.net/favicon-32x32.png",
        )

        # Post preview text
        preview = post_data.get("preview", "")
        if preview:
            embed.description = preview

        # Spoiler section titles — lets users know what's inside the dropdowns
        spoilers: list = post_data.get("spoilers", [])
        if spoilers:
            embed.add_field(
                name="📂 Sections in this post",
                value="\n".join(f"▸ {s}" for s in spoilers),
                inline=False,
            )

        embed.add_field(
            name="🔗 Read More",
            value=f"[Click to open on Hypixel Forums]({thread['url']})",
            inline=False,
        )

        embed.timestamp = datetime.utcnow()
        embed.set_footer(text="Hypixel SkyBlock Update Checker")
        return embed

    async def _safe_send(
        self,
        channel: discord.TextChannel,
        embed: discord.Embed,
        role_id: Optional[int] = None,
    ):
        try:
            if role_id:
                role = channel.guild.get_role(role_id)
                if role:
                    await channel.send(
                        role.mention,
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Failed to send embed to %s: %s", channel.id, exc)

    # ── Commands ─────────────────────────────────────────────────────────

    @commands.group(name="hypixel", invoke_without_command=True)
    @commands.guild_only()
    async def hypixel(self, ctx: commands.Context):
        """Hypixel SkyBlock Update Checker commands."""
        await ctx.send_help(ctx.command)

    @hypixel.command(name="setchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def set_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel to post update notifications in.

        **Example:** `[p]hypixel setchannel #skyblock-updates`
        """
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"✅ Updates will be posted to {channel.mention}.")

    @hypixel.command(name="status")
    async def status(self, ctx: commands.Context):
        """Show the current configuration and status."""
        conf = self.config.guild(ctx.guild)
        channel_id = await conf.channel_id()
        enabled = await conf.enabled_sources()
        do_previews = await conf.post_previews()
        seen = await conf.seen_threads()
        ping_roles = await conf.ping_roles()
        interval = await conf.check_interval()

        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        channel_str = channel.mention if channel else "❌ Not set"

        source_lines = []
        for k, v in SOURCES.items():
            state = "✅" if enabled.get(k, True) else "❌"
            source_data = seen.get(k, {})
            total = len(source_data)
            stickies = sum(
                1 for d in source_data.values()
                if isinstance(d, dict) and d.get("is_sticky")
            )
            detail = f"({total} seen" + (f", {stickies} pinned" if stickies else "") + ")"

            ping_role_id = ping_roles.get(k)
            ping_role = ctx.guild.get_role(ping_role_id) if ping_role_id else None
            ping_str = f" — ping: {ping_role.mention}" if ping_role else ""

            source_lines.append(f"{state} **{k}** — {v['label']} {detail}{ping_str}")

        embed = discord.Embed(title="Hypixel Update Checker — Status", color=0x55AAFF)
        embed.add_field(name="Channel", value=channel_str, inline=False)
        embed.add_field(name="Check interval", value=f"Every {interval // 60} minutes", inline=True)
        embed.add_field(
            name="Post previews", value="✅ Yes" if do_previews else "❌ No", inline=True
        )
        embed.add_field(name="Sources", value="\n".join(source_lines), inline=False)
        await ctx.send(embed=embed)

    @hypixel.command(name="togglesource")
    @commands.admin_or_permissions(manage_guild=True)
    async def toggle_source(self, ctx: commands.Context, source: str):
        """Enable or disable a specific update source.

        Valid sources: `patch_notes`, `news`, `alpha`

        **Example:** `[p]hypixel togglesource alpha`
        """
        source = source.lower()
        if source not in SOURCES:
            valid = ", ".join(f"`{k}`" for k in SOURCES)
            await ctx.send(f"❌ Unknown source. Valid options: {valid}")
            return

        enabled = await self.config.guild(ctx.guild).enabled_sources()
        enabled[source] = not enabled.get(source, True)
        await self.config.guild(ctx.guild).enabled_sources.set(enabled)
        state = "enabled" if enabled[source] else "disabled"
        await ctx.send(f"✅ Source `{source}` is now **{state}**.")

    @hypixel.command(name="togglepreview")
    @commands.admin_or_permissions(manage_guild=True)
    async def toggle_preview(self, ctx: commands.Context):
        """Toggle whether a text preview is shown in update embeds."""
        conf = self.config.guild(ctx.guild)
        current = await conf.post_previews()
        await conf.post_previews.set(not current)
        state = "enabled" if not current else "disabled"
        await ctx.send(f"✅ Post previews are now **{state}**.")

    @hypixel.command(name="check")
    @commands.admin_or_permissions(manage_guild=True)
    async def manual_check(self, ctx: commands.Context):
        """Manually trigger an update check right now."""
        async with ctx.typing():
            await self._check_all_guilds()
        await ctx.send("✅ Check complete.")

    @hypixel.command(name="setinterval")
    @commands.admin_or_permissions(manage_guild=True)
    async def set_interval(self, ctx: commands.Context, minutes: int):
        """Set how often the bot checks for new posts (minimum 5 minutes).

        Please keep this reasonable — 30 minutes is the default and is
        already more than frequent enough for Hypixel patch notes.

        **Example:** `[p]hypixel setinterval 30`
        """
        if minutes < 5:
            await ctx.send("❌ Minimum interval is 5 minutes. Please be respectful to Hypixel's servers.")
            return
        if minutes > 1440:
            await ctx.send("❌ Maximum interval is 1440 minutes (24 hours).")
            return
        seconds = minutes * 60
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(f"✅ Will check for updates every **{minutes} minutes**.")

    @hypixel.command(name="setpingrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def set_ping_role(
        self, ctx: commands.Context, source: str, role: discord.Role
    ):
        """Set a role to ping when a source posts a new update.

        Valid sources: `patch_notes`, `news`, `alpha`

        **Examples:**
        `[p]hypixel setpingrole alpha @SkyBlock Alpha Tester`
        `[p]hypixel setpingrole patch_notes @SkyBlock Updates`
        """
        source = source.lower()
        if source not in SOURCES:
            valid = ", ".join(f"`{k}`" for k in SOURCES)
            await ctx.send(f"❌ Unknown source. Valid options: {valid}")
            return

        ping_roles = await self.config.guild(ctx.guild).ping_roles()
        ping_roles[source] = role.id
        await self.config.guild(ctx.guild).ping_roles.set(ping_roles)
        await ctx.send(
            f"✅ {role.mention} will be pinged for **{SOURCES[source]['label']}** updates.",
            allowed_mentions=discord.AllowedMentions(roles=False),
        )

    @hypixel.command(name="clearpingrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def clear_ping_role(self, ctx: commands.Context, source: str):
        """Remove the ping role for a source.

        Valid sources: `patch_notes`, `news`, `alpha`

        **Example:** `[p]hypixel clearpingrole alpha`
        """
        source = source.lower()
        if source not in SOURCES:
            valid = ", ".join(f"`{k}`" for k in SOURCES)
            await ctx.send(f"❌ Unknown source. Valid options: {valid}")
            return

        ping_roles = await self.config.guild(ctx.guild).ping_roles()
        ping_roles[source] = None
        await self.config.guild(ctx.guild).ping_roles.set(ping_roles)
        await ctx.send(f"✅ Ping role cleared for **{SOURCES[source]['label']}**.")

    @hypixel.command(name="resetseen")
    @commands.admin_or_permissions(manage_guild=True)
    async def reset_seen(self, ctx: commands.Context, source: Optional[str] = None):
        """Reset the seen-threads list (⚠️ will re-announce old posts!).

        Optionally pass a source name to reset only that one.

        **Example:** `[p]hypixel resetseen patch_notes`
        """
        conf = self.config.guild(ctx.guild)
        if source:
            source = source.lower()
            if source not in SOURCES:
                valid = ", ".join(f"`{k}`" for k in SOURCES)
                await ctx.send(f"❌ Unknown source. Valid options: {valid}")
                return
            await conf.set_raw("seen_threads", source, value={})
            await ctx.send(f"✅ Reset seen threads for `{source}`.")
        else:
            await conf.seen_threads.set({k: {} for k in SOURCES})
            await ctx.send("✅ Reset seen threads for all sources.")