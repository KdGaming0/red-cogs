"""Enhanced utility functions for the Modrinth Notifier cog."""

import discord
from datetime import datetime
from typing import List, Optional, Tuple

from .models import ProjectInfo, VersionInfo, ChannelMonitor

def create_update_embed(project: ProjectInfo, version: VersionInfo,
                       monitor: Optional[ChannelMonitor] = None,
                       is_initial: bool = False,
                       title_prefix: str = "") -> discord.Embed:
    """Create an enhanced embed for version updates."""

    color_map = {
        "release": discord.Color.green(),
        "beta": discord.Color.orange(),
        "alpha": discord.Color.red()
    }
    color = color_map.get(version.version_type, discord.Color.blue())

    embed = discord.Embed(
        title=f"{title_prefix}{project.name} - New Version Available!",
        description=f"Version **{version.version_number}** has been released",
        color=color,
        url=f"https://modrinth.com/{project.project_type}/{project.slug}/version/{version.id}"
    )

    if project.icon_url:
        embed.set_thumbnail(url=project.icon_url)

    embed.add_field(name="Version", value=version.version_number, inline=True)
    embed.add_field(name="Type", value=version.version_type.title(), inline=True)
    embed.add_field(name="Published", value=discord.utils.format_dt(version.date_published, style="R"), inline=True)

    # Game versions
    game_versions_display = version.game_versions
    if monitor and monitor.required_game_versions:
        game_versions_display = [
            f"**{v}**" if v in monitor.required_game_versions else v
            for v in version.game_versions
        ]

    game_versions_str = ", ".join(game_versions_display[:8])
    if len(version.game_versions) > 8:
        game_versions_str += f" (+{len(version.game_versions) - 8} more)"
    embed.add_field(name="Minecraft Versions", value=game_versions_str, inline=True)

    # Loaders
    loaders_display = version.loaders
    if monitor and monitor.required_loaders:
        loaders_display = [
            f"**{l}**" if l in monitor.required_loaders else l
            for l in version.loaders
        ]

    loaders_str = ", ".join(loaders_display) if loaders_display else "Universal"
    embed.add_field(name="Loaders", value=loaders_str, inline=True)

    embed.add_field(name="Project Downloads", value=f"{project.downloads:,}", inline=True)

    # Filter information
    if monitor and (monitor.required_loaders or monitor.required_game_versions or monitor.required_version_types):
        filter_info = []
        if monitor.required_loaders:
            filter_info.append(f"Loaders: {', '.join(monitor.required_loaders)}")
        if monitor.required_game_versions:
            filter_info.append(f"MC Versions: {', '.join(monitor.required_game_versions)}")
        if monitor.required_version_types:
            filter_info.append(f"Release Types: {', '.join(monitor.required_version_types)}")

        embed.add_field(
            name="🔍 Active Filters",
            value=" • ".join(filter_info),
            inline=False
        )

    # Changelog
    if version.changelog:
        changelog = version.changelog.strip()
        if len(changelog) > 800:
            changelog = changelog[:797] + "..."
        embed.add_field(name="📝 Changelog", value=changelog, inline=False)

    # Links
    links = [
        f"[View on Modrinth](https://modrinth.com/{project.project_type}/{project.slug})",
        f"[Version Details](https://modrinth.com/{project.project_type}/{project.slug}/version/{version.id})"
    ]
    embed.add_field(name="🔗 Links", value=" • ".join(links), inline=False)

    if is_initial:
        embed.set_footer(text="🧪 This is a test notification to confirm monitoring is working")
    else:
        embed.set_footer(text="Modrinth Update Notifier • Updates every 5 minutes")

    embed.timestamp = datetime.utcnow()
    return embed

def create_project_info_embed(project: ProjectInfo) -> discord.Embed:
    """Create an embed with detailed project information."""
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
    embed.add_field(name="Project ID", value=f"`{project.id}`", inline=True)

    embed.set_footer(text="Modrinth Project Information")
    embed.timestamp = datetime.utcnow()
    return embed

def get_valid_loaders() -> List[str]:
    """Get list of valid Minecraft mod loaders."""
    return [
        "fabric", "forge", "neoforge", "quilt", "modloader",
        "rift", "liteloader", "datapack", "bukkit", "spigot",
        "paper", "purpur", "folia", "velocity", "waterfall",
        "bungeecord", "sponge", "vanilla", "minecraft"
    ]

def get_valid_version_types() -> List[str]:
    """Get list of valid version types."""
    return ["release", "beta", "alpha"]