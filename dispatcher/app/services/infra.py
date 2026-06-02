"""
基础设施控制层

统一通过 docker compose 管理 Agent 容器。
docker compose 的 service 名为 claw-{project_id[:8]}-{role}，与 agent_id 格式不同。

启动流程（两阶段确认）：
  1. Dispatcher 通过 SSH（或本地）执行 docker compose up -d {service}
  2. 容器状态标记为 starting
  3. Agent 容器启动后主动调用 POST /api/agents/heartbeat 报到
  4. Dispatcher 收到首次心跳后才标记为 online
  5. 超时未报到 → start_failed

支持模式：
  - docker-compose: 本地执行 docker compose
  - ssh: 通过 SSH 到远程 VM 执行 docker compose
  - kubernetes: 通过 kubectl 管理 Pod（保留，未来迁移）
"""

import logging
import asyncio
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from app.core import openclaw_config

if TYPE_CHECKING:
    from app.models import Agent, InfraNode

CLI_PREFIX = "openclaw-cli "
DISPATCHER_SHARED_SSH_KEY = "/root/.ssh/id_ed25519"

logger = logging.getLogger(__name__)

AGENT_START_TIMEOUT = 60  # Agent 报到超时（秒）


def agent_service_name(project_id: str, role: str) -> str:
    """docker compose 中的 service 名，与 claw_deployer 生成的一致"""
    return f"claw-{project_id[:8]}-{role}"


class InfraBackend(ABC):
    @abstractmethod
    async def start_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        """启动 Agent 容器（docker compose up -d），返回后状态为 starting，等待 Agent 报到"""

    @abstractmethod
    async def stop_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        """停止 Agent 容器（docker compose stop），保留 volume"""

    @abstractmethod
    async def restart_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        """重启 Agent 容器（docker compose restart），保留 volume"""

    @abstractmethod
    async def destroy_agent(self, agent_id: str, container_id: str = "", config: dict | None = None) -> bool:
        """销毁 Agent 容器（docker compose down），保留 volume"""

    @abstractmethod
    async def exec_command(self, agent_id: str, command: str, timeout: int = 120, config: dict | None = None) -> dict:
        """在 Agent 容器中执行命令"""

    @abstractmethod
    async def health_check(self, agent_id: str, config: dict | None = None) -> bool:
        """检查 Agent 容器是否在运行"""

    @abstractmethod
    async def logs(self, agent_id: str, tail: int = 50, config: dict | None = None) -> str:
        """获取 Agent 容器日志"""


class DockerComposeBackend(InfraBackend):
    """本地 docker compose 管理容器"""

    def __init__(self, config: dict):
        self.compose_file = config.get("compose_file", "./docker-compose.agents.yml")
        self.project_dir = config.get("project_dir", ".")

    def _compose_cmd(self, *args: str) -> list[str]:
        return ["docker", "compose", "-f", self.compose_file, *args]

    async def _run(self, *args: str, timeout: int = 60) -> dict:
        cmd = self._compose_cmd(*args)
        kw: dict = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
        if self.project_dir and self.project_dir != ".":
            kw["cwd"] = self.project_dir
        proc = await asyncio.create_subprocess_exec(*cmd, **kw)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {"exit_code": proc.returncode, "stdout": stdout.decode(), "stderr": stderr.decode()}
        except asyncio.TimeoutError:
            proc.kill()
            return {"exit_code": -1, "error": "timeout"}

    def _svc(self, agent_id: str, config: dict | None) -> str:
        return (config or {}).get("service_name") or agent_id

    async def start_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        svc = self._svc(agent_id, config)
        import os
        if not os.path.isfile(self.compose_file):
            # 没有 compose 文件，回退到 docker run
            return await _spawn_cc_worker_local(
                agent_id, role,
                dispatcher_base=(config or {}).get("dispatcher_base", ""),
                api_token=(config or {}).get("api_token", ""),
                env_vars=(config or {}).get("env_vars"),
            )
        result = await self._run("up", "-d", svc)
        if result.get("exit_code") != 0:
            # compose 失败也回退到 docker run
            logger.warning(f"Compose start failed for {agent_id}, fallback to docker run: {result.get('stderr','')}")
            return await _spawn_cc_worker_local(
                agent_id, role,
                dispatcher_base=(config or {}).get("dispatcher_base", ""),
                api_token=(config or {}).get("api_token", ""),
                env_vars=(config or {}).get("env_vars"),
            )
        return {"status": "starting", "agent_id": agent_id}

    async def stop_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        svc = self._svc(agent_id, config)
        result = await self._run("stop", svc)
        if result.get("exit_code") != 0:
            return {"error": result.get("stderr", "")}
        return {"status": "stopped"}

    async def restart_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        svc = self._svc(agent_id, config)
        result = await self._run("restart", svc)
        if result.get("exit_code") != 0:
            return {"error": result.get("stderr", "")}
        return {"status": "starting"}

    async def destroy_agent(self, agent_id: str, container_id: str = "", config: dict | None = None) -> bool:
        svc = self._svc(agent_id, config)
        result = await self._run("rm", "-f", "-s", svc)
        return result.get("exit_code") == 0

    async def exec_command(self, agent_id: str, command: str, timeout: int = 120, config: dict | None = None) -> dict:
        if command.strip().startswith(CLI_PREFIX):
            cli_args = command.strip()[len(CLI_PREFIX):]
            return await self._run("run", "--rm", "openclaw-cli", *cli_args.split(), timeout=timeout)
        svc = self._svc(agent_id, config)
        result = await self._run("exec", "-T", svc, "sh", "-c", command, timeout=timeout)
        return result

    async def health_check(self, agent_id: str, config: dict | None = None) -> bool:
        svc = self._svc(agent_id, config)
        result = await self._run("ps", svc, "--format", "json")
        if result.get("exit_code") != 0:
            return False
        return '"running"' in result.get("stdout", "").lower()

    async def logs(self, agent_id: str, tail: int = 50, config: dict | None = None) -> str:
        svc = self._svc(agent_id, config)
        result = await self._run("logs", "--tail", str(tail), svc)
        return result.get("stdout", "") + result.get("stderr", "")


class SSHBackend(InfraBackend):
    """通过 SSH 到远程 VM 执行 docker compose 命令"""

    def __init__(self, config: dict):
        self.host = config["host"]
        self.port = config.get("port", 22)
        self.user = config.get("user", "root")
        self.key_file = config.get("key_file", "~/.ssh/id_rsa")
        from app.core.config import settings
        deploy_root = settings.AGENT_DEPLOY_ROOT
        self.compose_file = config.get("compose_file", f"{deploy_root}/docker-compose.agents.yml")
        self.project_dir = config.get("project_dir", deploy_root)

    def _ssh_prefix(self) -> list[str]:
        return [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "IdentitiesOnly=yes",
            "-i", self.key_file,
            "-p", str(self.port),
            f"{self.user}@{self.host}",
        ]

    def _compose_prefix(self) -> str:
        return f"cd {self.project_dir} && docker compose -f {self.compose_file}"

    async def _run(self, compose_args: str, timeout: int = 60) -> dict:
        remote_cmd = f"{self._compose_prefix()} {compose_args}"
        cmd = self._ssh_prefix() + [remote_cmd]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {"exit_code": proc.returncode, "stdout": stdout.decode(), "stderr": stderr.decode()}
        except asyncio.TimeoutError:
            proc.kill()
            return {"exit_code": -1, "error": "timeout"}

    async def _ssh_run(self, remote_cmd: str, timeout: int = 60) -> dict:
        cmd = self._ssh_prefix() + [remote_cmd]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {"exit_code": proc.returncode, "stdout": stdout.decode(), "stderr": stderr.decode()}
        except asyncio.TimeoutError:
            proc.kill()
            return {"exit_code": -1, "error": "timeout"}

    def _svc(self, agent_id: str, config: dict | None) -> str:
        return (config or {}).get("service_name") or agent_id

    async def start_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        svc = self._svc(agent_id, config)
        result = await self._run(f"up -d {svc}")
        if result.get("exit_code") != 0:
            logger.error(f"Start agent {agent_id} failed: {result.get('stderr', '')}")
            return {"error": result.get("stderr", ""), "status": "start_failed"}
        return {"status": "starting", "agent_id": agent_id}

    async def stop_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        svc = self._svc(agent_id, config)
        result = await self._run(f"stop {svc}")
        if result.get("exit_code") != 0:
            return {"error": result.get("stderr", "")}
        return {"status": "stopped"}

    async def restart_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        svc = self._svc(agent_id, config)
        result = await self._run(f"restart {svc}")
        if result.get("exit_code") != 0:
            return {"error": result.get("stderr", "")}
        return {"status": "starting"}

    async def destroy_agent(self, agent_id: str, container_id: str = "", config: dict | None = None) -> bool:
        svc = self._svc(agent_id, config)
        result = await self._run(f"rm -f -s {svc}")
        return result.get("exit_code") == 0

    async def exec_command(self, agent_id: str, command: str, timeout: int = 120, config: dict | None = None) -> dict:
        if command.strip().startswith(CLI_PREFIX):
            cli_args = command.strip()[len(CLI_PREFIX):]
            return await self._run(f"run --rm openclaw-cli {cli_args}", timeout=timeout)
        svc = self._svc(agent_id, config)
        return await self._run(f"exec -T {svc} sh -c '{command}'", timeout=timeout)

    async def health_check(self, agent_id: str, config: dict | None = None) -> bool:
        svc = self._svc(agent_id, config)
        result = await self._run(f"ps {svc} --format json")
        if result.get("exit_code") != 0:
            return False
        return '"running"' in result.get("stdout", "").lower()

    async def logs(self, agent_id: str, tail: int = 50, config: dict | None = None) -> str:
        svc = self._svc(agent_id, config)
        result = await self._run(f"logs --tail {tail} {svc}")
        return result.get("stdout", "") + result.get("stderr", "")


class KubernetesBackend(InfraBackend):
    """通过 kubectl 管理 Pod（保留，未来 K8s 迁移用）"""

    def __init__(self, config: dict):
        self.kubeconfig = config.get("kubeconfig", "~/.kube/config")
        self.namespace = config.get("namespace", "openclaw-agents")
        self.image = config.get("image", "openclaw/agent:latest")
        self.resources = config.get("resources", {"cpu": "2", "memory": "4Gi"})

    def _kubectl(self, args: str) -> list[str]:
        return ["kubectl", "--kubeconfig", self.kubeconfig, "-n", self.namespace] + args.split()

    async def _run_kubectl(self, args: str, timeout: int = 60) -> dict:
        proc = await asyncio.create_subprocess_exec(
            *self._kubectl(args),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {"exit_code": proc.returncode, "stdout": stdout.decode(), "stderr": stderr.decode()}
        except asyncio.TimeoutError:
            proc.kill()
            return {"exit_code": -1, "error": "timeout"}

    async def start_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        image = (config or {}).get("image", self.image)
        cpu = self.resources.get("cpu", "2")
        mem = self.resources.get("memory", "4Gi")
        pod_name = f"agent-{agent_id}"
        cmd = (
            f"run {pod_name} --image={image} --restart=Never "
            f"--env=AGENT_ID={agent_id} --env=AGENT_ROLE={role} "
            f"--requests=cpu={cpu},memory={mem} "
            f"--limits=cpu={cpu},memory={mem}"
        )
        result = await self._run_kubectl(cmd)
        if result.get("exit_code") != 0:
            return {"error": result.get("stderr", ""), "status": "start_failed"}
        return {"status": "starting", "agent_id": agent_id}

    async def stop_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        result = await self._run_kubectl(f"delete pod agent-{agent_id} --grace-period=30")
        if result.get("exit_code") != 0:
            return {"error": result.get("stderr", "")}
        return {"status": "stopped"}

    async def restart_agent(self, agent_id: str, role: str = "", config: dict | None = None) -> dict:
        await self.stop_agent(agent_id, role, config)
        return await self.start_agent(agent_id, role, config)

    async def destroy_agent(self, agent_id: str, container_id: str = "", config: dict | None = None) -> bool:
        result = await self._run_kubectl(f"delete pod agent-{agent_id} --grace-period=10")
        return result.get("exit_code") == 0

    async def exec_command(self, agent_id: str, command: str, timeout: int = 120, config: dict | None = None) -> dict:
        return await self._run_kubectl(f"exec agent-{agent_id} -- sh -c {command}", timeout=timeout)

    async def health_check(self, agent_id: str, config: dict | None = None) -> bool:
        result = await self._run_kubectl(f"get pod agent-{agent_id} -o jsonpath={{.status.phase}}")
        return result.get("stdout", "").strip() == "Running"

    async def logs(self, agent_id: str, tail: int = 50, config: dict | None = None) -> str:
        result = await self._run_kubectl(f"logs agent-{agent_id} --tail={tail}")
        return result.get("stdout", "")


_backend: InfraBackend | None = None


def init():
    """根据配置初始化基础设施后端（全局兜底）"""
    global _backend
    mode = openclaw_config.get_infra_mode()
    config = openclaw_config.get_infra_config()

    if mode == "ssh":
        _backend = SSHBackend(config)
    elif mode == "kubernetes":
        _backend = KubernetesBackend(config)
    else:
        _backend = DockerComposeBackend(config)

    logger.info(f"Infrastructure backend: {mode}")


def get_backend() -> InfraBackend:
    if not _backend:
        init()
    return _backend


def get_backend_for_agent(agent: "Agent", node: "InfraNode | None") -> tuple[InfraBackend, dict]:
    """
    按项目 infra_group 解析 Agent 的 infra 后端与 config。
    返回 (backend, agent_config)，agent_config 含 service_name、compose 路径等。
    """
    from app.core.config import settings
    import os

    if node:
        project_dir = f"{settings.AGENT_DEPLOY_ROOT}/{agent.project_id}/{agent.id}"
    else:
        project_dir = os.path.abspath(os.path.join(settings.PROJECTS_DIR, agent.project_id, agent.id))
    compose_file = f"{project_dir}/docker-compose.yml"
    svc = agent_service_name(agent.project_id, agent.role)
    agent_config = {**(agent.config or {}), "service_name": svc}

    if node:
        key_file = DISPATCHER_SHARED_SSH_KEY
        if not os.path.exists(key_file):
            logger.warning("Dispatcher shared SSH key missing: %s", key_file)
        cfg = {
            "host": node.host,
            "port": node.port,
            "user": node.user,
            "key_file": key_file,
            "project_dir": project_dir,
            "compose_file": compose_file,
        }
        return SSHBackend(cfg), agent_config

    mode = openclaw_config.get_infra_mode()
    global_cfg = openclaw_config.get_infra_config()
    if mode == "ssh":
        cfg = {**global_cfg, "project_dir": project_dir, "compose_file": compose_file}
        return SSHBackend(cfg), agent_config
    if mode == "kubernetes":
        return KubernetesBackend(global_cfg), agent_config
    cfg = {**global_cfg, "project_dir": project_dir, "compose_file": compose_file}
    return DockerComposeBackend(cfg), agent_config


# ── CC Worker 专用容器管理 ──

CC_WORKER_IMAGE = "harbor.vaiteam.cn/vaiteam/cc-worker:latest"
CC_WORKER_COMPOSE = "./docker-compose.agents.yml"
# CC Worker 网络：nil=自动检测 dispatcher 所在的 compose 网络
CC_WORKER_NETWORK: str | None = None


def _dispatcher_network() -> str:
    """获取 dispatcher 自身所在的 Docker 网络名（CC Worker 需接入同一网络）。

    通过网络名后缀 _default 匹配，如 vaiteam-private_default、openclaw-team-src_default。
    """
    import socket
    hostname = socket.gethostname()
    # 通过 docker inspect 获取当前容器的网络
    return "vaiteam-private_default"


def _cc_container_name(agent_id: str) -> str:
    """CC Worker 容器名，全局唯一。"""
    return f"vaiteam-cc-{agent_id}"


async def _docker_run(
    cmd: list[str],
    *,
    timeout: int = 120,
    cwd: str | None = None,
) -> dict:
    """本地执行 docker 命令，返回 {exit_code, stdout, stderr}。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {"exit_code": -1, "error": "timeout"}


async def _ssh_exec(
    node: "InfraNode",
    cmd: list[str],
    *,
    timeout: int = 120,
) -> dict:
    """通过 SSH 在远程节点上执行命令，返回 {exit_code, stdout, stderr}。"""
    import shlex

    key_file = DISPATCHER_SHARED_SSH_KEY
    user = (node.user or "").strip() or "root"
    port = node.port or 22

    ssh_cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "IdentitiesOnly=yes",
        "-i", key_file,
        "-p", str(port),
        f"{user}@{node.host}",
    ]
    remote_cmd = " ".join(shlex.quote(arg) for arg in cmd)
    ssh_cmd.append(remote_cmd)

    proc = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {"exit_code": -1, "error": "timeout"}


def _cc_env_args(
    agent_id: str,
    role: str,
    dispatcher_base: str = "",
    api_token: str = "",
    env_vars: dict | None = None,
) -> list[str]:
    """构造 CC Worker 的环境变量参数列表。"""
    base_env = {
        "AGENT_ID": agent_id,
        "AGENT_ROLE": role,
        "DISPATCHER_BASE": dispatcher_base or "http://dispatcher:8080",
        "AGENT_API_TOKEN": api_token or "",
    }
    if env_vars:
        base_env.update(env_vars)
    args = []
    for k, v in base_env.items():
        args.extend(["-e", f"{k}={v}"])
    return args


# ── CC Worker 镜像远程分发（scp docker save tar） ──

CC_WORKER_IMAGE_TAR_NAME = "cc-worker-image.tar"


def _find_cc_worker_tar() -> str | None:
    """查找本地预打包的 CC Worker 镜像 tar 文件。"""
    import os

    # 优先级：install 包内 > 打包目录 > 当前目录
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "images", CC_WORKER_IMAGE_TAR_NAME),
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "images", CC_WORKER_IMAGE_TAR_NAME),
        os.path.join("/opt", "vaiteam", "images", CC_WORKER_IMAGE_TAR_NAME),
        os.path.join("/app", "images", CC_WORKER_IMAGE_TAR_NAME),
    ]
    for p in candidates:
        rp = os.path.abspath(p)
        if os.path.isfile(rp):
            return rp
    return None


async def _distribute_cc_worker_image(
    node: "InfraNode",
    *,
    image: str = CC_WORKER_IMAGE,
    timeout: int = 600,
) -> dict:
    """确保远程节点上有 CC Worker 镜像，没有则通过 scp 分发预打包 tar。

    流程：
    1. 检查远程是否已有镜像
    2. 查找本地预打包 tar，没有则现场 docker save
    3. scp 传输到远程节点 /tmp/
    4. 远程 docker load
    5. 清理临时文件
    """
    result = {"ok": False, "action": "", "error": ""}

    # 1. 检查远程是否已有镜像
    check = await _ssh_exec(
        node, ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", image], timeout=30
    )
    if check.get("exit_code") == 0 and image in check.get("stdout", ""):
        result["ok"] = True
        result["action"] = "already_exists"
        return result

    # 2. 查找本地预打包 tar，没有则现场 docker save
    tar_local = _find_cc_worker_tar()
    use_temp = False
    if not tar_local:
        import tempfile

        fd, tar_local = tempfile.mkstemp(suffix=".tar", prefix="vaiteam-cc-worker-")
        os.close(fd)
        use_temp = True
        save_r = await _docker_run(["docker", "save", "-o", tar_local, image], timeout=300)
        if save_r.get("exit_code") != 0:
            os.unlink(tar_local)
            result["error"] = f"docker save failed: {save_r.get('stderr', '')}"
            return result

    # 3. scp 传输到远程
    key_file = DISPATCHER_SHARED_SSH_KEY
    user = (node.user or "").strip() or "root"
    port = node.port or 22
    remote_tar = f"/tmp/{CC_WORKER_IMAGE_TAR_NAME}"

    scp_cmd = [
        "scp", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "IdentitiesOnly=yes",
        "-i", key_file,
        "-P", str(port),
        tar_local,
        f"{user}@{node.host}:{remote_tar}",
    ]
    scp_r = await _docker_run(scp_cmd, timeout=timeout)
    if scp_r.get("exit_code") != 0:
        if use_temp:
            os.unlink(tar_local)
        result["error"] = f"scp failed: {scp_r.get('stderr', scp_r.get('error', ''))}"
        return result

    # 4. 远程 docker load
    load_r = await _ssh_exec(node, ["docker", "load", "-i", remote_tar], timeout=300)
    # 5. 清理远程临时文件
    await _ssh_exec(node, ["rm", "-f", remote_tar], timeout=30)
    # 清理本地临时文件
    if use_temp:
        os.unlink(tar_local)

    if load_r.get("exit_code") != 0:
        result["error"] = f"docker load failed: {load_r.get('stderr', '')}"
        return result

    result["ok"] = True
    result["action"] = "distributed"
    return result


async def _spawn_cc_worker_local(
    agent_id: str,
    role: str,
    *,
    dispatcher_base: str = "",
    api_token: str = "",
    env_vars: dict | None = None,
) -> dict:
    """本地 Docker 启动 CC Worker。"""
    result: dict = {"ok": False, "container_id": "", "error": ""}
    container_name = _cc_container_name(agent_id)

    # 检查是否已存在
    check = await _docker_run(["docker", "ps", "-aq", "-f", f"name={container_name}"])
    existing = check.get("stdout", "").strip()
    if existing:
        start_r = await _docker_run(["docker", "start", container_name])
        if start_r.get("exit_code") == 0:
            result["ok"] = True
            result["container_id"] = existing
            logger.info(f"CC Worker {agent_id} already exists, started: {existing[:12]}")
            return result
        await _docker_run(["docker", "rm", "-f", container_name])

    # 尝试 docker compose up
    compose_svc = f"cc-{role}"
    import os
    if os.path.isfile(CC_WORKER_COMPOSE):
        compose_r = await _docker_run(
            ["docker", "compose", "-f", CC_WORKER_COMPOSE, "up", "-d", compose_svc],
            timeout=60,
        )
        if compose_r.get("exit_code") == 0:
            ps_r = await _docker_run(
                ["docker", "compose", "-f", CC_WORKER_COMPOSE, "ps", "-q", compose_svc]
            )
            cid = ps_r.get("stdout", "").strip().split("\n")[0]
            if cid:
                await _docker_run(["docker", "rename", cid, container_name])
                result["ok"] = True
                result["container_id"] = cid
                logger.info(f"CC Worker {agent_id} spawned via compose: {cid[:12]}")
                return result

    # Fallback: docker run
    env_list = _cc_env_args(agent_id, role, dispatcher_base, api_token, env_vars)
    network = CC_WORKER_NETWORK or _dispatcher_network()
    run_cmd = [
        "docker", "run", "-d", "--name", container_name,
        "--network", network,
        "--restart", "unless-stopped",
        *env_list,
        CC_WORKER_IMAGE,
    ]
    run_r = await _docker_run(run_cmd, timeout=60)
    if run_r.get("exit_code") == 0:
        cid = run_r.get("stdout", "").strip()
        result["ok"] = True
        result["container_id"] = cid
        logger.info(f"CC Worker {agent_id} spawned via docker run: {cid[:12]}")
    else:
        result["error"] = f"docker run failed: {run_r.get('stderr', run_r.get('error', 'unknown'))}"
        logger.error(f"Failed to spawn CC Worker {agent_id}: {result['error']}")

    return result


async def _spawn_cc_worker_remote(
    agent_id: str,
    role: str,
    node: "InfraNode",
    *,
    dispatcher_base: str = "",
    api_token: str = "",
    env_vars: dict | None = None,
) -> dict:
    """通过 SSH 在远程节点上启动 CC Worker 容器。"""
    result: dict = {"ok": False, "container_id": "", "error": "", "node_id": node.id}
    container_name = _cc_container_name(agent_id)

    # 1. 检查远程是否已存在该容器
    check = await _ssh_exec(node, ["docker", "ps", "-aq", "-f", f"name={container_name}"], timeout=30)
    existing = check.get("stdout", "").strip()
    if existing:
        start_r = await _ssh_exec(node, ["docker", "start", container_name], timeout=30)
        if start_r.get("exit_code") == 0:
            result["ok"] = True
            result["container_id"] = existing
            logger.info(f"CC Worker {agent_id} on {node.host} already exists, started")
            return result
        await _ssh_exec(node, ["docker", "rm", "-f", container_name], timeout=30)

    # 2. 确保远程节点有镜像（没有则 scp 分发）
    dist_r = await _distribute_cc_worker_image(node)
    if not dist_r.get("ok"):
        result["error"] = f"image distribution failed: {dist_r.get('error', '')}"
        logger.error(f"Failed to distribute CC Worker image to {node.host}: {result['error']}")
        return result
    if dist_r.get("action") == "distributed":
        logger.info(f"CC Worker image distributed to {node.host}")

    # 3. 远程 docker run 启动
    env_list = _cc_env_args(agent_id, role, dispatcher_base, api_token, env_vars)
    run_cmd = [
        "docker", "run", "-d", "--name", container_name,
        "--restart", "unless-stopped",
        *env_list,
        CC_WORKER_IMAGE,
    ]
    run_r = await _ssh_exec(node, run_cmd, timeout=60)
    if run_r.get("exit_code") == 0:
        cid = run_r.get("stdout", "").strip()
        result["ok"] = True
        result["container_id"] = cid
        logger.info(f"CC Worker {agent_id} spawned on remote {node.host}: {cid[:12]}")
    else:
        result["error"] = f"remote docker run failed on {node.host}: {run_r.get('stderr', run_r.get('error', 'unknown'))}"
        logger.error(f"Failed to spawn CC Worker {agent_id} on {node.host}: {result['error']}")

    return result


async def spawn_cc_worker(
    agent_id: str,
    role: str,
    *,
    project_id: str = "",
    dispatcher_base: str = "",
    api_token: str = "",
    env_vars: dict | None = None,
    node: "InfraNode | None" = None,
) -> dict:
    """启动 CC Worker 容器。

    策略：
    - 若传入 node，通过 SSH 在远程节点上启动
    - 否则，本地 Docker 启动

    返回 {"ok": bool, "container_id": str, "error": str, "node_id": str}
    """
    if node:
        return await _spawn_cc_worker_remote(
            agent_id, role, node,
            dispatcher_base=dispatcher_base,
            api_token=api_token,
            env_vars=env_vars,
        )
    return await _spawn_cc_worker_local(
        agent_id, role,
        dispatcher_base=dispatcher_base,
        api_token=api_token,
        env_vars=env_vars,
    )


async def destroy_cc_worker(agent_id: str, node: "InfraNode | None" = None) -> dict:
    """销毁 CC Worker 容器。"""
    container_name = _cc_container_name(agent_id)
    if node:
        result = await _ssh_exec(node, ["docker", "rm", "-f", container_name], timeout=30)
    else:
        result = await _docker_run(["docker", "rm", "-f", container_name], timeout=30)
    ok = result.get("exit_code") == 0
    if ok:
        logger.info(f"CC Worker {agent_id} destroyed")
    else:
        logger.warning(f"Failed to destroy CC Worker {agent_id}: {result.get('stderr', '')}")
    return {"ok": ok, "error": result.get("stderr", "")}


async def get_cc_worker_status(agent_id: str, node: "InfraNode | None" = None) -> dict:
    """获取 CC Worker 容器状态。"""
    container_name = _cc_container_name(agent_id)
    if node:
        result = await _ssh_exec(
            node,
            ["docker", "inspect", "--format", "{{.State.Status}}|{{.Id}}", container_name],
            timeout=10,
        )
    else:
        result = await _docker_run(
            ["docker", "inspect", "--format", "{{.State.Status}}|{{.Id}}", container_name],
            timeout=10,
        )
    if result.get("exit_code") != 0:
        return {"running": False, "status": "not_found", "error": result.get("stderr", "")}

    parts = result.get("stdout", "").strip().split("|")
    status = parts[0] if parts else "unknown"
    cid = parts[1] if len(parts) > 1 else ""
    return {
        "running": status == "running",
        "status": status,
        "container_id": cid,
    }


async def spawn_agent_container(
    agent_id: str,
    role: str,
    project_id: str,
    node: "InfraNode | None" = None,
) -> dict:
    """统一入口：按 agent 类型选择 spawn 策略。

    - CC Worker 角色 (senior/mid/junior/architect/devops/tester): 走 CC Worker 容器
    - OpenClaw/connector 角色: 走既有 InfraBackend
    """
    from app.core.config import settings

    dispatcher_base = getattr(settings, "DISPATCHER_PUBLIC_BASE_URL", "")
    if not dispatcher_base:
        # 本地部署使用内部地址，远程部署必须有公网地址
        dispatcher_base = "http://dispatcher:8080"
        logger.warning("DISPATCHER_PUBLIC_BASE_URL not configured, using internal address. Remote workers may fail to connect.")

    if role in ("senior", "mid", "junior", "architect", "devops", "tester", "archaeologist"):
        return await spawn_cc_worker(
            agent_id=agent_id,
            role=role,
            project_id=project_id,
            dispatcher_base=dispatcher_base,
            node=node,
        )

    # Fallback: 既有 OpenClaw 路径
    backend = get_backend()
    return await backend.start_agent(agent_id, role)
