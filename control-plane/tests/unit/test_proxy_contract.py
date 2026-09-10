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


def test_the_fingerprint_header_is_set_by_the_proxy_and_never_by_the_client() -> None:
    """Единственное, что делает ``X-Client-Fingerprint`` пригодным как признак identity, — три
    директивы в этом файле: агентский ``server`` перезаписывает его сертификатом клиента,
    публичный и enrollment обнуляют. Снять или подменить любую — и заголовок становится
    клиентским, то есть любая нода с валидным сертификатом объявляет себя любой другой
    (§7.1, `app/agent_api/deps.py::current_node`). Правило держалось на обещании в докстринге
    соседнего теста, а не на ассерте."""
    text = re.sub(r"#[^\n]*", "", NGINX_CONF.read_text(encoding="utf-8"))
    assert "$http_x_client_fingerprint" not in text, (
        "значение заголовка от клиента не попадает в апстрим ни на одном server"
    )
    expected = {"8443": "$ssl_client_fingerprint", "443 ssl": '""', "8444": '""'}
    for port, value in expected.items():
        block = _server_by_listen(text, port)
        found = re.findall(r"proxy_set_header\s+X-Client-Fingerprint\s+(\S+);", block)
        assert found == [value], (port, found)
    # Отпечаток осмыслен ровно настолько, насколько доверен якорь, по которому прокси проверяет
    # сертификат: подменённый ``ssl_client_certificate`` оставил бы всю схему на месте, только
    # доверять она стала бы другому CA.
    agent = _server_by_listen(text, "8443")
    assert re.findall(r"ssl_client_certificate\s+(\S+);", agent) == ["/etc/nginx/certs/ca.crt"]
    assert re.findall(r"ssl_verify_client\s+(\S+);", agent) == ["on"], "не optional"
    assert re.findall(r"ssl_verify_depth\s+(\d+);", agent) == ["2"]


def test_agent_server_holds_long_poll_longer_than_the_application_does() -> None:
    """Удержание состояния до 30 с (interfaces.md §5.2, реализация — 001.75) переживёт прокси
    только с запасом по ``proxy_read_timeout``: без него nginx рвёт соединение раньше и агент
    получает 504 вместо 204."""
    text = re.sub(r"#[^\n]*", "", NGINX_CONF.read_text(encoding="utf-8"))
    agent = _server_by_listen(text, "8443")
    # ``findall`` и равенство, а не первое совпадение: ``proxy_read_timeout`` внутри ``location``
    # перекрывает уровень ``server``, а long-poll обслуживает именно ``location``.
    timeouts = re.findall(r"proxy_read_timeout\s+(\d+)s;", agent)
    assert len(timeouts) == 1, ("предел объявлен один раз, на уровне server", timeouts)
    assert int(timeouts[0]) >= 60, timeouts


def test_agent_server_does_not_accept_report_sized_bodies_everywhere() -> None:
    """Тело запроса nginx принимает до того, как приложение проверит версию агента и identity
    (FastAPI читает его перед разрешением зависимостей). Поэтому предел агентского `server` —
    по самой большой операции раздела, а не по будущим отчётам о трафике: их предел вводит
    001.33 отдельным `location`."""
    text = re.sub(r"#[^\n]*", "", NGINX_CONF.read_text(encoding="utf-8"))
    agent = _server_by_listen(text, "8443")
    limits = re.findall(r"client_max_body_size\s+(\S+);", agent)
    assert limits == ["64k"], "предел агентского server — по операциям 001.28"


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
