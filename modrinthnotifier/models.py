"""Data models and validation for the Modrinth Notifier cog."""

from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import logging

log = logging.getLogger("red.modrinthnotifier.models")


@dataclass
class ProjectInfo:
    """Represents a Modrinth project."""
    id: str
    name: str
    slug: str
    description: str
    project_type: str
    downloads: int
    icon_url: Optional[str] = None
    color: Optional[int] = None

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> 'ProjectInfo':
        """Create ProjectInfo from Modrinth API response."""
        return cls(
            id=data["id"],
            name=data["title"],
            slug=data["slug"],
            description=data["description"],
            project_type=data["project_type"],
            downloads=data["downloads"],
            icon_url=data.get("icon_url"),
            color=data.get("color")
        )


@dataclass
class VersionInfo:
    """Represents a Modrinth version."""
    id: str
    name: str
    version_number: str
    changelog: Optional[str]
    version_type: str
    game_versions: List[str]
    loaders: List[str]
    date_published: datetime
    downloads: int
    project_id: str

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> 'VersionInfo':
        """Create VersionInfo from Modrinth API response."""
        return cls(
            id=data["id"],
            name=data["name"],
            version_number=data["version_number"],
            changelog=data.get("changelog"),
            version_type=data["version_type"],
            game_versions=data["game_versions"],
            loaders=data["loaders"],
            date_published=datetime.fromisoformat(data["date_published"].replace("Z", "+00:00")),
            downloads=data["downloads"],
            project_id=data["project_id"]
        )


@dataclass
class MonitoredProject:
    """Represents a project being monitored."""
    id: str
    name: str
    last_version: Optional[str] = None
    role_ids: List[int] = field(default_factory=list)
    added_by: Optional[int] = None
    added_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "name": self.name,
            "last_version": self.last_version,
            "role_ids": self.role_ids,
            "added_by": self.added_by,
            "added_at": self.added_at
        }

    @classmethod
    def from_dict(cls, project_id: str, data: Dict[str, Any]) -> 'MonitoredProject':
        """Create from stored dictionary."""
        return cls(
            id=project_id,
            name=data["name"],
            last_version=data.get("last_version"),
            role_ids=data.get("role_ids", []),
            added_by=data.get("added_by"),
            added_at=data.get("added_at", datetime.utcnow().timestamp())
        )


@dataclass
class GuildConfig:
    """Configuration for a guild."""
    channel_id: Optional[int] = None
    default_role_ids: List[int] = field(default_factory=list)
    check_interval: int = 15  # minutes
    enabled: bool = False
    projects: Dict[str, MonitoredProject] = field(default_factory=dict)
    last_check: float = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "channel_id": self.channel_id,
            "default_role_ids": self.default_role_ids,
            "check_interval": self.check_interval,
            "enabled": self.enabled,
            "projects": {pid: proj.to_dict() for pid, proj in self.projects.items()},
            "last_check": self.last_check
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GuildConfig':
        """Create from stored dictionary."""
        config = cls(
            channel_id=data.get("channel_id"),
            default_role_ids=data.get("default_role_ids", []),
            check_interval=data.get("check_interval", 15),
            enabled=data.get("enabled", False),
            last_check=data.get("last_check", 0)
        )

        # Load projects
        projects_data = data.get("projects", {})
        for project_id, project_data in projects_data.items():
            config.projects[project_id] = MonitoredProject.from_dict(project_id, project_data)

        return config


@dataclass
class UserProject:
    """Represents a project in a user's watchlist."""
    id: str
    name: str
    last_version: Optional[str] = None
    added_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "name": self.name,
            "last_version": self.last_version,
            "added_at": self.added_at
        }

    @classmethod
    def from_dict(cls, project_id: str, data: Dict[str, Any]) -> 'UserProject':
        """Create from stored dictionary."""
        return cls(
            id=project_id,
            name=data["name"],
            last_version=data.get("last_version"),
            added_at=data.get("added_at", datetime.utcnow().timestamp())
        )


@dataclass
class UserConfig:
    """Configuration for a user."""
    enabled: bool = True
    channel_id: Optional[int] = None
    use_dm: bool = True
    projects: Dict[str, UserProject] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "enabled": self.enabled,
            "channel_id": self.channel_id,
            "use_dm": self.use_dm,
            "projects": {pid: proj.to_dict() for pid, proj in self.projects.items()}
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserConfig':
        """Create from stored dictionary."""
        config = cls(
            enabled=data.get("enabled", True),
            channel_id=data.get("channel_id"),
            use_dm=data.get("use_dm", True)
        )

        # Load projects
        projects_data = data.get("projects", {})
        for project_id, project_data in projects_data.items():
            config.projects[project_id] = UserProject.from_dict(project_id, project_data)

        return config