"""Enhanced Modrinth API client with search functionality."""

import asyncio
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

import aiohttp

from .models import ProjectInfo, VersionInfo

log = logging.getLogger("red.modrinthnotifier.api")

class RateLimiter:
    """Rate limiter for Modrinth API requests."""

    def __init__(self, max_requests: int = 250, window: int = 60):
        self.max_requests = max_requests
        self.window = window
        self.requests: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire permission to make a request."""
        async with self._lock:
            now = datetime.utcnow().timestamp()
            self.requests = [req for req in self.requests if now - req < self.window]

            if len(self.requests) >= self.max_requests:
                wait_time = self.window - (now - self.requests[0])
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

class ModrinthAPI:
    """Enhanced Modrinth API client with search functionality."""

    BASE_URL = "https://api.modrinth.com/v2"
    USER_AGENT = "KdGaming0/ModrinthNotifier/2.0.0 (Discord Bot)"

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
            timeout = aiohttp.ClientTimeout(total=15)
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
                        retry_after = int(response.headers.get("Retry-After", 60))
                        log.warning(f"Rate limited, waiting {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                        continue
                    elif response.status >= 500:
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

    async def search_projects(self, query: str, limit: int = 10) -> List[ProjectInfo]:
        """Search for projects on Modrinth."""
        params = {
            "query": query,
            "limit": limit,
            "index": "relevance"
        }

        try:
            data = await self._make_request("/search", params)
            return [ProjectInfo.from_api_data(hit) for hit in data.get("hits", [])]
        except Exception as e:
            log.error(f"Error searching projects: {e}")
            raise ModrinthAPIError(f"Search failed: {e}")

    async def get_project(self, project_id: str) -> ProjectInfo:
        """Get project information by ID or slug."""
        try:
            data = await self._make_request(f"/project/{project_id}")
            return ProjectInfo.from_api_data(data)
        except ProjectNotFoundError:
            raise
        except Exception as e:
            log.error(f"Error fetching project {project_id}: {e}")
            raise ModrinthAPIError(f"Failed to fetch project: {e}")

    async def get_project_versions(self, project_id: str, limit: int = 100,
                                 loaders: Optional[List[str]] = None,
                                 game_versions: Optional[List[str]] = None) -> List[VersionInfo]:
        """Get versions for a project with optional filtering."""
        params = {"limit": limit}

        if loaders:
            params["loaders"] = '["' + '","'.join(loaders) + '"]'
        if game_versions:
            params["game_versions"] = '["' + '","'.join(game_versions) + '"]'

        try:
            data = await self._make_request(f"/project/{project_id}/version", params)
            return [VersionInfo.from_api_data(version) for version in data]
        except ProjectNotFoundError:
            raise
        except Exception as e:
            log.error(f"Error fetching versions for {project_id}: {e}")
            raise ModrinthAPIError(f"Failed to fetch versions: {e}")

    async def get_all_project_versions(self, project_id: str) -> List[VersionInfo]:
        """Get ALL versions for a project to determine complete support."""
        try:
            all_versions = []
            offset = 0
            limit = 100

            while True:
                data = await self._make_request(f"/project/{project_id}/version", {"limit": limit, "offset": offset})
                versions = [VersionInfo.from_api_data(version) for version in data]

                if not versions:
                    break

                all_versions.extend(versions)

                if len(versions) < limit:
                    break

                offset += limit

                if len(all_versions) >= 1000:  # Safety limit
                    break

            return all_versions

        except ProjectNotFoundError:
            raise
        except Exception as e:
            log.error(f"Error fetching all versions for {project_id}: {e}")
            raise ModrinthAPIError(f"Failed to fetch all versions: {e}")