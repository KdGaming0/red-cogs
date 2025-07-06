import re
from typing import List, Optional, Dict, Any
from packaging import version
import logging

log = logging.getLogger("red.modrinth_checker")


def extract_version_number(version_string: str) -> Optional[str]:
    """Extract version number from a version string using various patterns."""
    patterns = [
        r'(\d+\.\d+\.\d+)',  # X.Y.Z
        r'(\d+\.\d+)',  # X.Y
        r'v(\d+\.\d+\.\d+)',  # vX.Y.Z
        r'v(\d+\.\d+)',  # vX.Y
        r'(\d+\.\d+\.\d+\.\d+)',  # X.Y.Z.W
    ]

    for pattern in patterns:
        match = re.search(pattern, version_string)
        if match:
            return match.group(1)

    return None


def compare_versions(version1: str, version2: str) -> int:
    """Compare two version strings. Returns 1 if version1 > version2, -1 if version1 < version2, 0 if equal."""
    try:
        # Try to parse as semantic versions
        v1 = version.parse(str(version1))
        v2 = version.parse(str(version2))

        if v1 > v2:
            return 1
        elif v1 < v2:
            return -1
        else:
            return 0
    except Exception as e:
        log.warning(f"Version comparison failed for '{version1}' vs '{version2}': {e}")
        # Fall back to string comparison
        if str(version1) > str(version2):
            return 1
        elif str(version1) < str(version2):
            return -1
        else:
            return 0


def is_snapshot(version_string: str) -> bool:
    """Check if a version string represents a snapshot version."""
    version_lower = str(version_string).lower()

    # Minecraft snapshot patterns
    snapshot_patterns = [
        r'\d+w\d+[a-z]',  # e.g., 25w21a, 24w03b
        r'snapshot',  # Contains "snapshot"
        r'pre\d*',  # pre-release (pre1, pre2, etc.)
        r'rc\d*',  # release candidate
        r'alpha',  # alpha
        r'beta',  # beta
        r'dev',  # development
        r'experimental',  # experimental
        r'test',  # test
    ]

    # Check if it matches any snapshot pattern
    for pattern in snapshot_patterns:
        if re.search(pattern, version_lower):
            return True

    return False


def filter_minecraft_versions(versions: List[str], include_snapshots: bool = False) -> List[str]:
    """Filter Minecraft versions based on whether to include snapshots."""
    if include_snapshots:
        return versions

    return [v for v in versions if not is_snapshot(v)]


def format_version_list(versions: List[str], max_display: int = 10) -> str:
    """Format a list of versions for display, showing only the most recent ones."""
    if not versions:
        return "None"

    try:
        # Sort versions in descending order
        def sort_key(x):
            try:
                # Try to parse as semantic version
                return version.parse(str(x))
            except:
                # Fall back to string sorting
                return str(x)

        sorted_versions = sorted(versions, key=sort_key, reverse=True)
    except Exception as e:
        log.warning(f"Error sorting versions: {e}")
        # Fall back to simple string sorting
        sorted_versions = sorted(versions, reverse=True)

    if len(sorted_versions) <= max_display:
        return ", ".join(sorted_versions)
    else:
        displayed = sorted_versions[:max_display]
        remaining = len(sorted_versions) - max_display
        return f"{', '.join(displayed)} (+{remaining} more)"


def validate_project_id(project_id: str) -> bool:
    """Validate if a string looks like a valid Modrinth project ID."""
    # Modrinth project IDs are typically 8 characters of alphanumeric characters
    return bool(re.match(r'^[A-Za-z0-9]{8}$', project_id))


def truncate_text(text: str, max_length: int = 2000) -> str:
    """Truncate text to fit within Discord's limits."""
    if len(text) <= max_length:
        return text

    # Try to cut at a sensible point (sentence or paragraph)
    truncated = text[:max_length - 3]

    # Find the last sentence ending
    last_sentence = max(
        truncated.rfind('.'),
        truncated.rfind('!'),
        truncated.rfind('?'),
        truncated.rfind('\n\n')
    )

    if last_sentence > max_length * 0.7:  # If we can keep at least 70% of the text
        return truncated[:last_sentence + 1] + "..."
    else:
        return truncated + "..."