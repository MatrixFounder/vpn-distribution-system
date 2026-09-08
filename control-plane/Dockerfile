# Образ Control Plane — компоненты C-01 api, C-02 worker-critical / worker-background,
# C-03 scheduler (docs/architectures/system-architecture.md §3.2). Один образ, роль процесса
# задаёт переменная APP_ROLE — см. docker-entrypoint.sh. Собирается из deploy/compose/.
#
# Зависимости — только готовые колёса из requirements.lock (без компилятора в образе);
# приложение запускается из /app без установки пакета, поэтому docker-compose.dev.yml
# может подменить /app/app исходниками с хоста.

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app

WORKDIR /app

COPY requirements.lock ./
RUN pip install --only-binary=:all: -r requirements.lock \
    && rm requirements.lock

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY app ./app

# Без USER: точка входа стартует от root ради секретов Compose (права файлов хоста) и сразу
# переключается на app через setpriv — процесс роли всегда работает от uid 10001.
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
