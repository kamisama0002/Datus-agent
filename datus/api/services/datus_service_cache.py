"""Global LRU cache: project_id -> DatusService.

Uses Future-based thundering herd prevention — concurrent requests for
the same project_id share a single factory call.
"""

import asyncio
import collections
from typing import Awaitable, Callable, Optional

from datus.api.services.datus_service import DatusService
from datus.utils.loggings import get_logger

logger = get_logger(__name__)


class DatusServiceCache:
    """Async LRU cache for DatusService instances."""

    def __init__(self, max_size: int = 128):
        self._max_size = max_size
        self._cache: collections.OrderedDict[str, DatusService] = collections.OrderedDict()
        self._futures: dict[str, asyncio.Future[DatusService]] = {}
        self._future_fingerprints: dict[str, Optional[str]] = {}
        self._future_replacements: dict[
            asyncio.Future[DatusService], asyncio.Future[DatusService]
        ] = {}
        self._pending_evictions: set[str] = set()
        self._lock = asyncio.Lock()
        self._pending_tasks: set[asyncio.Task] = set()

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        """Register a background task so drain()/shutdown() can await it."""
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def get_or_create(
        self,
        project_id: str,
        factory: Callable[[], Awaitable[DatusService]],
        expected_fingerprint: Optional[str] = None,
    ) -> DatusService:
        """Return cached DatusService or create via factory (thundering-herd safe).

        If ``expected_fingerprint`` is provided and does not match the cached
        instance's ``config_fingerprint``, the stale entry is evicted before
        creating a new one.
        """
        is_creator = False
        stale_svc: Optional[DatusService] = None

        async with self._lock:
            # Fast path: cache hit
            if project_id in self._cache:
                cached = self._cache[project_id]
                if project_id in self._pending_evictions:
                    if cached.has_active_tasks():
                        self._cache.move_to_end(project_id)
                        return cached
                    stale_svc = self._cache.pop(project_id)
                    self._pending_evictions.discard(project_id)
                    logger.info(f"Applying deferred DatusService eviction for project {project_id}")
                elif expected_fingerprint is None or cached.config_fingerprint == expected_fingerprint:
                    self._cache.move_to_end(project_id)
                    return cached
                elif cached.has_active_tasks():
                    # A fingerprint swap would orphan the active task's
                    # interaction broker. Keep routing to the current instance.
                    self._cache.move_to_end(project_id)
                    logger.info(
                        f"Deferring DatusService rebuild for project {project_id}: "
                        f"AgentConfig fingerprint changed but tasks are still active"
                    )
                    return cached
                else:
                    stale_svc = self._cache.pop(project_id)
                    logger.info(
                        f"Evicting DatusService for project {project_id} due to AgentConfig fingerprint mismatch"
                    )

            # Another coroutine is already creating this entry — share its future
            if project_id in self._futures:
                existing_fut = self._futures[project_id]
                inflight_fingerprint = self._future_fingerprints.get(project_id)
                if expected_fingerprint is not None and inflight_fingerprint != expected_fingerprint:
                    # A newer configuration must not wait on or install an
                    # older in-flight factory. Chain old callers to the new
                    # generation so nobody receives an orphaned service.
                    fut = asyncio.get_running_loop().create_future()
                    self._futures[project_id] = fut
                    self._future_fingerprints[project_id] = expected_fingerprint
                    self._future_replacements[existing_fut] = fut
                    self._pending_evictions.discard(project_id)
                    is_creator = True
                else:
                    fut = existing_fut
            else:
                # We are the creator — register a future for waiters
                fut = asyncio.get_running_loop().create_future()
                self._futures[project_id] = fut
                self._future_fingerprints[project_id] = expected_fingerprint
                self._pending_evictions.discard(project_id)
                is_creator = True

        if stale_svc is not None:
            await self._dispose(project_id, stale_svc)

        if not is_creator:
            # Wait for the creator coroutine to finish
            return await fut

        # We are the creator — run the factory outside the lock
        try:
            svc = await factory()
        except Exception as e:
            async with self._lock:
                replacement = self._future_replacements.get(fut)
                if self._futures.get(project_id) is fut:
                    self._futures.pop(project_id, None)
                    self._future_fingerprints.pop(project_id, None)
            if replacement is not None:
                return await self._resolve_replacement(fut, replacement)
            if not fut.done():
                fut.set_exception(e)
                fut.exception()
            raise

        async with self._lock:
            replacement = self._future_replacements.get(fut)
            if self._futures.get(project_id) is fut:
                self._cache[project_id] = svc
                self._cache.move_to_end(project_id)
                self._futures.pop(project_id, None)
                self._future_fingerprints.pop(project_id, None)

                # Eviction can arrive while only the factory exists. Preserve
                # that pending marker on insertion; a following request either
                # keeps an active service routable or rebuilds it immediately.

                # Evict oldest if over capacity, but skip services with active tasks
                evicted = []
                while len(self._cache) > self._max_size:
                    # Find the oldest entry without active tasks
                    candidate_pid = None
                    for pid in self._cache:
                        if pid == project_id:
                            continue
                        if not self._cache[pid].has_active_tasks():
                            candidate_pid = pid
                            break
                    if candidate_pid is None:
                        break  # all entries have active tasks — allow cache to exceed max_size
                    old_svc = self._cache.pop(candidate_pid)
                    self._pending_evictions.discard(candidate_pid)
                    evicted.append((candidate_pid, old_svc))
            else:
                evicted = []

        if replacement is not None:
            await self._dispose(project_id, svc)
            return await self._resolve_replacement(fut, replacement)

        # Resolve the future so waiters get the result
        if not fut.done():
            fut.set_result(svc)

        # Shutdown evicted services outside the lock
        for old_pid, old_svc in evicted:
            logger.info(f"LRU evicting DatusService for project {old_pid}")
            self._track(asyncio.create_task(old_svc.shutdown()))

        return svc

    async def _resolve_replacement(
        self,
        superseded: asyncio.Future[DatusService],
        replacement: asyncio.Future[DatusService],
    ) -> DatusService:
        """Chain superseded creators and their waiters to the newest service."""
        try:
            service = await replacement
        except asyncio.CancelledError:
            if not superseded.done():
                superseded.cancel()
            raise
        except BaseException as exc:
            if not superseded.done():
                superseded.set_exception(exc)
            raise
        else:
            if not superseded.done():
                superseded.set_result(service)
            return service
        finally:
            async with self._lock:
                self._future_replacements.pop(superseded, None)

    async def evict(self, project_id: str) -> None:
        """Evict a DatusService from cache (config change).

        Active services remain routable until tasks drain. The next request
        applies the pending eviction and rebuilds the service.
        """
        async with self._lock:
            has_inflight_factory = project_id in self._futures
            if has_inflight_factory:
                self._pending_evictions.add(project_id)
            svc = self._cache.get(project_id)
            if svc is not None and svc.has_active_tasks():
                self._pending_evictions.add(project_id)
                self._cache.move_to_end(project_id)
                logger.info(f"Deferring DatusService eviction for project {project_id}: tasks are still active")
                return
            svc = self._cache.pop(project_id, None)
            if not has_inflight_factory:
                self._pending_evictions.discard(project_id)
        if not svc:
            return
        await self._dispose(project_id, svc)

    async def _dispose(self, project_id: str, svc: DatusService) -> None:
        """Shutdown a service, deferring if it still has active tasks."""
        if svc.has_active_tasks():
            logger.info(f"Evicting DatusService for project {project_id} (deferring shutdown — active tasks)")
            self._track(asyncio.create_task(self._deferred_shutdown(project_id, svc)))
        else:
            logger.info(f"Evicting DatusService for project {project_id}")
            await svc.shutdown()

    @staticmethod
    async def _deferred_shutdown(project_id: str, svc: DatusService) -> None:
        """Wait for active tasks to drain, then shutdown."""
        try:
            await svc.task_manager.wait_all_tasks()
            await svc.shutdown()
            logger.info(f"Deferred shutdown completed for project {project_id}")
        except Exception:
            logger.exception(f"Error in deferred shutdown for project {project_id}")

    async def drain(self) -> None:
        """Await all pending background shutdown tasks before the loop closes."""
        if self._pending_tasks:
            results = await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("Background shutdown task failed", exc_info=result)

    async def shutdown(self) -> None:
        """Shutdown all cached DatusService instances (application exit)."""
        await self.drain()
        async with self._lock:
            items = list(self._cache.items())
            self._cache.clear()
        for pid, svc in items:
            try:
                await svc.shutdown()
            except Exception:
                logger.exception(f"Error shutting down DatusService for project {pid}")
        logger.info("DatusServiceCache shut down")
