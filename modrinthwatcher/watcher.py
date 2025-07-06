import asyncio
import datetime
import httpx
import logging
from typing import List, Optional, Dict, Any, Union

import discord
from redbot.core import commands, app_commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, pagify
from redbot.core.utils.views import ConfirmView

# Setup logger
log = logging.getLogger("red.kdgaming.modrinthwatcher")

# Base Modrinth API URL
MODRINTH_API_BASE = "https://api.modrinth.com/v2"


class ModrinthWatcher(commands.Cog):
    """
    Monitor Modrinth projects for new game versions and receive notifications.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=1618033988, force_registration=True
        )
        # Using custom groups for a more structured database-like approach
        self.config.init_custom("PROJECTS", 1)  # Storing project data globally
        self.config.register_custom(
            "PROJECTS",
            slug=None,
            title=None,
            icon_url=None,
            known_version_ids=[],
            last_checked=None,
        )

        self.config.register_guild(
            trackings={},  # {project_id: {channel_id: int, role_ids: [int]}}
        )

        # User-Agent as required by Modrinth API docs
        self.user_agent = f"ModrinthWatcherCog/1.0.0 (Contact: @KdGaming0 on Discord; For: Red-DiscordBot)"
        self.client = httpx.AsyncClient(headers={"User-Agent": self.user_agent})

        # Start the background checking loop
        self.check_loop_task = self.bot.loop.create_task(self.check_loop())

        # Correctly initialize the app command group on the instance
        self.mwatch = app_commands.Group(name="mwatch", description="Commands to watch Modrinth projects.")

    def cog_unload(self):
        """Clean up when cog is unloaded."""
        self.check_loop_task.cancel()
        # It's good practice to close the httpx client
        self.bot.loop.create_task(self.client.aclose())

    async def check_loop(self):
        """Background loop to check for updates."""
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.check_for_updates()
            except Exception as e:
                log.error("An error occurred in the check_loop:", exc_info=e)
            # Check every 5 minutes to respect rate limits
            await asyncio.sleep(300)

            # --- Modrinth API Handling ---

    async def _api_request(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """Make a request to the Modrinth API with error handling."""
        try:
            response = await self.client.get(f"{MODRINTH_API_BASE}{endpoint}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP error for {endpoint}: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            log.error(f"Request error for {endpoint}: {e}")
        return None

    async def get_project(self, project_id_or_slug: str) -> Optional[Dict[str, Any]]:
        """Get a project's details from Modrinth."""
        return await self._api_request(f"/project/{project_id_or_slug}")

    async def get_project_versions(self, project_id_or_slug: str) -> Optional[List[Dict[str, Any]]]:
        """Get a project's versions from Modrinth."""
        return await self._api_request(f"/project/{project_id_or_slug}/version")

    # --- Core Logic ---

    async def check_for_updates(self):
        """The main update checking logic."""
        all_guild_configs = await self.config.all_guilds()

        # Gather all unique project IDs being tracked across all guilds
        all_tracked_project_ids = set()
        for guild_id, data in all_guild_configs.items():
            all_tracked_project_ids.update(data.get("trackings", {}).keys())

        if not all_tracked_project_ids:
            return  # No projects to check

        log.info(f"Checking for updates on {len(all_tracked_project_ids)} projects...")

        for project_id in all_tracked_project_ids:
            try:
                await self.check_single_project(project_id, all_guild_configs)
            except Exception as e:
                log.error(f"Failed to check project {project_id}:", exc_info=e)
            await asyncio.sleep(1)  # Small sleep to avoid bursting requests

    async def check_single_project(self, project_id: str, all_guild_configs: Dict):
        """Checks a single project for new versions and notifies relevant guilds."""
        project_data = await self.config.custom("PROJECTS", project_id).all()

        new_versions_data = await self.get_project_versions(project_id)
        if not new_versions_data:
            log.warning(f"Could not fetch versions for project {project_id}.")
            return

        current_version_ids = {v['id'] for v in new_versions_data}
        known_version_ids = set(project_data.get("known_version_ids", []))

        newly_found_ids = current_version_ids - known_version_ids

        if not newly_found_ids:
            return  # No new versions

        log.info(f"Found {len(newly_found_ids)} new version(s) for project {project_id} ({project_data.get('title')})")

        # Update known versions in the database
        await self.config.custom("PROJECTS", project_id).known_version_ids.set(list(current_version_ids))

        # Find which of the new versions is the latest
        # API returns versions sorted newest first
        latest_version = new_versions_data[0]
        if latest_version['id'] not in newly_found_ids:
            # This case is rare, but could happen if multiple versions are posted between checks.
            # We find the latest among the *newly discovered* ones.
            latest_version = next((v for v in new_versions_data if v['id'] in newly_found_ids), None)
            if not latest_version:
                return

        # Build the embed once
        embed = self.build_version_embed(project_data, latest_version)

        # Notify all guilds tracking this project
        for guild_id, guild_data in all_guild_configs.items():
            tracking_info = guild_data.get("trackings", {}).get(project_id)
            if tracking_info:
                await self.notify_guild(guild_id, tracking_info, embed)

    def build_version_embed(self, project_info: dict, version_info: dict) -> discord.Embed:
        """Builds a rich embed for a new version notification."""
        project_title = project_info.get("title", "Unknown Project")
        version_number = version_info.get("version_number", "Unknown Version")

        embed = discord.Embed(
            title=f"{project_title} - {version_number}",
            url=f"https://modrinth.com/project/{project_info.get('slug', '')}/version/{version_info.get('id')}",
            description=version_info.get("changelog", "No changelog provided."),
            color=discord.Color.green()
        )
        if project_info.get("icon_url"):
            embed.set_thumbnail(url=project_info["icon_url"])

        embed.add_field(name="Release Date",
                        value=f"<t:{int(datetime.datetime.fromisoformat(version_info['date_published'].replace('Z', '')).timestamp())}:D>",
                        inline=True)
        embed.add_field(name="Downloads", value=f"{version_info.get('downloads', 0):,}", inline=True)

        loaders = humanize_list(version_info.get('loaders', ['N/A']))
        embed.add_field(name="Loader", value=loaders, inline=True)

        game_versions = humanize_list(version_info.get('game_versions', ['N/A']))
        embed.add_field(name="Minecraft Version(s)", value=game_versions, inline=False)

        embed.set_footer(text=f"Modrinth Project ID: {version_info.get('project_id')}")
        return embed

    async def notify_guild(self, guild_id: int, tracking_info: dict, embed: discord.Embed):
        """Sends a notification to a specific guild."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return

        channel = guild.get_channel(tracking_info["channel_id"])
        if not channel or not isinstance(channel, discord.TextChannel):
            log.warning(f"Invalid channel ID {tracking_info['channel_id']} for guild {guild_id}")
            return

        content = ""
        role_ids = tracking_info.get("role_ids", [])
        if role_ids:
            mentions = []
            for role_id in role_ids:
                role = guild.get_role(role_id)
                if role:
                    mentions.append(role.mention)
            content = " ".join(mentions)

        try:
            await channel.send(content=content, embed=embed)
        except discord.Forbidden:
            log.error(f"No permission to send message in channel {channel.id} for guild {guild.id}")
        except Exception as e:
            log.error(f"Could not send message to {channel.id}:", exc_info=e)

    # --- App Commands ---

    @app_commands.command()
    @app_commands.describe(
        project_id_or_slug="The project ID or slug from Modrinth (e.g., 'sodium' or 'AANobbMI').",
        channel="The channel where update notifications should be sent.",
        role_to_ping="An optional role to ping for updates."
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def track_project(self, interaction: discord.Interaction, project_id_or_slug: str,
                            channel: discord.TextChannel, role_to_ping: Optional[discord.Role] = None):
        """Tracks a new Modrinth project for update notifications."""
        await interaction.response.defer(ephemeral=True)

        project_data = await self.get_project(project_id_or_slug)
        if not project_data:
            await interaction.followup.send(
                "Could not find a project with that ID or slug. Please check and try again.")
            return

        project_id = project_data["id"]

        # Store project info globally if it's new
        if not await self.config.custom("PROJECTS", project_id).slug():
            versions_data = await self.get_project_versions(project_id)
            known_version_ids = [v['id'] for v in versions_data] if versions_data else []

            await self.config.custom("PROJECTS", project_id).set({
                "slug": project_data["slug"],
                "title": project_data["title"],
                "icon_url": project_data.get("icon_url"),
                "known_version_ids": known_version_ids,
                "last_checked": datetime.datetime.utcnow().isoformat(),
            })

        # Add tracking info for this guild
        async with self.config.guild(interaction.guild).trackings() as trackings:
            if project_id in trackings:
                await interaction.followup.send(
                    f"Project '{project_data['title']}' is already being tracked in this server.")
                return

            trackings[project_id] = {
                "channel_id": channel.id,
                "role_ids": [role_to_ping.id] if role_to_ping else []
            }

        await interaction.followup.send(
            f"Successfully started tracking '{project_data['title']}'! Updates will be posted in {channel.mention}.")

    @app_commands.command()
    @app_commands.describe(project_id_or_slug="The project ID or slug to stop tracking.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def untrack_project(self, interaction: discord.Interaction, project_id_or_slug: str):
        """Stops tracking a Modrinth project in this server."""
        await interaction.response.defer(ephemeral=True)

        # Find the project ID from our stored data
        project_id_to_remove = None
        project_title = project_id_or_slug  # Fallback

        all_projects = await self.config.custom("PROJECTS").all()
        for pid, data in all_projects.items():
            if pid.lower() == project_id_or_slug.lower() or (
                    data.get("slug") and data["slug"].lower() == project_id_or_slug.lower()):
                project_id_to_remove = pid
                project_title = data.get("title", project_title)
                break

        if not project_id_to_remove:
            await interaction.followup.send("Could not find that project in the tracking list.")
            return

        async with self.config.guild(interaction.guild).trackings() as trackings:
            if project_id_to_remove not in trackings:
                await interaction.followup.send(f"Project '{project_title}' is not being tracked in this server.")
                return

            del trackings[project_id_to_remove]

        await interaction.followup.send(f"Successfully stopped tracking '{project_title}'.")

    @app_commands.command()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_tracked(self, interaction: discord.Interaction):
        """Lists all Modrinth projects being tracked in this server."""
        await interaction.response.defer(ephemeral=True)

        trackings = await self.config.guild(interaction.guild).trackings()
        if not trackings:
            await interaction.followup.send("No projects are currently being tracked in this server.")
            return

        lines = []
        for project_id, data in trackings.items():
            project_info = await self.config.custom("PROJECTS", project_id).all()
            title = project_info.get('title', f"Unknown Project (ID: {project_id})")
            channel = interaction.guild.get_channel(data['channel_id'])
            channel_mention = channel.mention if channel else f"ID: {data['channel_id']}"
            lines.append(f"• **{title}** -> {channel_mention}")

        description = "\n".join(lines)
        embed = discord.Embed(
            title=f"Tracked Projects in {interaction.guild.name}",
            description=description,
            color=await self.bot.get_embed_color(interaction.guild)
        )

        for page in pagify(description, page_length=4000):
            embed.description = page
            await interaction.followup.send(embed=embed, ephemeral=True)


# Add the commands to the cog class after they are defined
ModrinthWatcher.mwatch.add_command(ModrinthWatcher.track_project)
ModrinthWatcher.mwatch.add_command(ModrinthWatcher.untrack_project)
ModrinthWatcher.mwatch.add_command(ModrinthWatcher.list_tracked)