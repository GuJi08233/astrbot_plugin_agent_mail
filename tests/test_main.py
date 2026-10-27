"""Test the plugin adapter with AstrBot stubs and a non-network mail client."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]


class Event:
    """Supply the trusted identity fields the framework gives each handler."""

    def __init__(self, user="member", admin=False, session=None):
        self.user = user
        self.admin = admin
        self.unified_msg_origin = session or f"bot:FriendMessage:{user}"

    def get_platform_id(self):
        return "bot"

    def get_sender_id(self):
        return self.user

    def is_admin(self):
        return self.admin

    def is_private_chat(self):
        return ":FriendMessage:" in self.unified_msg_origin

    def plain_result(self, text):
        return text


class Context:
    """Retain disposable settings and deterministic notification delivery results."""

    def __init__(self):
        self.store = {}
        self.saved = asyncio.Queue()
        self.admins = ["admin"]
        self.plugins = {"reporter": SimpleNamespace(activated=True)}
        self.sent = []
        self.results = {}

    def get_registered_star(self, name):
        return self.plugins.get(name)

    def get_config(self, target=None):
        return {"admins_id": self.admins}

    async def send_message(self, target, chain):
        self.sent.append((target, chain.chain[0].text))
        return self.results.get(target, True)


class FakeClient:
    """Expose deterministic operations and an idle cancelable notification stream."""

    def __init__(self):
        self.calls = []
        self.events = asyncio.Queue()
        self.watch_started = asyncio.Event()
        self.watch_stopped = asyncio.Event()
        self.closed = False

    async def run(self, args, *, cwd=None):
        self.calls.append((args, cwd))
        return {"ok": True, "data": {"messages": []}}

    async def watch(self):
        self.watch_started.set()
        try:
            while True:
                yield await self.events.get()
        finally:
            self.watch_stopped.set()

    async def close(self):
        self.closed = True


@pytest_asyncio.fixture
async def plugin(tmp_path, monkeypatch):
    """Load the plugin in a stub package without importing AstrBot's runtime."""
    modules = {
        name: ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.message_components",
            "astrbot.api.star",
            "astrbot.core",
            "astrbot.core.utils",
            "astrbot.core.utils.astrbot_path",
        )
    }

    class Star:
        def __init__(self, context, config):
            self.context = context
            self.logger = logging.getLogger("agent-mail-test")

        async def put_kv_data(self, key, value):
            self.context.store[key] = copy.deepcopy(value)
            self.context.saved.put_nowait((key, copy.deepcopy(value)))

        async def get_kv_data(self, key, default):
            return copy.deepcopy(self.context.store.get(key, default))

    class Filters:
        PermissionType = SimpleNamespace(ADMIN="admin")

        @staticmethod
        def llm_tool(**kwargs):
            return lambda method: method

        @staticmethod
        def permission_type(*args):
            return lambda method: method

        @staticmethod
        def command_group(*args, **kwargs):
            def decorate(method):
                method.command = lambda *a, **kw: lambda child: child
                return method

            return decorate

    class Plain:
        def __init__(self, text):
            self.text = text

    class MessageChain:
        def __init__(self, chain):
            self.chain = chain

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api.event"].AstrMessageEvent = Event
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = Filters
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.star"].Context = Context
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_plugin_data_path = lambda: (
        str(tmp_path)
    )
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    package_name = "_agent_mail_adapter_test"
    package = ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", Path(package.__path__[0]) / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    context = Context()
    instance = module.AgentMailPlugin(context, {})
    client = FakeClient()
    instance.client = instance.service.client = client
    await instance.initialize()
    try:
        yield instance, context, client, module
    finally:
        await instance.terminate()
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                sys.modules.pop(name, None)


async def delivery_saved(context, predicate):
    """Wait for a persisted delivery snapshot satisfying a test condition."""
    while True:
        key, state = await context.saved.get()
        if key == "delivery" and predicate(state):
            return state


@pytest.mark.asyncio
async def test_non_admin_cannot_change_acl_even_when_called_without_filter(plugin):
    instance, _, client, _ = plugin
    before = instance.service.state
    results = [
        result
        async for result in instance.management_allow(
            Event(), "read", "user:bot:member"
        )
    ]
    assert "失败" in results[0]
    assert instance.service.state == before
    assert not client.calls


@pytest.mark.asyncio
async def test_default_llm_tools_reject_members_without_invoking_cli(plugin):
    instance, _, client, _ = plugin
    read = json.loads(await instance.mail_list(Event()))
    send = json.loads(
        await instance.mail_send(Event(), ["recipient@example.com"], "Subject", "Body")
    )
    assert read["ok"] is False
    assert send["ok"] is False
    assert not client.calls


@pytest.mark.asyncio
async def test_llm_permission_is_rechecked_after_revocation(plugin):
    instance, _, client, _ = plugin
    admin = instance._actor(Event("admin", True))
    await instance.service.set_access(admin, "read", "user:bot:member", "allow")
    assert json.loads(await instance.mail_list(Event()))["ok"] is True
    await instance.service.set_access(admin, "read", "user:bot:member", "deny")
    assert json.loads(await instance.mail_list(Event()))["ok"] is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_plugin_api_denies_by_default_and_rechecks_both_plugin_states(plugin):
    instance, context, client, module = plugin
    api = instance.get_api("reporter")
    with pytest.raises(module.MailPermissionError):
        await api.list_messages()
    admin = instance._actor(Event("admin", True))
    await instance.service.set_access(admin, "read", "plugin:reporter", "allow")
    assert (await api.list_messages())["ok"] is True
    context.plugins["reporter"].activated = False
    with pytest.raises(module.MailPermissionError):
        await api.list_messages()
    context.plugins["reporter"].activated = True
    instance._active = False
    with pytest.raises(module.MailPermissionError):
        await api.list_messages()
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_notifications_are_off_by_default(plugin):
    instance, context, client, _ = plugin
    assert not instance.service.state["notification_targets"]
    assert instance._watch_task is None
    assert not client.watch_started.is_set()
    await instance._enqueue_notification(
        {"message": {"message_id": "msg_1", "subject": "Data"}}
    )
    assert not instance._delivery["pending"]
    assert not context.sent


@pytest.mark.asyncio
async def test_successful_delivery_is_deduplicated_per_target(plugin):
    instance, context, _, _ = plugin
    actor = instance._actor(Event("admin", True))
    first, second = "bot:FriendMessage:one", "bot:FriendMessage:two"
    await instance.service.set_notification(actor, first, True)
    await instance.service.set_notification(actor, second, True)
    event = {"message": {"message_id": "msg_1", "subject": "Data"}}
    await instance._enqueue_notification(event)
    await asyncio.wait_for(
        delivery_saved(
            context,
            lambda state: all(
                "msg_1" in state["delivered"].get(target, [])
                for target in (first, second)
            ),
        ),
        timeout=2,
    )
    await instance._enqueue_notification(event)
    assert [target for target, _ in context.sent] == [first, second]
    assert not instance._delivery["pending"]


@pytest.mark.asyncio
async def test_failure_retries_only_failed_target_and_retains_pending_mail(plugin):
    instance, context, _, _ = plugin
    actor = instance._actor(Event("admin", True))
    first, second = "bot:FriendMessage:one", "bot:FriendMessage:two"
    await instance.service.set_notification(actor, first, True)
    await instance.service.set_notification(actor, second, True)
    context.results[second] = False
    await instance._enqueue_notification({"message": {"message_id": "msg_1"}})
    await asyncio.wait_for(
        delivery_saved(
            context,
            lambda state: "msg_1" in state["delivered"].get(first, []),
        ),
        timeout=2,
    )
    # The first target already has a persisted receipt; the second remains pending.
    assert instance._delivery["pending"]["msg_1"]["targets"] == [second]
    context.results[second] = True
    instance._retry_at.clear()
    instance._delivery_wakeup.set()
    await asyncio.wait_for(
        delivery_saved(context, lambda state: not state["pending"]), timeout=2
    )
    assert [target for target, _ in context.sent].count(first) == 1
    assert [target for target, _ in context.sent].count(second) >= 1


@pytest.mark.asyncio
async def test_revoke_cancels_watcher_and_prevents_pending_delivery(plugin):
    instance, context, client, _ = plugin
    admin = Event("admin", True)
    target = admin.unified_msg_origin
    await instance.service.set_notification(instance._actor(admin), target, True)
    await instance._sync_watcher()
    await asyncio.wait_for(client.watch_started.wait(), timeout=2)
    await instance._change_access(admin, "read", "user:bot:admin", "deny")
    await asyncio.wait_for(client.watch_stopped.wait(), timeout=2)
    await instance._enqueue_notification({"message": {"message_id": "msg_revoked"}})
    assert instance._watch_task is None
    assert not instance._delivery["pending"]
    assert not context.sent


@pytest.mark.asyncio
async def test_authorized_members_cannot_bind_another_session(plugin):
    instance, _, client, _ = plugin
    admin = instance._actor(Event("admin", True))
    for capability in ("read", "notify"):
        await instance.service.set_access(admin, capability, "user:bot:member", "allow")
    results = [
        result
        async for result in instance.management_notify(
            Event(), "on", "bot:FriendMessage:someone_else"
        )
    ]
    assert "失败" in results[0]
    assert not instance.service.state["notification_targets"]
    assert not client.watch_started.is_set()


@pytest.mark.asyncio
async def test_terminate_cancels_watcher_and_closes_the_client(plugin):
    instance, _, client, _ = plugin
    admin = Event("admin", True)
    await instance.service.set_notification(
        instance._actor(admin), admin.unified_msg_origin, True
    )
    await instance._sync_watcher()
    await asyncio.wait_for(client.watch_started.wait(), timeout=2)
    await instance.terminate()
    assert client.closed
    assert client.watch_stopped.is_set()
    assert instance._watch_task is instance._delivery_task is None


@pytest.mark.asyncio
async def test_external_admin_removal_stops_idle_watcher_on_reauthorization(plugin):
    instance, context, client, _ = plugin
    admin = Event("admin", True)
    await instance.service.set_notification(
        instance._actor(admin), admin.unified_msg_origin, True
    )
    await instance._sync_watcher()
    await asyncio.wait_for(client.watch_started.wait(), timeout=2)
    context.admins.clear()
    instance._delivery_wakeup.set()
    await asyncio.wait_for(client.watch_stopped.wait(), timeout=2)
    async with instance._watch_lock:
        pass
    assert instance._watch_task is None
    assert not instance._authorized_targets()


@pytest.mark.asyncio
async def test_inactive_plugin_rejects_notification_tool_without_changing_settings(
    plugin,
):
    instance, _, _, _ = plugin
    before = instance.service.state
    await instance.terminate()
    result = json.loads(await instance.mail_notifications(Event("admin", True), True))
    assert result["ok"] is False
    assert instance.service.state == before


@pytest.mark.asyncio
async def test_expired_authorization_pauses_watcher_instead_of_restarting(plugin):
    instance, _, client, module = plugin
    admin = Event("admin", True)
    await instance.service.set_notification(
        instance._actor(admin), admin.unified_msg_origin, True
    )
    calls = 0

    async def expired_watch():
        nonlocal calls
        calls += 1
        raise module.AgentlyError("Authorization expired", exit_code=3)
        yield {}  # pragma: no cover

    client.watch = expired_watch
    await instance._sync_watcher()
    await asyncio.wait_for(instance._watch_task, timeout=2)
    assert instance._watch_auth_expired is True
    await instance._sync_watcher()
    assert calls == 1


@pytest.mark.asyncio
async def test_full_notification_queue_waits_for_space_without_dropping_events(
    plugin, monkeypatch
):
    instance, context, _, _ = plugin
    instance._delivery_task.cancel()
    await asyncio.gather(instance._delivery_task, return_exceptions=True)
    instance._delivery_task = None
    admin = Event("admin", True)
    target = admin.unified_msg_origin
    await instance.service.set_notification(instance._actor(admin), target, True)
    instance._delivery["pending"] = {
        f"msg_{index}": {"text": f"Existing {index}", "targets": [target]}
        for index in range(200)
    }
    original_wait = instance._queue_space.wait
    waiting = asyncio.Event()

    async def wait_for_space():
        waiting.set()
        return await original_wait()

    monkeypatch.setattr(instance._queue_space, "wait", wait_for_space)
    task = asyncio.create_task(
        instance._enqueue_notification({"message": {"message_id": "msg_new"}})
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert not task.done()
        assert len(instance._delivery["pending"]) == 200
        assert "msg_0" in instance._delivery["pending"]
        assert "msg_new" not in instance._delivery["pending"]
        async with instance._delivery_lock:
            instance._delivery["pending"].pop("msg_0")
            instance._queue_space.set()
        await asyncio.wait_for(task, timeout=2)
        assert len(instance._delivery["pending"]) == 200
        assert "msg_new" in instance._delivery["pending"]
        assert all(
            f"msg_{index}" in instance._delivery["pending"] for index in range(1, 200)
        )
        assert "msg_new" in context.store["delivery"]["pending"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
