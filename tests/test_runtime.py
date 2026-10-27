"""Verify managed CLI installation using registry fixtures, never live mail."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import sys
import tarfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime  # noqa: E402


def make_archive(entries=None):
    """Create a small official-shaped archive or caller-specified attack entries."""
    output = io.BytesIO()
    if entries is None:
        entries = [("package/bin/agently-cli.exe", b"native-cli", tarfile.REGTYPE)]
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "../../outside"
            archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return output.getvalue()


@pytest.fixture
def registry(monkeypatch):
    """Serve signed fixture bytes through a fake registry opener."""
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "AMD64")
    archive = make_archive()
    fixture = {"archive": archive, "metadata": None, "calls": [], "response_url": None}

    class Response(io.BytesIO):
        def __init__(self, content, url):
            super().__init__(content)
            self.url = url
            self.headers = {"Content-Length": str(len(content))}

        def geturl(self):
            return fixture["response_url"] or self.url

    class Opener:
        def open(self, request, timeout):
            fixture["calls"].append(request.full_url)
            if request.full_url.endswith(".tgz"):
                return Response(fixture["archive"], request.full_url)
            metadata = fixture["metadata"]
            if metadata is None:
                metadata = {
                    "name": "@tencent-qqmail/agently-cli-win32-x64",
                    "version": runtime.CLI_VERSION,
                    "dist": {
                        "tarball": "https://registry.npmjs.org/package.tgz",
                        "integrity": "sha512-"
                        + base64.b64encode(
                            hashlib.sha512(fixture["archive"]).digest()
                        ).decode(),
                    },
                }
            return Response(json.dumps(metadata).encode(), request.full_url)

    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *args: Opener())
    return fixture


@pytest.mark.parametrize(
    ("system", "cpu", "directory", "binary"),
    [
        ("win32", "AMD64", "win32-x64", "agently-cli.exe"),
        ("win32", "ARM64", "win32-arm64", "agently-cli.exe"),
        ("linux", "x86_64", "linux-x64", "agently-cli"),
        ("linux", "aarch64", "linux-arm64", "agently-cli"),
        ("darwin", "x86_64", "darwin-x64", "agently-cli"),
        ("darwin", "arm64", "darwin-arm64", "agently-cli"),
    ],
)
def test_managed_path_matches_official_platform_packages(
    tmp_path, monkeypatch, system, cpu, directory, binary
):
    monkeypatch.setattr(runtime.sys, "platform", system)
    monkeypatch.setattr(runtime.platform, "machine", lambda: cpu)
    assert (
        runtime.get_managed_cli_path(tmp_path)
        == tmp_path / runtime.CLI_VERSION / directory / binary
    )


def test_unsupported_platform_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "freebsd")
    with pytest.raises(runtime.RuntimeInstallError, match="Configure cli_path"):
        runtime.get_managed_cli_path(tmp_path)


def test_unsupported_architecture_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "riscv64")
    with pytest.raises(runtime.RuntimeInstallError, match="Configure cli_path"):
        runtime.get_managed_cli_path(tmp_path)


@pytest.mark.asyncio
async def test_success_installs_only_the_expected_binary_and_reuses_cache(
    tmp_path, registry
):
    progress = []
    target = await runtime.install_cli(tmp_path, progress=progress.append)
    assert await asyncio.to_thread(target.read_bytes) == b"native-cli"
    assert progress == ["metadata", "download", "verify", "extract", "ready"]
    assert len(registry["calls"]) == 2
    assert await runtime.install_cli(tmp_path) == target
    assert len(registry["calls"]) == 2
    assert sorted(
        await asyncio.to_thread(lambda: [p.name for p in target.parent.iterdir()])
    ) == [target.name]


@pytest.mark.asyncio
async def test_integrity_failure_never_publishes_a_binary(tmp_path, registry):
    registry["metadata"] = {
        "name": "@tencent-qqmail/agently-cli-win32-x64",
        "version": runtime.CLI_VERSION,
        "dist": {
            "tarball": "https://registry.npmjs.org/package.tgz",
            "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode(),
        },
    }
    with pytest.raises(
        runtime.RuntimeInstallError, match="integrity verification failed"
    ):
        await runtime.install_cli(tmp_path)
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", tarfile.REGTYPE),
        ("/outside", tarfile.REGTYPE),
        ("package/../../outside", tarfile.REGTYPE),
        ("package/bin/agently-cli.exe", tarfile.SYMTYPE),
        ("package/bin/agently-cli.exe", tarfile.LNKTYPE),
    ],
)
async def test_unsafe_archive_entries_are_rejected_without_extraction(
    tmp_path, registry, name, kind
):
    registry["archive"] = make_archive([(name, b"not executable", kind)])
    with pytest.raises(runtime.RuntimeInstallError, match="unsafe entry"):
        await runtime.install_cli(tmp_path)
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)


@pytest.mark.asyncio
async def test_duplicate_executable_is_rejected(tmp_path, registry):
    entry = ("package/bin/agently-cli.exe", b"native", tarfile.REGTYPE)
    registry["archive"] = make_archive([entry, entry])
    with pytest.raises(runtime.RuntimeInstallError, match="invalid executable"):
        await runtime.install_cli(tmp_path)


@pytest.mark.asyncio
async def test_wrong_registry_and_redirect_hosts_are_rejected(tmp_path, registry):
    registry["response_url"] = "https://attacker.example/package"
    with pytest.raises(runtime.RuntimeInstallError, match="Only https"):
        await runtime.install_cli(tmp_path)
    handler = runtime._RegistryRedirect()
    with pytest.raises(runtime.RuntimeInstallError, match="Only https"):
        handler.redirect_request(
            None, None, 302, "redirect", {}, "https://attacker.example/package"
        )


@pytest.mark.asyncio
async def test_oversized_download_is_rejected_before_publication(
    tmp_path, registry, monkeypatch
):
    monkeypatch.setattr(runtime, "MAX_DOWNLOAD_BYTES", 4)
    with pytest.raises(runtime.RuntimeInstallError, match="size limit"):
        await runtime.install_cli(tmp_path)
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)


@pytest.mark.asyncio
async def test_oversized_binary_is_rejected_without_final_executable(
    tmp_path, registry, monkeypatch
):
    monkeypatch.setattr(runtime, "MAX_BINARY_BYTES", 4)
    with pytest.raises(runtime.RuntimeInstallError, match="unsafe entry"):
        await runtime.install_cli(tmp_path)
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)


@pytest.mark.asyncio
async def test_gzip_expansion_limit_is_checked_before_tar_parsing(
    tmp_path, registry, monkeypatch
):
    monkeypatch.setattr(runtime, "MAX_UNPACKED_BYTES", 1024)
    with pytest.raises(runtime.RuntimeInstallError, match="unpacked CLI archive"):
        await runtime.install_cli(tmp_path)
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)


@pytest.mark.asyncio
async def test_missing_executable_is_not_treated_as_success(tmp_path, registry):
    registry["archive"] = make_archive(
        [("package/README.md", b"Documentation", tarfile.REGTYPE)]
    )
    with pytest.raises(
        runtime.RuntimeInstallError, match="missing its native executable"
    ):
        await runtime.install_cli(tmp_path)


@pytest.mark.asyncio
async def test_non_registry_tarball_is_rejected_before_download(tmp_path, registry):
    registry["metadata"] = {
        "name": "@tencent-qqmail/agently-cli-win32-x64",
        "version": runtime.CLI_VERSION,
        "dist": {
            "tarball": "https://attacker.example/package.tgz",
            "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode(),
        },
    }
    with pytest.raises(runtime.RuntimeInstallError, match="Only https"):
        await runtime.install_cli(tmp_path)
    assert len(registry["calls"]) == 1


@pytest.mark.asyncio
async def test_atomic_publication_receives_only_the_fully_written_binary(
    tmp_path, registry, monkeypatch
):
    original = runtime.os.replace
    published = []

    def replace(source, target):
        assert source.read_bytes() == b"native-cli"
        assert not target.exists()
        assert source.parent.name.startswith(".prepare-")
        published.append(target)
        return original(source, target)

    monkeypatch.setattr(runtime.os, "replace", replace)
    installed = await runtime.install_cli(tmp_path)
    assert published == [installed]


@pytest.mark.asyncio
async def test_network_error_leaves_no_executable(tmp_path, registry, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Network unavailable")

    monkeypatch.setattr(runtime, "_download", fail)
    with pytest.raises(runtime.RuntimeInstallError, match="Network unavailable"):
        await runtime.install_cli(tmp_path)
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)


@pytest.mark.asyncio
async def test_external_cancellation_before_activation_cleans_staging(
    tmp_path, registry
):
    stop = threading.Event()

    def progress(phase):
        if phase == "extract":
            stop.set()

    with pytest.raises(runtime.RuntimeInstallError, match="cancelled"):
        await runtime.install_cli(tmp_path, cancel_event=stop, progress=progress)
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)


@pytest.mark.asyncio
async def test_async_cancellation_stops_worker_before_returning(
    tmp_path, registry, monkeypatch
):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = runtime._download

    def slow_download(url, limit, stop, deadline):
        loop.call_soon_threadsafe(started.set)
        assert stop.wait(5)
        return original(url, limit, stop, deadline)

    monkeypatch.setattr(runtime, "_download", slow_download)
    task = asyncio.create_task(runtime.install_cli(tmp_path))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await asyncio.to_thread(runtime.get_managed_cli_path(tmp_path).exists)
