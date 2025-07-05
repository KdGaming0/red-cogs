import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any
import discord
from redbot.core import commands, Config
from redbot.core.bot import Red

from .api import ModrinthAPI, ModrinthAPIError
from .models import ProjectInfo, VersionInfo, GuildConfig, UserConfig, MonitoredProject

log = logging.getLogger("red.modrinth_notifier")

class ModrinthNotifier(commands.Cog):
    """Monitor Modrinth projects for updates with enhanced features."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.api = ModrinthAPI()
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)

        # Guild configuration
        default_guild = {
            "projects": {},
            "channel_id": None,
            "enabled": True,
            "poll_interval": 300
        }

        # User configuration for personal watchlists
        default_user = {
            "projects": {},
            "enabled": True
        }

        self.config.register_guild(**default_guild)
        self.config.register_user(**default_user)

        self._poll_task: Optional[asyncio.Task] = None
        self._guild_configs: Dict[int, GuildConfig] = {}
        self._user_configs: Dict[int, UserConfig] = {}

        # Interactive session storage
        self._interactive_sessions: Dict[int, Dict] = {}

    async def cog_load(self):
        """Initialize the cog."""
        await self.api.start_session()
        await self._load_configs()
        self._start_polling()

    async def cog_unload(self):
        """Clean up when the cog is unloaded."""
        if self._poll_task:
            self._poll_task.cancel()
        await self.api.close_session()

    def _start_polling(self):
        """Start the polling task."""
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _load_configs(self):
        """Load all configurations from storage."""
        try:
            # Load guild configs
            all_guilds = await self.config.all_guilds()
            for guild_id, data in all_guilds.items():
                self._guild_configs[guild_id] = GuildConfig.from_dict(data)

            # Load user configs
            all_users = await self.config.all_users()
            for user_id, data in all_users.items():
                self._user_configs[user_id] = UserConfig.from_dict(data)

            log.info(f"Loaded {len(self._guild_configs)} guild configs and {len(self._user_configs)} user configs")
        except Exception as e:
            log.error(f"Error loading configs: {e}")

    async def _save_guild_config(self, guild_id: int):
        """Save guild configuration to storage."""
        config = self._guild_configs.get(guild_id, GuildConfig())
        await self.config.guild_from_id(guild_id).set(config.to_dict())

    async def _save_user_config(self, user_id: int):
        """Save user configuration to storage."""
        config = self._user_configs.get(user_id, UserConfig())
        await self.config.user_from_id(user_id).set(config.to_dict())

    def _get_guild_config(self, guild_id: int) -> GuildConfig:
        """Get or create guild configuration."""
        if guild_id not in self._guild_configs:
            self._guild_configs[guild_id] = GuildConfig()
        return self._guild_configs[guild_id]

    def _get_user_config(self, user_id: int) -> UserConfig:
        """Get or create user configuration."""
        if user_id not in self._user_configs:
            self._user_configs[user_id] = UserConfig()
        return self._user_configs[user_id]

    async def _poll_loop(self):
        """Main polling loop for checking updates."""
        await self.bot.wait_until_red_ready()

        while True:
            try:
                await self._check_all_updates()
                await asyncio.sleep(300)  # Poll every 5 minutes
            except Exception as e:
                log.error(f"Error in polling loop: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait 1 minute on error

    async def _check_all_updates(self):
        """Check for updates across all configured projects."""
        try:
            # Check guild projects
            for guild_id, config in self._guild_configs.items():
                if not config.enabled:
                    continue

                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue

                for project_id, monitored_project in config.projects.items():
                    try:
                        await self._check_project_update(guild, monitored_project)
                    except Exception as e:
                        log.error(f"Error checking project {project_id} for guild {guild_id}: {e}")

            # Check user projects
            for user_id, config in self._user_configs.items():
                if not config.enabled:
                    continue

                user = self.bot.get_user(user_id)
                if not user:
                    continue

                for project_id, monitored_project in config.projects.items():
                    try:
                        await self._check_user_project_update(user, monitored_project)
                    except Exception as e:
                        log.error(f"Error checking project {project_id} for user {user_id}: {e}")

        except Exception as e:
            log.error(f"Error in _check_all_updates: {e}", exc_info=True)

    async def _check_project_update(self, guild: discord.Guild, monitored_project: MonitoredProject):
        """Check for updates for a specific guild project."""
        try:
            versions = await self.api.get_project_versions(
                monitored_project.project_id,
                loaders=monitored_project.loaders,
                game_versions=monitored_project.game_versions
            )

            if not versions:
                return

            # Filter by release channel
            filtered_versions = [v for v in versions if v.version_type in monitored_project.release_channels]
            if not filtered_versions:
                return

            latest_version = filtered_versions[0]

            if latest_version.id != monitored_project.last_version:
                # New version found!
                await self._send_guild_update_notification(guild, monitored_project, latest_version)
                monitored_project.last_version = latest_version.id
                await self._save_guild_config(guild.id)

        except ModrinthAPIError as e:
            log.error(f"API error checking project {monitored_project.project_id}: {e}")

    async def _check_user_project_update(self, user: discord.User, monitored_project: MonitoredProject):
        """Check for updates for a specific user project."""
        try:
            versions = await self.api.get_project_versions(
                monitored_project.project_id,
                loaders=monitored_project.loaders,
                game_versions=monitored_project.game_versions
            )

            if not versions:
                return

            # Filter by release channel
            filtered_versions = [v for v in versions if v.version_type in monitored_project.release_channels]
            if not filtered_versions:
                return

            latest_version = filtered_versions[0]

            if latest_version.id != monitored_project.last_version:
                # New version found!
                await self._send_user_update_notification(user, monitored_project, latest_version)
                monitored_project.last_version = latest_version.id
                await self._save_user_config(user.id)

        except ModrinthAPIError as e:
            log.error(f"API error checking project {monitored_project.project_id}: {e}")

    async def _send_guild_update_notification(self, guild: discord.Guild, monitored_project: MonitoredProject, version: VersionInfo):
        """Send update notification to guild channel."""
        guild_config = self._get_guild_config(guild.id)

        if not guild_config.channel_id:
            return

        channel = guild.get_channel(guild_config.channel_id)
        if not channel:
            return

        try:
            # Get project info for the embed
            project_info = await self.api.get_project(monitored_project.project_id)

            embed = discord.Embed(
                title=f"🔄 {project_info.name} Updated!",
                description=f"**Version:** {version.version_number}\n**Type:** {version.version_type.title()}",
                color=discord.Color.green(),
                timestamp=version.date_published,
                url=f"https://modrinth.com/mod/{project_info.slug}"
            )

            if version.changelog:
                changelog = version.changelog[:1000] + "..." if len(version.changelog) > 1000 else version.changelog
                embed.add_field(name="📝 Changelog", value=changelog, inline=False)

            embed.add_field(name="🎮 Game Versions", value=", ".join(version.game_versions[:5]), inline=True)
            embed.add_field(name="⚙️ Loaders", value=", ".join(version.loaders), inline=True)
            embed.add_field(name="💾 Downloads", value=str(version.downloads), inline=True)

            if project_info.icon_url:
                embed.set_thumbnail(url=project_info.icon_url)

            embed.set_footer(text="Modrinth Update Notifier")

            # Prepare role mentions
            content = None
            if monitored_project.role_ids:
                valid_roles = []
                for role_id in monitored_project.role_ids:
                    role = guild.get_role(role_id)
                    if role:
                        valid_roles.append(role.mention)

                if valid_roles:
                    content = " ".join(valid_roles)

            await channel.send(content=content, embed=embed)

        except Exception as e:
            log.error(f"Error sending guild notification: {e}")

    async def _send_user_update_notification(self, user: discord.User, monitored_project: MonitoredProject, version: VersionInfo):
        """Send update notification to user DM."""
        try:
            # Get project info for the embed
            project_info = await self.api.get_project(monitored_project.project_id)

            embed = discord.Embed(
                title=f"🔄 {project_info.name} Updated!",
                description=f"**Version:** {version.version_number}\n**Type:** {version.version_type.title()}",
                color=discord.Color.blue(),
                timestamp=version.date_published,
                url=f"https://modrinth.com/mod/{project_info.slug}"
            )

            if version.changelog:
                changelog = version.changelog[:1000] + "..." if len(version.changelog) > 1000 else version.changelog
                embed.add_field(name="📝 Changelog", value=changelog, inline=False)

            embed.add_field(name="🎮 Game Versions", value=", ".join(version.game_versions[:5]), inline=True)
            embed.add_field(name="⚙️ Loaders", value=", ".join(version.loaders), inline=True)

            if project_info.icon_url:
                embed.set_thumbnail(url=project_info.icon_url)

            embed.set_footer(text="Personal Modrinth Watchlist")

            await user.send(embed=embed)

        except discord.Forbidden:
            log.warning(f"Cannot send DM to user {user.id}")
        except Exception as e:
            log.error(f"Error sending user notification: {e}")

    @commands.group(name="modrinth", aliases=["mr"])
    async def modrinth(self, ctx):
        """Modrinth update notifications with enhanced features."""
        pass

    @modrinth.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def add_project_interactive(self, ctx, *, project_name: str):
        """Add a project to monitor with interactive setup."""

        # Step 1: Search for projects
        async with ctx.typing():
            try:
                search_results = await self.api.search_projects(project_name, limit=5)
                if not search_results:
                    await ctx.send("❌ No projects found with that name.")
                    return
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error searching for projects: {e}")
                return

        # If multiple results, let user choose
        selected_project = None
        if len(search_results) == 1:
            selected_project = search_results[0]
        else:
            embed = discord.Embed(
                title="Multiple Projects Found",
                description="Please select a project by reacting with the corresponding number:",
                color=discord.Color.blue()
            )

            for i, project in enumerate(search_results[:5], 1):
                embed.add_field(
                    name=f"{i}. {project.name}",
                    value=f"Type: {project.project_type.title()}\n{project.description[:100]}...",
                    inline=False
                )

            msg = await ctx.send(embed=embed)

            # Add reactions
            reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
            for i in range(min(len(search_results), 5)):
                await msg.add_reaction(reactions[i])

            def check(reaction, user):
                return (user == ctx.author and
                        str(reaction.emoji) in reactions[:len(search_results)] and
                        reaction.message.id == msg.id)

            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
                selected_index = reactions.index(str(reaction.emoji))
                selected_project = search_results[selected_index]
                await msg.delete()
            except asyncio.TimeoutError:
                await msg.edit(content="❌ Selection timed out.", embed=None)
                return

        # Fetch project details and ALL supported versions/loaders (including all release channels)
        async with ctx.typing():
            try:
                # Get full project details
                project_info = await self.api.get_project(selected_project.id)

                # Get ALL versions to determine supported loaders and game versions across all release channels
                all_versions = await self.api.get_all_project_versions(selected_project.id)

                # Extract unique loaders and game versions from ALL release channels
                supported_loaders = set()
                supported_game_versions = set()

                for version in all_versions:
                    supported_loaders.update(version.loaders)
                    supported_game_versions.update(version.game_versions)

                # Convert to sorted lists
                supported_loaders = sorted(list(supported_loaders))
                supported_game_versions = sorted(list(supported_game_versions), reverse=True)  # Latest first

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error fetching project details: {e}")
                return

        # Start interactive session with project support info
        session = {
            'project': project_info,
            'supported_loaders': supported_loaders,
            'supported_game_versions': supported_game_versions,
            'step': 'confirm_project',
            'user_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'guild_id': ctx.guild.id
        }
        self._interactive_sessions[ctx.author.id] = session

        # Show project confirmation
        await self._show_project_confirmation(ctx, project_info, supported_loaders, supported_game_versions)

    async def _show_project_confirmation(self, ctx, project_info: ProjectInfo, supported_loaders: List[str], supported_game_versions: List[str]):
        """Show project confirmation with link to Modrinth page."""
        embed = discord.Embed(
            title="📦 Project Confirmation",
            description=f"**{project_info.name}**\n{project_info.description}",
            color=discord.Color.green(),
            url=f"https://modrinth.com/mod/{project_info.slug}"
        )

        embed.add_field(name="📊 Type", value=project_info.project_type.title(), inline=True)
        embed.add_field(name="📥 Downloads", value=f"{project_info.downloads:,}", inline=True)
        embed.add_field(name="👥 Followers", value=f"{project_info.followers:,}", inline=True)

        embed.add_field(
            name="🎮 Supported Game Versions",
            value=", ".join(supported_game_versions[:10]) + ("..." if len(supported_game_versions) > 10 else ""),
            inline=False
        )
        embed.add_field(
            name="⚙️ Supported Loaders",
            value=", ".join(supported_loaders),
            inline=False
        )

        if project_info.icon_url:
            embed.set_thumbnail(url=project_info.icon_url)

        embed.set_footer(text="Click the title to view on Modrinth • Type 'yes' to confirm or 'no' to cancel")

        await ctx.send(embed=embed)

    async def _ask_minecraft_version(self, ctx):
        """Ask for Minecraft version filtering with project-specific versions."""
        session = self._interactive_sessions.get(ctx.author.id)
        if not session:
            return

        supported_versions = session['supported_game_versions']

        embed = discord.Embed(
            title="🎮 Minecraft Version Selection",
            description="Which Minecraft version(s) do you want to monitor for this project?",
            color=discord.Color.blue()
        )

        # Show supported versions (limit to first 20 for display)
        versions_display = ", ".join(supported_versions[:20])
        if len(supported_versions) > 20:
            versions_display += f"\n... and {len(supported_versions) - 20} more"

        embed.add_field(
            name="📋 Supported Versions",
            value=versions_display,
            inline=False
        )

        embed.add_field(
            name="📝 Instructions",
            value="• Type specific versions separated by commas (e.g., `1.21.5, 1.20.6`)\n• Type `all` to monitor all supported versions\n• Type `latest` to monitor only the latest version",
            inline=False
        )

        embed.set_footer(text="Choose from the supported versions listed above")

        await ctx.send(embed=embed)

    async def _ask_loader_type(self, ctx):
        """Ask for loader type filtering with project-specific loaders."""
        session = self._interactive_sessions.get(ctx.author.id)
        if not session:
            return

        supported_loaders = session['supported_loaders']

        embed = discord.Embed(
            title="⚙️ Loader Selection",
            description="Which loader(s) do you want to monitor for this project?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="📋 Supported Loaders",
            value=", ".join(supported_loaders),
            inline=False
        )

        embed.add_field(
            name="📝 Instructions",
            value="• Type specific loaders separated by commas (e.g., `fabric, forge`)\n• Type `all` to monitor all supported loaders",
            inline=False
        )

        embed.set_footer(text="Choose from the supported loaders listed above")

        await ctx.send(embed=embed)

    async def _ask_release_channel(self, ctx):
        """Ask for release channel filtering."""
        embed = discord.Embed(
            title="📢 Release Channel Selection",
            description="Which release channels do you want to monitor?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🏷️ Available Channels",
            value="• `release` - Stable releases only\n• `beta` - Beta releases\n• `alpha` - Alpha releases\n• `all` - All release types",
            inline=False
        )

        embed.add_field(
            name="📝 Instructions",
            value="Type channels separated by commas (e.g., `release, beta`) or `all` for everything",
            inline=False
        )

        embed.set_footer(text="Note: If your selected MC version only has beta/alpha releases, you'll be warned")

        await ctx.send(embed=embed)

    async def _ask_notification_channel(self, ctx):
        """Ask for notification channel."""
        embed = discord.Embed(
            title="📺 Notification Channel",
            description="Which channel should receive update notifications for this project?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="📝 Instructions",
            value="• Mention a channel (e.g., #updates)\n• Type a channel name (e.g., `updates`)\n• Type `here` to use this channel",
            inline=False
        )

        embed.set_footer(text="You need manage channel permissions for the selected channel")

        await ctx.send(embed=embed)

    async def _ask_role_pings(self, ctx):
        """Ask for role pings."""
        embed = discord.Embed(
            title="🔔 Role Notifications",
            description="Which roles should be pinged when this project updates?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="📝 Instructions",
            value="• Mention roles (e.g., @Moderators @Members)\n• Type role names separated by commas (e.g., `Moderators, Members`)\n• Type `none` for no role pings",
            inline=False
        )

        embed.set_footer(text="Only mentionable roles will work")

        await ctx.send(embed=embed)

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            response = await self.bot.wait_for('message', timeout=60.0, check=check)
            session = self._interactive_sessions.get(ctx.author.id)
            if not session:
                return

            roles = []
            if response.content.lower() != "none":
                # Parse role mentions
                roles.extend(response.role_mentions)

                # Parse role names if no mentions
                if not roles and response.content.lower() != "none":
                    role_names = [name.strip() for name in response.content.split(',')]
                    for role_name in role_names:
                        role = discord.utils.get(ctx.guild.roles, name=role_name)
                        if role:
                            roles.append(role)

            session['roles'] = roles
            await self._finalize_setup(ctx)

        except asyncio.TimeoutError:
            await ctx.send("❌ Role selection timed out.")
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _finalize_setup(self, ctx):
        """Finalize the setup and create the monitoring configuration."""
        session = self._interactive_sessions.get(ctx.author.id)
        if not session:
            return

        try:
            # Create monitored project
            monitored_project = MonitoredProject(
                project_id=session['project'].id,
                name=session['project'].name,
                game_versions=session['game_versions'],
                loaders=session['loaders'],
                release_channels=session['release_channels'],
                role_ids=[role.id for role in session.get('roles', [])],
                last_version=None
            )

            # Add to guild config
            guild_config = self._get_guild_config(ctx.guild.id)
            guild_config.projects[session['project'].id] = monitored_project
            guild_config.channel_id = session['notification_channel'].id

            # Get the latest version for initial setup
            try:
                versions = await self.api.get_project_versions(
                    session['project'].id,
                    loaders=session['loaders'],
                    game_versions=session['game_versions']
                )

                if versions:
                    # Filter by release channels
                    filtered_versions = [v for v in versions if v.version_type in session['release_channels']]

                    if filtered_versions:
                        latest_version = filtered_versions[0]
                        monitored_project.last_version = latest_version.id

                        # Send initial notification
                        embed = discord.Embed(
                            title=f"✅ Now Monitoring: {session['project'].name}",
                            description=f"Latest version: **{latest_version.version_number}** ({latest_version.version_type})",
                            color=discord.Color.green(),
                            url=f"https://modrinth.com/mod/{session['project'].slug}"
                        )

                        embed.add_field(name="🎮 Monitoring Versions", value=", ".join(session['game_versions'][:5]), inline=True)
                        embed.add_field(name="⚙️ Monitoring Loaders", value=", ".join(session['loaders']), inline=True)
                        embed.add_field(name="📢 Release Channels", value=", ".join(session['release_channels']), inline=True)
                        embed.add_field(name="📺 Notification Channel", value=session['notification_channel'].mention, inline=True)

                        if session.get('roles'):
                            embed.add_field(name="🔔 Role Pings", value=", ".join([r.name for r in session['roles']]), inline=True)

                        if session['project'].icon_url:
                            embed.set_thumbnail(url=session['project'].icon_url)

                        embed.set_footer(text="Monitoring started! You'll be notified of new updates.")

                        await ctx.send(embed=embed)

                        # Also send a notification to the monitoring channel if it's different
                        if session['notification_channel'] != ctx.channel:
                            update_embed = discord.Embed(
                                title=f"🔄 {session['project'].name} - Monitoring Started",
                                description=f"Now monitoring this project for updates!\nCurrent version: **{latest_version.version_number}**",
                                color=discord.Color.blue(),
                                url=f"https://modrinth.com/mod/{session['project'].slug}"
                            )

                            if session['project'].icon_url:
                                update_embed.set_thumbnail(url=session['project'].icon_url)

                            content = None
                            if session.get('roles'):
                                content = " ".join([role.mention for role in session['roles']])

                            await session['notification_channel'].send(content=content, embed=update_embed)

            except Exception as e:
                log.error(f"Error getting initial version: {e}")
                # Still save the configuration even if we can't get the latest version
                pass

            # Save configuration
            await self._save_guild_config(ctx.guild.id)

        except Exception as e:
            log.error(f"Error finalizing setup: {e}")
            await ctx.send(f"❌ Error setting up monitoring: {e}")

        # Clean up session
        self._interactive_sessions.pop(ctx.author.id, None)

    @commands.Cog.listener()
    async def on_message(self, message):
        """Handle interactive session messages."""
        if message.author.bot or message.author.id not in self._interactive_sessions:
            return

        session = self._interactive_sessions[message.author.id]
        if message.channel.id != session.get('channel_id'):
            return

        try:
            # Handle project confirmation
            if session.get('step') == 'confirm_project':
                if message.content.lower() in ['yes', 'y', 'confirm']:
                    session['step'] = 'minecraft_version'
                    await self._ask_minecraft_version(message.channel)
                elif message.content.lower() in ['no', 'n', 'cancel']:
                    await message.channel.send("❌ Project addition cancelled.")
                    self._interactive_sessions.pop(message.author.id, None)
                return

            # Handle Minecraft version selection
            elif session.get('step') == 'minecraft_version':
                content = message.content.strip()
                supported_versions = session['supported_game_versions']

                if content.lower() == 'all':
                    game_versions = supported_versions
                elif content.lower() == 'latest':
                    game_versions = [supported_versions[0]] if supported_versions else []
                else:
                    # Parse comma-separated versions
                    requested_versions = [v.strip() for v in content.split(',')]
                    game_versions = []
                    invalid_versions = []

                    for version in requested_versions:
                        if version in supported_versions:
                            game_versions.append(version)
                        else:
                            invalid_versions.append(version)

                    if invalid_versions:
                        await message.channel.send(
                            f"❌ Invalid versions: {', '.join(invalid_versions)}\n"
                            f"Supported versions: {', '.join(supported_versions[:10])}"
                        )
                        return

                if not game_versions:
                    await message.channel.send("❌ No valid versions selected. Please try again.")
                    return

                session['game_versions'] = game_versions
                session['step'] = 'loader_type'
                await self._ask_loader_type(message.channel)
                return

            # Handle loader selection
            elif session.get('step') == 'loader_type':
                content = message.content.strip()
                supported_loaders = session['supported_loaders']

                if content.lower() == 'all':
                    loaders = supported_loaders
                else:
                    # Parse comma-separated loaders
                    requested_loaders = [l.strip().lower() for l in content.split(',')]
                    loaders = []
                    invalid_loaders = []

                    for loader in requested_loaders:
                        # Find matching loader (case-insensitive)
                        matching_loader = next((l for l in supported_loaders if l.lower() == loader), None)
                        if matching_loader:
                            loaders.append(matching_loader)
                        else:
                            invalid_loaders.append(loader)

                    if invalid_loaders:
                        await message.channel.send(
                            f"❌ Invalid loaders: {', '.join(invalid_loaders)}\n"
                            f"Supported loaders: {', '.join(supported_loaders)}"
                        )
                        return

                if not loaders:
                    await message.channel.send("❌ No valid loaders selected. Please try again.")
                    return

                session['loaders'] = loaders
                session['step'] = 'release_channel'
                await self._ask_release_channel(message.channel)
                return

            # Handle release channel selection
            elif session.get('step') == 'release_channel':
                content = message.content.strip().lower()

                if content == 'all':
                    release_channels = ['release', 'beta', 'alpha']
                else:
                    # Parse comma-separated channels
                    requested_channels = [c.strip().lower() for c in content.split(',')]
                    valid_channels = ['release', 'beta', 'alpha']
                    release_channels = []
                    invalid_channels = []

                    for channel in requested_channels:
                        if channel in valid_channels:
                            release_channels.append(channel)
                        else:
                            invalid_channels.append(channel)

                    if invalid_channels:
                        await message.channel.send(
                            f"❌ Invalid channels: {', '.join(invalid_channels)}\n"
                            f"Valid channels: release, beta, alpha"
                        )
                        return

                if not release_channels:
                    await message.channel.send("❌ No valid release channels selected. Please try again.")
                    return

                # Check if selected versions have releases in selected channels
                try:
                    versions = await self.api.get_project_versions(
                        session['project'].id,
                        loaders=session['loaders'],
                        game_versions=session['game_versions']
                    )

                    available_types = set(v.version_type for v in versions)
                    selected_types = set(release_channels)

                    if not available_types.intersection(selected_types):
                        warning_msg = (
                            f"⚠️ **Warning**: No {', '.join(release_channels)} versions found for your selected MC versions and loaders.\n"
                            f"Available release types: {', '.join(available_types) if available_types else 'None'}\n"
                            f"Continue anyway? (yes/no)"
                        )
                        await message.channel.send(warning_msg)
                        session['step'] = 'confirm_warning'
                        session['pending_release_channels'] = release_channels
                        return

                except Exception as e:
                    log.error(f"Error checking versions: {e}")
                    # Continue anyway if we can't check

                session['release_channels'] = release_channels
                session['step'] = 'notification_channel'
                await self._ask_notification_channel(message.channel)
                return

            # Handle warning confirmation
            elif session.get('step') == 'confirm_warning':
                if message.content.lower() in ['yes', 'y']:
                    session['release_channels'] = session['pending_release_channels']
                    session['step'] = 'notification_channel'
                    await self._ask_notification_channel(message.channel)
                elif message.content.lower() in ['no', 'n']:
                    session['step'] = 'release_channel'
                    await self._ask_release_channel(message.channel)
                else:
                    await message.channel.send("Please type 'yes' or 'no'.")
                return

            # Handle notification channel selection
            elif session.get('step') == 'notification_channel':
                channel = None

                # Try channel mention first
                if message.channel_mentions:
                    channel = message.channel_mentions[0]
                elif message.content.lower() == 'here':
                    channel = message.channel
                else:
                    # Try to find by name
                    channel_name = message.content.strip().replace('#', '')
                    channel = discord.utils.get(message.guild.channels, name=channel_name)

                if not channel or not isinstance(channel, discord.TextChannel):
                    await message.channel.send("❌ Invalid channel. Please mention a text channel, type a channel name, or use 'here'.")
                    return

                # Check permissions
                if not channel.permissions_for(message.guild.me).send_messages:
                    await message.channel.send(f"❌ I don't have permission to send messages in {channel.mention}.")
                    return

                session['notification_channel'] = channel
                session['step'] = 'role_pings'
                await self._ask_role_pings(message.channel)
                return

        except Exception as e:
            log.error(f"Error in message handler: {e}")
            await message.channel.send("❌ An error occurred. Please try again.")
            self._interactive_sessions.pop(message.author.id, None)

    # Rest of the commands remain the same...
    @modrinth.command(name="list")
    async def list_projects(self, ctx):
        """List all monitored projects in this server."""
        guild_config = self._get_guild_config(ctx.guild.id)

        if not guild_config.projects:
            await ctx.send("📭 No projects are currently being monitored in this server.")
            return

        embed = discord.Embed(
            title="📋 Monitored Projects",
            color=discord.Color.blue()
        )

        for project_id, monitored_project in guild_config.projects.items():
            embed.add_field(
                name=monitored_project.name,
                value=(
                    f"**Versions:** {', '.join(monitored_project.game_versions[:3])}{'...' if len(monitored_project.game_versions) > 3 else ''}\n"
                    f"**Loaders:** {', '.join(monitored_project.loaders)}\n"
                    f"**Channels:** {', '.join(monitored_project.release_channels)}"
                ),
                inline=True
            )

        channel = ctx.guild.get_channel(guild_config.channel_id) if guild_config.channel_id else None
        if channel:
            embed.set_footer(text=f"Updates sent to #{channel.name}")

        await ctx.send(embed=embed)

    @modrinth.command(name="remove", aliases=["delete", "del"])
    @commands.admin_or_permissions(manage_guild=True)
    async def remove_project(self, ctx, *, project_name: str):
        """Remove a project from monitoring."""
        guild_config = self._get_guild_config(ctx.guild.id)

        # Find project by name (case-insensitive)
        project_to_remove = None
        for project_id, monitored_project in guild_config.projects.items():
            if monitored_project.name.lower() == project_name.lower():
                project_to_remove = (project_id, monitored_project)
                break

        if not project_to_remove:
            await ctx.send(f"❌ Project '{project_name}' not found in monitoring list.")
            return

        project_id, monitored_project = project_to_remove
        del guild_config.projects[project_id]
        await self._save_guild_config(ctx.guild.id)

        embed = discord.Embed(
            title="✅ Project Removed",
            description=f"**{monitored_project.name}** is no longer being monitored.",
            color=discord.Color.green()
        )

        await ctx.send(embed=embed)

    @modrinth.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def set_channel(self, ctx, channel: discord.TextChannel = None):
        """Set the notification channel for this server."""
        if channel is None:
            channel = ctx.channel

        # Check permissions
        if not channel.permissions_for(ctx.guild.me).send_messages:
            await ctx.send(f"❌ I don't have permission to send messages in {channel.mention}.")
            return

        guild_config = self._get_guild_config(ctx.guild.id)
        guild_config.channel_id = channel.id
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Notification channel set to {channel.mention}")

    @modrinth.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def toggle_monitoring(self, ctx):
        """Toggle monitoring on/off for this server."""
        guild_config = self._get_guild_config(ctx.guild.id)
        guild_config.enabled = not guild_config.enabled
        await self._save_guild_config(ctx.guild.id)

        status = "enabled" if guild_config.enabled else "disabled"
        await ctx.send(f"✅ Monitoring {status} for this server.")

    @modrinth.command(name="test")
    async def test_project(self, ctx, project_id: str):
        """Force check for updates and send the latest version."""
        async with ctx.typing():
            try:
                project_info = await self.api.get_project(project_id)
                versions = await self.api.get_project_versions(project_id, limit=1)

                if not versions:
                    await ctx.send("❌ No versions found for this project.")
                    return

                latest_version = versions[0]

                embed = discord.Embed(
                    title=f"🔍 Test: {project_info.name}",
                    description=f"**Latest Version:** {latest_version.version_number}\n**Type:** {latest_version.version_type.title()}",
                    color=discord.Color.blue(),
                    timestamp=latest_version.date_published,
                    url=f"https://modrinth.com/mod/{project_info.slug}"
                )

                if latest_version.changelog:
                    changelog = latest_version.changelog[:500] + "..." if len(latest_version.changelog) > 500 else latest_version.changelog
                    embed.add_field(name="📝 Changelog", value=changelog, inline=False)

                embed.add_field(name="🎮 Game Versions", value=", ".join(latest_version.game_versions[:5]), inline=True)
                embed.add_field(name="⚙️ Loaders", value=", ".join(latest_version.loaders), inline=True)
                embed.add_field(name="💾 Downloads", value=str(latest_version.downloads), inline=True)

                if project_info.icon_url:
                    embed.set_thumbnail(url=project_info.icon_url)

                embed.set_footer(text="This is a test notification")

                await ctx.send(embed=embed)

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error testing project: {e}")

    @modrinth.group(name="watch", aliases=["personal"])
    async def personal_watch(self, ctx):
        """Personal watchlist commands (DM notifications)."""
        pass

    @personal_watch.command(name="add")
    async def add_personal_watch(self, ctx, *, project_name: str):
        """Add a project to your personal watchlist."""
        # Similar to server add but saves to user config
        async with ctx.typing():
            try:
                search_results = await self.api.search_projects(project_name, limit=5)
                if not search_results:
                    await ctx.send("❌ No projects found with that name.")
                    return
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error searching for projects: {e}")
                return

        # For personal watchlist, use simpler setup (monitor all versions/loaders/channels)
        if len(search_results) == 1:
            selected_project = search_results[0]
        else:
            # Show selection menu (similar to server add)
            embed = discord.Embed(
                title="Multiple Projects Found",
                description="Please select a project by reacting:",
                color=discord.Color.blue()
            )

            for i, project in enumerate(search_results[:5], 1):
                embed.add_field(
                    name=f"{i}. {project.name}",
                    value=f"Type: {project.project_type.title()}\n{project.description[:100]}...",
                    inline=False
                )

            msg = await ctx.send(embed=embed)
            reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
            for i in range(min(len(search_results), 5)):
                await msg.add_reaction(reactions[i])

            def check(reaction, user):
                return (user == ctx.author and
                        str(reaction.emoji) in reactions[:len(search_results)] and
                        reaction.message.id == msg.id)

            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
                selected_index = reactions.index(str(reaction.emoji))
                selected_project = search_results[selected_index]
                await msg.delete()
            except asyncio.TimeoutError:
                await msg.edit(content="❌ Selection timed out.", embed=None)
                return

        # Add to personal watchlist with default settings
        user_config = self._get_user_config(ctx.author.id)

        if selected_project.id in user_config.projects:
            await ctx.send(f"❌ You're already watching **{selected_project.name}**.")
            return

        monitored_project = MonitoredProject(
            project_id=selected_project.id,
            name=selected_project.name,
            game_versions=["all"],  # Monitor all versions for personal watchlist
            loaders=["all"],        # Monitor all loaders
            release_channels=["release", "beta", "alpha"],  # Monitor all channels
            role_ids=[],            # No roles for personal
            last_version=None
        )

        user_config.projects[selected_project.id] = monitored_project
        await self._save_user_config(ctx.author.id)

        embed = discord.Embed(
            title="✅ Added to Personal Watchlist",
            description=f"**{selected_project.name}** added to your personal watchlist!\nYou'll receive DM notifications for all updates.",
            color=discord.Color.green(),
            url=f"https://modrinth.com/mod/{selected_project.slug}"
        )

        await ctx.send(embed=embed)

    @personal_watch.command(name="list")
    async def list_personal_watch(self, ctx):
        """List your personal watchlist."""
        user_config = self._get_user_config(ctx.author.id)

        if not user_config.projects:
            await ctx.send("📭 Your personal watchlist is empty.")
            return

        embed = discord.Embed(
            title="👤 Your Personal Watchlist",
            color=discord.Color.blue()
        )

        for project_id, monitored_project in user_config.projects.items():
            embed.add_field(
                name=monitored_project.name,
                value="Monitoring all updates via DM",
                inline=True
            )

        embed.set_footer(text="Updates sent via Direct Message")
        await ctx.send(embed=embed)

    @personal_watch.command(name="remove")
    async def remove_personal_watch(self, ctx, *, project_name: str):
        """Remove a project from your personal watchlist."""
        user_config = self._get_user_config(ctx.author.id)

        # Find project by name
        project_to_remove = None
        for project_id, monitored_project in user_config.projects.items():
            if monitored_project.name.lower() == project_name.lower():
                project_to_remove = (project_id, monitored_project)
                break

        if not project_to_remove:
            await ctx.send(f"❌ Project '{project_name}' not found in your watchlist.")
            return

        project_id, monitored_project = project_to_remove
        del user_config.projects[project_id]
        await self._save_user_config(ctx.author.id)

        await ctx.send(f"✅ **{monitored_project.name}** removed from your personal watchlist.")