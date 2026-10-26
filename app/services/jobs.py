import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import settings


class JobRunner:
    """Run background jobs with a global concurrency limit.

    A semaphore is created per event loop, so the runner is safe to use from tests
    that call ``asyncio.run`` repeatedly. The actual job is passed as a factory:
    otherwise a coroutine waiting for a slot would be created and never awaited
    when the application shuts down.
    """

    def __init__(self, max_concurrent: int = 1) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
            self._loop = loop
        return self._semaphore

    def submit(self, factory: Callable[[], Awaitable[None]]) -> asyncio.Task:
        task = asyncio.create_task(self._run(factory))
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    async def _run(self, factory: Callable[[], Awaitable[None]]) -> None:
        async with self._get_semaphore():
            if self._closing:
                return
            await factory()

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("Background job failed")

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Stop accepting jobs and cancel the running ones."""
        self._closing = True
        tasks = list(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            logging.warning("Background job did not stop within %.1fs", timeout)


job_runner = JobRunner(settings.max_concurrent_jobs)
