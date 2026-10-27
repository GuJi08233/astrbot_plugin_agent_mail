"""Bounded, shell-free access to the official Tencent Agent Mail CLI."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import signal
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

MAX_OUTPUT = 4 * 1024 * 1024
STDERR_LIMIT = 64 * 1024


class AgentlyError(RuntimeError):
    """Report a CLI failure without treating a failed operation as successful.

    Args:
        message: Human-readable failure description.
        exit_code: CLI exit code, when available.
        payload: Parsed CLI error envelope, when available.
    """

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.payload = payload


class AgentlyClient:
    """Run the CLI without shell interpolation or implicit write retries.

    Args:
        cli_path: Executable path, or an empty string to search PATH.
        timeout: Maximum duration of a regular command, in seconds.
        install_dir: Optional plugin-owned storage for the managed native CLI.
        auto_install: Whether a missing CLI may be downloaded to install_dir.
    """

    def __init__(
        self,
        cli_path: str = "",
        timeout: float = 60,
        *,
        install_dir: Path | None = None,
        auto_install: bool = True,
    ) -> None:
        self.cli_path = cli_path.strip()
        self.timeout = max(float(timeout), 1.0)
        self._processes: set[asyncio.subprocess.Process] = set()
        self._closed = False
        self.install_dir = Path(install_dir) if install_dir is not None else None
        self.auto_install = auto_install is True
        self._managed_executable: Path | None = None
        self._prepare_lock = asyncio.Lock()
        self._prepare_token: object | None = None
        self._install_cancel = threading.Event()
        self._runtime_state = {
            "status": "idle",
            "message": "Checking the official Agent Mail CLI.",
            "path": "",
            "version": "",
            "source": "",
        }

    @property
    def runtime_state(self) -> dict[str, str]:
        """Return public dependency preparation status without mailbox credentials.

        Returns:
            An independent status, message, executable path, and version snapshot.
        """
        return dict(self._runtime_state)

    async def ensure_available(self, *, retry: bool = False) -> str:
        """Find a configured CLI or prepare one managed, verified local copy.

        Args:
            retry: Whether to retry a previous automatic preparation failure.

        Returns:
            The usable executable path; concurrent callers share one installation.

        Raises:
            AgentlyError: If configuration, platform, or dependency preparation fails.
        """
        async with self._prepare_lock:
            if self._closed:
                raise AgentlyError("The Agent Mail client is closed.")
            try:
                executable = self._executable()
            except AgentlyError as missing:
                if self.cli_path or self.install_dir is None:
                    self._runtime_state.update(status="failed", message=str(missing))
                    raise
                from .runtime import (
                    CLI_VERSION,
                    RuntimeInstallError,
                    get_managed_cli_path,
                    install_cli,
                )

                try:
                    cached = get_managed_cli_path(self.install_dir)
                    if (
                        cached.is_file()
                        and not cached.is_symlink()
                        and cached.stat().st_size > 0
                        and cached.resolve().is_relative_to(self.install_dir.resolve())
                    ):
                        self._managed_executable = cached.resolve()
                    elif not self.auto_install:
                        message = "Automatic CLI installation is disabled. Install @tencent-qqmail/agently-cli or configure cli_path."
                        self._runtime_state.update(status="disabled", message=message)
                        raise AgentlyError(message)
                    else:
                        if self._runtime_state["status"] == "failed" and not retry:
                            raise AgentlyError(self._runtime_state["message"])
                        self._runtime_state.update(
                            status="checking",
                            message="Preparing the official native Agent Mail CLI.",
                            version=CLI_VERSION,
                        )
                        attempt = self._prepare_token = object()
                        loop = asyncio.get_running_loop()

                        def progress(phase: str) -> None:
                            # Registry IO runs in a worker; only mutate UI state on this loop.
                            messages = {
                                "metadata": "Checking the official npm package metadata.",
                                "download": "Downloading the official Agent Mail CLI.",
                                "verify": "Verifying the CLI package integrity.",
                                "extract": "Preparing the native CLI executable.",
                                "ready": "The CLI download has completed.",
                            }

                            def apply_progress() -> None:
                                """Apply only progress belonging to an active preparation."""
                                if (
                                    not self._closed
                                    and self._prepare_token is attempt
                                    and self._runtime_state["status"]
                                    in {"checking", "downloading"}
                                ):
                                    self._runtime_state.update(
                                        status="downloading",
                                        message=messages.get(phase, phase),
                                    )

                            if not loop.is_closed():
                                loop.call_soon_threadsafe(apply_progress)

                        self._managed_executable = await install_cli(
                            self.install_dir,
                            cancel_event=self._install_cancel,
                            progress=progress,
                        )
                    executable = self._executable()
                    self._runtime_state.update(version=CLI_VERSION)
                except RuntimeInstallError as exc:
                    message = f"Automatic Agent Mail CLI preparation failed: {exc} Retry in the mailbox management page, or configure cli_path."
                    self._runtime_state.update(status="failed", message=message)
                    raise AgentlyError(message) from exc
                except asyncio.CancelledError:
                    if not self._closed:
                        self._runtime_state.update(
                            status="idle", message="CLI preparation was cancelled."
                        )
                        self._install_cancel = threading.Event()
                    raise
            if self._closed:
                raise AgentlyError("The Agent Mail client is closed.")
            source = (
                "configured"
                if self.cli_path
                else "managed"
                if self._managed_executable
                else "path"
            )
            self._runtime_state.update(
                status="ready",
                message="Agent Mail CLI is ready.",
                path=executable,
                source=source,
            )
            if source != "managed":
                self._runtime_state["version"] = ""
            return executable

    def _executable(self) -> str:
        """Resolve npm shims to native binaries, especially on Windows.

        Returns:
            An executable path safe to pass to create_subprocess_exec.

        Raises:
            AgentlyError: If the CLI is absent or only an unsafe shim is found.
        """
        if not self.cli_path and self._managed_executable is not None:
            if self._managed_executable.is_file():
                return str(self._managed_executable)
            self._managed_executable = None
        executable = shutil.which(self.cli_path or "agently-cli")
        if executable is None and self.cli_path:
            candidate = Path(self.cli_path).expanduser()
            if candidate.is_file():
                executable = str(candidate.resolve())
        if executable is None:
            raise AgentlyError(
                "Agent Mail CLI was not found. Install "
                "@tencent-qqmail/agently-cli in the AstrBot environment, "
                "or configure cli_path."
            )

        path = Path(executable).resolve()
        system = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(
            sys.platform
        )
        architecture = {
            "amd64": "x64",
            "x86_64": "x64",
            "arm64": "arm64",
            "aarch64": "arm64",
        }.get(platform.machine().lower())
        if system and architecture:
            package = f"agently-cli-{system}-{architecture}"
            binary = "agently-cli.exe" if system == "win32" else "agently-cli"
            # npm global shims, local .bin shims, and resolved scripts/run.js
            # all have a node_modules directory among these nearby locations.
            for parent in list(path.parents)[:6]:
                candidates = (
                    parent
                    / "node_modules"
                    / "@tencent-qqmail"
                    / package
                    / "bin"
                    / binary,
                    parent / "@tencent-qqmail" / package / "bin" / binary,
                    parent / package / "bin" / binary,
                )
                for candidate in candidates:
                    if candidate.is_file():
                        return str(candidate.resolve())
        if path.suffix.lower() in {".cmd", ".bat", ".ps1", ".js"}:
            raise AgentlyError(
                "The Agent Mail npm shim has no installed native package. "
                "Reinstall the CLI with optional dependencies enabled, or set "
                "cli_path to its platform-specific native executable."
            )
        return str(path)

    async def _spawn(
        self, args: list[str], cwd: Path | None = None
    ) -> asyncio.subprocess.Process:
        """Create a tracked process in a process group on POSIX.

        Args:
            args: Already separated CLI arguments.
            cwd: Optional command working directory.

        Returns:
            The tracked process.

        Raises:
            AgentlyError: If the client is closed or the CLI cannot start.
        """
        if self._closed:
            raise AgentlyError("The Agent Mail client is closed.")
        try:
            executable = await self.ensure_available()
            process = await asyncio.create_subprocess_exec(
                executable,
                *args,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_OUTPUT,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as exc:
            raise AgentlyError(f"Could not start Agent Mail CLI: {exc}") from exc
        self._processes.add(process)
        if self._closed:
            await self._stop(process)
            raise AgentlyError("The Agent Mail client is closed.")
        return process

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        """Terminate and reap a tracked command, including POSIX descendants.

        Args:
            process: Process created by this client.
        """
        try:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
        finally:
            self._processes.discard(process)

    async def _read_output(
        self, stream: asyncio.StreamReader, *, tail: bool = False
    ) -> bytes:
        """Drain a pipe while bounding retained output.

        Args:
            stream: Process output stream.
            tail: Whether to retain only the stderr tail instead of rejecting overflow.

        Returns:
            Bounded output bytes.

        Raises:
            AgentlyError: If regular command output exceeds the limit.
        """
        output = bytearray()
        while chunk := await stream.read(65536):
            output.extend(chunk)
            if tail:
                del output[:-STDERR_LIMIT]
            elif len(output) > MAX_OUTPUT:
                raise AgentlyError("Agent Mail CLI output exceeded the 4 MiB limit.")
        return bytes(output)

    def _payload(self, output: bytes, error: bytes, exit_code: int) -> dict[str, Any]:
        """Validate a regular command's JSON envelope.

        Args:
            output: CLI stdout.
            error: Bounded CLI stderr.
            exit_code: Actual process exit code.

        Returns:
            The complete successful JSON envelope.

        Raises:
            AgentlyError: For every nonzero exit or malformed response.
        """
        try:
            payload = json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if exit_code != 0 or (isinstance(payload, dict) and payload.get("ok") is False):
            message = "Agent Mail CLI failed."
            if isinstance(payload, dict):
                detail = payload.get("error")
                if isinstance(detail, dict) and isinstance(detail.get("message"), str):
                    message = detail["message"]
            elif error:
                message = error.decode("utf-8", errors="replace")[-2000:].strip()
            raise AgentlyError(
                message,
                exit_code=exit_code,
                payload=payload if isinstance(payload, dict) else None,
            )
        if not isinstance(payload, dict):
            raise AgentlyError("Agent Mail CLI returned an invalid JSON object.")
        return payload

    async def run(self, args: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
        """Execute one bounded CLI request without retries.

        Args:
            args: Command and flags, each supplied as a separate argument.
            cwd: Working directory for CLI-relative attachment and body paths.

        Returns:
            The complete JSON envelope.

        Raises:
            AgentlyError: If the request fails, times out, or returns invalid output.
        """
        process = await self._spawn(args, cwd)
        tasks = [
            asyncio.create_task(self._read_output(process.stdout)),
            asyncio.create_task(self._read_output(process.stderr, tail=True)),
            asyncio.create_task(process.wait()),
        ]
        try:
            try:
                output, error, code = await asyncio.wait_for(
                    asyncio.gather(*tasks), timeout=self.timeout
                )
            except asyncio.TimeoutError as exc:
                raise AgentlyError(
                    f"Agent Mail CLI timed out after {self.timeout:g} seconds."
                ) from exc
            return self._payload(output, error, code)
        finally:
            await self._stop(process)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def watch(self) -> AsyncIterator[dict[str, Any]]:
        """Yield full new-mail events until canceled or the CLI exits.

        Yields:
            Parsed NDJSON event objects from the official CLI.

        Raises:
            AgentlyError: If the stream is malformed, ends, or authorization fails.
        """
        process = await self._spawn(["message", "+watch", "--msg-format", "full"])
        errors = asyncio.create_task(self._read_output(process.stderr, tail=True))
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as exc:
                    raise AgentlyError(
                        "Agent Mail notification exceeded 4 MiB."
                    ) from exc
                if not line:
                    break
                if not line.strip():
                    continue
                if len(line) > MAX_OUTPUT:
                    raise AgentlyError("Agent Mail notification exceeded 4 MiB.")
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AgentlyError(
                        "Agent Mail returned invalid notification JSON."
                    ) from exc
                if not isinstance(event, dict):
                    raise AgentlyError("Agent Mail notification must be a JSON object.")
                if event.get("ok") is False:
                    try:
                        code = await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        code = 1
                    self._payload(line, b"", code or 1)
                yield event
            try:
                code = await asyncio.wait_for(process.wait(), timeout=2)
                error = await asyncio.wait_for(asyncio.shield(errors), timeout=2)
            except asyncio.TimeoutError as exc:
                raise AgentlyError(
                    "Agent Mail notification stream stopped responding."
                ) from exc
            if code != 0:
                self._payload(b"", error, code)
            raise AgentlyError("Agent Mail notification stream ended.", exit_code=code)
        finally:
            await self._stop(process)
            errors.cancel()
            await asyncio.gather(errors, return_exceptions=True)

    async def login(
        self,
        on_url: Callable[[str], Awaitable[None]],
        timeout: float = 300,
    ) -> dict[str, Any]:
        """Complete one explicit OAuth login and deliver the original URL.

        Args:
            on_url: Async callback receiving each exact authorization URL.
            timeout: Maximum authorization duration, in seconds.

        Returns:
            The +me envelope after successful authorization.

        Raises:
            AgentlyError: If authorization fails or times out; it is never retried.
        """
        process = await self._spawn(["auth", "login"])
        seen_urls: set[str] = set()
        outputs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}

        async def read_login(stream: asyncio.StreamReader, key: str) -> None:
            """Drain an authorization pipe and emit exact URLs.

            Args:
                stream: CLI stdout or stderr.
                key: Buffer key for the corresponding stream.
            """
            while line := await stream.readline():
                outputs[key].extend(line)
                if len(outputs[key]) > MAX_OUTPUT:
                    raise AgentlyError(
                        "Agent Mail authorization output exceeded 4 MiB."
                    )
                decoded = line.decode("utf-8", errors="replace")
                for match in re.finditer(r"https?://[^\s\x1b]+", decoded):
                    url = match.group(0)
                    if url not in seen_urls:
                        seen_urls.add(url)
                        await on_url(url)

        tasks = [
            asyncio.create_task(read_login(process.stdout, "stdout")),
            asyncio.create_task(read_login(process.stderr, "stderr")),
            asyncio.create_task(process.wait()),
        ]
        try:
            try:
                _, _, code = await asyncio.wait_for(
                    asyncio.gather(*tasks), timeout=max(float(timeout), 1.0)
                )
            except asyncio.TimeoutError as exc:
                raise AgentlyError("Agent Mail authorization timed out.") from exc
            if code != 0:
                self._payload(bytes(outputs["stdout"]), bytes(outputs["stderr"]), code)
        finally:
            await self._stop(process)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return await self.run(["+me"])

    async def close(self) -> None:
        """Prevent new commands and terminate every process owned by this client."""
        self._closed = True
        self._install_cancel.set()
        async with self._prepare_lock:
            pass
        await asyncio.gather(
            *(self._stop(process) for process in tuple(self._processes)),
            return_exceptions=True,
        )
