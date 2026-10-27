"""Exercise WebUI authorization and mail contracts without an AstrBot server."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import logging
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
CURRENT_REQUEST = ContextVar("mail_webui_test_request")


class Query(dict):
    """Provide the query access used by the public plugin request API."""

    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        return type(value) if type is not None else value


class Request:
    """Keep all supplied request fields separate from the trusted username."""

    def __init__(self, username="admin", query=None, body=None, files=None):
        self.username = username
        self.query = Query(query or {})
        self.payload = body
        self.uploads = files or {}

    async def json(self, default=None):
        return self.payload if self.payload is not None else default

    async def files(self):
        return self.uploads


class RequestProxy:
    """Bind each test invocation to the same style of ContextVar proxy."""

    def __getattr__(self, key):
        return getattr(CURRENT_REQUEST.get(), key)


class Upload:
    """Simulate bounded async upload saving into disposable storage."""

    def __init__(self, filename, content=b"attachment"):
        self.filename = filename
        self.content = content
        self.saved = None

    async def save(self, destination, *, max_bytes=None):
        if len(self.content) > max_bytes:
            raise ValueError("Uploaded file exceeds the limit")
        await asyncio.to_thread(destination.write_bytes, self.content)
        self.saved = destination
        return len(self.content)


class Client:
    """Capture CLI operations and return controlled envelopes."""

    def __init__(self):
        self.calls = []
        self.result = {"ok": True, "data": {"data": [], "next_cursor": ""}}
        self.failure = None
        self.saved_to = None

    async def run(self, args, *, cwd=None):
        self.calls.append((args, cwd))
        if self.failure:
            raise self.failure
        if self.saved_to is not None:
            destination = Path(self.saved_to)
            await asyncio.to_thread(destination.write_bytes, b"downloaded")
            return {"ok": True, "data": {"saved_to": str(destination)}}
        return copy.deepcopy(self.result)


class Context:
    """Mirror registration's route replacement semantics."""

    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods, description):
        entry = (route, handler, methods, description)
        for index, previous in enumerate(self.registered_web_apis):
            if previous[0] == route and previous[2] == methods:
                self.registered_web_apis[index] = entry
                break
        else:
            self.registered_web_apis.append(entry)


class Plugin:
    """Expose only the lifecycle methods the Web adapter is allowed to invoke."""

    def __init__(self, service, context, permission_error):
        self.service = service
        self.context = context
        self.logger = logging.getLogger("agent-mail-webui-test")
        self._active = True
        self._watch_task = None
        self._watch_error = ""
        self._delivery = {"pending": {}}
        self._delivery_wakeup = asyncio.Event()
        self.auth_state = {"status": "idle", "url": ""}
        self.auth_generation = 0
        self._operation_lock = asyncio.Lock()
        self.permission_error = permission_error
        self.syncs = 0

    async def _sync_watcher(self):
        self.syncs += 1

    @asynccontextmanager
    async def mail_operation(self):
        async with self._operation_lock:
            if not self._active or self.auth_state.get("status") in {
                "pending",
                "logged_out",
            }:
                raise self.permission_error(
                    "Mailbox authorization is changing or logged out."
                )
            yield

    async def mark_authenticated(self, account, *, generation=None):
        if generation is not None and generation != self.auth_generation:
            return
        self.auth_state = {"status": "authenticated", "account": account, "url": ""}

    async def start_login(self):
        async with self._operation_lock:
            self.auth_generation += 1
            self.auth_state = {
                "status": "pending",
                "url": "https://agent.qq.com/oauth?state=a%2Bb",
            }
            return self.auth_state

    async def cancel_login(self):
        self.auth_generation += 1
        self.auth_state = {"status": "cancelled", "url": ""}
        return self.auth_state

    async def logout(self):
        async with self._operation_lock:
            self.auth_generation += 1
            self.auth_state = {"status": "logged_out", "url": ""}
            return self.auth_state


@pytest_asyncio.fixture
async def web(tmp_path, monkeypatch):
    """Load real adapter/service code with a minimal public Web API substitute."""
    module = ModuleType("astrbot.api.web")
    module.request = RequestProxy()
    module.PluginUploadFile = Upload
    module.json_response = lambda data=None, status_code=200, headers=None: (
        SimpleNamespace(status_code=status_code, data=data, headers=headers)
    )
    module.error_response = lambda message, status_code=400, data=None, **kwargs: (
        SimpleNamespace(
            status_code=status_code,
            data={"status": "error", "message": message, "data": data},
        )
    )
    module.file_response = lambda path, filename=None, **kwargs: SimpleNamespace(
        status_code=200, path=path, filename=filename
    )
    for name in ("astrbot", "astrbot.api"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, "astrbot.api.web", module)
    package_name = "_agent_mail_webui_test"
    package = ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)
    adapter = importlib.import_module(f"{package_name}.webui")
    service_module = importlib.import_module(f"{package_name}.service")
    client = Client()
    stored = []

    async def persist(state):
        stored.append(copy.deepcopy(state))

    service = service_module.MailService(client, tmp_path, persist)
    await service.initialize({})
    plugin = Plugin(service, Context(), service_module.MailPermissionError)
    ui = adapter.MailWebUI(plugin)

    async def call(
        operation, *, username="admin", query=None, body=None, files=None, message_id=""
    ):
        token = CURRENT_REQUEST.set(Request(username, query, body, files))
        try:
            return await ui._dispatch(operation, message_id)
        finally:
            CURRENT_REQUEST.reset(token)

    try:
        yield SimpleNamespace(
            call=call,
            ui=ui,
            module=adapter,
            service=service,
            plugin=plugin,
            client=client,
            stored=stored,
            actor=service_module.MailActor,
        )
    finally:
        ui.close()
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                sys.modules.pop(name, None)


@pytest.mark.parametrize(
    "username,code", [(None, 401), ("", 401), ("api_key:key_123", 403)]
)
async def test_all_operations_reject_untrusted_identity_before_cli(web, username, code):
    for operation in (
        "status",
        "account",
        "send",
        "login_start",
        "permissions",
        "upload",
        "download",
    ):
        response = await web.call(
            operation, username=username, body={"username": "admin", "is_admin": True}
        )
        assert response.status_code == code
    assert not web.client.calls
    assert web.plugin.auth_state["status"] == "idle"


async def test_inactive_plugin_refuses_mail_management(web):
    web.plugin._active = False
    assert (await web.call("account")).status_code == 503
    assert not web.client.calls


async def test_status_is_local_and_does_not_probe_mailbox(web):
    response = await web.call("status", username="a name")
    assert response.status_code == 200
    assert response.data["actor"] == "user:dashboard:a%20name"
    assert not web.client.calls
    assert len(web.plugin.context.registered_web_apis) == 15


async def test_account_validates_and_refreshes_authorization_state(web):
    web.client.result = {"ok": True, "data": {"email": "agent@agent.qq.com"}}
    response = await web.call("account")
    assert response.status_code == 200
    assert response.data == {"account": {"email": "agent@agent.qq.com"}}
    assert web.plugin.auth_state["status"] == "authenticated"
    assert web.client.calls[0][0] == ["+me"]


async def test_stale_account_response_cannot_overwrite_an_authorization_change(web):
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_account(args, *, cwd=None):
        started.set()
        await release.wait()
        return {"ok": True, "data": {"email": "old@agent.qq.com"}}

    web.client.run = delayed_account
    task = asyncio.create_task(web.call("account"))
    await asyncio.wait_for(started.wait(), timeout=2)
    web.plugin.auth_generation += 1
    web.plugin.auth_state = {"status": "pending"}
    release.set()
    response = await asyncio.wait_for(task, timeout=2)
    assert response.status_code == 409
    assert "old@agent.qq.com" not in json.dumps(response.data)
    assert web.plugin.auth_state["status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "logged_out"])
async def test_mail_operations_pause_while_authorization_changes(web, status):
    web.plugin.auth_state = {"status": status}
    for operation in (
        "account",
        "messages",
        "message",
        "send",
        "reply",
        "forward",
        "upload",
        "download",
    ):
        assert (await web.call(operation)).status_code == 409
    assert (await web.call("status")).status_code == 200
    assert (await web.call("login")).status_code == 200
    assert not web.client.calls


async def test_logout_waits_for_an_in_flight_web_send_to_finish(web):
    send_started, release_send, logout_started = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def delayed_send(args, *, cwd=None):
        send_started.set()
        await release_send.wait()
        return {"ok": True, "data": {"message_id": "msg_sent"}}

    original_logout = web.plugin.logout

    async def tracked_logout():
        logout_started.set()
        return await original_logout()

    web.client.run = delayed_send
    web.plugin.logout = tracked_logout
    sending = asyncio.create_task(
        web.call(
            "send",
            body={
                "to": ["person@example.com"],
                "subject": "Subject",
                "body": "Body",
            },
        )
    )
    await asyncio.wait_for(send_started.wait(), timeout=2)
    logging_out = asyncio.create_task(web.call("logout"))
    try:
        await asyncio.wait_for(logout_started.wait(), timeout=2)
        assert not logging_out.done()
        assert web.plugin.auth_state["status"] == "idle"
        release_send.set()
        sent, logged_out = await asyncio.wait_for(
            asyncio.gather(sending, logging_out), timeout=2
        )
        assert sent.status_code == logged_out.status_code == 200
        assert sent.data["result"]["data"]["message_id"] == "msg_sent"
        assert logged_out.data["login"]["status"] == "logged_out"
    finally:
        release_send.set()
        sending.cancel()
        logging_out.cancel()
        await asyncio.gather(sending, logging_out, return_exceptions=True)


@pytest.mark.parametrize("shape", ["data", "messages", "direct"])
async def test_lists_normalize_known_shapes_and_preserve_pagination(web, shape):
    rows = [{"message_id": "msg_1", "subject": "Report"}]
    data = {shape: rows, "next_cursor": "opaque-next"}
    web.client.result = {"ok": True, "data": data}
    if shape == "direct":
        web.client.result = {"ok": True, "data": rows, "next_cursor": "opaque-next"}
    response = await web.call("messages", query={"folder": "sent", "limit": "20"})
    assert response.status_code == 200
    assert response.data == {
        "messages": rows,
        "next_cursor": "opaque-next",
        "has_more": True,
    }
    assert web.client.calls[0][0] == [
        "message",
        "+list",
        "--dir",
        "sent",
        "--limit",
        "20",
    ]


async def test_search_keeps_query_and_opaque_cursor(web):
    response = await web.call(
        "messages",
        query={
            "query": "contract",
            "cursor": "next-token",
            "limit": "5",
            "folder": "sent",
        },
    )
    assert response.status_code == 200
    assert web.client.calls[0][0] == [
        "message",
        "+search",
        "--q",
        "contract",
        "--limit",
        "5",
        "--dir",
        "sent",
        "--cursor",
        "next-token",
    ]


@pytest.mark.parametrize(
    "data",
    [{"unknown": []}, {"data": ["not a message"]}, {"data": [], "has_more": True}],
)
async def test_unknown_lists_fail_instead_of_reporting_an_empty_inbox(web, data):
    web.client.result = {"ok": True, "data": data}
    assert (await web.call("messages")).status_code == 502


async def test_message_html_is_converted_to_inert_text(web):
    web.client.result = {
        "ok": True,
        "data": {
            "message_id": "msg_1",
            "body_format": "HTML",
            "body": '<div>Hello &amp; welcome</div><script>steal()</script><img src="https://evil.invalid/tracker" onerror="attack()">',
        },
    }
    response = await web.call("message", message_id="msg_1")
    assert response.status_code == 200
    assert response.data["message"]["body_text"] == "Hello & welcome\n"
    assert response.data["message"]["message_id"] == "msg_1"


async def test_body_identity_cannot_bypass_dashboard_blacklist(web):
    admin = web.actor("user:bot:admin", True)
    await web.service.set_access(admin, "send", "user:dashboard:admin", "deny")
    response = await web.call(
        "send",
        body={
            "to": ["person@example.com"],
            "subject": "Subject",
            "body": "Body",
            "actor": "user:bot:admin",
            "is_admin": True,
        },
    )
    assert response.status_code == 403
    assert not web.client.calls


async def test_write_validates_inputs_and_never_reports_cli_false_as_success(web):
    invalid = await web.call(
        "send", body={"to": ["--bad"], "subject": "S", "body": "B"}
    )
    assert invalid.status_code == 400
    assert not web.client.calls
    web.client.result = {
        "ok": False,
        "error": {"message": "denied", "credential": "must-not-return"},
    }
    failed = await web.call(
        "send", body={"to": ["person@example.com"], "subject": "S", "body": "B"}
    )
    assert failed.status_code == 502
    assert "must-not-return" not in json.dumps(failed.data)


async def test_permissions_and_notifications_persist_trusted_dashboard_owner(web):
    response = await web.call(
        "permissions",
        body={"capability": "read", "principal": "user:bot:member", "mode": "allow"},
    )
    assert response.status_code == 200
    assert web.stored[-1]["access"]["read"]["allow"] == ["user:bot:member"]
    response = await web.call(
        "notifications", body={"target": "bot:FriendMessage:owner", "enabled": True}
    )
    assert response.status_code == 200
    assert web.stored[-1]["notification_owners"]["bot:FriendMessage:owner"] == {
        "key": "user:dashboard:admin",
        "platform_id": "dashboard",
        "user_id": "",
    }
    assert web.plugin.syncs == 2
    assert web.plugin._delivery_wakeup.is_set()


async def test_login_routes_wrap_states_and_preserve_opaque_url(web):
    response = await web.call("login_start")
    assert response.data["login"]["url"] == "https://agent.qq.com/oauth?state=a%2Bb"
    assert (await web.call("login")).data == response.data
    assert (await web.call("login_cancel")).data["login"]["status"] == "cancelled"
    assert (await web.call("logout")).data["login"]["status"] == "logged_out"


async def test_upload_uses_unique_confined_paths_and_download_rejects_arbitrary_paths(
    web,
):
    upload = Upload("../../folder\\invoice.pdf", b"invoice")
    response = await web.call("upload", files={"file": upload})
    assert response.status_code == 200
    assert response.data["filename"] == "invoice.pdf"
    assert upload.saved.parent == web.service.attachment_dir.resolve()
    assert upload.saved.name == response.data["path"]
    assert upload.saved.read_bytes() == b"invoice"
    bad = await web.call(
        "download", query={"message_id": "../../secret", "attachment_id": "att_1"}
    )
    assert bad.status_code == 400
    assert not web.client.calls


async def test_download_returns_only_verified_plugin_owned_file(web):
    web.client.saved_to = web.service.attachment_dir / "document.pdf"
    response = await web.call(
        "download", query={"message_id": "msg_1", "attachment_id": "att_1"}
    )
    assert response.status_code == 200
    assert response.path == web.client.saved_to.resolve()
    assert response.filename == "document.pdf"


async def test_old_instance_cleanup_preserves_replacement_routes(web):
    newer = web.module.MailWebUI(web.plugin)
    assert len(web.plugin.context.registered_web_apis) == 15
    web.ui.close()
    assert len(web.plugin.context.registered_web_apis) == 15
    newer.close()
    assert not web.plugin.context.registered_web_apis


async def test_cleanup_preserves_unrelated_unhashable_callable_routes(web):
    class Handler:
        __hash__ = None

        async def __call__(self):
            return None

    handler = Handler()
    web.plugin.context.register_web_api("/another/plugin", handler, ["GET"], "Other")
    web.ui.close()
    assert len(web.plugin.context.registered_web_apis) == 1
    assert web.plugin.context.registered_web_apis[0][1] is handler
