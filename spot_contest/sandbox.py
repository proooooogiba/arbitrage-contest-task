import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .models import Config, dumps


class StrategyProcess:
    """Bounded JSONL RPC, with a Docker boundary for competition submissions.

    subprocess mode is deliberately labelled local-only: it is not a sandbox.
    """

    def __init__(
        self,
        submission: Path,
        config: Config,
        runner: str = "docker",
        image: str = "spot-contest-strategy:0.1.0",
        runtime: str = "",
    ):
        self.config = config
        self.container = None
        self.image_id = None
        self.process = None
        self.selector = None
        self.buffer = b""
        self.started = time.monotonic()
        root = submission.resolve()
        if not (root / "strategy.py").is_file():
            raise ValueError("submission must contain strategy.py")
        if runner == "docker":
            if not shutil.which("docker"):
                raise ValueError(
                    "Docker is not installed; subprocess mode is for trusted local development only"
                )
            try:
                inspection = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", image],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError("could not inspect strategy Docker image") from exc
            if inspection.returncode != 0 or not inspection.stdout.strip().startswith("sha256:"):
                raise ValueError(
                    "strategy image is not available locally; build or pull it before evaluation"
                )
            self.image_id = inspection.stdout.strip()
            self.container = "spot-strategy-" + uuid.uuid4().hex
            command = [
                "docker",
                "run",
                "--rm",
                "--name",
                self.container,
                "-i",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                "65534:65534",
                "--memory",
                str(config.sandbox_memory_mb) + "m",
                "--memory-swap",
                str(config.sandbox_memory_mb) + "m",
                "--cpus",
                str(config.sandbox_cpus),
                "--pids-limit",
                "64",
                "--ulimit",
                "nofile=256:256",
                "--log-driver",
                "none",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--mount",
                "type=bind,source=%s,target=/submission,readonly" % root,
            ]
            if runtime:
                command += ["--runtime", runtime]
            # Run the resolved immutable image, not a mutable tag.
            command += [self.image_id]
        elif runner == "subprocess":
            command = [
                sys.executable,
                "-I",
                "-u",
                str(Path(__file__).with_name("participant_runner.py")),
                str(root),
            ]
        else:
            raise ValueError("unknown runner")
        self.selector = selectors.DefaultSelector()
        try:
            # Do not forward the judge's credentials/environment to a local strategy.
            self.process = subprocess.Popen(
                command,
                cwd=str(root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                start_new_session=True,
                bufsize=0,
            )
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
        except Exception:
            self.close()
            raise

    def request(self, message):
        deadline = min(
            time.monotonic() + self.config.callback_timeout_s,
            self.started + self.config.max_run_seconds,
        )
        data = dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(data) > self.config.max_message_bytes:
            raise ValueError("judge request exceeds protocol limit")
        # Nonblocking writes also prevent a strategy that stops reading from hanging the judge.
        writer = selectors.DefaultSelector()
        try:
            writer.register(self.process.stdin, selectors.EVENT_WRITE)
            offset = 0
            while offset < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writer.select(remaining):
                    raise TimeoutError("strategy callback/run timed out while sending")
                try:
                    offset += os.write(self.process.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    continue
        finally:
            writer.close()
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("strategy callback/run timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError("strategy exited before returning a response")
            self.buffer += chunk
            if len(self.buffer) > self.config.max_message_bytes:
                raise ValueError("strategy response exceeds protocol limit")
        line, self.buffer = self.buffer.split(b"\n", 1)
        if self.buffer:
            raise ValueError("strategy returned unsolicited protocol data")
        try:
            response = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("strategy response is not valid JSON")
        if not isinstance(response, dict) or set(response) != {"orders"}:
            raise ValueError("strategy response must contain only orders")
        return response["orders"]

    def close(self):
        if self.container:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", self.container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.process:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    if self.process.poll() is None:
                        self.process.kill()
            self.process.wait(timeout=10)
            self.process.stdin.close()
            self.process.stdout.close()
        if self.selector:
            self.selector.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
