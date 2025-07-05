"""Enhanced Modrinth Update Notifier cog for Red-DiscordBot."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

import discord
from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list

from .api import ModrinthAPI, ModrinthAPIError, ProjectNotFoundError
from .models import ProjectInfo, VersionInfo, ChannelMonitor, MonitoredProject, GuildConfig, UserConfig
from .utils import create_update_embed, create_project_info_embed, parse_filter_string, get_valid_loaders

log = logging.getLogger("red.modrinthnotifier")

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
        # Load guild configs
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            self._guild_configs[guild_id] = GuildConfig.from_dict(data)

        # Load user configs
        all_users = await self.config.all_users()
        for user_id, data in all_users.items():
            self._user_configs[user_id] = UserConfig.from_dict(data)

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
        """Check for updates on all monitored projects."""
        # Check guild projects
        for guild_id, config in self._guild_configs.items():
            if not config.enabled:
                continue

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            for project_id, project in config.projects.items():
                await self._check_guild_project_updates(guild, project)

        # Check user projects
        for user_id, config in self._user_configs.items():
            if not config.enabled:
                continue

            user = self.bot.get_user(user_id)
            if not user:
                continue

            for project_id, project in config.projects.items():
                await self._check_user_project_updates(user, project)

    async def _check_guild_project_updates(self, guild: discord.Guild, project: MonitoredProject):
        """Check for updates on a guild project."""
        try:
            versions = await self.api.get_project_versions(project.id)
            if not versions:
                return

            latest_version = versions[0]

            # Check if this is a new version
            if project.last_version and latest_version.id == project.last_version:
                return

            # Send notifications to all monitored channels
            for channel_id, monitor in project.channels.items():
                channel = guild.get_channel(channel_id)
                if not channel:
                    continue

                # Check if version matches filters
                if not latest_version.matches_filters(monitor.required_loaders, monitor.required_game_versions):
                    continue

                # Get project info for embed
                project_info = await self.api.get_project(project.id)

                # Create and send embed
                embed = create_update_embed(project_info, latest_version, monitor)

                # Prepare mentions
                mentions = []
                for role_id in monitor.role_ids:
                    role = guild.get_role(role_id)
                    if role:
                        mentions.append(role.mention)

                content = " ".join(mentions) if mentions else None

                try:
                    await channel.send(content=content, embed=embed)
                    log.info(f"Sent update notification for {project.name} to {guild.name}#{channel.name}")
                except discord.HTTPException as e:
                    log.error(f"Failed to send update to {guild.name}#{channel.name}: {e}")

            # Update last version
            project.last_version = latest_version.id
            await self._save_guild_config(guild.id)

        except Exception as e:
            log.error(f"Error checking guild project {project.id}: {e}")

    async def _check_user_project_updates(self, user: discord.User, project: MonitoredProject):
        """Check for updates on a user project."""
        try:
            versions = await self.api.get_project_versions(project.id)
            if not versions:
                return

            latest_version = versions[0]

            # Check if this is a new version
            if project.last_version and latest_version.id == project.last_version:
                return

            # Get project info for embed
            project_info = await self.api.get_project(project.id)

            # Create and send embed
            embed = create_update_embed(project_info, latest_version, title_prefix="🔔 Personal Watchlist: ")

            try:
                await user.send(embed=embed)
                log.info(f"Sent personal update notification for {project.name} to {user.name}")
            except discord.HTTPException as e:
                log.error(f"Failed to send DM to {user.name}: {e}")

            # Update last version
            project.last_version = latest_version.id
            await self._save_user_config(user.id)

        except Exception as e:
            log.error(f"Error checking user project {project.id}: {e}")

    # Enhanced Commands

    @commands.group(name="modrinth", aliases=["mr"])
    async def modrinth(self, ctx):
        """Modrinth update notifications with enhanced features."""
        pass

    @modrinth.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def add_project_interactive(self, ctx, *, project_name: str):
        """Add a project to monitoring with interactive setup.

        Usage: !modrinth add sodium

        This will start an interactive setup process to configure monitoring.
        """
        # Search for projects
        async with ctx.typing():
            try:
                search_results = await self.api.search_projects(project_name, limit=10)
                if not search_results:
                    await ctx.send(f"❌ No projects found matching '{project_name}'.")
                    return
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error searching for projects: {e}")
                return

        # If only one result, proceed directly
        if len(search_results) == 1:
            selected_project = search_results[0]
        else:
            # Show multiple options
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

        # Start interactive session
        session = {
            'project': selected_project,
            'step': 'confirm_project',
            'user_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'guild_id': ctx.guild.id
        }
        self._interactive_sessions[ctx.author.id] = session

        # Show project confirmation
        await self._show_project_confirmation(ctx, selected_project)

    async def _show_project_confirmation(self, ctx, project: ProjectInfo):
        """Show project confirmation step."""
        embed = discord.Embed(
            title="Project Confirmation",
            description=f"You selected: **{project.name}**",
            color=discord.Color.green(),
            url=f"https://modrinth.com/{project.project_type}/{project.slug}"
        )

        if project.icon_url:
            embed.set_thumbnail(url=project.icon_url)

        embed.add_field(name="Type", value=project.project_type.title(), inline=True)
        embed.add_field(name="Downloads", value=f"{project.downloads:,}", inline=True)
        embed.add_field(name="Description", value=project.description[:500], inline=False)

        embed.set_footer(text="React with ✅ to confirm or ❌ to cancel")

        msg = await ctx.send(embed=embed)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        def check(reaction, user):
            return (user == ctx.author and
                   str(reaction.emoji) in ["✅", "❌"] and
                   reaction.message.id == msg.id)

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
            await msg.delete()

            if str(reaction.emoji) == "✅":
                await self._ask_minecraft_version(ctx)
            else:
                await ctx.send("❌ Project addition cancelled.")
                self._interactive_sessions.pop(ctx.author.id, None)
        except asyncio.TimeoutError:
            await msg.edit(content="❌ Confirmation timed out.", embed=None)
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _ask_minecraft_version(self, ctx):
        """Ask for Minecraft version filtering."""
        embed = discord.Embed(
            title="Minecraft Version Filter",
            description="Which Minecraft versions should be monitored?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Options",
            value="1️⃣ All versions\n2️⃣ Specific versions (you'll specify)\n3️⃣ Latest major version only",
            inline=False
        )

        msg = await ctx.send(embed=embed)
        reactions = ["1️⃣", "2️⃣", "3️⃣"]
        for reaction in reactions:
            await msg.add_reaction(reaction)

        def check(reaction, user):
            return (user == ctx.author and
                   str(reaction.emoji) in reactions and
                   reaction.message.id == msg.id)

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
            await msg.delete()

            session = self._interactive_sessions[ctx.author.id]

            if str(reaction.emoji) == "1️⃣":
                session['minecraft_versions'] = None
                await self._ask_loader_type(ctx)
            elif str(reaction.emoji) == "2️⃣":
                session['step'] = 'specify_versions'
                await ctx.send("Please specify the Minecraft versions you want to monitor (comma-separated, e.g., `1.20.1, 1.21`):")
            elif str(reaction.emoji) == "3️⃣":
                session['minecraft_versions'] = ["1.21"]  # Current latest
                await self._ask_loader_type(ctx)

        except asyncio.TimeoutError:
            await msg.edit(content="❌ Selection timed out.", embed=None)
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _ask_loader_type(self, ctx):
        """Ask for loader type filtering."""
        embed = discord.Embed(
            title="Loader Type Filter",
            description="Which mod loaders should be monitored?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Options",
            value="1️⃣ All loaders\n2️⃣ Fabric only\n3️⃣ Forge only\n4️⃣ NeoForge only\n5️⃣ Custom selection",
            inline=False
        )

        msg = await ctx.send(embed=embed)
        reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for reaction in reactions:
            await msg.add_reaction(reaction)

        def check(reaction, user):
            return (user == ctx.author and
                   str(reaction.emoji) in reactions and
                   reaction.message.id == msg.id)

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
            await msg.delete()

            session = self._interactive_sessions[ctx.author.id]

            loader_map = {
                "1️⃣": None,
                "2️⃣": ["fabric"],
                "3️⃣": ["forge"],
                "4️⃣": ["neoforge"],
            }

            if str(reaction.emoji) in loader_map:
                session['loaders'] = loader_map[str(reaction.emoji)]
                await self._ask_release_channel(ctx)
            elif str(reaction.emoji) == "5️⃣":
                session['step'] = 'specify_loaders'
                await ctx.send("Please specify the loaders you want to monitor (comma-separated, e.g., `fabric, forge`):")

        except asyncio.TimeoutError:
            await msg.edit(content="❌ Selection timed out.", embed=None)
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _ask_release_channel(self, ctx):
        """Ask for release channel filtering."""
        embed = discord.Embed(
            title="Release Channel Filter",
            description="Which release channels should be monitored?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Options",
            value="1️⃣ All channels\n2️⃣ Release only\n3️⃣ Beta and Release\n4️⃣ Alpha, Beta, and Release",
            inline=False
        )

        msg = await ctx.send(embed=embed)
        reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
        for reaction in reactions:
            await msg.add_reaction(reaction)

        def check(reaction, user):
            return (user == ctx.author and
                   str(reaction.emoji) in reactions and
                   reaction.message.id == msg.id)

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
            await msg.delete()

            session = self._interactive_sessions[ctx.author.id]

            channel_map = {
                "1️⃣": None,
                "2️⃣": ["release"],
                "3️⃣": ["release", "beta"],
                "4️⃣": ["release", "beta", "alpha"]
            }

            session['release_channels'] = channel_map[str(reaction.emoji)]
            await self._ask_notification_channel(ctx)

        except asyncio.TimeoutError:
            await msg.edit(content="❌ Selection timed out.", embed=None)
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _ask_notification_channel(self, ctx):
        """Ask for notification channel."""
        embed = discord.Embed(
            title="Notification Channel",
            description="Which channel should receive update notifications?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Instructions",
            value="Please mention the channel (e.g., #updates) or type 'current' to use the current channel:",
            inline=False
        )

        await ctx.send(embed=embed)

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            msg = await self.bot.wait_for('message', timeout=60.0, check=check)

            session = self._interactive_sessions[ctx.author.id]

            if msg.content.lower() == 'current':
                session['notification_channel'] = ctx.channel
            elif msg.channel_mentions:
                session['notification_channel'] = msg.channel_mentions[0]
            else:
                await ctx.send("❌ Invalid channel. Please mention a channel or type 'current'.")
                return

            await self._ask_role_pings(ctx)

        except asyncio.TimeoutError:
            await ctx.send("❌ Channel selection timed out.")
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _ask_role_pings(self, ctx):
        """Ask for role pings."""
        embed = discord.Embed(
            title="Role Notifications",
            description="Which roles should be pinged for updates?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Instructions",
            value="Mention the roles you want to ping (e.g., @Mod Updates @Everyone) or type 'none' for no pings:",
            inline=False
        )

        await ctx.send(embed=embed)

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            msg = await self.bot.wait_for('message', timeout=60.0, check=check)

            session = self._interactive_sessions[ctx.author.id]

            if msg.content.lower() == 'none':
                session['roles'] = []
            else:
                session['roles'] = msg.role_mentions

            await self._finalize_setup(ctx)

        except asyncio.TimeoutError:
            await ctx.send("❌ Role selection timed out.")
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _finalize_setup(self, ctx):
        """Finalize the setup and create the monitoring configuration."""
        session = self._interactive_sessions.get(ctx.author.id)
        if not session:
            return

        project = session['project']
        config = self._get_guild_config(ctx.guild.id)

        # Create monitored project if it doesn't exist
        if project.id not in config.projects:
            monitored_project = MonitoredProject(
                id=project.id,
                name=project.name,
                added_by=ctx.author.id
            )
            config.projects[project.id] = monitored_project
        else:
            monitored_project = config.projects[project.id]

        # Create channel monitor
        channel_monitor = ChannelMonitor(
            channel_id=session['notification_channel'].id,
            role_ids=[role.id for role in session['roles']],
            required_loaders=session.get('loaders'),
            required_game_versions=session.get('minecraft_versions')
        )

        monitored_project.channels[session['notification_channel'].id] = channel_monitor

        # Save configuration
        await self._save_guild_config(ctx.guild.id)

        # Send confirmation and initial version
        embed = discord.Embed(
            title="✅ Monitoring Setup Complete",
            description=f"Successfully set up monitoring for **{project.name}**",
            color=discord.Color.green()
        )

        embed.add_field(name="Channel", value=session['notification_channel'].mention, inline=True)
        embed.add_field(name="Roles", value=humanize_list([role.mention for role in session['roles']]) if session['roles'] else "None", inline=True)

        if session.get('minecraft_versions'):
            embed.add_field(name="Minecraft Versions", value=", ".join(session['minecraft_versions']), inline=True)
        if session.get('loaders'):
            embed.add_field(name="Loaders", value=", ".join(session['loaders']), inline=True)

        await ctx.send(embed=embed)

        # Send initial version to confirm monitoring is working
        try:
            versions = await self.api.get_project_versions(project.id)
            if versions:
                latest_version = versions[0]
                project_info = await self.api.get_project(project.id)

                if latest_version.matches_filters(session.get('loaders'), session.get('minecraft_versions')):
                    update_embed = create_update_embed(
                        project_info,
                        latest_version,
                        channel_monitor,
                        is_initial=True
                    )

                    content = None
                    if session['roles']:
                        content = " ".join([role.mention for role in session['roles']])

                    await session['notification_channel'].send(content=content, embed=update_embed)

                    # Update last version
                    monitored_project.last_version = latest_version.id
                    await self._save_guild_config(ctx.guild.id)
        except Exception as e:
            log.error(f"Error sending initial notification: {e}")

        # Clean up session
        self._interactive_sessions.pop(ctx.author.id, None)

    @commands.Cog.listener()
    async def on_message(self, message):
        """Handle interactive session messages."""
        if message.author.bot:
            return

        session = self._interactive_sessions.get(message.author.id)
        if not session:
            return

        # Handle version specification
        if session.get('step') == 'specify_versions':
            try:
                versions = [v.strip() for v in message.content.split(',')]
                session['minecraft_versions'] = versions
                await self._ask_loader_type(message.channel)
            except Exception:
                await message.channel.send("❌ Invalid format. Please use comma-separated versions (e.g., `1.20.1, 1.21`):")

        # Handle loader specification
        elif session.get('step') == 'specify_loaders':
            try:
                loaders = [l.strip().lower() for l in message.content.split(',')]
                valid_loaders = get_valid_loaders()
                invalid = [l for l in loaders if l not in valid_loaders]

                if invalid:
                    await message.channel.send(f"❌ Invalid loaders: {', '.join(invalid)}\nValid loaders: {', '.join(valid_loaders[:10])}...")
                    return

                session['loaders'] = loaders
                await self._ask_release_channel(message.channel)
            except Exception:
                await message.channel.send("❌ Invalid format. Please use comma-separated loaders (e.g., `fabric, forge`):")

    @modrinth.command(name="list")
    async def list_projects(self, ctx):
        """List all monitored projects in this server."""
        config = self._get_guild_config(ctx.guild.id)

        if not config.projects:
            await ctx.send("❌ No projects are currently being monitored in this server.")
            return

        embed = discord.Embed(
            title="Monitored Projects",
            color=discord.Color.blue()
        )

        for project_id, project in config.projects.items():
            channels = []
            for channel_id, monitor in project.channels.items():
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    channels.append(channel.mention)

            embed.add_field(
                name=project.name,
                value=f"ID: `{project_id}`\nChannels: {', '.join(channels) if channels else 'None'}",
                inline=True
            )

        await ctx.send(embed=embed)

    @modrinth.command(name="info")
    async def project_info(self, ctx, project_id: str):
        """Show detailed information about a monitored project."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored in this server.")
            return

        project = config.projects[project_id]

        try:
            project_info = await self.api.get_project(project_id)
            versions = await self.api.get_project_versions(project_id, limit=1)

            embed = create_project_info_embed(project_info)

            if versions:
                latest = versions[0]
                embed.add_field(
                    name="Latest Version",
                    value=f"{latest.version_number} ({latest.version_type})",
                    inline=True
                )

            # Add monitoring info
            channels_info = []
            for channel_id, monitor in project.channels.items():
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    filters = []
                    if monitor.required_loaders:
                        filters.append(f"Loaders: {', '.join(monitor.required_loaders)}")
                    if monitor.required_game_versions:
                        filters.append(f"Versions: {', '.join(monitor.required_game_versions)}")

                    channel_info = channel.mention
                    if filters:
                        channel_info += f" ({'; '.join(filters)})"
                    channels_info.append(channel_info)

            if channels_info:
                embed.add_field(
                    name="Monitoring Channels",
                    value="\n".join(channels_info),
                    inline=False
                )

            await ctx.send(embed=embed)

        except ModrinthAPIError as e:
            await ctx.send(f"❌ Error fetching project info: {e}")

    @modrinth.command(name="test")
    async def test_project(self, ctx, project_id: str):
        """Force check for updates and send the latest version."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored in this server.")
            return

        project = config.projects[project_id]

        async with ctx.typing():
            try:
                project_info = await self.api.get_project(project_id)
                versions = await self.api.get_project_versions(project_id, limit=1)

                if not versions:
                    await ctx.send(f"❌ No versions found for project `{project_id}`.")
                    return

                latest_version = versions[0]

                # Send test notifications to all monitored channels
                for channel_id, monitor in project.channels.items():
                    channel = ctx.guild.get_channel(channel_id)
                    if not channel:
                        continue

                    # Check if version matches filters
                    if not latest_version.matches_filters(monitor.required_loaders, monitor.required_game_versions):
                        await ctx.send(f"⚠️ Latest version doesn't match filters for {channel.mention}")
                        continue

                    embed = create_update_embed(
                        project_info,
                        latest_version,
                        monitor,
                        is_initial=True,
                        title_prefix="🧪 Test: "
                    )

                    await channel.send(embed=embed)

                await ctx.send(f"✅ Test notifications sent for **{project_info.name}**")

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error testing project: {e}")

    @modrinth.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def remove_project(self, ctx, project_id: str, channel: discord.TextChannel = None):
        """Remove a project from monitoring."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored.")
            return

        project = config.projects[project_id]

        if channel:
            # Remove from specific channel
            if channel.id in project.channels:
                del project.channels[channel.id]
                await ctx.send(f"✅ Stopped monitoring **{project.name}** in {channel.mention}")

                # Remove project entirely if no channels left
                if not project.channels:
                    del config.projects[project_id]
                    await ctx.send(f"✅ Completely removed **{project.name}** from monitoring (no channels left)")
            else:
                await ctx.send(f"❌ Project `{project_id}` is not being monitored in {channel.mention}")
                return
        else:
            # Remove from all channels
            del config.projects[project_id]
            await ctx.send(f"✅ Completely removed **{project.name}** from monitoring")

        await self._save_guild_config(ctx.guild.id)

    @modrinth.command(name="edit")
    @commands.admin_or_permissions(manage_guild=True)
    async def edit_project(self, ctx, project_id: str, channel: discord.TextChannel):
        """Edit monitoring settings for a project in a specific channel."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored.")
            return

        project = config.projects[project_id]

        if channel.id not in project.channels:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored in {channel.mention}")
            return

        monitor = project.channels[channel.id]

        # Show current settings
        embed = discord.Embed(
            title=f"Current Settings for {project.name}",
            description=f"Channel: {channel.mention}",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Loaders",
            value=", ".join(monitor.required_loaders) if monitor.required_loaders else "All",
            inline=True
        )
        embed.add_field(
            name="Game Versions",
            value=", ".join(monitor.required_game_versions) if monitor.required_game_versions else "All",
            inline=True
        )

        roles = []
        for role_id in monitor.role_ids:
            role = ctx.guild.get_role(role_id)
            if role:
                roles.append(role.mention)

        embed.add_field(
            name="Pinged Roles",
            value=", ".join(roles) if roles else "None",
            inline=True
        )

        await ctx.send(embed=embed)
        await ctx.send("Use the interactive add command to reconfigure this project.")

    # Personal watchlist commands
    @modrinth.group(name="watch", aliases=["personal"])
    async def personal_watch(self, ctx):
        """Personal watchlist commands (DM notifications)."""
        pass

    @personal_watch.command(name="add")
    async def add_personal_watch(self, ctx, project_id: str):
        """Add a project to your personal watchlist."""
        config = self._get_user_config(ctx.author.id)

        if project_id in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is already in your personal watchlist.")
            return

        async with ctx.typing():
            try:
                project_info = await self.api.get_project(project_id)

                project = MonitoredProject(
                    id=project_id,
                    name=project_info.name,
                    added_by=ctx.author.id
                )

                config.projects[project_id] = project
                await self._save_user_config(ctx.author.id)

                await ctx.send(f"✅ Added **{project_info.name}** to your personal watchlist. You'll receive DM notifications for updates.")

            except ProjectNotFoundError:
                await ctx.send(f"❌ Project `{project_id}` not found on Modrinth.")
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error adding project: {e}")

    @personal_watch.command(name="list")
    async def list_personal_watch(self, ctx):
        """List your personal watchlist."""
        config = self._get_user_config(ctx.author.id)

        if not config.projects:
            await ctx.send("❌ Your personal watchlist is empty.")
            return

        embed = discord.Embed(
            title="Your Personal Watchlist",
            color=discord.Color.blue()
        )

        for project_id, project in config.projects.items():
            embed.add_field(
                name=project.name,
                value=f"ID: `{project_id}`",
                inline=True
            )

        await ctx.send(embed=embed)

    @personal_watch.command(name="remove")
    async def remove_personal_watch(self, ctx, project_id: str):
        """Remove a project from your personal watchlist."""
        config = self._get_user_config(ctx.author.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not in your personal watchlist.")
            return

        project = config.projects[project_id]
        del config.projects[project_id]
        await self._save_user_config(ctx.author.id)

        await ctx.send(f"✅ Removed **{project.name}** from your personal watchlist.")