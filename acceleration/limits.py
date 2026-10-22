"""Shared admission limit for workspace jobs in the application's event loop."""
import asyncio
from weakref import WeakKeyDictionary

from config.settings import settings

_task_slots = WeakKeyDictionary()


def get_workspace_task_slots() -> asyncio.Semaphore:
    """All workflows and customer-service workers share one set of task slots.

    ponytail: single-process scope; use Redis leases before adding app workers.
    """
    loop = asyncio.get_running_loop()
    if loop not in _task_slots:
        _task_slots[loop] = asyncio.Semaphore(settings.WORKSPACE_MAX_CONCURRENT_TASKS)
    return _task_slots[loop]
