# The docker SDK is only partially typed; its members stay confined to this module.
# pyright: reportUnknownMemberType=false
"""Sandbox: isolated Docker environment in which agents try to get a certificate.

Layout::

    sandbox network (internal, no internet)     control network (internet)
    ┌─────────────┐     ┌──────────────────────────────┐     ┌──────────────┐
    │ agent box   │ ──▶ │ caddy                        │ ──▶ │ web          │
    │ (fresh/run) │     │  evil-captcha.org  (HTTPS)   │     │  site + admin│
    └─────────────┘     │  llm-gateway:8080  (API key) │ ──▶ openrouter.ai
                        └──────────────────────────────┘     └──────────────┘

The agent box can only reach caddy. Caddy's network aliases make the domains
resolve through Docker's DNS, so nothing unusual shows up in /etc/hosts.
The admin API is published on the host's loopback only.
"""

import contextlib
import io
import logging
import tarfile
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
from docker.errors import BuildError, NotFound
from docker.models.containers import Container

import docker

log = logging.getLogger(__name__)

PREFIX = "evil-captcha"
SANDBOX_NET = f"{PREFIX}-sandbox"
CONTROL_NET = f"{PREFIX}-control"
WEB = f"{PREFIX}-web"
CADDY = f"{PREFIX}-caddy"
CADDY_DATA = f"{PREFIX}-caddy-data"
WEB_IMAGE = f"{PREFIX}-web:latest"
AGENT_IMAGE = f"{PREFIX}-agent:latest"
CADDY_IMAGE = "caddy:2"
SITE_DOMAIN = "evil-captcha.org"
GATEWAY_HOST = "llm-gateway"
ROOT_CA_PATH = "/data/caddy/pki/authorities/local/root.crt"
AGENT_USER = "user"
AGENT_HOME = "/home/user"
DOCKER_POOL_SIZE = 64  # each running agent holds one Docker API connection for its whole run


class SandboxError(Exception):
    """The sandbox could not be set up or used."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class AgentBox:
    """A fresh, isolated Linux container for one agent run."""

    def __init__(self, container: Container) -> None:
        self._container = container
        container.reload()
        networks = cast(dict[str, Any], container.attrs["NetworkSettings"]["Networks"])
        self.ip = str(networks[SANDBOX_NET]["IPAddress"])

    def exec(self, command: list[str], env: dict[str, str] | None = None) -> ExecResult:
        result = self._container.exec_run(
            command, user=AGENT_USER, workdir=AGENT_HOME, environment=env, demux=True
        )
        stdout, stderr = cast(tuple[bytes | None, bytes | None], result.output)
        return ExecResult(
            exit_code=cast(int, result.exit_code),
            stdout=(stdout or b"").decode(errors="replace"),
            stderr=(stderr or b"").decode(errors="replace"),
        )

    def put_file(self, path: str, content: str) -> None:
        """Write a file owned by the agent user, creating parent directories."""
        target = Path(path)
        self.exec(["mkdir", "-p", str(target.parent)])
        data = content.encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(target.name)
            info.size, info.uid, info.gid, info.mode = len(data), 1000, 1000, 0o644
            tar.addfile(info, io.BytesIO(data))
        if not self._container.put_archive(str(target.parent), buffer.getvalue()):
            raise SandboxError(f"could not write {path} into the agent box")

    def read_file(self, path: str) -> bytes | None:
        """The file's content, or None if there is no regular file at ``path``."""
        try:
            chunks, _ = self._container.get_archive(path)
        except NotFound:
            return None
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as tar:
            member = tar.next()
            content = tar.extractfile(member) if member and member.isfile() else None
            return content.read() if content else None


class Sandbox:
    def __init__(
        self, project_dir: Path, api_key: str, tasks_file: Path, env: dict[str, str]
    ) -> None:
        self._client = docker.from_env(max_pool_size=DOCKER_POOL_SIZE)
        self._project_dir = project_dir.resolve()
        self._api_key = api_key
        self._tasks_file = tasks_file.resolve()
        self._env = env
        self.admin_url = "http://127.0.0.1:8001"

    def up(self) -> None:
        """Build images and start the site and caddy. Replaces running instances."""
        self.down()
        self._network(SANDBOX_NET, internal=True)
        self._network(CONTROL_NET, internal=False)
        self._build(WEB_IMAGE, self._project_dir, "docker/web.Dockerfile")
        self._start_web()
        self._client.images.pull(CADDY_IMAGE)
        self._start_caddy()
        self._build_agent_image(self._wait_for_root_ca())
        self._wait_for_admin()
        log.info("sandbox is up")

    def down(self) -> None:
        for name in (CADDY, WEB):
            self._remove_container(name)
        for container in self._client.containers.list(
            all=True, filters={"label": f"{PREFIX}.role=agent"}
        ):
            container.remove(force=True)
        for name in (SANDBOX_NET, CONTROL_NET):
            with contextlib.suppress(NotFound):
                self._client.networks.get(name).remove()

    @contextmanager
    def agent_box(self) -> Generator[AgentBox]:
        container = self._client.containers.run(
            AGENT_IMAGE,
            detach=True,
            network=SANDBOX_NET,
            hostname="workstation",
            labels={f"{PREFIX}.role": "agent"},
            mem_limit="2g",
            nano_cpus=2_000_000_000,
            pids_limit=512,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )
        try:
            yield AgentBox(container)
        finally:
            container.remove(force=True)

    def _network(self, name: str, internal: bool) -> None:
        self._client.networks.create(name, driver="bridge", internal=internal)

    def _build(self, tag: str, context: Path, dockerfile: str) -> None:
        log.info("building %s", tag)
        try:
            self._client.images.build(path=str(context), dockerfile=dockerfile, tag=tag, rm=True)
        except BuildError as error:
            log_chunks = cast(list[dict[str, Any]], list(error.build_log))
            output = "".join(str(chunk.get("stream", "")) for chunk in log_chunks)
            raise SandboxError(f"building {tag} failed: {error}\n{output[-3000:]}") from error

    def _start_web(self) -> None:
        self._client.containers.run(
            WEB_IMAGE,
            name=WEB,
            detach=True,
            network=CONTROL_NET,
            environment={
                **self._env,
                "OPENROUTER_API_KEY": self._api_key,
                "TASKS_FILE": "/app/tasks.toml",
            },
            volumes={str(self._tasks_file): {"bind": "/app/tasks.toml", "mode": "ro"}},
            ports={"8001/tcp": ("127.0.0.1", 8001)},
        )
        self._client.networks.get(CONTROL_NET).reload()

    def _start_caddy(self) -> None:
        caddyfile = self._project_dir / "docker" / "Caddyfile"
        container = self._client.containers.create(
            CADDY_IMAGE,
            name=CADDY,
            environment={"OPENROUTER_API_KEY": self._api_key},
            volumes={
                str(caddyfile): {"bind": "/etc/caddy/Caddyfile", "mode": "ro"},
                CADDY_DATA: {"bind": "/data", "mode": "rw"},
            },
            network=CONTROL_NET,
        )
        self._client.networks.get(SANDBOX_NET).connect(
            container, aliases=[SITE_DOMAIN, GATEWAY_HOST]
        )
        container.start()

    def _wait_for_root_ca(self, timeout: float = 30) -> str:
        caddy = self._client.containers.get(CADDY)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = caddy.exec_run(["cat", ROOT_CA_PATH])
            if result.exit_code == 0:
                return cast(bytes, result.output).decode()
            time.sleep(0.5)
        raise SandboxError(f"caddy did not create its root CA: {caddy.logs(tail=30).decode()}")

    def _build_agent_image(self, root_ca: str) -> None:
        with tempfile.TemporaryDirectory() as context:
            dockerfile = (self._project_dir / "docker" / "agent.Dockerfile").read_text()
            (Path(context) / "Dockerfile").write_text(dockerfile)
            (Path(context) / "root-ca.crt").write_text(root_ca)
            self._build(AGENT_IMAGE, Path(context), "Dockerfile")

    def _wait_for_admin(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{self.admin_url}/docs", timeout=2).raise_for_status()
                return
            except httpx.HTTPError:
                time.sleep(0.5)
        web = self._client.containers.get(WEB)
        raise SandboxError(f"site did not start: {web.logs(tail=30).decode()}")

    def _remove_container(self, name: str) -> None:
        with contextlib.suppress(NotFound):
            self._client.containers.get(name).remove(force=True)
