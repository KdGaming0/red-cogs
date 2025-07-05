"""Utility functions for embeds and formatting."""

import discord
from typing import List, Optional
from datetime import datetime
from .models import ProjectInfo, VersionInfo


def create_update_embed(project: ProjectInfo, version: VersionInfo) -> discord.Embed:
    """Create a rich embed for update notifications."""
    embed = discord.Embed(
        title=f"{project.name} - New Version Available!",
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
    game_versions_str = ", ".join(version.game_versions[:5])
    if len(version.game_versions) > 5:
        game_versions_str += f" (+{len(version.game_versions) - 5} more)"
    embed.add_field(name="Game Versions", value=game_versions_str, inline=True)

    # Loaders
    loaders_str = ", ".join(version.loaders)
    embed.add_field(name="Loaders", value=loaders_str, inline=True)

    # Publication date
    embed.add_field(
        name="Published",
        value=discord.utils.format_dt(version.date_published, style="R"),
        inline=True
    )

    # Changelog (truncated if too long)
    if version.changelog:
        changelog = version.changelog.strip()
        if len(changelog) > 1000:
            changelog = changelog[:997] + "..."
        embed.add_field(name="Changelog", value=changelog, inline=False)

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

        # Add role information if guild is provided
        if guild and hasattr(project, 'role_ids') and project.role_ids:
            roles = [guild.get_role(role_id) for role_id in project.role_ids]
            valid_roles = [role for role in roles if role is not None]
            if valid_roles:
                line += f" - Roles: {', '.join(role.mention for role in valid_roles)}"

        # Add last version if available
        if project.last_version:
            line += f" - Last: `{project.last_version}`"

        lines.append(line)

    return lines


def truncate_text(text: str, max_length: int = 2000) -> str:
    """Truncate text to fit Discord message limits."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def format_time_ago(timestamp: float) -> str:
    """Format a timestamp as time ago."""
    dt = datetime.fromtimestamp(timestamp)
    return discord.utils.format_dt(dt, style="R")