# VAI TEAM — 通用 CC Worker 容器
# 基于 deploy/agent/Dockerfile 扩展，支持多角色全栈开发
# 构建：docker build -t vaiteam/cc-worker:latest -f Dockerfile .

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai
ENV CC_WORKER_HOME=/opt/vaiteam-cc-worker

ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 换源（国内默认清华，先用 http 避免证书问题，安装 ca-certificates 后可用 https）
RUN sed -i "s|http://archive.ubuntu.com|http://${APT_MIRROR}|g; s|http://security.ubuntu.com|http://${APT_MIRROR}|g" /etc/apt/sources.list

# 基础工具链
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    jq \
    unzip \
    tar \
    rsync \
    bash \
    ripgrep \
    openssh-client \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    docker.io \
    sqlite3 \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 + Claude Code CLI
RUN curl -fsSL http://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm config set registry ${NPM_REGISTRY} \
    && npm install -g @anthropic-ai/claude-code

# 非 root 用户（先创建，后续目录操作需要）
RUN useradd -m -u 1000 -s /bin/bash worker

# 预创建 Claude Code 配置目录（避免首次运行交互式配置阻塞）
RUN mkdir -p /home/worker/.claude \
    && chown -R worker:worker /home/worker/.claude

# Python 依赖
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -i ${PIP_INDEX_URL} -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# 工作目录结构
RUN mkdir -p ${CC_WORKER_HOME} /workspace /var/log/vaiteam
COPY . ${CC_WORKER_HOME}/

# 原型 Workshop wrapper（同步自 prototype-workshop/worker/）
COPY run_cc_wrapper.sh ${CC_WORKER_HOME}/run_cc_wrapper.sh
COPY fetch_task_pack.py ${CC_WORKER_HOME}/fetch_task_pack.py
RUN chmod +x ${CC_WORKER_HOME}/run_cc_wrapper.sh
RUN chmod +x ${CC_WORKER_HOME}/entrypoint.sh \
    && chown -R worker:worker /workspace /var/log/vaiteam

WORKDIR /workspace
ENTRYPOINT ["/opt/vaiteam-cc-worker/entrypoint.sh"]
