"""Main cog file for Modrinth Update Notifier."""

import discord
from redbot.core import commands, Config, tasks
from redbot.core.utils.chat_formatting import box, humanize_list
from redbot.core.utils.predicates import MessagePredicate
import asyncio
import logging
from typing import Dict, List, Optional, Union
from datetime import datetime, timedelta

from .api import ModrinthAPI, ProjectNotFoundError, ModrinthAPIError
from .models import GuildConfig, UserConfig, MonitoredProject, UserProject
from .utils import (
    create_update_embed,
    create_project_info_embed,
    format_role_list,
    format_project_list,
    truncate_text,
    format_time_ago
)

log = logging.getLogger("red.modrinthnotifier")


class ModrinthNotifier(commands.Cog):
    """Monitor Modrinth projects for updates and send notifications."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)

        # Default configurations
        default_guild = {
            "channel_id": None,
            "default_role_ids": [],
            "check_interval": 15,
            "enabled": False,
            "projects": {},
            "last_check": 0
        }

        default_user = {
            "enabled": True,
            "channel_id": None,
            "use_dm": True,
            "projects": {}
        }

        self.config.register_guild(**default_guild)
        self.config.register_user(**default_user)

        # Runtime storage for loaded configs
        self.guild_configs: Dict[int, GuildConfig] = {}
        self.user_configs: Dict[int, UserConfig] = {}

        # API client
        self.api = ModrinthAPI()

        # Start background tasks
        self.update_checker.start()

    def cog_unload(self):
        """Clean up when cog is unloaded."""
        self.update_checker.cancel()
        asyncio.create_task(self.api.close_session())

    async def cog_load(self):
        """Load configurations when cog starts."""
        await self.api.start_session()
        await self._load_all_configs()

    async def _load_all_configs(self):
        """Load all guild and user configurations."""
        # Load guild configs
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            self.guild_configs[guild_id] = GuildConfig.from_dict(data)

        # Load user configs
        all_users = await self.config.all_users()
        for user_id, data in all_users.items():
            self.user_configs[user_id] = UserConfig.from_dict(data)

        log.info(f"Loaded {len(self.guild_configs)} guild configs and {len(self.user_configs)} user configs")

    async def _save_guild_config(self, guild_id: int):
        """Save a guild configuration."""
        if guild_id in self.guild_configs:
            await self.config.guild_from_id(guild_id).set(self.guild_configs[guild_id].to_dict())

    async def _save_user_config(self, user_id: int):
        """Save a user configuration."""
        if user_id in self.user_configs:
            await self.config.user_from_id(user_id).set(self.user_configs[user_id].to_dict())

    def _get_guild_config(self, guild_id: int) -> GuildConfig:
        """Get or create guild configuration."""
        if guild_id not in self.guild_configs:
            self.guild_configs[guild_id] = GuildConfig()
        return self.guild_configs[guild_id]

    def _get_user_config(self, user_id: int) -> UserConfig:
        """Get or create user configuration."""
        if user_id not in self.user_configs:
            self.user_configs[user_id] = UserConfig()
        return self.user_configs[user_id]

    # Background Tasks

    @tasks.loop(minutes=1)
    async def update_checker(self):
        """Main background task to check for updates."""
        try:
            current_time = datetime.utcnow()

            # Check guild projects
            for guild_id, config in self.guild_configs.items():
                if not config.enabled or not config.projects:
                    continue

                # Check if it's time to check this guild
                last_check = datetime.fromtimestamp(config.last_check) if config.last_check else datetime.min
                if current_time - last_check >= timedelta(minutes=config.check_interval):
                    await self._check_guild_projects(guild_id)
                    config.last_check = current_time.timestamp()
                    await self._save_guild_config(guild_id)

            # Check user projects every 10 minutes
            if current_time.minute % 10 == 0:
                await self._check_user_projects()

        except Exception as e:
            log.error(f"Error in update checker: {e}", exc_info=True)

    @update_checker.before_loop
    async def before_update_checker(self):
        """Wait for bot to be ready before starting update checker."""
        await self.bot.wait_until_ready()
        # Wait a bit more for everything to settle
        await asyncio.sleep(30)

    async def _check_guild_projects(self, guild_id: int):
        """Check for updates on all projects for a guild."""
        config = self.guild_configs.get(guild_id)
        if not config or not config.enabled:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(config.channel_id) if config.channel_id else None
        if not channel:
            log.warning(f"No valid notification channel for guild {guild_id}")
            return

        log.debug(f"Checking {len(config.projects)} projects for guild {guild.name}")

        for project_id, project in config.projects.items():
            try:
                await self._check_project_update(project, guild, channel, config)
                # Small delay between checks to be nice to the API
                await asyncio.sleep(0.5)
            except Exception as e:
                log.error(f"Error checking project {project_id} for guild {guild_id}: {e}")

    async def _check_user_projects(self):
        """Check for updates on all user projects."""
        for user_id, config in self.user_configs.items():
            if not config.enabled or not config.projects:
                continue

            user = self.bot.get_user(user_id)
            if not user:
                continue

            log.debug(f"Checking {len(config.projects)} projects for user {user}")

            for project_id, project in config.projects.items():
                try:
                    await self._check_user_project_update(project, user, config)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    log.error(f"Error checking project {project_id} for user {user_id}: {e}")

    async def _check_project_update(self, project: MonitoredProject, guild: discord.Guild,
                                    channel: discord.TextChannel, config: GuildConfig):
        """Check for updates on a specific project for a guild."""
        try:
            latest_version = await self.api.get_latest_version(project.id)
            if not latest_version:
                return

            # Check if this is a new version
            if project.last_version is None:
                # First time checking, just record the current version
                project.last_version = latest_version.id
                await self._save_guild_config(guild.id)
                return

            if latest_version.id == project.last_version:
                return  # No update

            # New version found!
            log.info(f"New version {latest_version.version_number} found for {project.name}")

            # Get full project info for the embed
            project_info = await self.api.get_project(project.id)
            embed = create_update_embed(project_info, latest_version)

            # Prepare role mentions
            role_mentions = []

            # Add default roles
            for role_id in config.default_role_ids:
                role = guild.get_role(role_id)
                if role:
                    role_mentions.append(role.mention)

            # Add project-specific roles
            for role_id in project.role_ids:
                role = guild.get_role(role_id)
                if role and role.mention not in role_mentions:
                    role_mentions.append(role.mention)

            content = " ".join(role_mentions) if role_mentions else None

            # Send notification
            try:
                await channel.send(content=content, embed=embed)
                log.info(f"Sent update notification for {project.name} to {guild.name}")
            except discord.Forbidden:
                log.warning(f"No permission to send message in {channel} ({guild.name})")
            except discord.HTTPException as e:
                log.error(f"Failed to send message: {e}")

            # Update last version
            project.last_version = latest_version.id
            await self._save_guild_config(guild.id)

        except ProjectNotFoundError:
            log.warning(f"Project {project.id} not found, removing from monitoring")
            # Remove invalid project
            config.projects.pop(project.id, None)
            await self._save_guild_config(guild.id)
        except ModrinthAPIError as e:
            log.error(f"API error checking {project.id}: {e}")

    async def _check_user_project_update(self, project: UserProject, user: discord.User, config: UserConfig):
        """Check for updates on a specific project for a user."""
        try:
            latest_version = await self.api.get_latest_version(project.id)
            if not latest_version:
                return

            # Check if this is a new version
            if project.last_version is None:
                project.last_version = latest_version.id
                await self._save_user_config(user.id)
                return

            if latest_version.id == project.last_version:
                return  # No update

            # New version found!
            project_info = await self.api.get_project(project.id)
            embed = create_update_embed(project_info, latest_version)

            # Send notification
            try:
                if config.use_dm:
                    await user.send(embed=embed)
                elif config.channel_id:
                    channel = self.bot.get_channel(config.channel_id)
                    if channel:
                        await channel.send(f"{user.mention}", embed=embed)
                    else:
                        # Fallback to DM if channel not accessible
                        await user.send(embed=embed)

                log.info(f"Sent personal update notification for {project.name} to {user}")
            except discord.Forbidden:
                log.warning(f"Cannot send DM to {user}")
            except discord.HTTPException as e:
                log.error(f"Failed to send message to {user}: {e}")

            # Update last version
            project.last_version = latest_version.id
            await self._save_user_config(user.id)

        except ProjectNotFoundError:
            log.warning(f"User project {project.id} not found, removing from watchlist")
            config.projects.pop(project.id, None)
            await self._save_user_config(user.id)
        except ModrinthAPIError as e:
            log.error(f"API error checking user project {project.id}: {e}")

    # Commands

    @commands.group(name="modrinth", aliases=["mr"])
    async def modrinth(self, ctx):
        """Modrinth update notifications."""
        pass

    # Admin Commands

    @modrinth.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def add_project(self, ctx, project_id: str, *roles: discord.Role):
        """Add a project to server monitoring.

        Example: `[p]modrinth add sodium @ModUpdates @Everyone`
        """
        config = self._get_guild_config(ctx.guild.id)

        # Check if project already exists
        if project_id in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is already being monitored.")
            return

        # Validate project with API
        async with ctx.typing():
            try:
                project_name = await self.api.validate_project_id(project_id)
                if not project_name:
                    await ctx.send(f"❌ Project `{project_id}` not found on Modrinth.")
                    return
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error validating project: {e}")
                return

        # Add project
        role_ids = [role.id for role in roles]
        project = MonitoredProject(
            id=project_id,
            name=project_name,
            role_ids=role_ids,
            added_by=ctx.author.id
        )

        config.projects[project_id] = project
        await self._save_guild_config(ctx.guild.id)

        # Create confirmation message
        msg = f"✅ Now monitoring **{project_name}** (`{project_id}`)"
        if roles:
            msg += f" - Will ping: {', '.join(role.mention for role in roles)}"

        await ctx.send(msg)

    @modrinth.command(name="remove", aliases=["delete", "del"])
    @commands.admin_or_permissions(manage_guild=True)
    async def remove_project(self, ctx, project_id: str):
        """Remove a project from server monitoring."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored.")
            return

        project_name = config.projects[project_id].name
        del config.projects[project_id]
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Removed **{project_name}** (`{project_id}`) from monitoring.")

    @modrinth.command(name="addrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def add_role(self, ctx, project_id: str, role: discord.Role):
        """Add a role to ping for specific project updates."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored.")
            return

        project = config.projects[project_id]
        if role.id in project.role_ids:
            await ctx.send(f"❌ {role.mention} is already set to be pinged for **{project.name}**.")
            return

        project.role_ids.append(role.id)
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Added {role.mention} to **{project.name}** notifications.")

    @modrinth.command(name="removerole")
    @commands.admin_or_permissions(manage_guild=True)
    async def remove_role(self, ctx, project_id: str, role: discord.Role):
        """Remove a role from project notifications."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored.")
            return

        project = config.projects[project_id]
        if role.id not in project.role_ids:
            await ctx.send(f"❌ {role.mention} is not set to be pinged for **{project.name}**.")
            return

        project.role_ids.remove(role.id)
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Removed {role.mention} from **{project.name}** notifications.")

    @modrinth.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def set_channel(self, ctx, channel: discord.TextChannel):
        """Set the notification channel for server updates."""
        config = self._get_guild_config(ctx.guild.id)
        config.channel_id = channel.id
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Set notification channel to {channel.mention}")

    @modrinth.command(name="defaultrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def add_default_role(self, ctx, role: discord.Role):
        """Add a role to the default ping list."""
        config = self._get_guild_config(ctx.guild.id)

        if role.id in config.default_role_ids:
            await ctx.send(f"❌ {role.mention} is already in the default roles list.")
            return

        config.default_role_ids.append(role.id)
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Added {role.mention} to default notification roles.")

    @modrinth.command(name="removedefaultrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def remove_default_role(self, ctx, role: discord.Role):
        """Remove a role from the default ping list."""
        config = self._get_guild_config(ctx.guild.id)

        if role.id not in config.default_role_ids:
            await ctx.send(f"❌ {role.mention} is not in the default roles list.")
            return

        config.default_role_ids.remove(role.id)
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Removed {role.mention} from default notification roles.")

    @modrinth.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def set_interval(self, ctx, minutes: int):
        """Set check interval (minimum 5 minutes)."""
        if minutes < 5:
            await ctx.send("❌ Minimum interval is 5 minutes.")
            return

        config = self._get_guild_config(ctx.guild.id)
        config.check_interval = minutes
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Set check interval to {minutes} minutes.")

    @modrinth.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def toggle_notifications(self, ctx):
        """Enable/disable server notifications."""
        config = self._get_guild_config(ctx.guild.id)

        if not config.channel_id:
            await ctx.send("❌ Please set a notification channel first with `[p]modrinth channel`.")
            return

        config.enabled = not config.enabled
        await self._save_guild_config(ctx.guild.id)

        status = "enabled" if config.enabled else "disabled"
        await ctx.send(f"✅ Server notifications {status}.")

    # User Commands

    @modrinth.group(name="user", aliases=["personal"])
    async def user_commands(self, ctx):
        """Personal project watchlist commands."""
        pass

    @user_commands.command(name="add")
    async def user_add(self, ctx, project_id: str):
        """Add project to personal watchlist."""
        config = self._get_user_config(ctx.author.id)

        if project_id in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is already in your watchlist.")
            return

        # Validate project
        async with ctx.typing():
            try:
                project_name = await self.api.validate_project_id(project_id)
                if not project_name:
                    await ctx.send(f"❌ Project `{project_id}` not found on Modrinth.")
                    return
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error validating project: {e}")
                return

        # Add to watchlist
        project = UserProject(id=project_id, name=project_name)
        config.projects[project_id] = project
        await self._save_user_config(ctx.author.id)

        await ctx.send(f"✅ Added **{project_name}** (`{project_id}`) to your watchlist.")

    @user_commands.command(name="remove", aliases=["delete", "del"])
    async def user_remove(self, ctx, project_id: str):
        """Remove project from personal watchlist."""
        config = self._get_user_config(ctx.author.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not in your watchlist.")
            return

        project_name = config.projects[project_id].name
        del config.projects[project_id]
        await self._save_user_config(ctx.author.id)

        await ctx.send(f"✅ Removed **{project_name}** (`{project_id}`) from your watchlist.")

    @user_commands.command(name="list")
    async def user_list(self, ctx):
        """List personal projects with names and versions."""
        config = self._get_user_config(ctx.author.id)

        if not config.projects:
            await ctx.send("📝 Your watchlist is empty.")
            return

        lines = []
        for project_id, project in config.projects.items():
            line = f"**{project.name}** (`{project_id}`)"
            if project.last_version:
                line += f" - Last: `{project.last_version}`"
            lines.append(line)

        content = "\n".join(lines)
        content = truncate_text(content)

        embed = discord.Embed(
            title="Your Project Watchlist",
            description=content,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"{len(config.projects)} projects")

        await ctx.send(embed=embed)

    @user_commands.command(name="channel")
    async def user_channel(self, ctx, channel: discord.TextChannel):
        """Set private channel for notifications (if accessible)."""
        # Check if user can see the channel
        if not channel.permissions_for(ctx.author).read_messages:
            await ctx.send("❌ You don't have access to that channel.")
            return

        config = self._get_user_config(ctx.author.id)
        config.channel_id = channel.id
        config.use_dm = False
        await self._save_user_config(ctx.author.id)

        await ctx.send(f"✅ Personal notifications will be sent to {channel.mention}")

    @user_commands.command(name="dm")
    async def user_dm(self, ctx):
        """Enable DM notifications."""
        config = self._get_user_config(ctx.author.id)
        config.use_dm = True
        config.channel_id = None
        await self._save_user_config(ctx.author.id)

        await ctx.send("✅ Personal notifications will be sent via DM.")

    @user_commands.command(name="toggle")
    async def user_toggle(self, ctx):
        """Enable/disable personal notifications."""
        config = self._get_user_config(ctx.author.id)
        config.enabled = not config.enabled
        await self._save_user_config(ctx.author.id)

        status = "enabled" if config.enabled else "disabled"
        await ctx.send(f"✅ Personal notifications {status}.")

    @user_commands.command(name="settings")
    async def user_settings(self, ctx):
        """Display current user configuration."""
        config = self._get_user_config(ctx.author.id)

        embed = discord.Embed(
            title="Your Personal Settings",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Status",
            value="✅ Enabled" if config.enabled else "❌ Disabled",
            inline=True
        )

        if config.use_dm:
            method = "Direct Messages"
        elif config.channel_id:
            channel = self.bot.get_channel(config.channel_id)
            method = f"#{channel.name}" if channel else "Invalid Channel"
        else:
            method = "Not Set"

        embed.add_field(name="Notification Method", value=method, inline=True)
        embed.add_field(name="Projects", value=str(len(config.projects)), inline=True)

        await ctx.send(embed=embed)

    # General Commands

    @modrinth.command(name="list")
    async def list_projects(self, ctx):
        """List all server-monitored projects with names."""
        config = self._get_guild_config(ctx.guild.id)

        if not config.projects:
            await ctx.send("📝 No projects are being monitored in this server.")
            return

        lines = format_project_list(config.projects, ctx.guild)
        content = "\n".join(lines)
        content = truncate_text(content)

        embed = discord.Embed(
            title="Monitored Projects",
            description=content,
            color=discord.Color.green()
        )
        embed.set_footer(text=f"{len(config.projects)} projects")

        await ctx.send(embed=embed)

    @modrinth.command(name="info")
    async def project_info(self, ctx, project_id: str):
        """Show detailed project information from Modrinth API."""
        async with ctx.typing():
            try:
                project = await self.api.get_project(project_id)
                embed = create_project_info_embed(project)
                await ctx.send(embed=embed)
            except ProjectNotFoundError:
                await ctx.send(f"❌ Project `{project_id}` not found on Modrinth.")
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error fetching project info: {e}")

    @modrinth.command(name="check")
    @commands.admin_or_permissions(manage_guild=True)
    async def manual_check(self, ctx, project_id: str):
        """Manually check for updates on specific project."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored.")
            return

        project = config.projects[project_id]

        async with ctx.typing():
            try:
                latest_version = await self.api.get_latest_version(project_id)
                if not latest_version:
                    await ctx.send(f"❌ Could not fetch latest version for **{project.name}**.")
                    return

                if project.last_version == latest_version.id:
                    await ctx.send(f"✅ **{project.name}** is up to date (Version: {latest_version.version_number})")
                else:
                    project_info = await self.api.get_project(project_id)
                    embed = create_update_embed(project_info, latest_version)
                    embed.title = f"Manual Check: {embed.title}"
                    await ctx.send(embed=embed)

                    # Ask if they want to mark as notified
                    pred = MessagePredicate.yes_or_no(ctx)
                    await ctx.send("Mark this version as already notified? (yes/no)")
                    try:
                        await self.bot.wait_for("message", check=pred, timeout=30)
                        if pred.result:
                            project.last_version = latest_version.id
                            await self._save_guild_config(ctx.guild.id)
                            await ctx.send("✅ Marked as notified.")
                    except asyncio.TimeoutError:
                        pass

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error checking project: {e}")

    @modrinth.command(name="settings")
    @commands.admin_or_permissions(manage_guild=True)
    async def show_settings(self, ctx):
        """Display current server configuration."""
        config = self._get_guild_config(ctx.guild.id)

        embed = discord.Embed(
            title="Server Settings",
            color=discord.Color.blue()
        )

        # Status
        status = "✅ Enabled" if config.enabled else "❌ Disabled"
        embed.add_field(name="Status", value=status, inline=True)

        # Channel
        if config.channel_id:
            channel = ctx.guild.get_channel(config.channel_id)
            channel_name = channel.mention if channel else "Invalid Channel"
        else:
            channel_name = "Not Set"
        embed.add_field(name="Channel", value=channel_name, inline=True)

        # Interval
        embed.add_field(name="Check Interval", value=f"{config.check_interval} minutes", inline=True)

        # Default roles
        default_roles = [ctx.guild.get_role(rid) for rid in config.default_role_ids]
        default_roles = [role for role in default_roles if role]
        roles_text = format_role_list(default_roles)
        embed.add_field(name="Default Roles", value=roles_text, inline=False)

        # Project count
        embed.add_field(name="Projects", value=str(len(config.projects)), inline=True)

        # Last check
        if config.last_check:
            last_check = format_time_ago(config.last_check)
        else:
            last_check = "Never"
        embed.add_field(name="Last Check", value=last_check, inline=True)

        await ctx.send(embed=embed)