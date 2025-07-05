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
                if not latest_version.matches_filters(monitor.required_loaders, monitor.required_game_versions, monitor.required_version_types):
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

        # Fetch project details and ALL supported versions/loaders (including beta/alpha)
        async with ctx.typing():
            try:
                # Get full project details
                project_info = await self.api.get_project(selected_project.id)

                # Get ALL versions to determine supported loaders and game versions
                all_versions = await self.api.get_all_project_versions(selected_project.id)

                # Extract unique loaders and game versions across ALL channels
                supported_loaders = set()
                supported_game_versions = set()

                for version in all_versions:
                    supported_loaders.update(version.loaders)
                    supported_game_versions.update(version.game_versions)

                # Convert to sorted lists
                supported_loaders = sorted(list(supported_loaders))
                # Sort game versions with latest first (reverse chronological)
                supported_game_versions = sorted(list(supported_game_versions), reverse=True)

                if not supported_loaders or not supported_game_versions:
                    await ctx.send(f"❌ Could not determine supported loaders or game versions for this project.")
                    return

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error fetching project details: {e}")
                return

        # Start interactive session with project support info
        session = {
            'project': project_info,
            'supported_loaders': supported_loaders,
            'supported_game_versions': supported_game_versions,
            'all_versions': all_versions,  # Store for release channel validation
            'step': 'confirm_project',
            'user_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'guild_id': ctx.guild.id
        }
        self._interactive_sessions[ctx.author.id] = session

        # Show project confirmation
        await self._show_project_confirmation(ctx, project_info, supported_loaders, supported_game_versions)

    async def _show_project_confirmation(self, ctx, project: ProjectInfo, supported_loaders: List[str], supported_game_versions: List[str]):
        """Show project confirmation step with support info."""
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
        embed.add_field(name="Project ID", value=f"`{project.id}`", inline=True)

        # Show supported loaders
        loaders_str = ", ".join(supported_loaders) if supported_loaders else "None"
        embed.add_field(name="Supported Loaders", value=loaders_str, inline=False)

        # Show latest 10 game versions
        latest_versions = supported_game_versions[:10]
        if len(supported_game_versions) > 10:
            version_display = ", ".join(latest_versions) + f" (+{len(supported_game_versions) - 10} more)"
        else:
            version_display = ", ".join(latest_versions)

        embed.add_field(name="Supported Game Versions", value=version_display, inline=False)
        embed.add_field(name="Description", value=project.description[:300] + ("..." if len(project.description) > 300 else ""), inline=False)

        embed.set_footer(text="Click the link above to view on Modrinth. React with ✅ to confirm or ❌ to cancel")

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
        """Ask for Minecraft version filtering with project-specific versions."""
        session = self._interactive_sessions[ctx.author.id]
        supported_versions = session['supported_game_versions']

        embed = discord.Embed(
            title="Minecraft Version Filter",
            description="Which Minecraft versions should be monitored?",
            color=discord.Color.blue()
        )

        # Show latest 8 supported versions as examples
        latest_examples = supported_versions[:8]
        examples_text = ", ".join(latest_examples)
        if len(supported_versions) > 8:
            examples_text += f" (and {len(supported_versions) - 8} more)"

        embed.add_field(
            name="Supported Versions",
            value=examples_text,
            inline=False
        )

        embed.add_field(
            name="Options",
            value="1️⃣ All supported versions\n2️⃣ Specific versions (you'll specify)\n3️⃣ Latest major version only",
            inline=False
        )

        embed.add_field(
            name="📝 Note",
            value="Choose versions that the mod actually supports. Selecting unsupported versions will result in no notifications.",
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
                session['minecraft_versions'] = None  # All versions
                await self._ask_loader_type(ctx)
            elif str(reaction.emoji) == "2️⃣":
                session['step'] = 'specify_versions'
                await ctx.send(f"Please specify the Minecraft versions you want to monitor.\n\n**Supported versions:** {', '.join(supported_versions[:15])}{'...' if len(supported_versions) > 15 else ''}\n\n**Format:** comma-separated (e.g., `{supported_versions[0]}, {supported_versions[1] if len(supported_versions) > 1 else supported_versions[0]}`)\n**Single version:** just type the version (e.g., `{supported_versions[0]}`)")
            elif str(reaction.emoji) == "3️⃣":
                # Use the latest version
                latest_version = supported_versions[0] if supported_versions else "1.21"
                session['minecraft_versions'] = [latest_version]
                await self._ask_loader_type(ctx)

        except asyncio.TimeoutError:
            await msg.edit(content="❌ Selection timed out.", embed=None)
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _ask_loader_type(self, ctx):
        """Ask for loader type filtering with project-specific loaders."""
        session = self._interactive_sessions[ctx.author.id]
        supported_loaders = session['supported_loaders']

        embed = discord.Embed(
            title="Loader Type Filter",
            description="Which mod loaders should be monitored?",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Supported Loaders",
            value=", ".join(supported_loaders) if supported_loaders else "None specified",
            inline=False
        )

        # Build options based on what's actually supported
        options = ["1️⃣ All supported loaders"]
        reactions = ["1️⃣"]

        # Add specific loader options only if they're supported
        loader_map = {}
        option_num = 2

        common_loaders = ["fabric", "forge", "neoforge", "quilt"]
        for loader in common_loaders:
            if loader in supported_loaders:
                emoji = f"{option_num}️⃣"
                options.append(f"{emoji} {loader.title()} only")
                reactions.append(emoji)
                loader_map[emoji] = [loader]
                option_num += 1

        # Add custom selection option if there are multiple loaders
        if len(supported_loaders) > 1:
            custom_emoji = f"{option_num}️⃣"
            options.append(f"{custom_emoji} Custom selection")
            reactions.append(custom_emoji)

        embed.add_field(
            name="Options",
            value="\n".join(options),
            inline=False
        )

        embed.add_field(
            name="📝 Note",
            value="Choose loaders that the mod actually supports. Selecting unsupported loaders will result in no notifications.",
            inline=False
        )

        msg = await ctx.send(embed=embed)
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
                session['loaders'] = None  # All loaders
                await self._ask_release_channel(ctx)
            elif str(reaction.emoji) in loader_map:
                session['loaders'] = loader_map[str(reaction.emoji)]
                await self._ask_release_channel(ctx)
            elif str(reaction.emoji) == reactions[-1] and len(supported_loaders) > 1:  # Custom selection
                session['step'] = 'specify_loaders'
                await ctx.send(f"Please specify the loaders you want to monitor.\n\n**Supported loaders:** {', '.join(supported_loaders)}\n\n**Format:** comma-separated (e.g., `{supported_loaders[0]}{', ' + supported_loaders[1] if len(supported_loaders) > 1 else ''}`)\n**Single loader:** just type the loader name (e.g., `{supported_loaders[0]}`)")

        except asyncio.TimeoutError:
            await msg.edit(content="❌ Selection timed out.", embed=None)
            self._interactive_sessions.pop(ctx.author.id, None)

    async def _ask_release_channel(self, ctx):
        """Ask for release channel filtering with validation."""
        session = self._interactive_sessions[ctx.author.id]

        # Check what release types are available for the selected versions/loaders
        available_types = set()
        minecraft_versions = session.get('minecraft_versions')
        loaders = session.get('loaders')

        for version in session['all_versions']:
            # Check if this version matches our filters
            version_matches = True

            if minecraft_versions:
                if not any(mc_version in version.game_versions for mc_version in minecraft_versions):
                    version_matches = False

            if loaders:
                if not any(loader in version.loaders for loader in loaders):
                    version_matches = False

            if version_matches:
                available_types.add(version.version_type)

        embed = discord.Embed(
            title="Release Channel Filter",
            description="Which release channels should be monitored?",
            color=discord.Color.blue()
        )

        # Show what's available for the selected filters
        if available_types:
            embed.add_field(
                name="Available for your filters",
                value=", ".join(sorted(available_types)),
                inline=False
            )

        embed.add_field(
            name="Options",
            value="1️⃣ All channels\n2️⃣ Release only\n3️⃣ Beta and Release\n4️⃣ Alpha, Beta, and Release",
            inline=False
        )

        # Add warning if there might be conflicts
        warnings = []
        channel_map = {
            "2️⃣": ["release"],
            "3️⃣": ["release", "beta"],
            "4️⃣": ["release", "beta", "alpha"]
        }

        for emoji, types in channel_map.items():
            if not any(t in available_types for t in types):
                option_name = {
                    "2️⃣": "Release only",
                    "3️⃣": "Beta and Release",
                    "4️⃣": "All channels"
                }[emoji]
                warnings.append(f"⚠️ {option_name}: No matching versions found for your filters")

        if warnings:
            embed.add_field(
                name="⚠️ Warnings",
                value="\n".join(warnings),
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

            channel_selection_map = {
                "1️⃣": None,
                "2️⃣": ["release"],
                "3️⃣": ["release", "beta"],
                "4️⃣": ["release", "beta", "alpha"]
            }

            selected_types = channel_selection_map[str(reaction.emoji)]
            session['release_channels'] = selected_types

            # Show warning if no versions match the selected criteria
            if selected_types and not any(t in available_types for t in selected_types):
                await ctx.send("⚠️ **Warning:** No versions found matching your selected release channels and filters. You may not receive any notifications until the mod releases versions matching your criteria.")

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
            value="Please mention the channel (e.g., #updates) or type `current` to use the current channel:",
            inline=False
        )

        await ctx.send(embed=embed)

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            msg = await self.bot.wait_for('message', timeout=60.0, check=check)

            session = self._interactive_sessions[ctx.author.id]

            if msg.content.lower().strip() == 'current':
                session['notification_channel'] = ctx.channel
            elif msg.channel_mentions:
                session['notification_channel'] = msg.channel_mentions[0]
            else:
                await ctx.send("❌ Invalid channel. Please mention a channel (e.g., #updates) or type `current`.")
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
            value="Mention the roles you want to ping (e.g., @Mod Updates @Everyone) or type `none` for no pings:",
            inline=False
        )

        await ctx.send(embed=embed)

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            msg = await self.bot.wait_for('message', timeout=60.0, check=check)

            session = self._interactive_sessions[ctx.author.id]

            if msg.content.lower().strip() == 'none':
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
            required_game_versions=session.get('minecraft_versions'),
            required_version_types=session.get('release_channels')
        )

        monitored_project.channels[session['notification_channel'].id] = channel_monitor

        # Save configuration
        await self._save_guild_config(ctx.guild.id)

        # Send confirmation
        embed = discord.Embed(
            title="✅ Monitoring Setup Complete",
            description=f"Successfully set up monitoring for **{project.name}**",
            color=discord.Color.green()
        )

        embed.add_field(name="Channel", value=session['notification_channel'].mention, inline=True)
        embed.add_field(name="Roles", value=humanize_list([role.mention for role in session['roles']]) if session['roles'] else "None", inline=True)

        if session.get('minecraft_versions'):
            embed.add_field(name="Minecraft Versions", value=", ".join(session['minecraft_versions']), inline=False)
        else:
            embed.add_field(name="Minecraft Versions", value="All supported versions", inline=False)

        if session.get('loaders'):
            embed.add_field(name="Loaders", value=", ".join(session['loaders']), inline=False)
        else:
            embed.add_field(name="Loaders", value="All supported loaders", inline=False)

        if session.get('release_channels'):
            embed.add_field(name="Release Channels", value=", ".join(session['release_channels']), inline=False)
        else:
            embed.add_field(name="Release Channels", value="All channels", inline=False)

        await ctx.send(embed=embed)

        # Send initial version to confirm monitoring is working
        try:
            versions = await self.api.get_project_versions(project.id, limit=5)
            if versions:
                # Find the first version that matches filters
                matching_version = None
                for version in versions:
                    if version.matches_filters(
                        session.get('loaders'),
                        session.get('minecraft_versions'),
                        session.get('release_channels')
                    ):
                        matching_version = version
                        break

                if matching_version:
                    update_embed = create_update_embed(
                        project,
                        matching_version,
                        channel_monitor,
                        is_initial=True
                    )

                    content = None
                    if session['roles']:
                        content = " ".join([role.mention for role in session['roles']])

                    await session['notification_channel'].send(content=content, embed=update_embed)

                    # Update last version
                    monitored_project.last_version = matching_version.id
                    await self._save_guild_config(ctx.guild.id)
                else:
                    await ctx.send("⚠️ **Note:** No existing versions match your filter criteria. You'll receive notifications when new matching versions are released.")
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
                # Clean up the input
                versions_input = message.content.strip()

                # Handle single version or comma-separated versions
                if ',' in versions_input:
                    versions = [v.strip() for v in versions_input.split(',')]
                else:
                    versions = [versions_input.strip()]

                # Remove empty strings
                versions = [v for v in versions if v]

                if not versions:
                    await message.channel.send("❌ Please specify at least one version.")
                    return

                supported_versions = session['supported_game_versions']

                # Validate that all specified versions are supported
                invalid_versions = [v for v in versions if v not in supported_versions]
                if invalid_versions:
                    await message.channel.send(
                        f"❌ **Invalid versions:** {', '.join(invalid_versions)}\n\n**Supported versions:** {', '.join(supported_versions[:15])}{'...' if len(supported_versions) > 15 else ''}\n\nPlease try again with supported versions only.")
                    return

                session['minecraft_versions'] = versions
                session['step'] = None  # Clear the step
                await self._ask_loader_type(message.channel)
                return  # ADD THIS LINE - This was missing!

            except Exception as e:
                log.error(f"Error processing versions: {e}")
                await message.channel.send(
                    "❌ Invalid format. Please use comma-separated versions (e.g., `1.21.4, 1.21.3`) or a single version (e.g., `1.21.4`).")
                return

        # Handle loader specification
        elif session.get('step') == 'specify_loaders':
            try:
                # Clean up the input
                loaders_input = message.content.strip().lower()

                # Handle single loader or comma-separated loaders
                if ',' in loaders_input:
                    loaders = [l.strip() for l in loaders_input.split(',')]
                else:
                    loaders = [loaders_input.strip()]

                # Remove empty strings
                loaders = [l for l in loaders if l]

                if not loaders:
                    await message.channel.send("❌ Please specify at least one loader.")
                    return

                supported_loaders = session['supported_loaders']

                # Validate that all specified loaders are supported
                invalid_loaders = [l for l in loaders if l not in supported_loaders]
                if invalid_loaders:
                    await message.channel.send(
                        f"❌ **Invalid loaders:** {', '.join(invalid_loaders)}\n\n**Supported loaders:** {', '.join(supported_loaders)}\n\nPlease try again with supported loaders only.")
                    return

                session['loaders'] = loaders
                session['step'] = None  # Clear the step
                await self._ask_release_channel(message.channel)
                return  # ADD THIS LINE - This was missing too!

            except Exception as e:
                log.error(f"Error processing loaders: {e}")
                await message.channel.send(
                    "❌ Invalid format. Please use comma-separated loaders (e.g., `fabric, forge`) or a single loader (e.g., `fabric`).")
                return

    # Additional commands
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
                    filter_info = []
                    if monitor.required_game_versions:
                        filter_info.append(f"MC: {', '.join(monitor.required_game_versions[:3])}{'...' if len(monitor.required_game_versions) > 3 else ''}")
                    if monitor.required_loaders:
                        filter_info.append(f"Loaders: {', '.join(monitor.required_loaders)}")
                    if monitor.required_version_types:
                        filter_info.append(f"Types: {', '.join(monitor.required_version_types)}")

                    filter_str = f" ({'; '.join(filter_info)})" if filter_info else ""
                    channels.append(f"{channel.mention}{filter_str}")

            embed.add_field(
                name=project.name,
                value=f"ID: `{project_id}`\nChannels: {chr(10).join(channels) if channels else 'None'}",
                inline=False
            )

        await ctx.send(embed=embed)

    @modrinth.command(name="remove", aliases=["rm", "delete"])
    @commands.admin_or_permissions(manage_guild=True)
    async def remove_project(self, ctx, project_id: str):
        """Remove a project from monitoring."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored in this server.")
            return

        project_name = config.projects[project_id].name
        del config.projects[project_id]
        await self._save_guild_config(ctx.guild.id)

        await ctx.send(f"✅ Removed **{project_name}** from monitoring.")

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
                versions = await self.api.get_project_versions(project_id, limit=5)

                if not versions:
                    await ctx.send(f"❌ No versions found for project `{project_id}`.")
                    return

                # Send test notifications to all monitored channels
                notifications_sent = 0
                for channel_id, monitor in project.channels.items():
                    channel = ctx.guild.get_channel(channel_id)
                    if not channel:
                        continue

                    # Find a version that matches filters
                    matching_version = None
                    for version in versions:
                        if version.matches_filters(
                            monitor.required_loaders,
                            monitor.required_game_versions,
                            monitor.required_version_types
                        ):
                            matching_version = version
                            break

                    if not matching_version:
                        await ctx.send(f"⚠️ No versions match the filters for {channel.mention}")
                        continue

                    embed = create_update_embed(
                        project_info,
                        matching_version,
                        monitor,
                        is_initial=True,
                        title_prefix="🧪 Test: "
                    )

                    await channel.send(embed=embed)
                    notifications_sent += 1

                if notifications_sent > 0:
                    await ctx.send(f"✅ Test notifications sent for **{project_info.name}** to {notifications_sent} channel(s)")
                else:
                    await ctx.send(f"❌ No test notifications sent - no versions match any channel filters")

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error testing project: {e}")

    @modrinth.command(name="search")
    async def search_projects(self, ctx, *, query: str):
        """Search for projects on Modrinth."""
        async with ctx.typing():
            try:
                results = await self.api.search_projects(query, limit=10)
                if not results:
                    await ctx.send(f"❌ No projects found matching '{query}'.")
                    return
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error searching: {e}")
                return

        embed = discord.Embed(
            title=f"Search Results for '{query}'",
            color=discord.Color.blue()
        )

        for project in results[:5]:
            embed.add_field(
                name=f"{project.name} ({project.project_type})",
                value=f"ID: `{project.id}`\nDownloads: {project.downloads:,}\n{project.description[:100]}...",
                inline=False
            )

        await ctx.send(embed=embed)