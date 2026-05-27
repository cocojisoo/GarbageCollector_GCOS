"""DockerSandboxRunner — real isolation.

Each `run_python(code)` call:

    docker run --rm \\
        --network=none \\
        --read-only \\
        --cap-drop=ALL \\
        --security-opt=no-new-privileges \\
        --memory=128m --memory-swap=128m \\
        --pids-limit=64 \\
        --cpus=1 \\
        --tmpfs /work:rw,size=8m,exec \\
        -w /work \\
        python:3.11-slim \\
        timeout 5s python /work/main.py

The host-side wrapper writes `code` to a host temp file, then copies it into
the container's tmpfs via `docker cp`. The container has no network, no
writable host paths, no Linux capabilities, no privilege escalation, capped
memory + CPU + PID count, and a wall-clock kill via `timeout(1)` inside.

The Python `docker` SDK is an optional dependency. We import it lazily so
`from gcos.sandbox import ...` works even on machines without it installed.

The `is_available()` classmethod is the entry condition the sandbox factory
uses to decide whether to pick this or fall back to SubprocessSandboxRunner.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from gcos.sandbox.runner import SandboxResult, SandboxRunner


log = logging.getLogger(__name__)


DEFAULT_IMAGE = "python:3.11-slim"


class DockerNotAvailable(RuntimeError):
    pass


class DockerSandboxRunner(SandboxRunner):
    name = "docker"

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        memory: str = "128m",
        pids_limit: int = 64,
        cpus: float = 1.0,
        tmpfs_size: str = "8m",
    ) -> None:
        self.image = image
        self.memory = memory
        self.pids_limit = pids_limit
        self.cpus = cpus
        self.tmpfs_size = tmpfs_size
        self._client = self._connect()

    # --- factory bits ------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        """Cheap check used by sandbox factory. No side effects."""
        try:
            import docker  # noqa: F401
        except Exception:
            return False
        try:
            client = _new_client()
            client.ping()
            return True
        except Exception:
            return False

    @staticmethod
    def _connect():
        try:
            return _new_client()
        except Exception as e:
            raise DockerNotAvailable(
                "Docker daemon not reachable. Install Docker Desktop and run it, "
                "or use SubprocessSandboxRunner for tests."
            ) from e

    # --- main entry --------------------------------------------------------

    def run_python(self, code: str, *, timeout: float = 5.0) -> SandboxResult:
        import docker  # type: ignore  # noqa: F401  (proves availability)

        start = time.monotonic()
        killed = False
        stdout = ""
        stderr = ""
        exit_code = -1
        container = None

        try:
            # We use a tmpfs-backed /work and pipe the script in via stdin
            # using `python -c` is awkward for multi-line; safer is to
            # `docker run` with the script as the command argument fed via heredoc.
            # The Docker SDK's `containers.run` doesn't allow heredocs directly,
            # so we use exec_create after starting a sleeper. Simpler approach:
            # encode the script into a base64 blob and decode inside.
            import base64
            blob = base64.b64encode(code.encode("utf-8")).decode("ascii")
            shell_cmd = (
                "python -c \""
                "import base64,os,sys;"
                "src=base64.b64decode('" + blob + "');"
                "open('/work/main.py','wb').write(src);"
                "os.execvp('python',['python','/work/main.py'])\""
            )

            container = self._client.containers.run(
                image=self.image,
                command=["sh", "-c", shell_cmd],
                detach=True,
                network_mode="none",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                mem_limit=self.memory,
                memswap_limit=self.memory,
                pids_limit=self.pids_limit,
                nano_cpus=int(self.cpus * 1_000_000_000),
                tmpfs={"/work": f"rw,size={self.tmpfs_size},exec"},
                working_dir="/work",
                stdout=True,
                stderr=True,
                remove=False,
            )

            try:
                result = container.wait(timeout=timeout)
                exit_code = int(result.get("StatusCode", -1))
            except Exception:
                # SDK raises various timeout errors depending on backend; treat all as timeout
                killed = True
                exit_code = -1
                try:
                    container.kill()
                except Exception:
                    pass

            try:
                stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
                stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass

        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_s=time.monotonic() - start,
            killed_by_timeout=killed,
            runner=self.name,
        )


def _new_client(timeout: float = 2.0):
    import docker  # lazy
    return docker.from_env(timeout=timeout)
