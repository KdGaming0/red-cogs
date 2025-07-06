import asyncio
import aiohttp
import json
import re
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union, Any
import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu
from redbot.core.utils.predicates import MessagePredicate

log = logging.getLogger("red.modrinth_checker")


class ModrinthChecker(commands.Cog):
    """Monitor Modrinth projects for updates and post notifications to Discord channels."""

    def __init__(self, bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.config = Config.get_conf(self, identifier=1234567890)

        # Default configuration
        default_guild = {
            "projects": {},  # project_id: project_data
            "enabled": True,
            "check_interval": 1800  # 30 minutes
        }

        self.config.register_guild(**default_guild)

        # API constants
        self.api_base = "https://api.modrinth.com/v2"
        self.rate_limit = 300  # requests per minute
        self.user_agent = "RedDiscordBot-ModrinthChecker/1.0.0"

        # Start background task
        self.bg_task = self.bot.loop.create_task(self.background_checker())

    def cog_unload(self):
        if hasattr(self, 'session'):
            asyncio.create_task(self.session.close())
        if hasattr(self, 'bg_task'):
            self.bg_task.cancel()

    def _is_snapshot(self, version: str) -> bool:
        """Check if a Minecraft version is a snapshot."""
        # Snapshot patterns: 25w21a, 1.21-pre1, 1.21-rc1, etc.
        snapshot_patterns = [
            r'^\d+w\d+[a-z]$',  # Weekly snapshots like 25w21a
            r'.*-pre\d+$',  # Pre-releases like 1.21-pre1
            r'.*-rc\d+$',  # Release candidates like 1.21-rc1
            r'.*snapshot.*',  # Any version with "snapshot"
        ]

        for pattern in patterns:
            if re.match(pattern, version, re.IGNORECASE):
                return True
        return False

    def _filter_minecraft_versions(self, versions: List[str], include_snapshots: bool = False) -> List[str]:
        """Filter Minecraft versions to show only releases or include snapshots."""
        if include_snapshots:
            return versions

        # Filter out snapshots
        return [v for v in versions if not self._is_snapshot(v)]

    async def _api_request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make an API request to Modrinth."""
        url = f"{self.api_base}{endpoint}"
        headers = {"User-Agent": self.user_agent}

        try:
            async with self.session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 404:
                    return None
                else:
                    log.error(f"API request failed: {response.status} - {endpoint}")
                    return None
        except Exception as e:
            log.error(f"API request exception: {e}")
            return None

    async def _get_project_info(self, project_id: str) -> Optional[dict]:
        """Get project information from Modrinth API."""
        return await self._api_request(f"/project/{project_id}")

    async def _get_project_versions(self, project_id: str) -> Optional[List[dict]]:
        """Get all versions for a project."""
        return await self._api_request(f"/project/{project_id}/version")

    def _extract_version_number(self, version_string: str) -> Optional[str]:
        """Extract version number from version string using semantic versioning patterns."""
        patterns = [
            r'(\d+\.\d+\.\d+)',  # X.Y.Z
            r'(\d+\.\d+)',  # X.Y
            r'v(\d+\.\d+\.\d+)',  # vX.Y.Z
            r'v(\d+\.\d+)',  # vX.Y
        ]

        for pattern in patterns:
            match = re.search(pattern, version_string)
            if match:
                return match.group(1)

        return None

    def _compare_versions(self, v1: str, v2: str) -> int:
        """Compare two version strings. Returns 1 if v1 > v2, -1 if v1 < v2, 0 if equal."""
        try:
            v1_parts = [int(x) for x in v1.split('.')]
            v2_parts = [int(x) for x in v2.split('.')]

            # Pad shorter version with zeros
            max_len = max(len(v1_parts), len(v2_parts))
            v1_parts.extend([0] * (max_len - len(v1_parts)))
            v2_parts.extend([0] * (max_len - len(v2_parts)))

            for i in range(max_len):
                if v1_parts[i] > v2_parts[i]:
                    return 1
                elif v1_parts[i] < v2_parts[i]:
                    return -1
            return 0
        except ValueError:
            # Fall back to string comparison
            if v1 > v2:
                return 1
            elif v1 < v2:
                return -1
            return 0

    def _filter_versions(self, versions: List[dict], mc_versions: List[str],
                         loaders: List[str], channels: List[str]) -> List[dict]:
        """Filter versions based on criteria."""
        filtered = []

        for version in versions:
            # Check minecraft version
            if mc_versions and mc_versions != "latest_always":
                if not any(mv in version.get('game_versions', []) for mv in mc_versions):
                    continue

            # Check loader
            if loaders and not any(loader in version.get('loaders', []) for loader in loaders):
                continue

            # Check release channel
            if channels and version.get('version_type') not in channels:
                continue

            filtered.append(version)

        return filtered

    async def _create_update_embed(self, project: dict, version: dict) -> discord.Embed:
        """Create an embed for a version update notification."""
        embed = discord.Embed(
            title=f"{project['title']} - New Version Available!",
            description=f"**{version['name']}** has been released",
            color=project.get('color', 0x1bd96a),
            timestamp=datetime.fromisoformat(version['date_published'].replace('Z', '+00:00'))
        )

        # Set project icon
        if project.get('icon_url'):
            embed.set_thumbnail(url=project['icon_url'])

        # Add version info
        embed.add_field(name="Version", value=version['version_number'], inline=True)
        embed.add_field(name="Release Channel", value=version['version_type'].title(), inline=True)
        embed.add_field(name="Downloads", value=f"{version['downloads']:,}", inline=True)

        # Game versions
        if version.get('game_versions'):
            game_versions = ', '.join(version['game_versions'][:5])
            if len(version['game_versions']) > 5:
                game_versions += f" (+{len(version['game_versions']) - 5} more)"
            embed.add_field(name="Game Versions", value=game_versions, inline=True)

        # Loaders
        if version.get('loaders'):
            loaders = ', '.join(version['loaders'])
            embed.add_field(name="Loaders", value=loaders, inline=True)

        # Published date
        embed.add_field(name="Published",
                        value=f"<t:{int(datetime.fromisoformat(version['date_published'].replace('Z', '+00:00')).timestamp())}:R>",
                        inline=True)

        # Changelog
        if version.get('changelog'):
            changelog = version['changelog']
            if len(changelog) > 1024:
                changelog = changelog[:1021] + "..."
            embed.add_field(name="Changelog", value=changelog, inline=False)

        # Download link
        version_url = f"https://modrinth.com/mod/{project['slug']}/version/{version['id']}"
        embed.add_field(name="Download", value=f"[View on Modrinth]({version_url})", inline=False)

        return embed

    @commands.group(name="modrinth", aliases=["mr"])
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def modrinth(self, ctx):
        """Modrinth project update checker commands."""
        pass

    @modrinth.command(name="add")
    async def add_project(self, ctx, project_id: str):
        """Add a Modrinth project to monitor for updates."""

        # Get project info
        async with ctx.typing():
            project = await self._get_project_info(project_id)

        if not project:
            await ctx.send("❌ Project not found. Please check the project ID.")
            return

        # Check if already exists
        guild_config = await self.config.guild(ctx.guild).all()
        if project_id in guild_config['projects']:
            await ctx.send(f"❌ Project **{project['title']}** is already being monitored.")
            return

        # Show project info and confirm
        embed = discord.Embed(
            title=f"Add {project['title']} to monitoring?",
            description=project.get('description', 'No description available'),
            color=project.get('color', 0x1bd96a)
        )

        if project.get('icon_url'):
            embed.set_thumbnail(url=project['icon_url'])

        embed.add_field(name="Project Type", value=project['project_type'].title(), inline=True)
        embed.add_field(name="Downloads", value=f"{project['downloads']:,}", inline=True)
        embed.add_field(name="Followers", value=f"{project['followers']:,}", inline=True)
        embed.add_field(name="Project ID", value=project_id, inline=False)

        view = ConfirmView()
        message = await ctx.send(embed=embed, view=view)
        await view.wait()

        if not view.value:
            embed = discord.Embed(
                title="❌ Setup Cancelled",
                description="Project addition has been cancelled.",
                color=0xff0000
            )
            await message.edit(embed=embed, view=None)
            return

        # Start setup process
        await self._setup_project_monitoring(ctx, message, project)

    async def _setup_project_monitoring(self, ctx, message, project):
        """Setup monitoring configuration for a project."""
        project_id = project['id']

        try:
            # Get project versions to determine available options
            versions = await self._get_project_versions(project_id)
            if not versions:
                embed = discord.Embed(
                    title="❌ Setup Failed",
                    description="Could not retrieve project versions.",
                    color=0xff0000
                )
                await message.edit(embed=embed, view=None)
                return

            # Extract available minecraft versions and loaders
            all_mc_versions = set()
            all_loaders = set()

            for version in versions:
                all_mc_versions.update(version.get('game_versions', []))
                all_loaders.update(version.get('loaders', []))

            all_mc_versions = sorted(list(all_mc_versions), reverse=True)
            all_loaders = sorted(list(all_loaders))

            # Step 1: Minecraft versions
            mc_config = await self._setup_minecraft_versions(ctx, message, all_mc_versions)
            if not mc_config:
                return

            # Step 2: Loaders
            loader_config = await self._setup_loaders(ctx, message, all_loaders)
            if not loader_config:
                return

            # Step 3: Release channels
            channel_config = await self._setup_release_channels(ctx, message)
            if not channel_config:
                return

            # Step 4: Discord channel
            discord_channel = await self._setup_discord_channel(ctx, message)
            if not discord_channel:
                return

            # Step 5: Roles
            roles = await self._setup_roles(ctx, message)
            if roles is None:
                return

            # Save configuration
            project_config = {
                'name': project['title'],
                'slug': project['slug'],
                'mc_versions': mc_config,
                'loaders': loader_config,
                'channels': channel_config,
                'discord_channel': discord_channel.id,
                'roles': [role.id for role in roles],
                'last_version': None,
                'last_check': None
            }

            async with self.config.guild(ctx.guild).projects() as projects:
                projects[project_id] = project_config

            # Send initial notification
            await self._send_initial_notification(ctx, project, project_config, versions)

            embed = discord.Embed(
                title="✅ Setup Complete",
                description=f"Successfully added **{project['title']}** to monitoring!",
                color=0x00ff00
            )
            await message.edit(embed=embed, view=None)

        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="⏰ Setup Timed Out",
                description="Setup process timed out. Please try again.",
                color=0xff0000
            )
            await message.edit(embed=embed, view=None)
        except Exception as e:
            log.error(f"Error setting up project monitoring: {e}")
            embed = discord.Embed(
                title="❌ Setup Error",
                description="An error occurred during setup. Please try again.",
                color=0xff0000
            )
            await message.edit(embed=embed, view=None)

    async def _setup_minecraft_versions(self, ctx, message, available_versions):
        """Setup minecraft version monitoring."""
        # Filter versions to show releases by default
        release_versions = self._filter_minecraft_versions(available_versions, include_snapshots=False)
        has_snapshots = len(release_versions) < len(available_versions)

        embed = discord.Embed(
            title="Minecraft Version Configuration",
            description="Which Minecraft versions should be monitored?",
            color=0x1bd96a
        )

        # Add explanation
        embed.add_field(
            name="Options Explained:",
            value=(
                "🟢 **All supported versions** - Monitor all current and future versions\n"
                "🔹 **Specific versions** - Select individual versions to monitor\n"
                "📋 **Latest current version** - Monitor only the current latest version\n"
                "🔄 **Latest version always** - Auto-update to newest supported version"
            ),
            inline=False
        )

        # Show available versions (releases only by default)
        version_display = ", ".join(release_versions[:15])
        if len(release_versions) > 15:
            version_display += f" (+{len(release_versions) - 15} more)"

        embed.add_field(
            name="Available Versions (Releases)",
            value=version_display,
            inline=False
        )

        if has_snapshots:
            embed.add_field(
                name="Note",
                value="📸 Click 'Show Snapshots' to see experimental versions",
                inline=False
            )

        view = MinecraftVersionView(available_versions, release_versions, has_snapshots)
        await message.edit(embed=embed, view=view)
        await view.wait()

        return view.result

    async def _setup_loaders(self, ctx, message, available_loaders):
        """Setup loader monitoring."""
        embed = discord.Embed(
            title="Loader Configuration",
            description="Which mod loaders should be monitored?",
            color=0x1bd96a
        )

        embed.add_field(
            name="Options Explained:",
            value=(
                "🟢 **All supported loaders** - Monitor all loaders this project supports\n"
                "🔹 **Individual loaders** - Select specific loaders to monitor\n"
                "Click individual loader buttons to select/deselect them, then click Continue."
            ),
            inline=False
        )

        embed.add_field(
            name="Available Loaders",
            value=", ".join(available_loaders),
            inline=False
        )

        view = LoaderView(available_loaders)
        await message.edit(embed=embed, view=view)
        await view.wait()

        return view.result

    async def _setup_release_channels(self, ctx, message):
        """Setup release channel monitoring."""
        embed = discord.Embed(
            title="Release Channel Configuration",
            description="Which release channels should be monitored?",
            color=0x1bd96a
        )

        embed.add_field(
            name="Options Explained:",
            value=(
                "🟢 **All Channels** - Monitor all release types\n"
                "🔴 **Release** - Stable releases only\n"
                "🟡 **Beta** - Beta/testing versions\n"
                "🟠 **Alpha** - Early development versions\n"
                "Click individual channel buttons to select/deselect them, then click Continue."
            ),
            inline=False
        )

        view = ReleaseChannelView()
        await message.edit(embed=embed, view=view)
        await view.wait()

        return view.result

    async def _setup_discord_channel(self, ctx, message):
        """Setup Discord channel for notifications."""
        embed = discord.Embed(
            title="Discord Channel Configuration",
            description="Please mention the channel where notifications should be sent.",
            color=0x1bd96a
        )

        embed.add_field(
            name="How to specify:",
            value="• Mention a channel: #updates\n• Use channel ID: 123456789012345678",
            inline=False
        )

        await message.edit(embed=embed, view=None)

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            response = await self.bot.wait_for('message', check=check, timeout=120)

            # Try to parse channel mention or ID
            if response.channel_mentions:
                return response.channel_mentions[0]

            # Try to get channel by ID
            try:
                channel_id = int(response.content.strip('<>#'))
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    return channel
            except ValueError:
                pass

            embed = discord.Embed(
                title="❌ Invalid Channel",
                description="Please mention a valid channel or provide a channel ID.",
                color=0xff0000
            )
            await ctx.send(embed=embed)
            return None

        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="⏰ Setup Timed Out",
                description="Timed out waiting for channel input.",
                color=0xff0000
            )
            await message.edit(embed=embed, view=None)
            return None

    async def _setup_roles(self, ctx, message):
        """Setup roles to ping."""
        embed = discord.Embed(
            title="Role Configuration",
            description="Please mention the roles to ping for notifications, or type 'none' for no pings.",
            color=0x1bd96a
        )

        embed.add_field(
            name="How to specify:",
            value="• Mention roles: @testers @mods\n• Type 'none' for no role pings",
            inline=False
        )

        await message.edit(embed=embed, view=None)

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            response = await self.bot.wait_for('message', check=check, timeout=120)

            if response.content.lower() == 'none':
                return []

            return response.role_mentions

        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="⏰ Setup Timed Out",
                description="Timed out waiting for role input.",
                color=0xff0000
            )
            await message.edit(embed=embed, view=None)
            return None

    async def _send_initial_notification(self, ctx, project, config, versions):
        """Send the initial notification for a newly added project."""
        # Filter versions based on config
        filtered_versions = self._filter_versions(
            versions,
            config['mc_versions'],
            config['loaders'],
            config['channels']
        )

        if not filtered_versions:
            return

        # Get the latest version
        latest_version = max(filtered_versions, key=lambda v: v['date_published'])

        # Update last version
        version_number = self._extract_version_number(latest_version['version_number'])
        config['last_version'] = version_number or latest_version['version_number']
        config['last_check'] = datetime.now(timezone.utc).isoformat()

        # Send notification
        channel = ctx.guild.get_channel(config['discord_channel'])
        if channel:
            embed = await self._create_update_embed(project, latest_version)

            content = ""
            if config['roles']:
                role_mentions = [f"<@&{role_id}>" for role_id in config['roles']]
                content = " ".join(role_mentions)

            await channel.send(content=content, embed=embed)

    @modrinth.command(name="list")
    async def list_projects(self, ctx):
        """List all monitored projects."""
        guild_config = await self.config.guild(ctx.guild).all()
        projects = guild_config['projects']

        if not projects:
            await ctx.send("No projects are currently being monitored.")
            return

        embed = discord.Embed(
            title="Monitored Projects",
            color=0x1bd96a
        )

        for project_id, config in projects.items():
            channel = ctx.guild.get_channel(config['discord_channel'])
            channel_name = channel.mention if channel else "Unknown Channel"

            embed.add_field(
                name=config['name'],
                value=f"ID: `{project_id}`\nChannel: {channel_name}",
                inline=True
            )

        await ctx.send(embed=embed)

    @modrinth.command(name="info")
    async def project_info(self, ctx, project_id: str):
        """Show detailed information about a Modrinth project."""
        async with ctx.typing():
            project = await self._get_project_info(project_id)

        if not project:
            await ctx.send("❌ Project not found.")
            return

        embed = discord.Embed(
            title=project['title'],
            description=project.get('description', 'No description available'),
            color=project.get('color', 0x1bd96a),
            url=f"https://modrinth.com/{project['project_type']}/{project['slug']}"
        )

        if project.get('icon_url'):
            embed.set_thumbnail(url=project['icon_url'])

        embed.add_field(name="Project Type", value=project['project_type'].title(), inline=True)
        embed.add_field(name="Downloads", value=f"{project['downloads']:,}", inline=True)
        embed.add_field(name="Followers", value=f"{project['followers']:,}", inline=True)

        if project.get('categories'):
            embed.add_field(name="Categories", value=", ".join(project['categories']), inline=False)

        if project.get('game_versions'):
            versions = ', '.join(project['game_versions'][:10])
            if len(project['game_versions']) > 10:
                versions += f" (+{len(project['game_versions']) - 10} more)"
            embed.add_field(name="Game Versions", value=versions, inline=False)

        if project.get('loaders'):
            embed.add_field(name="Loaders", value=", ".join(project['loaders']), inline=False)

        embed.add_field(name="Project ID", value=project_id, inline=False)

        await ctx.send(embed=embed)

    @modrinth.command(name="remove")
    async def remove_project(self, ctx, *, project_identifier: str):
        """Remove a project from monitoring."""
        guild_config = await self.config.guild(ctx.guild).all()
        projects = guild_config['projects']

        # Find project by ID or name
        project_id = None
        project_name = None

        # Check if it's a direct ID match
        if project_identifier in projects:
            project_id = project_identifier
            project_name = projects[project_identifier]['name']
        else:
            # Search by name
            matches = []
            for pid, config in projects.items():
                if config['name'].lower() == project_identifier.lower():
                    matches.append((pid, config['name']))

            if len(matches) == 1:
                project_id, project_name = matches[0]
            elif len(matches) > 1:
                embed = discord.Embed(
                    title="Multiple Projects Found",
                    description="Please specify which project to remove:",
                    color=0xff0000
                )

                for pid, name in matches:
                    embed.add_field(name=name, value=f"ID: `{pid}`", inline=False)

                await ctx.send(embed=embed)
                return

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Confirm removal
        embed = discord.Embed(
            title="Confirm Removal",
            description=f"Are you sure you want to stop monitoring **{project_name}**?",
            color=0xff0000
        )

        view = ConfirmView()
        message = await ctx.send(embed=embed, view=view)
        await view.wait()

        if view.value:
            async with self.config.guild(ctx.guild).projects() as projects:
                del projects[project_id]

            embed = discord.Embed(
                title="✅ Project Removed",
                description=f"Removed **{project_name}** from monitoring.",
                color=0x00ff00
            )
            await message.edit(embed=embed, view=None)
        else:
            embed = discord.Embed(
                title="❌ Removal Cancelled",
                description="Project removal has been cancelled.",
                color=0xff0000
            )
            await message.edit(embed=embed, view=None)

    @modrinth.command(name="check")
    async def manual_check(self, ctx, *, project_identifier: str):
        """Manually check for updates on a specific project."""
        guild_config = await self.config.guild(ctx.guild).all()
        projects = guild_config['projects']

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            for pid, config in projects.items():
                if config['name'].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        async with ctx.typing():
            await self._check_project_updates(ctx.guild, project_id, manual=True)

        await ctx.send(f"✅ Manual check completed for **{projects[project_id]['name']}**.")

    @modrinth.command(name="toggle")
    async def toggle_notifications(self, ctx):
        """Enable or disable notifications for this server."""
        current = await self.config.guild(ctx.guild).enabled()
        new_state = not current

        await self.config.guild(ctx.guild).enabled.set(new_state)

        state_text = "enabled" if new_state else "disabled"
        await ctx.send(f"✅ Notifications have been **{state_text}** for this server.")

    @modrinth.command(name="config")
    async def show_config(self, ctx):
        """Show current server configuration."""
        guild_config = await self.config.guild(ctx.guild).all()

        embed = discord.Embed(
            title="Server Configuration",
            color=0x1bd96a
        )

        embed.add_field(
            name="Status",
            value="✅ Enabled" if guild_config['enabled'] else "❌ Disabled",
            inline=True
        )

        embed.add_field(
            name="Check Interval",
            value=f"{guild_config['check_interval'] // 60} minutes",
            inline=True
        )

        embed.add_field(
            name="Monitored Projects",
            value=str(len(guild_config['projects'])),
            inline=True
        )

        await ctx.send(embed=embed)

    @modrinth.command(name="editchannel")
    async def edit_channel(self, ctx, project_identifier: str, channel: discord.TextChannel):
        """Change the notification channel for a project."""
        guild_config = await self.config.guild(ctx.guild).all()
        projects = guild_config['projects']

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            for pid, config in projects.items():
                if config['name'].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Update channel
        async with self.config.guild(ctx.guild).projects() as projects_config:
            projects_config[project_id]['discord_channel'] = channel.id

        project_name = projects[project_id]['name']
        await ctx.send(f"✅ Updated notification channel for **{project_name}** to {channel.mention}.")

    @modrinth.command(name="addrole")
    async def add_role(self, ctx, project_identifier: str, role: discord.Role):
        """Add a role to ping for a project's notifications."""
        guild_config = await self.config.guild(ctx.guild).all()
        projects = guild_config['projects']

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            for pid, config in projects.items():
                if config['name'].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Add role
        async with self.config.guild(ctx.guild).projects() as projects_config:
            if role.id not in projects_config[project_id]['roles']:
                projects_config[project_id]['roles'].append(role.id)

        project_name = projects[project_id]['name']
        await ctx.send(f"✅ Added role {role.mention} to **{project_name}** notifications.")

    @modrinth.command(name="removerole")
    async def remove_role(self, ctx, project_identifier: str, role: discord.Role):
        """Remove a role from a project's notifications."""
        guild_config = await self.config.guild(ctx.guild).all()
        projects = guild_config['projects']

        # Find project
        project_id = None
        if project_identifier in projects:
            project_id = project_identifier
        else:
            for pid, config in projects.items():
                if config['name'].lower() == project_identifier.lower():
                    project_id = pid
                    break

        if not project_id:
            await ctx.send("❌ Project not found.")
            return

        # Remove role
        async with self.config.guild(ctx.guild).projects() as projects_config:
            if role.id in projects_config[project_id]['roles']:
                projects_config[project_id]['roles'].remove(role.id)

        project_name = projects[project_id]['name']
        await ctx.send(f"✅ Removed role {role.mention} from **{project_name}** notifications.")

    async def background_checker(self):
        """Background task to check for updates."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                for guild in self.bot.guilds:
                    guild_config = await self.config.guild(guild).all()

                    if not guild_config['enabled']:
                        continue

                    for project_id in guild_config['projects']:
                        await self._check_project_updates(guild, project_id)
                        await asyncio.sleep(2)  # Rate limiting

                # Wait for next check cycle
                await asyncio.sleep(1800)  # 30 minutes

            except Exception as e:
                log.error(f"Error in background checker: {e}")
                await asyncio.sleep(60)  # Wait a minute before retrying

    async def _check_project_updates(self, guild, project_id, manual=False):
        """Check for updates on a specific project."""
        try:
            guild_config = await self.config.guild(guild).all()
            project_config = guild_config['projects'][project_id]

            # Get project and versions
            project = await self._get_project_info(project_id)
            if not project:
                return

            versions = await self._get_project_versions(project_id)
            if not versions:
                return

            # Filter versions
            filtered_versions = self._filter_versions(
                versions,
                project_config['mc_versions'],
                project_config['loaders'],
                project_config['channels']
            )

            if not filtered_versions:
                return

            # Get latest version
            latest_version = max(filtered_versions, key=lambda v: v['date_published'])
            current_version = self._extract_version_number(latest_version['version_number'])

            # Compare with stored version
            last_version = project_config.get('last_version')

            is_newer = False
            if not last_version:
                is_newer = True
            elif current_version and last_version:
                if self._extract_version_number(last_version):
                    is_newer = self._compare_versions(current_version, self._extract_version_number(last_version)) > 0
                else:
                    is_newer = current_version != last_version
            else:
                is_newer = latest_version['version_number'] != last_version

            # Also check by publication date
            last_check = project_config.get('last_check')
            if last_check:
                last_check_dt = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                version_dt = datetime.fromisoformat(latest_version['date_published'].replace('Z', '+00:00'))
                is_newer = is_newer or version_dt > last_check_dt

            if is_newer or manual:
                # Send notification
                channel = guild.get_channel(project_config['discord_channel'])
                if channel:
                    embed = await self._create_update_embed(project, latest_version)

                    content = ""
                    if project_config['roles'] and not manual:
                        role_mentions = [f"<@&{role_id}>" for role_id in project_config['roles']]
                        content = " ".join(role_mentions)

                    if manual:
                        embed.set_footer(text="Manual check")

                    await channel.send(content=content, embed=embed)

                # Update stored version
                if not manual:
                    async with self.config.guild(guild).projects() as projects:
                        projects[project_id]['last_version'] = current_version or latest_version['version_number']
                        projects[project_id]['last_check'] = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            log.error(f"Error checking project {project_id}: {e}")


# Discord UI Components
class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.value = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        # Disable all buttons when timeout occurs
        for item in self.children:
            item.disabled = True


class MinecraftVersionView(discord.ui.View):
    def __init__(self, all_versions, release_versions, has_snapshots):
        super().__init__(timeout=120)
        self.all_versions = all_versions
        self.release_versions = release_versions
        self.has_snapshots = has_snapshots
        self.result = None
        self.showing_snapshots = False
        self.selected_versions = []
        self.specific_mode = False

    @discord.ui.button(label="1️⃣ All supported versions", style=discord.ButtonStyle.primary)
    async def all_versions_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = self.all_versions
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="2️⃣ Specific versions", style=discord.ButtonStyle.secondary)
    async def specific_versions(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.specific_mode = True

        # Create dropdown for version selection
        embed = discord.Embed(
            title="Select Specific Minecraft Versions",
            description="Select up to 25 versions to monitor:",
            color=0x1bd96a
        )

        view = VersionSelectView(self.release_versions if not self.showing_snapshots else self.all_versions,
                                 self.has_snapshots)
        await interaction.response.edit_message(embed=embed, view=view)
        await view.wait()

        self.result = view.selected_versions
        self.stop()

    @discord.ui.button(label="3️⃣ Latest current version", style=discord.ButtonStyle.secondary)
    async def latest_current(self, interaction: discord.Interaction, button: discord.ui.Button):
        versions_to_use = self.release_versions if not self.showing_snapshots else self.all_versions
        self.result = [versions_to_use[0]] if versions_to_use else []
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="4️⃣ Latest version always", style=discord.ButtonStyle.secondary)
    async def latest_always(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = "latest_always"
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="📸 Show Snapshots", style=discord.ButtonStyle.gray, row=1)
    async def toggle_snapshots(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.has_snapshots:
            await interaction.response.defer()
            return

        self.showing_snapshots = not self.showing_snapshots

        # Update embed
        embed = discord.Embed(
            title="Minecraft Version Configuration",
            description="Which Minecraft versions should be monitored?",
            color=0x1bd96a
        )

        embed.add_field(
            name="Options Explained:",
            value=(
                "🟢 **All supported versions** - Monitor all current and future versions\n"
                "🔹 **Specific versions** - Select individual versions to monitor\n"
                "📋 **Latest current version** - Monitor only the current latest version\n"
                "🔄 **Latest version always** - Auto-update to newest supported version"
            ),
            inline=False
        )

        # Show appropriate versions
        versions_to_show = self.all_versions if self.showing_snapshots else self.release_versions
        version_display = ", ".join(versions_to_show[:15])
        if len(versions_to_show) > 15:
            version_display += f" (+{len(versions_to_show) - 15} more)"

        version_type = "All Versions" if self.showing_snapshots else "Releases"
        embed.add_field(
            name=f"Available Versions ({version_type})",
            value=version_display,
            inline=False
        )

        # Update button label
        button.label = "📋 Show Releases" if self.showing_snapshots else "📸 Show Snapshots"

        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class VersionSelectView(discord.ui.View):
    def __init__(self, versions, has_snapshots):
        super().__init__(timeout=120)
        self.versions = versions
        self.has_snapshots = has_snapshots
        self.selected_versions = []
        self.showing_snapshots = False

        # Add version dropdown
        if len(versions) > 0:
            self.add_item(VersionSelect(versions[:25]))  # Discord limit of 25 options

        # Add continue button
        continue_btn = discord.ui.Button(label="Continue", style=discord.ButtonStyle.green, row=2)
        continue_btn.callback = self.continue_callback
        self.add_item(continue_btn)

        # Add snapshot toggle if needed
        if has_snapshots:
            snapshot_btn = discord.ui.Button(label="📸 Toggle Snapshots", style=discord.ButtonStyle.gray, row=2)
            snapshot_btn.callback = self.toggle_snapshots
            self.add_item(snapshot_btn)

    async def continue_callback(self, interaction: discord.Interaction):
        if not self.selected_versions:
            await interaction.response.send_message("❌ Please select at least one version.", ephemeral=True)
            return
        self.stop()
        await interaction.response.defer()

    async def toggle_snapshots(self, interaction: discord.Interaction):
        # This would need to be implemented to switch between release and snapshot versions
        await interaction.response.send_message("Snapshot toggle not yet implemented.", ephemeral=True)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class VersionSelect(discord.ui.Select):
    def __init__(self, versions):
        options = [
            discord.SelectOption(
                label=version,
                value=version,
                description=f"Minecraft {version}"
            )
            for version in versions
        ]

        super().__init__(
            placeholder="Select Minecraft versions...",
            min_values=1,
            max_values=min(len(options), 25),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_versions = self.values

        embed = discord.Embed(
            title="Selected Versions",
            description=f"Selected: {', '.join(self.values)}",
            color=0x1bd96a
        )

        await interaction.response.edit_message(embed=embed, view=self.view)


class LoaderView(discord.ui.View):
    def __init__(self, available_loaders):
        super().__init__(timeout=120)
        self.available_loaders = available_loaders
        self.result = None
        self.selected_loaders = []

        # Add "All supported loaders" button
        all_btn = discord.ui.Button(label="All supported loaders", style=discord.ButtonStyle.primary)
        all_btn.callback = self.all_loaders_callback
        self.add_item(all_btn)

        # Add individual loader buttons
        for loader in available_loaders:
            btn = discord.ui.Button(label=loader.title(), style=discord.ButtonStyle.secondary)
            btn.callback = self.create_loader_callback(loader)
            self.add_item(btn)

        # Add continue button
        continue_btn = discord.ui.Button(label="Continue", style=discord.ButtonStyle.green, row=2)
        continue_btn.callback = self.continue_callback
        self.add_item(continue_btn)

    async def all_loaders_callback(self, interaction: discord.Interaction):
        self.result = self.available_loaders
        self.stop()
        await interaction.response.defer()

    def create_loader_callback(self, loader):
        async def loader_callback(interaction: discord.Interaction):
            if loader in self.selected_loaders:
                self.selected_loaders.remove(loader)
                # Change button style back to secondary
                for item in self.children:
                    if hasattr(item, 'label') and item.label == loader.title():
                        item.style = discord.ButtonStyle.secondary
                        break
            else:
                self.selected_loaders.append(loader)
                # Change button style to success
                for item in self.children:
                    if hasattr(item, 'label') and item.label == loader.title():
                        item.style = discord.ButtonStyle.success
                        break

            # Update embed to show selected loaders
            embed = discord.Embed(
                title="Loader Configuration",
                description="Which mod loaders should be monitored?",
                color=0x1bd96a
            )

            embed.add_field(
                name="Options Explained:",
                value=(
                    "🟢 **All supported loaders** - Monitor all loaders this project supports\n"
                    "🔹 **Individual loaders** - Select specific loaders to monitor\n"
                    "Click individual loader buttons to select/deselect them, then click Continue."
                ),
                inline=False
            )

            embed.add_field(
                name="Available Loaders",
                value=", ".join(self.available_loaders),
                inline=False
            )

            if self.selected_loaders:
                embed.add_field(
                    name="Selected Loaders",
                    value=", ".join(self.selected_loaders),
                    inline=False
                )

            await interaction.response.edit_message(embed=embed, view=self)

        return loader_callback

    async def continue_callback(self, interaction: discord.Interaction):
        if not self.selected_loaders:
            await interaction.response.send_message("❌ Please select at least one loader.", ephemeral=True)
            return

        self.result = self.selected_loaders
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ReleaseChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.result = None
        self.selected_channels = []

        # Add "All Channels" button
        all_btn = discord.ui.Button(label="All Channels", style=discord.ButtonStyle.primary)
        all_btn.callback = self.all_channels_callback
        self.add_item(all_btn)

        # Add individual channel buttons
        channels = [("Release", "release"), ("Beta", "beta"), ("Alpha", "alpha")]
        for display_name, channel_type in channels:
            btn = discord.ui.Button(label=display_name, style=discord.ButtonStyle.secondary)
            btn.callback = self.create_channel_callback(channel_type, display_name)
            self.add_item(btn)

        # Add continue button
        continue_btn = discord.ui.Button(label="Continue", style=discord.ButtonStyle.green, row=1)
        continue_btn.callback = self.continue_callback
        self.add_item(continue_btn)

    async def all_channels_callback(self, interaction: discord.Interaction):
        self.result = ['release', 'beta', 'alpha']
        self.stop()
        await interaction.response.defer()

    def create_channel_callback(self, channel_type, display_name):
        async def channel_callback(interaction: discord.Interaction):
            if channel_type in self.selected_channels:
                self.selected_channels.remove(channel_type)
                # Change button style back to secondary
                for item in self.children:
                    if hasattr(item, 'label') and item.label == display_name:
                        item.style = discord.ButtonStyle.secondary
                        break
            else:
                self.selected_channels.append(channel_type)
                # Change button style to success
                for item in self.children:
                    if hasattr(item, 'label') and item.label == display_name:
                        item.style = discord.ButtonStyle.success
                        break

            # Update embed to show selected channels
            embed = discord.Embed(
                title="Release Channel Configuration",
                description="Which release channels should be monitored?",
                color=0x1bd96a
            )

            embed.add_field(
                name="Options Explained:",
                value=(
                    "🟢 **All Channels** - Monitor all release types\n"
                    "🔴 **Release** - Stable releases only\n"
                    "🟡 **Beta** - Beta/testing versions\n"
                    "🟠 **Alpha** - Early development versions\n"
                    "Click individual channel buttons to select/deselect them, then click Continue."
                ),
                inline=False
            )

            if self.selected_channels:
                embed.add_field(
                    name="Selected Channels",
                    value=", ".join([c.title() for c in self.selected_channels]),
                    inline=False
                )

            await interaction.response.edit_message(embed=embed, view=self)

        return channel_callback

    async def continue_callback(self, interaction: discord.Interaction):
        if not self.selected_channels:
            await interaction.response.send_message("❌ Please select at least one release channel.", ephemeral=True)
            return

        self.result = self.selected_channels
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True