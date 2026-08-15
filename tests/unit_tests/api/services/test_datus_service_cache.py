"""Tests for datus.api.services.datus_service_cache — async LRU cache."""

import asyncio
import gc
from unittest.mock import AsyncMock, MagicMock

import pytest

from datus.api.services.datus_service_cache import DatusServiceCache


def _mock_service(project_id="p1", has_active=False, fingerprint="fp-default"):
    """Create a mock DatusService."""
    svc = MagicMock()
    svc.project_id = project_id
    svc.config_fingerprint = fingerprint
    svc.has_active_tasks.return_value = has_active
    svc.shutdown = AsyncMock()
    svc.task_manager = MagicMock()
    svc.task_manager.wait_all_tasks = AsyncMock()
    return svc


class TestDatusServiceCacheInit:
    """Tests for cache initialization."""

    def test_default_max_size(self):
        """Default max_size is 128."""
        cache = DatusServiceCache()
        assert cache._max_size == 128

    def test_custom_max_size(self):
        """Custom max_size is respected."""
        cache = DatusServiceCache(max_size=5)
        assert cache._max_size == 5

    def test_cache_starts_empty(self):
        """Cache and futures dicts are empty on init."""
        cache = DatusServiceCache()
        assert len(cache._cache) == 0
        assert len(cache._futures) == 0


@pytest.mark.asyncio
class TestGetOrCreate:
    """Tests for get_or_create — cache hit, miss, and thundering herd."""

    async def test_cache_miss_calls_factory(self):
        """Factory is called on first access for a project_id."""
        cache = DatusServiceCache()
        svc = _mock_service("proj-1")

        async def factory():
            return svc

        result = await cache.get_or_create("proj-1", factory)
        assert result is svc
        assert "proj-1" in cache._cache

    async def test_cache_hit_returns_existing(self):
        """Second call returns cached instance, not factory."""
        cache = DatusServiceCache()
        svc = _mock_service("proj-2")
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            return svc

        await cache.get_or_create("proj-2", factory)
        result = await cache.get_or_create("proj-2", factory)
        assert result is svc
        assert call_count == 1

    async def test_different_projects_get_different_services(self):
        """Different project_ids get separate cache entries."""
        cache = DatusServiceCache()
        svc_a = _mock_service("a")
        svc_b = _mock_service("b")

        result_a = await cache.get_or_create("a", AsyncMock(return_value=svc_a))
        result_b = await cache.get_or_create("b", AsyncMock(return_value=svc_b))
        assert result_a is svc_a
        assert result_b is svc_b
        assert len(cache._cache) == 2

    async def test_factory_exception_propagates(self):
        """Factory exception propagates and doesn't leave stale future."""
        cache = DatusServiceCache()

        async def bad_factory():
            raise ValueError("config error")

        with pytest.raises(ValueError, match="config error"):
            await cache.get_or_create("fail", bad_factory)

        assert "fail" not in cache._cache
        assert "fail" not in cache._futures

    async def test_factory_exception_is_consumed_when_creator_has_no_waiters(self):
        """Creator failure must not emit an un-retrieved Future exception."""
        cache = DatusServiceCache()
        loop = asyncio.get_running_loop()
        contexts = []
        previous_handler = loop.get_exception_handler()

        async def bad_factory():
            raise ValueError("config error")

        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        try:
            with pytest.raises(ValueError, match="config error"):
                await cache.get_or_create("fail-without-waiters", bad_factory)
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert not [
            context
            for context in contexts
            if context.get("message") == "Future exception was never retrieved"
        ]

    async def test_factory_exception_propagates_to_creator_and_waiter(self):
        """Consuming the creator Future must not swallow waiter failures."""
        cache = DatusServiceCache()
        started = asyncio.Event()
        release = asyncio.Event()

        async def bad_factory():
            started.set()
            await release.wait()
            raise ValueError("shared config error")

        creator = asyncio.create_task(cache.get_or_create("shared-failure", bad_factory))
        await started.wait()
        waiter = asyncio.create_task(cache.get_or_create("shared-failure", bad_factory))
        await asyncio.sleep(0)
        release.set()

        results = await asyncio.gather(creator, waiter, return_exceptions=True)

        assert len(results) == 2
        assert all(isinstance(result, ValueError) for result in results)
        assert all(str(result) == "shared config error" for result in results)
        assert "shared-failure" not in cache._futures

    async def test_lru_eviction_when_over_capacity(self):
        """Oldest inactive entry is evicted when cache exceeds max_size."""
        cache = DatusServiceCache(max_size=2)

        svc_a = _mock_service("a", has_active=False)
        svc_b = _mock_service("b", has_active=False)
        svc_c = _mock_service("c", has_active=False)

        await cache.get_or_create("a", AsyncMock(return_value=svc_a))
        await cache.get_or_create("b", AsyncMock(return_value=svc_b))
        await cache.get_or_create("c", AsyncMock(return_value=svc_c))

        # 'a' should be evicted (oldest)
        assert "a" not in cache._cache
        assert "b" in cache._cache
        assert "c" in cache._cache

    async def test_active_tasks_skip_eviction(self):
        """Entries with active tasks are not evicted, cache may exceed max_size."""
        cache = DatusServiceCache(max_size=2)

        svc_a = _mock_service("a", has_active=True)
        svc_b = _mock_service("b", has_active=True)
        svc_c = _mock_service("c", has_active=False)

        await cache.get_or_create("a", AsyncMock(return_value=svc_a))
        await cache.get_or_create("b", AsyncMock(return_value=svc_b))
        await cache.get_or_create("c", AsyncMock(return_value=svc_c))

        # All should remain since a and b have active tasks
        assert len(cache._cache) == 3

    async def test_thundering_herd_shares_factory_result(self):
        """Concurrent requests for same project_id share a single factory call."""
        cache = DatusServiceCache()
        svc = _mock_service("shared")
        call_count = 0

        async def slow_factory():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return svc

        results = await asyncio.gather(
            cache.get_or_create("shared", slow_factory),
            cache.get_or_create("shared", slow_factory),
            cache.get_or_create("shared", slow_factory),
        )
        assert all(r is svc for r in results)
        assert call_count == 1


@pytest.mark.asyncio
class TestFingerprintEviction:
    """Tests for fingerprint-based stale-entry eviction in get_or_create."""

    async def test_matching_fingerprint_returns_cached(self):
        cache = DatusServiceCache()
        svc = _mock_service("p", fingerprint="fp-1")
        await cache.get_or_create("p", AsyncMock(return_value=svc))

        factory = AsyncMock()
        result = await cache.get_or_create("p", factory, expected_fingerprint="fp-1")
        assert result is svc
        factory.assert_not_called()

    async def test_mismatched_fingerprint_evicts_and_rebuilds(self):
        cache = DatusServiceCache()
        old = _mock_service("p", fingerprint="fp-old")
        await cache.get_or_create("p", AsyncMock(return_value=old))

        new = _mock_service("p", fingerprint="fp-new")
        factory = AsyncMock(return_value=new)
        result = await cache.get_or_create("p", factory, expected_fingerprint="fp-new")

        assert result is new
        factory.assert_awaited_once()
        old.shutdown.assert_awaited_once()
        assert cache._cache["p"] is new

    async def test_mismatched_fingerprint_with_active_tasks_defers_rebuild(self):
        """Fingerprint changes are deferred while tasks are active.

        Rebuilding would orphan the in-flight task's interaction broker, so the
        existing instance must keep serving requests (e.g. a pending
        /chat/user_interaction answer) until its tasks drain.
        """
        cache = DatusServiceCache()
        old = _mock_service("p", has_active=True, fingerprint="fp-old")
        await cache.get_or_create("p", AsyncMock(return_value=old))

        factory = AsyncMock()
        result = await cache.get_or_create("p", factory, expected_fingerprint="fp-new")

        # No rebuild: the busy instance is preserved and returned as-is.
        factory.assert_not_called()
        old.shutdown.assert_not_awaited()
        assert result is old
        assert cache._cache["p"] is old

    async def test_mismatched_fingerprint_rebuilds_after_tasks_drain(self):
        """Once the busy instance goes idle, the next request rebuilds."""
        cache = DatusServiceCache()
        old = _mock_service("p", has_active=True, fingerprint="fp-old")
        await cache.get_or_create("p", AsyncMock(return_value=old))

        # First mismatched request is deferred (tasks still active).
        await cache.get_or_create("p", AsyncMock(return_value=_mock_service("p")), expected_fingerprint="fp-new")
        assert cache._cache["p"] is old

        # Task drained — the next mismatched request evicts and rebuilds.
        old.has_active_tasks.return_value = False
        new = _mock_service("p", fingerprint="fp-new")
        result = await cache.get_or_create("p", AsyncMock(return_value=new), expected_fingerprint="fp-new")

        assert result is new
        old.shutdown.assert_awaited_once()
        assert cache._cache["p"] is new

    async def test_none_fingerprint_preserves_legacy_behavior(self):
        cache = DatusServiceCache()
        svc = _mock_service("p", fingerprint="fp-1")
        await cache.get_or_create("p", AsyncMock(return_value=svc))

        factory = AsyncMock()
        result = await cache.get_or_create("p", factory)  # no expected_fingerprint
        assert result is svc
        factory.assert_not_called()

    async def test_inflight_mismatched_fingerprint_supersedes_factory_result(self):
        """A config-B request cannot join or install config A's in-flight future."""
        cache = DatusServiceCache()
        started_a = asyncio.Event()
        release_a = asyncio.Event()
        started_b = asyncio.Event()
        old = _mock_service("p", fingerprint="fp-a")
        new = _mock_service("p", fingerprint="fp-b")

        async def factory_a():
            started_a.set()
            await release_a.wait()
            return old

        async def factory_b():
            started_b.set()
            return new

        request_a = asyncio.create_task(cache.get_or_create("p", factory_a, expected_fingerprint="fp-a"))
        await started_a.wait()
        await cache.evict("p")
        request_b = asyncio.create_task(cache.get_or_create("p", factory_b, expected_fingerprint="fp-b"))
        await asyncio.sleep(0)
        release_a.set()

        result_a, result_b = await asyncio.gather(request_a, request_b)

        assert started_b.is_set()
        assert result_a is new
        assert result_b is new
        assert cache._cache["p"] is new
        assert cache._cache["p"].config_fingerprint == "fp-b"
        old.shutdown.assert_awaited_once()
        new.shutdown.assert_not_awaited()

    @pytest.mark.parametrize("next_fingerprint", ["fp-a", None])
    async def test_inflight_explicit_eviction_survives_until_delayed_next_request(self, next_fingerprint):
        """Eviction during construction forces a rebuild even without fingerprint drift."""
        cache = DatusServiceCache()
        started = asyncio.Event()
        release = asyncio.Event()
        evicted = _mock_service("p", fingerprint="fp-a")
        replacement = _mock_service("p", fingerprint="fp-a")

        async def delayed_factory():
            started.set()
            await release.wait()
            return evicted

        first_request = asyncio.create_task(
            cache.get_or_create("p", delayed_factory, expected_fingerprint="fp-a")
        )
        await started.wait()
        await cache.evict("p")
        release.set()
        assert await first_request is evicted

        # No replacement request existed when eviction happened. The stale
        # generation may finish, but its pending marker must survive insertion.
        assert cache._cache["p"] is evicted
        assert "p" in cache._pending_evictions

        factory = AsyncMock(return_value=replacement)
        result = await cache.get_or_create(
            "p",
            factory,
            expected_fingerprint=next_fingerprint,
        )

        assert result is replacement
        assert cache._cache["p"] is replacement
        assert "p" not in cache._pending_evictions
        factory.assert_awaited_once()
        evicted.shutdown.assert_awaited_once()


@pytest.mark.asyncio
class TestEvict:
    """Tests for evict — cache removal and deferred shutdown."""

    async def test_evict_removes_from_cache(self):
        """Evict removes the service and calls shutdown."""
        cache = DatusServiceCache()
        svc = _mock_service("proj-e")
        await cache.get_or_create("proj-e", AsyncMock(return_value=svc))

        await cache.evict("proj-e")
        assert "proj-e" not in cache._cache
        svc.shutdown.assert_awaited_once()

    async def test_evict_nonexistent_is_noop(self):
        """Evicting a non-existent key doesn't raise."""
        cache = DatusServiceCache()
        await cache.evict("ghost")
        assert cache._cache == {}

    async def test_evict_with_active_tasks_preserves_routing_until_drain(self):
        """Config eviction cannot orphan an active task's interaction broker."""
        cache = DatusServiceCache()
        svc = _mock_service("defer-proj", has_active=True)
        await cache.get_or_create("defer-proj", AsyncMock(return_value=svc))

        await cache.evict("defer-proj")
        assert cache._cache["defer-proj"] is svc
        svc.task_manager.wait_all_tasks.assert_not_awaited()
        svc.shutdown.assert_not_awaited()


@pytest.mark.asyncio
class TestPendingTaskTracking:
    """Tests for _track() / drain() — background task lifecycle management."""

    async def test_lru_eviction_task_is_tracked(self):
        """LRU eviction create_task is registered in _pending_tasks."""
        cache = DatusServiceCache(max_size=1)
        svc_a = _mock_service("a", has_active=False)
        svc_b = _mock_service("b", has_active=False)

        # Pause the shutdown task so it stays in _pending_tasks when we check.
        pause = asyncio.Event()

        async def _paused_shutdown():
            await pause.wait()

        svc_a.shutdown = AsyncMock(side_effect=_paused_shutdown)

        await cache.get_or_create("a", AsyncMock(return_value=svc_a))
        await cache.get_or_create("b", AsyncMock(return_value=svc_b))

        assert len(cache._pending_tasks) == 1

        pause.set()
        await cache.drain()

    async def test_deferred_shutdown_task_is_tracked(self):
        """_dispose with active tasks registers the deferred-shutdown Task."""
        cache = DatusServiceCache()
        pause = asyncio.Event()

        async def _paused_wait():
            await pause.wait()

        svc = _mock_service("p", has_active=True)
        svc.task_manager.wait_all_tasks = AsyncMock(side_effect=_paused_wait)
        await cache.get_or_create("p", AsyncMock(return_value=svc))

        await cache._dispose("p", svc)

        assert len(cache._pending_tasks) == 1

        pause.set()
        await cache.drain()

    async def test_pending_task_removed_on_completion(self):
        """Completed task is discarded from _pending_tasks via done callback."""
        cache = DatusServiceCache()
        svc = _mock_service("p")
        await cache.get_or_create("p", AsyncMock(return_value=svc))
        await cache._dispose("p", svc)  # has_active=False → direct await, no task

        assert len(cache._pending_tasks) == 0

    async def test_drain_awaits_pending_tasks(self):
        """drain() awaits all registered background tasks."""
        cache = DatusServiceCache()
        completed = []

        async def _slow_work():
            await asyncio.sleep(0.01)
            completed.append(1)

        task = asyncio.create_task(_slow_work())
        cache._track(task)
        assert len(cache._pending_tasks) == 1

        await cache.drain()
        assert completed == [1]
        assert len(cache._pending_tasks) == 0

    async def test_drain_tolerates_task_exception(self):
        """drain() swallows task exceptions and clears the pending set."""
        cache = DatusServiceCache()

        async def _boom():
            raise RuntimeError("background failure")

        cache._track(asyncio.create_task(_boom()))
        await cache.drain()
        assert len(cache._pending_tasks) == 0

    async def test_shutdown_drains_before_clearing_cache(self):
        """shutdown() calls drain() before shutting down cached services."""
        cache = DatusServiceCache()
        order = []

        async def _background():
            await asyncio.sleep(0.01)
            order.append("drained")

        svc = _mock_service("p")
        svc.shutdown = AsyncMock(side_effect=lambda: order.append("service-shutdown"))
        await cache.get_or_create("p", AsyncMock(return_value=svc))
        cache._track(asyncio.create_task(_background()))

        await cache.shutdown()
        assert order == ["drained", "service-shutdown"]


@pytest.mark.asyncio
class TestShutdown:
    """Tests for shutdown — clean teardown of all services."""

    async def test_shutdown_clears_all_services(self):
        """Shutdown calls shutdown() on each cached service and clears cache."""
        cache = DatusServiceCache()
        svc_a = _mock_service("a")
        svc_b = _mock_service("b")
        await cache.get_or_create("a", AsyncMock(return_value=svc_a))
        await cache.get_or_create("b", AsyncMock(return_value=svc_b))

        await cache.shutdown()
        assert len(cache._cache) == 0
        svc_a.shutdown.assert_awaited_once()
        svc_b.shutdown.assert_awaited_once()

    async def test_shutdown_handles_exception_in_service(self):
        """Shutdown continues even if one service.shutdown() raises."""
        cache = DatusServiceCache()
        svc_ok = _mock_service("ok")
        svc_bad = _mock_service("bad")
        svc_bad.shutdown = AsyncMock(side_effect=RuntimeError("boom"))

        await cache.get_or_create("bad", AsyncMock(return_value=svc_bad))
        await cache.get_or_create("ok", AsyncMock(return_value=svc_ok))

        await cache.shutdown()  # should not raise
        assert len(cache._cache) == 0
        svc_ok.shutdown.assert_awaited_once()


@pytest.mark.asyncio
class TestPluginChangeRebuild:
    """Cross-component: real config + real service + real cache.

    Replays what ``get_datus_service`` does per request — recompute the
    fingerprint from the live AgentConfig, hand it to the cache — to pin that a
    plugin change actually swaps the cached instance instead of only moving a
    hash in isolation.
    """

    @staticmethod
    async def _request(cache, agent_config):
        from datus.api.services.datus_service import DatusService

        async def factory():
            return DatusService(agent_config=agent_config, project_id="p")

        return await cache.get_or_create(
            "p", factory, expected_fingerprint=DatusService.compute_fingerprint(agent_config)
        )

    async def test_plugin_profile_change_rebuilds_on_next_request(self, real_agent_config):
        cache = DatusServiceCache()
        try:
            real_agent_config.init_plugin_services({"hello": {"prod": {"api_base_url": "https://one"}}})
            first = await self._request(cache, real_agent_config)
            # Unchanged plugin state must reuse the cached instance.
            assert await self._request(cache, real_agent_config) is first

            real_agent_config.init_plugin_services({"hello": {"prod": {"api_base_url": "https://two"}}})

            assert await self._request(cache, real_agent_config) is not first
        finally:
            await cache.shutdown()

    async def test_plugin_disable_rebuilds_on_next_request(self, real_agent_config):
        cache = DatusServiceCache()
        try:
            first = await self._request(cache, real_agent_config)

            real_agent_config.plugins_enabled = False

            assert await self._request(cache, real_agent_config) is not first
        finally:
            await cache.shutdown()
