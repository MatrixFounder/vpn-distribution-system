"""Статический контракт границы nginx: заголовки прокси (задача 001.10, ревью раунда 2),
разделение серверов Node API (задача 001.24, ревью раунда 1), пределы тела, одновременности и
частоты агентского и enrollment-server, бюджет Н-4 и потолки памяти контейнеров (001.28, 001.33).

uvicorn с ``--forwarded-allow-ips '*'`` берёт крайний левый элемент ``X-Forwarded-For``; значит
nginx обязан перезаписывать заголовок адресом соединения (``$remote_addr``), а не дополнять его
(``$proxy_add_x_forwarded_for``) — иначе клиент подставляет себе любой адрес. Enrollment
(``POST /agent/v1/enroll``) — единственный маршрут ``/agent/v1`` без клиентского сертификата
(security.md §7.1), поэтому он обслуживается отдельным ``server`` и обязан быть недоступен на
агентском (mTLS) и публичном. Файлы сверяются статически (сеть в тесте не поднимается);
поведение на стенде — в отчётах задач. Разбор конфигурации: комментарии снимаются вне кавычек,
содержимое строк в кавычках не образует блоков и не обрывает их, матчеры ``location`` уникальны,
``include`` кроме таблицы MIME запрещён — иначе страж читал бы не тот файл или не тот блок.
Стражи здесь положительные: каждая проксирующая операция Node API обязана нести свои зоны, тела
без известной длины — получать 411, буферы — равняться пределам, а потолки Compose — читаться из
файлов, которые стенд действительно накладывает (команда запуска в шапке базового файла и в
``deploy/README.md`` — одна и та же).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO = Path(__file__).resolve().parents[3]
NGINX_CONF = REPO / "deploy" / "nginx" / "nginx.conf"
ENTRYPOINT = REPO / "control-plane" / "docker-entrypoint.sh"
COMPOSE_DIR = REPO / "deploy" / "compose"


def _strip_comments(text: str) -> str:
    """Конфигурация без комментариев: ``#`` вне кавычек начинает комментарий до конца строки,
    ``#`` внутри строки (``add_header X "#fff"``) — часть значения. Регулярное выражение по
    ``#[^\\n]*`` срезало бы закрывающую кавычку и сдвинуло бы границы блоков."""
    out: list[str] = []
    i, quote = 0, None
    while i < len(text):
        char = text[i]
        if quote:
            out.append(char)
            if char == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "#":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _mask_strings(text: str) -> str:
    """Тот же текст той же длины, где содержимое строк в кавычках заменено пробелами: слово
    ``server`` и скобки внутри значения (``add_header X "server {"``, регулярный матчер
    ``location ~ "^/x\\{2\\}$"``) не образуют блоков и не обрывают их. Позиции, найденные по
    маске, применяются к исходному тексту."""
    out: list[str] = []
    i, quote = 0, None
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\" and i + 1 < len(text):
                out.append("  ")
                i += 2
                continue
            out.append(char if char == quote else " ")
            if char == quote:
                quote = None
        else:
            if char in ("'", '"'):
                quote = char
            out.append(char)
        i += 1
    return "".join(out)


def _config() -> str:
    return _strip_comments(NGINX_CONF.read_text(encoding="utf-8"))


def _block_end(masked: str, start: int) -> int:
    """Позиция закрывающей скобки блока, открытого в ``start`` (по маске без строк)."""
    depth = 0
    for i in range(start, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise AssertionError("блок не закрыт")


def _spans(text: str, opener: str) -> list[tuple[int, int, int]]:
    """Для каждого блока ``<opener> … { … }`` — начало директивы, позиция ``{`` и позиция ``}``."""
    masked = _mask_strings(text)
    return [
        (match.start(), match.end() - 1, _block_end(masked, match.end() - 1))
        for match in re.finditer(rf"\b{opener}\b[^{{;}}]*\{{", masked)
    ]


def _blocks(text: str, opener: str) -> list[str]:
    """Тела блоков ``<opener> … { … }`` со всем вложенным содержимым."""
    return [text[open_ + 1 : close] for _, open_, close in _spans(text, opener)]


def _server_blocks(text: str) -> list[str]:
    return _blocks(text, "server")


def _location_blocks(server: str) -> list[str]:
    return _blocks(server, "location")


def _locations(server: str) -> list[tuple[str, str]]:
    """Пары «матчер location — тело блока» в порядке объявления; матчеры уникальны (дубль
    nginx отверг бы, а ``dict(_locations(...))`` схлопнул бы молча)."""
    found = [
        (server[start + len("location") : open_].strip(), server[open_ + 1 : close])
        for start, open_, close in _spans(server, "location")
    ]
    matchers = [matcher for matcher, _ in found]
    assert len(set(matchers)) == len(matchers), matchers
    return found


def _without_blocks(text: str, opener: str) -> str:
    """Текст без блоков ``<opener> … { … }`` целиком — директивы уровня server без вложенных
    location."""
    out, cursor = [], 0
    for start, _, close in _spans(text, opener):
        if start < cursor:
            continue  # вложенный блок уже вырезан вместе с родителем
        out.append(text[cursor:start])
        cursor = close + 1
    out.append(text[cursor:])
    return "".join(out)


def _server_by_listen(text: str, port: str) -> str:
    """Единственный server, слушающий порт — в любой записи адреса (``8443``, ``[::]:8443``,
    ``0.0.0.0:8443``, ``*:8443``): второй server на том же порту в другой записи иначе остался бы
    невидимым."""
    listen = re.compile(rf"\blisten\s+(?:[\w.:\[\]*]*:)?{port}(?:\s|;)")
    found = [block for block in _server_blocks(text) if listen.search(block)]
    assert len(found) == 1, f"ровно один server слушает {port}"
    return found[0]


ENROLL_PATH = "/agent/v1/enroll"


def _map_rules(http_level: str, source: str, variable: str) -> tuple[str, list[tuple[str, str]]]:
    """Правила ``map <source> <variable> { … }``: значение по умолчанию и пары (шаблон, значение)
    в порядке объявления. Источник и переменная — литералами, как в файле."""
    header = re.compile(rf"map\s+{re.escape(source)}\s+{re.escape(variable)}\s*\{{")
    found = header.search(http_level)
    assert found is not None, (source, variable)
    body = http_level[found.end() : _block_end(_mask_strings(http_level), found.end() - 1)]
    default = re.search(r"\bdefault\s+(\S+);", body)
    assert default is not None, (variable, "default")
    rules = [
        (pattern.strip('"'), value)
        for pattern, value in re.findall(r'^\s*("[^"]+"|\S+)\s+(\S+);', body, re.M)
        if not pattern.startswith("default")
    ]
    return default.group(1), rules


def _map_lookup(default: str, rules: list[tuple[str, str]], key: str) -> str:
    """Результат map для ключа по правилам nginx: сначала точные строки (хеш по ключу в нижнем
    регистре — сравнение без учёта регистра, порядок в файле не важен), затем регулярные
    правила в порядке объявления по исходной строке (``~`` — с учётом регистра, ``~*`` — без),
    иначе умолчание. Раунд 8 перебирал правила подряд — точный ключ после покрывающего его
    регулярного правила отдавал бы значение регулярного (роаст раунда 8)."""
    for pattern, value in rules:
        if not pattern.startswith("~") and pattern.casefold() == key.casefold():
            return value
    for pattern, value in rules:
        if pattern.startswith("~*"):
            if re.search(pattern[2:], key, re.IGNORECASE):
                return value
        elif pattern.startswith("~"):
            if re.search(pattern[1:], key):
                return value
    return default


# ``include`` в любой записи — с несколькими пробелами, с пробелом перед ``;``, с маской: всё,
# что стоит между словом и точкой с запятой, — подключаемый путь.
INCLUDE = re.compile(r"\binclude\b([^;]*);")


def test_the_proxy_configuration_is_one_file_and_its_parser_survives_quoted_braces() -> None:
    """Стражи ниже читают один файл: ``include`` (кроме таблицы MIME) подмешал бы директивы,
    которых здесь не видно, — предел тела в подключённом файле обошёл бы страж пределов.
    Разбор блоков не считает скобки и слова внутри строк: фантомный ``server`` в значении
    заголовка или скобка в регулярном матчере сдвинули бы границы блоков, и стражи мерили бы не
    тот блок; второй server на том же порту в записи ``[::]:порт`` или ``*:порт`` не остаётся
    невидимым; ``include`` ловится в любой записи, а не только «слово, пробел, путь, точка с
    запятой»."""
    text = _config()
    assert [m.strip() for m in INCLUDE.findall(text)] == ["/etc/nginx/mime.types"]
    assert [m.strip() for m in INCLUDE.findall("include   /etc/nginx/conf.d/*.conf ;")] == [
        "/etc/nginx/conf.d/*.conf"
    ]
    planted = (
        'server { listen 1; add_header X "server { listen 2; }"; '
        'location ~ "^/x\\{2\\}$" { return 404; } location = /a { return 200 "{"; } }'
    )
    assert len(_server_blocks(planted)) == 1, "server внутри строки — не блок"
    assert [m for m, _ in _locations(_server_blocks(planted)[0])] == ['~ "^/x\\{2\\}$"', "= /a"]
    assert 'return 200 "{"' in dict(_locations(_server_blocks(planted)[0]))["= /a"]
    assert _server_by_listen("server { listen [::]:7; } server { listen 8; }", "7").strip() == (
        "listen [::]:7;"
    )
    assert _server_by_listen("server { listen *:9 ssl; } server { listen 8; }", "9").strip() == (
        "listen *:9 ssl;"
    )


def test_nginx_overwrites_forwarded_headers() -> None:
    """Во всём файле каждое вхождение X-Forwarded-For / X-Real-IP — только $remote_addr, каждое
    X-Forwarded-Proto — $scheme или https, дополнения $proxy_add_x_forwarded_for нет; в каждом
    из трёх server, проксирующих на api, полный набор задан на уровне server, а внутри location
    нет ни одного proxy_set_header — одна такая директива в location отменяет весь набор уровня
    server (наследование nginx), включая обнуление X-Client-Fingerprint."""
    text = _config()  # без комментариев
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
    text = _config()
    assert "$http_x_client_fingerprint" not in text, (
        "значение заголовка от клиента не попадает в апстрим ни на одном server"
    )
    expected = {"8443": "$ssl_client_fingerprint", "443": '""', "8444": '""'}
    for port, value in expected.items():
        block = _server_by_listen(text, port)
        found = re.findall(r"proxy_set_header\s+X-Client-Fingerprint\s+(\S+);", block)
        assert found == [value], (port, found)
    # Отпечаток осмыслен ровно настолько, насколько доверен якорь, по которому прокси проверяет
    # сертификат: подменённый ``ssl_client_certificate`` оставил бы всю схему на месте, только
    # доверять она стала бы другому CA. Глубина 1 — только листовые сертификаты, подписанные
    # этим CA напрямую: при глубине 2 нода с листом без CA:FALSE подписывала бы себе «сиблингов»
    # и множила отпечатки — ключи всех зон по сертификату.
    agent = _server_by_listen(text, "8443")
    assert re.findall(r"ssl_client_certificate\s+(\S+);", agent) == ["/etc/nginx/certs/ca.crt"]
    assert re.findall(r"ssl_verify_client\s+(\S+);", agent) == ["on"], "не optional"
    assert re.findall(r"ssl_verify_depth\s+(\d+);", agent) == ["1"]


def test_agent_server_holds_long_poll_longer_than_the_application_does() -> None:
    """Удержание состояния до 30 с (interfaces.md §5.2, реализация — 001.75) переживёт прокси
    только с запасом по ``proxy_read_timeout``: без него nginx рвёт соединение раньше и агент
    получает 504 вместо 204."""
    text = _config()
    agent = _server_by_listen(text, "8443")
    # Уровень server держит long-poll; location отчётов перекрывает его коротким пределом
    # (ответ на часть — две строки JSON, зависший api не держит слот парка 90 с); другие
    # location предел не трогают — общий location обслуживает long-poll и наследует server.
    server_level = re.findall(r"proxy_read_timeout\s+(\d+)s;", _without_blocks(agent, "location"))
    assert server_level and int(server_level[0]) >= 60, ("на уровне server ≥ 60 с", server_level)
    assert len(server_level) == 1, server_level
    for matcher, body in _locations(agent):
        found = re.findall(r"proxy_read_timeout\s+(\d+)s;", body)
        if matcher == f"= {REPORTS_PATH}":
            assert found and int(found[0]) <= 15, ("отчёты: короткий предел чтения ответа", found)
        else:
            assert not found, (matcher, "предел чтения наследуется от server")
    http_level = _without_blocks(text, "server")
    assert re.findall(r"proxy_connect_timeout\s+(\S+);", http_level) == ["2s"], "апстрим рядом"
    assert re.findall(r"proxy_send_timeout\s+(\S+);", http_level) == ["10s"]


def test_enrollment_is_served_only_by_its_own_server() -> None:
    """Граница §7.1: enrollment проксируется единственным server (8444, без клиентского
    сертификата) и ровно одним точным location; агентский server (8443, ``ssl_verify_client
    on``) отдаёт на этот путь 404, публичный не знает ``/agent`` вовсе. Проверяется статически —
    ручной curl в отчёте задачи 001.24 не удерживает конфигурацию от правки."""
    text = _config()
    public, agent, enrollment = (_server_by_listen(text, port) for port in ("443", "8443", "8444"))

    assert re.search(r"ssl_verify_client\s+on;", agent)
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
            # Всё прочее закрыто: 404, а именованные location отказов прокси — 429, 411 и 503
            # без апстрима.
            assert "proxy_pass" not in body and re.search(
                r"return\s+(404|411|429|503)\b|try_files\s+\S+\s+=404;", body
            ), (
                matcher,
                body,
            )

    agent_enroll = [b for m, b in _locations(agent) if m == f"= {ENROLL_PATH}"]
    assert len(agent_enroll) == 1 and "return 404" in agent_enroll[0], (
        "агентский server обязан отдавать 404 на enrollment, а не проксировать его"
    )
    assert "proxy_pass" not in agent_enroll[0]

    # Публичный server закрывает Node API положительно: точный `/agent` и префикс `^~ /agent/`
    # отвечают 404 (страж наличия, а не отсутствия — удаление обоих блоков открыло бы путь
    # через `location /`, проксирующий на api). `^~` побеждает и `location /`, и регулярные.
    public_locations = dict(_locations(public))
    assert "return 404" in public_locations["= /agent"], "точный /agent закрыт"
    assert "return 404" in public_locations["^~ /agent/"], "префикс /agent/ закрыт"
    assert "proxy_pass" not in public_locations["^~ /agent/"]
    for matcher, body in _locations(public):
        assert "/agent" not in matcher or "return 404" in body, (matcher, body)
    enrollment_locations = dict(_locations(enrollment))
    assert "try_files /nonexistent =404;" in enrollment_locations["/"], (
        "enrollment-server: всё прочее закрыто — через try_files, чтобы пределы применялись"
    )


REPORTS_PATH = "/agent/v1/reports"
BODY_LIMIT = re.compile(r"client_max_body_size\s+(\S+);")
BODY_BUFFER = re.compile(r"client_body_buffer_size\s+(\S+);")
# Полоса между самым большим допустимым телом и пределом прокси: тела в ней приложение читает —
# шире самого большого по числу скобок и запятых отвергает по байтам до разбора, остальное
# (пробелы, лишние ключи в пределах счётчиков) разбирает и отвергает по пределу модели. 1 МиБ —
# предел этой полосы, и он не растёт вместе с пределом.
DISCARD_BAND = 1024**2
RATE_LIMITED_BODY = (
    '{"error":{"code":"rate_limited","message":"превышен предел прокси","details":{}}}'
)
UPSTREAM_UNAVAILABLE_BODY = (
    '{"error":{"code":"upstream_unavailable","message":"сервис временно недоступен","details":{}}}'
)
NOT_FOUND_BODY = '{"error":{"code":"not_found","message":"нет такого пути","details":{}}}'
LENGTH_REQUIRED_BODY = (
    '{"error":{"code":"length_required","message":"тело без известной длины не принимается",'
    '"details":{}}}'
)
# Правило ``$body_without_length`` — таблицей входов «метод:длина:Transfer-Encoding» → значение:
# Transfer-Encoding при любом методе — 1, любой метод, кроме GET и HEAD, без Content-Length — 1
# (по HTTP/2 заголовка Transfer-Encoding нет: DELETE без длины иначе читался бы до 405 —
# роаст раунда 8), GET без тела (long-poll состояния) и тело с известной длиной — 0. Страж
# прогоняет таблицу через правила из конфигурации, а не сверяет их текст.
BODY_WITHOUT_LENGTH_CASES = [
    ("GET::", "0"),
    ("GET:0:", "0"),
    ("HEAD::", "0"),
    ("DELETE::", "1"),
    ("OPTIONS::", "1"),
    ("M-SEARCH::", "1"),
    ("DELETE:0:", "0"),
    ("POST:123:", "0"),
    ("POST:0:", "0"),
    ("PUT:1048576:", "0"),
    ("POST::", "1"),
    ("PUT::", "1"),
    ("PATCH::", "1"),
    ("POST::chunked", "1"),
    ("POST:123:chunked", "1"),
    ("DELETE::chunked", "1"),
    ("OPTIONS::chunked", "1"),
    ("GET::chunked", "1"),
    ("M-SEARCH::chunked", "1"),
    ("POST:123:identity", "1"),
]


def _bytes(size: str) -> int:
    """Значение ``client_max_body_size`` в байтах: суффиксы nginx ``k`` и ``m`` (степени двойки)."""
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    return int(size[:-1]) * units[size[-1]] if size[-1] in units else int(size)


def _reports_location(agent: str) -> str:
    reports = [body for matcher, body in _locations(agent) if matcher == f"= {REPORTS_PATH}"]
    assert len(reports) == 1, "точный location отчётов объявлен ровно один раз"
    return reports[0]


def test_agent_server_does_not_accept_report_sized_bodies_everywhere() -> None:
    """Тело запроса nginx принимает до того, как приложение проверит версию агента и identity
    (FastAPI читает его перед разрешением зависимостей). Поэтому предел агентского `server` —
    по самой большой из мелких операций раздела (64 КБ, 001.28) и объявлен один раз на уровне
    server; отчёт о трафике получает свой предел ровно в одном точном `location`, который
    проксирует на тот же апстрим `$api`, и ни один другой `location` предел не переопределяет
    (001.33). Отчёты не принимает ни enrollment-, ни публичный server — и у обоих Node API
    закрыт положительно (см. страж enrollment); у enrollment-server предел и буфер те же 64 КиБ.
    Буферы равны пределам: тело с известной длиной до предела лежит в памяти целиком и во
    временный файл не пишется; тел без длины на этих server не бывает (411, страж ниже)."""
    text = _config()
    agent = _server_by_listen(text, "8443")
    server_level = _without_blocks(agent, "location")
    assert BODY_LIMIT.findall(server_level) == ["64k"], (
        "предел уровня server — по мелким операциям 001.28, один раз"
    )
    reports = _reports_location(agent)
    assert "proxy_pass $api;" in reports, "location отчётов проксирует на api, не на чужой апстрим"
    assert BODY_LIMIT.findall(reports) == ["1m"], "предел отчётов — литерал 001.33"
    for matcher, body in _locations(agent):
        if matcher != f"= {REPORTS_PATH}":
            assert not BODY_LIMIT.search(body), (matcher, "предел поднят только для отчётов")
    for port in ("443", "8444"):
        for matcher, _ in _locations(_server_by_listen(text, port)):
            assert REPORTS_PATH not in matcher, (port, "отчёты принимает только агентский server")
    assert "return 404" in dict(_locations(_server_by_listen(text, "443")))["^~ /agent/"]
    # Буферы равны пределам: тело операции раздела во временный файл не пишется (nginx кладёт
    # туда всё, что не поместилось в буфер, — на стенде это случалось с 60-КиБ heartbeat при
    # буфере по умолчанию 16k), а тело отчёта (адреса пользователей, §16) остаётся в памяти
    # целиком. Буфер больше предела был бы памятью, которую тело с известной длиной не
    # заполнит, буфер меньше — временным файлом на каждом теле между ними.
    assert BODY_BUFFER.findall(server_level) == ["64k"], "буфер server — литерал"
    assert BODY_BUFFER.findall(reports) == ["1m"], "буфер отчётов — литерал"
    for name, block in (("server", server_level), ("reports", reports)):
        assert _bytes(BODY_BUFFER.findall(block)[0]) == _bytes(BODY_LIMIT.findall(block)[0]), (
            name,
            "буфер равен пределу",
        )
    enrollment_level = _without_blocks(_server_by_listen(text, "8444"), "location")
    assert BODY_LIMIT.findall(enrollment_level) == ["64k"], "enrollment: тот же предел"
    assert BODY_BUFFER.findall(enrollment_level) == ["64k"], "enrollment: буфер равен пределу"
    for block in (agent, _server_by_listen(text, "8444")):
        for matcher, body in _locations(block):
            if matcher != f"= {REPORTS_PATH}":
                assert not BODY_BUFFER.search(body), (matcher, "буфер задан вместе с пределом")


def test_the_reports_location_bounds_concurrency_and_rate_per_certificate() -> None:
    """Предел тела без предела одновременности — умножение: тело читается до identity, и
    держатель любого сертификата CA (в том числе с отозванной identity — nginx до 001.25 её не
    знает) оплачивался бы приложением без ограничений. Зоны — по отпечатку клиентского
    сертификата (единственный признак, который прокси знает без базы) и общие на парк с
    постоянным ключом (``$server_name`` у всех трёх server — ``_``, и та же зона на другом
    server делила бы ведро с парком); значения — литералами. `limit_conn` не наследуется
    location, объявившим свой, поэтому предел соединений ноды обязан быть повторён в location
    отчётов — иначе у ноды 8 + 1 соединений. Остальные операции раздела получают предел частоты
    по сертификату и на парк в общем location и наследуют предел соединений server; пауза между
    чтениями тела ограничена на уровне server для всех операций. Enrollment-server — единственный
    анонимный вход — получает пределы по адресу источника (``$binary_remote_addr``: один адрес не
    выбирает enrollment всему парку) и общий потолок частоты (зона ``"enroll"``). Страж
    положительный: каждая проксирующая операция обоих server несёт зону по своему ключу и зону
    парка — новый location без зон был бы дырой, которую страж отсутствия не заметил бы."""
    text = _config()
    http_level = _without_blocks(text, "server")
    zones = re.findall(r"limit_conn_zone\s+(\S+)\s+zone=(\w+):(\w+);", http_level)
    assert zones == [
        ("$ssl_client_fingerprint", "agent_conn", "1m"),
        ("$ssl_client_fingerprint", "agent_report_conn", "1m"),
        ('"agent"', "agent_report_total", "1m"),
        ("$binary_remote_addr", "enroll_conn_addr", "1m"),
    ], zones
    assert re.findall(r"limit_req_zone\s+(\S+)\s+zone=(\w+):\w+\s+rate=(\S+);", http_level) == [
        ("$ssl_client_fingerprint", "agent_report_rate", "2r/s"),
        ("$ssl_client_fingerprint", "agent_rate", "5r/s"),
        ('"agent"', "agent_fleet_rate", "15r/s"),
        ("$binary_remote_addr", "enroll_addr_rate", "1r/s"),
        ('"enroll"', "enroll_fleet_rate", "2r/s"),
    ]
    agent = _server_by_listen(text, "8443")
    server_level = _without_blocks(agent, "location")
    assert re.findall(r"limit_conn\s+(\w+)\s+(\d+);", server_level) == [("agent_conn", "8")]
    # Частота остальных операций — на уровне server: наследуется всеми location без своего
    # limit_req, включая пути отказа (411, 404, @rate_limited) — их не выбрать без 429.
    assert re.findall(r"limit_req\s+zone=(\w+)\s+burst=(\d+);", server_level) == [
        ("agent_rate", "20"),
        ("agent_fleet_rate", "200"),
    ], "частота остальных операций — на сертификат и на весь парк, на уровне server"
    assert re.findall(r"limit_conn_status\s+(\d+);", server_level) == ["429"], "код объявлен в §5.2"
    assert re.findall(r"limit_req_status\s+(\d+);", server_level) == ["429"]
    assert re.findall(r"client_body_timeout\s+(\S+);", server_level) == ["10s"], (
        "пауза между чтениями тела — на уровне server, для всех операций"
    )
    reports = _reports_location(agent)
    assert re.findall(r"limit_conn\s+(\w+)\s+(\d+);", reports) == [
        ("agent_conn", "8"),
        ("agent_report_conn", "1"),
        ("agent_report_total", "2"),
    ], "предел ноды повторён; части отчёта идут последовательно; две на парк — бюджет Н-4"
    assert re.findall(r"limit_req\s+zone=(\w+)\s+burst=(\d+);", reports) == [
        ("agent_report_rate", "10")
    ], "без nodelay: лишние отчёты ждут очереди, а не получают отказ"
    general = dict(_locations(agent))["^~ /agent/v1/"]
    assert "limit_req" not in general and "limit_conn" not in general, (
        "остальные операции наследуют частоту и соединения от server"
    )
    for matcher, body in _locations(agent):
        if matcher != f"= {REPORTS_PATH}":
            assert "limit_req" not in body and "limit_conn" not in body, matcher
        assert "client_body_timeout" not in body, (matcher, "пауза задана один раз на server")
    enrollment = _server_by_listen(text, "8444")
    enrollment_level = _without_blocks(enrollment, "location")
    assert re.findall(r"limit_conn\s+(\w+)\s+(\d+);", enrollment_level) == [
        ("enroll_conn_addr", "4")
    ], "соединения enrollment — по адресу источника"
    assert re.findall(r"limit_req\s+zone=(\w+)\s+burst=(\d+);", enrollment_level) == [
        ("enroll_addr_rate", "5"),
        ("enroll_fleet_rate", "10"),
    ], "частота enrollment — по адресу источника и на всех клиентов, на уровне server"
    assert re.findall(r"limit_conn_status\s+(\d+);", enrollment_level) == ["429"]
    assert re.findall(r"limit_req_status\s+(\d+);", enrollment_level) == ["429"]
    assert re.findall(r"client_body_timeout\s+(\S+);", enrollment_level) == ["10s"]
    for matcher, body in _locations(enrollment):
        assert "limit_req" not in body and "limit_conn" not in body, (matcher, "наследуется")
    # Положительное покрытие с наследованием: действующие зоны location — его собственные
    # limit_req (если объявил хоть один) или зоны уровня server, плюс limit_conn по тому же
    # правилу; каждый location обоих server — проксирующий, отказной или именованный — несёт
    # зону по своему ключу и зону парка. Ключ зоны берётся из объявления на уровне http.
    key_of = {name: key for key, name, _ in zones}
    key_of.update(
        {
            name: key
            for key, name in re.findall(
                r"limit_req_zone\s+(\S+)\s+zone=(\w+):\w+\s+rate=\S+;", http_level
            )
        }
    )
    expected_keys = {
        "8443": {"$ssl_client_fingerprint", '"agent"'},
        "8444": {"$binary_remote_addr", '"enroll"'},
    }

    def effective(server: str, body: str) -> set[str]:
        level = _without_blocks(server, "location")
        req = re.findall(r"limit_req\s+zone=(\w+)", body) or re.findall(
            r"limit_req\s+zone=(\w+)", level
        )
        conn = re.findall(r"limit_conn\s+(\w+)\s+\d+;", body) or re.findall(
            r"limit_conn\s+(\w+)\s+\d+;", level
        )
        return {key_of[z] for z in req + conn}

    for port, keys in expected_keys.items():
        server = _server_by_listen(text, port)
        located = _locations(server)
        assert any("proxy_pass" in b for _, b in located), port
        for matcher, body in located:
            assert effective(server, body) == keys, (port, matcher, effective(server, body))
    # Зоны — только на своём server: по отпечатку сертификата на server без mTLS они пусты и
    # молча ничего не ограничат, зоны enrollment на агентском делили бы ведро с анонимами. Все
    # server файла, включая служебный (8081), — по объявленным на http именам зон.
    agent_zones = {name for name in key_of if name.startswith("agent_")}
    enroll_zones = {name for name in key_of if name.startswith("enroll_")}
    assert agent_zones | enroll_zones == set(key_of), "каждая зона — семейства agent_ или enroll_"
    for block in _server_blocks(text):
        port = re.search(r"\blisten\s+(?:[\w.:\[\]*]*:)?(\d+)", block).group(1)  # type: ignore[union-attr]
        used = set(re.findall(r"limit_(?:conn|req)\s+(?:zone=)?(\w+)", block))
        allowed = {"8443": agent_zones, "8444": enroll_zones}.get(port, set())
        assert used <= allowed, (port, used - allowed)


# Норматив Н-4: p95 ≤ 200 мс на Node API — по решению заказчика считается по времени апстрима
# (`urt`: прокси буферизует тело целиком до передачи и ответ целиком после — заливка при полосе
# ноды и отдача ответа клиенту вне норматива, записаны в README как обязанность таймаута
# агента), поэтому стоимость разбора — p95 `urt`. Измерено на стенде (VM, `python:3.14-slim`,
# 2026-09-14…15) через nginx: тело потолка из `MEASURED_ELEMENTS` элементов (500 строк / 2 500
# адресов — константа измерена при этом отношении, иной состав тела означает перемер) — p95
# `MEASURED_P95_MS` (раунд 5, два потока отчётов, 200 замеров; раунд 6, один поток — 46 мс,
# медиана 23; на теле из 6 000 — 88 мс, 14,7 мкс на элемент: доля накладных на запрос падает с
# размером, гейт берёт большее); отсюда `PARSE_US_PER_ELEMENT` — самостоятельная константа гейта,
# согласованная с замером отдельным ассертом (а не частное от пределов, которые гейт держит: оно
# сокращалось бы). Модель «две части одновременно = 2 × p95» проверена сотней пар частей,
# стартующих в один момент: p95 второй из пары — `MEASURED_PAIR_P95_MS`, не больше модели. Худшее
# тело отчёта в пределах формы (3 000 элементов по три лишних ключа — 18 000 ошибок pydantic) —
# `MEASURED_REPORT_WORST_FAILURE_P95_MS` (раунд 8, 100 замеров): путь отказа не дороже пути приёма,
# и это измерено, а не предположено. Остальные операции: после проверки формы по байтам на
# каждой из них (раунд 8) самое дорогое 64-КиБ тело из минимально коротких лишних ключей (≈8 500
# ключей) стоит `MEASURED_HEARTBEAT_JUNK_P95_MS` (200 замеров), результат команды с бюджетом
# свободного текста `error` (2 000 лишних ключей доходят до pydantic) —
# `MEASURED_COMMAND_RESULT_JUNK_P95_MS` (закрытие, 200 замеров) — самая дорогая из остальных
# операций, то же на enrollment-server — `MEASURED_ENROLL_JUNK_P95_MS` (60 замеров; заглушка без
# подписи CSR — перемер в 001.25; в раундах 6–7 без проверки формы те же тела стоили 13…24 мс, и
# частоту парка резали до 8 r/s).
# Операции остальных видов входят в бюджет отчёта не одной штукой, а всеми, что прокси
# пропускает за окно разбора двух частей, по обеим зонам — парка (`agent_fleet_rate`) и
# enrollment (`enroll_fleet_rate`, тот же цикл событий): очередь выдаёт их равномерно, цикл
# обслуживает в порядке поступления — за окно `parse` секунд прибывает `ceil(parse × rate)`
# операций плюс одна, начатая до окна. Частота на парк и на enrollment вместе держится долей
# одного цикла `H4_OTHER_LOAD_SHARE` (решение, записанное в §5.2). Гейт держит Н-4 одним
# процессом: потолок одновременности отчётов (2) ограничивает и очередь цикла, и его загрузку —
# на насыщении цикл занят отчётами целиком, что и означает потолок; при 4 мс на операцию гейт
# допускал бы и ≈60 r/s парка — частоту 15 r/s закрепляет литерал зоны, не бюджет. Резерв на
# транзакцию приёма 001.34/001.77 — `H4_TRANSACTION_ALLOWANCE_MS`, оценка без замера (транзакции
# ещё нет), уточняет 001.73; ниже `H4_TRANSACTION_ALLOWANCE_FLOOR_MS` без замера не опускается.
# Бюджет Н-4 читается из строки норматива в docs/idea.md — литерал здесь расходился бы с
# постановкой молча. Константа стоимости элемента измерена при `MEASURED_ELEMENTS` элементах в
# отношении 500/2 500: другие пределы модели — перемер, и гейт это требует равенством.
PARSE_US_PER_ELEMENT = 16.7
MEASURED_ELEMENTS = 3_000
MEASURED_P95_MS = 50
MEASURED_PAIR_P95_MS = 69
MEASURED_REPORT_WORST_FAILURE_P95_MS = 37
MEASURED_HEARTBEAT_JUNK_P95_MS = 2
MEASURED_COMMAND_RESULT_JUNK_P95_MS = 4
MEASURED_ENROLL_JUNK_P95_MS = 2
H4_OTHER_OPERATION_MS = 4
H4_ENROLL_OPERATION_MS = 2
H4_TRANSACTION_ALLOWANCE_MS = 50
H4_TRANSACTION_ALLOWANCE_FLOOR_MS = 50
H4_OTHER_LOAD_SHARE = 0.25
IDEA = REPO / "docs" / "idea.md"


def _h4_budget_ms() -> int:
    """p95 норматива Н-4 из таблицы нефункциональных требований постановки."""
    row = re.search(r"^\| Н-4 \|[^|]*p95 ≤ (\d+) мс", IDEA.read_text(encoding="utf-8"), re.M)
    assert row is not None, "строка Н-4 в docs/idea.md"
    return int(row.group(1))


def _rate_per_second(http_level: str, zone: str) -> float:
    found = re.search(rf"zone={zone}:\w+\s+rate=(\d+)r/([sm]);", http_level)
    assert found is not None, zone
    return int(found.group(1)) / (1 if found.group(2) == "s" else 60)


def test_the_fleet_ceiling_keeps_the_parse_budget_of_h4() -> None:
    """Худший случай Н-4 для отчёта: все части, которые прокси пропускает одновременно, в
    одном процессе, плюс операции остальных видов и enrollment, прибывающие за окно их разбора,
    — в бюджете за вычетом резерва на транзакцию; остальные операции парка и enrollment-server
    вместе не занимают больше доли цикла. Каждый множитель — самостоятельный литерал,
    привязанный к замеру равенством или нижней границей; бюджет — из постановки. Ассерта «сумма
    классов на потолках прокси не больше цикла» нет намеренно: при потолке одновременности 2
    цикл на насыщении занят отчётами целиком — это и есть назначение потолка, задержку держит
    оконная модель выше, а раунд 8 считал знаменатель по самому широкому телу и был зелёным
    только на нём (компактная законная часть с теми же элементами давала 1 050 > 1 000 —
    роаст раунда 8); ёмкость на насыщении — 1/p95 = 20 частей/с при спросе парка §5.7 около
    одной части в секунду (001.73)."""
    from app.accounting.service import REPORT_MAX_LINES, REPORT_MAX_ONLINE_IPS

    text = _config()
    reports = _reports_location(_server_by_listen(text, "8443"))
    concurrency = int(re.search(r"limit_conn\s+agent_report_total\s+(\d+);", reports).group(1))  # type: ignore[union-attr]
    elements = REPORT_MAX_LINES + REPORT_MAX_ONLINE_IPS
    budget_ms = _h4_budget_ms()
    assert budget_ms == 200, "число постановки — при его смене гейт пересчитывается осознанно"
    # Константы согласованы с замерами — иначе они числа из головы; пределы модели — те, при
    # которых мерили стоимость элемента.
    assert elements == MEASURED_ELEMENTS, (
        "пределы модели изменились — стоимость элемента перемерить"
    )
    assert (REPORT_MAX_LINES, REPORT_MAX_ONLINE_IPS) == (500, 2_500), "отношение замера"
    assert abs(MEASURED_ELEMENTS * PARSE_US_PER_ELEMENT / 1000 - MEASURED_P95_MS) <= 1
    assert H4_OTHER_OPERATION_MS == max(
        MEASURED_HEARTBEAT_JUNK_P95_MS, MEASURED_COMMAND_RESULT_JUNK_P95_MS
    ), "самая дорогая из остальных операций — результат команды с бюджетом свободного текста"
    assert H4_ENROLL_OPERATION_MS == MEASURED_ENROLL_JUNK_P95_MS
    assert MEASURED_REPORT_WORST_FAILURE_P95_MS <= MEASURED_P95_MS, (
        "путь отказа отчёта дороже пути приёма — стоимость части считать по отказу"
    )
    assert H4_TRANSACTION_ALLOWANCE_MS >= H4_TRANSACTION_ALLOWANCE_FLOOR_MS, (
        "резерв на транзакцию снижается только по замеру 001.73"
    )
    assert H4_TRANSACTION_ALLOWANCE_FLOOR_MS == 50, "пол резерва — число задачи, не гейта"
    assert H4_OTHER_LOAD_SHARE == 0.25, "четверть цикла — решение, записанное в interfaces.md §5.2"
    parse_ms = elements * PARSE_US_PER_ELEMENT / 1000 * concurrency
    assert MEASURED_PAIR_P95_MS <= parse_ms, "замер пар частей опровергает модель «слоты × p95»"
    http_level = _without_blocks(text, "server")
    fleet_rate = _rate_per_second(http_level, "agent_fleet_rate")
    enroll_rate = _rate_per_second(http_level, "enroll_fleet_rate")
    # Окно разбора: операции обеих зон, которые их очереди выдают за это время, плюс по одной,
    # начатой до окна, — все в том же цикле событий перед ответом на части.
    others_fleet = math.ceil(parse_ms / 1000 * fleet_rate) + 1
    others_enroll = math.ceil(parse_ms / 1000 * enroll_rate) + 1
    worst_ms = (
        parse_ms + others_fleet * H4_OTHER_OPERATION_MS + others_enroll * H4_ENROLL_OPERATION_MS
    )
    allowed_ms = budget_ms - H4_TRANSACTION_ALLOWANCE_MS
    assert worst_ms <= allowed_ms, (
        f"{concurrency} частей × {elements} элементов × {PARSE_US_PER_ELEMENT} мкс + "
        f"{others_fleet} × {H4_OTHER_OPERATION_MS} мс операций парка + {others_enroll} × "
        f"{H4_ENROLL_OPERATION_MS} мс enrollment за окно = {worst_ms:.0f} мс — больше бюджета "
        f"Н-4 {budget_ms} мс за вычетом резерва {H4_TRANSACTION_ALLOWANCE_MS} мс"
    )
    other_load_ms = fleet_rate * H4_OTHER_OPERATION_MS + enroll_rate * H4_ENROLL_OPERATION_MS
    assert other_load_ms <= H4_OTHER_LOAD_SHARE * 1000, (
        f"остальные операции парка и enrollment занимают {other_load_ms:.0f} мс цикла в секунду"
        f" — больше доли {H4_OTHER_LOAD_SHARE:.0%}"
    )


def test_the_upstream_time_reading_of_h4_rests_on_request_buffering() -> None:
    """Н-4 считается по времени апстрима (`urt`) потому, что прокси читает тело целиком до
    передачи и ответ целиком без оглядки на клиента: с `proxy_request_buffering off` заливка при
    полосе ноды вошла бы в `urt`, с `proxy_buffering off` — отдача ответа клиенту, и замеры
    гейта мерили бы сеть. Оба умолчания nginx объявлены явно на уровне http и нигде не
    переопределены; ответ сверх буферов уходит во временный файл на tmpfs не больше
    `proxy_max_temp_file_size` (литерал; запрет файлов раунда 7 держал апстрим на скорости
    клиента — снят решением заказчика); ошибки апстрима не перехватываются
    (`proxy_intercept_errors` — 429 приложения дойдут со своим телом); заикание DNS Docker
    ограничено `resolver_timeout`; отказы пределов не пишутся в error.log уровня warn;
    заголовки запроса ждутся не дольше 5 с (`limit_conn` считает запросы в полёте, а
    соединение с байтом заголовка раз в минуту держало бы слот worker'а, общий у трёх server, —
    роаст раунда 8), и соединение по таймауту сбрасывается, а не ждёт закрытия."""
    text = _config()
    http_level = _without_blocks(text, "server")
    for directive in ("proxy_request_buffering", "proxy_buffering"):
        assert re.findall(rf"{directive}\s+(\w+);", text) == ["on"], (directive, "один раз, on")
        assert re.findall(rf"{directive}\s+(\w+);", http_level) == ["on"], (directive, "http")
    assert re.findall(r"proxy_max_temp_file_size\s+(\S+);", text) == ["8m"]
    assert re.findall(r"proxy_max_temp_file_size\s+(\S+);", http_level) == ["8m"]
    assert "proxy_intercept_errors" not in text, "429 и 4xx приложения проходят со своим телом"
    assert re.findall(r"resolver_timeout\s+(\S+);", http_level) == ["5s"]
    assert re.findall(r"limit_req_log_level\s+(\w+);", http_level) == ["notice"]
    assert re.findall(r"limit_conn_log_level\s+(\w+);", http_level) == ["notice"]
    assert re.findall(r"client_header_timeout\s+(\S+);", text) == ["5s"]
    assert re.findall(r"client_header_timeout\s+(\S+);", http_level) == ["5s"]
    assert re.findall(r"reset_timedout_connection\s+(\w+);", http_level) == ["on"]


def test_bodies_without_known_length_are_refused_with_411_on_both_node_api_servers() -> None:
    """Контракт Node API обещает `Content-Length`; тело без известной длины (chunked, поток
    HTTP/2 без content-length) нужно только враждебному клиенту — с ним прокси держал бы сырой
    поток с обрамлением без верхней границы и писал бы временные файлы. Правило — одна map на
    уровне http и `if … return 411` на уровне каждого из двух server Node API (не в location:
    иначе новый location его бы не унаследовал), ответ — единый формат без `Retry-After`
    (не повторять), через `error_page` уровня server."""
    text = _config()
    http_level = _without_blocks(text, "server")
    default, rules = _map_rules(
        http_level,
        '"$request_method:$content_length:$http_transfer_encoding"',
        "$body_without_length",
    )
    # Страж подаёт на правила входы, а не сверяет их текст с самим собой: любой метод с
    # Transfer-Encoding — 1 (в раунде 7 DELETE с chunked читался до 405 — стенд), любой метод,
    # кроме GET и HEAD, без длины — 1 (по HTTP/2 DELETE без длины читался до 405 — раунд 8),
    # GET без тела и тело с известной длиной — 0.
    for key, expected in BODY_WITHOUT_LENGTH_CASES:
        assert _map_lookup(default, rules, key) == expected, (key, rules)
    for port in ("8443", "8444"):
        server = _server_by_listen(text, port)
        level = _without_blocks(server, "location")
        guards = [b for b in _blocks(level, "if") if "return 411" in b]
        assert len(guards) == 1, (
            port,
            "if ($body_without_length) { return 411; } на уровне server",
        )
        assert re.search(r"if\s*\(\$body_without_length\)\s*\{", level), port
        assert re.findall(r"error_page\s+411\s+=\s+(\S+);", level) == ["@length_required"], port
        named = [body for matcher, body in _locations(server) if matcher == "@length_required"]
        assert len(named) == 1, port
        assert "default_type application/json;" in named[0]
        assert f"return 411 '{LENGTH_REQUIRED_BODY}';" in named[0]
        assert "Retry-After" not in named[0], "411 не повторяется"
        for matcher, body in _locations(server):
            assert "$body_without_length" not in body, (port, matcher, "правило — уровня server")
    assert json.loads(LENGTH_REQUIRED_BODY)["error"]["code"] == "length_required"
    public = _server_by_listen(text, "443")
    assert "$body_without_length" not in public, "публичный server: браузеры и chunked — 001.66"


def test_proxy_rate_limit_answers_in_the_unified_error_format_with_retry_after() -> None:
    """§5.2: отказы несут `Retry-After`, §5.1: тело ошибки — единый формат. 429 от пределов
    прокси иначе был бы страницей nginx без заголовка, и агент разбирал бы HTML — на агентском
    и на enrollment-server одинаково. `error_page` внутри location перекрыл бы обработку именно
    там — его нет ни в одном location."""
    text = _config()
    for port in ("8443", "8444"):
        server = _server_by_listen(text, port)
        assert re.findall(
            r"error_page\s+429\s+=\s+(\S+);", _without_blocks(server, "location")
        ) == ["@rate_limited"], port
        for matcher, body in _locations(server):
            assert "error_page" not in body, (port, matcher)
        limited = [body for matcher, body in _locations(server) if matcher == "@rate_limited"]
        assert len(limited) == 1, port
        assert "default_type application/json;" in limited[0]
        assert re.findall(r"add_header\s+Retry-After\s+(\d+)\s+always;", limited[0]) == ["1"]
        assert f"return 429 '{RATE_LIMITED_BODY}';" in limited[0]
        # Апстрим недоступен (перезапуск api, таймаут прокси) — тот же принцип: код, о котором
        # агент узнал бы только в бою, объявлен и отдаётся в едином формате с Retry-After.
        assert re.findall(
            r"error_page\s+502\s+503\s+504\s+=\s+(\S+);", _without_blocks(server, "location")
        ) == ["@upstream_unavailable"], port
        unavailable = [
            body for matcher, body in _locations(server) if matcher == "@upstream_unavailable"
        ]
        assert len(unavailable) == 1, port
        assert "default_type application/json;" in unavailable[0]
        assert re.findall(r"add_header\s+Retry-After\s+(\d+)\s+always;", unavailable[0]) == ["5"]
        assert f"return 503 '{UPSTREAM_UNAVAILABLE_BODY}';" in unavailable[0]
        # 404 — тоже через именованный location: `return 404` прямо в location исполняется в
        # фазе REWRITE, раньше PREACCESS с limit_req/limit_conn, и залп на несуществующий путь
        # не знал бы пределов (роаст раунда 8); внутренний редирект проходит PREACCESS заново.
        assert re.findall(
            r"error_page\s+404\s+=\s+(\S+);", _without_blocks(server, "location")
        ) == ["@not_found"], port
        missing = [body for matcher, body in _locations(server) if matcher == "@not_found"]
        assert len(missing) == 1, port
        assert "default_type application/json;" in missing[0]
        assert "Retry-After" not in missing[0], "404 не повторяется"
        assert f"return 404 '{NOT_FOUND_BODY}';" in missing[0]
        # Сам отказ — через try_files, не return: return исполняется в REWRITE до пределов, и
        # error_page после него пределов не даёт (стенд, закрытие: 30 параллельных 404 без 429);
        # try_files решает в PRECONTENT после limit_req/limit_conn.
        catch_all = dict(_locations(server))["/"]
        assert "try_files /nonexistent =404;" in catch_all and "return" not in catch_all, port
    assert json.loads(RATE_LIMITED_BODY)["error"]["code"] == "rate_limited"
    assert json.loads(UPSTREAM_UNAVAILABLE_BODY)["error"]["code"] == "upstream_unavailable"
    assert json.loads(NOT_FOUND_BODY)["error"]["code"] == "not_found"


def test_nginx_logs_request_and_upstream_times() -> None:
    """Норматив Н-4 (p95 ≤ 200 мс, по времени апстрима) проверяем только там, где видно время:
    журнал доступа несёт время запроса целиком и время ответа апстрима — иначе заливку тела
    отчёта не отделить от его разбора."""
    text = _config()
    log_format = re.search(r"log_format\s+main\s+((?:'[^']*'\s*)+);", text)
    assert log_format is not None
    assert "rt=$request_time" in log_format.group(1)
    assert "urt=$upstream_response_time" in log_format.group(1)


def test_the_subscription_token_never_reaches_the_access_log() -> None:
    """Н-25: журнал пишется только по `$loggable`, а он требует согласия трёх ключей — по
    нормализованному `$uri` (формы `//s/`, `/%73/`, `/x/../s/` маршрутизируются в `/s/`), по
    сырой строке `$request` (`/s/` или `/%73/` в любом месте строки: метод с дефисом, второй
    пробел, absolute-form и сырые `//s/` обходили якорь «^[A-Z]+ /s/» — раунд 7, стенд) и по
    признаку «отвергнут до разбора URI» (400 с пустым `$uri` — такая строка пишется форматом
    без строки запроса). Страж подаёт на правила таблицу сырых строк и ключей, а не сверяет их
    текст; четвёртая линия — `access_log off` в самом location `/s/`."""
    text = _config()
    http_level = _without_blocks(text, "server")
    # Самопроверка эмулятора map: точный ключ побеждает покрывающее его регулярное правило,
    # объявленное раньше, и сравнивается без учёта регистра; регулярные — по порядку.
    assert _map_lookup("d", [("~^a", "r"), ("A", "x")], "a") == "x"
    assert _map_lookup("d", [("~^a", "r"), ("~^ab", "s")], "ab") == "r"
    assert _map_lookup("d", [("~^A", "r")], "ab") == "d"
    default, rules = _map_rules(http_level, "$request", "$loggable_raw")
    for line, expected in (
        ("GET /s/T HTTP/1.1", "0"),
        ("GET /s/T?x=1 HTTP/1.1", "0"),
        ("GET /healthz?/s/ HTTP/1.1", "1"),
        ("GET /healthz?x=/%73/y HTTP/1.1", "1"),
        ("POST /agent/v1/reports?x=/s/ HTTP/2.0", "1"),
        ("GET /s/T%zz HTTP/1.1", "0"),
        ("GET  /s/T%zz HTTP/1.1", "0"),
        ("M-SEARCH /s/T%zz HTTP/1.1", "0"),
        ("GET //s/T%zz HTTP/1.1", "0"),
        ("GET /x/../s/T HTTP/1.1", "0"),
        ("GET /%73/T%zz HTTP/1.1", "0"),
        ("GET /%53/T HTTP/1.1", "0"),
        ("GET http://host/s/T HTTP/1.1", "0"),
        ("GET /S/T HTTP/2.0", "0"),
        ("GET //s/T", "0"),
        ("GET /healthz HTTP/1.1", "1"),
        ("POST /agent/v1/reports HTTP/2.0", "1"),
        ("GET /assets/logo.svg HTTP/1.1", "1"),
        ("GET /sub/s2/x HTTP/1.1", "1"),
    ):
        assert _map_lookup(default, rules, line) == expected, (line, rules)
    default, rules = _map_rules(http_level, "$uri", "$loggable_uri")
    for uri, expected in (("/s/T", "0"), ("/S/T", "0"), ("/healthz", "1"), ("", "1")):
        assert _map_lookup(default, rules, uri) == expected, (uri, rules)
    default, rules = _map_rules(http_level, '"$status:$uri"', "$unparsed")
    for key, expected in (("400:", "1"), ("400:/s/T", "0"), ("404:", "0"), ("200:/healthz", "0")):
        assert _map_lookup(default, rules, key) == expected, (key, rules)
    default, rules = _map_rules(http_level, '"$loggable_uri$loggable_raw$unparsed"', "$loggable")
    for key, expected in (("110", "1"), ("010", "0"), ("100", "0"), ("111", "0"), ("000", "0")):
        assert _map_lookup(default, rules, key) == expected, (key, rules)
    logs = re.findall(r"access_log\s+(\S+)\s+(\w+)\s+if=(\S+);", http_level)
    assert logs == [
        ("/var/log/nginx/access.log", "main", "$loggable"),
        ("/var/log/nginx/access.log", "unparsed", "$unparsed"),
    ], logs
    unparsed_format = re.search(r"log_format\s+unparsed\s+((?:'[^']*'\s*)+);", http_level)
    assert unparsed_format is not None
    for variable in ("$request", "$uri", "$request_uri", "$args", "$http_referer", "$log_referer"):
        assert not re.search(rf"\{variable}(?![A-Za-z_])", unparsed_format.group(1)), (
            variable,
            "не в формате без разбора",
        )
    assert "$request_uri" not in text, "по $request_uri токен из отвергнутого запроса не виден"
    subscription = dict(_locations(_server_by_listen(text, "443")))["^~ /s/"]
    assert "access_log off;" in subscription


def _launch_files() -> list[Path]:
    """Файлы Compose, которые стенд накладывает, — из команды запуска в шапке базового файла
    (единственное место в репозитории, где команда объявлена рядом с файлом); каждый ``-f`` —
    путь от корня репозитория. ``-f`` перекрывает ``COMPOSE_FILE`` из ``.env``, и .env.example
    его не задаёт."""
    header = (COMPOSE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    launch = re.compile(r"-f\s+(deploy/compose/\S+\.ya?ml)")
    listed = launch.findall(header)
    files = [REPO / path for path in listed]
    assert files and files[0] == COMPOSE_DIR / "docker-compose.yml", files
    assert "COMPOSE_FILE" not in (COMPOSE_DIR / ".env.example").read_text(encoding="utf-8")
    # Та же команда в инструкции оператора: расхождение означало бы, что стенд накладывает не
    # те файлы, которые читает страж.
    readme = (REPO / "deploy" / "README.md").read_text(encoding="utf-8")
    assert launch.findall(readme) == listed, "deploy/README.md и шапка compose расходятся"
    return files


def _compose_files() -> tuple[Path, list[Path]]:
    """Базовый файл и оверлеи стенда; любой другой файл YAML в каталоге (``compose.yaml``,
    ``*.override.*``, ``docker-compose.prod.yml``) — отказ: Compose подхватил бы его сам или
    третьим ``-f``, а страж его не прочитал бы. ``include:`` и ``extends:`` подмешивают чужие
    файлы мимо разбора — запрещены в каждом файле."""
    files = _launch_files()
    stray = sorted(p.name for p in COMPOSE_DIR.glob("*.y*ml") if p not in files)
    assert stray == [], ("файл Compose вне команды запуска", stray)
    for path in files:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict) and "include" not in document, (path.name, "include")
        for name, service in document.get("services", {}).items():
            assert isinstance(service, dict) and "extends" not in service, (path.name, name)
    return files[0], files[1:]


def _service(document: object, name: str) -> dict[str, Any]:
    services = document.get("services", {}) if isinstance(document, dict) else {}
    service = services.get(name, {})
    assert isinstance(service, dict), (name, service)
    return service


def test_the_containers_that_hold_bodies_have_a_memory_ceiling_and_no_swap() -> None:
    """Потолок памяти превращает перерасход в OOM одного контейнера, а не в голодание соседей
    на хосте; swap не выдаётся — страницы с адресами пользователей (§16) на диск хоста не
    уезжают. Тела держат оба контейнера: nginx — в буферах заливки и во временных файлах тел,
    api — при разборе; оба ограничены в базовом файле, оверлей их не переопределяет ни
    `mem_limit`, ни `deploy.resources`. Временные файлы nginx (тела публичного server больше
    буфера; на Node API их нет по конструкции) лежат на tmpfs с пределом под тем же потолком —
    оба пути временных файлов из nginx.conf лежат под точкой монтирования, размер tmpfs читается
    из файла и меньше потолка, оверлей тома nginx не трогает. Процессор и число процессов тоже
    ограничены — анонимные рукопожатия публичного server иначе морят api и PostgreSQL на том же
    хосте. Файлы разбираются как YAML (якоря, слияния и потоковая запись не обходят проверку),
    а не по тексту."""
    base_file, overlays = _compose_files()
    base = yaml.safe_load(base_file.read_text(encoding="utf-8"))
    for name in ("api", "nginx"):
        service = _service(base, name)
        assert service.get("mem_limit") == service.get("memswap_limit"), name
        assert _bytes(str(service["mem_limit"]).lower()) >= 256 * 1024**2, name
        assert "deploy" not in service, (name, "предел задаётся mem_limit, не deploy.resources")
        for overlay in overlays:
            over = _service(yaml.safe_load(overlay.read_text(encoding="utf-8")), name)
            assert not {"mem_limit", "memswap_limit", "deploy"} & set(over), (overlay.name, name)
    assert _service(base, "api")["mem_limit"] == "1g"
    nginx = _service(base, "nginx")
    assert nginx["mem_limit"] == "256m"
    config = _config()
    temp_paths = {
        name: re.findall(rf"{name}\s+(\S+);", config)
        for name in ("client_body_temp_path", "proxy_temp_path")
    }
    assert all(len(paths) == 1 for paths in temp_paths.values()), temp_paths
    volumes = nginx.get("volumes", [])
    tmpfs = [v for v in volumes if isinstance(v, dict) and v.get("type") == "tmpfs"]
    assert [v["target"] for v in tmpfs] == ["/var/cache/nginx"], "временные файлы — tmpfs"
    mount = tmpfs[0]["target"]
    for name, paths in temp_paths.items():
        assert paths[0].startswith(mount + "/"), (name, paths, "путь под точкой монтирования")
    size = str(tmpfs[0]["tmpfs"]["size"]).lower()
    assert size == "64m", tmpfs[0]
    assert _bytes(size) < _bytes(str(nginx["mem_limit"])), "tmpfs считается памятью контейнера"
    # Точка монтирования принадлежит root, а пишет в подкаталоги рабочий процесс nginx: ему
    # нужен проход (x для остальных) без чтения списка; 0700 раунда 7 давал Permission denied
    # и 500 публичному POST — стенд. Проверяется смысл битов, не литерал.
    mode = tmpfs[0]["tmpfs"]["mode"]
    assert mode & 0o001, ("рабочий процесс nginx проходит в каталог", oct(mode))
    assert not mode & 0o066, ("каталог не читают и не пишут посторонние", oct(mode))
    assert mode & 0o700 == 0o700, oct(mode)
    # Временный файл ответа не больше proxy_max_temp_file_size; tmpfs вмещает восемь таких —
    # столько соединений на один сертификат (agent_conn 8), не потолок парка; каталог общий с
    # телами публичного server (разделение и размер под парк — 001.66/001.75).
    per_response = _bytes(re.findall(r"proxy_max_temp_file_size\s+(\S+);", config)[0])
    assert 8 * per_response <= _bytes(size), (per_response, size)
    for name, cpus, pids in (("nginx", "1.0", 256), ("api", "2.0", 512)):
        service = _service(base, name)
        assert str(service.get("cpus")) == cpus, (name, "потолок процессора")
        assert service.get("pids_limit") == pids, (name, "потолок числа процессов")
    workers = re.findall(r"worker_processes\s+(\S+);", config)
    assert workers == [str(int(float(nginx["cpus"])))], (workers, "worker'ов — по квоте cpus")
    for overlay in overlays:
        document = yaml.safe_load(overlay.read_text(encoding="utf-8"))
        over = _service(document, "nginx")
        assert "volumes" not in over, (overlay.name, "оверлей не переопределяет тома nginx")
        for name in ("api", "nginx"):
            assert not {"cpus", "pids_limit"} & set(_service(document, name)), (overlay.name, name)


def test_container_logs_are_bounded() -> None:
    """Журналы json-file без предела растут, пока не кончится диск хоста, а строки журнала
    дешевле всего генерирует анонимный клиент (enrollment- и публичный server): у каждой службы
    стенда предел размера и числа файлов журнала."""
    base_file, overlays = _compose_files()
    base = yaml.safe_load(base_file.read_text(encoding="utf-8"))
    for name, service in base["services"].items():
        logging = service.get("logging", {})
        assert logging.get("driver") == "json-file", (name, "драйвер с пределами")
        options = logging.get("options", {})
        assert options.get("max-size") == "10m" and options.get("max-file") == "3", name
    # Оверлей, переопределивший `logging` (другой драйвер, без options), снял бы предел молча.
    for overlay in overlays:
        document = yaml.safe_load(overlay.read_text(encoding="utf-8"))
        for name, service in document.get("services", {}).items():
            assert "logging" not in service, (
                overlay.name,
                name,
                "предел журнала — в базовом файле",
            )


def test_the_reports_body_limit_is_derived_from_the_model_limits() -> None:
    """Предел `location` отчётов — не число из головы: самое большое тело, которое приложение
    принимает, обязано пройти через прокси, а предел не вправе быть больше этого тела плюс
    полоса выброшенного разбора. Тело строится из самих пределов модели и самой широкой формы
    каждого поля, которую модель принимает (`tests/_reports.py::widest_report`); наборы полей
    закреплены равенством: поле, добавленное в модель (001.39 — `conn_stats[]`), обязано
    пересчитать и это тело, и предел. Запись — `json.dumps` с пробелами после разделителей:
    самая широкая из тех, что README называет компактной (без отступов; экранирование ASCII
    `\\uXXXX` в контракт не входит — README). Размер закреплён числом: тем же телом, байт в байт,
    меряет стенд."""
    from app.accounting.canonical import TIMESTAMP_MAX_CHARS
    from app.accounting.service import (
        REPORT_MAX_LINES,
        REPORT_MAX_ONLINE_IPS,
        OnlineIp,
        ReportIn,
        ReportLine,
    )

    from tests._reports import STAMP, widest_report

    assert (REPORT_MAX_LINES, REPORT_MAX_ONLINE_IPS, TIMESTAMP_MAX_CHARS) == (500, 2_500, 32)
    assert set(ReportIn.model_fields) == {
        "node_id",
        "counter_epoch",
        "report_seq",
        "parts_total",
        "period_start",
        "period_end",
        "lines",
        "online_ips",
        "node_rx_bytes",
        "node_tx_bytes",
    }
    assert set(ReportLine.model_fields) == {"user_id", "uplink_bytes", "downlink_bytes"}
    assert set(OnlineIp.model_fields) == {"user_id", "ip", "last_seen"}
    assert len(STAMP) == TIMESTAMP_MAX_CHARS
    widest = widest_report()
    assert all(len(row["ip"]) == 45 for row in widest["online_ips"]), "самая длинная запись IPv6"
    body = json.dumps(widest).encode()
    parsed = ReportIn.model_validate_json(body)
    assert (len(parsed.lines), len(parsed.online_ips)) == (REPORT_MAX_LINES, REPORT_MAX_ONLINE_IPS)
    assert len(body) == 457_369, "число стенда: тело там строится теми же правилами"

    text = _config()
    reports = _reports_location(_server_by_listen(text, "8443"))
    limit = _bytes(BODY_LIMIT.findall(reports)[0])
    assert len(body) <= limit <= len(body) + DISCARD_BAND, (len(body), limit)
