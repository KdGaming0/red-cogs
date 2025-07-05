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
from .models import (ProjectInfo, VersionInfo, ChannelMonitor, MonitoredProject,
                    GuildConfig, extract_minecraft_version)
from .utils import create_update_embed, create_project_info_embed, get_valid_loaders

log = logging.getLogger("red.modrinthnotifier")

class ModrinthNotifier(commands.Cog):
    """Monitor Modrinth projects for updates with enhanced features."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.api = ModrinthAPI()
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)

        default_guild = {
            "projects": {},
            "enabled": True,
            "poll_interval": 300
        }

        self.config.register_guild(**default_guild)

        self._poll_task: Optional[asyncio.Task] = None
        self._guild_configs: Dict[int, GuildConfig] = {}
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
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            self._guild_configs[guild_id] = GuildConfig.from_dict(data)

    async def _save_guild_config(self, guild_id: int):
        """Save guild configuration to storage."""
        config = self._guild_configs.get(guild_id, GuildConfig())
        await self.config.guild_from_id(guild_id).set(config.to_dict())

    def _get_guild_config(self, guild_id: int) -> GuildConfig:
        """Get or create guild configuration."""
        if guild_id not in self._guild_configs:
            self._guild_configs[guild_id] = GuildConfig()
        return self._guild_configs[guild_id]

    def _get_user_from_context(self, ctx) -> Optional[int]:
        """Extract user ID from context, handling both command context and channel objects."""
        if hasattr(ctx, 'author'):
            return ctx.author.id
        else:
            # ctx is a channel, find the session by channel ID
            for user_id, session in self._interactive_sessions.items():
                if session.get('channel_id') == ctx.id:
                    return user_id
            return None

    async def _poll_loop(self):
        """Main polling loop for checking updates."""
        await self.bot.wait_until_red_ready()

        while True:
            try:
                await self._check_all_updates()
                await asyncio.sleep(300)  # Poll every 5 minutes
            except Exception as e:
                log.error(f"Error in polling loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_all_updates(self):
        """Check for updates on all monitored projects."""
        for guild_id, config in self._guild_configs.items():
            if not config.enabled:
                continue

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            for project_id, project in config.projects.items():
                await self._check_guild_project_updates(guild, project)

    async def _check_guild_project_updates(self, guild: discord.Guild, project: MonitoredProject):
        """Check for updates on a guild project."""
        try:
            # Send notifications to all monitored channels
            for channel_id, monitor in project.channels.items():
                channel = guild.get_channel(channel_id)
                if not channel:
                    continue

                # Handle latest supported version monitoring
                minecraft_versions = monitor.required_game_versions
                if monitor.use_latest_supported:
                    latest_supported = await self.api.get_latest_supported_minecraft_version(project.id)
                    if latest_supported:
                        # Update the monitor to use the latest supported version
                        minecraft_versions = [latest_supported]
                        monitor.required_game_versions = [latest_supported]

                # Get versions with filters
                versions = await self.api.get_project_versions(
                    project.id,
                    limit=1,
                    loaders=monitor.required_loaders,
                    game_versions=minecraft_versions
                )

                if not versions:
                    continue

                latest_version = versions[0]

                # Check if this is a new version for this specific monitor
                if monitor.last_version and latest_version.id == monitor.last_version:
                    continue

                # Double-check version matches filters
                if not latest_version.matches_filters(
                    monitor.required_loaders,
                    minecraft_versions,
                    monitor.required_version_types
                ):
                    continue

                project_info = await self.api.get_project(project.id)
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

                    # Update last version for this specific monitor
                    monitor.last_version = latest_version.id

                except discord.HTTPException as e:
                    log.error(f"Failed to send update to {guild.name}#{channel.name}: {e}")

            # Save config after all updates
            if project.channels:  # Only save if there are still channels
                await self._save_guild_config(guild.id)

        except Exception as e:
            log.error(f"Error checking guild project {project.id}: {e}")

    @commands.group(name="modrinth", aliases=["mr"])
    async def modrinth(self, ctx):
        """Modrinth update notifications with enhanced features."""
        pass

    @modrinth.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def add_project_interactive(self, ctx, *, project_name: str):
        """Add a project to monitoring with interactive setup."""
        # Check if project already exists
        config = self._get_guild_config(ctx.guild.id)

        # Search for projects first to check for duplicates
        async with ctx.typing():
            try:
                search_results = await self.api.search_projects(project_name, limit=10)
                if not search_results:
                    await ctx.send(f"❌ No projects found matching '{project_name}'.")
                    return
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error searching for projects: {e}")
                return

        # Check if any of the search results are already being monitored
        existing_projects = []
        for result in search_results:
            if result.id in config.projects:
                existing_projects.append(result)

        # If there are existing projects, show warning
        if existing_projects:
            embed = discord.Embed(
                title="⚠️ Project Already Monitored",
                description="The following projects are already being monitored in this server:",
                color=discord.Color.orange()
            )

            for project in existing_projects:
                channels = []
                monitored_project = config.projects[project.id]
                for channel_id in monitored_project.channels.keys():
                    channel = ctx.guild.get_channel(channel_id)
                    if channel:
                        channels.append(channel.mention)

                embed.add_field(
                    name=project.name,
                    value=f"ID: `{project.id}`\nChannels: {', '.join(channels) if channels else 'None'}",
                    inline=False
                )

            embed.add_field(
                name="Available Commands",
                value=f"`{ctx.prefix}modrinth remove {existing_projects[0].id}` - Remove completely\n"
                      f"`{ctx.prefix}modrinth remove {existing_projects[0].id} #channel` - Remove from specific channel\n"
                      f"`{ctx.prefix}modrinth list` - View all monitored projects",
                inline=False
            )

            await ctx.send(embed=embed)
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

        # Fetch project details and ALL supported versions/loaders
        async with ctx.typing():
            try:
                project_info = await self.api.get_project(selected_project.id)
                all_versions = await self.api.get_all_project_versions(selected_project.id)

                # Extract unique loaders and game versions from ALL versions (all release channels)
                supported_loaders = set()
                supported_game_versions = set()

                for version in all_versions:
                    supported_loaders.update(version.loaders)
                    supported_game_versions.update(version.game_versions)

                # Convert to sorted lists
                supported_loaders = sorted(list(supported_loaders))
                supported_game_versions = sorted(list(supported_game_versions), reverse=True)

                # Get both latest versions for different options
                latest_supported_version = await self.api.get_latest_supported_minecraft_version(selected_project.id)
                current_latest_version = await self.api.get_current_latest_supported_minecraft_version(selected_project.id)

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error fetching project details: {e}")
                return

        # Start interactive session
        session = {
            'project': project_info,
            'supported_loaders': supported_loaders,
            'supported_game_versions': supported_game_versions,
            'latest_supported_version': latest_supported_version,
            'current_latest_version': current_latest_version,
            'step': 'confirm_project',
            'user_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'guild_id': ctx.guild.id
        }
        self._interactive_sessions[ctx.author.id] = session

        # Show project confirmation
        await self._show_project_confirmation(ctx, project_info, supported_loaders, supported_game_versions, latest_supported_version)

    async def _show_project_confirmation(self, ctx, project: ProjectInfo, supported_loaders: List[str], supported_game_versions: List[str], latest_supported_version: Optional[str]):
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
        embed.add_field(name="Supported Loaders", value=", ".join(supported_loaders) if supported_loaders else "None", inline=False)

        # Show latest 10 game versions
        latest_versions = supported_game_versions[:10]
        if len(supported_game_versions) > 10:
            version_display = ", ".join(latest_versions) + f" (+{len(supported_game_versions) - 10} more)"
        else:
            version_display = ", ".join(latest_versions)

        embed.add_field(name="Supported Game Versions", value=version_display, inline=False)

        if latest_supported_version:
            embed.add_field(name="Latest Supported Version", value=latest_supported_version, inline=True)

        embed.add_field(name="Description", value=project.description[:300] + ("..." if len(project.description) > 300 else ""), inline=False)
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
        user_id = self._get_user_from_context(ctx)
        if not user_id:
            return

        session = self._interactive_sessions.get(user_id)
        if not session:
            return

        supported_versions = session['supported_game_versions']
        latest_supported = session.get('latest_supported_version')
        current_latest = session.get('current_latest_version')

        embed = discord.Embed(
            title="Minecraft Version Filter",
            description="Choose how to filter Minecraft versions for notifications:",
            color=discord.Color.blue()
        )

        latest_examples = supported_versions[:5]
        examples_text = ", ".join(latest_examples)
        if len(supported_versions) > 5:
            examples_text += f" (and {len(supported_versions) - 5} more)"

        embed.add_field(
            name="Supported Versions",
            value=examples_text,
            inline=False
        )

        options = [
            "1️⃣ **All supported versions** - Notify for any update regardless of MC version",
            "2️⃣ **Specific versions** - Choose exact MC versions to monitor",
            f"3️⃣ **Latest current version only** - Only {current_latest or 'latest'} (fixed at setup time)",
        ]
        reactions = ["1️⃣", "2️⃣", "3️⃣"]

        if latest_supported:
            options.append(f"4️⃣ **Latest version supported by mod** - Always track the newest MC version the mod supports (currently {latest_supported})")
            reactions.append("4️⃣")

        embed.add_field(
            name="Options",
            value="\n".join(options),
            inline=False
        )

        embed.add_field(
            name="Explanation",
            value="• **Option 1**: Gets notified for every update\n"
                  "• **Option 2**: Only gets notified for updates to specific MC versions\n"
                  "• **Option 3**: Only gets notified for updates to the current latest MC version\n"
                  "• **Option 4**: Dynamically tracks the newest MC version the mod supports",
            inline=False
        )

        msg = await ctx.send(embed=embed)
        for reaction in reactions:
            await msg.add_reaction(reaction)

        def check(reaction, user):
            return (user.id == user_id and
                   str(reaction.emoji) in reactions and
                   reaction.message.id == msg.id)

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
            await msg.delete()

            session = self._interactive_sessions[user_id]

            if str(reaction.emoji) == "1️⃣":
                session['minecraft_versions'] = None
                session['use_latest_supported'] = False
                await self._ask_loader_type(ctx)
            elif str(reaction.emoji) == "2️⃣":
                session['step'] = 'specify_versions'
                session['use_latest_supported'] = False
                await ctx.send(f"Please specify the Minecraft versions you want to monitor.\nSupported: {', '.join(supported_versions[:10])}{'...' if len(supported_versions) > 10 else ''}\nFormat: comma-separated or single version (e.g., `{supported_versions[0]}` or `{supported_versions[0]}, {supported_versions[1] if len(supported_versions) > 1 else supported_versions[0]}`):")
            elif str(reaction.emoji) == "3️⃣":
                current_version = current_latest or supported_versions[0] if supported_versions else "1.21"
                session['minecraft_versions'] = [current_version]
                session['use_latest_supported'] = False
                await self._ask_loader_type(ctx)
            elif str(reaction.emoji) == "4️⃣" and latest_supported:
                session['minecraft_versions'] = [latest_supported]
                session['use_latest_supported'] = True
                await self._ask_loader_type(ctx)

        except asyncio.TimeoutError:
            await msg.edit(content="❌ Selection timed out.", embed=None)
            self._interactive_sessions.pop(user_id, None)

    async def _ask_loader_type(self, ctx):
        """Ask for loader type filtering."""
        user_id = self._get_user_from_context(ctx)
        if not user_id:
            return

        session = self._interactive_sessions.get(user_id)
        if not session:
            return

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

        options = ["1️⃣ All supported loaders"]
        reactions = ["1️⃣"]

        loader_map = {}
        option_num = 2

        for loader in ["fabric", "forge", "neoforge", "quilt"]:
            if loader in supported_loaders:
                emoji = f"{option_num}️⃣"
                options.append(f"{emoji} {loader.title()} only")
                reactions.append(emoji)
                loader_map[emoji] = [loader]
                option_num += 1

        custom_emoji = f"{option_num}️⃣"
        options.append(f"{custom_emoji} Custom selection")
        reactions.append(custom_emoji)

        embed.add_field(
            name="Options",
            value="\n".join(options),
            inline=False
        )

        msg = await ctx.send(embed=embed)
        for reaction in reactions:
            await msg.add_reaction(reaction)

        def check(reaction, user):
            return (user.id == user_id and
                   str(reaction.emoji) in reactions and
                   reaction.message.id == msg.id)

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
            await msg.delete()

            session = self._interactive_sessions[user_id]

            if str(reaction.emoji) == "1️⃣":
                session['loaders'] = None
                await self._ask_release_channel(ctx)
            elif str(reaction.emoji) in loader_map:
                session['loaders'] = loader_map[str(reaction.emoji)]
                await self._ask_release_channel(ctx)
            elif str(reaction.emoji) == custom_emoji:
                session['step'] = 'specify_loaders'
                await ctx.send(f"Please specify the loaders you want to monitor.\nSupported: {', '.join(supported_loaders)}\nFormat: comma-separated or single loader (e.g., `{supported_loaders[0]}` or `{supported_loaders[0]}, {supported_loaders[1] if len(supported_loaders) > 1 else ''}`):")

        except asyncio.TimeoutError:
            await msg.edit(content="❌ Selection timed out.", embed=None)
            self._interactive_sessions.pop(user_id, None)

    async def _ask_release_channel(self, ctx):
        """Ask for release channel filtering."""
        user_id = self._get_user_from_context(ctx)
        if not user_id:
            return

        session = self._interactive_sessions.get(user_id)
        if not session:
            return

        # Check what release types are actually available for the filtered criteria
        async with ctx.typing():
            try:
                # Get some recent versions to check what release types exist
                test_versions = await self.api.get_project_versions(
                    session['project'].id,
                    limit=50,
                    loaders=session.get('loaders'),
                    game_versions=session.get('minecraft_versions')
                )

                available_types = set()
                for version in test_versions:
                    available_types.add(version.version_type)

                available_types = sorted(list(available_types))

            except Exception:
                available_types = ["release", "beta", "alpha"]  # Fallback

        embed = discord.Embed(
            title="Release Channel Filter",
            description="Which release channels should be monitored?",
            color=discord.Color.blue()
        )

        if available_types:
            embed.add_field(
                name="Available Release Types",
                value=", ".join(available_types),
                inline=False
            )

        embed.add_field(
            name="Options",
            value="1️⃣ All channels\n2️⃣ Release only\n3️⃣ Beta and Release\n4️⃣ Alpha, Beta, and Release",
            inline=False
        )

        # Add warning if specific combinations might not work
        if session.get('minecraft_versions') and "release" not in available_types:
            embed.add_field(
                name="⚠️ Warning",
                value="The selected Minecraft version(s) may only have beta/alpha releases available.",
                inline=False
            )

        msg = await ctx.send(embed=embed)
        reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
        for reaction in reactions:
            await msg.add_reaction(reaction)

        def check(reaction, user):
            return (user.id == user_id and
                   str(reaction.emoji) in reactions and
                   reaction.message.id == msg.id)

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
            await msg.delete()

            session = self._interactive_sessions[user_id]

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
            self._interactive_sessions.pop(user_id, None)

    async def _ask_notification_channel(self, ctx):
        """Ask for notification channel."""
        user_id = self._get_user_from_context(ctx)
        if not user_id:
            return

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
            return message.author.id == user_id and message.channel.id == ctx.channel.id

        try:
            msg = await self.bot.wait_for('message', timeout=60.0, check=check)

            session = self._interactive_sessions[user_id]

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
            self._interactive_sessions.pop(user_id, None)

    async def _ask_role_pings(self, ctx):
        """Ask for role pings."""
        user_id = self._get_user_from_context(ctx)
        if not user_id:
            return

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
            return message.author.id == user_id and message.channel.id == ctx.channel.id

        try:
            msg = await self.bot.wait_for('message', timeout=60.0, check=check)

            session = self._interactive_sessions[user_id]

            if msg.content.lower() == 'none':
                session['roles'] = []
            else:
                session['roles'] = msg.role_mentions

            await self._finalize_setup(ctx)

        except asyncio.TimeoutError:
            await ctx.send("❌ Role selection timed out.")
            self._interactive_sessions.pop(user_id, None)

    async def _finalize_setup(self, ctx):
        """Finalize the setup and create the monitoring configuration."""
        user_id = self._get_user_from_context(ctx)
        if not user_id:
            return

        session = self._interactive_sessions.get(user_id)
        if not session:
            return

        # Get guild_id from session since ctx might be a channel
        guild_id = session['guild_id']

        project = session['project']
        config = self._get_guild_config(guild_id)

        # Create or update monitored project
        if project.id not in config.projects:
            monitored_project = MonitoredProject(
                id=project.id,
                name=project.name,
                added_by=user_id
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
            required_version_types=session.get('release_channels'),
            use_latest_supported=session.get('use_latest_supported', False)
        )

        monitored_project.channels[session['notification_channel'].id] = channel_monitor
        await self._save_guild_config(guild_id)

        # Send confirmation
        embed = discord.Embed(
            title="✅ Monitoring Setup Complete",
            description=f"Successfully set up monitoring for **{project.name}**",
            color=discord.Color.green()
        )

        embed.add_field(name="Channel", value=session['notification_channel'].mention, inline=True)
        embed.add_field(name="Roles", value=humanize_list([role.mention for role in session['roles']]) if session['roles'] else "None", inline=True)

        if session.get('use_latest_supported'):
            embed.add_field(name="Minecraft Versions", value="Latest supported by mod (dynamic)", inline=True)
        elif session.get('minecraft_versions'):
            embed.add_field(name="Minecraft Versions", value=", ".join(session['minecraft_versions']), inline=True)
        else:
            embed.add_field(name="Minecraft Versions", value="All supported versions", inline=True)

        if session.get('loaders'):
            embed.add_field(name="Loaders", value=", ".join(session['loaders']), inline=True)
        if session.get('release_channels'):
            embed.add_field(name="Release Channels", value=", ".join(session['release_channels']), inline=True)

        await ctx.send(embed=embed)

        # Send initial version to confirm monitoring
        try:
            # Handle latest supported version monitoring
            minecraft_versions = session.get('minecraft_versions')
            if session.get('use_latest_supported'):
                latest_supported = await self.api.get_latest_supported_minecraft_version(project.id)
                if latest_supported:
                    minecraft_versions = [latest_supported]

            versions = await self.api.get_project_versions(
                project.id,
                limit=1,
                loaders=session.get('loaders'),
                game_versions=minecraft_versions
            )

            if versions:
                latest_version = versions[0]

                if latest_version.matches_filters(
                    session.get('loaders'),
                    minecraft_versions,
                    session.get('release_channels')
                ):
                    update_embed = create_update_embed(
                        project,
                        latest_version,
                        channel_monitor,
                        is_initial=True
                    )

                    content = None
                    if session['roles']:
                        content = " ".join([role.mention for role in session['roles']])

                    await session['notification_channel'].send(content=content, embed=update_embed)
                    monitored_project.last_version = latest_version.id
                    channel_monitor.last_version = latest_version.id
                    await self._save_guild_config(guild_id)
        except Exception as e:
            log.error(f"Error sending initial notification: {e}")

        # Clean up session
        self._interactive_sessions.pop(user_id, None)

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
                versions_input = message.content.strip()

                log.info(f"User {message.author.id} input version: '{versions_input}'")
                log.info(f"Supported versions: {session['supported_game_versions']}")

                # Parse versions (handle both single and comma-separated)
                if ',' in versions_input:
                    versions = [extract_minecraft_version(v.strip()) for v in versions_input.split(',')]
                else:
                    versions = [extract_minecraft_version(versions_input)]

                # Remove empty strings and None values
                versions = [v for v in versions if v]

                log.info(f"Parsed versions: {versions}")

                if not versions:
                    await message.channel.send("❌ No valid versions found. Please try again.")
                    return

                supported_versions = session['supported_game_versions']

                # Validate versions
                valid_versions = [v for v in versions if v in supported_versions]
                invalid_versions = [v for v in versions if v not in supported_versions]

                log.info(f"Matched: {valid_versions}, Invalid: {invalid_versions}")

                if invalid_versions:
                    await message.channel.send(
                        f"❌ Invalid versions: {', '.join(invalid_versions)}\n"
                        f"Supported versions: {', '.join(supported_versions[:15])}{'...' if len(supported_versions) > 15 else ''}")
                    return

                if not valid_versions:
                    await message.channel.send("❌ No valid versions found. Please try again.")
                    return

                session['minecraft_versions'] = valid_versions
                session['use_latest_supported'] = False
                session['step'] = None
                await self._ask_loader_type(message.channel)
                return

            except Exception as e:
                log.error(f"Error processing versions: {e}")
                await message.channel.send(
                    "❌ Invalid format. Please use comma-separated versions or a single version.")
                return

        # Handle loader specification
        elif session.get('step') == 'specify_loaders':
            try:
                loaders_input = message.content.strip().lower()

                # Parse loaders
                if ',' in loaders_input:
                    loaders = [l.strip() for l in loaders_input.split(',')]
                else:
                    loaders = [loaders_input]

                # Remove empty strings
                loaders = [l for l in loaders if l]

                if not loaders:
                    await message.channel.send("❌ No valid loaders found. Please try again.")
                    return

                supported_loaders = session['supported_loaders']

                # Validate loaders
                invalid_loaders = [l for l in loaders if l not in supported_loaders]
                if invalid_loaders:
                    await message.channel.send(
                        f"❌ Invalid loaders: {', '.join(invalid_loaders)}\n"
                        f"Supported loaders: {', '.join(supported_loaders)}")
                    return

                session['loaders'] = loaders
                session['step'] = None
                await self._ask_release_channel(message.channel)
                return

            except Exception as e:
                log.error(f"Error processing loaders: {e}")
                await message.channel.send(
                    "❌ Invalid format. Please use comma-separated loaders or a single loader.")
                return

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

    @modrinth.command(name="remove", aliases=["delete", "del"])
    @commands.admin_or_permissions(manage_guild=True)
    async def remove_project(self, ctx, project_id: str, channel: Optional[discord.TextChannel] = None):
        """Remove a project from monitoring.

        If channel is specified, only removes monitoring from that channel.
        Otherwise, removes the project entirely.
        """
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored in this server.")
            return

        project = config.projects[project_id]

        if channel:
            # Remove from specific channel
            if channel.id in project.channels:
                del project.channels[channel.id]
                await self._save_guild_config(ctx.guild.id)
                await ctx.send(f"✅ Removed **{project.name}** monitoring from {channel.mention}")

                # Remove project entirely if no channels left
                if not project.channels:
                    del config.projects[project_id]
                    await self._save_guild_config(ctx.guild.id)
                    await ctx.send(f"🗑️ **{project.name}** completely removed (no channels remaining)")
            else:
                await ctx.send(f"❌ **{project.name}** is not being monitored in {channel.mention}")
        else:
            # Remove entirely
            del config.projects[project_id]
            await self._save_guild_config(ctx.guild.id)
            await ctx.send(f"✅ Completely removed **{project.name}** from monitoring")

    @modrinth.command(name="test")
    @commands.admin_or_permissions(manage_guild=True)
    async def test_project(self, ctx, project_id: str):
        """Send a test notification for a monitored project."""
        config = self._get_guild_config(ctx.guild.id)

        if project_id not in config.projects:
            await ctx.send(f"❌ Project `{project_id}` is not being monitored in this server.")
            return

        project = config.projects[project_id]

        async with ctx.typing():
            try:
                project_info = await self.api.get_project(project_id)

                # Send test notifications to all monitored channels
                for channel_id, monitor in project.channels.items():
                    channel = ctx.guild.get_channel(channel_id)
                    if not channel:
                        continue

                    # Handle latest supported version monitoring for test
                    minecraft_versions = monitor.required_game_versions
                    if monitor.use_latest_supported:
                        latest_supported = await self.api.get_latest_supported_minecraft_version(project_id)
                        if latest_supported:
                            minecraft_versions = [latest_supported]

                    versions = await self.api.get_project_versions(
                        project_id,
                        limit=1,
                        loaders=monitor.required_loaders,
                        game_versions=minecraft_versions
                    )

                    if not versions:
                        await ctx.send(f"❌ No versions found for project `{project_id}` with current filters for {channel.mention}")
                        continue

                    latest_version = versions[0]

                    if not latest_version.matches_filters(
                        monitor.required_loaders,
                        minecraft_versions,
                        monitor.required_version_types
                    ):
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

    @modrinth.command(name="info")
    async def project_info(self, ctx, project_id: str):
        """Get detailed information about a Modrinth project."""
        async with ctx.typing():
            try:
                project = await self.api.get_project(project_id)
                embed = create_project_info_embed(project)
                await ctx.send(embed=embed)
            except ProjectNotFoundError:
                await ctx.send(f"❌ Project `{project_id}` not found.")
            except ModrinthAPIError as e:
                await ctx.send(f"❌ Error fetching project: {e}")

    @modrinth.command(name="search")
    async def search_projects(self, ctx, *, query: str):
        """Search for projects on Modrinth."""
        async with ctx.typing():
            try:
                results = await self.api.search_projects(query, limit=10)
                if not results:
                    await ctx.send(f"❌ No projects found for '{query}'.")
                    return

                embed = discord.Embed(
                    title=f"Search Results for '{query}'",
                    color=discord.Color.blue()
                )

                for i, project in enumerate(results[:5], 1):
                    embed.add_field(
                        name=f"{i}. {project.name}",
                        value=f"ID: `{project.id}`\nType: {project.project_type.title()}\nDownloads: {project.downloads:,}",
                        inline=True
                    )

                await ctx.send(embed=embed)

            except ModrinthAPIError as e:
                await ctx.send(f"❌ Search error: {e}")

    @modrinth.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def enable_monitoring(self, ctx):
        """Enable monitoring for this server."""
        config = self._get_guild_config(ctx.guild.id)
        config.enabled = True
        await self._save_guild_config(ctx.guild.id)
        await ctx.send("✅ Monitoring enabled for this server.")

    @modrinth.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def disable_monitoring(self, ctx):
        """Disable monitoring for this server."""
        config = self._get_guild_config(ctx.guild.id)
        config.enabled = False
        await self._save_guild_config(ctx.guild.id)
        await ctx.send("❌ Monitoring disabled for this server.")

    @modrinth.command(name="status")
    async def monitoring_status(self, ctx):
        """Show monitoring status for this server."""
        config = self._get_guild_config(ctx.guild.id)

        status = "✅ Enabled" if config.enabled else "❌ Disabled"
        project_count = len(config.projects)

        embed = discord.Embed(
            title="Monitoring Status",
            color=discord.Color.green() if config.enabled else discord.Color.red()
        )

        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Projects", value=str(project_count), inline=True)
        embed.add_field(name="Poll Interval", value=f"{config.poll_interval}s", inline=True)

        await ctx.send(embed=embed)