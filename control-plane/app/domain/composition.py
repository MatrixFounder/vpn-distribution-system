"""Потоки конфигурации и состава для ноды (interfaces.md §5.2; постановка §4.6 «Матрица влияния»;
data-model.md §4.2.3 ``node_config_versions``, ``node_user_state``, ``node_user_credentials``;
UC-01, UC-07, UC-11).

Задача 001.28: схемы ответа состояния и ``CompositionService`` с фиксированными значениями.
Настоящий отбор дельты по ``updated_seq``, выделение ``desired_users_seq`` под блокировкой строки
ноды и запись ``resync_required`` — 001.29; полный снапшот, поколение, удержание long-poll до
30 с на канале Redis ``node:{id}`` и подтверждение курсоров — 001.75; конфигурация Xray и её
версии — 001.26, 001.27.

Что в заглушке настоящее: вентиль по статусу берётся из ``domain.statuses`` (§4.6 —
единственный источник правил), контрольная сумма конфигурации считается от неё самой
(``config_checksum``), тег пользователя выводится из его идентификатора (``xray_email``), а
поток конфигурации не несёт credentials — это инвариант §5.2, а не свойство фиксированных
значений.

Чего заглушка не хранит: состояния между вызовами. «Что нового» она выводит из курсоров запроса —
нода, отставшая хотя бы по одному курсору, получает весь фиксированный ответ, нода на текущих
курсорах — 204 «изменений нет». Это соглашение заглушки, а не правило §5.2.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.commands import Command, CommandService
from app.domain.nodes import Node
from app.domain.statuses import INT4_MAX, INT8_MAX, STREAM_GATE
from app.errors import ApiError

# Перечисление ``user_node_state`` data-model.md §4.2.3.
UserState = Literal["active", "suspended_quota", "suspended_admin", "expired", "removed"]
# «Только отзывы» §4.6: изменения, снимающие доступ. Всё, кроме ``active``, доступ снимает.
REVOKING_STATES: frozenset[UserState] = frozenset(
    ("suspended_quota", "suspended_admin", "expired", "removed")
)

Checksum = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def xray_email(user_id: uuid.UUID) -> str:
    """Устойчивый тег пользователя в Xray (§4.2.3 ``node_user_credentials.xray_email``, §5.9):
    ``u`` и идентификатор. Тег переживает ротацию credentials — по нему сводятся счётчики."""
    return f"u{user_id}"


def config_checksum(document: Mapping[str, Any]) -> str:
    """SHA-256 канонической записи конфигурации: ключи отсортированы, разделители без пробелов,
    не-ASCII символы не экранируются, кодировка UTF-8. Нода считает сумму тем же правилом и
    сверяет её с полем ``checksum`` — сериализация ответа при этом может отличаться."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class StateConfig(BaseModel):
    """Поток конфигурации: полная конфигурация Xray без credentials пользователей (§5.2), её
    версия ``node_config_versions.config_version`` и контрольная сумма."""

    version: int = Field(ge=1)
    checksum: Checksum
    document: dict[str, Any] = Field(
        alias="json", description="конфигурация Xray целиком, без credentials пользователей"
    )


class Credentials(BaseModel):
    """Credentials пары «пользователь × нода» (``node_user_credentials``), расшифрованные C-01
    для ноды, определённой по identity запроса (§5.2). Расшифровка — 001.29.

    Тега здесь нет: он о пользователе, а не о паре, и нужен как раз строкам без ключей —
    поэтому живёт в ``UserRow``."""

    vless_uuid: uuid.UUID
    trojan_password: str
    version: int = Field(ge=1, description="версия credentials, растёт при ротации Н-32")


class UserRow(BaseModel):
    """Строка потока состава (``node_user_state``): состояние доступа, устойчивый тег, остаток
    гранта квоты, заблокированные адреса (R-27) и номер изменения.

    Credentials несёт только строка, которая доступ **выдаёт** (``active``): ключ нужен, чтобы
    добавить пользователя в inbound. Строке, снимающей доступ, он не нужен — удалять и
    блокировать нода умеет по тегу ``xray_email``, который есть у **каждой** строки, — и потому
    не выдаётся: отфильтрованный поток
    ``disabled`` и ``suspended`` (§4.6 «Только отзывы») состоит из одних таких строк, а это
    статусы «отключена администратором» и «приостановлена провайдером по жалобе» (Р-3), где
    везти на ноду живые ключи пар «пользователь × нода» незачем. Восстановление доступа приходит
    новой строкой ``active`` — с ключом."""

    user_id: uuid.UUID
    state: UserState
    xray_email: str = Field(description="устойчивый тег §4.2.3: им нода применяет любую строку")
    quota_grant_bytes: int = Field(ge=0)
    blocked_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address]
    updated_seq: int = Field(ge=1)
    credentials: Credentials | None = None

    @model_validator(mode="after")
    def _keys_only_where_access_is_granted(self) -> UserRow:
        """Инвариант держится моделью, а не подбором фиксированных строк: в 001.29 строки
        соберёт JOIN ``node_user_state × node_user_credentials``, и джойн, вернувший ключ на
        приостановленную строку, молча отправил бы живые ключи на отключённую ноду."""
        if (self.credentials is not None) is not (self.state == "active"):
            raise ValueError("ключи несёт ровно строка, выдающая доступ")
        return self


class Users(BaseModel):
    """Поток состава: новый курсор ноды, признак полноты и строки.

    ``seq`` — наибольший ``updated_seq`` выборки **до** фильтра по статусу: строки, скрытые
    вентилем, нода добирает снапшотом после выхода из фильтрующего статуса, а не повторной
    дельтой (§5.2). При пустой выборке курсор не двигается и равен присланному; у снапшота
    ``seq`` — текущий номер изменения, а не наибольший среди выданных строк.

    ``full`` — состав описан целиком: применяя такой ответ, нода удаляет пользователей, которых
    в нём нет (§4.2.3). Ответ, урезанный вентилем по статусу или пределом размера, приходит с
    ``full = false``, даже если нода просила снапшот, — иначе она вычистила бы у себя всех, кого
    скрыл вентиль."""

    seq: int = Field(ge=0)
    full: bool = Field(description="состав описан целиком: отсутствующих нода удаляет")
    rows: list[UserRow]


class StateResponse(BaseModel):
    """Ответ ``GET /agent/v1/state``: дельты обоих потоков, поколение identity, признак
    обязательного снапшота и команды канала (§5.2)."""

    config: StateConfig | None = None
    users: Users
    generation: int = Field(ge=1)
    resync_required: bool
    commands: list[Command]


class AckIn(BaseModel):
    """Тело ``POST /agent/v1/ack`` (§5.2): курсоры, которые нода применила и удержит после
    перезапуска."""

    model_config = ConfigDict(extra="forbid")

    applied_config_version: int = Field(ge=0, le=INT4_MAX)
    applied_users_seq: int = Field(ge=0, le=INT8_MAX)


# --- фиксированные значения заглушки ------------------------------------------------------------

STUB_CONFIG_VERSION = 1
STUB_GENERATION = 1  # совпадает с поколением identity заглушки (``domain.nodes.stub_identity``)
STUB_USERS_SEQ = 4  # текущий номер изменения состава (``current_users_seq`` по строкам ниже)
STUB_USER_ACTIVE = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
STUB_USER_SUSPENDED = uuid.UUID("00000000-0000-7000-8000-0000000000a2")
STUB_USER_REMOVED = uuid.UUID("00000000-0000-7000-8000-0000000000a3")
STUB_USER_LATEST = uuid.UUID("00000000-0000-7000-8000-0000000000a4")
STUB_BLOCKED_IP = ipaddress.IPv4Address("203.0.113.77")
STUB_QUOTA_GRANT_BYTES = 1073741824  # 1 ГиБ — размер гранта задаёт 001.36 (R-26, ОВ-A7)

# Каркас конфигурации Xray: структура без пользователей. Настоящий генератор (inbound-профили
# §4.4, ключи REALITY, маршрутизация) — 001.26 и 001.27; здесь важно одно свойство §5.2:
# ``clients`` пуст, credentials пользователей потоком конфигурации не передаются.
STUB_CONFIG_DOCUMENT: dict[str, Any] = {
    "log": {"loglevel": "warning"},
    "inbounds": [
        {
            "tag": "vless_raw_vision",
            "port": 443,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {"network": "raw", "security": "reality"},
        }
    ],
    "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "blackhole", "protocol": "blackhole"},
    ],
}
# Сумма постоянна для пары «нода × версия конфигурации»: в бою она лежит в строке
# ``node_config_versions`` и считается один раз при публикации (001.27), а не при каждой выдаче.
STUB_CONFIG_CHECKSUM = config_checksum(STUB_CONFIG_DOCUMENT)

# Пространство имён для воспроизводимых credentials заглушки. В бою ключ и пароль случайны и
# хранятся зашифрованными (§7.2); здесь они выводятся из пары «нода × пользователь», чтобы
# фикстуры контракта не менялись от прогона к прогону.
STUB_CREDENTIALS_NS = uuid.UUID("00000000-0000-7000-8000-0000000000cd")


def stub_credentials(node_id: uuid.UUID, user_id: uuid.UUID) -> Credentials:
    """Credentials заглушки: тег выводится из идентификатора пользователя, ключ и пароль — из
    пары «пользователь × нода» (§11.3: уникальность на пару — компенсирующий контроль для
    недоверенной ноды, поэтому на разных нодах у одного пользователя они разные). Настоящий
    выпуск и шифрование ``node_user_credentials`` — 001.29 (создаёт строки при отсутствии)."""
    derived = uuid.uuid5(STUB_CREDENTIALS_NS, f"{node_id}:{user_id}")
    return Credentials(
        vless_uuid=derived,
        trojan_password=f"stub-trojan-{derived.hex}",  # noqa: S106 — производное значение
        version=1,
    )


def current_users_seq(node_id: uuid.UUID) -> int:
    """Текущий номер изменения состава ноды — по всем строкам, включая ``removed``: в бою это
    ``nodes.desired_users_seq`` (§4.2.3), и снапшот описывает состав именно на него. Считать его
    по строкам снапшота нельзя: ``removed`` в снапшот не входит, и номер удалённой пары пришёл бы
    ноде ещё раз."""
    return max(row.updated_seq for row in stub_rows(node_id))


def stub_rows(node_id: uuid.UUID) -> list[UserRow]:
    """Четыре строки состава по номерам изменений 1…4: доступ выдан, доступ снят по исчерпанию
    квоты (с заблокированным адресом R-27 и без ключей), пара удалена, доступ выдан ещё одному.

    Четвёртая строка не украшение: самое свежее изменение здесь **не** снимает доступ, поэтому
    в фильтрующем статусе (``disabled``, ``suspended``) курсор выборки (4) и наибольший номер
    среди выданных строк (3) расходятся — иначе правило «курсор идёт по выборке до фильтра»
    было бы неотличимо от «курсор равен константе» и не проверялось бы ничем."""
    return [
        UserRow(
            user_id=STUB_USER_ACTIVE,
            xray_email=xray_email(STUB_USER_ACTIVE),
            state="active",
            quota_grant_bytes=STUB_QUOTA_GRANT_BYTES,
            blocked_ips=[],
            updated_seq=1,
            credentials=stub_credentials(node_id, STUB_USER_ACTIVE),
        ),
        UserRow(
            user_id=STUB_USER_SUSPENDED,
            xray_email=xray_email(STUB_USER_SUSPENDED),
            state="suspended_quota",
            quota_grant_bytes=0,
            blocked_ips=[STUB_BLOCKED_IP],
            updated_seq=2,
            credentials=None,  # доступ снят: ключ не нужен, хватает тега
        ),
        UserRow(
            user_id=STUB_USER_REMOVED,
            xray_email=xray_email(STUB_USER_REMOVED),
            state="removed",
            quota_grant_bytes=0,
            blocked_ips=[],
            updated_seq=3,
            credentials=None,
        ),
        UserRow(
            user_id=STUB_USER_LATEST,
            xray_email=xray_email(STUB_USER_LATEST),
            state="active",
            quota_grant_bytes=STUB_QUOTA_GRANT_BYTES,
            blocked_ips=[],
            updated_seq=4,
            credentials=stub_credentials(node_id, STUB_USER_LATEST),
        ),
    ]


def stub_config() -> StateConfig:
    """Поток конфигурации заглушки: версия, каркас и контрольная сумма от него самого."""
    return StateConfig(
        version=STUB_CONFIG_VERSION, checksum=STUB_CONFIG_CHECKSUM, json=STUB_CONFIG_DOCUMENT
    )


# Отказы раздела несут ``Retry-After``: иначе частоту повторов определяет только агент, а
# сервер на неё повлиять не может. Подтверждение ноды — действие администратора (UC-01 шаг 7),
# поэтому пауза длинная; несовпадение поколения агент исправляет сам, снапшотом, — короткая.
RETRY_AFTER_APPROVAL = 300
RETRY_AFTER_RESYNC = 1


def node_not_approved() -> ApiError:
    """``pending``: нода зарегистрирована, но не подтверждена — раздел ей ничего не выдаёт
    (§4.6, матрица влияния: ни потоков, ни канала команд)."""
    return ApiError(
        "node_not_approved",
        "нода не подтверждена администратором",
        status=403,
        headers={"Retry-After": str(RETRY_AFTER_APPROVAL)},
    )


def cursor_ahead() -> ApiError:
    """Нода предъявила курсор больше того, что выдавал Control Plane. Так бывает после
    восстановления из резервной копии (§9) и при подделке: в обоих случаях дельты неприменимы, и
    единственный ответ — снапшот. Молчаливые 204 здесь недопустимы: нода, объявившая себя
    впереди, перестала бы получать отзывы доступа, а сервер считал бы её синхронизированной
    (§11.3 — нода недоверенная)."""
    return ApiError(
        "cursor_ahead",
        "курсор ноды опережает состояние Control Plane: требуется полный снапшот",
        status=409,
        headers={"Retry-After": str(RETRY_AFTER_RESYNC)},
    )


def generation_mismatch(expected: int) -> ApiError:
    """Нода предъявила чужое поколение identity: дельты к её состоянию неприменимы, нужен
    снапшот с текущим поколением (§5.2, код 409)."""
    return ApiError(
        "generation_mismatch",
        "поколение identity не совпадает: требуется полный снапшот",
        status=409,
        details={"generation": expected},
        headers={"Retry-After": str(RETRY_AFTER_RESYNC)},
    )


class CompositionService:
    """Потоки конфигурации и состава поверх пула asyncpg. Заглушка 001.28: база не читается и
    не пишется, Redis не публикуется."""

    def __init__(self, pool: Any, commands: CommandService | None = None) -> None:
        self._pool = pool
        self._commands = commands or CommandService(pool)

    async def state_for(
        self, node: Node, config_version: int, users_seq: int, generation: int, full: bool
    ) -> StateResponse | None:
        """Состояние для ноды (§5.2). ``None`` — изменений нет: маршрут отвечает 204.

        Вентиль по статусу §4.6 применяется до всего остального и к обеим формам запроса —
        снапшот его не обходит: ``pending`` — отказ 403; поколение, отличное от действующего, —
        409 (нода обязана взять снапшот). Поколение заглушки фиксировано; поиск действующего в
        ``node_identities`` — 001.75, окно перекрытия при ротации (Н-28) — 001.31.

        «Нода отстала» считается только по потокам, которые вентиль открывает. Иначе нода в
        ``provisioning`` (состав закрыт, курсор состава не двигается) отставала бы по нему
        вечно и получала немедленный ответ на каждый запрос — вместо удержания до таймаута,
        которого требует §5.2 именно для этого статуса.

        Инвариант для 001.75, который здесь соблюдён и обязан сохраниться: удержание long-poll
        не должно держать ни подключения из пула asyncpg (служба берёт пул, а не подключение),
        ни разделяемый клиент Redis — у него ``socket_timeout`` 2 с (``app.redis``), и наивный
        ``pubsub.listen()`` на 30 с даст 503 вместо 204.
        """
        gate = STREAM_GATE[node.status]
        if gate.refused:
            raise node_not_approved()
        if generation != STUB_GENERATION:
            raise generation_mismatch(STUB_GENERATION)
        # Курсор состава читается один раз и до строк: три чтения в разные моменты — та самая
        # форма, которую 001.29 повторит на базе, а обратный порядок накрыл бы объявленным
        # курсором пользователя, появившегося между чтениями (ответ с ``users.full`` велит
        # удалить всех, кого в нём нет).
        current = current_users_seq(node.id)
        # Курсор впереди серверного бывает после восстановления Control Plane из копии (§9) и
        # при подделке; дельта к такому состоянию неприменима. Снапшот — исключение и есть выход
        # из него: он присланный курсор игнорирует и сам ставит ``seq = current``. Отказывать и
        # ему значило бы оставить ноду без выхода: отказ велит взять снапшот, а снапшот отвечает
        # тем же отказом.
        if not full and (config_version > STUB_CONFIG_VERSION or users_seq > current):
            raise cursor_ahead()
        composition_open = gate.composition != "none"
        # Полным состав считается, только когда вентиль отдаёт его целиком: снапшот в
        # фильтрующем статусе полным не является, и нода, приняв его за полный, вычистила бы у
        # себя всех, кого вентиль скрыл (§4.2.3).
        complete = full and gate.composition == "all"
        config = (
            stub_config()
            if gate.config and (full or config_version < STUB_CONFIG_VERSION)
            else None
        )
        behind = composition_open and (full or users_seq < current)
        # Взведённый признак — самостоятельный повод ответить: §5.2 требует показать его при
        # выходе из фильтрующего статуса, а нода к этому моменту уже на текущих курсорах, и
        # молчаливый 204 съел бы поручение ровно там, где оно нужно.
        if config is None and not behind and not node.resync_required:
            return None
        rows: list[UserRow] = []
        seq = users_seq
        if composition_open:
            # Отсутствие строки означает «удали» только в ответе, который объявил себя полным.
            # Урезанный вентилем ответ обязан везти ``removed`` строкой: иначе отключённая нода,
            # взяв снапшот по указанию ``resync_required``, получит меньше отзывов, чем дала бы
            # обычная дельта, а курсор уйдёт за потерянное удаление.
            selected = (
                [row for row in stub_rows(node.id) if row.state != "removed"]
                if complete
                else [row for row in stub_rows(node.id) if row.updated_seq > users_seq]
            )
            # Курсор продвигается по выборке — до фильтра по статусу (§5.2). Считать его по
            # выданным строкам значило бы застревать всякий раз, когда самое свежее изменение
            # вентиль скрыл: нода просила бы ту же дельту снова и снова. Пропущенное она добирает
            # снапшотом после выхода из статуса — для того и ``resync_required``.
            seq = current if full else max((row.updated_seq for row in selected), default=current)
            rows = (
                [row for row in selected if row.state in REVOKING_STATES]
                if gate.composition == "revocations"
                else selected
            )
        return StateResponse(
            config=config,
            users=Users(seq=seq, full=complete, rows=rows),
            generation=STUB_GENERATION,
            # Признак приходит из сохранённого состояния ноды (колонка ``nodes.resync_required``,
            # §4.2.3); заглушка состояния не хранит, поэтому добавляет к нему «этот ответ
            # отфильтрован статусом» — правило, по которому колонку взводит 001.29. Полный состав
            # — это и есть выполненная пересинхронизация, поэтому в таком ответе признак
            # снимается: иначе нода, применив снапшот, тут же пошла бы за ним снова. Условие
            # остановки для агента наблюдаемо — ``users.full``.
            resync_required=(node.resync_required or gate.filters_composition) and not complete,
            # Канал команд: заглушка не помнит доставку, поэтому её канал никогда не пуст —
            # включить его в решение «изменений нет» значило бы сделать 204 недостижимым, а
            # удержание long-poll — невозможным. Отсюда соглашение заглушки: команда едет вместе
            # с ответом, который и так отправляется. В 001.76 ожидающая команда обязана сама
            # быть причиной ответить: канал проверяется до решения о 204.
            commands=await self._commands.pending(node) if gate.commands else [],
        )

    async def ack(self, node: Node, config_version: int, users_seq: int) -> None:
        """Принять подтверждение применённых курсоров (``nodes.applied_*``, ``applied_at``).

        Принимает ноду, а не её идентификатор (отклонение от сигнатуры задачи): вентиль §4.6
        решает и здесь — неподтверждённая нода не вправе объявить себя применившей то, чего ей
        не выдавали. Запись ``applied_*`` и отказ подтверждению выше выданного курсора — 001.75.
        Там же снятие ``resync_required`` — но **только** по подтверждению курсора ответа,
        пришедшего с ``users.full: true``: снять признак по подтверждению урезанного ответа
        значило бы закрыть ноде пересинхронизацию, которой не было."""
        if STREAM_GATE[node.status].refused:
            raise node_not_approved()

    async def publish_config(self, node_id: uuid.UUID) -> None:
        """Новая версия конфигурации ноды: запись ``node_config_versions`` и ``PUBLISH
        node:{id}`` — 001.29 (генератор конфигурации — 001.27)."""

    async def publish_user(self, user_id: uuid.UUID) -> None:
        """Выдать или обновить доступ пользователя на всех его нодах: строки ``node_user_state``
        с новым ``updated_seq`` и пробуждение каналов — 001.29."""

    async def remove_user(self, user_id: uuid.UUID) -> None:
        """Снять доступ пользователя: строки в ``removed`` и отзыв credentials — 001.29
        (бюджет Н-13 на доставку отзыва — §5.4)."""
