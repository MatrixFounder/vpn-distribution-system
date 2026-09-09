"""Сквозные проверки очереди задач (задача 001.11, §5.4) на живом стенде — рядом с рабочими
исполнителями и планировщиком (герметичность — см. ``_jobs``).

TC-E2E-01: enqueue → одна итерация исполнителя → done; повторный enqueue с активным ключом
отклонён. TC-E2E-02: очереди независимы — исполнитель critical не забирает background. Плюс:
порядок выборки run_at, id; неизвестный тип не выбирается, падение обработчика → failed с
текстом; run_at в будущем не выбирается; FOR UPDATE SKIP LOCKED — занятая другой сессией строка
пропускается; NOTIFY уходит только при коммите; главный цикл просыпается по NOTIFY быстрее
страховочного опроса и переживает обрыв LISTEN и ошибку базы; планировщик держит
advisory-блокировку и отпускает её при остановке; /metrics отдаёт глубину очереди и db_up 0
без базы.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import asyncpg
import httpx
import pytest
from app import metrics
from app.db import pool as pool_module
from app.jobs import queue as jobs
from app.jobs import scheduler, worker

from ._jobs import BOOM, NOOP, PREFIX, TEST_LOCK_KEY, VANISH, job_row, stand_pool

TYPES = (NOOP, BOOM, VANISH)


async def test_job_cycle_and_idempotency(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TC-E2E-01 + TC-UNIT-01: k1 → done за одну итерацию; дубль активного ключа отклонён без
    порчи транзакции; после done ключ свободен — новая задача создана."""
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        async with pool.acquire() as conn, conn.transaction():
            job_id = await jobs.enqueue(conn, "background", NOOP, {"n": 1}, f"{PREFIX}k1")
            with pytest.raises(jobs.DuplicateJobError):
                await jobs.enqueue(conn, "background", NOOP, {}, f"{PREFIX}k1")
            assert await conn.fetchval("select 1") == 1, "транзакция после дубля жива"
        assert (await job_row(pool, job_id))["status"] == "pending"

        assert await worker.run_once(pool, "background", "w1") == 1
        row = await job_row(pool, job_id)
        assert row == {
            "status": "done",
            "attempts": 1,
            "locked_at": None,
            "locked_by": None,
            "last_error": None,
        }
        assert await worker.run_once(pool, "background", "w1") == 0, "очередь пуста"

        async with pool.acquire() as conn:
            again = await jobs.enqueue(conn, "background", NOOP, {}, f"{PREFIX}k1")
            assert again != job_id, "ключ done-задачи свободен (частичная уникальность §4.4)"
            await jobs.claim(conn, "background", "w1", types=TYPES)
            with pytest.raises(jobs.DuplicateJobError):  # running тоже держит ключ
                await jobs.enqueue(conn, "background", NOOP, {}, f"{PREFIX}k1")
            await jobs.complete(conn, again)
            with pytest.raises(LookupError):  # завершать нечего — задача уже done
                await jobs.complete(conn, again)


async def test_queues_are_independent(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TC-E2E-02: исполнитель critical не забирает задачу background и наоборот."""
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        async with pool.acquire() as conn:
            background = await jobs.enqueue(conn, "background", NOOP, {}, f"{PREFIX}bg")
            critical = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}cr")
            claimed = await jobs.claim(conn, "critical", "wc", types=TYPES)
            assert claimed is not None and claimed.id == critical
            assert claimed.queue == "critical" and claimed.status == "running"
            assert await jobs.claim(conn, "critical", "wc", types=TYPES) is None, (
                "background не видна"
            )
            await jobs.complete(conn, critical)
        assert await worker.run_once(pool, "critical", "wc") == 0
        assert await worker.run_once(pool, "background", "wb") == 1
        assert (await job_row(pool, background))["status"] == "done"


async def test_failures_unknown_type_and_run_at(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Исключение обработчика → failed с текстом, блокировка снята; строка, исчезнувшая во
    время обработки, не роняет исполнителя; задача неизвестного типа не выбирается (остаётся
    pending исполнителю, который её знает — §17.2); run_at в будущем не выбирается; после failed
    ключ свободен."""
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        async with pool.acquire() as conn:
            unknown = await jobs.enqueue(conn, "background", "test-unknown", {}, f"{PREFIX}u")
            failing = await jobs.enqueue(conn, "background", BOOM, {"x": 7}, f"{PREFIX}f")
            vanishing = await jobs.enqueue(conn, "background", VANISH, {}, f"{PREFIX}v")
            later = await jobs.enqueue(
                conn,
                "background",
                NOOP,
                {},
                f"{PREFIX}later",
                run_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
            )
        assert await worker.run_once(pool, "background", "w") == 2, "boom и vanish обработаны"
        async with pool.acquire() as conn:
            assert await conn.fetchval("select count(*) from jobs where id = $1", vanishing) == 0
        assert await job_row(pool, failing) == {
            "status": "failed",
            "attempts": 1,
            "locked_at": None,
            "locked_by": None,
            "last_error": "RuntimeError: сломалось 7",
        }
        assert (await job_row(pool, unknown))["status"] == "pending", "неизвестный тип не выбран"
        assert (await job_row(pool, later))["status"] == "pending", "run_at в будущем"
        async with pool.acquire() as conn:  # пустой набор типов — ничего, None — любые
            assert await jobs.claim(conn, "background", "w", types=()) is None
            anything = await jobs.claim(conn, "background", "w", types=None)
            assert anything is not None and anything.id == unknown
            await jobs.fail(conn, unknown, "проба types=None")
        async with pool.acquire() as conn:
            assert await jobs.enqueue(conn, "background", BOOM, {}, f"{PREFIX}f") != failing, (
                "после failed ключ свободен (повторы — 001.74)"
            )


async def test_claim_order_is_run_at_then_id(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Выборка — самая ранняя по run_at, при равных run_at — по id (§5.4, контракт claim)."""
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        base = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
        minute = dt.timedelta(minutes=1)
        async with pool.acquire() as conn:
            late = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}o3", base + 3 * minute)
            early = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}o1", base + minute)
            middle = await jobs.enqueue(
                conn, "critical", NOOP, {}, f"{PREFIX}o2", base + 2 * minute
            )
            same_a = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}o4", base)
            same_b = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}o5", base)
            order = []
            for _ in range(5):
                job = await jobs.claim(conn, "critical", "wo", types=TYPES)
                assert job is not None
                order.append(job.id)
                await jobs.complete(conn, job.id)
        assert order == [same_a, same_b, early, middle, late]


async def test_claim_skips_rows_locked_by_another_session(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FOR UPDATE SKIP LOCKED: строка, заблокированная чужой транзакцией, пропускается без
    ожидания; после её освобождения — забирается."""
    assert "for update skip locked" in jobs.CLAIM_SQL, "критерий приёмки 001.11"
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        async with pool.acquire() as conn:
            job_id = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}locked")
        blocker = await asyncpg.connect(pg_dsn)
        try:
            tx = blocker.transaction()
            await tx.start()
            await blocker.execute("select id from jobs where id = $1 for update", job_id)
            async with pool.acquire() as conn:
                skipped = await asyncio.wait_for(
                    jobs.claim(conn, "critical", "w2", types=TYPES), timeout=5
                )
            assert skipped is None, "занятая строка пропущена, а не ожидалась"
            await tx.rollback()
            async with pool.acquire() as conn:
                claimed = await jobs.claim(conn, "critical", "w2", types=TYPES)
            assert claimed is not None and claimed.id == job_id
            assert claimed.attempts == 1
        finally:
            await blocker.close()


async def test_notify_only_on_commit(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NOTIFY jobs_<queue> с id задачи доходит до слушателя после коммита и не доходит при
    откате постановки."""
    received: asyncio.Queue[str] = asyncio.Queue()
    listener = await asyncpg.connect(pg_dsn)
    try:
        await listener.add_listener(
            jobs.channel("critical"), lambda *args: received.put_nowait(str(args[-1]))
        )
        async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
            async with pool.acquire() as conn:
                tx = conn.transaction()
                await tx.start()
                rolled = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}rb")
                await tx.rollback()
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(received.get(), timeout=1.0)
                async with conn.transaction():
                    committed = await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}cm")
                    with pytest.raises(TimeoutError):  # до коммита уведомления нет
                        await asyncio.wait_for(received.get(), timeout=0.5)
                assert await asyncio.wait_for(received.get(), timeout=5.0) == str(committed)
                assert rolled != committed
    finally:
        await listener.close()


async def wait_done(pool: asyncpg.Pool, job_id: int, limit: float, why: str) -> None:
    started = asyncio.get_running_loop().time()
    while (await job_row(pool, job_id))["status"] != "done":
        assert asyncio.get_running_loop().time() - started < limit, why
        await asyncio.sleep(0.1)


async def test_worker_loop_wakes_on_notify_and_survives_faults(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Главный цикл фоновой очереди (опрос раз в 10 с): задача выполняется за секунды — по
    NOTIFY; после обрыва подключения LISTEN (pg_terminate_backend) оно восстанавливается и
    следующая задача снова выполняется быстрее опроса; ошибка базы при выборке (посаженная
    подменой claim) не убивает цикл — после неё задача выполняется."""
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run("background", stop=stop, pool=pool))
        try:
            await asyncio.sleep(1.0)  # исполнитель подписался на канал
            async with pool.acquire() as conn:
                job_id = await jobs.enqueue(conn, "background", NOOP, {}, f"{PREFIX}loop")
            await wait_done(pool, job_id, 5.0, "не проснулся по NOTIFY")

            async with pool.acquire() as conn:  # обрыв LISTEN: свой backend по application_name
                killed = await conn.fetch(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where application_name = 'worker:background:listener' "
                    "and usename = current_user and pid <> pg_backend_pid()"
                )
            assert killed, "подключение LISTEN исполнителя не найдено"
            await asyncio.sleep(2.5)  # переподключение (пауза 1 с) и новая подписка
            async with pool.acquire() as conn:
                job_id = await jobs.enqueue(conn, "background", NOOP, {}, f"{PREFIX}loop2")
            await wait_done(pool, job_id, 5.0, "после обрыва LISTEN не восстановился")

            calls = 0
            original = jobs.claim

            async def flaky_claim(*args: object, **kwargs: object) -> jobs.Job | None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise asyncpg.InternalServerError("cache lookup failed for type 1 (посадка)")
                return await original(*args, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(jobs, "claim", flaky_claim)
            async with pool.acquire() as conn:
                job_id = await jobs.enqueue(conn, "background", NOOP, {}, f"{PREFIX}loop3")
            await wait_done(pool, job_id, 8.0, "после ошибки базы цикл не восстановился")
            assert calls >= 2 and not task.done(), "цикл жив после ошибки выборки"

            # Обрыв LISTEN при недоступной базе: переподключение падает, флаг пробуждения уже
            # взведён обрывом — попытки идут с паузой 1 с, а не вхолостую (ревью, раунд 2).
            attempts = 0

            async def refused(*args: object, **kwargs: object) -> asyncpg.Connection:
                nonlocal attempts
                attempts += 1
                raise OSError("посадка: база недоступна")

            monkeypatch.setattr(worker, "connect_listener", refused)
            async with pool.acquire() as conn:
                await conn.fetch(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where application_name = 'worker:background:listener' "
                    "and usename = current_user and pid <> pg_backend_pid()"
                )
            await asyncio.sleep(2.5)
            assert 1 <= attempts <= 4, f"переподключение крутится вхолостую: {attempts} за 2,5 с"
            assert not task.done()
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)
        row = await job_row(pool, job_id)  # переданный пул run() не закрывает
        assert row["locked_by"] is None and row["attempts"] == 1


async def test_scheduler_leadership_lock(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Планировщик держит pg_advisory_lock своего ключа, пока работает, и отпускает при
    остановке (ключ тестов — не ключ планировщика стенда, который лидер всегда); второй
    экземпляр с тем же ключом лидером не становится; пустое расписание ничего не ставит;
    периодический элемент ставится один раз на слот; нулевой интервал отклоняется."""
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        stop = asyncio.Event()
        task = asyncio.create_task(
            scheduler.run(stop=stop, schedule=[], pool=pool, lock_key=TEST_LOCK_KEY)
        )
        probe = await asyncpg.connect(pg_dsn)
        try:
            for _ in range(50):
                if not await probe.fetchval("select pg_try_advisory_lock($1)", TEST_LOCK_KEY):
                    break
                await probe.execute("select pg_advisory_unlock($1)", TEST_LOCK_KEY)
                await asyncio.sleep(0.1)
            else:
                pytest.fail("планировщик не взял блокировку лидера")
            assert await scheduler.acquire_leadership(probe, TEST_LOCK_KEY) is False, (
                "второй экземпляр не лидер, пока первый жив"
            )
            assert await scheduler.acquire_leadership(probe, scheduler.LEADER_LOCK_KEY) is False, (
                "боевой ключ держит планировщик стенда"
            )
            stop.set()
            await asyncio.wait_for(task, timeout=10)
            assert await probe.fetchval("select pg_try_advisory_lock($1)", TEST_LOCK_KEY), (
                "после остановки блокировка свободна"
            )
            await probe.execute("select pg_advisory_unlock($1)", TEST_LOCK_KEY)
        finally:
            await probe.close()
        with pytest.raises(ValueError):
            scheduler.Periodic("zero", dt.timedelta(0), "background", NOOP, {})
        with pytest.raises(ValueError):
            scheduler.Periodic("bad-queue", dt.timedelta(minutes=1), "urgent", NOOP, {})
        periodic = scheduler.Periodic(
            f"{PREFIX}tick", dt.timedelta(minutes=5), "background", NOOP, {}
        )
        now = dt.datetime.now(dt.UTC)
        assert await scheduler.tick(pool, now, [periodic]) == 1
        assert await scheduler.tick(pool, now, [periodic]) == 0, "тот же слот — no-op"
        assert await scheduler.tick(pool, now + dt.timedelta(minutes=5), [periodic]) == 1


async def test_metrics_report_queue_depth(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_client: httpx.AsyncClient
) -> None:
    """/metrics: control_plane_jobs{queue,status} по всем парам, база доступна."""
    async with stand_pool(pg_dsn, monkeypatch, tmp_path) as pool:
        async with pool.acquire() as conn:
            await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}m1")
            await jobs.enqueue(conn, "critical", NOOP, {}, f"{PREFIX}m2")
        response = await app_client.get("/metrics")
        assert response.status_code == 200
        text = response.text
        assert "control_plane_db_up 1\n" in text
        assert "# TYPE control_plane_jobs gauge\n" in text
        assert 'control_plane_jobs{queue="critical",status="pending"} 2\n' in text
        for queue in jobs.QUEUES:
            for status in jobs.STATUSES:
                assert f'control_plane_jobs{{queue="{queue}",status="{status}"}} ' in text


async def test_metrics_without_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """База недоступна → /metrics всё равно отвечает: control_plane_up 1, db_up 0, без рядов
    очереди и без исключения."""
    key = tmp_path / "key"
    key.write_text("k" * 44)
    monkeypatch.setenv("PG_DSN", "postgresql://app_rw@127.0.0.1:1/control_plane")  # закрытый порт
    monkeypatch.setenv("APP_ROLE", "api")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", str(key))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.delenv("PG_PASSWORD_FILE", raising=False)
    await pool_module.close_pool()
    try:
        text = await metrics.render()
    finally:
        await pool_module.close_pool()
    assert "control_plane_up 1\n" in text
    assert "control_plane_db_up 0\n" in text
    assert "control_plane_jobs" not in text
