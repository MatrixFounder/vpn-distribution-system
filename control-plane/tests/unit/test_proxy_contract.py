"""Статический контракт границы nginx: заголовки прокси (задача 001.10, ревью раунда 2),
разделение серверов Node API (задача 001.24, ревью раунда 1), пределы тела, одновременности и
частоты агентского и enrollment-server, бюджет Н-4 и потолки памяти контейнеров (001.28, 001.33).

uvicorn с ``--forwarded-allow-ips '*'`` берёт крайний левый элемент ``X-Forwarded-For``; значит
nginx обязан перезаписывать заголовок адресом соединения (``$remote_addr``), а не дополнять его
(``$proxy_add_x_forwarded_for``) — иначе клиент подставляет себе любой адрес. Enrollment
(``POST /agent/v1/enroll``) — единственный маршрут ``/agent/v1`` без клиентского сертификата
(security.md §7.1), поэтому он обслуживается отдельным ``server`` и обязан быть недоступен на
агентском (mTLS) и публичном. Файлы сверяются статически (сеть в тесте не поднимается);
поведение на стенде — в отчётах задач. Разбор конфигурации — словами, как их читает nginx
(``_nginx_tokens``): стражи видят каноническую запись файла, где кавычки сняты везде, где они не
нужны nginx, — ``set "$x" 0`` для стража то же, что ``set $x 0`` (роаст 001.25, раунд 8), —
комментарии сняты по правилам nginx, содержимое строк в кавычках не образует блоков и не
обрывает их, матчеры ``location`` уникальны, ``include`` кроме таблицы MIME запрещён — иначе
страж читал бы не тот файл или не тот блок.
Стражи здесь положительные: каждая проксирующая операция Node API обязана нести свои зоны, тела
без известной длины — получать 411, буферы — равняться пределам, а потолки Compose — читаться из
файлов, которые стенд действительно накладывает (команда запуска в шапке базового файла и в
``deploy/README.md`` — одна и та же).
"""

from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

import click
import pytest
import uvicorn.main  # noqa: F401 - команда click uvicorn для разбора строк запуска
import yaml  # type: ignore[import-untyped]

REPO = Path(__file__).resolve().parents[3]
NGINX_CONF = REPO / "deploy" / "nginx" / "nginx.conf"
ENTRYPOINT = REPO / "control-plane" / "docker-entrypoint.sh"
DOCKERFILE = REPO / "control-plane" / "Dockerfile"
COMPOSE_DIR = REPO / "deploy" / "compose"


_ESCAPES = {'"': '"', "'": "'", "\\": "\\", "t": "\t", "r": "\r", "n": "\n"}
# Слово, которому кавычки не нужны: ни пробела, ни знаков, которые разбор nginx читает иначе.
_SIMPLE_WORD = re.compile(r"[^\s;{}#'\"\\]+")


def _unescape(body: str) -> str:
    """Значение слова по правилу nginx: ``\\"``, ``\\'``, ``\\\\``, ``\\t``, ``\\r``, ``\\n``
    разбираются, прочая обратная косая черта остаётся как есть."""
    out, i = [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body) and body[i + 1] in _ESCAPES:
            out.append(_ESCAPES[body[i + 1]])
            i += 2
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def _nginx_tokens(text: str) -> list[tuple[str, str, str]]:
    """Токены файла так, как их читает nginx (``ngx_conf_read_token``): тройки (вид, значение,
    запись в файле); вид — ``word``, ``;``, ``{`` или ``}``. ``#`` начинает комментарий только в
    начале слова (в середине слова — часть значения), кавычка открывает строку тоже только в
    начале слова, ``{`` сразу после ``$`` блока не открывает и признак переменной не сбрасывает
    (``${{a}`` — одно слово), ``}`` внутри слова его не обрывает, обратная косая черта делает
    следующий знак частью слова; за закрывающей кавычкой — пробел, ``;``, ``{`` или ``)``, иначе
    nginx файл не примет, и страж его не читает (роаст 001.25, раунд 9). Регулярное выражение
    по тексту видело бы другое: ``set "$x" 0`` — не ``set $x``, а ``a#b`` — комментарий."""
    tokens: list[tuple[str, str, str]] = []
    i, size = 0, len(text)
    while i < size:
        char = text[i]
        if char in " \t\r\n":
            i += 1
            continue
        if char == "#":
            end = text.find("\n", i)
            i = size if end < 0 else end
            continue
        if char in ";{}":
            tokens.append((char, char, char))
            i += 1
            continue
        start = i
        if char in ("'", '"'):
            i += 1
            while i < size and text[i] != char:
                i += 2 if text[i] == "\\" else 1
            assert i < size, "строка в кавычках не закрыта"
            i += 1
            assert i >= size or text[i] in " \t\r\n;{)", "за кавычкой — не пробел, nginx откажет"
            raw = text[start:i]
            tokens.append(("word", _unescape(raw[1:-1]), raw))
            continue
        variable = False
        while i < size:
            current = text[i]
            if current == "\\":
                i += 2
                variable = False
                continue
            if current == "{" and variable:
                i += 1  # как у nginx: признак переменной остаётся
                continue
            if current in " \t\r\n;{":
                break
            variable = current == "$"
            i += 1
        raw = text[start:i]
        tokens.append(("word", _unescape(raw), raw))
    return tokens


def _canonical(text: str) -> str:
    """Каноническая запись конфигурации: директива на строке, отступ по глубине, слова через
    пробел; слово, которому кавычки не нужны, — без них, остальные — как в файле. Комментарии
    сняты. Стражи сверяют этот текст, а не исходный."""
    lines: list[str] = []
    words: list[str] = []
    depth = 0
    for kind, value, raw in _nginx_tokens(text):
        if kind == "word":
            words.append(value if _SIMPLE_WORD.fullmatch(value) else raw)
        elif kind == ";":
            lines.append("    " * depth + " ".join(words) + ";")
            words = []
        elif kind == "{":
            lines.append("    " * depth + " ".join(words) + " {")
            words = []
            depth += 1
        else:
            assert not words and depth > 0, "} посреди директивы или лишняя"
            depth -= 1
            lines.append("    " * depth + "}")
    assert not words and depth == 0, "директива или блок не закрыты"
    return "\n".join(lines) + "\n"


class Directive(NamedTuple):
    """Директива дерева конфигурации: значения слов (имя и аргументы) и вложенный блок."""

    words: tuple[str, ...]
    block: tuple[Directive, ...] | None


def _tree(text: str) -> tuple[Directive, ...]:
    """Дерево директив файла по токенам nginx."""
    stack: list[list[Directive]] = [[]]
    heads: list[tuple[str, ...]] = []
    words: list[str] = []
    for kind, value, _raw in _nginx_tokens(text):
        if kind == "word":
            words.append(value)
        elif kind == ";":
            stack[-1].append(Directive(tuple(words), None))
            words = []
        elif kind == "{":
            heads.append(tuple(words))
            stack.append([])
            words = []
        else:
            body = stack.pop()
            stack[-1].append(Directive(heads.pop(), tuple(body)))
    assert len(stack) == 1 and not words, "директива или блок не закрыты"
    return tuple(stack[0])


def _walk(
    tree: tuple[Directive, ...], context: tuple[tuple[str, ...], ...] = ()
) -> list[tuple[tuple[tuple[str, ...], ...], Directive]]:
    """Все директивы дерева с цепочкой заголовков блоков, внутри которых они стоят."""
    found: list[tuple[tuple[tuple[str, ...], ...], Directive]] = []
    for directive in tree:
        found.append((context, directive))
        if directive.block is not None:
            found += _walk(directive.block, (*context, directive.words))
    return found


def _nginx_tree() -> tuple[Directive, ...]:
    return _tree(NGINX_CONF.read_text(encoding="utf-8"))


def _server_tree(tree: tuple[Directive, ...], port: str) -> tuple[Directive, ...]:
    """Блок единственного server, слушающего порт (в любой записи адреса)."""
    (http,) = [d for d in tree if d.words == ("http",)]
    assert http.block is not None
    listen = re.compile(rf"(?:[\w.:\[\]*]*:)?{port}")
    found = [
        d.block
        for d in http.block
        if d.words == ("server",)
        and d.block is not None
        and any(e.words[:1] == ("listen",) and listen.fullmatch(e.words[1]) for e in d.block)
    ]
    assert len(found) == 1, f"ровно один server слушает {port}"
    return found[0]


def _mask_strings(text: str) -> str:
    """Тот же текст той же длины, где содержимое строк в кавычках заменено пробелами: слово
    ``server`` и скобки внутри значения (``add_header X "server {"``, регулярный матчер
    ``location ~ "^/x\\{2\\}$"``) не образуют блоков и не обрывают их. Строку открывает кавычка
    только в начале слова, как у nginx: кавычка в середине слова (``a"``) — буква значения, и
    маска по ней спрятала бы от стражей всё до следующей кавычки — например, location со своим
    ``proxy_set_header`` (роаст 001.25, раунд 9). Позиции, найденные по маске, применяются к
    исходному тексту."""
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
            if char in ("'", '"') and (i == 0 or text[i - 1] in " \t\r\n;{}"):
                quote = char
            out.append(char)
        i += 1
    return "".join(out)


def _config() -> str:
    return _canonical(NGINX_CONF.read_text(encoding="utf-8"))


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
    в порядке объявления. Источник и переменная — литералами, как в канонической записи
    (``_canonical``: без кавычек, если они не нужны)."""
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
    # Разбор словами, как nginx: кавычки, которые не нужны, снимаются (`set "$x" 0` — тот же
    # `set $x 0`), нужные остаются; `#` в середине слова — не комментарий; `${x}` блока не
    # открывает; `}` внутри слова его не обрывает; экранирования — по правилу nginx.
    assert _canonical("set \"$Uri_Has_Control\" '0'; # c\nset $a b#c;") == (
        "set $Uri_Has_Control 0;\nset $a b#c;\n"
    )
    assert _canonical('add_header X "a b"; return 200 "ok\\n"; set $b ${a}x}; set $c "";') == (
        'add_header X "a b";\nreturn 200 "ok\\n";\nset $b ${a}x};\nset $c "";\n'
    )
    assert _tree('if ($x ~ "^a b$") { return 404; }') == (
        Directive(("if", "($x", "~", "^a b$", ")"), (Directive(("return", "404"), None),)),
    )
    assert _tree('map "$a" "$B" { "x\\"y" 1; }')[0].words == ("map", "$a", "$B")
    # `${` признак переменной не сбрасывает — `${{a}` одно слово, как у nginx; за закрывающей
    # кавычкой nginx требует пробел, `;`, `{` или `)`; кавычка в середине слова — буква, и маска
    # строк по ней location не прячет (роаст 001.25, раунд 9).
    assert [value for _, value, _ in _nginx_tokens("set $x ${{a};")] == ["set", "$x", "${{a}", ";"]
    with pytest.raises(AssertionError, match="за кавычкой"):
        _nginx_tokens('add_header X "a"b;')
    assert [value for _, value, _ in _nginx_tokens('if ($x = "a") {')][-3:] == ["a", ")", "{"]
    hidden = 'add_header X-A a"; location = /x { proxy_pass $api; } add_header X-B b";'
    assert [matcher for matcher, _ in _locations(hidden)] == ["= /x"], "кавычка в слове — буква"


def test_nginx_overwrites_forwarded_headers() -> None:
    """Во всём файле каждое вхождение X-Forwarded-For / X-Real-IP — только $remote_addr, каждое
    X-Forwarded-Proto — $scheme или https, дополнения $proxy_add_x_forwarded_for нет; в каждом
    из трёх server, проксирующих на api, полный набор задан на уровне server, а внутри location
    нет ни одного proxy_set_header — одна такая директива в location отменяет весь набор уровня
    server (наследование nginx), включая обнуление X-Client-Cert."""
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


# Директивы, которые подменяют адрес клиента ($remote_addr) значением заголовка или PROXY.
REAL_IP_DIRECTIVES = {"set_real_ip_from", "real_ip_header", "real_ip_recursive", "proxy_protocol"}


def test_the_client_address_is_the_connection_peer() -> None:
    """``$remote_addr`` — адрес TCP-соединения: на нём стоят ``X-Forwarded-For`` и ``X-Real-IP``
    для апстрима (``enrolled_from`` identity, который администратор сверяет перед подтверждением
    ноды, — UC-01 шаг 6; адрес в пределах приложения) и зоны enrollment по
    ``$binary_remote_addr``. Модуль realip (``set_real_ip_from``, ``real_ip_header``,
    ``real_ip_recursive``) и ``proxy_protocol`` в ``listen`` подменили бы его значением
    заголовка клиента или заголовка PROXY: держатель утёкшего bootstrap-токена обменял бы его
    откуда угодно с адресом VPS ноды в заголовке, а пределы по адресу стали бы пределами по
    значению заголовка (роаст 001.25, раунд 8). Переменных адреса из заголовков в файле нет.
    Балансировщик перед прокси (001.66) — со своим списком доверенных адресов и своим стражем."""
    for context, directive in _walk(_nginx_tree()):
        assert directive.words[0] not in REAL_IP_DIRECTIVES, (context, directive.words)
        if directive.words[0] == "listen":
            assert "proxy_protocol" not in directive.words[1:], (context, directive.words)
    text = _config()
    for variable in (
        "$proxy_protocol_addr",
        "$realip_remote_addr",
        "$http_x_forwarded_for",
        "$http_x_real_ip",
        "$http_forwarded",
    ):
        assert variable not in text, variable


# Другие серверы приложения: запуск ими прошёл бы мимо разбора строк uvicorn ниже.
OTHER_APP_SERVERS = re.compile(r"\b(?:gunicorn|hypercorn|granian|daphne|waitress)\b")


def _uvicorn_launch_lines() -> tuple[str, list[str]]:
    """Точка входа с продолжениями строк (обратная косая черта), склеенными в одну, и каждая её
    строка, где стоит слово ``uvicorn`` (кроме комментариев): новая ветка запуска с другим
    порядком аргументов из разбора не выпадает (роаст 001.25, раунд 9)."""
    text = ENTRYPOINT.read_text(encoding="utf-8").replace("\\\n", " ")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if OTHER_APP_SERVERS.search(line)], "другой сервер"
    lines = [line for line in code if re.search(r"\buvicorn\b", line)]
    assert len(lines) == 2, ("ветки --reload и --workers", lines)
    return text, lines


def _uvicorn_launches() -> list[dict[str, Any]]:
    """Опции каждой строки запуска api так, как их прочтёт uvicorn: разбор его собственной
    командой click (последнее вхождение опции побеждает, записи ``--opt value`` и ``--opt=value``,
    пары флагов ``--x``/``--no-x``), без переменных окружения и без приведения типов."""
    command = sys.modules["uvicorn.main"].main
    launches = []
    for line in _uvicorn_launch_lines()[1]:
        words = shlex.split(line)
        argv = words[words.index("uvicorn") + 1 :]
        opts, rest, _order = command.make_parser(click.Context(command)).parse_args(args=argv)
        assert rest == [], (line, rest)
        assert opts.get("app") == "app.main:create_app" and opts.get("factory") is True, opts
        launches.append(opts)
    return launches


def test_uvicorn_trusts_only_overwritten_headers() -> None:
    """Обе ветки запуска api передают --proxy-headers --forwarded-allow-ips '*' (заголовки
    принимаются только потому, что nginx их перезаписывает — см. тест выше)."""
    for opts in _uvicorn_launches():
        assert opts.get("proxy_headers") is True, opts
        assert opts.get("forwarded_allow_ips") == "*", opts


# Ключи окружения образа (ENV Dockerfile): ни одного UVICORN_*.
IMAGE_ENV = {
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    "PIP_NO_CACHE_DIR",
}


def test_uvicorn_writes_no_access_log() -> None:
    """Н-25: access-log uvicorn пишет строку запроса целиком — ``GET /s/<токен>`` ложился в журнал
    контейнера api (стенд, роаст 001.25, раунд 6). Журнал запросов ведёт nginx, где путь /s/
    исключён. Строка запуска — единственный источник конфигурации uvicorn: в обеих ветках
    последнее слово за ``--no-access-log``, ``--ws none`` (строки рукопожатия WebSocket
    uvicorn пишет с путём мимо access-log — раунд 7) и ``--log-level info`` (уровень ``trace``
    включает журнал сообщений ASGI, ``Started scope=…`` с путём); своей конфигурации журналов
    (``--log-config``) и файла окружения (``--env-file``) нет. Строка разбирается командой
    click самого uvicorn — ``--ws none --ws=websockets`` и ``--log-config=…`` видны так, как
    их прочтёт он. Опции, которых в строке нет, uvicorn взял бы из ``UVICORN_*`` окружения
    (``auto_envvar_prefix``), а ``.env`` оператора роли приложения получают целиком: точка
    входа снимает все ``UVICORN_*`` до запуска, в Compose, ``.env.example`` и образе их нет, а
    ``CMD`` образа (точка входа исполнила бы его вместо роли) не задан (роаст 001.25, раунд 8).
    ``--lifespan on``: пару CA проверяет lifespan приложения, и с ``off`` api стартовал бы
    здоровым с негодным CA; инструкции ``Dockerfile`` — без учёта регистра, как их читает
    Docker (``cmd`` — тот же ``CMD``; раунд 9)."""
    for opts in _uvicorn_launches():
        assert opts.get("access_log") is False, opts
        assert opts.get("ws") == "none", opts
        assert opts.get("log_level") == "info", opts
        assert opts.get("lifespan") == "on", opts
        assert opts.get("log_config") is None and opts.get("env_file") is None, opts
    joined, launch_lines = _uvicorn_launch_lines()
    strip = [line for line in joined.splitlines() if "unset" in line and "UVICORN_" in line]
    assert len(strip) == 1, strip
    assert all(joined.index(strip[0]) < joined.index(line) for line in launch_lines), (
        "снять до запуска"
    )
    shown = subprocess.run(  # noqa: S603 - строка самой точки входа
        ["/bin/sh", "-c", f"{strip[0].strip()}\nenv"],
        env={"PATH": "/usr/bin:/bin", "UVICORN_LOG_LEVEL": "trace", "UVICORN_WS": "auto"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "UVICORN_" not in shown and "PATH=" in shown, shown
    compose, overlays = _compose_files()
    for path in (compose, *overlays):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, service in (document.get("services") or {}).items():
            environment = service.get("environment") or {}
            keys = (
                list(environment)
                if isinstance(environment, dict)
                else [entry.split("=", 1)[0] for entry in environment]
            )
            assert not [key for key in keys if key.upper().startswith("UVICORN_")], (path, name)
    example = (COMPOSE_DIR / ".env.example").read_text(encoding="utf-8")
    active = [line for line in example.splitlines() if line.strip() and not line.startswith("#")]
    assert not [line for line in active if line.upper().startswith("UVICORN_")], active
    dockerfile = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    instructions = [line.split(None, 1) for line in dockerfile.splitlines() if line.strip()]
    instructions = [
        [words[0].upper(), *words[1:]] for words in instructions if not words[0].startswith("#")
    ]
    assert [w[1].strip() for w in instructions if w[0] == "ENTRYPOINT"] == [
        '["docker-entrypoint.sh"]'
    ]
    assert not [w for w in instructions if w[0] == "CMD"], "CMD исполнился бы вместо роли"
    env_keys = {
        pair.split("=", 1)[0]
        for words in instructions
        if words[0] == "ENV"
        for pair in words[1].split()
    }
    assert env_keys == IMAGE_ENV, env_keys


def test_the_certificate_header_is_set_by_the_proxy_and_never_by_the_client() -> None:
    """Единственное, что делает ``X-Client-Cert`` пригодным как признак identity, — три
    директивы в этом файле: агентский ``server`` перезаписывает его сертификатом, который сам
    проверил по CA (``$ssl_client_escaped_cert``), публичный и enrollment обнуляют. Снять или
    подменить любую — и заголовок становится клиентским, то есть держатель любого сертификата
    CA (или вовсе без него — через публичный server) объявляет себя любой нодой (§7.1,
    `app/agent_api/deps.py::presented_identity`). Прежний заголовок ``X-Client-Fingerprint``
    (SHA-1) приложением не читается с 001.25 и не передаётся ни на одном server: оставшийся, он
    выглядел бы признаком identity, которым не является."""
    text = _config()
    for header in ("$http_x_client_cert", "$http_x_client_fingerprint"):
        assert header not in text, (header, "значение от клиента не уходит в апстрим")
    expected = {"8443": "$ssl_client_escaped_cert", "443": '""', "8444": '""'}
    for port, value in expected.items():
        block = _server_by_listen(text, port)
        found = re.findall(r"proxy_set_header\s+X-Client-Cert\s+(\S+);", block)
        assert found == [value], (port, found)
        assert "X-Client-Fingerprint" not in block, port
    # Отпечаток осмыслен ровно настолько, насколько доверен якорь, по которому прокси проверяет
    # сертификат: подменённый ``ssl_client_certificate`` оставил бы всю схему на месте, только
    # доверять она стала бы другому CA. Глубина 0 — только лист, подписанный этим CA напрямую: в
    # OpenSSL глубина считает промежуточных CA, и уже 1 пропускала бы одного — нода с листом,
    # способным подписывать, выпускала бы себе «сиблингов» и множила серийные номера, ключи всех
    # зон по сертификату (роаст 001.25, раунд 3: в раундах 1–2 здесь стояла 1).
    agent = _server_by_listen(text, "8443")
    assert re.findall(r"ssl_client_certificate\s+(\S+);", agent) == ["/etc/nginx/certs/ca.crt"]
    assert re.findall(r"ssl_verify_client\s+(\S+);", agent) == ["on"], "не optional"
    assert re.findall(r"ssl_verify_depth\s+(\d+);", agent) == ["0"]
    # Якорь проверки клиента задают не только ``ssl_client_certificate``: nginx проверяет
    # клиентский сертификат и по ``ssl_trusted_certificate`` (уровень http наследуется), OpenSSL —
    # и по ``ssl_conf_command VerifyCAFile``. Типовой фрагмент OCSP stapling публичного server
    # (001.66) на уровне http молча добавил бы якорю агентского порта чужую пачку CA (роаст 001.25,
    # раунд 4) — поэтому ни того, ни другого нет ни на уровне http, ни на агентском server.
    for scope, block in (("http", _without_blocks(text, "server")), ("8443", agent)):
        assert "ssl_trusted_certificate" not in block, (scope, "второй якорь клиента")
        assert "ssl_conf_command" not in block, (scope, "VerifyCAFile — второй якорь клиента")


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
            # Всё прочее закрыто: 404, служебные location и именованные location отказов прокси
            # — 411, 413, 429 и 503 без апстрима.
            assert "proxy_pass" not in body and re.search(
                r"return\s+(404|411|413|429|503)\b|try_files\s+\S+\s+=(404|411|413);", body
            ), (
                matcher,
                body,
            )

    agent_enroll = [b for m, b in _locations(agent) if m == f"= {ENROLL_PATH}"]
    assert len(agent_enroll) == 1 and "try_files /nonexistent =404;" in agent_enroll[0], (
        "агентский server обязан отдавать 404 на enrollment — через try_files, после пределов"
    )
    assert "return" not in agent_enroll[0], "return исполняется до пределов (роаст 001.25, раунд 3)"
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
# Служебные пути отказов после пределов (роаст 001.25, раунды 3–4): тело без длины и тело длиннее
# предела location переводятся сюда `rewrite` в фазе SERVER_REWRITE, решение даёт try_files.
LENGTH_REQUIRED_PATH = "/__length_required"
TOO_LARGE_PATH = "/__too_large"
# Путь с управляющими символами уходит сюда первым правилом server Node API — в служебный
# location 404 без предела тела (404 через try_files, после пределов): с пределом тело длиннее
# 64k отвергла бы FIND_CONFIG до пределов (стенд, раунд 6: 40 × 413 без 429).
NOT_FOUND_PATH = "/__not_found"
# Управляющие символы (C0 и DEL) в декодированном пути — входы для карты $uri_has_control.
CONTROL_CHARACTERS = [chr(code) for code in (*range(0x20), 0x7F)]
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
        # Служебные location 413 и 404 снимают предел ради своего назначения — их тела сверены
        # равенством стражами 413 и отказов после пределов.
        if matcher not in (f"= {REPORTS_PATH}", f"= {TOO_LARGE_PATH}", f"= {NOT_FOUND_PATH}"):
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
    знает) оплачивался бы приложением без ограничений. Зоны — по серийному номеру клиентского
    сертификата (признак, который прокси знает без базы; не отпечаток — у податливой подписи
    ECDSA держатель листа получает близнеца с другим отпечатком и тем же серийным номером,
    роаст 001.25, ``test_every_leaf_has_a_twin_that_differs_only_in_its_bytes``) и общие на парк с
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
        ("$ssl_client_serial", "agent_conn", "1m"),
        ("$ssl_client_serial", "agent_report_conn", "1m"),
        ("agent", "agent_report_total", "1m"),
        ("$binary_remote_addr", "enroll_conn_addr", "1m"),
    ], zones
    assert re.findall(r"limit_req_zone\s+(\S+)\s+zone=(\w+):\w+\s+rate=(\S+);", http_level) == [
        ("$ssl_client_serial", "agent_report_rate", "2r/s"),
        ("$ssl_client_serial", "agent_rate", "5r/s"),
        ("agent", "agent_fleet_rate", "15r/s"),
        ("$binary_remote_addr", "enroll_addr_rate", "1r/s"),
        ("enroll", "enroll_fleet_rate", "2r/s"),
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
        "8443": {"$ssl_client_serial", "agent"},
        "8444": {"$binary_remote_addr", "enroll"},
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
    # Зоны — только на своём server: по серийному номеру сертификата на server без mTLS они пусты и
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
# операций, то же на enrollment-server — `MEASURED_ENROLL_JUNK_P95_MS` (60 замеров, путь отказа по
# форме; в раундах 6–7 без проверки формы те же тела стоили 13…24 мс, и частоту парка резали до
# 8 r/s). Enrollment с 001.25 настоящий — отказ по токену (запрос в базу), приём (транзакция и
# подпись листа), приём CSR на пределе `CSR_MAX_CHARS` и отказ CA с годным токеном (CSR на пределе
# с ключом RSA-16384). Консервативная оценка занятости цикла событий одной такой операцией —
# стенка запроса в процессе api стенда (`MEASURED_ENROLL_WALL_P95_MS`, вызов ASGI без сети,
# `tests/stand/enrollment_cpu.py`: работа приложения вместе с ожиданием базы, которое цикл не
# занимает) плюс накладные HTTP и прокси, которых вызов в процессе не несёт, — их оценивает `urt`
# контрольного пути на тихом стенде (`MEASURED_ENROLL_JUNK_P95_MS`: отказ по форме, код не менялся
# с 001.33, тело 64 КиБ — шире любого тела обмена). Это сумма p95 двух разных прогонов — оценка, а
# не граница (p95 суммы она не ограничивает; роаст 001.25, раунд 3). `urt` самих путей через nginx
# на VM сегодня меряет среду (контроль той же серии — 10…35 мс против 2): p95 каждого пути,
# приведённый контролем своей серии (`p95 × MEASURED_ENROLL_JUNK_P95_MS / p95 контроля`,
# `MEASURED_ENROLL_URT_P95_MS`, `tests/stand/enrollment_timing.py` — он повторяет режимы порядка и
# выдачи токенов раунда 1), — сверка: при аддитивном фоне приведение занижает дорогой путь (роаст
# 001.25, раунд 2), поэтому она может только поднять константу. Константа — наибольшее из оценки и
# приведённых значений с округлением вверх; процессорное время (`MEASURED_ENROLL_CPU_P95_MS`) не
# больше стенки ни на одном пути.
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
# p95 `urt` через nginx стенда, мс (001.25): контроль `junk` и пути той же серии.
MEASURED_ENROLL_URT_P95_MS: dict[str, dict[str, float]] = {
    "раунд 1, подряд": {"junk": 35, "token": 47, "accept": 24},
    "раунд 1, по кругу": {"junk": 35, "token": 67, "accept": 25},
    "раунд 1, по кругу, токены заранее": {"junk": 33, "token": 53, "accept": 98},
    "раунд 2, по кругу, пять путей": {
        "junk": 10,
        "token": 21,
        "accept": 57,
        "accept_wide": 36,
        "refusal_wide": 19,
    },
}
# Запрос в процессе api стенда, p95, мс: процессорное время и стенка — замер на коде раунда 3
# 001.25 (отметка аннулирования, условное погашение, серийный номер листа).
MEASURED_ENROLL_CPU_P95_MS = {
    "junk": 0.39,
    "token": 1.13,
    "accept": 1.96,
    "accept_wide": 1.90,
    "refusal_wide": 1.52,
}
MEASURED_ENROLL_WALL_P95_MS = {
    "junk": 0.39,
    "token": 1.41,
    "accept": 3.56,
    "accept_wide": 2.65,
    "refusal_wide": 1.91,
}
H4_OTHER_OPERATION_MS = 4
H4_ENROLL_OPERATION_MS = 12
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
    # Enrollment: консервативная оценка — стенка в процессе самого дорогого пути плюс накладные
    # HTTP и прокси (тихий `urt` контроля); приведённый `urt` серий — сверка, которая может только
    # поднять константу (001.33 требовала: дорогой путь — в гейт).
    measured_paths = {path for paths in MEASURED_ENROLL_URT_P95_MS.values() for path in paths}
    assert measured_paths == set(MEASURED_ENROLL_WALL_P95_MS) == set(MEASURED_ENROLL_CPU_P95_MS), (
        "каждый путь мерен и через nginx, и в процессе"
    )
    assert all(
        MEASURED_ENROLL_CPU_P95_MS[path] <= MEASURED_ENROLL_WALL_P95_MS[path]
        for path in measured_paths
    ), "процессорное время не больше стенки того же замера"
    estimate_ms = max(MEASURED_ENROLL_WALL_P95_MS.values()) + MEASURED_ENROLL_JUNK_P95_MS
    normalized = {
        (series, path): p95 * MEASURED_ENROLL_JUNK_P95_MS / paths["junk"]
        for series, paths in MEASURED_ENROLL_URT_P95_MS.items()
        for path, p95 in paths.items()
    }
    enroll_ms = max(estimate_ms, *normalized.values())
    assert enroll_ms <= H4_ENROLL_OPERATION_MS < enroll_ms + 1, (
        "стоимость enrollment в гейте — вывод из замеров с округлением вверх, не число из головы"
    )
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
    клиента — снят решением заказчика; на server Node API он не переопределён, запрет — только
    в location подписки публичного server, Н-25); ошибки апстрима не перехватываются
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
    assert re.findall(r"proxy_max_temp_file_size\s+(\S+);", http_level) == ["8m"]
    for port in ("8443", "8444"):
        server = _server_by_listen(text, port)
        assert "proxy_max_temp_file_size" not in server, (port, "Н-4: не переопределён")
    public = _server_by_listen(text, "443")
    assert re.findall(r"proxy_max_temp_file_size\s+(\S+);", public) == ["0"], "только /s/"
    subscription = dict(_locations(public))["^~ /s/"]
    assert "proxy_max_temp_file_size 0;" in subscription
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
    уровне http и `if` на уровне каждого из двух server Node API (не в location: иначе новый
    location его бы не унаследовал); ответ — единый формат без `Retry-After` (не повторять).
    Сам отказ — не `return 411` в `if` (фаза SERVER_REWRITE, раньше limit_req/limit_conn: на
    стенде 30 параллельных chunked-запросов получали 30 × 411 без единого 429 — роаст 001.25,
    раунд 3), а переход в служебный location, где 411 даёт try_files после пределов. Служебный
    location не `internal`: внешнему запросу к internal location nginx отвечает 404 в фазе
    FIND_CONFIG — тоже до пределов (стенд: 30 × 404 без 429, раунд 4); внешний запрос проходит
    пределы и получает 404 единого формата веткой в @length_required."""
    text = _config()
    http_level = _without_blocks(text, "server")
    default, rules = _map_rules(
        http_level,
        "$request_method:$content_length:$http_transfer_encoding",
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
        conditions = re.findall(r"if\s*\(\$body_without_length\)\s*\{([^}]*)\}", level)
        assert [c.strip() for c in conditions] == ["rewrite ^ /__length_required last;"], (
            port,
            "if ($body_without_length) { rewrite ^ /__length_required last; } на уровне server",
        )
        assert re.findall(r"error_page\s+411\s+=\s+(\S+);", level) == ["@length_required"], port
        service = dict(_locations(server))["= /__length_required"]
        assert service.split() == ["try_files", "/nonexistent", "=411;"], (port, service)
        named = [body for matcher, body in _locations(server) if matcher == "@length_required"]
        assert len(named) == 1, port
        assert "default_type application/json;" in named[0]
        branches = [
            (named[0][start + len("if") : open_].strip(), named[0][open_ + 1 : close].strip())
            for start, open_, close in _spans(named[0], "if")
        ]
        assert branches == [("($body_without_length = 0)", f"return 404 '{NOT_FOUND_BODY}';")], (
            port,
            branches,
        )
        assert f"return 411 '{LENGTH_REQUIRED_BODY}';" in _without_blocks(named[0], "if"), port
        assert "Retry-After" not in named[0], "411 не повторяется"
        for matcher, body in _locations(server):
            if matcher == "@length_required":
                continue  # ветка формата ответа — не правило; сверена равенством выше
            assert "$body_without_length" not in body, (port, matcher, "правило — уровня server")
    assert json.loads(LENGTH_REQUIRED_BODY)["error"]["code"] == "length_required"
    public = _server_by_listen(text, "443")
    assert "$body_without_length" not in public, "публичный server: браузеры и chunked — 001.66"


def _select_location(server: str, uri: str) -> str:
    """Матчер location, который nginx выберет для нормализованного пути: точное совпадение, иначе
    самый длинный префикс (``^~`` и обычный). Регулярных location на server Node API нет — иначе
    выбор зависел бы от их порядка, и страж длины не знал бы, чей предел действует."""
    exact, prefixes = None, []
    for matcher, _ in _locations(server):
        if matcher.startswith("@"):
            continue
        assert not matcher.startswith("~"), (matcher, "регулярный location на Node API")
        if matcher.startswith("= "):
            if matcher[2:] == uri:
                exact = matcher
        elif uri.startswith(matcher.removeprefix("^~ ")):
            prefixes.append((len(matcher.removeprefix("^~ ")), matcher))
    assert exact or prefixes, uri
    return exact or max(prefixes)[1]


def _lengths_around(limits: list[int]) -> list[str]:
    """Длины для карт по Content-Length: без заголовка, ноль, по обе стороны каждого предела,
    с ведущими нулями (nginx их принимает) и длиннее любого предела."""
    lengths = ["", "0", "1", "123456789012345"]
    for limit in limits:
        lengths += [str(limit - 1), str(limit), str(limit + 1), f"000{limit}", f"000{limit + 1}"]
        lengths += [str(limit * 10), "9" * len(str(limit)), "1" + "0" * len(str(limit))]
    return lengths


def test_bodies_over_the_limit_are_refused_with_413_after_the_rate_limits() -> None:
    """Тело длиннее ``client_max_body_size`` своего location nginx отвергает 413 в фазе FIND_CONFIG
    — раньше PREACCESS, где стоят limit_req и limit_conn (стенд: 30 параллельных 413 без единого
    429 и по строке error.log на каждый — роаст 001.25, раунд 4). Поэтому на обоих server Node API
    длину сверяет карта по Content-Length ещё в фазе SERVER_REWRITE и переводит тело длиннее
    предела в служебный location без предела тела, где 413 даёт try_files после пределов. Страж
    выводит пороги из ``client_max_body_size`` каждого location (своего или server) и подаёт на
    карты таблицу длин по путям, которые nginx отдал бы этому location: решение карты обязано
    совпасть с тем, отвергла бы тело проверка предела. 413 — страница nginx, как до 001.25
    (контракт README); прямой запрос к служебному пути (карта длины тело длинным не считает) —
    404 единого формата, а 413 чтения тела в другом location остаётся 413."""
    text = _config()
    http_level = _without_blocks(text, "server")
    sources = {
        "$content_length_over_64k": "$content_length",
        "$content_length_over_1m": "$content_length",
        "$agent_body_too_large": "$content_length_over_64k:$content_length_over_1m:$uri",
    }
    maps = {var: (src, *_map_rules(http_level, src, var)) for var, src in sources.items()}
    # Прямой запрос на служебный путь 413 (путь /__too_large, а карта длины тело длинным не
    # считает): 404; переведённому сюда и отказу при чтении тела в других location — 413.
    direct_default, direct_rules = _map_rules(
        http_level, "$agent_body_too_large:$uri", "$too_large_requested_directly"
    )
    for key, expected in (
        (f"0:{TOO_LARGE_PATH}", "1"),
        (f"1:{TOO_LARGE_PATH}", "0"),
        (f"0:{TOO_LARGE_PATH}/x", "0"),
        ("0:/agent/v1/heartbeat", "0"),
        (f"0:{REPORTS_PATH}", "0"),
    ):
        assert _map_lookup(direct_default, direct_rules, key) == expected, key

    def evaluate(variable: str, env: dict[str, str]) -> str:
        source, default, rules = maps[variable]
        key = re.sub(
            r"\$\w+",
            lambda ref: env[ref.group()] if ref.group() in env else evaluate(ref.group(), env),
            source,
        )
        return _map_lookup(default, rules, key)

    # Пути для сверки — от location обоих server и из самих карт: карта, которая на одном server
    # считает путь особым (отчёты — 1m), на другом отдала бы его location с пределом 64k, и тело
    # между пределами ушло бы в FIND_CONFIG мимо пределов (путь не обязан существовать на server).
    servers = {port: _server_by_listen(text, port) for port in ("8443", "8444")}
    # Путь с управляющим символом сюда не доходит (его перехватывает первое правило server), но
    # карты сверяются и на нём: конец строки у них — \z, и путь отчётов с \n в конце — не
    # отчётный location (роаст 001.25, раунд 5: с $ карта считала его отчётным, и 413 решался в
    # FIND_CONFIG до пределов).
    uris = {REPORTS_PATH, f"{REPORTS_PATH}/", f"{REPORTS_PATH}\n", f"{REPORTS_PATH}\r"}
    for server in servers.values():
        for matcher, _ in _locations(server):
            if matcher.startswith("= "):
                uris.add(matcher[2:])
            elif not matcher.startswith("@"):
                prefix = matcher.removeprefix("^~ ")
                uris |= {f"{prefix}state", f"{prefix}x/y"}
    for port, server in servers.items():
        level = _without_blocks(server, "location")
        rules = [
            (condition, body.strip())
            for condition, body in re.findall(r"if\s*\((\$\w+)\)\s*\{([^}]*)\}", level)
        ]
        too_large = [c for c, body in rules if body == f"rewrite ^ {TOO_LARGE_PATH} last;"]
        assert len(too_large) == 1 and too_large[0] in maps, (port, rules)
        variable = too_large[0]
        assert re.findall(r"error_page\s+413\s+=\s+(\S+);", level) == ["@too_large"], port
        server_limit = _bytes(BODY_LIMIT.findall(level)[0])
        limits: dict[str, int] = {}
        for matcher, body in _locations(server):
            if matcher.startswith("@") or matcher in (f"= {TOO_LARGE_PATH}", f"= {NOT_FOUND_PATH}"):
                continue
            own = BODY_LIMIT.findall(body)
            limits[matcher] = _bytes(own[0]) if own else server_limit
        lengths = _lengths_around(sorted({*limits.values(), _bytes("1m")}))
        for uri in sorted(uris):
            matcher = _select_location(server, uri)
            if matcher in (f"= {TOO_LARGE_PATH}", f"= {NOT_FOUND_PATH}"):
                continue
            limit = limits[matcher]
            for length in lengths:
                expected = "1" if length and int(length) > limit else "0"
                decided = evaluate(variable, {"$content_length": length, "$uri": uri})
                assert decided == expected, (port, matcher, uri, length, decided)
        service = dict(_locations(server))[f"= {TOO_LARGE_PATH}"]
        assert service.split() == [
            "client_max_body_size",
            "0;",
            "try_files",
            "/nonexistent",
            "=413;",
        ], (port, service)
        named = [body for matcher, body in _locations(server) if matcher == "@too_large"]
        assert len(named) == 1, port
        assert "default_type application/json;" in named[0]
        branches = [
            (named[0][start + len("if") : open_].strip(), named[0][open_ + 1 : close].strip())
            for start, open_, close in _spans(named[0], "if")
        ]
        assert branches == [
            ("($too_large_requested_directly)", f"return 404 '{NOT_FOUND_BODY}';")
        ], (port, branches)
        rest = _without_blocks(named[0], "if")
        assert re.findall(r"\breturn\b[^;]*;", rest) == ["return 413;"], (port, "страница nginx")
        assert "Retry-After" not in named[0], "413 не повторяется"


def test_node_api_refusals_are_decided_after_the_rate_limits() -> None:
    """На агентском и enrollment-server ни один отказ не решается `return` вне именованных
    location: `return` исполняется в фазе (SERVER_)REWRITE — раньше PREACCESS, где стоят
    limit_req и limit_conn, — и `error_page` после него пределов уже не даёт (стенд: 30 × 411 на
    9444 и 30 × 404 на несуществующий путь без единого 429 — 001.33, роаст 001.25, раунд 3).
    Отказы решает try_files (фаза PRECONTENT); `return` остаётся только в именованных location,
    куда error_page приводит уже после решения — там он лишь формирует тело ответа. `internal`
    тоже нет: внешнему запросу к такому location nginx отвечает 404 в фазе FIND_CONFIG, до
    пределов (стенд: 30 × 404 без единого 429 — роаст 001.25, раунд 4). В той же фазе nginx
    отвечает 301 на путь без косой черты, если prefix-location с ``*_pass`` оканчивается на «/»
    (auto_redirect; стенд: 40 × 301 на `/agent/v1` без единого 429 — роаст 001.25, раунд 4):
    у каждого такого location есть точный сосед без косой черты с try_files — и у точного
    location на «/» тоже: auto_redirect ставит `*_pass` любому location, имя которого кончается
    косой (роаст 001.25, раунд 6). `rewrite` — только переходы в служебные location
    (`redirect`/`permanent` и абсолютный адрес — ответ в фазе REWRITE)."""
    text = _config()
    for port in ("8443", "8444"):
        server = _server_by_listen(text, port)
        unnamed = server
        for matcher, _body in _locations(server):
            if matcher.startswith("@"):
                span = next(
                    (start, close)
                    for start, open_, close in _spans(unnamed, "location")
                    if unnamed[start + len("location") : open_].strip() == matcher
                )
                unnamed = unnamed[: span[0]] + unnamed[span[1] + 1 :]
        assert not re.search(r"\breturn\b", unnamed), (port, "return вне именованных location")
        assert not re.search(r"\binternal\s*;", server), (port, "internal — 404 до пределов")
        rewrites = sorted(" ".join(r.split()) for r in re.findall(r"\brewrite\b[^;]*;", server))
        allowed = sorted(
            f"rewrite ^ {path} last;"
            for path in (NOT_FOUND_PATH, LENGTH_REQUIRED_PATH, TOO_LARGE_PATH)
        )
        assert rewrites == allowed, (port, rewrites)
        assert _select_location(server, NOT_FOUND_PATH) == f"= {NOT_FOUND_PATH}", port
        assert dict(_locations(server))[f"= {NOT_FOUND_PATH}"].split() == [
            "client_max_body_size",
            "0;",
            "try_files",
            "/nonexistent",
            "=404;",
        ], (port, "служебный 404 — без предела тела, иначе FIND_CONFIG отвергнет тело до пределов")
        exact = dict(_locations(server))
        for matcher, body in _locations(server):
            if matcher.startswith(("@", "~")):
                continue
            prefix = matcher.removeprefix("= ").removeprefix("^~ ")
            if len(prefix) > 1 and prefix.endswith("/") and re.search(r"\b\w+_pass\b", body):
                sibling = exact.get(f"= {prefix[:-1]}")
                assert sibling is not None and sibling.split() == [
                    "try_files",
                    "/nonexistent",
                    "=404;",
                ], (port, matcher, "auto_redirect: 301 в FIND_CONFIG, до пределов")


def test_paths_with_control_characters_are_refused_before_any_location() -> None:
    """Путь с управляющими символами (%0A, %0D, %09, коды до 0x20 и 0x7F) nginx декодирует в $uri:
    он не совпадает ни с одним точным location, а маршруты приложения и карты PCRE с $ совпадают
    и перед завершающим \\n (стенд: /metrics%0A на публичном — 200 с метриками, enroll%0A на
    агентском порту — обработчик обмена; роаст 001.25, раунд 5). Поэтому на каждом server такой
    путь отвергается первым правилом уровня server, раньше карт длины и выбора location: на server
    Node API — переходом в служебный location ``= /__not_found`` без предела тела (404 даёт
    try_files, после пределов), на публичном — 404 сразу (пределов там нет). Страж подаёт на карту
    каждый управляющий символ в конце и в середине пути, а не сверяет её текст."""
    text = _config()
    http_level = _without_blocks(text, "server")
    default, rules = _map_rules(http_level, "$uri", "$uri_has_control")
    for char in CONTROL_CHARACTERS:
        for uri in (f"/metrics{char}", f"/agent/v1/enroll{char}", f"/s/abc{char}def"):
            assert _map_lookup(default, rules, uri) == "1", (repr(char), uri)
    for uri in ("/metrics", "/agent/v1/enroll", "/s/abc-DEF_09~", "/agent/v1/reports"):
        assert _map_lookup(default, rules, uri) == "0", uri
    # Первое правило server — первая директива модуля rewrite уровня server (фаза
    # SERVER_REWRITE исполняет их по порядку): любая форма `if`, а не только `if ($var)` (роаст
    # раунда 7), и `return`, `rewrite`, `break` вне `if` — голый `break;` выше правила снял бы все
    # правила server разом (роаст раунда 8). `set $api` перед ним — адрес апстрима, не решение.
    tree = _nginx_tree()
    for port, first in (
        ("8443", ("rewrite", "^", NOT_FOUND_PATH, "last")),
        ("8444", ("rewrite", "^", NOT_FOUND_PATH, "last")),
        ("443", ("return", "404")),
    ):
        rewrite_rules = [
            d
            for d in _server_tree(tree, port)
            if d.words[:1] in (("if",), ("return",), ("rewrite",), ("break",), ("set",))
            and d.words[:2] != ("set", "$api")
        ]
        assert rewrite_rules, port
        first_rule = rewrite_rules[0]
        assert first_rule.words == ("if", "($uri_has_control)"), (port, first_rule)
        assert first_rule.block is not None, port
        assert [d.words for d in first_rule.block] == [first], port
    assert not [d for _, d in _walk(tree) if d.words[:1] == ("break",)], "break снял бы правила"


# Директивы, которые пишут переменную nginx, и место записываемой переменной среди их слов
# (отрицательное — от конца заголовка блока). Переменные map изменяемы: второй писатель —
# ``set``, второй ``map`` той же переменной (побеждает последний, имена без учёта регистра),
# ``geo``, ``split_clients``, ``auth_request_set``, ``perl_set``, ``js_set``, ``js_var`` — выключил
# бы правило, не тронув сам map (роаст 001.25, раунды 6–8). Слова — значения из разбора nginx:
# имя в кавычках (``set "$x" 0``) — то же имя (роаст раунда 8).
VARIABLE_WRITERS = {
    "map": 2,
    "geo": -1,
    "split_clients": 2,
    "set": 1,
    "auth_request_set": 1,
    "perl_set": 1,
    "js_set": 1,
    "js_var": 1,
}
# Именованная группа регулярного выражения — тоже переменная nginx того же имени, и совпадение
# пишет в неё захват.
NAMED_GROUP = re.compile(r"\(\?(?:P?<|')([A-Za-z_]\w*)[>']")


def _variable(word: str) -> str:
    """Имя переменной nginx из слова: ``$Name`` и ``${Name}`` — ``$name`` (без учёта регистра)."""
    assert word.startswith("$"), word
    return "$" + word[1:].strip("{}").lower()


def test_map_results_have_a_single_writer() -> None:
    """``set $uri_has_control 0;`` в server выключил бы первое правило незаметно для стража порядка
    ``if``, ``set $agent_body_too_large 0;`` вернул бы 413 в FIND_CONFIG до пределов, второй
    ``map … $loggable`` — токен подписки в журнал; то же делают ``geo``, ``split_clients``,
    ``auth_request_set``, ``perl_set``, ``js_set``, ``js_var`` и именованная группа регулярного
    выражения. Поэтому у каждой переменной map писатель один — сама map, именованных групп в
    файле нет, а ``set`` пишет только ``$api``. Писатели ищутся по дереву директив со значениями
    слов, как их читает nginx: имя в кавычках — то же имя (роаст 001.25, раунды 6–8)."""
    writers: list[tuple[str, str]] = []
    for _context, directive in _walk(_nginx_tree()):
        name = directive.words[0]
        if name in VARIABLE_WRITERS:
            writers.append((name, _variable(directive.words[VARIABLE_WRITERS[name]])))
    mapped = [variable for name, variable in writers if name == "map"]
    assert {
        "$uri_has_control",
        "$body_without_length",
        "$agent_body_too_large",
        "$too_large_requested_directly",
        "$loggable",
        "$uri_is_service",
        "$raw_path_has_s",
    } <= set(mapped), mapped
    for variable in set(mapped):
        count = sum(1 for _, written in writers if written == variable)
        assert count == 1, (variable, "писатель переменной map — только она сама")
    assert {variable for _, variable in writers if variable not in mapped} == {"$api"}, writers
    assert NAMED_GROUP.findall(_config()) == [], "именованная группа — переменная nginx"


ERROR_LOG_LEVELS = ("warn", "error", "crit", "alert", "emerg")


def test_the_request_log_is_declared_only_at_the_http_level() -> None:
    """Н-25: ``access_log`` уровня server или location заменяет унаследованную пару уровня http
    целиком — строка без фильтра писала бы токен подписки (на 8443/8444 путь /s/ не
    маршрутизируется, но прислать его клиент может); ниже уровня http — только ``off``.
    ``error_log`` — не мягче ``warn``: отказы и задержки пределов (``limit_*_log_level notice``)
    пишут в error.log строку запроса, и при ``notice`` токен из пути лёг бы туда (роаст 001.25,
    раунд 7). Путь /s/ там, куда его отдаёт nginx: на публичном server — location подписки без
    журналов вовсе (любая строка error.log в контексте запроса несёт request-строку: ALERT
    «worker_connections are not enough», CRIT записи временного файла — порог уровня их не
    держит, роаст раунда 8) и без временных файлов ответа и тела; на server Node API — location
    без апстрима (ошибок апстрима с этим путём не бывает)."""
    tree = _nginx_tree()
    found = _walk(tree)
    at_http = [
        d.words[1:]
        for context, d in found
        if context == (("http",),) and d.words[0] == "access_log"
    ]
    assert at_http == [
        ("/var/log/nginx/access.log", "main", "if=$loggable"),
        ("/var/log/nginx/access.log", "unparsed", "if=$unparsed"),
    ], at_http
    below = [
        (context, d.words[1:])
        for context, d in found
        if d.words[0] == "access_log" and context != (("http",),)
    ]
    assert below and all(words == ("off",) for _, words in below), below
    for context, d in found:
        if d.words[0] != "error_log":
            continue
        target, *level = d.words[1:]
        assert target and len(level) == 1 and level[0] in ERROR_LOG_LEVELS, (context, d.words)
    assert [d.words for context, d in found if not context and d.words[0] == "error_log"] == [
        ("error_log", "/var/log/nginx/error.log", "warn")
    ]
    for limit in ("limit_req_log_level", "limit_conn_log_level"):
        assert {d.words[1:] for _, d in found if d.words[0] == limit} == {("notice",)}, limit
    text = _config()
    public = _server_by_listen(text, "443")
    matcher = _select_location(public, "/s/T")
    assert matcher == "^~ /s/", matcher
    subscription = [d.words for d in _location_tree(tree, "443", matcher)]
    assert ("access_log", "off") in subscription, subscription
    assert ("error_log", "/dev/null", "emerg") in subscription, subscription
    assert ("proxy_max_temp_file_size", "0") in subscription, subscription
    body_limit = [words[1] for words in subscription if words[0] == "client_max_body_size"]
    buffer = [words[1] for words in subscription if words[0] == "client_body_buffer_size"]
    assert len(body_limit) == 1 and len(buffer) == 1, subscription
    assert 0 < _bytes(body_limit[0]) <= _bytes(buffer[0]), (body_limit, buffer, "тело — в буфере")
    for port in ("8443", "8444"):
        server = _server_by_listen(text, port)
        for uri in ("/s/T", "/S/T", "/sub/s/T"):
            body = dict(_locations(server))[_select_location(server, uri)]
            assert not re.search(r"\b\w+_pass\b", body), (port, uri, "путь /s/ — без апстрима")


def _location_tree(tree: tuple[Directive, ...], port: str, matcher: str) -> tuple[Directive, ...]:
    """Директивы location с матчером (как в канонической записи) единственного server порта."""
    found = [
        d.block
        for d in _server_tree(tree, port)
        if d.words[:1] == ("location",) and " ".join(d.words[1:]) == matcher and d.block is not None
    ]
    assert len(found) == 1, (port, matcher)
    return found[0]


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


def _map_source(http_level: str, variable: str) -> str:
    """Источник map переменной — как в канонической записи (``"$server_port:$uri"``)."""
    found = re.findall(rf"map\s+(\S+)\s+{re.escape(variable)}\s*\{{", http_level)
    assert len(found) == 1, (variable, found)
    return str(found[0])


def _map_key(source: str, values: dict[str, str]) -> str:
    """Ключ map для запроса: переменные источника заменены значениями запроса."""
    return re.sub(r"\$(\w+)", lambda match: values[match.group(1)], source)


def test_the_subscription_token_never_reaches_the_access_log() -> None:
    """Н-25: журнал пишется только по `$loggable` — согласию четырёх ключей: нормализованного
    `$uri` (формы `//s/`, `/%73/`, `/x/../s/`, `/s%2F` маршрутизируются в `/s/`), признака
    «`$uri` — служебный путь своего server» (запрос, переведённый `rewrite` на 8443/8444, несёт к
    журналу служебный путь — роаст 001.25, раунд 6: строка `/s%2F<токен>%0A` в журнале 9444; у
    публичного server служебных путей нет — раунд 9), сырого ключа «в пути строки запроса есть
    /s/» (до «?» и «#», с косой и в виде `%2F`, без учёта регистра) — он читается только у
    служебного `$uri` — и признака «отвергнут до разбора URI» (пустой `$uri` при любом статусе:
    400, 408, 414, 505 — такая строка пишется форматом без строки запроса; раунд 9 знал только
    400, и 408, 414, 505 писали токен — стенд, роаст раунда 9). Раунды 7–8 читали сырой ключ у
    всех запросов, и суффикс `#/s/` у любого пути прятал запрос из журнала целиком. Страж
    подаёт на правила таблицу (сырая строка, `$uri` к журналу, статус, порт) — ключи карт
    собираются из их источников в файле, а не из списка здесь — и сверяет служебные пути карты
    с целями `rewrite` каждого server; четвёртая линия — `access_log off` в самом location
    `/s/`, и Referer со страницы подписки пишется «-»."""
    text = _config()
    http_level = _without_blocks(text, "server")
    # Самопроверка эмулятора map: точный ключ побеждает покрывающее его регулярное правило,
    # объявленное раньше, и сравнивается без учёта регистра; регулярные — по порядку.
    assert _map_lookup("d", [("~^a", "r"), ("A", "x")], "a") == "x"
    assert _map_lookup("d", [("~^a", "r"), ("~^ab", "s")], "ab") == "r"
    assert _map_lookup("d", [("~^A", "r")], "ab") == "d"
    parts = ("$loggable_uri", "$uri_is_service", "$raw_path_has_s", "$unparsed")
    sources = {var: _map_source(http_level, var) for var in (*parts, "$loggable")}
    maps = {var: (source, _map_rules(http_level, source, var)) for var, source in sources.items()}

    def value(variable: str, **values: str) -> str:
        source, rules = maps[variable]
        return _map_lookup(*rules, _map_key(source, values))

    # Сырой ключ: путь строки запроса — до «?» и «#»; /s/ в любом написании и месте пути.
    for line, expected in (
        ("GET /s/T HTTP/1.1", "1"),
        ("GET /s/T?x=1 HTTP/1.1", "1"),
        ("GET /s/T#x HTTP/1.1", "1"),
        ("GET /healthz?/s/ HTTP/1.1", "0"),
        ("GET /healthz#/s/ HTTP/1.1", "0"),
        ("GET /healthz#x/%73/y HTTP/1.1", "0"),
        ("GET /healthz?x=/%73/y HTTP/1.1", "0"),
        ("GET /s/T%zz HTTP/1.1", "1"),
        ("GET  /s/T%zz HTTP/1.1", "1"),
        ("M-SEARCH /s/T%zz HTTP/1.1", "1"),
        ("GET //s/T%zz HTTP/1.1", "1"),
        ("GET /x/../s/T HTTP/1.1", "1"),
        ("GET /%73/T%zz HTTP/1.1", "1"),
        ("GET /%53/T HTTP/1.1", "1"),
        ("GET http://host/s/T HTTP/1.1", "1"),
        ("GET /S/T HTTP/2.0", "1"),
        ("GET //s/T", "1"),
        ("GET /s%2FT%0A HTTP/1.1", "1"),
        ("GET /s%2fT HTTP/2.0", "1"),
        ("GET /%73%2FT HTTP/1.1", "1"),
        ("GET /%2Fs/T HTTP/1.1", "1"),
        ("POST /x%2F..%2Fs%2FT HTTP/1.1", "1"),
        ("GET /agent/v1/state%2Fs HTTP/1.1", "0"),
        ("GET /sub%2Fs2/x HTTP/1.1", "0"),
        ("GET /healthz HTTP/1.1", "0"),
        ("GET /sub/s2/x HTTP/1.1", "0"),
    ):
        assert value("$raw_path_has_s", request=line) == expected, line

    def loggable(request: str, uri: str, status: str, port: str = "443") -> str:
        values = {"request": request, "uri": uri, "status": status, "server_port": port}
        return value("$loggable", **{var[1:]: value(var, **values) for var in parts})

    # Запрос без перехода: `$uri` журнала — путь, по которому nginx выбрал location.
    for request, uri, status, expected in (
        ("GET /s/T HTTP/1.1", "/s/T", "200", "0"),
        ("GET //s/T HTTP/1.1", "/s/T", "200", "0"),
        ("GET /x/../s/T HTTP/1.1", "/s/T", "200", "0"),
        ("GET /%73/T HTTP/1.1", "/s/T", "200", "0"),
        ("GET /s%2FT HTTP/2.0", "/s/T", "200", "0"),
        ("GET http://host/s/T HTTP/1.1", "/s/T", "200", "0"),
        ("GET /S/T HTTP/1.1", "/S/T", "404", "0"),
        ("GET /s/T%0A HTTP/1.1", "/s/T\n", "404", "0"),
        ("GET /healthz HTTP/1.1", "/healthz", "200", "1"),
        ("GET /healthz#/s/T HTTP/1.1", "/healthz", "200", "1"),
        ("POST /api/v1/auth/login#/s/ HTTP/1.1", "/api/v1/auth/login", "401", "1"),
        ("POST /agent/v1/enroll#/%73/ HTTP/2.0", "/agent/v1/enroll", "422", "1"),
        ("GET /healthz?/s/ HTTP/1.1", "/healthz", "200", "1"),
        ("GET /api/v1/admin/nodes/s/state HTTP/1.1", "/api/v1/admin/nodes/s/state", "422", "1"),
        ("GET /sub/s2/x HTTP/1.1", "/sub/s2/x", "404", "1"),
        ("POST /agent/v1/reports HTTP/2.0", "/agent/v1/reports", "200", "1"),
        # На публичном server служебных путей нет: путь, нормализованный в служебный, — обычный
        # запрос к приложению, и сырой /s/ его не прячет (роаст раунда 9).
        ("GET /s/T/../../__not_found HTTP/1.1", NOT_FOUND_PATH, "404", "1"),
        ("GET /%73/../__TOO_LARGE HTTP/1.1", "/__TOO_LARGE", "404", "1"),
    ):
        assert loggable(request, uri, status) == expected, (request, uri, status)
    # Отвергнут до разбора URI — пустой `$uri` при любом статусе: 400 (негодный процент), 408
    # (строка запроса не пришла за client_header_timeout), 414 (строка длиннее буфера), 505
    # (HTTP/2.0 и выше поверх HTTP/1); `$request` тогда — сырые байты строки. Строка — форматом
    # без строки запроса, не основным (роаст раунда 9: 408, 414 и 505 писали токен, стенд).
    for request, status in (
        ("GET /s/T%zz HTTP/1.1", "400"),
        ("GET /healthz%zz HTTP/1.1", "400"),
        ("GET /s/T", "408"),
        ("GET /s/T?" + "a" * 9000, "414"),
        ("GET /s/T HTTP/2.0", "505"),
        ("GET /%73/T HTTP/3.0", "505"),
    ):
        for port in ("443", "8443", "8444"):
            assert loggable(request, "", status, port) == "0", (request, status, port)
            assert value("$unparsed", request=request, uri="", status=status, server_port=port) == (
                "1"
            ), (request, status, port)
    for uri in ("/s/T", "/healthz", "/", NOT_FOUND_PATH):
        for status in ("200", "400", "404", "408", "414", "505"):
            assert value("$unparsed", request="GET / HTTP/1.1", uri=uri, status=status) == "0", uri
    # Переведённый запрос: `$uri` журнала — служебный путь; служебные пути карты — ровно цели
    # `rewrite` своего server (новый переход без записи в карте журнал бы открыл, а запись без
    # перехода прятала бы обычные запросы).
    tree = _nginx_tree()
    targets = {
        port: {
            directive.words[2]
            for _context, directive in _walk(_server_tree(tree, port))
            if directive.words[:1] == ("rewrite",)
        }
        for port in ("443", "8443", "8444")
    }
    service = {NOT_FOUND_PATH, LENGTH_REQUIRED_PATH, TOO_LARGE_PATH}
    assert targets["8443"] == targets["8444"] == service, targets
    assert targets["443"] == set(), targets
    for port in ("443", "8443", "8444"):
        for path in sorted(targets["8443"] | {"/healthz", "/s/T"}):
            expected = "1" if path in targets[port] else "0"
            got = value("$uri_is_service", uri=path, server_port=port)
            assert got == expected, (port, path)
    for port in ("8443", "8444"):
        for target in sorted(targets[port]):
            for request, expected in (
                ("GET /s%2FT%0A HTTP/1.1", "0"),
                ("POST /s%2FT HTTP/1.1", "0"),
                ("POST /%2Fs%2FT HTTP/2.0", "0"),
                ("GET /s/T%0A HTTP/1.1", "0"),
                ("GET /%73%2FT%0A HTTP/1.1", "0"),
                ("GET /healthz%0A HTTP/1.1", "1"),
                ("GET /healthz%0A#/s/T HTTP/1.1", "1"),
                ("GET /healthz%0A?/s/T HTTP/1.1", "1"),
            ):
                for status in ("404", "411", "413", "429"):
                    got = loggable(request, target, status, port)
                    assert got == expected, (port, target, request, status)
    # Referer со страницы подписки — «-» в любом написании пути, как у сырого ключа (раунд 7:
    # прежний ~/s/ пропускал /S/ и /%73/); основной формат пишет именно `$log_referer`, а ответы
    # `/s/` несут Referrer-Policy — браузер такой Referer и не отправит (раунд 9).
    default, rules = _map_rules(http_level, "$http_referer", "$log_referer")
    for referer, expected in (
        ("https://h/s/T", "-"),
        ("https://h/S/T", "-"),
        ("https://h/%73/T", "-"),
        ("https://h/s%2FT", "-"),
        ("", "-"),
        ("https://h/cabinet", "$http_referer"),
    ):
        assert _map_lookup(default, rules, referer) == expected, (referer, rules)
    main_format = re.search(r"log_format\s+main\s+((?:'[^']*'\s*)+);", http_level)
    assert main_format is not None
    assert '"$log_referer"' in main_format.group(1), main_format.group(1)
    assert "$http_referer" not in main_format.group(1), "Referer — только через $log_referer"
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
    subscription = [d.words for d in _location_tree(tree, "443", "^~ /s/")]
    assert ("access_log", "off") in subscription, subscription
    assert ("add_header", "Referrer-Policy", "no-referrer", "always") in subscription, subscription


# Директивы, которые меняют `$uri` запроса внутренним переходом или отдают файлы (index,
# autoindex делают переход на индексный файл), и модули, чьи обработчики делают переходы во время
# выполнения (njs, perl, lua — `internalRedirect`, `internal_redirect`, `ngx.exec`), — вне файла.
STATIC_OR_SCRIPTED = ("index", "autoindex", "random_index", "load_module", "alias", "root")
OTHER_UPSTREAMS = ("fastcgi_pass", "uwsgi_pass", "scgi_pass", "grpc_pass", "memcached_pass")


def test_only_the_service_rewrites_change_the_uri() -> None:
    """Н-25: журнал решает по `$uri` к моменту записи и держится на том, что `$uri` меняют только
    переходы `rewrite` 8443/8444 в служебные location — их пути в карте `$uri_is_service`. Любой
    другой внутренний переход увёл бы запрос подписки из `location /s/` (с его `access_log off`
    и `error_log /dev/null`) на путь, который журнал пишет, — со строкой запроса и токеном (стенд:
    `error_page 500 502 503 504 /50x.html;` при лежащем api — токен в access.log и в error.log;
    роаст 001.25, раунд 9). Поэтому `error_page` — только в именованный location (он `$uri` не
    меняет); `rewrite` — только в server 8443/8444 и только в служебные пути; `try_files` —
    проба заведомо отсутствующего файла и код; каждый location решает сам (`proxy_pass`,
    `return` или `try_files`), и ни у одного server нет запросов без location — отдачи файлов и
    перехода на индексный файл нет; `X-Accel-Redirect` апстрима не исполняется (один
    `proxy_ignore_headers` на уровне http — ниже он заменил бы список целиком), апстрим — только
    `proxy_pass`; модулей скриптов и подключаемых модулей нет."""
    tree = _nginx_tree()
    found = _walk(tree)
    for context, directive in found:
        name = directive.words[0]
        if name == "error_page":
            assert directive.words[-1].startswith("@"), (context, directive.words)
        if name == "try_files":
            assert directive.words[1:-1] == ("/nonexistent",), (context, directive.words)
            assert re.fullmatch(r"=[1-5]\d\d", directive.words[-1]), (context, directive.words)
        assert name not in STATIC_OR_SCRIPTED and name not in OTHER_UPSTREAMS, directive.words
        assert not name.startswith(("js_", "perl")) and "_by_lua" not in name, directive.words
    rewrites = [directive for _, directive in found if directive.words[0] == "rewrite"]
    node_rewrites = [
        directive
        for port in ("8443", "8444")
        for _, directive in _walk(_server_tree(tree, port))
        if directive.words[0] == "rewrite"
    ]
    assert len(rewrites) == len(node_rewrites) == 6, rewrites
    for directive in node_rewrites:
        assert directive.words[1] == "^" and directive.words[3:] == ("last",), directive.words
        assert directive.words[2] in {NOT_FOUND_PATH, LENGTH_REQUIRED_PATH, TOO_LARGE_PATH}
    (http,) = [d for d in tree if d.words == ("http",)]
    assert http.block is not None
    for server in (d for d in http.block if d.words == ("server",)):
        assert server.block is not None
        locations = [d for d in server.block if d.words[0] == "location"]
        assert [d for d in locations if d.words[1:] == ("/",)], "запрос без location — к файлам"
        for location in locations:
            assert location.block is not None
            decided = {d.words[0] for d in location.block} & {"proxy_pass", "return", "try_files"}
            assert decided, (location.words, "location без решения отдал бы файл")
    ignored = [
        (context, directive.words[1:])
        for context, directive in found
        if directive.words[0] == "proxy_ignore_headers"
    ]
    assert len(ignored) == 1 and ignored[0][0] == (("http",),), ignored
    assert "x-accel-redirect" in {word.lower() for word in ignored[0][1]}, ignored


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


def test_the_node_ca_of_the_api_is_the_one_nginx_trusts() -> None:
    """Пара CA одна у прокси и у C-01 (001.25): nginx проверяет клиентов по ``ca.crt`` из
    смонтированного каталога ``secrets/tls``, ``api`` подписывает листы ключом ``ca_key`` и
    отдаёт ноде ``ca_pem`` из ``ca_cert``. Секрет ``ca_cert`` объявлен тем самым файлом
    ``tls/ca.crt``, а не копией: разойдись они, CA выпускал бы листы, которые прокси отвергнет у
    всего парка, и стенд молчал бы до первой ноды. Переменные ``api`` указывают на копии,
    которые делает entrypoint в ``/run/secrets``; оверлей их не переопределяет."""
    base_file, overlays = _compose_files()
    base = yaml.safe_load(base_file.read_text(encoding="utf-8"))
    assert base["secrets"]["ca_cert"] == {"file": "./secrets/tls/ca.crt"}
    assert base["secrets"]["ca_key"] == {"file": "./secrets/ca_key"}
    assert "./secrets/tls:/etc/nginx/certs:ro" in _service(base, "nginx")["volumes"]
    agent = _server_by_listen(_config(), "8443")
    assert re.findall(r"ssl_client_certificate\s+(\S+);", agent) == ["/etc/nginx/certs/ca.crt"]
    api = _service(base, "api")
    assert api["environment"]["CA_KEY_FILE"] == "/run/secrets/ca_key"
    assert api["environment"]["CA_CERT_FILE"] == "/run/secrets/ca_cert"
    mounted = {(item["source"], item["target"]) for item in api["secrets"]}
    assert {
        ("ca_key", "/run/host-secrets/ca_key"),
        ("ca_cert", "/run/host-secrets/ca_cert"),
    } <= mounted
    for overlay in overlays:
        over = _service(yaml.safe_load(overlay.read_text(encoding="utf-8")), "api")
        environment = over.get("environment") or {}
        assert not {"CA_KEY_FILE", "CA_CERT_FILE"} & set(environment), overlay.name
        assert "secrets" not in over, overlay.name


def _host_path(compose_file: Path, value: str, where: str) -> Path:
    """Путь хоста из значения Compose. Интерполяция (``${PWD}``, ``${CA_DIR:-./secrets}``) —
    отказ: её значение задаёт окружение запуска, и страж, не зная его, не может решить, что
    окажется в контейнере (роаст 001.25, раунд 3), — пути томов, секретов, конфигов и сборки
    пишутся литералом."""
    assert "$" not in value, (compose_file.name, where, value, "путь с интерполяцией")
    return (compose_file.parent / Path(value).expanduser()).resolve()


def _host_sources(
    compose_file: Path,
    service: dict[str, Any],
    declared: dict[str, dict[str, Path]],
    bound_volumes: dict[str, Path],
) -> list[Path]:
    """Пути хоста, которые служба видит внутри контейнера или вносит в образ: файлы её секретов
    и конфигов (по объявлениям верхнего уровня), источники томов-привязок (короткая и длинная
    запись; в длинной ``type: bind`` источник — путь хоста и без префикса: Compose разрешает его
    от каталога проекта), именованные тома, привязанные к каталогу хоста (``driver_opts`` с
    ``device``), ``env_file``, пути ``develop.watch`` (синхронизация файлов хоста в контейнер),
    контекст сборки, дополнительные контексты и секреты сборки."""
    found = []
    for kind in ("secrets", "configs"):
        for item in service.get(kind) or []:
            name = item["source"] if isinstance(item, dict) else item
            if name in declared[kind]:
                found.append(declared[kind][name])
    for volume in service.get("volumes") or []:
        if isinstance(volume, str):
            source, bind = volume.split(":", 1)[0], False
        elif volume.get("type", "volume") in ("bind", "volume"):
            source, bind = volume.get("source", ""), volume.get("type") == "bind"
        else:
            continue  # tmpfs и прочее — не путь хоста
        if bind or source.startswith((".", "/", "~", "$")):
            found.append(_host_path(compose_file, source, "volumes"))
        elif source in bound_volumes:
            found.append(bound_volumes[source])
    for rule in (service.get("develop") or {}).get("watch") or []:
        found.append(_host_path(compose_file, str(rule.get("path", "")), "develop.watch"))
    env_files = service.get("env_file") or []
    for entry in [env_files] if isinstance(env_files, (str, dict)) else env_files:
        path = entry.get("path") if isinstance(entry, dict) else entry
        found.append(_host_path(compose_file, str(path), "env_file"))
    build = service.get("build")
    if build:
        context = build.get("context", ".") if isinstance(build, dict) else build
        found.append(_host_path(compose_file, str(context), "build.context"))
        if isinstance(build, dict):
            for extra in (build.get("additional_contexts") or {}).values():
                if "://" not in str(extra) and not str(extra).startswith("service:"):
                    found.append(_host_path(compose_file, str(extra), "build.additional_contexts"))
            for item in build.get("secrets") or []:
                name = item["source"] if isinstance(item, dict) else item
                if name in declared["secrets"]:
                    found.append(declared["secrets"][name])
    return found


def test_only_the_api_holds_the_node_ca_key() -> None:
    """Ключ CA подписывает identity всего парка (§7.1): он доступен только ``api``, который
    выпускает листы, — воркерам, планировщику, прокси и базе он не нужен, а каждый лишний
    держатель — ещё одно место кражи (роаст 001.25, раунд 1). Сверка — по путям хоста, а не по
    именам (раунд 2: второе имя секрета на тот же файл, том с каталогом-предком, ``configs``;
    раунд 3: интерполированные пути, ``volumes_from``, ``pid`` соседа, именованный том с
    ``driver_opts`` bind, секреты и контексты сборки; раунд 4: ``bind`` без префикса,
    ``develop.watch``, ``privileged`` и ``devices``): у каждой службы основы и оверлеев, в том
    числе унаследовавшей общий якорь, её пути (``_host_sources``) не содержат файла ключа CA —
    кроме секрета ``ca_key`` у ``api``; чужие тома (``volumes_from``), пространство процессов
    соседа (``pid``), весь хост (``privileged``) и его устройства (``devices`` — диск с файлом
    ключа) не берёт ни одна служба."""
    base_file, overlays = _compose_files()
    documents = [
        (path, yaml.safe_load(path.read_text(encoding="utf-8"))) for path in (base_file, *overlays)
    ]
    ca_key = (base_file.parent / documents[0][1]["secrets"]["ca_key"]["file"]).resolve()
    declared: dict[str, dict[str, Path]] = {"secrets": {}, "configs": {}}
    bound_volumes: dict[str, Path] = {}
    for path, document in documents:
        for kind in ("secrets", "configs"):
            for name, spec in (document.get(kind) or {}).items():
                if isinstance(spec, dict) and "file" in spec:
                    declared[kind][name] = _host_path(path, spec["file"], f"{kind}.{name}")
        for name, spec in (document.get("volumes") or {}).items():
            device = ((spec or {}).get("driver_opts") or {}).get("device")
            if device:
                bound_volumes[name] = _host_path(path, str(device), f"volumes.{name}")
    holders = set()
    for path, document in documents:
        for name, service in (document.get("services") or {}).items():
            assert "volumes_from" not in service, (path.name, name, "чужие тома — и их секреты")
            assert "pid" not in service, (path.name, name, "чужие процессы — и их /proc/*/root")
            assert not service.get("privileged"), (path.name, name, "privileged — весь хост")
            assert "devices" not in service, (path.name, name, "устройства хоста — и его диски")
            sources = _host_sources(path, service, declared, bound_volumes)
            if any(ca_key == source or ca_key.is_relative_to(source) for source in sources):
                holders.add((path.name, name))
            environment = service.get("environment") or {}
            names = (
                {entry.split("=", 1)[0] for entry in environment}
                if isinstance(environment, list)
                else set(environment)
            )
            if "CA_KEY_FILE" in names:
                holders.add((path.name, name))
    assert sorted(holders) == [("docker-compose.yml", "api")], sorted(holders)
    api_secrets = [
        item["source"] if isinstance(item, dict) else item
        for item in documents[0][1]["services"]["api"]["secrets"]
    ]
    assert [name for name in api_secrets if declared["secrets"].get(name) == ca_key] == ["ca_key"]


# Механизмы Compose, которыми служба получает файлы хоста или чужого контейнера, — список
# разрешений, а не запретов (роаст 001.25, раунд 4): список запретов стража ключа CA три раунда
# подряд обходили новыми способами — второе имя секрета, интерполяция пути, ``volumes_from``,
# ``pid``, сокет Docker, ``/proc`` хоста, ``cap_add``, ``device_cgroup_rules``, внешний том,
# ``build.ssh``. Всё, чего здесь нет, — отказ: новый ключ службы или новая привязка сначала
# пересматриваются и вносятся сюда. Пути привязок — от каталога Compose, как их пишут файлы.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"name", "services", "secrets", "volumes"})
ALLOWED_SERVICE_KEYS = frozenset(
    {
        "image",
        "build",
        "entrypoint",
        "command",
        "environment",
        "env_file",
        "secrets",
        "volumes",
        "tmpfs",
        "ports",
        "depends_on",
        "healthcheck",
        "logging",
        "mem_limit",
        "memswap_limit",
        "cpus",
        "pids_limit",
    }
)
ALLOWED_BUILD = {"context": "../../control-plane", "dockerfile": "Dockerfile"}
ALLOWED_ENV_FILES = frozenset({".env"})
ALLOWED_BINDS: dict[str, frozenset[str]] = {
    "postgres": frozenset(
        {
            "./postgres/entrypoint.sh",
            "./postgres/initdb.d/10-roles.sh",
            "../../control-plane/migrations/bootstrap/roles.sql",
        }
    ),
    "redis": frozenset(),
    "nginx": frozenset({"../nginx/nginx.conf", "./secrets/tls"}),
    "api": frozenset({"../../control-plane/app", "../../control-plane/migrations"}),
    "worker-critical": frozenset({"../../control-plane/app"}),
    "worker-background": frozenset({"../../control-plane/app"}),
    "scheduler": frozenset({"../../control-plane/app"}),
}
ALLOWED_NAMED_VOLUMES: dict[str, frozenset[str]] = {
    "postgres": frozenset({"pg_data"}),
    "redis": frozenset({"redis_data"}),
}
# Опубликованные порты — как их пишут файлы: наружу смотрит только nginx; база и Redis — в оверлее
# разработки для тестов и только на 127.0.0.1 литералом: адрес переменной решала бы среда запуска,
# и запись в файле ничего не говорила бы об открытости (роаст 001.25, раунд 6: стенд с
# DEV_BIND_ADDR=0.0.0.0 открывал Redis без пароля и базу соседним проектам VM; с рабочей машины —
# туннель deploy/scripts/stand-tunnel.sh). Роли приложения не публикуются (правило границы 2):
# порт api 8000 снаружи дал бы клиенту задать X-Client-Cert и X-Forwarded-For самому — uvicorn
# доверяет заголовкам прокси с любого адреса (роаст 001.25, раунд 5).
ALLOWED_PORTS: dict[str, frozenset[str]] = {
    "nginx": frozenset(
        {
            "${BIND_ADDR:-0.0.0.0}:${HTTP_PORT:-80}:80",
            "${BIND_ADDR:-0.0.0.0}:${HTTPS_PORT:-443}:443",
            "${BIND_ADDR:-0.0.0.0}:${AGENT_PORT:-8443}:8443",
            "${BIND_ADDR:-0.0.0.0}:${ENROLL_PORT:-8444}:8444",
        }
    ),
    "postgres": frozenset({"127.0.0.1:${PG_HOST_PORT:-5432}:5432"}),
    "redis": frozenset({"127.0.0.1:${REDIS_HOST_PORT:-6379}:6379"}),
}
# Команда и точка входа — только у служб из чужих образов: роль приложения с ``command:`` или
# ``entrypoint:`` обошла бы docker-entrypoint.sh — флаги uvicorn (access-log, заголовки прокси),
# миграции при старте, копирование секретов и понижение прав (роаст 001.25, раунд 7).
ALLOWED_LAUNCH_KEYS: dict[str, frozenset[str]] = {
    "postgres": frozenset({"command", "entrypoint"}),
    "redis": frozenset({"command"}),
}


def test_compose_services_use_only_reviewed_mechanisms() -> None:
    """Ключ CA держит только ``api`` (страж выше сверяет пути хоста), но путь к файлу — не
    единственный способ его получить: сокет Docker даёт ``docker exec`` в ``api``, ``/proc`` хоста
    — ``/proc/<pid api>/root`` (все роли под одним uid), ``cap_add`` и ``device_cgroup_rules`` —
    диск хоста, ``build.ssh`` — подпись ключом при сборке. Поэтому у служб основы и оверлеев —
    только разрешённые ключи, у сборки — только контекст и Dockerfile, у томов — только
    разрешённые привязки своей службы, объявленные именованные тома без драйвера, опций и
    ``external``, у секретов — только литеральный ``file``, у ``env_file`` — только ``.env``
    (роаст 001.25, раунд 4), у ``ports`` — только разрешённые записи своей службы: роли
    приложения не публикуются (раунд 5), база и Redis — только на литеральном 127.0.0.1
    (раунд 6), ``command`` и ``entrypoint`` — только у postgres и redis (раунд 7)."""
    base_file, overlays = _compose_files()
    for path in (base_file, *overlays):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        top = {key for key in document if not key.startswith("x-")}
        assert top <= ALLOWED_TOP_LEVEL_KEYS, (path.name, sorted(top - ALLOWED_TOP_LEVEL_KEYS))
        for name, spec in (document.get("volumes") or {}).items():
            assert not spec, (path.name, name, "именованный том — без драйвера, опций и external")
        for name, spec in (document.get("secrets") or {}).items():
            assert isinstance(spec, dict) and set(spec) == {"file"}, (path.name, name, spec)
            assert "$" not in str(spec["file"]), (path.name, name, "путь секрета — литерал")
        for name, service in (document.get("services") or {}).items():
            assert name in ALLOWED_BINDS, (path.name, name, "служба вне списка разрешений")
            extra = set(service) - ALLOWED_SERVICE_KEYS
            assert not extra, (path.name, name, sorted(extra))
            if "build" in service:
                assert service["build"] == ALLOWED_BUILD, (path.name, name, service["build"])
            for port in service.get("ports") or []:
                assert port in ALLOWED_PORTS.get(name, frozenset()), (path.name, name, port)
            for key in ("command", "entrypoint"):
                if key in service:
                    assert key in ALLOWED_LAUNCH_KEYS.get(name, frozenset()), (path.name, name, key)
            env_files = service.get("env_file") or []
            for entry in [env_files] if isinstance(env_files, (str, dict)) else env_files:
                file = entry.get("path") if isinstance(entry, dict) else entry
                assert file in ALLOWED_ENV_FILES, (path.name, name, file)
            for volume in service.get("volumes") or []:
                if isinstance(volume, str):
                    source = volume.split(":", 1)[0]
                    kind = "bind" if source.startswith((".", "/", "~", "$")) else "volume"
                else:
                    kind, source = volume.get("type", "volume"), volume.get("source", "")
                    assert kind in ("bind", "volume", "tmpfs"), (path.name, name, volume)
                    assert set(volume) <= {"type", "source", "target", "read_only", "tmpfs"}, (
                        path.name,
                        name,
                        volume,
                    )
                if kind == "bind":
                    assert source in ALLOWED_BINDS[name], (path.name, name, source)
                elif kind == "volume":
                    assert source in ALLOWED_NAMED_VOLUMES.get(name, frozenset()), (
                        path.name,
                        name,
                        source,
                    )
                else:
                    assert "source" not in volume, (path.name, name, volume)


def test_the_app_roles_keep_copied_secrets_in_memory() -> None:
    """Точка входа образа копирует секреты из ``/run/host-secrets`` в ``/run/secrets`` владельцу
    ``app`` (``docker-entrypoint.sh``). Без tmpfs копия — в записываемом слое контейнера: ключ CA
    и ключ шифрования полей лежали бы на диске хоста всё время жизни контейнера и попадали бы в
    ``docker commit``/``export`` (роаст 001.25, раунд 2). Каждая служба с секретами хоста держит
    ``/run/secrets`` в tmpfs."""
    base_file, overlays = _compose_files()
    base = yaml.safe_load(base_file.read_text(encoding="utf-8"))
    roles = {
        name: service
        for name, service in base["services"].items()
        if any(
            isinstance(item, dict) and str(item.get("target", "")).startswith("/run/host-secrets/")
            for item in service.get("secrets") or []
        )
    }
    assert set(roles) == {"api", "worker-critical", "worker-background", "scheduler"}, set(roles)
    for name, service in roles.items():
        tmpfs = service.get("tmpfs") or []
        mounts = [tmpfs] if isinstance(tmpfs, str) else tmpfs
        assert any(str(m).split(":", 1)[0] == "/run/secrets" for m in mounts), (name, tmpfs)
    for overlay in overlays:
        for name, service in (
            yaml.safe_load(overlay.read_text(encoding="utf-8")).get("services") or {}
        ).items():
            assert "tmpfs" not in service, (overlay.name, name, "оверлей не переопределяет tmpfs")


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
