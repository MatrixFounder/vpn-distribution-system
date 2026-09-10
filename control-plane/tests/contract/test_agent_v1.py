"""Стадия «Контракты» (`docs/architectures/deployment.md` §10.2) со стороны Control Plane:
зафиксированные запросы `contracts/agent_v1/*.json` воспроизводятся против приложения, ответ
сверяется со снятым. Ту же папку читает Node Agent (`node-agent/internal/contracts`, 001.53) —
расхождение сторон обмена видно раньше стенда.

Тесты фикстуры только читают. Ответ, не совпавший со снятым, — либо изменение контракта (тогда
файл правится осознанно и попадает в тот же коммит), либо дефект.

Прогон герметичен: пул подменён объектом, который на любое обращение падает. Заглушки задачи
001.28 в базу не ходят, и это проверяется здесь, а не декларируется, — поэтому `make
test-contract` работает и на машине без стенда, и в CI без служебных контейнеров.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from app.agent_api.deps import Agent, CurrentNode, served_node
from app.db.pool import db_pool
from app.domain.nodes import NodeStatus, stub_node
from app.main import create_app

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "agent_v1"

# Набор фикстур закреплён: пропавший файл обязан валить прогон, а не молча сокращать его.
# Параметризация читает каталог с диска — список ниже сверяется с прочитанным.
EXPECTED = {
    "ack",
    "command-result",
    "enroll",
    "heartbeat",
    "metrics",
    "state-delta",
    "state-filtered",
    "state-cursor-ahead",
    "state-generation-mismatch",
    "state-no-changes",
    "state-not-approved",
    "state-provisioning",
    "state-snapshot",
    "state-unauthenticated",
    "state-unsupported-agent",
    "state-users-only",
}


def fixture_files() -> list[Path]:
    """``rglob``: файл в подкаталоге — тоже часть каталога, и он не должен быть невидим."""
    return sorted(CONTRACTS.rglob("*.json"))


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


class NoDatabase:
    """Пул, которого нет: любое обращение — провал теста, а не молчаливое подключение."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"заглушка /agent/v1 обратилась к базе: {name}")


def agent_app(node_status: str = "active") -> httpx.AsyncClient:
    """Приложение без базы. ``node_status`` — предусловие фикстуры: статус ноды живёт на
    сервере, а не в запросе, поэтому фикстура объявляет его отдельно — иначе запрос не
    определял бы ответ и воспроизведение было бы невозможно."""
    app = create_app()
    app.dependency_overrides[db_pool] = NoDatabase

    async def as_status(node: Agent) -> CurrentNode:
        # Надстройка над настоящей зависимостью, а не подмена её: подменённая целиком, она не
        # разбирала бы записанные в фикстуре заголовки, и негодный отпечаток в фикстуре учил бы
        # вторую сторону обмена форме запроса, которую живой сервер отвергает.
        return replace(node, node=stub_node(status=node_status))  # type: ignore[arg-type]

    app.dependency_overrides[served_node] = as_status
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://control-plane")


def test_every_fixture_declares_its_precondition() -> None:
    """Предусловие обязательно и берётся из перечисления `node_status` §4.2.3: фикстура без
    него не воспроизводима, а с выдуманным статусом — не сверяема."""
    statuses = set(get_args(NodeStatus))
    for path in fixture_files():
        given = load(path).get("given")
        assert given is not None and given["node_status"] in statuses, path.stem
    assert {load(p)["given"]["node_status"] for p in fixture_files()} == {
        "active",
        "pending",
        "provisioning",
        "disabled",
    }, "вентиль §4.6 показан второй стороне обмена всеми четырьмя видами ответа"


def test_the_fixture_set_is_the_declared_one() -> None:
    """Каталог фикстур — часть контракта: и лишний файл, и пропавший меняют то, что сверяет
    вторая сторона обмена."""
    assert CONTRACTS.is_dir(), f"каталог контрактов не найден: {CONTRACTS}"
    assert {path.stem for path in fixture_files()} == EXPECTED
    assert len(fixture_files()) == len(EXPECTED)


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: p.stem)
async def test_recorded_request_gets_the_recorded_response(path: Path) -> None:
    """Запрос из фикстуры → ровно тот ответ, который в ней записан: код и разобранное тело."""
    fixture = load(path)
    request, expected = fixture["request"], fixture["response"]
    async with agent_app(fixture["given"]["node_status"]) as client:
        response = await client.request(
            request["method"],
            request["path"],
            params=request["query"] or None,
            json=request["body"],
            headers=request["headers"],
        )
    assert response.status_code == expected["status"], response.text
    for name, value in expected["headers"].items():
        assert response.headers.get(name) == value, (path.stem, name)
    if expected["body"] is None:
        assert response.content == b"", "тело не предусмотрено контрактом"
    else:
        assert response.json() == expected["body"]


def test_config_checksum_is_sha256_of_the_canonical_form() -> None:
    """`config.checksum` считается по правилу из `contracts/agent_v1/README.md`, и здесь оно
    записано отдельно от реализации: иначе сумма сверялась бы сама с собой, а вторая сторона
    обмена (Go) считает её по описанию, а не по коду Control Plane."""
    checked = 0
    for path in fixture_files():
        config = (load(path)["response"]["body"] or {}).get("config")
        if config is None:
            continue
        canonical = json.dumps(
            config["json"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        assert config["checksum"] == hashlib.sha256(canonical.encode()).hexdigest(), path.stem
        checked += 1
    assert checked >= 2, "поток конфигурации закреплён и дельтой, и снапшотом"


def test_only_a_complete_composition_is_marked_full() -> None:
    """`users.full` — разрешение ноде удалить отсутствующих (§4.2.3). Ответ, урезанный вентилем
    по статусу, таким не помечается: иначе отключённая нода, применив «снапшот» из одних отзывов,
    вычистила бы у себя всех действующих пользователей. Проверяется по каталогу, а не по одному
    случаю: фикстура с `resync_required` и `full` одновременно — противоречие."""
    marked = []
    for path in fixture_files():
        body = load(path)["response"]["body"] or {}
        users = body.get("users")
        if users is None:
            continue
        if users["full"]:
            marked.append(path.stem)
            assert not body["resync_required"], path.stem
            assert all(row["state"] != "removed" for row in users["rows"]), path.stem
        if body["resync_required"]:
            assert not users["full"], path.stem
    assert marked == ["state-snapshot"], "полным объявлен ровно снапшот неотфильтрованной ноды"


def test_the_status_gate_is_visible_in_the_catalogue() -> None:
    """Половина §5.2, где живёт вентиль по статусу, показана второй стороне обмена: отказ
    неподтверждённой ноде, первичная настройка без состава и отфильтрованный ответ с
    продвинутым курсором. Без этих случаев агент на Go писался бы вслепую."""
    refused = load(CONTRACTS / "state-not-approved.json")["response"]
    assert refused["status"] == 403 and refused["body"]["error"]["code"] == "node_not_approved"
    provisioning = load(CONTRACTS / "state-provisioning.json")["response"]["body"]
    assert provisioning["config"] is not None and provisioning["users"]["rows"] == []
    assert provisioning["users"]["seq"] == 0, "курсор состава в provisioning не двигается"
    filtered = load(CONTRACTS / "state-filtered.json")["response"]["body"]
    assert filtered["resync_required"] is True and filtered["commands"] == []
    assert {row["state"] for row in filtered["users"]["rows"]} == {"suspended_quota", "removed"}
    assert filtered["users"]["seq"] > max(
        row["updated_seq"] for row in filtered["users"]["rows"]
    ), "курсор ушёл дальше выданных строк: скрытое нода добирает снапшотом, а не дельтой"


def test_every_answer_records_the_headers_its_status_requires() -> None:
    """Сверять «записанные заголовки совпадают с ответом» мало: удалённый из фикстуры заголовок
    так не ловится — это утверждение об отсутствии, а не страж. Требуемый набор выводится из
    кода ответа: у состояния (200 и 204) обязателен `Cache-Control: no-store` — в теле
    credentials; у отказов 403, 409 и 426 обязателен `Retry-After` — иначе частоту повторов
    определяет только агент, а сервер повлиять на неё не может."""
    checked = {200: 0, 204: 0, 403: 0, 409: 0, 426: 0}
    for path in fixture_files():
        fixture = load(path)
        response = fixture["response"]
        status, headers = response["status"], response["headers"]
        # Признак операции берётся из пути запроса, а не из поля ``operation``: оно лежит в том
        # же файле, что и проверяемые заголовки, и подмена его вместе с удалением заголовка
        # прошла бы молча — снова утверждение об отсутствии вместо стража.
        is_state = fixture["request"]["path"] == "/agent/v1/state"
        assert fixture["operation"] == ("state" if is_state else fixture["operation"])
        if status == 200:
            assert headers.get("Content-Type", "").startswith("application/json"), path.stem
        if is_state and status in (200, 204):
            assert headers.get("Cache-Control") == "no-store", path.stem
        if status in (403, 409, 426):
            assert headers.get("Retry-After", "").isdigit(), path.stem
        checked[status] = checked.get(status, 0) + 1
    assert all(checked[status] for status in (200, 204, 403, 409, 426)), checked


def test_the_readme_table_lists_exactly_the_fixtures_on_disk() -> None:
    """Таблица в README — то, по чему вторая сторона обмена понимает, какие случаи закреплены.
    Строка, выпавшая из неё, оставляет фикстуру невидимой для читателя, а лишняя обещает случай,
    которого нет."""
    readme = (CONTRACTS / "README.md").read_text()
    listed = set(
        re.findall(r"`(state-[a-z-]+|enroll|ack|heartbeat|metrics|command-result)\.json`", readme)
    )
    assert listed == {path.stem for path in fixture_files()}


def test_only_rows_that_grant_access_carry_credentials() -> None:
    """Ключи везёт только строка, выдающая доступ. Проверяется по всему каталогу: именно отсюда
    вторая сторона обмена узнаёт, что отзыв применяется по тегу, а не по ключу, — и что нода в
    отключённом статусе живых ключей не получает вовсе."""
    seen_active = seen_revoking = 0
    for path in fixture_files():
        for row in ((load(path)["response"]["body"] or {}).get("users") or {}).get("rows", ()):
            granting = row["state"] == "active"
            assert (row["credentials"] is not None) is granting, (path.stem, row["state"])
            seen_active += granting
            seen_revoking += not granting
    assert seen_active and seen_revoking, "в каталоге есть строки обоих видов"


def test_a_delta_without_config_changes_is_in_the_catalogue() -> None:
    """Форма «конфигурация не менялась, состав менялся» — самый частый ответ в бою, и второй
    стороне обмена показано, что `config` бывает `null`, а не только заполненным."""
    body = load(CONTRACTS / "state-users-only.json")["response"]["body"]
    assert body["config"] is None
    assert body["users"]["rows"], "состав при этом непуст"


def test_the_recorded_commands_have_not_expired() -> None:
    """Контракт предписывает агенту сверять `expires_at` перед исполнением. Команда, протухшая
    от одного лишь течения времени, дала бы красный контрактный тест на верной реализации."""
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    seen = 0
    for path in fixture_files():
        for command in (load(path)["response"]["body"] or {}).get("commands", ()):
            assert dt.datetime.fromisoformat(command["expires_at"]) > now, path.stem
            assert dt.datetime.fromisoformat(command["issued_at"]) < now, path.stem
            seen += 1
    assert seen >= 1, "канал команд показан хотя бы одной фикстурой"


def test_the_config_stream_carries_no_user_credentials() -> None:
    """Инвариант §5.2: поток конфигурации идёт без credentials пользователей. Проверяется по
    самим фикстурам — значения из потока состава не должны встречаться в тексте конфигурации."""
    checked = 0
    for path in fixture_files():
        body = load(path)["response"]["body"] or {}
        config, users = body.get("config"), body.get("users")
        if config is None or users is None:
            continue
        text = json.dumps(config, ensure_ascii=False)
        for row in users["rows"]:
            credentials = row["credentials"]
            if credentials is None:
                continue
            for secret in (credentials["vless_uuid"], credentials["trojan_password"]):
                assert secret not in text, (path.stem, row["user_id"])
            checked += 1
    assert checked >= 2, "в фикстурах есть строки состава с credentials рядом с конфигурацией"
