import asyncio
import aiohttp
import discord
from discord.ext import commands
from redbot.core import Config, checks, commands as red_commands
from redbot.core.utils.chat_formatting import humanize_list, box
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
from typing import Dict, List, Optional, Any, Union
import logging
from datetime import datetime, timedelta

from .api import ModrinthAPI
from .tasks import UpdateChecker
from .views import (
    ConfirmView, MinecraftVersionView, LoaderView,
    ReleaseChannelView, ChannelSelect, RoleSelect
)
from .utils import (
    extract_version_number, validate_project_id,
    truncate_text, format_version_list
)

log = logging.getLogger("red.modrinth_checker")


class ModrinthChecker(red_commands.Cog):
    """Monitor Modrinth projects for updates and send notifications."""

    def __init__(self, bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.api = ModrinthAPI(self.session)

        # Config setup
        self.config = Config.get_conf(self, identifier=1234567890)

        default_guild = {
            "projects": {},
            "notifications_enabled": True,
            "check_interval": 1800  # 30 minutes
        }

        self.config.register_guild(**default_guild)

        # Initialize update checker
        self.update_checker = UpdateChecker(bot, self.config, self.api)

        # Start background task
        self.bot.loop.create_task(self._start_background_tasks())

    async def _start_background_tasks(self):
        """Start background tasks after bot is ready."""
        await self.bot.wait_until_ready()
        await self.update_checker.start()

    async def cog_unload(self):
        """Clean up when cog is unloaded."""
        await self.update_checker.stop()
        await self.session.close()

    @red_commands.group(name="modrinth", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def modrinth(self, ctx):
        """Modrinth project monitoring commands."""
        await ctx.send_help(ctx.command)

    @modrinth.command(name="add")
    async def add_project(self, ctx, project_id: str):
        """Add a Modrinth project to monitor for updates.

        Use the project ID from the Modrinth page URL.
        Example: AANobbMI for Sodium
        """
        # Validate project ID format
        if not validate_project_id(project_id):
            await ctx.send("❌ Invalid project ID format. Project IDs should be 8 alphanumeric characters.")
            return

        # Check if project exists
        if not await self.api.validate_project_exists(project_id):
            await ctx.send("❌ Project not found. Please check the project ID.")
            return

        # Check if already monitoring
        async with self.config.guild(ctx.guild).projects() as projects:
            if project_id in projects:
                await ctx.send("❌ This project is already being monitored.")
                return

        # Get project info
        project_info = await self.api.get_project(project_id)
        if not project_info:
            await ctx.send("❌ Failed to get project information.")
            return

        # Show project info and ask for confirmation
        embed = await self._create_project_info_embed(project_info)
        embed.title = "Add Project to Monitoring"
        embed.description = f"Do you want to add **{project_info['title']}** to monitoring?"

        view = ConfirmView()
        message = await ctx.send(embed=embed, view=view)

        await view.wait()

        if view.value is None:
            await message.edit(content="⏰ Timed out.", embed=None, view=None)
            return
        elif not view.value:
            await message.edit(content="❌ Cancelled.", embed=None, view=None)
            return

        # Start setup process
        await self._setup_project_monitoring(ctx, project_id, project_info, message)

    async def _setup_project_monitoring(self, ctx, project_id: str, project_info: Dict[str, Any],
                                        message: discord.Message):
        """Set up monitoring configuration for a project."""
        try:
            # Step 1: Minecraft versions
            available_versions = await self.api.get_project_game_versions(project_id)
            if not available_versions:
                await message.edit(content="❌ No versions found for this project.", embed=None, view=None)
                return

            version_config = await self._setup_minecraft_versions(ctx, available_versions, message)
            if not version_config:
                await message.edit(content="❌ Setup cancelled.", embed=None, view=None)
                return

            # Step 2: Loaders
            available_loaders = await self.api.get_project_loaders(project_id)
            if not available_loaders:
                await message.edit(content="❌ No loaders found for this project.", embed=None, view=None)
                return

            loader_config = await self._setup_loaders(ctx, available_loaders, message)
            if not loader_config:
                await message.edit(content="❌ Setup cancelled.", embed=None, view=None)
                return

            # Step 3: Release channels
            channel_config = await self._setup_release_channels(ctx, message)
            if not channel_config:
                await message.edit(content="❌ Setup cancelled.", embed=None, view=None)
                return

            # Step 4: Discord channel
            discord_channel = await self._setup_discord_channel(ctx, message)
            if not discord_channel:
                await message.edit(content="❌ Setup cancelled.", embed=None, view=None)
                return

            # Step 5: Roles
            roles = await self._setup_roles(ctx, message)
            if roles is None:
                await message.edit(content="❌ Setup cancelled.", embed=None, view=None)
                return

            # Save configuration
            monitoring_config = {
                "version_type": version_config["type"],
                "versions": version_config["versions"],
                "loader_type": loader_config["type"],
                "loaders": loader_config["loaders"],
                "channel_type": channel_config["type"],
                "channels": channel_config["channels"]
            }

            project_data = {
                "name": project_info["title"],
                "monitoring_config": monitoring_config,
                "channel_id": discord_channel.id,
                "roles": [role.id for role in roles],
                "added_by": ctx.author.id,
                "added_at": datetime.now().isoformat(),
                "current_version": None,
                "last_checked": None
            }

            async with self.config.guild(ctx.guild).projects() as projects:
                projects[project_id] = project_data

            # Send initial notification
            await self._send_initial_notification(ctx, project_id, project_data, message)

        except Exception as e:
            log.error(f"Error setting up project monitoring: {e}")
            await message.edit(content="❌ An error occurred during setup.", embed=None, view=None)

    async def _setup_minecraft_versions(self, ctx, available_versions: List[str], message: discord.Message) -> Optional[
        Dict[str, Any]]:
        """Set up Minecraft version monitoring."""
        # Check for snapshots
        has_snapshots = any(self._is_snapshot(v) for v in available_versions)

        embed = discord.Embed(
            title="Step 1: Minecraft Versions",
            description="Which Minecraft versions should be monitored?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Available Versions",
            value=format_version_list(available_versions),
            inline=False
        )

        view = MinecraftVersionView(available_versions, has_snapshots)
        await message.edit(embed=embed, view=view)

        await view.wait()
        return view.result

    async def _setup_loaders(self, ctx, available_loaders: List[str], message: discord.Message) -> Optional[
        Dict[str, Any]]:
        """Set up loader monitoring."""
        embed = discord.Embed(
            title="Step 2: Mod Loaders",
            description="Which mod loaders should be monitored?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Available Loaders",
            value=humanize_list([loader.title() for loader in available_loaders]),
            inline=False
        )

        view = LoaderView(available_loaders)
        await message.edit(embed=embed, view=view)

        await view.wait()
        return view.result

    async def _setup_release_channels(self, ctx, message: discord.Message) -> Optional[Dict[str, Any]]:
        """Set up release channel monitoring."""
        embed = discord.Embed(
            title="Step 3: Release Channels",
            description="Which release channels should be monitored?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Available Channels",
            value="• **Release** - Stable releases\n• **Beta** - Beta releases\n• **Alpha** - Alpha releases",
            inline=False
        )

        view = ReleaseChannelView()
        await message.edit(embed=embed, view=view)

        await view.wait()
        return view.result

    async def _setup_discord_channel(self, ctx, message: discord.Message) -> Optional[discord.TextChannel]:
        """Set up Discord notification channel."""
        embed = discord.Embed(
            title="Step 4: Notification Channel",
            description="Select the channel where notifications should be sent:",
            color=discord.Color.blue()
        )

        result = None

        async def channel_callback(interaction: discord.Interaction, channel: discord.TextChannel):
            nonlocal result
            result = channel
            await interaction.response.edit_message(
                content=f"✅ Selected channel: {channel.mention}",
                embed=None,
                view=None
            )

        view = discord.ui.View()
        view.add_item(ChannelSelect(channel_callback))

        await message.edit(embed=embed, view=view)

        # Wait for selection
        for _ in range(120):  # 2 minutes timeout
            if result:
                break
            await asyncio.sleep(1)

        return result

    async def _setup_roles(self, ctx, message: discord.Message) -> Optional[List[discord.Role]]:
        """Set up role pinging."""
        embed = discord.Embed(
            title="Step 5: Role Notifications",
            description="Select roles to ping when updates are found (optional):",
            color=discord.Color.blue()
        )

        result = []

        async def role_callback(interaction: discord.Interaction, roles: List[discord.Role]):
            nonlocal result
            result = roles
            role_mentions = [role.mention for role in roles]
            await interaction.response.edit_message(
                content=f"✅ Selected roles: {humanize_list(role_mentions) if role_mentions else 'None'}",
                embed=None,
                view=None
            )

        view = discord.ui.View()
        view.add_item(RoleSelect(role_callback))

        # Add skip button
        skip_btn = discord.ui.Button(label="Skip (No roles)", style=discord.ButtonStyle.secondary)

        async def skip_callback(interaction: discord.Interaction):
            nonlocal result
            result = []
            await interaction.response.edit_message(
                content="✅ No roles selected",
                embed=None,
                view=None
            )

        skip_btn.callback = skip_callback
        view.add_item(skip_btn)

        await message.edit(embed=embed, view=view)

        # Wait for selection
        for _ in range(120):  # 2 minutes timeout
            if result is not None:
                break
            await asyncio.sleep(1)

        return result

    async def _send_initial_notification(self, ctx, project_id: str, project_data: Dict[str, Any],
                                         message: discord.Message):
        """Send the initial notification and set up the project."""
        try:
            # Get the latest version
            latest_version = await self.update_checker._get_latest_monitored_version(project_id, project_data)

            if not latest_version:
                await message.edit(content="❌ No versions found matching your criteria.", embed=None, view=None)
                return

            # Get project info
            project_info = await self.api.get_project(project_id)

            # Create embed
            embed = await self._create_update_embed(project_info, latest_version)
            embed.title = f"🎯 Now Monitoring: {project_info['title']}"

            # Get the notification channel
            channel = ctx.guild.get_channel(project_data["channel_id"])
            if not channel:
                await message.edit(content="❌ Selected channel not found.", embed=None, view=None)
                return

            # Send to notification channel
            await channel.send(embed=embed)

            # Update the stored version
            await self.update_checker._update_project_version(ctx.guild.id, project_id, latest_version)

            # Send confirmation
            await message.edit(
                content=f"✅ Successfully set up monitoring for **{project_info['title']}**!\n"
                        f"Initial notification sent to {channel.mention}",
                embed=None,
                view=None
            )

        except Exception as e:
            log.error(f"Error sending initial notification: {e}")
            await message.edit(content="❌ Failed to send initial notification.", embed=None, view=None)

    async def _create_update_embed(self, project_info: Dict[str, Any], version: Dict[str, Any]) -> discord.Embed:
        """Create an embed for update notifications."""
        embed = discord.Embed(
            title=f"🔔 {project_info['title']} Update Available",
            description=project_info.get("description", ""),
            color=discord.Color.green(),
            timestamp=datetime.fromisoformat(version["date_published"].replace('Z', '+00:00'))
        )

        # Add version info
        embed.add_field(
            name="Version",
            value=version["version_number"],
            inline=True
        )

        embed.add_field(
            name="Release Type",
            value=version.get("version_type", "release").title(),
            inline=True
        )

        embed.add_field(
            name="Game Versions",
            value=humanize_list(version.get("game_versions", [])),
            inline=True
        )

        embed.add_field(
            name="Loaders",
            value=humanize_list([loader.title() for loader in version.get("loaders", [])]),
            inline=True
        )

        # Add changelog if available
        changelog = version.get("changelog", "")
        if changelog:
            embed.add_field(
                name="Changelog",
                value=truncate_text(changelog, 1000),
                inline=False
            )

        # Add download link
        if "files" in version and version["files"]:
            primary_file = next((f for f in version["files"] if f.get("primary", False)), version["files"][0])
            embed.add_field(
                name="Download",
                value=f"[Download {primary_file['filename']}]({primary_file['url']})",
                inline=False
            )

        # Set thumbnail
        if "icon_url" in project_info and project_info["icon_url"]:
            embed.set_thumbnail(url=project_info["icon_url"])

        # Add footer
        embed.set_footer(text=f"Project ID: {project_info['id']}")

        return embed

    async def _create_project_info_embed(self, project_info: Dict[str, Any]) -> discord.Embed:
        """Create an embed showing project information."""
        embed = discord.Embed(
            title=project_info["title"],
            description=project_info.get("description", ""),
            color=discord.Color.blue(),
            url=f"https://modrinth.com/mod/{project_info['id']}"
        )

        # Add basic info
        embed.add_field(
            name="Project Type",
            value=project_info.get("project_type", "mod").title(),
            inline=True
        )

        embed.add_field(
            name="Downloads",
            value=f"{project_info.get('downloads', 0):,}",
            inline=True
        )

        embed.add_field(
            name="Followers",
            value=f"{project_info.get('followers', 0):,}",
            inline=True
        )

        # Add categories
        if "categories" in project_info:
            embed.add_field(
                name="Categories",
                value=humanize_list(project_info["categories"]),
                inline=False
            )

        # Set thumbnail
        if "icon_url" in project_info and project_info["icon_url"]:
            embed.set_thumbnail(url=project_info["icon_url"])

        # Add footer
        embed.set_footer(text=f"Project ID: {project_info['id']}")

        return embed

    def _is_snapshot(self, version: str) -> bool:
        """Check if a version is a snapshot."""
        from .utils import is_snapshot
        return is_snapshot(version)

    @modrinth.command(name="list")
    async def list_projects(self, ctx):
        """List all monitored projects in this server."""
        projects = await self.config.guild(ctx.guild).projects()

        if not projects:
            await ctx.send("No projects are currently being monitored.")
            return

        embed = discord.Embed(
            title="Monitored Projects",
            color=discord.Color.blue()
        )

        for project_id, project_data in projects.items():
            channel = ctx.guild.get_channel(project_data.get("channel_id"))
            channel_mention = channel.mention if channel else "Unknown"

            embed.add_field(
                name=project_data["name"],
                value=f"**ID:** {project_id}\n**Channel:** {channel_mention}",
                inline=True
            )

        await ctx.send(embed=embed)

    @modrinth.command(name="info")
    async def project_info(self, ctx, project_id: str):
        """Show detailed information about a Modrinth project."""
        if not validate_project_id(project_id):
            await ctx.send("❌ Invalid project ID format.")
            return

        project_info = await self.api.get_project(project_id)
        if not project_info:
            await ctx.send("❌ Project not found.")
            return

        embed = await self._create_project_info_embed(project_info)

        # Add version info
        game_versions = await self.api.get_project_game_versions(project_id)
        loaders = await self.api.get_project_loaders(project_id)

        if game_versions:
            embed.add_field(
                name="Supported Versions",
                value=format_version_list(game_versions),
                inline=False
            )

        if loaders:
            embed.add_field(
                name="Supported Loaders",
                value=humanize_list([loader.title() for loader in loaders]),
                inline=False
            )

        await ctx.send(embed=embed)

    @modrinth.command(name="remove")
    async def remove_project(self, ctx, project_identifier: str):
        """Remove a project from monitoring.

        You can use either the project ID or the project name.
        """
        projects = await self.config.guild(ctx.guild).projects()

        if not projects:
            await ctx.send("No projects are currently being monitored.")
            return

        # Find project by ID or name
        project_id = None
        project_name = None

        if project_identifier in projects:
            project_id = project_identifier
            project_name = projects[project_id]["name"]
        else:
            # Search by name
            matching_projects = []
            for pid, pdata in projects.items():
                if pdata["name"].lower() == project_identifier.lower():
                    matching_projects.append((pid, pdata))

            if len(matching_projects) == 1:
                project_id, project_data = matching_projects[0]
                project_name = project_data["name"]
            elif len(matching_projects) > 1:
                # Multiple matches, ask user to specify
                embed = discord.Embed(
                    title="Multiple Projects Found",
                    description="Multiple projects match that name. Please use the project ID instead:",
                    color=discord.Color.orange()
                )

                for pid, pdata in matching_projects:
                    embed.add_field(
                        name=pdata["name"],
                        value=f"ID: {pid}",
                        inline=True
                    )

                await ctx.send(embed=embed)
                return

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Confirm removal
        embed = discord.Embed(
            title="Remove Project",
            description=f"Are you sure you want to stop monitoring **{project_name}**?",
            color=discord.Color.red()
        )

        view = ConfirmView()
        message = await ctx.send(embed=embed, view=view)

        await view.wait()

        if view.value is None:
            await message.edit(content="⏰ Timed out.", embed=None, view=None)
            return
        elif not view.value:
            await message.edit(content="❌ Cancelled.", embed=None, view=None)
            return

        # Remove project
        async with self.config.guild(ctx.guild).projects() as projects:
            del projects[project_id]

        await message.edit(content=f"✅ Removed **{project_name}** from monitoring.", embed=None, view=None)

    @modrinth.command(name="check")
    async def manual_check(self, ctx, project_identifier: str):
        """Manually check a project for updates."""
        projects = await self.config.guild(ctx.guild).projects()

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            # Search by name
            for pid, pdata in projects.items():
                if pdata["name"].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Perform manual check
        success = await self.update_checker.check_project_manually(ctx.guild, project_id)

        if success:
            await ctx.send(f"✅ Manual check completed for **{projects[project_id]['name']}**.")
        else:
            await ctx.send("❌ Failed to check project.")

    @modrinth.command(name="toggle")
    async def toggle_notifications(self, ctx):
        """Toggle server-wide notifications on/off."""
        current = await self.config.guild(ctx.guild).notifications_enabled()
        new_state = not current

        await self.config.guild(ctx.guild).notifications_enabled.set(new_state)

        status = "enabled" if new_state else "disabled"
        await ctx.send(f"✅ Server notifications {status}.")

    @modrinth.command(name="config")
    async def show_config(self, ctx):
        """Show current server configuration."""
        config = await self.config.guild(ctx.guild).all()

        embed = discord.Embed(
            title="Server Configuration",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Notifications",
            value="✅ Enabled" if config["notifications_enabled"] else "❌ Disabled",
            inline=True
        )

        embed.add_field(
            name="Check Interval",
            value=f"{config['check_interval']} seconds",
            inline=True
        )

        embed.add_field(
            name="Monitored Projects",
            value=len(config["projects"]),
            inline=True
        )

        await ctx.send(embed=embed)

    @modrinth.command(name="channel")
    async def edit_channel(self, ctx, project_identifier: str, channel: discord.TextChannel):
        """Change the notification channel for a project."""
        projects = await self.config.guild(ctx.guild).projects()

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            # Search by name
            for pid, pdata in projects.items():
                if pdata["name"].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Update channel
        async with self.config.guild(ctx.guild).projects() as projects:
            projects[project_id]["channel_id"] = channel.id

        await ctx.send(f"✅ Changed notification channel for **{projects[project_id]['name']}** to {channel.mention}.")

    @modrinth.command(name="addrole")
    async def add_role(self, ctx, project_identifier: str, role: discord.Role):
        """Add a role to ping for a project's notifications."""
        projects = await self.config.guild(ctx.guild).projects()

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            # Search by name
            for pid, pdata in projects.items():
                if pdata["name"].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Add role
        async with self.config.guild(ctx.guild).projects() as projects:
            if role.id not in projects[project_id]["roles"]:
                projects[project_id]["roles"].append(role.id)
                await ctx.send(f"✅ Added {role.mention} to notifications for **{projects[project_id]['name']}**.")
            else:
                await ctx.send(f"❌ {role.mention} is already in the notification list.")

    @modrinth.command(name="removerole")
    async def remove_role(self, ctx, project_identifier: str, role: discord.Role):
        """Remove a role from a project's notifications."""
        projects = await self.config.guild(ctx.guild).projects()

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            # Search by name
            for pid, pdata in projects.items():
                if pdata["name"].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Remove role
        async with self.config.guild(ctx.guild).projects() as projects:
            if role.id in projects[project_id]["roles"]:
                projects[project_id]["roles"].remove(role.id)
                await ctx.send(f"✅ Removed {role.mention} from notifications for **{projects[project_id]['name']}**.")
            else:
                await ctx.send(f"❌ {role.mention} is not in the notification list.")