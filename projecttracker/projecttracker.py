import asyncio
import aiohttp
import discord
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from redbot.core import commands, Config, checks, app_commands
from redbot.core.utils.chat_formatting import pagify
import logging

log = logging.getLogger("red.projecttracker")


class ProjectTracker(commands.Cog):
    """Track Modrinth project updates and post them to Discord channels."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)

        # Default settings
        default_global = {
            "check_interval": 900,  # 15 minutes in seconds
            "api_rate_limit": 280,  # Max API calls per minute
        }

        default_guild = {
            "tracked_projects": {},  # project_id -> list of track configs
            "custom_messages": {},  # project_id -> custom message config
        }

        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)

        # Runtime data
        self.update_task = None
        self.last_api_calls = []
        self.session = None

    async def cog_load(self):
        """Initialize the cog."""
        self.session = aiohttp.ClientSession()
        self.update_task = asyncio.create_task(self.update_checker_loop())

    async def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.update_task:
            self.update_task.cancel()
        if self.session:
            await self.session.close()

    async def update_checker_loop(self):
        """Main loop for checking project updates."""
        while True:
            try:
                interval = await self.config.check_interval()
                await asyncio.sleep(interval)
                await self.check_all_projects()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in update checker loop: {e}")
                await asyncio.sleep(60)

    async def rate_limit_check(self):
        """Check if we can make an API call without hitting rate limits."""
        now = datetime.now()
        rate_limit = await self.config.api_rate_limit()

        cutoff = now - timedelta(minutes=1)
        self.last_api_calls = [call_time for call_time in self.last_api_calls if call_time > cutoff]

        if len(self.last_api_calls) >= rate_limit:
            return False

        self.last_api_calls.append(now)
        return True

    async def make_api_request(self, url: str) -> Optional[Dict[str, Any]]:
        """Make a rate-limited API request to Modrinth."""
        if not await self.rate_limit_check():
            log.warning("Rate limit reached, skipping API call")
            return None

        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    log.error(f"API request failed with status {response.status}")
                    return None
        except Exception as e:
            log.error(f"API request error: {e}")
            return None

    async def get_project_info(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Get project information from Modrinth API."""
        url = f"https://api.modrinth.com/v2/project/{project_id}"
        return await self.make_api_request(url)

    async def get_project_versions(self, project_id: str, mc_version: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Get project versions from Modrinth API."""
        url = f"https://api.modrinth.com/v2/project/{project_id}/version"
        if mc_version:
            url += f"?game_versions=[%22{mc_version}%22]"
        return await self.make_api_request(url)

    async def get_latest_version(self, project_id: str, mc_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get the latest version of a project."""
        versions = await self.get_project_versions(project_id, mc_version)
        if versions and len(versions) > 0:
            return versions[0]
        return None

    def format_update_message(self, project_id: str, project_info: Dict[str, Any], version_info: Dict[str, Any],
                              custom_config: Dict[str, Any], mc_version: Optional[str] = None) -> str:
        project_name = project_info.get("title", "Unknown Project")
        version_number = version_info.get("version_number", "Unknown Version")
        date_published = version_info.get("date_published", "")
        changelog = version_info.get("changelog", "No changelog provided.")
        version_id = version_info.get("id", "")

        game_versions = version_info.get("game_versions", [])
        mc_version_display = f" for MC {mc_version}" if mc_version else ""
        if not mc_version and game_versions:
            mc_version_display = f" for MC {', '.join(game_versions)}"

        formatted_date = ""
        if date_published:
            try:
                dt = datetime.fromisoformat(date_published.replace('Z', '+00:00'))
                formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except:
                formatted_date = date_published

        message_parts = []

        if custom_config.get("start_message"):
            message_parts.append(custom_config["start_message"])
        else:
            message_parts.append(
                f"🎉 A new update for **{project_name}** has been released{mc_version_display}! Find it with the link below.")

        message_parts.append(f"# 🔄 {project_name} - {version_number}")
        if formatted_date:
            message_parts.append(f"**Published:** {formatted_date}")
        if mc_version_display:
            message_parts.append(f"**Minecraft Version:** {mc_version_display[5:]}")
        if version_id:
            message_parts.append(
                f"**Version Page:** https://modrinth.com/mod/{project_info.get('slug', project_id)}/version/{version_id}")

        if changelog and changelog.strip():
            message_parts.append("\n## Changelog")
            message_parts.append(changelog)

        if custom_config.get("end_message"):
            message_parts.append(custom_config["end_message"])

        return "\n\n".join(message_parts)

    async def check_project_updates(self, guild_id: int, project_id: str, track_configs: List[Dict[str, Any]]):
        try:
            project_info = await self.get_project_info(project_id)
            if not project_info:
                log.error(f"Failed to get project info for {project_id}")
                return

            for config in track_configs:
                mc_versions = config.get("mc_versions")
                last_version_ids = config.get("last_version_ids", {})

                if mc_versions is None:
                    versions_to_check = [None]
                elif isinstance(mc_versions, str):
                    versions_to_check = [mc_versions]
                    config["mc_versions"] = [mc_versions]
                    if "last_version_id" in config:
                        config["last_version_ids"] = {mc_versions: config["last_version_id"]}
                        del config["last_version_id"]
                else:
                    versions_to_check = mc_versions

                config_updated = False

                for mc_version in versions_to_check:
                    latest_version = await self.get_latest_version(project_id, mc_version)
                    if not latest_version:
                        continue

                    version_key = mc_version if mc_version else "all"
                    last_version_id = last_version_ids.get(version_key)
                    current_version_id = latest_version.get("id")

                    log.debug(
                        f"Checking {project_id} MC:{mc_version} - Last: {last_version_id}, Current: {current_version_id}")

                    if last_version_id != current_version_id:
                        log.info(f"New version found for {project_id} MC:{mc_version}: {current_version_id}")

                        await self.send_update_message(guild_id, project_id, project_info, latest_version, config, mc_version)

                        last_version_ids[version_key] = current_version_id
                        config_updated = True

                if config_updated:
                    config["last_version_ids"] = last_version_ids

        except Exception as e:
            log.error(f"Error checking updates for project {project_id}: {e}")

    async def send_update_message(self, guild_id: int, project_id: str, project_info: Dict[str, Any],
                                  version_info: Dict[str, Any], config: Dict[str, Any],
                                  mc_version: Optional[str] = None):
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            channel = guild.get_channel(config["channel_id"])
            if not channel:
                return

            custom_messages = await self.config.guild(guild).custom_messages()
            custom_config = custom_messages.get(project_id, {})

            message = self.format_update_message(project_id, project_info, version_info, custom_config, mc_version)

            pages = list(pagify(message, delims=["\n\n", "\n"], page_length=2000))

            for i, page in enumerate(pages):
                embed = discord.Embed(description=page, color=discord.Color.green())
                content = None
                if i == 0 and config.get("ping_role_id"):
                    role = guild.get_role(config["ping_role_id"])
                    if role:
                        content = role.mention
                await channel.send(content=content, embed=embed)

        except Exception as e:
            log.error(f"Error sending update message: {e}")

    async def check_all_projects(self):
        for guild in self.bot.guilds:
            guild_config = self.config.guild(guild)
            tracked_projects = await guild_config.tracked_projects()
            for project_id, track_configs in tracked_projects.items():
                await self.check_project_updates(guild.id, project_id, track_configs)
            await guild_config.tracked_projects.set(tracked_projects)

    # ---- HYBRID COMMANDS ----

    @commands.hybrid_group(name="track", invoke_without_command=True, description="Project tracking commands.")
    async def track(self, ctx):
        await ctx.send_help(ctx.command)

    @track.hybrid_command(name="add", description="Track a Modrinth project for updates.")
    @app_commands.describe(
        project_id="The Modrinth project ID or slug",
        channel="The channel to post updates to",
        role="Optional role to ping on updates",
        mc_versions="One or more Minecraft versions to filter (e.g., 1.21.4 1.21.5)",
    )
    async def track_add(self, ctx: commands.Context, project_id: str, channel: discord.TextChannel,
                        role: Optional[discord.Role] = None, *mc_versions: str):
        # Validate project exists
        project_info = await self.get_project_info(project_id)
        if not project_info:
            await ctx.send(f"❌ Could not find project with ID: {project_id}")
            return

        mc_versions_list = list(mc_versions) if mc_versions else None

        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        if project_id in tracked_projects:
            for config in tracked_projects[project_id]:
                if (config["channel_id"] == channel.id and
                        config.get("mc_versions") == mc_versions_list):
                    versions_str = ", ".join(mc_versions_list) if mc_versions_list else "any"
                    await ctx.send(
                        f"❌ Project {project_info['title']} is already tracked in {channel.mention} for MC versions: {versions_str}")
                    return

        last_version_ids = {}
        if mc_versions_list:
            for mc_version in mc_versions_list:
                latest_version = await self.get_latest_version(project_id, mc_version)
                if latest_version:
                    last_version_ids[mc_version] = latest_version.get("id")
                else:
                    await ctx.send(f"⚠️ Warning: Could not find any versions for MC {mc_version}")
        else:
            latest_version = await self.get_latest_version(project_id, None)
            if latest_version:
                last_version_ids["all"] = latest_version.get("id")
            else:
                await ctx.send(f"❌ Could not find any versions for project {project_info['title']}")
                return

        track_config = {
            "channel_id": channel.id,
            "ping_role_id": role.id if role else None,
            "mc_versions": mc_versions_list,
            "last_version_ids": last_version_ids,
            "added_by": ctx.author.id,
            "added_at": datetime.now().isoformat()
        }

        if project_id not in tracked_projects:
            tracked_projects[project_id] = []
        tracked_projects[project_id].append(track_config)

        await guild_config.tracked_projects.set(tracked_projects)

        if mc_versions_list:
            versions_str = ", ".join(mc_versions_list)
            version_info = f" (MC versions: {versions_str})"
        else:
            version_info = " (all MC versions)"
        role_info = f" pinging {role.mention}" if role else ""
        await ctx.send(
            f"✅ Now tracking **{project_info['title']}** ({project_id}){version_info} in {channel.mention}{role_info}")

    @track.hybrid_command(name="remove", description="Stop tracking a project or in a specific channel.")
    @app_commands.describe(
        project_id="The Modrinth project ID or slug",
        channel="Channel to stop tracking in (optional)",
    )
    async def track_remove(self, ctx: commands.Context, project_id: str, channel: Optional[discord.TextChannel] = None):
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        if project_id not in tracked_projects:
            await ctx.send(f"❌ Project {project_id} is not being tracked")
            return

        if channel:
            original_count = len(tracked_projects[project_id])
            tracked_projects[project_id] = [
                config for config in tracked_projects[project_id]
                if config["channel_id"] != channel.id
            ]

            if len(tracked_projects[project_id]) == original_count:
                await ctx.send(f"❌ Project {project_id} is not being tracked in {channel.mention}")
                return

            if not tracked_projects[project_id]:
                del tracked_projects[project_id]

            await ctx.send(f"✅ Stopped tracking {project_id} in {channel.mention}")
        else:
            del tracked_projects[project_id]
            await ctx.send(f"✅ Stopped tracking {project_id} in all channels")

        await guild_config.tracked_projects.set(tracked_projects)

    @track.hybrid_command(name="list", description="List all tracked projects.")
    async def track_list(self, ctx: commands.Context):
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        if not tracked_projects:
            await ctx.send("No projects are currently being tracked.")
            return

        embed = discord.Embed(title="📋 Tracked Projects", color=discord.Color.blue())

        for project_id, configs in tracked_projects.items():
            project_info = await self.get_project_info(project_id)
            project_name = project_info.get("title", project_id) if project_info else project_id

            config_lines = []
            for config in configs:
                channel = ctx.guild.get_channel(config["channel_id"])
                channel_name = channel.mention if channel else "Unknown Channel"

                role_info = ""
                if config.get("ping_role_id"):
                    role = ctx.guild.get_role(config["ping_role_id"])
                    if role:
                        role_info = f" (pings {role.mention})"

                mc_versions = config.get("mc_versions")
                if mc_versions:
                    if isinstance(mc_versions, list):
                        mc_info = f" [MC {', '.join(mc_versions)}]"
                    else:
                        mc_info = f" [MC {mc_versions}]"
                elif config.get("mc_version"):
                    mc_info = f" [MC {config['mc_version']}]"
                else:
                    mc_info = ""

                config_lines.append(f"• {channel_name}{role_info}{mc_info}")

            embed.add_field(
                name=f"📦 {project_name} ({project_id})",
                value="\n".join(config_lines),
                inline=False
            )

        await ctx.send(embed=embed)

    @track.hybrid_command(name="check", description="Force check for updates on all tracked projects.")
    @checks.admin_or_permissions(manage_guild=True)
    async def force_check(self, ctx: commands.Context):
        await ctx.send("🔄 Checking for updates...")
        await self.check_all_projects()
        await ctx.send("✅ Update check completed!")

    @track.hybrid_command(name="latest", description="Show the latest version info for tracked projects.")
    @app_commands.describe(
        project_id="Project ID to show (optional, shows all if omitted)"
    )
    async def show_latest(self, ctx: commands.Context, project_id: Optional[str] = None):
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        if not tracked_projects:
            await ctx.send("No projects are currently being tracked.")
            return

        projects_to_check = {}
        if project_id:
            if project_id in tracked_projects:
                projects_to_check[project_id] = tracked_projects[project_id]
            else:
                await ctx.send(f"❌ Project {project_id} is not being tracked")
                return
        else:
            projects_to_check = tracked_projects

        for proj_id, configs in projects_to_check.items():
            project_info = await self.get_project_info(proj_id)
            if not project_info:
                continue

            for config in configs:
                channel = ctx.guild.get_channel(config["channel_id"])
                if not channel:
                    continue

                mc_versions = config.get("mc_versions")
                if mc_versions:
                    if isinstance(mc_versions, list):
                        versions_to_check = mc_versions
                    else:
                        versions_to_check = [mc_versions]
                elif config.get("mc_version"):
                    versions_to_check = [config["mc_version"]]
                else:
                    versions_to_check = [None]

                for mc_version in versions_to_check:
                    latest_version = await self.get_latest_version(proj_id, mc_version)
                    if not latest_version:
                        continue

                    custom_messages = await guild_config.custom_messages()
                    custom_config = custom_messages.get(proj_id, {})

                    await self.send_update_message(ctx.guild.id, proj_id, project_info, latest_version, config, mc_version)

        await ctx.send("✅ Latest version info sent!")

    # ---- TRACKCONFIG HYBRID GROUP ----

    @commands.hybrid_group(name="trackconfig", invoke_without_command=True, description="Configuration commands for project tracking.")
    @checks.admin_or_permissions(manage_guild=True)
    async def trackconfig(self, ctx):
        await ctx.send_help(ctx.command)

    @trackconfig.hybrid_command(name="interval", description="Set the update check interval in seconds (minimum 60).")
    @checks.admin_or_permissions(manage_guild=True)
    async def set_interval(self, ctx: commands.Context, seconds: int):
        if seconds < 60:
            await ctx.send("❌ Interval must be at least 60 seconds")
            return

        await self.config.check_interval.set(seconds)
        await ctx.send(f"✅ Update check interval set to {seconds} seconds")

        if self.update_task:
            self.update_task.cancel()
        self.update_task = asyncio.create_task(self.update_checker_loop())

    @trackconfig.hybrid_command(name="ratelimit", description="Set the API rate limit (calls per minute).")
    @checks.admin_or_permissions(manage_guild=True)
    async def set_rate_limit(self, ctx: commands.Context, calls_per_minute: int):
        if calls_per_minute < 1:
            await ctx.send("❌ Rate limit must be at least 1 call per minute")
            return

        await self.config.api_rate_limit.set(calls_per_minute)
        await ctx.send(f"✅ API rate limit set to {calls_per_minute} calls per minute")

    # ---- TRACKMSG HYBRID GROUP ----

    @commands.hybrid_group(name="trackmsg", invoke_without_command=True, description="Customize tracking messages.")
    async def trackmsg(self, ctx):
        await ctx.send_help(ctx.command)

    @trackmsg.hybrid_command(name="start", description="Set a custom start message for a project.")
    async def set_start_message(self, ctx: commands.Context, project_id: str, *, message: str):
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        if project_id not in tracked_projects:
            await ctx.send(f"❌ Project {project_id} is not being tracked")
            return

        custom_messages = await guild_config.custom_messages()
        if project_id not in custom_messages:
            custom_messages[project_id] = {}

        custom_messages[project_id]["start_message"] = message
        await guild_config.custom_messages.set(custom_messages)

        await ctx.send(f"✅ Custom start message set for {project_id}")

    @trackmsg.hybrid_command(name="end", description="Set a custom end message for a project.")
    async def set_end_message(self, ctx: commands.Context, project_id: str, *, message: str):
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        if project_id not in tracked_projects:
            await ctx.send(f"❌ Project {project_id} is not being tracked")
            return

        custom_messages = await guild_config.custom_messages()
        if project_id not in custom_messages:
            custom_messages[project_id] = {}

        custom_messages[project_id]["end_message"] = message
        await guild_config.custom_messages.set(custom_messages)

        await ctx.send(f"✅ Custom end message set for {project_id}")

    @trackmsg.hybrid_command(name="clear", description="Clear custom messages for a project.")
    async def clear_custom_messages(self, ctx: commands.Context, project_id: str):
        guild_config = self.config.guild(ctx.guild)
        custom_messages = await guild_config.custom_messages()

        if project_id in custom_messages:
            del custom_messages[project_id]
            await guild_config.custom_messages.set(custom_messages)
            await ctx.send(f"✅ Custom messages cleared for {project_id}")
        else:
            await ctx.send(f"❌ No custom messages found for {project_id}")
