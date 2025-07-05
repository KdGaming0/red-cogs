"""Utility functions for embeds and formatting."""

import discord
from typing import List, Optional
from datetime import datetime
from .models import ProjectInfo, VersionInfo, ChannelMonitor

def create_update_embed(project: ProjectInfo, version: VersionInfo,
                       monitor: Optional[ChannelMonitor] = None, is_initial: bool = False) -> discord.Embed:
    """Create a rich embed for update notifications."""
    title_prefix = "🎉 Initial Notification: " if is_initial else ""
    embed = discord.Embed(
        title=f"{title_prefix}{project.name} - New Version Available!",
        description=f"Version {version.version_number} has been released",
        color=project.color or discord.Color.green(),
        url=f"https://modrinth.com/{project.project_type}/{project.slug}"
    )

    if project.icon_url:
        embed.set_thumbnail(url=project.icon_url)

    # Add version information
    embed.add_field(name="Version", value=version.version_number, inline=True)
    embed.add_field(name="Type", value=version.version_type.title(), inline=True)
    embed.add_field(name="Downloads", value=f"{project.downloads:,}", inline=True)

    # Game versions (limit to prevent embed from being too long)
    game_versions_display = version.game_versions
    if monitor and monitor.required_game_versions:
        # Highlight matching versions
        game_versions_display = [
            f"**{v}**" if v in monitor.required_game_versions else v
            for v in version.game_versions
        ]

    game_versions_str = ", ".join(game_versions_display[:5])
    if len(version.game_versions) > 5:
        game_versions_str += f" (+{len(version.game_versions) - 5} more)"
    embed.add_field(name="Game Versions", value=game_versions_str, inline=True)

    # Loaders
    loaders_display = version.loaders
    if monitor and monitor.required_loaders:
        # Highlight matching loaders
        loaders_display = [
            f"**{l}**" if l in monitor.required_loaders else l
            for l in version.loaders
        ]

    loaders_str = ", ".join(loaders_display)
    embed.add_field(name="Loaders", value=loaders_str, inline=True)

    # Publication date
    embed.add_field(
        name="Published",
        value=discord.utils.format_dt(version.date_published, style="R"),
        inline=True
    )

    # Add filter information if applicable
    if monitor and (monitor.required_loaders or monitor.required_game_versions):
        filter_info = []
        if monitor.required_loaders:
            filter_info.append(f"Loaders: {', '.join(monitor.required_loaders)}")
        if monitor.required_game_versions:
            filter_info.append(f"Versions: {', '.join(monitor.required_game_versions)}")

        embed.add_field(
            name="🔍 Channel Filters",
            value=" | ".join(filter_info),
            inline=False
        )

    # Changelog (truncated if too long)
    if version.changelog:
        changelog = version.changelog.strip()
        if len(changelog) > 1000:
            changelog = changelog[:997] + "..."
        embed.add_field(name="Changelog", value=changelog, inline=False)

    if is_initial:
        embed.set_footer(text="Modrinth Update Notifier - This is an initial notification to confirm monitoring is working")
    else:
        embed.set_footer(text="Modrinth Update Notifier")

    embed.timestamp = datetime.utcnow()

    return embed

def create_project_info_embed(project: ProjectInfo) -> discord.Embed:
    """Create an embed with project information."""
    embed = discord.Embed(
        title=project.name,
        description=project.description[:500] + ("..." if len(project.description) > 500 else ""),
        color=project.color or discord.Color.blue(),
        url=f"https://modrinth.com/{project.project_type}/{project.slug}"
    )

    if project.icon_url:
        embed.set_thumbnail(url=project.icon_url)

    embed.add_field(name="Type", value=project.project_type.title(), inline=True)
    embed.add_field(name="Downloads", value=f"{project.downloads:,}", inline=True)
    embed.add_field(name="Project ID", value=project.id, inline=True)

    embed.set_footer(text="Modrinth")

    return embed

def format_role_list(roles: List[discord.Role]) -> str:
    """Format a list of roles for display."""
    if not roles:
        return "None"
    return ", ".join(role.mention for role in roles)

def format_project_list(projects: dict, guild: Optional[discord.Guild] = None) -> List[str]:
    """Format a list of projects for display."""
    if not projects:
        return ["No projects being monitored."]

    lines = []
    for project_id, project in projects.items():
        line = f"**{project.name}** (`{project_id}`)"

        # Add channel information
        if project.channels:
            channel_count = len(project.channels)
            if guild:
                channel_names = []
                for channel_id in project.channels.keys():
                    channel = guild.get_channel(channel_id)
                    if channel:
                        channel_names.append(f"#{channel.name}")
                if channel_names:
                    line += f" - Channels: {', '.join(channel_names[:3])}"
                    if len(channel_names) > 3:
                        line += f" (+{len(channel_names) - 3} more)"
            else:
                line += f" - {channel_count} channel{'s' if channel_count != 1 else ''}"

        lines.append(line)

    return lines

def format_channel_list(project, guild: discord.Guild) -> List[str]:
    """Format a list of channels monitoring a project."""
    if not project.channels:
        return ["No channels monitoring this project."]

    lines = []
    for channel_id, monitor in project.channels.items():
        channel = guild.get_channel(channel_id)
        if not channel:
            continue

        line = f"#{channel.name}"

        # Add filter information
        filters = []
        if monitor.required_loaders:
            filters.append(f"Loaders: {', '.join(monitor.required_loaders)}")
        if monitor.required_game_versions:
            filters.append(f"Versions: {', '.join(monitor.required_game_versions)}")

        if filters:
            line += f" - {' | '.join(filters)}"

        # Add role information
        if monitor.role_ids:
            roles = [guild.get_role(role_id) for role_id in monitor.role_ids]
            valid_roles = [role for role in roles if role is not None]
            if valid_roles:
                line += f" - Roles: {', '.join(role.mention for role in valid_roles)}"

        lines.append(line)

    return lines

def parse_filter_string(filter_str: str) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """Parse filter string in format 'loaders|game_versions' (e.g., 'fabric,forge|1.20,1.21')."""
    if not filter_str or filter_str.strip() == "":
        return None, None

    parts = filter_str.split('|', 1)

    loaders = None
    game_versions = None

    # Parse loaders (first part)
    if len(parts) >= 1 and parts[0].strip():
        loaders = [loader.strip() for loader in parts[0].split(',') if loader.strip()]
        if not loaders:
            loaders = None

    # Parse game versions (second part)
    if len(parts) >= 2 and parts[1].strip():
        game_versions = [version.strip() for version in parts[1].split(',') if version.strip()]
        if not game_versions:
            game_versions = None

    return loaders, game_versions

def format_filters(loaders: Optional[List[str]], game_versions: Optional[List[str]]) -> str:
    """Format filters for display."""
    if not loaders and not game_versions:
        return "No filters"

    parts = []
    if loaders:
        parts.append(f"Loaders: {', '.join(loaders)}")
    if game_versions:
        parts.append(f"Versions: {', '.join(game_versions)}")

    return " | ".join(parts)

def truncate_text(text: str, max_length: int = 2000) -> str:
    """Truncate text to fit Discord message limits."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."

def format_time_ago(timestamp: float) -> str:
    """Format a timestamp as time ago."""
    dt = datetime.fromtimestamp(timestamp)
    return discord.utils.format_dt(dt, style="R")

def get_valid_loaders() -> List[str]:
    """Get list of valid Modrinth loaders."""
    return ["fabric", "forge", "quilt", "neoforge", "modloader", "rift", "liteloader", "minecraft", "bukkit", "spigot", "paper", "purpur", "sponge", "bungeecord", "waterfall", "velocity"]

def get_common_game_versions() -> List[str]:
    """Get list of common Minecraft versions."""
    return ["1.21", "1.20.6", "1.20.5", "1.20.4", "1.20.3", "1.20.2", "1.20.1", "1.20", "1.19.4", "1.19.3", "1.19.2", "1.19.1", "1.19", "1.18.2", "1.18.1", "1.18", "1.17.1", "1.17", "1.16.5", "1.16.4", "1.16.3", "1.16.2", "1.16.1", "1.16"]