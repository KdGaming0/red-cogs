import asyncio
import aiohttp
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

log = logging.getLogger("red.modrinth_checker")


class ModrinthAPI:
    """Handle all Modrinth API interactions."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.api_base = "https://api.modrinth.com/v2"
        self.rate_limit = asyncio.Semaphore(5)  # 5 concurrent requests
        self.user_agent = "Red-DiscordBot/ModrinthChecker"

    async def _make_request(self, endpoint: str, params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """Make a request to the Modrinth API with proper error handling."""
        async with self.rate_limit:
            try:
                headers = {"User-Agent": self.user_agent}
                url = f"{self.api_base}/{endpoint}"

                async with self.session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:  # Rate limited
                        retry_after = int(response.headers.get("Retry-After", 60))
                        log.warning(f"Rate limited. Waiting {retry_after} seconds.")
                        await asyncio.sleep(retry_after)
                        return await self._make_request(endpoint, params)
                    else:
                        log.error(f"API request failed: {response.status} - {await response.text()}")
                        return None

            except Exception as e:
                log.error(f"API request error: {e}")
                return None

    async def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Get project information by ID."""
        return await self._make_request(f"project/{project_id}")

    async def get_project_versions(self, project_id: str, game_versions: List[str] = None,
                                   loaders: List[str] = None, featured: bool = None) -> Optional[List[Dict[str, Any]]]:
        """Get project versions with optional filtering."""
        params = {}
        if game_versions:
            params["game_versions"] = game_versions
        if loaders:
            params["loaders"] = loaders
        if featured is not None:
            params["featured"] = str(featured).lower()

        return await self._make_request(f"project/{project_id}/version", params)

    async def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        """Get specific version information."""
        return await self._make_request(f"version/{version_id}")

    async def search_projects(self, query: str, limit: int = 10) -> Optional[Dict[str, Any]]:
        """Search for projects."""
        params = {"query": query, "limit": limit}
        return await self._make_request("search", params)

    async def get_project_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Get project by slug (name)."""
        return await self._make_request(f"project/{slug}")

    async def get_latest_version(self, project_id: str, game_versions: List[str] = None,
                                 loaders: List[str] = None, featured: bool = None) -> Optional[Dict[str, Any]]:
        """Get the latest version of a project."""
        versions = await self.get_project_versions(project_id, game_versions, loaders, featured)
        if not versions:
            return None

        # Sort by date_published (most recent first)
        try:
            versions.sort(key=lambda x: datetime.fromisoformat(x["date_published"].replace('Z', '+00:00')),
                          reverse=True)
        except Exception as e:
            log.warning(f"Error sorting versions by date: {e}")
            # If date sorting fails, just return the first version

        return versions[0] if versions else None

    async def get_project_game_versions(self, project_id: str) -> List[str]:
        """Get all game versions supported by a project."""
        versions = await self.get_project_versions(project_id)
        if not versions:
            return []

        game_versions = set()
        for version in versions:
            game_versions.update(version.get("game_versions", []))

        # Convert to list and sort safely
        version_list = list(game_versions)
        try:
            # Try to sort versions intelligently
            from packaging import version as pkg_version

            def sort_key(v):
                try:
                    # Clean up the version string for parsing
                    clean_v = str(v).strip()
                    # Remove common prefixes
                    if clean_v.startswith('mc'):
                        clean_v = clean_v[2:]
                    if clean_v.startswith('v'):
                        clean_v = clean_v[1:]

                    # Try to parse as version
                    parsed = pkg_version.parse(clean_v)
                    return (0, parsed)  # 0 for releases, 1 for pre-releases
                except:
                    # Fall back to string representation for sorting
                    # Put snapshots at the end by using tuple (1, string)
                    if any(char.isalpha() for char in str(v)) and 'w' in str(v):
                        return (1, str(v))  # Likely a snapshot
                    return (0, str(v))  # Likely a release

            version_list.sort(key=sort_key, reverse=True)
        except Exception as e:
            log.warning(f"Error sorting game versions: {e}")
            # Fall back to simple string sorting
            try:
                version_list.sort(key=str, reverse=True)
            except Exception as e2:
                log.warning(f"Error in fallback sorting: {e2}")
                # If all else fails, just return the list as-is

        return version_list

    async def get_project_loaders(self, project_id: str) -> List[str]:
        """Get all loaders supported by a project."""
        versions = await self.get_project_versions(project_id)
        if not versions:
            return []

        loaders = set()
        for version in versions:
            loaders.update(version.get("loaders", []))

        return sorted(list(loaders))

    async def get_project_categories(self, project_id: str) -> List[str]:
        """Get project categories."""
        project = await self.get_project(project_id)
        if not project:
            return []

        return project.get("categories", [])

    async def validate_project_exists(self, project_id: str) -> bool:
        """Check if a project exists."""
        project = await self.get_project(project_id)
        return project is not None