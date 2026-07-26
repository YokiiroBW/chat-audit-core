from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from collector.app.config import CollectorConfig
from collector.sync.scanner import QQNTScanner, ScanResult
from collector.sync.state import CollectorStateStore
from collector.sync.uploader import CollectorApiError, QueueUploader


logger = logging.getLogger("collector.scheduler")


@dataclass(frozen=True)
class SchedulerResult:
    run_id: str
    mode: str
    status: str
    stats: dict
    error_code: str | None = None


class CollectorScheduler:
    def __init__(
        self,
        config: CollectorConfig,
        store: CollectorStateStore,
        uploader: QueueUploader,
        scanner: QQNTScanner | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.uploader = uploader
        self.scanner = scanner
        self._last_reconcile_at = time.monotonic()

    async def run_once(self, mode: str = "incremental") -> SchedulerResult:
        if mode not in {"initial", "incremental", "reconcile", "media_rescan"}:
            raise ValueError(f"unsupported collector run mode: {mode}")
        run_id = self.store.try_start_sync_run(mode)
        if run_id is None:
            return SchedulerResult("", mode, "skipped", {"skipped": "active_run"})
        started = time.monotonic()
        try:
            scan_result: ScanResult | None = None
            if self.scanner is not None and mode != "media_rescan":
                scan_result = self.scanner.scan(
                    mode=mode,
                    max_messages=(
                        self.config.collector.initial_import_batch_size
                        if mode == "initial"
                        else None
                    ),
                )
                if mode == "initial" and not scan_result.issues and not scan_result.has_more:
                    self.store.set_meta("initial_scan_completed", "1")
            stats = await self.uploader.run_cycle(mode=mode)
            if scan_result is not None:
                stats["scan"] = scan_result.to_dict()
            failed = stats["messages"].get("failed", 0)
            dead_letters = stats["messages"].get("dead_letter", 0) + stats["media"].get("dead_letter", 0)
            scan_partial = scan_result is not None and bool(scan_result.issues)
            status = "partial" if failed or dead_letters or scan_partial else "completed"
            self.store.finish_sync_run(run_id, status=status, stats=stats)
            logger.info(
                "collector run completed",
                extra={
                    "event": "sync.run",
                    "message_count": stats["messages"].get("completed", 0),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return SchedulerResult(run_id, mode, status, stats)
        except CollectorApiError as exc:
            self.store.finish_sync_run(run_id, status="failed", error_code=exc.error_code, detail=str(exc))
            logger.warning(
                "collector run failed",
                extra={"event": "sync.run", "error_code": exc.error_code},
            )
            return SchedulerResult(run_id, mode, "failed", {}, exc.error_code)
        except Exception:
            self.store.finish_sync_run(run_id, status="failed", error_code="COLLECTOR_INTERNAL_ERROR")
            logger.exception(
                "collector run failed unexpectedly",
                extra={"event": "sync.run", "error_code": "COLLECTOR_INTERNAL_ERROR"},
            )
            raise

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        sync_seconds = self.config.collector.sync_interval_seconds
        reconcile_seconds = self.config.collector.reconcile_interval_hours * 3600
        while not stop.is_set():
            now = time.monotonic()
            if self.scanner is not None and self.store.get_meta("initial_scan_completed") != "1":
                mode = "initial"
            else:
                mode = "reconcile" if now - self._last_reconcile_at >= reconcile_seconds else "incremental"
            try:
                result = await self.run_once(mode)
            except Exception:
                # run_once already recorded the failed run and logged the traceback.
                # A single bad cycle must not end the daemon: without this the next
                # scheduled sync never happens and the queue silently stops draining
                # until someone notices and restarts the process.
                logger.exception(
                    "collector cycle aborted; continuing with the next one",
                    extra={"event": "sync.loop", "error_code": "COLLECTOR_INTERNAL_ERROR"},
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=sync_seconds)
                except TimeoutError:
                    pass
                continue
            if result.status == "skipped":
                await asyncio.sleep(min(5, sync_seconds))
                continue
            if mode == "reconcile":
                self._last_reconcile_at = time.monotonic()
            delay_seconds = sync_seconds
            if mode == "initial" and self.store.get_meta("initial_scan_completed") != "1":
                delay_seconds = min(5, sync_seconds)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay_seconds)
            except TimeoutError:
                pass
