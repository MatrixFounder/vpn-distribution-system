"""Статический контракт границы nginx: заголовки прокси (задача 001.10, ревью раунда 2) и
разделение серверов Node API (задача 001.24, ревью раунда 1).

uvicorn с ``--forwarded-allow-ips '*'`` берёт крайний левый элемент ``X-Forwarded-For``; значит
nginx обязан перезаписывать заголовок адресом соединения (``$remote_addr``), а не дополнять его
(``$proxy_add_x_forwarded_for``) — иначе клиент подставляет себе любой адрес. Enrollment
(``POST /agent/v1/enroll``) — единственный маршрут ``/agent/v1`` без клиентского сертификата
(security.md §7.1), поэтому он обслуживается отдельным ``server`` и обязан быть недоступен на
агентском (mTLS) и публичном. Файлы сверяются статически (сеть в тесте не поднимается);
поведение на стенде — в отчётах задач.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NGINX_CONF = REPO / "deploy" / "nginx" / "nginx.conf"
ENTRYPOINT = REPO / "control-plane" / "docker-entrypoint.sh"


def _blocks(text: str, opener: str) -> list[str]:
    """Тела блоков ``<opener> … { … }`` со всем вложенным содержимым (сопоставление скобок)."""
    blocks: list[str] = []
    for match in re.finditer(rf"\b{opener}\b[^{{;]*\{{", text):
        depth, i = 0, match.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[match.end() : i])
                    break
            i += 1
    return blocks


def _server_blocks(text: str) -> list[str]:
    return _blocks(text, "server")


def _location_blocks(server: str) -> list[str]:
    return _blocks(server, "location")


def _locations(server: str) -> list[tuple[str, str]]:
    """Пары «матчер location — тело блока» в порядке объявления."""
    matchers = [m.group(1).strip() for m in re.finditer(r"\blocation\b([^{;]*)\{", server)]
    return list(zip(matchers, _location_blocks(server), strict=True))


def _server_by_listen(text: str, port: str) -> str:
    found = [b for b in _server_blocks(text) if re.search(rf"listen\s+{port}\b", b)]
    assert len(found) == 1, f"ровно один server слушает {port}"
    return found[0]


ENROLL_PATH = "/agent/v1/enroll"


def test_nginx_overwrites_forwarded_headers() -> None:
    """Во всём файле каждое вхождение X-Forwarded-For / X-Real-IP — только $remote_addr, каждое
    X-Forwarded-Proto — $scheme или https, дополнения $proxy_add_x_forwarded_for нет; в каждом
    из трёх server, проксирующих на api, полный набор задан на уровне server, а внутри location
    нет ни одного proxy_set_header — одна такая директива в location отменяет весь набор уровня
    server (наследование nginx), включая обнуление X-Client-Fingerprint."""
    text = re.sub(r"#[^\n]*", "", NGINX_CONF.read_text(encoding="utf-8"))  # без комментариев
    assert "$proxy_add_x_forwarded_for" not in text, "дополнение цепочки XFF — подделка адреса"
    forwarded_for = re.findall(r"proxy_set_header\s+X-Forwarded-For\s+([^;]+);", text)
    assert forwarded_for == ["$remote_addr"] * 3, forwarded_for
    real_ip = re.findall(r"proxy_set_header\s+X-Real-IP\s+([^;]+);", text)
    assert real_ip == ["$remote_addr"] * 3, real_ip
    proto = re.findall(r"proxy_set_header\s+X-Forwarded-Proto\s+([^;]+);", text)
    assert len(proto) == 3 and set(proto) <= {"$scheme", "https"}, proto
    # Любой server с proxy_pass (не только по переменной $api): новый server без набора
    # заголовков пропустил бы клиентский X-Forwarded-For как есть (ревью 001.10, раунд 4).
    proxying = [block for block in _server_blocks(text) if "proxy_pass" in block]
    assert len(proxying) == 3, "публичный, агентский и enrollment server"
    for block in proxying:
        for location in _location_blocks(block):
            assert "proxy_set_header" not in location, (
                "proxy_set_header внутри location отменяет набор заголовков server"
            )


def test_uvicorn_trusts_only_overwritten_headers() -> None:
    """Обе ветки запуска api передают --proxy-headers --forwarded-allow-ips '*' (заголовки
    принимаются только потому, что nginx их перезаписывает — см. тест выше)."""
    # Команда со строками-продолжениями (обратный слэш) склеивается в один оператор.
    text = ENTRYPOINT.read_text(encoding="utf-8").replace("\\\n", " ")
    launches = [line for line in text.splitlines() if "uvicorn app.main:create_app" in line]
    assert len(launches) == 2, "ветки --reload и --workers"
    for line in launches:
        assert "--proxy-headers" in line and "--forwarded-allow-ips '*'" in line, line


def test_enrollment_is_served_only_by_its_own_server() -> None:
    """Граница §7.1: enrollment проксируется единственным server (8444, без клиентского
    сертификата) и ровно одним точным location; агентский server (8443, ``ssl_verify_client
    on``) отдаёт на этот путь 404, публичный не знает ``/agent`` вовсе. Проверяется статически —
    ручной curl в отчёте задачи 001.24 не удерживает конфигурацию от правки."""
    text = re.sub(r"#[^\n]*", "", NGINX_CONF.read_text(encoding="utf-8"))
    public, agent, enrollment = (
        _server_by_listen(text, port) for port in ("443 ssl", "8443", "8444")
    )

    assert "ssl_verify_client      on" in agent or "ssl_verify_client on" in agent
    assert not re.search(r"ssl_verify_client\s+on", enrollment), (
        "на enrollment-server клиентского сертификата ещё нет (§7.1)"
    )
    assert not re.search(r"ssl_verify_client\s+on", public)

    enroll_locations = [(m, b) for m, b in _locations(enrollment) if "proxy_pass" in b]
    assert [m for m, _ in enroll_locations] == [f"= {ENROLL_PATH}"], (
        f"enrollment-server проксирует только {ENROLL_PATH}: {enroll_locations}"
    )
    for matcher, body in _locations(enrollment):
        if matcher != f"= {ENROLL_PATH}":
            assert "return 404" in body, (matcher, body)

    agent_enroll = [b for m, b in _locations(agent) if m == f"= {ENROLL_PATH}"]
    assert len(agent_enroll) == 1 and "return 404" in agent_enroll[0], (
        "агентский server обязан отдавать 404 на enrollment, а не проксировать его"
    )
    assert "proxy_pass" not in agent_enroll[0]

    for matcher, body in _locations(public):
        assert "/agent" not in matcher or "return 404" in body, (matcher, body)
