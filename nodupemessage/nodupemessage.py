import asyncio
import hashlib
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import humanize_timedelta

# Short/common replies that should never be flagged as duplicates
IGNORED_CONTENT = {
    "yes", "no", "ok", "okay", "k", "yep", "yup", "nope", "nah",
    "sure", "fine", "alright", "agreed", "same", "same here",
    "thanks", "thank you", "ty", "thx", "np", "no problem", "yw",
    "lol", "lmao", "haha", "hehe", "gg", "nice", "cool", "wow",
    "omg", "good", "bad", "true", "false", "indeed", "exactly",
    "👍", "👎", "❤️", "😂", "🙏", "+1", "-1",
}
# Also ignore anything 3 characters or shorter
MIN_LENGTH = 4


class NoDupeMessage(commands.Cog):
    """Detects and removes duplicate messages sent across multiple channels."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=198273645, force_registration=True)

        self.config.register_guild(
            time_window=60,       # seconds within which a repeat counts as a duplicate
            mute_threshold=3,     # violations before a timeout is issued
            mute_duration=300,    # timeout duration in seconds
            enabled=True,
            exempt_roles=[],      # role IDs exempt from the filter
        )

        # {guild_id: {user_id: deque[(content_hash, monotonic_time, channel_id)]}}
        self._cache: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(deque))

        # {guild_id: {user_id: [violation_count, last_violation_monotonic]}}
        self._violations: dict[int, dict[int, list]] = defaultdict(dict)

        self._cleanup_task = self.bot.loop.create_task(self._cleanup_loop())

    def cog_unload(self):
        self._cleanup_task.cancel()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _cleanup_loop(self):
        """Background task: prune stale cache entries every 60 s."""
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(60)
            cutoff = time.monotonic() - 600  # keep at most 10 min of history
            for guild_data in self._cache.values():
                for dq in guild_data.values():
                    while dq and dq[0][1] < cutoff:
                        dq.popleft()
            # Expire violation records older than 10 minutes
            for guild_data in self._violations.values():
                stale = [uid for uid, v in guild_data.items() if time.monotonic() - v[1] > 600]
                for uid in stale:
                    del guild_data[uid]

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.md5(content.lower().strip().encode()).hexdigest()

    @staticmethod
    def _is_ignored(content: str) -> bool:
        stripped = content.lower().strip()
        return len(stripped) <= MIN_LENGTH or stripped in IGNORED_CONTENT

    async def _temp_message(self, channel: discord.TextChannel, content: str, delay: int = 30):
        """Send a message that auto-deletes after `delay` seconds."""
        try:
            msg = await channel.send(content)
            await asyncio.sleep(delay)
            await msg.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Core listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if self._is_ignored(message.content):
            return

        settings = await self.config.guild(message.guild).all()
        if not settings["enabled"]:
            return

        # Skip users who have an exempt role
        if settings["exempt_roles"]:
            member_role_ids = {r.id for r in message.author.roles}
            if member_role_ids & set(settings["exempt_roles"]):
                return

        now = time.monotonic()
        window = settings["time_window"]
        content_hash = self._hash(message.content)
        user_cache = self._cache[message.guild.id][message.author.id]

        # Prune entries outside the time window
        while user_cache and now - user_cache[0][1] > window:
            user_cache.popleft()

        # Check if this exact message was already sent in a *different* channel
        is_duplicate = any(
            h == content_hash and ch != message.channel.id
            for h, _, ch in user_cache
        )

        # Record this message regardless
        user_cache.append((content_hash, now, message.channel.id))

        if not is_duplicate:
            return

        # ---- Handle duplicate ----
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        # Update violation record
        record = self._violations[message.guild.id].get(message.author.id, [0, now])
        record[0] += 1
        record[1] = now
        self._violations[message.guild.id][message.author.id] = record
        violations = record[0]

        threshold = settings["mute_threshold"]
        mute_dur = settings["mute_duration"]

        if violations >= threshold:
            # Reset counter and apply timeout
            record[0] = 0
            await self._apply_timeout(message.author, message.guild, mute_dur, message.channel)
        else:
            remaining = threshold - violations
            await self._temp_message(
                message.channel,
                f"{message.author.mention} Please don't post the same message in multiple channels — "
                f"your message was removed. "
                f"({remaining} more strike(s) before a temporary mute)",
            )

    async def _apply_timeout(
        self,
        member: discord.Member,
        guild: discord.Guild,
        duration: int,
        channel: discord.TextChannel,
    ):
        until = discord.utils.utcnow() + timedelta(seconds=duration)
        try:
            await member.timeout(until, reason="Repeatedly posting duplicate messages across channels.")
            dur_str = humanize_timedelta(seconds=duration)
            await self._temp_message(
                channel,
                f"{member.mention} has been muted for **{dur_str}** for repeatedly "
                f"posting the same message across multiple channels.",
                delay=15,
            )
        except discord.Forbidden:
            await self._temp_message(
                channel,
                f"{member.mention} Please stop posting the same message in multiple channels. "
                f"(Could not apply mute — missing Moderate Members permission.)",
            )

    # ------------------------------------------------------------------
    # Admin commands  [p]nodupe ...
    # ------------------------------------------------------------------

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def nodupe(self, ctx: commands.Context):
        """Manage the duplicate-message filter."""

    @nodupe.command(name="enable")
    async def nodupe_enable(self, ctx):
        """Enable the duplicate-message filter."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("✅ Duplicate message filter **enabled**.")

    @nodupe.command(name="disable")
    async def nodupe_disable(self, ctx):
        """Disable the duplicate message filter."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("⛔ Duplicate message filter **disabled**.")

    @nodupe.command(name="window")
    async def nodupe_window(self, ctx, seconds: int):
        """Set the time window (10–600 s) for detecting cross-channel duplicates."""
        if not 10 <= seconds <= 600:
            return await ctx.send("Time window must be between **10** and **600** seconds.")
        await self.config.guild(ctx.guild).time_window.set(seconds)
        await ctx.send(f"⏱ Time window set to **{seconds}s**.")

    @nodupe.command(name="threshold")
    async def nodupe_threshold(self, ctx, count: int):
        """Set how many violations trigger a mute (1–10)."""
        if not 1 <= count <= 10:
            return await ctx.send("Threshold must be between **1** and **10**.")
        await self.config.guild(ctx.guild).mute_threshold.set(count)
        await ctx.send(f"⚠️ Mute threshold set to **{count}** violation(s).")

    @nodupe.command(name="muteduration")
    async def nodupe_muteduration(self, ctx, seconds: int):
        """Set the timeout duration in seconds (30 s – 24 h)."""
        if not 30 <= seconds <= 86400:
            return await ctx.send("Duration must be between **30 seconds** and **86400 seconds** (24 h).")
        await self.config.guild(ctx.guild).mute_duration.set(seconds)
        await ctx.send(f"🔇 Mute duration set to **{humanize_timedelta(seconds=seconds)}**.")

    @nodupe.command(name="exemptadd")
    async def nodupe_exemptadd(self, ctx, role: discord.Role):
        """Add a role to the exempt list (members with this role are ignored)."""
        async with self.config.guild(ctx.guild).exempt_roles() as exempt:
            if role.id in exempt:
                return await ctx.send(f"**{role.name}** is already exempt.")
            exempt.append(role.id)
        await ctx.send(f"✅ **{role.name}** added to exempt roles.")

    @nodupe.command(name="exemptremove")
    async def nodupe_exemptremove(self, ctx, role: discord.Role):
        """Remove a role from the exempt list."""
        async with self.config.guild(ctx.guild).exempt_roles() as exempt:
            if role.id not in exempt:
                return await ctx.send(f"**{role.name}** is not in the exempt list.")
            exempt.remove(role.id)
        await ctx.send(f"🗑 **{role.name}** removed from exempt roles.")

    @nodupe.command(name="exemptlist")
    async def nodupe_exemptlist(self, ctx):
        """List all exempt roles."""
        exempt_ids = await self.config.guild(ctx.guild).exempt_roles()
        if not exempt_ids:
            return await ctx.send("No roles are currently exempt.")
        roles = [ctx.guild.get_role(rid) for rid in exempt_ids]
        role_names = [r.mention if r else f"(deleted role {rid})" for r, rid in zip(roles, exempt_ids)]
        await ctx.send("**Exempt roles:** " + ", ".join(role_names))

    @nodupe.command(name="settings")
    async def nodupe_settings(self, ctx):
        """Show the current configuration."""
        s = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="NoDupe Settings", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value=str(s["enabled"]), inline=True)
        embed.add_field(name="Time Window", value=f"{s['time_window']}s", inline=True)
        embed.add_field(name="Mute Threshold", value=f"{s['mute_threshold']} violation(s)", inline=True)
        embed.add_field(
            name="Mute Duration",
            value=humanize_timedelta(seconds=s["mute_duration"]),
            inline=True,
        )
        if s["exempt_roles"]:
            roles = [ctx.guild.get_role(rid) for rid in s["exempt_roles"]]
            role_names = [r.name if r else f"(deleted {rid})" for r, rid in zip(roles, s["exempt_roles"])]
            embed.add_field(name="Exempt Roles", value=", ".join(role_names), inline=False)
        else:
            embed.add_field(name="Exempt Roles", value="None", inline=False)
        await ctx.send(embed=embed)