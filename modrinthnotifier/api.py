"""Modrinth API wrapper with rate limiting and error handling."""

import aiohttp
import asyncio
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from .models import ProjectInfo, VersionInfo

log = logging.getLogger("red.modrinthnotifier.api")


class RateLimiter:
    """Simple rate limiter for API requests."""

    def __init__(self, max_requests: int = 250, window: int = 60):
        self.max_requests = max_requests
        self.window = window
        self.requests = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Wait if necessary to respect rate limits."""
        async with self._lock:
            now = datetime.utcnow()
            # Remove old requests outside the window
            self.requests = [req_time for req_time in self.requests
                             if now - req_time < timedelta(seconds=self.window)]

            if len(self.requests) >= self.max_requests:
                # Calculate wait time
                oldest_request = min(self.requests)
                wait_time = (oldest_request + timedelta(seconds=self.window) - now).total_seconds()
                if wait_time > 0:
                    log.info(f"Rate limit reached, waiting {wait_time:.2f} seconds")
                    await asyncio.sleep(wait_time)

            self.requests.append(now)


class ModrinthAPIError(Exception):
    """Base exception for Modrinth API errors."""
    pass


class ProjectNotFoundError(ModrinthAPIError):
    """Raised when a project is not found."""
    pass


class RateLimitError(ModrinthAPIError):
    """Raised when rate limited."""
    pass


class ModrinthAPI:
    """Modrinth API client with rate limiting and error handling."""

    BASE_URL = "https://api.modrinth.com/v2"
    USER_AGENT = "KdGaming0/ModrinthNotifier/1.0.0 (Discord Bot)"

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.rate_limiter = RateLimiter()

    async def __aenter__(self):
        await self.start_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_session()

    async def start_session(self):
        """Start the aiohttp session."""
        if self.session is None or self.session.closed:
            headers = {"User-Agent": self.USER_AGENT}
            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)

    async def close_session(self):
        """Close the aiohttp session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _make_request(self, endpoint: str, params: Optional[Dict] = None, retries: int = 3) -> Dict[str, Any]:
        """Make a request to the Modrinth API with retry logic."""
        await self.rate_limiter.acquire()

        url = f"{self.BASE_URL}{endpoint}"

        for attempt in range(retries):
            try:
                if not self.session or self.session.closed:
                    await self.start_session()

                async with self.session.get(url, params=params) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 404:
                        raise ProjectNotFoundError(f"Project not found: {endpoint}")
                    elif response.status == 429:
                        # Rate limited, wait and retry
                        retry_after = int(response.headers.get("Retry-After", 60))
                        log.warning(f"Rate limited, waiting {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                        continue
                    elif response.status >= 500:
                        # Server error, retry with exponential backoff
                        if attempt < retries - 1:
                            wait_time = 2 ** attempt
                            log.warning(f"Server error {response.status}, retrying in {wait_time}s")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            raise ModrinthAPIError(f"Server error: {response.status}")
                    else:
                        raise ModrinthAPIError(f"Unexpected status: {response.status}")

            except aiohttp.ClientError as e:
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    log.warning(f"Network error: {e}, retrying in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    raise ModrinthAPIError(f"Network error: {e}")

        raise ModrinthAPIError("Max retries exceeded")

    async def get_project(self, project_id: str) -> ProjectInfo:
        """Get project information by ID or slug."""
        data = await self._make_request(f"/project/{project_id}")
        return ProjectInfo.from_api_data(data)

    async def get_projects(self, project_ids: List[str]) -> List[ProjectInfo]:
        """Get multiple projects by IDs."""
        if not project_ids:
            return []

        # Modrinth supports batch requests with comma-separated IDs
        ids_param = ",".join(project_ids)
        data = await self._make_request("/projects", params={"ids": f'["{ids_param}"]'})

        if isinstance(data, list):
            return [ProjectInfo.from_api_data(item) for item in data]
        else:
            # Single project returned as object
            return [ProjectInfo.from_api_data(data)]

    async def get_project_versions(self, project_id: str, limit: int = 10) -> List[VersionInfo]:
        """Get versions for a project."""
        params = {"limit": limit}
        data = await self._make_request(f"/project/{project_id}/version", params=params)
        return [VersionInfo.from_api_data(version) for version in data]

    async def get_version(self, version_id: str) -> VersionInfo:
        """Get specific version information."""
        data = await self._make_request(f"/version/{version_id}")
        return VersionInfo.from_api_data(data)

    async def get_latest_version(self, project_id: str) -> Optional[VersionInfo]:
        """Get the latest version for a project."""
        try:
            versions = await self.get_project_versions(project_id, limit=1)
            return versions[0] if versions else None
        except ProjectNotFoundError:
            return None

    async def validate_project_id(self, project_id: str) -> Optional[str]:
        """Validate a project ID and return the project name if valid."""
        try:
            project = await self.get_project(project_id)
            return project.name
        except ProjectNotFoundError:
            return None