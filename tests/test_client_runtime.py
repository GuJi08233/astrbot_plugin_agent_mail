"""Exercise managed-runtime integration through the real packaged CLI client."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def packaged_client(tmp_path, monkeypatch):
    """Load a private package namespace without altering existing client imports."""
    package_name = "_agent_mail_client_runtime_test"
    package = ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)
    client_module = importlib.import_module(f"{package_name}.client")
    runtime_module = importlib.import_module(f"{package_name}.runtime")
    monkeypatch.setattr(client_module.shutil, "which", lambda value: None)
    directory = tmp_path / "runtime"
    target = runtime_module.get_managed_cli_path(directory)

    async def installed_binary():
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, b"native-cli-fixture")
        return target

    try:
        yield SimpleNamespace(
            client=client_module,
            runtime=runtime_module,
            directory=directory,
            target=target,
            installed_binary=installed_binary,
        )
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                sys.modules.pop(name, None)


@pytest.mark.asyncio
async def test_existing_path_cli_is_used_without_managed_download(
    packaged_client, monkeypatch
):
    bundle = packaged_client
    executable = await bundle.installed_binary()
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Existing CLI must not trigger installation")

    monkeypatch.setattr(bundle.client.shutil, "which", lambda value: str(executable))
    monkeypatch.setattr(bundle.runtime, "install_cli", forbidden)
    client = bundle.client.AgentlyClient(install_dir=bundle.directory)
    assert await client.ensure_available() == str(executable.resolve())
    assert client.runtime_state["source"] == "path"
    assert not calls


@pytest.mark.asyncio
async def test_invalid_custom_path_is_not_silently_replaced(
    packaged_client, monkeypatch
):
    bundle = packaged_client

    async def forbidden(*args, **kwargs):
        raise AssertionError("Explicit configuration must not be overridden")

    monkeypatch.setattr(bundle.runtime, "install_cli", forbidden)
    client = bundle.client.AgentlyClient(
        cli_path=str(bundle.directory / "missing-custom-cli"),
        install_dir=bundle.directory,
    )
    with pytest.raises(bundle.client.AgentlyError, match="not found"):
        await client.ensure_available()
    assert client.runtime_state["status"] == "failed"
    assert not await asyncio.to_thread(bundle.target.exists)


@pytest.mark.asyncio
async def test_missing_cli_downloads_and_exposes_ready_managed_state(
    packaged_client, monkeypatch
):
    bundle = packaged_client
    calls = []

    async def install(directory, *, cancel_event, progress):
        calls.append(directory)
        assert not cancel_event.is_set()
        await asyncio.to_thread(progress, "metadata")
        return await bundle.installed_binary()

    monkeypatch.setattr(bundle.runtime, "install_cli", install)
    client = bundle.client.AgentlyClient(install_dir=bundle.directory)
    assert await client.ensure_available() == str(bundle.target.resolve())
    assert calls == [bundle.directory]
    state = client.runtime_state
    assert state["status"] == "ready" and state["source"] == "managed"
    assert state["version"] == bundle.runtime.CLI_VERSION
    state["status"] = "failed"
    assert client.runtime_state["status"] == "ready"


@pytest.mark.asyncio
async def test_concurrent_callers_share_exactly_one_install(
    packaged_client, monkeypatch
):
    bundle = packaged_client
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def install(directory, **kwargs):
        nonlocal count
        count += 1
        started.set()
        await release.wait()
        return await bundle.installed_binary()

    monkeypatch.setattr(bundle.runtime, "install_cli", install)
    client = bundle.client.AgentlyClient(install_dir=bundle.directory)
    tasks = [asyncio.create_task(client.ensure_available()) for _ in range(5)]
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert count == 1
        release.set()
        paths = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert paths == [str(bundle.target.resolve())] * 5
        assert count == 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_failure_is_cached_until_explicit_retry(packaged_client, monkeypatch):
    bundle = packaged_client
    count = 0

    async def install(directory, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise bundle.runtime.RuntimeInstallError("Registry unavailable")
        return await bundle.installed_binary()

    monkeypatch.setattr(bundle.runtime, "install_cli", install)
    client = bundle.client.AgentlyClient(install_dir=bundle.directory)
    for _ in range(3):
        with pytest.raises(bundle.client.AgentlyError, match="Registry unavailable"):
            await client.ensure_available()
    assert count == 1 and client.runtime_state["status"] == "failed"
    assert await client.ensure_available(retry=True) == str(bundle.target.resolve())
    assert count == 2 and client.runtime_state["status"] == "ready"


@pytest.mark.asyncio
async def test_disabled_installation_never_downloads(packaged_client, monkeypatch):
    bundle = packaged_client

    async def forbidden(*args, **kwargs):
        raise AssertionError("Disabled preparation must not download")

    monkeypatch.setattr(bundle.runtime, "install_cli", forbidden)
    client = bundle.client.AgentlyClient(
        install_dir=bundle.directory, auto_install=False
    )
    with pytest.raises(bundle.client.AgentlyError, match="disabled"):
        await client.ensure_available()
    assert client.runtime_state["status"] == "disabled"


@pytest.mark.asyncio
async def test_completed_managed_cache_is_reused_even_if_downloads_disabled(
    packaged_client, monkeypatch
):
    bundle = packaged_client
    await bundle.installed_binary()

    async def forbidden(*args, **kwargs):
        raise AssertionError("A completed local runtime must be reused")

    monkeypatch.setattr(bundle.runtime, "install_cli", forbidden)
    client = bundle.client.AgentlyClient(
        install_dir=bundle.directory, auto_install=False
    )
    assert await client.ensure_available() == str(bundle.target.resolve())
    assert client.runtime_state["status"] == "ready"
    assert client.runtime_state["source"] == "managed"


@pytest.mark.asyncio
async def test_close_blocks_process_launch_after_inflight_preparation(
    packaged_client, monkeypatch
):
    bundle = packaged_client
    started = asyncio.Event()
    stop_signals = []

    async def install(directory, *, cancel_event, progress):
        stop_signals.append(cancel_event)
        started.set()
        assert await asyncio.to_thread(cancel_event.wait, 2)
        return await bundle.installed_binary()

    async def forbidden(*args, **kwargs):
        raise AssertionError("A closed client must never launch a CLI")

    monkeypatch.setattr(bundle.runtime, "install_cli", install)
    monkeypatch.setattr(bundle.client.asyncio, "create_subprocess_exec", forbidden)
    client = bundle.client.AgentlyClient(install_dir=bundle.directory)
    task = asyncio.create_task(client.run(["+me"]))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await client.close()
        assert stop_signals[0].is_set()
        with pytest.raises(bundle.client.AgentlyError, match="closed"):
            await asyncio.wait_for(task, timeout=2)
        with pytest.raises(bundle.client.AgentlyError, match="closed"):
            await client.run(["+me"])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_preparation_can_retry_with_a_fresh_stop_signal(
    packaged_client, monkeypatch
):
    bundle = packaged_client
    started = asyncio.Event()
    signals = []

    async def install(directory, *, cancel_event, progress):
        signals.append(cancel_event)
        if len(signals) == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_event.set()
                raise
        return await bundle.installed_binary()

    monkeypatch.setattr(bundle.runtime, "install_cli", install)
    client = bundle.client.AgentlyClient(install_dir=bundle.directory)
    task = asyncio.create_task(client.ensure_available())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.runtime_state["status"] == "idle"
    await client.ensure_available()
    assert len(signals) == 2 and signals[0] is not signals[1]
    assert signals[0].is_set() and not signals[1].is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeded", [True, False])
async def test_late_worker_progress_does_not_override_terminal_status(
    packaged_client, monkeypatch, succeeded
):
    bundle = packaged_client
    callbacks = []

    async def install(directory, *, cancel_event, progress):
        callbacks.append(progress)
        if not succeeded:
            raise bundle.runtime.RuntimeInstallError("Registry unavailable")
        return await bundle.installed_binary()

    monkeypatch.setattr(bundle.runtime, "install_cli", install)
    client = bundle.client.AgentlyClient(install_dir=bundle.directory)
    if succeeded:
        await client.ensure_available()
    else:
        with pytest.raises(bundle.client.AgentlyError):
            await client.ensure_available()
    expected = "ready" if succeeded else "failed"
    await asyncio.to_thread(callbacks[0], "download")
    await asyncio.sleep(0)
    assert client.runtime_state["status"] == expected
