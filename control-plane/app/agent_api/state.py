"""``GET /agent/v1/state`` и ``POST /agent/v1/ack`` — потоки конфигурации и состава
(interfaces.md §5.2; UC-01 шаг 8, UC-07, UC-11).

Задача 001.28: маршруты, схемы и фиксированные ответы ``CompositionService``. Настоящая дельта
по ``updated_seq`` — 001.29; удержание запроса до 30 с на канале Redis ``node:{id}`` (C-01 ждёт,
C-02 публикует после записи), полный снапшот, поколение и запись подтверждённых курсоров —
001.75.

Удержанный long-poll не входит в выборку норматива Н-4 (p95 ≤ 200 мс для Node API): штатные 30 с
ожидания — не задержка обработки. Разделение рядов метрики — 001.68.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status

from app.agent_api.body import BoundedBodyRoute
from app.agent_api.deps import Served, Streams
from app.domain.composition import AckIn, StateResponse
from app.domain.statuses import INT4_MAX, INT8_MAX

router = APIRouter(route_class=BoundedBodyRoute, strict_content_type=True)

AGENT_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {
        "description": "тело не разобрано до проверки формы (негодный UTF-8, число длиннее "
        "4 300 цифр) — «тело запроса не разобрано», не повторять как есть; тот же ответ у "
        "оборванной заливки (его клиент уже не получает)"
    },
    401: {"description": "identity не предъявлена, неизвестна или отозвана"},
    403: {"description": "нода не подтверждена (`pending`, §4.6)"},
    411: {
        "description": "тело без известной длины (chunked, поток HTTP/2 без content-length) "
        "прокси не принимает — `length_required` единого формата: не повторять, тело "
        "отправляется с `Content-Length`"
    },
    # Пределы прокси действуют на всех операциях раздела уже сейчас (001.33, `deploy/nginx/
    # nginx.conf`): тело больше 64 КиБ — 413 страницей nginx без тела единого формата; тело без
    # известной длины — 411 единого формата; больше восьми соединений от ноды одновременно,
    # чаще пяти запросов в секунду от ноды или пятнадцати от всего парка — очередь, сверх
    # очереди — 429 в едином формате с `Retry-After`; апстрим недоступен — 503 единого формата
    # с `Retry-After`. Ограничение частоты по identity ноды в приложении вводит 001.72.
    # Коды объявлены здесь, потому что контракт читает вторая сторона обмена (001.53), и код,
    # о котором она узнает только в бою, станет для неё неизвестным фатальным ответом.
    413: {
        "description": "тело больше предела прокси (64 КиБ); ответ — страница nginx без тела "
        "единого формата"
    },
    426: {"description": "версия агента не поддерживается (`X-Agent-Version`)"},
    429: {
        "description": "предел прокси: не больше восьми соединений от ноды одновременно, не "
        "чаще пяти запросов в секунду от ноды и пятнадцати от всего парка — сверх частоты "
        "запрос ждёт в очереди прокси, 429 — сверх очереди (`Retry-After`); лимит по identity "
        "— 001.72"
    },
    503: {
        "description": "апстрим недоступен (перезапуск api, истёк таймаут прокси) — "
        "`upstream_unavailable` единого формата от прокси с `Retry-After`: повторить тот же "
        "запрос"
    },
}
# У операций без тела (GET состояния) 400 (тело не разобрано) и 411 (тело без длины: правило
# прокси не касается GET) недостижимы и не объявляются — контракт не обещает кодов, которых не
# бывает; 413 у GET достижим (прокси сверяет Content-Length независимо от метода) и остаётся.
BODY_CODES = frozenset({400, 411})
AGENT_ERRORS_WITHOUT_BODY: dict[int | str, dict[str, Any]] = {
    code: text for code, text in AGENT_ERRORS.items() if code not in BODY_CODES
}
# Ответ несёт credentials пользователей (§5.2): его не кэширует ни прокси, ни клиент.
NO_STORE = {"Cache-Control": "no-store"}


@router.get(
    "/state",
    response_model=None,
    summary="Состояние ноды: дельты потоков, поколение identity, команды (§5.2)",
    responses={
        200: {"model": StateResponse},
        204: {"description": "изменений нет — удержание истекло"},
        409: {"description": "поколение identity не совпадает — нужен снапшот"},
        **AGENT_ERRORS_WITHOUT_BODY,
    },
)
async def state(
    agent: Served,
    streams: Streams,
    config_version: Annotated[int, Query(ge=0, le=INT4_MAX, description="применённая версия")],
    users_seq: Annotated[int, Query(ge=0, le=INT8_MAX, description="применённый курсор состава")],
    generation: Annotated[int, Query(ge=0, le=INT4_MAX, description="поколение identity ноды")],
    full: Annotated[bool, Query(description="полный снапшот вместо дельт")] = False,
) -> Response:
    """Курсоры обязательны и в форме снапшота: нода знает их всегда, а Control Plane по ним
    видит, с чего началась пересинхронизация (§5.2 показывает `?full=1` сокращённо).

    Ответ сериализуется моделью напрямую, без ``response_model``: он уже собран и проверен
    доменом, а повторная проверка на выходе прошла бы всё дерево строк состава ещё трижды —
    в цикле событий процесса api (их два), где 001.75 держит припаркованные запросы. ``by_alias``
    обязателен: поле контракта называется ``json`` (псевдоним ``document``), и контрактные
    тесты по фикстурам — страж этой строки."""
    answer = await streams.state_for(agent.node, config_version, users_seq, generation, full)
    if answer is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=NO_STORE)
    return Response(
        answer.model_dump_json(by_alias=True), media_type="application/json", headers=NO_STORE
    )


@router.post(
    "/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Подтверждение применённых курсоров обоих потоков (§5.2)",
    responses=AGENT_ERRORS,
)
async def ack(body: AckIn, agent: Served, streams: Streams) -> None:
    await streams.ack(agent.node, body.applied_config_version, body.applied_users_seq)
