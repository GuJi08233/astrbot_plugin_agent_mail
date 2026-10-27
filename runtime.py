"""Install a verified official native Agent Mail CLI into plugin-owned storage."""

from __future__ import annotations

import asyncio
import base64
import binascii
import gzip
import hashlib
import hmac
import io
import json
import os
import platform
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath

CLI_VERSION = "1.0.18"
MAX_METADATA_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = MAX_BINARY_BYTES + 2 * 1024 * 1024
INSTALL_TIMEOUT = 120
SOCKET_TIMEOUT = 10


class RuntimeInstallError(RuntimeError):
    """Report an unsupported platform or a failed managed CLI installation."""


def get_managed_cli_path(install_dir: Path) -> Path:
    """Return the pinned binary path for this host without accessing credentials.

    Args:
        install_dir: Plugin-owned runtime storage root.

    Returns:
        The platform-specific executable beneath the pinned version directory.

    Raises:
        RuntimeInstallError: If the operating system or CPU is unsupported.
    """
    system = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(sys.platform)
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "x64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine().lower())
    if system is None or architecture is None:
        raise RuntimeInstallError(
            "Automatic Agent Mail CLI installation supports Windows, Linux, and "
            "macOS on x64 or Arm64. Configure cli_path for this platform."
        )
    binary = "agently-cli.exe" if system == "win32" else "agently-cli"
    return Path(install_dir) / CLI_VERSION / f"{system}-{architecture}" / binary


def _registry_url(url: str) -> str:
    """Require the official HTTPS npm registry for all requests and redirects.

    Args:
        url: Requested URL or redirect destination.

    Returns:
        The unchanged, validated URL.

    Raises:
        RuntimeInstallError: If the destination is not the official registry.
    """
    if not isinstance(url, str):
        raise RuntimeInstallError("The official package contains an invalid URL.")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "registry.npmjs.org"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeInstallError(
            "Only https://registry.npmjs.org downloads are allowed."
        )
    return url


class _RegistryRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects before contacting any non-registry host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Validate a redirect destination before urllib follows it."""
        return super().redirect_request(
            req, fp, code, msg, headers, _registry_url(newurl)
        )


def _check_running(cancel_event: threading.Event, deadline: float) -> None:
    """Abort repeated IO stages after cancellation or the installation deadline.

    Args:
        cancel_event: Cooperative stop signal shared with the async caller.
        deadline: Absolute monotonic deadline.

    Raises:
        RuntimeInstallError: If preparation was cancelled or exceeded its deadline.
    """
    if cancel_event.is_set():
        raise RuntimeInstallError("Agent Mail CLI preparation was cancelled.")
    if time.monotonic() >= deadline:
        raise RuntimeInstallError("Agent Mail CLI download timed out.")


def _download(
    url: str, limit: int, cancel_event: threading.Event, deadline: float
) -> bytes:
    """Fetch a bounded response from the official registry.

    Args:
        url: Official npm metadata or archive URL.
        limit: Maximum accepted response size.
        cancel_event: Cooperative stop signal.
        deadline: Absolute monotonic deadline shared by all network stages.

    Returns:
        Response bytes whose size does not exceed the limit.

    Raises:
        RuntimeInstallError: If the URL, response size, or stop condition is invalid.
        OSError: If the network request cannot complete.
    """
    _check_running(cancel_event, deadline)
    request = urllib.request.Request(
        _registry_url(url), headers={"User-Agent": "AstrBot-Agent-Mail-runtime"}
    )
    opener = urllib.request.build_opener(_RegistryRedirect())
    with opener.open(
        request, timeout=max(1, min(SOCKET_TIMEOUT, deadline - time.monotonic()))
    ) as response:
        _registry_url(response.geturl())
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > limit:
            raise RuntimeInstallError(
                "Agent Mail package download exceeds its size limit."
            )
        output = bytearray()
        read = getattr(response, "read1", response.read)
        while True:
            _check_running(cancel_event, deadline)
            chunk = read(min(65536, limit - len(output) + 1))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise RuntimeInstallError(
                    "Agent Mail package download exceeds its size limit."
                )
        return bytes(output)


def _install_sync(
    install_dir: Path,
    cancel_event: threading.Event,
    progress: Callable[[str], None] | None,
) -> Path:
    """Prepare and atomically activate a checksum-verified native package.

    Args:
        install_dir: Plugin-owned runtime directory.
        cancel_event: Cooperative stop signal checked before publication.
        progress: Optional worker-thread callback receiving installation phase names.

    Returns:
        The completed native executable path.

    Raises:
        RuntimeInstallError: If installation fails or is cancelled.
    """
    try:
        deadline = time.monotonic() + INSTALL_TIMEOUT
        _check_running(cancel_event, deadline)
        root = install_dir.resolve()
        target = get_managed_cli_path(root)
        if not target.parent.resolve().is_relative_to(root) or target.is_symlink():
            raise RuntimeInstallError(
                "The managed CLI path escapes its runtime directory."
            )
        if target.is_file() and target.stat().st_size > 0:
            if progress:
                progress("ready")
            return target
        package = f"@tencent-qqmail/agently-cli-{target.parent.name}"
        metadata_url = (
            "https://registry.npmjs.org/"
            + urllib.parse.quote(package, safe="")
            + f"/{CLI_VERSION}"
        )
        if progress:
            progress("metadata")
        metadata = json.loads(
            _download(metadata_url, MAX_METADATA_BYTES, cancel_event, deadline)
        )
        if metadata.get("name") != package or metadata.get("version") != CLI_VERSION:
            raise RuntimeInstallError(
                "The npm registry returned a different CLI package."
            )
        dist = metadata.get("dist", {})
        integrity = dist.get("integrity", "")
        if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
            raise RuntimeInstallError(
                "The official CLI package has no SHA-512 integrity value."
            )
        expected_digest = base64.b64decode(
            integrity.removeprefix("sha512-"), validate=True
        )
        if len(expected_digest) != 64:
            raise RuntimeInstallError("The official CLI integrity value is invalid.")
        if progress:
            progress("download")
        archive_data = _download(
            _registry_url(dist.get("tarball")),
            MAX_DOWNLOAD_BYTES,
            cancel_event,
            deadline,
        )
        if progress:
            progress("verify")
        if not hmac.compare_digest(
            hashlib.sha512(archive_data).digest(), expected_digest
        ):
            raise RuntimeInstallError(
                "Agent Mail CLI package integrity verification failed."
            )
        _check_running(cancel_event, deadline)
        with gzip.GzipFile(fileobj=io.BytesIO(archive_data)) as compressed:
            unpacked = compressed.read(MAX_UNPACKED_BYTES + 1)
        if len(unpacked) > MAX_UNPACKED_BYTES:
            raise RuntimeInstallError(
                "The unpacked CLI archive exceeds its size limit."
            )
        if progress:
            progress("extract")
        _check_running(cancel_event, deadline)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected_member = f"package/bin/{target.name}"
        with tempfile.TemporaryDirectory(
            prefix=".prepare-", dir=target.parent
        ) as staging:
            staged = Path(staging) / target.name
            with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as archive:
                selected = None
                for index, member in enumerate(archive):
                    _check_running(cancel_event, deadline)
                    parts = PurePosixPath(member.name).parts
                    if (
                        index >= 128
                        or member.name.startswith("/")
                        or ".." in parts
                        or "\\" in member.name
                        or ":" in member.name
                        or member.issym()
                        or member.islnk()
                        or not (member.isfile() or member.isdir())
                        or member.size < 0
                        or member.size > MAX_BINARY_BYTES
                    ):
                        raise RuntimeInstallError(
                            "The CLI archive contains an unsafe entry."
                        )
                    if member.name == expected_member:
                        if (
                            selected is not None
                            or not member.isfile()
                            or member.size < 1
                        ):
                            raise RuntimeInstallError(
                                "The CLI archive has an invalid executable."
                            )
                        selected = member
                if selected is None:
                    raise RuntimeInstallError(
                        "The CLI archive is missing its native executable."
                    )
                source = archive.extractfile(selected)
                if source is None:
                    raise RuntimeInstallError("The CLI executable could not be read.")
                with source, staged.open("wb") as destination:
                    while chunk := source.read(65536):
                        _check_running(cancel_event, deadline)
                        destination.write(chunk)
                if os.name != "nt":
                    staged.chmod(0o755)
                _check_running(cancel_event, deadline)
                os.replace(staged, target)
        if progress:
            progress("ready")
        return target
    except RuntimeInstallError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        binascii.Error,
        tarfile.TarError,
        EOFError,
    ) as exc:
        raise RuntimeInstallError(f"Could not prepare Agent Mail CLI: {exc}") from exc


async def install_cli(
    install_dir: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Install the official native CLI without Node.js, npm, or global changes.

    Args:
        install_dir: Plugin-owned runtime directory, never a global bin directory.
        cancel_event: Optional externally owned cooperative stop signal.
        progress: Optional callback invoked in the worker thread with metadata,
            download, verify, extract, or ready phase names.

    Returns:
        The cached or newly installed native executable.

    Raises:
        RuntimeInstallError: If preparation fails or an external stop signal is set.
        asyncio.CancelledError: If the async caller cancels preparation. The worker
            observes a stop signal and finishes cleanup before cancellation returns.
    """
    stop = cancel_event if cancel_event is not None else threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(_install_sync, Path(install_dir), stop, progress)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        raise
