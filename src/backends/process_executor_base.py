"""Shared process-tracking lifecycle for subprocess-backed executors."""

import asyncio
from dataclasses import dataclass
from typing import Optional

from src.backends.process_registry import ProcessRegistry
from src.backends.process_termination import terminate_processes
from src.utils.execution_scope import build_session_scope
from src.utils.process_utils import terminate_process_safely


@dataclass(frozen=True)
class ProcessTrackingContext:
    """Process-tracking identifiers for a single executor run."""

    track_id: str
    session_scope: str


class ProcessExecutorBase:
    """Shared process-registry lifecycle used by backend executors."""

    def __init__(self) -> None:
        self._registry = ProcessRegistry()
        # Backwards-compatible aliases retained for tests/integration points.
        self._active_processes = self._registry.active_processes
        self._process_channels = self._registry.process_channels
        self._process_scopes = self._registry.process_scopes
        self._execution_track_ids = self._registry.execution_track_ids
        self._lock = self._registry.lock

    @staticmethod
    def create_tracking_context(
        execution_id: Optional[str],
        session_id: Optional[str],
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> ProcessTrackingContext:
        """Create stable tracking identifiers for a process execution."""
        track_id = ProcessRegistry.build_track_id(
            execution_id=execution_id,
            session_id=session_id,
            channel_id=channel_id,
        )
        session_scope = build_session_scope(channel_id or "", thread_ts)
        return ProcessTrackingContext(track_id=track_id, session_scope=session_scope)

    async def register_process(
        self,
        *,
        context: ProcessTrackingContext,
        process: asyncio.subprocess.Process,
        channel_id: Optional[str],
        execution_id: Optional[str],
    ) -> None:
        """Register a process in shared cancellation lookups."""
        await self._registry.register(
            track_id=context.track_id,
            process=process,
            channel_id=channel_id,
            session_scope=context.session_scope,
            execution_id=execution_id,
        )

    async def unregister_process(
        self,
        *,
        context: ProcessTrackingContext,
        execution_id: Optional[str],
    ) -> None:
        """Unregister a process from shared cancellation lookups."""
        await self._registry.unregister(
            track_id=context.track_id,
            execution_id=execution_id,
        )

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution."""
        tracked = await self._registry.pop_for_execution(execution_id)
        if not tracked:
            return False
        await terminate_process_safely(tracked.process)
        return True

    async def cancel_by_scope(self, session_scope: str) -> int:
        """Cancel active executions for a channel/thread session scope."""
        tracked = await self._registry.pop_for_scope(session_scope)
        await terminate_processes(entry.process for entry in tracked)
        return len(tracked)

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Cancel all active executions for a specific channel."""
        tracked = await self._registry.pop_for_channel(channel_id)
        await terminate_processes(entry.process for entry in tracked)
        return len(tracked)

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        tracked = await self._registry.pop_all()
        await terminate_processes(entry.process for entry in tracked)
        return len(tracked)

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()
