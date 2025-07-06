import asyncio
import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta
from redbot.core import Config
from .api import ModrinthAPI
from .utils import extract_version_number, compare_versions

log = logging.getLogger("red.modrinth_checker.tasks")


class UpdateChecker:
    """Handles background update checking for monitored projects."""

    def __init__(self, bot, config: Config, api: ModrinthAPI):
        self.bot = bot
        self.config = config
        self.api = api
        self.check_interval = 1800  # 30 minutes default
        self.running = False
        self.task = None

    async def start(self):
        """Start the background update checker."""
        if self.running:
            return

        self.running = True
        self.task = asyncio.create_task(self._background_checker())
        log.info("Background update checker started")

    async def stop(self):
        """Stop the background update checker."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        log.info("Background update checker stopped")

    async def _background_checker(self):
        """Main background checking loop."""
        while self.running:
            try:
                await self._check_all_projects()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in background checker: {e}")
                await asyncio.sleep(300)  # Wait 5 minutes before retrying

    async def _check_all_projects(self):
        """Check all monitored projects for updates."""
        all_guilds = await self.config.all_guilds()

        for guild_id, guild_data in all_guilds.items():
            if not guild_data.get("notifications_enabled", True):
                continue

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            projects = guild_data.get("projects", {})

            for project_id, project_data in projects.items():
                try:
                    await self._check_project_update(guild, project_id, project_data)
                except Exception as e:
                    log.error(f"Error checking project {project_id}: {e}")

                # Small delay between checks to avoid rate limiting
                await asyncio.sleep(2)

    async def _check_project_update(self, guild, project_id: str, project_data: Dict[str, Any]):
        """Check a specific project for updates."""
        try:
            # Get the latest version based on the project's monitoring config
            latest_version = await self._get_latest_monitored_version(project_id, project_data)

            if not latest_version:
                log.warning(f"No version found for project {project_id}")
                return

            # Check if this is a new version
            current_version = project_data.get("current_version")
            if not current_version:
                # First time checking, set current version
                await self._update_project_version(guild.id, project_id, latest_version)
                return

            # Compare versions
            is_newer = await self._is_newer_version(current_version, latest_version)

            if is_newer:
                log.info(f"New version found for {project_id}: {latest_version['version_number']}")
                await self._send_update_notification(guild, project_id, project_data, latest_version)
                await self._update_project_version(guild.id, project_id, latest_version)

        except Exception as e:
            log.error(f"Error checking project {project_id}: {e}")

    async def _get_latest_monitored_version(self, project_id: str, project_data: Dict[str, Any]):
        """Get the latest version that matches the monitoring criteria."""
        monitoring_config = project_data.get("monitoring_config", {})

        # Determine what versions to check
        game_versions = None
        if monitoring_config.get("version_type") == "specific":
            game_versions = monitoring_config.get("versions", [])
        elif monitoring_config.get("version_type") == "latest_current":
            game_versions = monitoring_config.get("versions", [])
        elif monitoring_config.get("version_type") == "latest_always":
            # Get the latest supported version dynamically
            all_versions = await self.api.get_project_game_versions(project_id)
            game_versions = [all_versions[0]] if all_versions else None

        # Get loader filter
        loaders = monitoring_config.get("loaders", [])
        if monitoring_config.get("loader_type") == "all":
            loaders = None

        # Get the latest version
        latest_version = await self.api.get_latest_version(
            project_id,
            game_versions=game_versions,
            loaders=loaders
        )

        if not latest_version:
            return None

        # Filter by release channel
        channels = monitoring_config.get("channels", ["release"])
        if "all" not in channels:
            version_type = latest_version.get("version_type", "release")
            if version_type not in channels:
                return None

        return latest_version

    async def _is_newer_version(self, current_version: Dict[str, Any], latest_version: Dict[str, Any]) -> bool:
        """Check if the latest version is newer than the current version."""
        # First check by publication date
        try:
            current_date = datetime.fromisoformat(current_version["date_published"].replace('Z', '+00:00'))
            latest_date = datetime.fromisoformat(latest_version["date_published"].replace('Z', '+00:00'))

            if latest_date > current_date:
                return True
        except Exception:
            pass

        # Fallback to version number comparison
        current_version_num = extract_version_number(current_version["version_number"])
        latest_version_num = extract_version_number(latest_version["version_number"])

        if current_version_num and latest_version_num:
            return compare_versions(latest_version_num, current_version_num) > 0

        # Final fallback to string comparison
        return latest_version["version_number"] != current_version["version_number"]

    async def _update_project_version(self, guild_id: int, project_id: str, version: Dict[str, Any]):
        """Update the stored current version for a project."""
        async with self.config.guild_from_id(guild_id).projects() as projects:
            if project_id in projects:
                projects[project_id]["current_version"] = version
                projects[project_id]["last_checked"] = datetime.now().isoformat()

    async def _send_update_notification(self, guild, project_id: str, project_data: Dict[str, Any],
                                        version: Dict[str, Any]):
        """Send an update notification to the configured channel."""
        try:
            from .cog import ModrinthChecker  # Import here to avoid circular import

            # Get the cog instance
            cog = self.bot.get_cog("ModrinthChecker")
            if not cog:
                log.error("ModrinthChecker cog not found")
                return

            # Get channel
            channel_id = project_data.get("channel_id")
            if not channel_id:
                log.warning(f"No channel configured for project {project_id}")
                return

            channel = guild.get_channel(channel_id)
            if not channel:
                log.warning(f"Channel {channel_id} not found for project {project_id}")
                return

            # Get project info
            project_info = await self.api.get_project(project_id)
            if not project_info:
                log.error(f"Could not get project info for {project_id}")
                return

            # Create embed
            embed = await cog._create_update_embed(project_info, version)

            # Get roles to ping
            roles_to_ping = project_data.get("roles", [])
            ping_content = ""

            if roles_to_ping:
                role_mentions = []
                for role_id in roles_to_ping:
                    role = guild.get_role(role_id)
                    if role:
                        role_mentions.append(role.mention)

                if role_mentions:
                    ping_content = " ".join(role_mentions)

            # Send notification
            await channel.send(content=ping_content, embed=embed)
            log.info(f"Sent update notification for {project_id} to {channel.name}")

        except Exception as e:
            log.error(f"Error sending update notification: {e}")

    async def check_project_manually(self, guild, project_id: str) -> bool:
        """Manually check a specific project for updates."""
        try:
            guild_data = await self.config.guild(guild).all()
            projects = guild_data.get("projects", {})

            if project_id not in projects:
                return False

            await self._check_project_update(guild, project_id, projects[project_id])
            return True

        except Exception as e:
            log.error(f"Error in manual check for {project_id}: {e}")
            return False

    async def set_check_interval(self, interval: int):
        """Set the check interval in seconds."""
        self.check_interval = max(300, interval)  # Minimum 5 minutes
        log.info(f"Check interval set to {self.check_interval} seconds")