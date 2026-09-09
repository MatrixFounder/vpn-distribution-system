"""TC-UNIT-01 задачи 001.74 (tdd-strict): экспоненциальная задержка повторов ``backoff`` и
решение исполнителя «повторить / похоронить / отказ без повторов» — без базы."""

from __future__ import annotations

import pytest
from app.jobs import queue as jobs
from app.jobs import worker


# EXPECTED_FAIL_REASON: AttributeError: worker.backoff — задача 001.74
def test_backoff_doubles_with_bounded_jitter() -> None:
    """Попытки 1, 2, 3 → 1 с, 2 с, 4 с плюс джиттер до четверти интервала (детерминированный
    генератор): повторы расходятся во времени, а не бьют в базу синхронно."""
    assert [worker.backoff(n, rng=lambda: 0.0) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]
    assert [worker.backoff(n, rng=lambda: 0.999) for n in (1, 2, 3)] == pytest.approx(
        [1.24975, 2.4995, 4.999]
    )
    for attempt in (1, 2, 3, 7):
        low = worker.BACKOFF_BASE * 2 ** (attempt - 1)
        for _ in range(20):
            delay = worker.backoff(attempt)
            assert low <= delay < low * (1 + worker.BACKOFF_JITTER), (attempt, delay)


# EXPECTED_FAIL_REASON: AttributeError: worker.backoff — задача 001.74
def test_backoff_is_capped_and_scalable() -> None:
    """Задержка не растёт бесконечно (потолок) и масштабируется базой — тесты со стендом ждут
    сотые доли секунды, а не минуты."""
    assert worker.backoff(30, rng=lambda: 0.0) == worker.BACKOFF_CAP
    assert worker.backoff(3, base=0.1, rng=lambda: 0.0) == pytest.approx(0.4)
    with pytest.raises(ValueError):
        worker.backoff(0)


# EXPECTED_FAIL_REASON: AttributeError: worker.decide — задача 001.74
def test_decision_retry_dead_or_fail() -> None:
    """Исключение обработчика: пока есть попытки — повтор с задержкой по номеру попытки;
    исчерпаны — ``dead``; ``NonRetryableError`` — ``failed`` сразу, без повторов."""
    assert worker.decide(RuntimeError("x"), attempts=1, max_attempts=3) == ("retry", 1)
    assert worker.decide(RuntimeError("x"), attempts=2, max_attempts=3) == ("retry", 2)
    assert worker.decide(RuntimeError("x"), attempts=3, max_attempts=3) == ("dead", 3)
    assert worker.decide(RuntimeError("x"), attempts=9, max_attempts=3) == ("dead", 9)
    assert worker.decide(jobs.NonRetryableError("нет смысла"), attempts=1, max_attempts=3) == (
        "failed",
        1,
    )
