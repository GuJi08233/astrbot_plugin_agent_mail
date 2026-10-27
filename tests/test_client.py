"""Exercise CLI process behavior with a disposable local fake executable."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import AgentlyClient, AgentlyError  # noqa: E402

FAKE_CLI = r"""
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

mode = sys.argv[1]
if mode == "echo":
    print(json.dumps({"ok": True, "data": sys.argv[2:]}, ensure_ascii=False))
elif mode == "confirmation":
    print(json.dumps({"ok": False, "error": {"message": "Confirmation required"},
                      "data": {"confirmation_token": "ctk_test"}}))
    raise SystemExit(8)
elif mode == "bad":
    print("not JSON")
elif mode == "array":
    print("[]")
elif mode == "stderr":
    sys.stderr.write("x" * (512 * 1024))
    print(json.dumps({"ok": True}))
elif mode == "huge":
    sys.stdout.write("x" * (5 * 1024 * 1024))
    sys.stdout.flush()
    time.sleep(30)
elif mode == "sleep":
    time.sleep(30)
elif mode == "message":
    if "--bad" in sys.argv:
        print("not JSON", flush=True)
    elif "--unauthorized" in sys.argv:
        print(json.dumps({"ok": False, "error": {"message": "Authorization expired"}}), flush=True)
        raise SystemExit(3)
    elif "--eof" in sys.argv:
        raise SystemExit(0)
    else:
        print(json.dumps({"message": {"message_id": "msg_1", "subject": "你好"}}), flush=True)
        print(json.dumps({"message": {"message_id": "msg_2"}}), flush=True)
    time.sleep(30)
elif mode == "auth":
    print("Please visit https://agent.qq.com/auth?token=a%2Bb&state=x=y", file=sys.stderr, flush=True)
    print("Authorized", flush=True)
elif mode == "+me":
    print(json.dumps({"ok": True, "data": {"email": "agent@agent.qq.com"}}))
"""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Create a real subprocess fake without contacting the mail service."""
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    client = AgentlyClient(timeout=1)
    client._test_started = asyncio.Event()
    monkeypatch.setattr(client, "_executable", lambda: sys.executable)
    original_spawn = client._spawn
    processes = []

    async def spawn(args, cwd=None):
        process = await original_spawn([str(script), *args], cwd)
        processes.append(process)
        client._test_started.set()
        return process

    monkeypatch.setattr(client, "_spawn", spawn)
    return client, processes


@pytest.mark.asyncio
async def test_run_preserves_unicode_and_shell_metacharacters(cli):
    client, processes = cli
    values = ["中文\n正文", "a;b|c", "$(touch nope)", '"quoted"']
    result = await client.run(["echo", *values])
    assert result["data"] == values
    assert not client._processes
    assert processes[0].returncode == 0


@pytest.mark.asyncio
async def test_confirmation_is_a_failure_with_the_original_payload(cli):
    client, _ = cli
    with pytest.raises(AgentlyError, match="Confirmation required") as caught:
        await client.run(["confirmation"])
    assert caught.value.exit_code == 8
    assert caught.value.payload["data"]["confirmation_token"] == "ctk_test"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bad", "array"])
async def test_invalid_success_output_is_rejected(cli, mode):
    client, _ = cli
    with pytest.raises(AgentlyError, match="invalid JSON object"):
        await client.run([mode])


@pytest.mark.asyncio
async def test_stderr_is_drained_without_blocking_stdout(cli):
    client, _ = cli
    assert await client.run(["stderr"]) == {"ok": True}


@pytest.mark.asyncio
async def test_output_overflow_terminates_the_process(cli):
    client, processes = cli
    with pytest.raises(AgentlyError, match="4 MiB"):
        await client.run(["huge"])
    assert processes[0].returncode is not None
    assert not client._processes


@pytest.mark.asyncio
async def test_timeout_terminates_and_reaps_the_process(cli):
    client, processes = cli
    with pytest.raises(AgentlyError, match="timed out"):
        await client.run(["sleep"])
    assert processes[0].returncode is not None
    assert not client._processes


@pytest.mark.asyncio
async def test_cancellation_terminates_and_reaps_the_process(cli):
    client, processes = cli
    task = asyncio.create_task(client.run(["sleep"]))
    await asyncio.wait_for(client._test_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
    assert not client._processes


@pytest.mark.asyncio
async def test_watch_streams_events_and_aclose_cleans_up(cli):
    client, processes = cli
    events = client.watch()
    first = await anext(events)
    second = await anext(events)
    assert first["message"]["subject"] == "你好"
    assert second["message"]["message_id"] == "msg_2"
    await events.aclose()
    assert processes[0].returncode is not None
    assert not client._processes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "message", "exit_code"),
    [
        ("--bad", "invalid notification JSON", None),
        ("--unauthorized", "Authorization expired", 3),
        ("--eof", "stream ended", 0),
    ],
)
async def test_watch_rejects_bad_output_and_preserves_auth_exit(
    cli, monkeypatch, flag, message, exit_code
):
    client, processes = cli
    original_spawn = client._spawn

    async def spawn(args, cwd=None):
        return await original_spawn([*args, flag], cwd)

    monkeypatch.setattr(client, "_spawn", spawn)
    events = client.watch()
    with pytest.raises(AgentlyError, match=message) as caught:
        await anext(events)
    assert caught.value.exit_code == exit_code
    assert processes[0].returncode is not None
    assert not client._processes


@pytest.mark.asyncio
async def test_login_delivers_the_opaque_url_and_verifies_account(cli):
    client, _ = cli
    urls = []

    async def collect(url):
        urls.append(url)

    result = await client.login(collect)
    assert urls == ["https://agent.qq.com/auth?token=a%2Bb&state=x=y"]
    assert result["data"]["email"] == "agent@agent.qq.com"


@pytest.mark.asyncio
async def test_close_stops_running_commands_and_rejects_new_ones(cli):
    client, processes = cli
    task = asyncio.create_task(client.run(["sleep"]))
    await asyncio.wait_for(client._test_started.wait(), timeout=2)
    await client.close()
    with pytest.raises(AgentlyError):
        await task
    assert processes[0].returncode is not None
    with pytest.raises(AgentlyError, match="closed"):
        await client.run(["echo"])


def test_npm_shim_resolves_the_platform_native_binary(tmp_path, monkeypatch):
    import client as module

    shim = tmp_path / "agently-cli.cmd"
    shim.write_text("not executed", encoding="utf-8")
    native = (
        tmp_path
        / "node_modules"
        / "@tencent-qqmail"
        / "agently-cli-win32-x64"
        / "bin"
        / "agently-cli.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native placeholder")
    monkeypatch.setattr(module.shutil, "which", lambda _: str(shim))
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    assert AgentlyClient()._executable() == str(native.resolve())


def test_npm_shim_without_native_package_is_rejected(tmp_path, monkeypatch):
    import client as module

    shim = tmp_path / "agently-cli.cmd"
    shim.write_text("not executed", encoding="utf-8")
    monkeypatch.setattr(module.shutil, "which", lambda _: str(shim))
    with pytest.raises(AgentlyError, match="native package"):
        AgentlyClient()._executable()
