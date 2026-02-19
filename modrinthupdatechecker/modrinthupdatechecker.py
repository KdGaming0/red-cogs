import asyncio
import aiohttp
from datetime import datetime
from typing import Optional

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

MODRINTH_API = "https://api.modrinth.com/v2"
USER_AGENT = "RedBot-ModrinthUpdateChecker/1.0.0 (github.com/KdGaming0/red-cogs)"
VERSION_URL = "https://modrinth.com/mod/{project_id}/version/{version_id}"

VALID_LOADERS = {"fabric", "forge", "quilt", "neoforge"}


class ModrinthUpdateChecker(commands.Cog):
    """Track Modrinth mods and get notified when they update."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4D6F6472696E7468, force_registration=True)

        # Global defaults
        self.config.register_global(
            check_interval=600,  # seconds (5 minutes)
            default_loader=None,  # e.g. "fabric"
        )

        # Guild-level defaults
        self.config.register_guild(
            tracked={},  # project_id -> { channel_id, roles, mc_versions, loader, last_version_id, project_name }
            default_loader=None,
        )

        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        self._task = self.bot.loop.create_task(self._update_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    # ─────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────

    async def _get_project(self, project_id: str) -> Optional[dict]:
        """Fetch project metadata from Modrinth."""
        try:
            async with self._session.get(f"{MODRINTH_API}/project/{project_id}") as resp:
                if resp.status == 200:
                    return await resp.json()
        except aiohttp.ClientError:
            pass
        return None

    async def _get_versions(
        self,
        project_id: str,
        loaders: Optional[list] = None,
        game_versions: Optional[list] = None,
    ) -> Optional[list]:
        """Fetch versions for a project, optionally filtered."""
        params = {"include_changelog": "true"}
        if loaders:
            params["loaders"] = f'["{",".join(loaders)}"]'
        if game_versions:
            params["game_versions"] = f'["{",".join(game_versions)}"]'
        try:
            async with self._session.get(
                f"{MODRINTH_API}/project/{project_id}/version", params=params
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except aiohttp.ClientError:
            pass
        return None

    def _build_update_embed(self, project: dict, version: dict) -> discord.Embed:
        """Build a rich embed for an update notification."""
        project_id = project["id"]
        project_slug = project.get("slug", project_id)
        version_id = version["id"]

        url = f"https://modrinth.com/mod/{project_slug}/version/{version_id}"

        embed = discord.Embed(
            title=f"🆕 {project['title']} — {version['version_number']}",
            url=url,
            color=0x1BD96A,  # Modrinth green
            description=None,
        )

        if project.get("icon_url"):
            embed.set_thumbnail(url=project["icon_url"])

        embed.add_field(name="Version Name", value=version.get("name", version["version_number"]), inline=True)
        embed.add_field(name="Release Type", value=version.get("version_type", "release").capitalize(), inline=True)

        loaders = ", ".join(version.get("loaders", [])) or "—"
        embed.add_field(name="Loaders", value=loaders, inline=True)

        mc_versions = version.get("game_versions", [])
        if mc_versions:
            # Show at most 10 versions to keep embed tidy
            shown = ", ".join(mc_versions[:10])
            if len(mc_versions) > 10:
                shown += f" (+{len(mc_versions) - 10} more)"
            embed.add_field(name="Minecraft Versions", value=shown, inline=False)

        changelog = version.get("changelog") or ""
        if changelog:
            # Discord embed field limit is 1024 chars
            if len(changelog) > 900:
                changelog = changelog[:900] + "…\n\n[View full changelog](" + url + ")"
            embed.add_field(name="Changelog", value=changelog, inline=False)

        published = version.get("date_published", "")
        if published:
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                embed.set_footer(text=f"Published {dt.strftime('%Y-%m-%d %H:%M UTC')}")
            except ValueError:
                pass

        return embed

    async def _post_update(self, guild: discord.Guild, entry: dict, project: dict, version: dict):
        """Post an update notification to the configured channel."""
        channel = guild.get_channel(entry["channel_id"])
        if channel is None:
            return

        embed = self._build_update_embed(project, version)

        # Build role mentions
        mentions = ""
        for role_id in entry.get("roles", []):
            role = guild.get_role(role_id)
            if role:
                mentions += f"{role.mention} "

        await channel.send(content=mentions.strip() or None, embed=embed)

    # ─────────────────────────────────────────────
    # Background task
    # ─────────────────────────────────────────────

    async def _update_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._check_all_guilds()
            except Exception as e:
                # Don't let a crash kill the loop
                print(f"[ModrinthUpdateChecker] Error in update loop: {e}")
            interval = await self.config.check_interval()
            await asyncio.sleep(interval)

    async def _check_all_guilds(self):
        all_guilds = await self.config.all_guilds()
        for guild_id, guild_data in all_guilds.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            tracked = guild_data.get("tracked", {})
            if not tracked:
                continue
            guild_default_loader = guild_data.get("default_loader")

            for project_id, entry in tracked.items():
                await self._check_project(guild, project_id, entry, guild_default_loader)
                await asyncio.sleep(1)  # small delay between requests to be polite

    async def _check_project(self, guild: discord.Guild, project_id: str, entry: dict, guild_default_loader: Optional[str]):
        loaders = None
        loader = entry.get("loader") or guild_default_loader
        if loader:
            loaders = [loader]

        mc_versions = entry.get("mc_versions") or None

        versions = await self._get_versions(project_id, loaders=loaders, game_versions=mc_versions)
        if not versions:
            return

        # Most recent listed release version
        latest = next(
            (v for v in versions if v.get("status") == "listed"),
            versions[0] if versions else None,
        )
        if latest is None:
            return

        latest_id = latest["id"]
        stored_id = entry.get("last_version_id")

        if stored_id == latest_id:
            return  # no update

        # There's a new version — fetch project info for the embed
        project = await self._get_project(project_id)
        if project is None:
            return

        # Save the new version ID before posting (avoid double-posting on error)
        async with self.config.guild(guild).tracked() as tracked:
            if project_id in tracked:
                tracked[project_id]["last_version_id"] = latest_id

        await self._post_update(guild, entry, project, latest)

    # ─────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────

    @commands.group(name="track", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def track(self, ctx: commands.Context):
        """Manage Modrinth mod tracking."""
        await self._send_help(ctx)

    @track.command(name="help")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_help(self, ctx: commands.Context):
        """Show all ModrinthUpdateChecker commands."""
        await self._send_help(ctx)

    async def _send_help(self, ctx: commands.Context):
        p = ctx.clean_prefix

        embed = discord.Embed(
            title="📦 Modrinth Update Checker — Help",
            description=(
                "Tracks mods on [Modrinth](https://modrinth.com) and posts a notification embed "
                "whenever a new version is released.\n\u200b"
            ),
            color=0x1BD96A,
        )

        embed.add_field(
            name="➕ Tracking",
            value=(
                f"`{p}track add <id> <#channel> [@role...] [--mc 1.21.4] [--loader fabric]`\n"
                f"Start tracking a mod. Project ID or slug from Modrinth.\n\n"
                f"`{p}track remove <id>`\n"
                f"Stop tracking a mod.\n\n"
                f"`{p}track list`\n"
                f"Show all tracked mods and their settings.\n\n"
                f"`{p}track check`\n"
                f"Manually trigger an update check right now."
            ),
            inline=False,
        )

        embed.add_field(
            name="⚙️ Per-Project Settings  (`track set …`)",
            value=(
                f"`{p}track set channel <id> <#channel>`\n"
                f"Move notifications to a different channel.\n\n"
                f"`{p}track set mc <id> [versions...]`\n"
                f"Set (or clear) the Minecraft version filter for one mod.\n\n"
                f"`{p}track set loader <id> [loader]`\n"
                f"Set (or clear) the loader filter for one mod (e.g. `fabric`).\n\n"
                f"`{p}track set roles <id> [@role...]`\n"
                f"Set (or clear) which roles get pinged for one mod."
            ),
            inline=False,
        )

        embed.add_field(
            name="📢 Bulk MC Version  (`track set mc-…`)",
            value=(
                f"`{p}track set mc-all [versions...]`\n"
                f"Set (or clear) the MC version filter for **every** tracked mod.\n\n"
                f"`{p}track set mc-channel <#channel> [versions...]`\n"
                f"Set (or clear) the MC version filter for all mods in a specific channel."
            ),
            inline=False,
        )

        embed.add_field(
            name="🌐 Server Defaults  (`track default …`)",
            value=(
                f"`{p}track default loader [loader]`\n"
                f"Set a server-wide default loader filter (e.g. `fabric`). "
                f"Per-project overrides take priority."
            ),
            inline=False,
        )

        embed.add_field(
            name="🔧 Bot Owner Only",
            value=(
                f"`{p}track interval <seconds>`\n"
                f"Change how often the bot polls for updates (default: 300s, minimum: 60s)."
            ),
            inline=False,
        )

        embed.add_field(
            name="💡 Tips",
            value=(
                "• You can use a Modrinth **slug** (e.g. `sodium`) or the full **project ID**.\n"
                "• Valid loaders: `fabric`, `forge`, `quilt`, `neoforge`, `liteloader`.\n"
                "• Omit `--mc` or `--loader` to track updates for all versions/loaders.\n"
                "• All commands require **Admin** or **Manage Server** permission."
            ),
            inline=False,
        )

        embed.set_footer(text="Modrinth Update Checker • Data from modrinth.com")
        await ctx.send(embed=embed)

    # ── track add ──────────────────────────────

    @track.command(name="add")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_add(self, ctx: commands.Context, project_id: str, channel: discord.TextChannel, *args):
        """Add a mod to track for updates.

        **Usage:**
        `[p]track add <project_id> <channel> [role1] [role2] ... [--mc 1.21.4 1.21.5] [--loader fabric]`

        **Examples:**
        `[p]track add sodium #updates`
        `[p]track add sodium #updates @Modded --mc 1.21.4`
        `[p]track add sodium #updates @Modded --loader fabric --mc 1.21.4 1.21.5`
        """
        # Parse args: roles, --mc versions, --loader
        roles = []
        mc_versions = []
        loader = None

        i = 0
        args = list(args)
        while i < len(args):
            arg = args[i]
            if arg == "--mc":
                i += 1
                while i < len(args) and not args[i].startswith("--"):
                    mc_versions.append(args[i])
                    i += 1
                continue
            elif arg == "--loader":
                i += 1
                if i < len(args):
                    loader = args[i].lower()
                    if loader not in VALID_LOADERS:
                        await ctx.send(f"❌ `{loader}` is not a recognised loader. Valid loaders: {', '.join(sorted(VALID_LOADERS))}")
                        return
                    i += 1
                continue
            else:
                # Try to resolve as a role
                try:
                    role = await commands.RoleConverter().convert(ctx, arg)
                    roles.append(role.id)
                except commands.BadArgument:
                    await ctx.send(f"⚠️ Could not resolve `{arg}` as a role — skipping.")
            i += 1

        async with ctx.typing():
            project = await self._get_project(project_id)
            if project is None:
                await ctx.send(f"❌ Could not find a Modrinth project with ID/slug `{project_id}`.")
                return

            # Get the current latest version to record as baseline
            guild_default_loader = await self.config.guild(ctx.guild).default_loader()
            effective_loader = loader or guild_default_loader
            versions = await self._get_versions(
                project["id"],
                loaders=[effective_loader] if effective_loader else None,
                game_versions=mc_versions or None,
            )

            latest_version_id = None
            if versions:
                latest = next((v for v in versions if v.get("status") == "listed"), versions[0])
                latest_version_id = latest["id"]

            entry = {
                "channel_id": channel.id,
                "roles": roles,
                "mc_versions": mc_versions,
                "loader": loader,
                "last_version_id": latest_version_id,
                "project_name": project.get("title", project_id),
            }

            async with self.config.guild(ctx.guild).tracked() as tracked:
                tracked[project["id"]] = entry

        # Confirmation embed
        embed = discord.Embed(
            title=f"✅ Now tracking: {project['title']}",
            url=f"https://modrinth.com/mod/{project.get('slug', project['id'])}",
            color=0x1BD96A,
        )
        if project.get("icon_url"):
            embed.set_thumbnail(url=project["icon_url"])
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Loader Filter", value=loader or guild_default_loader or "Any", inline=True)
        embed.add_field(name="MC Versions", value=", ".join(mc_versions) if mc_versions else "Any", inline=True)
        if roles:
            role_mentions = ", ".join(f"<@&{r}>" for r in roles)
            embed.add_field(name="Ping Roles", value=role_mentions, inline=False)
        embed.set_footer(text=f"Project ID: {project['id']} • Current version recorded")

        await ctx.send(f"**{project['title']}** was added and is now being tracked.", embed=embed)

    # ── track remove ───────────────────────────

    @track.command(name="remove", aliases=["delete", "rm"])
    @checks.admin_or_permissions(manage_guild=True)
    async def track_remove(self, ctx: commands.Context, project_id: str):
        """Stop tracking a mod.

        You can use either the project ID or slug.
        """
        async with self.config.guild(ctx.guild).tracked() as tracked:
            # Support slug lookup — find by stored project name or direct ID match
            match_key = None
            for key in tracked:
                if key == project_id or tracked[key].get("project_name", "").lower() == project_id.lower():
                    match_key = key
                    break
            if match_key is None:
                await ctx.send(f"❌ `{project_id}` is not being tracked.")
                return
            name = tracked[match_key].get("project_name", match_key)
            del tracked[match_key]

        await ctx.send(f"✅ Stopped tracking **{name}**.")

    # ── track list ─────────────────────────────

    @track.command(name="list")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_list(self, ctx: commands.Context):
        """List all mods currently being tracked in this server."""
        tracked = await self.config.guild(ctx.guild).tracked()
        if not tracked:
            await ctx.send("No mods are currently being tracked.")
            return

        embed = discord.Embed(title="Tracked Mods", color=0x1BD96A)
        for project_id, entry in tracked.items():
            channel = ctx.guild.get_channel(entry["channel_id"])
            channel_str = channel.mention if channel else f"<deleted channel {entry['channel_id']}>"
            loader = entry.get("loader") or "—"
            mc = ", ".join(entry.get("mc_versions") or []) or "Any"
            roles = ", ".join(f"<@&{r}>" for r in entry.get("roles", [])) or "None"

            value = (
                f"**Channel:** {channel_str}\n"
                f"**Loader:** {loader}\n"
                f"**MC Versions:** {mc}\n"
                f"**Ping Roles:** {roles}"
            )
            embed.add_field(
                name=f"{entry.get('project_name', project_id)} (`{project_id}`)",
                value=value,
                inline=False,
            )

        await ctx.send(embed=embed)

    # ── track set ──────────────────────────────

    @track.group(name="set", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def track_set(self, ctx: commands.Context):
        """Update settings for a tracked mod or server defaults."""
        await ctx.send_help(ctx.command)

    @track_set.command(name="channel")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_set_channel(self, ctx: commands.Context, project_id: str, channel: discord.TextChannel):
        """Change the notification channel for a tracked mod."""
        async with self.config.guild(ctx.guild).tracked() as tracked:
            if project_id not in tracked:
                await ctx.send(f"❌ `{project_id}` is not being tracked.")
                return
            tracked[project_id]["channel_id"] = channel.id
        await ctx.send(f"✅ Update notifications for `{project_id}` will now go to {channel.mention}.")

    @track_set.command(name="mc")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_set_mc(self, ctx: commands.Context, project_id: str, *versions: str):
        """Set or clear the Minecraft version filter for a tracked mod.

        Pass no versions to remove the filter (track all MC versions).

        **Examples:**
        `[p]track set mc sodium 1.21.4 1.21.5`
        `[p]track set mc sodium` — clears filter
        """
        async with self.config.guild(ctx.guild).tracked() as tracked:
            if project_id not in tracked:
                await ctx.send(f"❌ `{project_id}` is not being tracked.")
                return
            tracked[project_id]["mc_versions"] = list(versions)

        if versions:
            await ctx.send(f"✅ MC version filter for `{project_id}` set to: {', '.join(versions)}")
        else:
            await ctx.send(f"✅ MC version filter for `{project_id}` cleared (tracking all versions).")

    @track_set.command(name="loader")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_set_loader(self, ctx: commands.Context, project_id: str, loader: Optional[str] = None):
        """Set or clear the loader filter for a tracked mod.

        Pass no loader to clear the per-project filter (falls back to server default).

        **Examples:**
        `[p]track set loader sodium fabric`
        `[p]track set loader sodium` — clears the per-project override
        """
        if loader and loader.lower() not in VALID_LOADERS:
            await ctx.send(f"❌ `{loader}` is not a recognised loader. Valid: {', '.join(sorted(VALID_LOADERS))}")
            return
        async with self.config.guild(ctx.guild).tracked() as tracked:
            if project_id not in tracked:
                await ctx.send(f"❌ `{project_id}` is not being tracked.")
                return
            tracked[project_id]["loader"] = loader.lower() if loader else None

        if loader:
            await ctx.send(f"✅ Loader filter for `{project_id}` set to `{loader.lower()}`.")
        else:
            await ctx.send(f"✅ Loader filter for `{project_id}` cleared (will use server default or any).")

    @track_set.command(name="mc-all")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_set_mc_all(self, ctx: commands.Context, *versions: str):
        """Set the MC version filter for ALL tracked mods at once.

        Pass no versions to clear the filter on all mods.

        **Examples:**
        `[p]track set mc-all 1.21.4 1.21.5`
        `[p]track set mc-all` — clears filter on everything
        """
        async with self.config.guild(ctx.guild).tracked() as tracked:
            if not tracked:
                await ctx.send("No mods are currently being tracked.")
                return
            for project_id in tracked:
                tracked[project_id]["mc_versions"] = list(versions)
            count = len(tracked)

        if versions:
            await ctx.send(f"✅ MC version filter set to `{', '.join(versions)}` for all {count} tracked mod(s).")
        else:
            await ctx.send(f"✅ MC version filter cleared for all {count} tracked mod(s).")

    @track_set.command(name="mc-channel")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_set_mc_channel(self, ctx: commands.Context, channel: discord.TextChannel, *versions: str):
        """Set the MC version filter for all mods posting to a specific channel.

        Pass no versions to clear the filter on those mods.

        **Examples:**
        `[p]track set mc-channel #updates 1.21.4 1.21.5`
        `[p]track set mc-channel #updates` — clears filter for mods in that channel
        """
        async with self.config.guild(ctx.guild).tracked() as tracked:
            if not tracked:
                await ctx.send("No mods are currently being tracked.")
                return
            affected = [pid for pid, e in tracked.items() if e["channel_id"] == channel.id]
            if not affected:
                await ctx.send(f"No mods are posting to {channel.mention}.")
                return
            for pid in affected:
                tracked[pid]["mc_versions"] = list(versions)

        if versions:
            await ctx.send(f"✅ MC version filter set to `{', '.join(versions)}` for {len(affected)} mod(s) in {channel.mention}.")
        else:
            await ctx.send(f"✅ MC version filter cleared for {len(affected)} mod(s) in {channel.mention}.")

    @track_set.command(name="roles")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_set_roles(self, ctx: commands.Context, project_id: str, *roles: discord.Role):
        """Replace the ping roles for a tracked mod.

        Pass no roles to remove all pings.
        """
        async with self.config.guild(ctx.guild).tracked() as tracked:
            if project_id not in tracked:
                await ctx.send(f"❌ `{project_id}` is not being tracked.")
                return
            tracked[project_id]["roles"] = [r.id for r in roles]

        if roles:
            role_str = ", ".join(r.mention for r in roles)
            await ctx.send(f"✅ Ping roles for `{project_id}` updated to: {role_str}")
        else:
            await ctx.send(f"✅ Ping roles for `{project_id}` cleared.")

    # ── track default ──────────────────────────

    @track.group(name="default", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def track_default(self, ctx: commands.Context):
        """Manage server-wide default settings."""
        await ctx.send_help(ctx.command)

    @track_default.command(name="loader")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_default_loader(self, ctx: commands.Context, loader: Optional[str] = None):
        """Set or clear the server-wide default loader filter.

        Per-project loader overrides take precedence over this setting.
        Pass no loader to clear.
        """
        if loader and loader.lower() not in VALID_LOADERS:
            await ctx.send(f"❌ `{loader}` is not a recognised loader. Valid: {', '.join(sorted(VALID_LOADERS))}")
            return
        await self.config.guild(ctx.guild).default_loader.set(loader.lower() if loader else None)
        if loader:
            await ctx.send(f"✅ Server default loader set to `{loader.lower()}`.")
        else:
            await ctx.send("✅ Server default loader cleared.")

    # ── track interval ─────────────────────────

    @track.command(name="interval")
    @checks.is_owner()
    async def track_interval(self, ctx: commands.Context, seconds: int):
        """Set how often (in seconds) to check for updates. Bot owner only.

        Minimum: 60 seconds.
        """
        if seconds < 60:
            await ctx.send("❌ Interval must be at least 60 seconds.")
            return
        await self.config.check_interval.set(seconds)
        # Restart the loop with the new interval
        if self._task:
            self._task.cancel()
        self._task = self.bot.loop.create_task(self._update_loop())
        await ctx.send(f"✅ Check interval set to {seconds} seconds. Loop restarted.")

    # ── track check ────────────────────────────

    @track.command(name="check")
    @checks.admin_or_permissions(manage_guild=True)
    async def track_check(self, ctx: commands.Context):
        """Manually trigger an update check right now for this server."""
        async with ctx.typing():
            tracked = await self.config.guild(ctx.guild).tracked()
            if not tracked:
                await ctx.send("No mods are being tracked.")
                return
            guild_default_loader = await self.config.guild(ctx.guild).default_loader()
            for project_id, entry in tracked.items():
                await self._check_project(ctx.guild, project_id, entry, guild_default_loader)
                await asyncio.sleep(0.5)
        await ctx.send("✅ Manual check complete.")