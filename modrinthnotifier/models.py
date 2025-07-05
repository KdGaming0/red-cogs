"""Enhanced data models for the Modrinth Notifier cog."""

from typing import Dict, List, Optional, Any, Set
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

    def matches_filters(self, required_loaders: Optional[List[str]] = None,
                       required_game_versions: Optional[List[str]] = None,
                       required_version_types: Optional[List[str]] = None) -> bool:
        """Check if this version matches the specified filters."""
        if required_loaders:
            if not any(loader in self.loaders for loader in required_loaders):
                return False

        if required_game_versions:
            if not any(version in self.game_versions for version in required_game_versions):
                return False

        if required_version_types:
            if self.version_type not in required_version_types:
                return False

        return True

@dataclass
class ChannelMonitor:
    """Represents monitoring configuration for a specific channel."""
    channel_id: int
    role_ids: List[int] = field(default_factory=list)
    required_loaders: Optional[List[str]] = None
    required_game_versions: Optional[List[str]] = None
    required_version_types: Optional[List[str]] = None
    last_version: Optional[str] = None
    added_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "channel_id": self.channel_id,
            "role_ids": self.role_ids,
            "required_loaders": self.required_loaders,
            "required_game_versions": self.required_game_versions,
            "required_version_types": self.required_version_types,
            "last_version": self.last_version,
            "added_at": self.added_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChannelMonitor':
        """Create from stored dictionary."""
        return cls(
            channel_id=data["channel_id"],
            role_ids=data.get("role_ids", []),
            required_loaders=data.get("required_loaders"),
            required_game_versions=data.get("required_game_versions"),
            required_version_types=data.get("required_version_types"),
            last_version=data.get("last_version"),
            added_at=data.get("added_at", datetime.utcnow().timestamp())
        )

@dataclass
class MonitoredProject:
    """Represents a project being monitored."""
    id: str
    name: str
    added_by: int
    channels: Dict[int, ChannelMonitor] = field(default_factory=dict)
    last_version: Optional[str] = None
    added_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "name": self.name,
            "added_by": self.added_by,
            "channels": {str(k): v.to_dict() for k, v in self.channels.items()},
            "last_version": self.last_version,
            "added_at": self.added_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MonitoredProject':
        """Create from stored dictionary."""
        channels = {}
        for k, v in data.get("channels", {}).items():
            channels[int(k)] = ChannelMonitor.from_dict(v)

        return cls(
            id=data["id"],
            name=data["name"],
            added_by=data["added_by"],
            channels=channels,
            last_version=data.get("last_version"),
            added_at=data.get("added_at", datetime.utcnow().timestamp())
        )

@dataclass
class GuildConfig:
    """Guild-specific configuration."""
    projects: Dict[str, MonitoredProject] = field(default_factory=dict)
    channel_id: Optional[int] = None
    enabled: bool = True
    poll_interval: int = 300

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "projects": {k: v.to_dict() for k, v in self.projects.items()},
            "channel_id": self.channel_id,
            "enabled": self.enabled,
            "poll_interval": self.poll_interval
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GuildConfig':
        """Create from stored dictionary."""
        projects = {}
        for k, v in data.get("projects", {}).items():
            projects[k] = MonitoredProject.from_dict(v)

        return cls(
            projects=projects,
            channel_id=data.get("channel_id"),
            enabled=data.get("enabled", True),
            poll_interval=data.get("poll_interval", 300)
        )

@dataclass
class UserConfig:
    """User-specific configuration for personal watchlists."""
    projects: Dict[str, MonitoredProject] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "projects": {k: v.to_dict() for k, v in self.projects.items()},
            "enabled": self.enabled
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserConfig':
        """Create from stored dictionary."""
        projects = {}
        for k, v in data.get("projects", {}).items():
            projects[k] = MonitoredProject.from_dict(v)

        return cls(
            projects=projects,
            enabled=data.get("enabled", True)
        )