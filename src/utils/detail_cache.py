"""Cache for storing detailed outputs for on-demand viewing.

Stores detailed command outputs temporarily so they can be displayed
in a modal when the user clicks "Show Details" button.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional


@dataclass
class CachedDetail:
    """A cached detailed output entry."""

    command_id: int
    content: str
    created_at: float


class DetailCache:
    """LRU cache for detailed outputs with TTL expiration.

    - Automatically expires entries after max_age_seconds
    - Limits total entries to max_entries (LRU eviction)
    - Cleans up on every access to prevent unbounded growth
    """

    _cache: OrderedDict[int, CachedDetail] = OrderedDict()
    _max_age_seconds: int = 28800  # 8 hour default
    _max_entries: int = 1000  # Maximum cached entries

    @classmethod
    def store(cls, command_id: int, content: str) -> None:
        """Store detailed output for a command.

        Parameters
        ----------
        command_id : int
            The command history ID.
        content : str
            The detailed output content.
        """
        # Remove existing entry first (to update LRU order)
        cls._cache.pop(command_id, None)

        cls._cache[command_id] = CachedDetail(
            command_id=command_id,
            content=content,
            created_at=time.time(),
        )

        # Clean up expired and enforce max size
        cls._cleanup()

    @classmethod
    def get(cls, command_id: int) -> Optional[str]:
        """Retrieve detailed output for a command.

        Parameters
        ----------
        command_id : int
            The command history ID.

        Returns
        -------
        str or None
            The detailed output if found and not expired, None otherwise.
        """
        entry = cls._cache.get(command_id)
        if not entry:
            return None

        # Check if expired
        if time.time() - entry.created_at > cls._max_age_seconds:
            cls._cache.pop(command_id, None)
            return None

        # Move to end (most recently accessed)
        cls._cache.move_to_end(command_id)
        return entry.content

    @classmethod
    def _cleanup(cls) -> None:
        """Remove expired entries and enforce max size."""
        now = time.time()

        # Remove expired entries
        expired = [
            cmd_id
            for cmd_id, entry in cls._cache.items()
            if now - entry.created_at > cls._max_age_seconds
        ]
        for cmd_id in expired:
            cls._cache.pop(cmd_id, None)

        # Enforce max size by removing oldest entries (LRU)
        while len(cls._cache) > cls._max_entries:
            cls._cache.popitem(last=False)  # Remove oldest (first) entry

    @classmethod
    def clear(cls) -> None:
        """Clear all cached entries."""
        cls._cache.clear()
