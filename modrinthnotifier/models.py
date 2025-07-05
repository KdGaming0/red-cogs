"""Data models and validation for the Modrinth Notifier cog."""

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
                       required_game_versions: Optional[List[str]] = None) -> bool:
        """Check if this version matches the specified filters."""
        if required_loaders:
            if not any(loader in self.loaders for loader in required_loaders):
                return False

        if required_game_versions:
            if not any(version in self.game_versions for version in required_game_versions):
                return False

        return True

@dataclass
class ChannelMonitor:
    """Represents monitoring configuration for a specific channel."""
    channel_id: int
    role_ids: List[int] = field(default_factory=list)
    required_loaders: Optional[List[str]] = None
    required_game_versions: Optional[List[str]] = None
    last_version: Optional[str] = None
    added_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "channel_id": self.channel_id,
            "role_ids": self.role_ids,
            "required_loaders": self.required_loaders,
            "required_game_versions": self.required_game_versions,
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
            last_version=data.get("last_version"),
            added_at=data.get("added_at", datetime.utcnow().timestamp())
        )

    def matches_version(self, version: VersionInfo) -> bool:
        """Check if this channel should be notified for this version."""
        return version.matches_filters(self.required_loaders, self.required_game_versions)

@dataclass
class MonitoredProject:
    """Represents a project being monitored."""
    id: str
    name: str
    channels: Dict[int, ChannelMonitor] = field(default_factory=dict)  # channel_id -> ChannelMonitor
    added_by: Optional[int] = None
    added_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    # Legacy fields for backwards compatibility
    last_version: Optional[str] = None
    role_ids: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "name": self.name,
            "channels": {str(cid): monitor.to_dict() for cid, monitor in self.channels.items()},
            "added_by": self.added_by,
            "added_at": self.added_at,
            # Legacy fields
            "last_version": self.last_version,
            "role_ids": self.role_ids
        }

    @classmethod
    def from_dict(cls, project_id: str, data: Dict[str, Any]) -> 'MonitoredProject':
        """Create from stored dictionary."""
        project = cls(
            id=project_id,
            name=data["name"],
            added_by=data.get("added_by"),
            added_at=data.get("added_at", datetime.utcnow().timestamp()),
            # Legacy fields
            last_version=data.get("last_version"),
            role_ids=data.get("role_ids", [])
        )

        # Load channels
        channels_data = data.get("channels", {})
        for channel_id_str, channel_data in channels_data.items():
            channel_id = int(channel_id_str)
            project.channels[channel_id] = ChannelMonitor.from_dict(channel_data)

        # Convert legacy single-channel format to new multi-channel format
        if not project.channels and "last_version" in data:
            # This is a legacy project, we'll convert it when we have guild context
            pass

        return project

    def add_channel(self, channel_id: int, role_ids: List[int] = None,
                   required_loaders: List[str] = None, required_game_versions: List[str] = None) -> ChannelMonitor:
        """Add a channel monitor to this project."""
        monitor = ChannelMonitor(
            channel_id=channel_id,
            role_ids=role_ids or [],
            required_loaders=required_loaders,
            required_game_versions=required_game_versions
        )
        self.channels[channel_id] = monitor
        return monitor

    def remove_channel(self, channel_id: int) -> bool:
        """Remove a channel monitor from this project."""
        return self.channels.pop(channel_id, None) is not None

    def get_matching_channels(self, version: VersionInfo) -> List[ChannelMonitor]:
        """Get all channels that should be notified for this version."""
        return [monitor for monitor in self.channels.values() if monitor.matches_version(version)]

@dataclass
class UserProject:
    """Represents a project in a user's watchlist."""
    id: str
    name: str
    last_version: Optional[str] = None
    required_loaders: Optional[List[str]] = None
    required_game_versions: Optional[List[str]] = None
    added_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "name": self.name,
            "last_version": self.last_version,
            "required_loaders": self.required_loaders,
            "required_game_versions": self.required_game_versions,
            "added_at": self.added_at
        }

    @classmethod
    def from_dict(cls, project_id: str, data: Dict[str, Any]) -> 'UserProject':
        """Create from stored dictionary."""
        return cls(
            id=project_id,
            name=data["name"],
            last_version=data.get("last_version"),
            required_loaders=data.get("required_loaders"),
            required_game_versions=data.get("required_game_versions"),
            added_at=data.get("added_at", datetime.utcnow().timestamp())
        )

    def matches_version(self, version: VersionInfo) -> bool:
        """Check if this user should be notified for this version."""
        return version.matches_filters(self.required_loaders, self.required_game_versions)

@dataclass
class GuildConfig:
    """Configuration for a guild."""
    # Legacy single-channel support (for backwards compatibility)
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

    def convert_legacy_projects(self):
        """Convert legacy single-channel projects to multi-channel format."""
        if not self.channel_id:
            return

        for project in self.projects.values():
            # If project has no channels but has legacy data, convert it
            if not project.channels and project.last_version is not None:
                monitor = project.add_channel(
                    channel_id=self.channel_id,
                    role_ids=project.role_ids.copy()
                )
                monitor.last_version = project.last_version
                # Clear legacy fields
                project.last_version = None
                project.role_ids = []

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