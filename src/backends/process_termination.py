"""Shared helpers for terminating tracked subprocesses safely."""

import asyncio
from collections.abc import Iterable

from src.utils.process_utils import terminate_process_safely


async def terminate_processes(processes: Iterable[asyncio.subprocess.Process]) -> None:
    """Terminate all provided processes, ignoring individual failures."""
    process_list = list(processes)
    if not process_list:
        return
    await asyncio.gather(
        *(terminate_process_safely(process) for process in process_list),
        return_exceptions=True,
    )
