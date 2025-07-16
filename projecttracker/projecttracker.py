import asyncio
import aiohttp
import discord
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from redbot.core import commands, Config, checks
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
                await asyncio.sleep(60)  # Wait 1 minute before retrying

    async def rate_limit_check(self):
        """Check if we can make an API call without hitting rate limits."""
        now = datetime.now()
        rate_limit = await self.config.api_rate_limit()

        # Clean old entries
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

    async def get_project_versions(self, project_id: str, mc_version: Optional[str] = None) -> Optional[
        List[Dict[str, Any]]]:
        """Get project versions from Modrinth API."""
        url = f"https://api.modrinth.com/v2/project/{project_id}/version"
        if mc_version:
            url += f"?game_versions=[%22{mc_version}%22]"
        return await self.make_api_request(url)

    async def get_latest_version(self, project_id: str, mc_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get the latest version of a project."""
        versions = await self.get_project_versions(project_id, mc_version)
        if versions and len(versions) > 0:
            return versions[0]  # Modrinth returns versions sorted by date_published desc
        return None

    def format_update_message(self, project_id: str, project_info: Dict[str, Any], version_info: Dict[str, Any],
                              custom_config: Dict[str, Any], mc_version: Optional[str] = None) -> str:
        """Format the update message for Discord."""
        project_name = project_info.get("title", "Unknown Project")
        version_number = version_info.get("version_number", "Unknown Version")
        date_published = version_info.get("date_published", "")
        changelog = version_info.get("changelog", "No changelog provided.")
        version_id = version_info.get("id", "")

        # Get MC version info from version data
        game_versions = version_info.get("game_versions", [])
        mc_version_display = f" for MC {mc_version}" if mc_version else ""
        if not mc_version and game_versions:
            mc_version_display = f" for MC {', '.join(game_versions)}"

        # Format date
        formatted_date = ""
        if date_published:
            try:
                dt = datetime.fromisoformat(date_published.replace('Z', '+00:00'))
                formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except:
                formatted_date = date_published

        # Build message
        message_parts = []

        # Custom start message or default
        if custom_config.get("start_message"):
            message_parts.append(custom_config["start_message"])
        else:
            # Default start message
            message_parts.append(
                f"🎉 A new update for **{project_name}** has been released{mc_version_display}! Find it with the link below.")

        # Main update info
        message_parts.append(f"# 🔄 {project_name} - {version_number}")
        if formatted_date:
            message_parts.append(f"**Published:** {formatted_date}")
        if mc_version_display:
            message_parts.append(f"**Minecraft Version:** {mc_version_display[5:]}")  # Remove " for "

        if version_id:
            message_parts.append(
                f"**Version Page:** https://modrinth.com/mod/{project_info.get('slug', project_id)}/version/{version_id}")

        # Changelog
        if changelog and changelog.strip():
            message_parts.append("\n## Changelog")
            message_parts.append(changelog)

        # Custom end message
        if custom_config.get("end_message"):
            message_parts.append(custom_config["end_message"])

        return "\n\n".join(message_parts)

    async def check_project_updates(self, guild_id: int, project_id: str, track_configs: List[Dict[str, Any]]):
        """Check for updates for a specific project."""
        try:
            project_info = await self.get_project_info(project_id)
            if not project_info:
                log.error(f"Failed to get project info for {project_id}")
                return

            # Check each tracking configuration
            for config in track_configs:
                mc_versions = config.get("mc_versions")  # List of MC versions or None
                last_version_ids = config.get("last_version_ids", {})

                # Handle both old format (single mc_version) and new format (multiple mc_versions)
                if mc_versions is None:
                    # Old format or no MC version filter
                    versions_to_check = [None]
                elif isinstance(mc_versions, str):
                    # Migration from old format
                    versions_to_check = [mc_versions]
                    # Update to new format
                    config["mc_versions"] = [mc_versions]
                    if "last_version_id" in config:
                        config["last_version_ids"] = {mc_versions: config["last_version_id"]}
                        del config["last_version_id"]
                else:
                    # New format with multiple versions
                    versions_to_check = mc_versions

                # Track if any version was updated
                config_updated = False

                # Check each MC version
                for mc_version in versions_to_check:
                    latest_version = await self.get_latest_version(project_id, mc_version)

                    if not latest_version:
                        continue

                    # Check if this is a new version
                    version_key = mc_version if mc_version else "all"
                    last_version_id = last_version_ids.get(version_key)
                    current_version_id = latest_version.get("id")

                    # Log for debugging
                    log.debug(
                        f"Checking {project_id} MC:{mc_version} - Last: {last_version_id}, Current: {current_version_id}")

                    if last_version_id != current_version_id:
                        # New version found!
                        log.info(f"New version found for {project_id} MC:{mc_version}: {current_version_id}")

                        await self.send_update_message(guild_id, project_id, project_info, latest_version, config,
                                                       mc_version)

                        # Update stored version
                        last_version_ids[version_key] = current_version_id
                        config_updated = True

                # Only update config if something changed
                if config_updated:
                    config["last_version_ids"] = last_version_ids

        except Exception as e:
            log.error(f"Error checking updates for project {project_id}: {e}")

    async def send_update_message(self, guild_id: int, project_id: str, project_info: Dict[str, Any],
                                  version_info: Dict[str, Any], config: Dict[str, Any],
                                  mc_version: Optional[str] = None):
        """Send update message to Discord channel."""
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            channel = guild.get_channel(config["channel_id"])
            if not channel:
                return

            # Get custom message configuration
            custom_messages = await self.config.guild(guild).custom_messages()
            custom_config = custom_messages.get(project_id, {})

            # Format message
            message = self.format_update_message(project_id, project_info, version_info, custom_config, mc_version)

            # Split message if too long
            pages = list(pagify(message, delims=["\n\n", "\n"], page_length=2000))

            for i, page in enumerate(pages):
                embed = discord.Embed(description=page, color=discord.Color.green())

                # Add role pings only to the first message
                content = None
                if i == 0:
                    ping_role_ids = config.get("ping_role_ids", [])
                    # Handle legacy single role format
                    if config.get("ping_role_id") and config["ping_role_id"] not in ping_role_ids:
                        ping_role_ids.append(config["ping_role_id"])

                    if ping_role_ids:
                        role_mentions = []
                        for role_id in ping_role_ids:
                            role = guild.get_role(role_id)
                            if role:
                                role_mentions.append(role.mention)
                        if role_mentions:
                            content = " ".join(role_mentions)

                await channel.send(content=content, embed=embed)

        except Exception as e:
            log.error(f"Error sending update message: {e}")

    async def check_all_projects(self):
        """Check all tracked projects for updates."""
        for guild in self.bot.guilds:
            guild_config = self.config.guild(guild)
            tracked_projects = await guild_config.tracked_projects()

            for project_id, track_configs in tracked_projects.items():
                await self.check_project_updates(guild.id, project_id, track_configs)

            # Save updated configurations
            await guild_config.tracked_projects.set(tracked_projects)

    def _find_tracking_config(self, tracked_projects: Dict, project_id: str, channel_id: int) -> Optional[Dict]:
        """Find a specific tracking configuration."""
        if project_id not in tracked_projects:
            return None

        for config in tracked_projects[project_id]:
            if config["channel_id"] == channel_id:
                return config
        return None

    @commands.group(invoke_without_command=True)
    async def track(self, ctx):
        """Project tracking commands."""
        await ctx.send_help(ctx.command)

    @track.command(name="add")
    async def track_add(self, ctx, project_id: str, channel: discord.TextChannel, *args):
        """
        Track a Modrinth project for updates.

        Usage: `track add <project_id> <channel> [role1] [role2] ... [--mc version1 version2 ...]`

        Parameters:
        - project_id: The Modrinth project ID or slug
        - channel: Channel to post updates to
        - roles: Optional roles to ping on updates (mention them or use role IDs)
        - --mc: Flag to specify Minecraft versions (e.g., --mc 1.21.4 1.21.5)
        """
        roles = []
        mc_versions = []

        # Parse arguments
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--mc":
                # Everything after --mc are MC versions
                mc_versions = list(args[i + 1:])
                break
            else:
                # Try to parse as role
                try:
                    # Try to get role by mention or ID
                    role = await commands.RoleConverter().convert(ctx, arg)
                    roles.append(role)
                except commands.BadArgument:
                    await ctx.send(f"❌ Could not find role: {arg}")
                    return
            i += 1

        # Validate project exists
        project_info = await self.get_project_info(project_id)
        if not project_info:
            await ctx.send(f"❌ Could not find project with ID: {project_id}")
            return

        # Convert to lists
        mc_versions_list = mc_versions if mc_versions else None
        role_ids = [role.id for role in roles]

        # Get current tracking config
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        # Check if project is already tracked in this channel
        existing_config = self._find_tracking_config(tracked_projects, project_id, channel.id)
        if existing_config:
            await ctx.send(
                f"❌ Project {project_info['title']} is already tracked in {channel.mention}. Use `track edit` to modify the configuration.")
            return

        # Initialize last_version_ids for each MC version
        last_version_ids = {}
        if mc_versions_list:
            for mc_version in mc_versions_list:
                latest_version = await self.get_latest_version(project_id, mc_version)
                if latest_version:
                    last_version_ids[mc_version] = latest_version.get("id")
                else:
                    await ctx.send(f"⚠️ Warning: Could not find any versions for MC {mc_version}")
        else:
            # No MC version filter - use "all" as key
            latest_version = await self.get_latest_version(project_id, None)
            if latest_version:
                last_version_ids["all"] = latest_version.get("id")
            else:
                await ctx.send(f"❌ Could not find any versions for project {project_info['title']}")
                return

        # Create tracking configuration
        track_config = {
            "channel_id": channel.id,
            "ping_role_ids": role_ids,
            "mc_versions": mc_versions_list,
            "last_version_ids": last_version_ids,
            "added_by": ctx.author.id,
            "added_at": datetime.now().isoformat()
        }

        # Add to tracked projects
        if project_id not in tracked_projects:
            tracked_projects[project_id] = []
        tracked_projects[project_id].append(track_config)

        await guild_config.tracked_projects.set(tracked_projects)

        # Confirmation message
        if mc_versions_list:
            versions_str = ", ".join(mc_versions_list)
            version_info = f" (MC versions: {versions_str})"
        else:
            version_info = " (all MC versions)"

        role_info = ""
        if roles:
            role_mentions = [role.mention for role in roles]
            role_info = f" pinging {', '.join(role_mentions)}"

        await ctx.send(
            f"✅ Now tracking **{project_info['title']}** ({project_id}){version_info} in {channel.mention}{role_info}")

    @track.command(name="edit")
    async def track_edit(self, ctx, project_id: str, channel: discord.TextChannel, action: str, *args):
        """
        Edit tracking configuration for a project.

        Actions:
        - `roles add <role1> [role2] ...` - Add roles to ping
        - `roles remove <role1> [role2] ...` - Remove roles from ping
        - `roles set <role1> [role2] ...` - Set roles to ping (replaces all)
        - `roles clear` - Remove all role pings
        - `mc add <version1> [version2] ...` - Add MC versions to track
        - `mc remove <version1> [version2] ...` - Remove MC versions from tracking
        - `mc set <version1> [version2] ...` - Set MC versions to track (replaces all)
        - `mc clear` - Track all MC versions
        """
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        # Find the tracking configuration
        config = self._find_tracking_config(tracked_projects, project_id, channel.id)
        if not config:
            await ctx.send(f"❌ Project {project_id} is not tracked in {channel.mention}")
            return

        if action == "roles":
            if not args:
                await ctx.send("❌ Please specify a roles action: add, remove, set, or clear")
                return

            sub_action = args[0]
            role_args = args[1:]

            # Initialize ping_role_ids if not present (for legacy configs)
            if "ping_role_ids" not in config:
                config["ping_role_ids"] = []
                # Migrate legacy single role
                if config.get("ping_role_id"):
                    config["ping_role_ids"].append(config["ping_role_id"])
                    del config["ping_role_id"]

            if sub_action == "clear":
                config["ping_role_ids"] = []
                await ctx.send(f"✅ Cleared all role pings for {project_id} in {channel.mention}")

            elif sub_action in ["add", "remove", "set"]:
                if not role_args:
                    await ctx.send(f"❌ Please specify roles to {sub_action}")
                    return

                roles = []
                for role_arg in role_args:
                    try:
                        role = await commands.RoleConverter().convert(ctx, role_arg)
                        roles.append(role)
                    except commands.BadArgument:
                        await ctx.send(f"❌ Could not find role: {role_arg}")
                        return

                role_ids = [role.id for role in roles]

                if sub_action == "add":
                    for role_id in role_ids:
                        if role_id not in config["ping_role_ids"]:
                            config["ping_role_ids"].append(role_id)
                    action_text = "Added"
                elif sub_action == "remove":
                    config["ping_role_ids"] = [rid for rid in config["ping_role_ids"] if rid not in role_ids]
                    action_text = "Removed"
                elif sub_action == "set":
                    config["ping_role_ids"] = role_ids
                    action_text = "Set"

                role_mentions = [role.mention for role in roles]
                await ctx.send(
                    f"✅ {action_text} roles for {project_id} in {channel.mention}: {', '.join(role_mentions)}")

            else:
                await ctx.send("❌ Invalid roles action. Use: add, remove, set, or clear")
                return

        elif action == "mc":
            if not args:
                await ctx.send("❌ Please specify an MC action: add, remove, set, or clear")
                return

            sub_action = args[0]
            mc_args = args[1:]

            if sub_action == "clear":
                # Clear MC versions and reset last_version_ids
                config["mc_versions"] = None
                latest_version = await self.get_latest_version(project_id, None)
                if latest_version:
                    config["last_version_ids"] = {"all": latest_version.get("id")}
                await ctx.send(f"✅ Now tracking all MC versions for {project_id} in {channel.mention}")

            elif sub_action in ["add", "remove", "set"]:
                if not mc_args:
                    await ctx.send(f"❌ Please specify MC versions to {sub_action}")
                    return

                current_versions = config.get("mc_versions", []) or []

                if sub_action == "add":
                    for version in mc_args:
                        if version not in current_versions:
                            current_versions.append(version)
                            # Get initial version for this MC version
                            latest_version = await self.get_latest_version(project_id, version)
                            if latest_version:
                                config["last_version_ids"][version] = latest_version.get("id")
                    action_text = "Added"
                elif sub_action == "remove":
                    current_versions = [v for v in current_versions if v not in mc_args]
                    # Remove from last_version_ids too
                    for version in mc_args:
                        config["last_version_ids"].pop(version, None)
                    action_text = "Removed"
                elif sub_action == "set":
                    current_versions = list(mc_args)
                    # Reset last_version_ids for new versions
                    config["last_version_ids"] = {}
                    for version in current_versions:
                        latest_version = await self.get_latest_version(project_id, version)
                        if latest_version:
                            config["last_version_ids"][version] = latest_version.get("id")
                    action_text = "Set"

                config["mc_versions"] = current_versions if current_versions else None
                await ctx.send(
                    f"✅ {action_text} MC versions for {project_id} in {channel.mention}: {', '.join(mc_args)}")

            else:
                await ctx.send("❌ Invalid MC action. Use: add, remove, set, or clear")
                return

        else:
            await ctx.send("❌ Invalid action. Use 'roles' or 'mc'")
            return

        # Save changes
        await guild_config.tracked_projects.set(tracked_projects)

    @track.command(name="remove")
    async def track_remove(self, ctx, project_id: str, channel: Optional[discord.TextChannel] = None):
        """
        Stop tracking a project.

        If channel is specified, only remove tracking for that channel.
        Otherwise, remove all tracking for the project.
        """
        guild_config = self.config.guild(ctx.guild)
        tracked_projects = await guild_config.tracked_projects()

        if project_id not in tracked_projects:
            await ctx.send(f"❌ Project {project_id} is not being tracked")
            return

        if channel:
            # Remove tracking for specific channel
            original_count = len(tracked_projects[project_id])
            tracked_projects[project_id] = [
                config for config in tracked_projects[project_id]
                if config["channel_id"] != channel.id
            ]

            if len(tracked_projects[project_id]) == original_count:
                await ctx.send(f"❌ Project {project_id} is not being tracked in {channel.mention}")
                return

            # Remove project entirely if no more channels
            if not tracked_projects[project_id]:
                del tracked_projects[project_id]

            await ctx.send(f"✅ Stopped tracking {project_id} in {channel.mention}")
        else:
            # Remove all tracking for project
            del tracked_projects[project_id]
            await ctx.send(f"✅ Stopped tracking {project_id} in all channels")

        await guild_config.tracked_projects.set(tracked_projects)

    @track.command(name="list")
    async def track_list(self, ctx):
        """List all tracked projects."""
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

                # Handle role pings (both old and new format)
                role_info = ""
                ping_role_ids = config.get("ping_role_ids", [])
                if config.get("ping_role_id") and config["ping_role_id"] not in ping_role_ids:
                    ping_role_ids.append(config["ping_role_id"])

                if ping_role_ids:
                    role_mentions = []
                    for role_id in ping_role_ids:
                        role = ctx.guild.get_role(role_id)
                        if role:
                            role_mentions.append(role.mention)
                    if role_mentions:
                        role_info = f" (pings {', '.join(role_mentions)})"

                # Handle MC versions
                mc_versions = config.get("mc_versions")
                if mc_versions:
                    if isinstance(mc_versions, list):
                        mc_info = f" [MC {', '.join(mc_versions)}]"
                    else:
                        mc_info = f" [MC {mc_versions}]"
                elif config.get("mc_version"):  # Legacy format
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

    @commands.group(invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def trackconfig(self, ctx):
        """Configuration commands for project tracking."""
        await ctx.send_help(ctx.command)

    @trackconfig.command(name="interval")
    @checks.admin_or_permissions(manage_guild=True)
    async def set_interval(self, ctx, seconds: int):
        """Set the update check interval in seconds (minimum 60)."""
        if seconds < 60:
            await ctx.send("❌ Interval must be at least 60 seconds")
            return

        await self.config.check_interval.set(seconds)
        await ctx.send(f"✅ Update check interval set to {seconds} seconds")

        # Restart the update loop with new interval
        if self.update_task:
            self.update_task.cancel()
        self.update_task = asyncio.create_task(self.update_checker_loop())

    @trackconfig.command(name="ratelimit")
    @checks.admin_or_permissions(manage_guild=True)
    async def set_rate_limit(self, ctx, calls_per_minute: int):
        """Set the API rate limit (calls per minute)."""
        if calls_per_minute < 1:
            await ctx.send("❌ Rate limit must be at least 1 call per minute")
            return

        await self.config.api_rate_limit.set(calls_per_minute)
        await ctx.send(f"✅ API rate limit set to {calls_per_minute} calls per minute")

    @track.command(name="check")
    @checks.admin_or_permissions(manage_guild=True)
    async def force_check(self, ctx):
        """Force check for updates on all tracked projects."""
        await ctx.send("🔄 Checking for updates...")
        await self.check_all_projects()
        await ctx.send("✅ Update check completed!")

    @track.command(name="latest")
    async def show_latest(self, ctx, project_id: Optional[str] = None):
        """
        Show the latest version info for tracked projects.

        If project_id is specified, show only that project.
        Otherwise, show all tracked projects.
        """
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

                # Handle both old and new MC version formats
                mc_versions = config.get("mc_versions")
                if mc_versions:
                    if isinstance(mc_versions, list):
                        versions_to_check = mc_versions
                    else:
                        versions_to_check = [mc_versions]
                elif config.get("mc_version"):  # Legacy format
                    versions_to_check = [config["mc_version"]]
                else:
                    versions_to_check = [None]

                for mc_version in versions_to_check:
                    latest_version = await self.get_latest_version(proj_id, mc_version)
                    if not latest_version:
                        continue

                    # Get custom message configuration
                    custom_messages = await guild_config.custom_messages()
                    custom_config = custom_messages.get(proj_id, {})

                    # Send the latest version info
                    await self.send_update_message(ctx.guild.id, proj_id, project_info, latest_version, config,
                                                   mc_version)

        await ctx.send("✅ Latest version info sent!")

    @commands.group(invoke_without_command=True)
    async def trackmsg(self, ctx):
        """Customize tracking messages."""
        await ctx.send_help(ctx.command)

    @trackmsg.command(name="start")
    async def set_start_message(self, ctx, project_id: str, *, message: str):
        """Set a custom start message for a project."""
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

    @trackmsg.command(name="end")
    async def set_end_message(self, ctx, project_id: str, *, message: str):
        """Set a custom end message for a project."""
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

    @trackmsg.command(name="clear")
    async def clear_custom_messages(self, ctx, project_id: str):
        """Clear custom messages for a project."""
        guild_config = self.config.guild(ctx.guild)
        custom_messages = await guild_config.custom_messages()

        if project_id in custom_messages:
            del custom_messages[project_id]
            await guild_config.custom_messages.set(custom_messages)
            await ctx.send(f"✅ Custom messages cleared for {project_id}")
        else:
            await ctx.send(f"❌ No custom messages found for {project_id}")