FROM python:3.12-slim

ENV TZ=Asia/Shanghai
ARG APT_MIRROR=mirrors.aliyun.com
ARG INSTALL_LIBREOFFICE=1
# 默认国内源写死在镜像内，无需目标机再配 compose。ARG APT_MIRROR 仅作覆盖（如自建 apt 缓存）。
# Trixie 以 deb822 为主，sed 易漏；改为按 VERSION_CODENAME 重写 sources.list 并移走官方 .sources。
RUN set -eux; \
    mir="${APT_MIRROR:-mirrors.aliyun.com}"; \
    [ -n "$mir" ] || mir=mirrors.aliyun.com; \
    . /etc/os-release; \
    codename="${VERSION_CODENAME:?}"; \
    if [ -d /etc/apt/sources.list.d ]; then \
      mkdir -p /etc/apt/sources.list.d/disabled-vaiteam; \
      for f in /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
        [ -f "$f" ] || continue; \
        mv "$f" /etc/apt/sources.list.d/disabled-vaiteam/; \
      done; \
    fi; \
    { \
      echo "deb https://${mir}/debian ${codename} main"; \
      echo "deb https://${mir}/debian ${codename}-updates main"; \
      echo "deb https://${mir}/debian-security ${codename}-security main"; \
    } > /etc/apt/sources.list; \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git unrar-free docker.io openssh-client rsync \
    && if [ "${INSTALL_LIBREOFFICE}" = "1" ]; then apt-get install -y --no-install-recommends libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress; fi \
    && update-ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_DEFAULT_TIMEOUT=300
ENV PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT}

RUN pip config set global.index-url "${PIP_INDEX_URL}" && \
    pip config set install.trusted-host mirrors.aliyun.com

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 8 --timeout "${PIP_DEFAULT_TIMEOUT}" -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
