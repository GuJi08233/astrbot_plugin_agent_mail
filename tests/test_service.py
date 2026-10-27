"""Test access boundaries and real filesystem behavior without an email account."""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service import MailActor, MailPermissionError, MailService  # noqa: E402

ADMIN = MailActor("user:bot:admin", True, "bot:FriendMessage:admin", "bot", "admin")
MEMBER = MailActor(
    "user:bot:member", False, "bot:FriendMessage:member", "bot", "member"
)
PLUGIN = MailActor("plugin:weekly_report")


class FakeClient:
    """Capture fixed CLI invocations and staged files without network access."""

    def __init__(self):
        self.calls = []
        self.body = None
        self.attachments = []
        self.result = {"ok": True, "data": {"message_id": "msg_sent"}}
        self.download_path = None

    async def run(self, args, *, cwd=None):
        self.calls.append((args, cwd))
        if "--body-file" in args:
            self.body = (cwd / args[args.index("--body-file") + 1]).read_text(
                encoding="utf-8"
            )
            self.attachments = [
                (cwd / args[index + 1]).read_bytes()
                for index, arg in enumerate(args)
                if arg == "--attachment"
            ]
        if self.download_path is not None:
            target = Path(self.download_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, b"downloaded")
            return {"ok": True, "data": {"saved_to": str(target)}}
        return copy.deepcopy(self.result)


@pytest_asyncio.fixture
async def setup(tmp_path):
    client = FakeClient()
    saved = []

    async def persist(state):
        saved.append(copy.deepcopy(state))

    service = MailService(client, tmp_path / "plugin", persist)
    await service.initialize(None)
    return service, client, saved


@pytest.mark.asyncio
async def test_default_access_and_exact_platform_identity(setup):
    service, client, _ = setup
    await service.account(ADMIN)
    assert client.calls == [(["+me"], None)]
    for actor in (MEMBER, PLUGIN):
        with pytest.raises(MailPermissionError):
            await service.account(actor)
    await service.set_access(ADMIN, "read", MEMBER.key, "allow")
    await service.account(MEMBER)
    other_bot = MailActor("user:other_bot:member")
    with pytest.raises(MailPermissionError):
        await service.account(other_bot)
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_deny_precedes_admin_and_administrator_can_recover(setup):
    service, client, _ = setup
    await service.set_access(ADMIN, "send", ADMIN.key, "deny")
    with pytest.raises(MailPermissionError):
        await service.send_message(ADMIN, ["a@example.com"], "Subject", "Body")
    assert not client.calls
    await service.set_access(ADMIN, "send", ADMIN.key, "revoke")
    await service.send_message(ADMIN, ["a@example.com"], "Subject", "Body")
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_capabilities_are_independent_for_users_and_plugins(setup):
    service, client, _ = setup
    await service.set_access(ADMIN, "read", MEMBER.key, "allow")
    await service.set_access(ADMIN, "send", PLUGIN.key, "allow")
    await service.read_message(MEMBER, "msg_a")
    await service.send_message(PLUGIN, ["a@example.com"], "Subject", "Body")
    with pytest.raises(MailPermissionError):
        await service.send_message(MEMBER, ["a@example.com"], "Subject", "Body")
    with pytest.raises(MailPermissionError):
        await service.read_message(PLUGIN, "msg_a")
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_send_only_cannot_forward_reply_or_attach_shared_mail_files(setup):
    service, client, _ = setup
    await service.set_access(ADMIN, "send", MEMBER.key, "allow")
    source = service.attachment_dir / "downloaded.pdf"
    source.write_bytes(b"private attachment")
    for operation in (
        service.forward_message(MEMBER, "msg_private", ["a@example.com"]),
        service.reply_message(MEMBER, "msg_private", "Reply"),
        service.send_message(MEMBER, ["a@example.com"], "S", "B", [source.name]),
    ):
        with pytest.raises(MailPermissionError):
            await operation
    assert not client.calls


@pytest.mark.asyncio
async def test_all_business_calls_deny_before_cli_or_file_access(setup):
    service, client, _ = setup
    operations = [
        service.account(MEMBER),
        service.list_messages(MEMBER),
        service.search_messages(MEMBER, "report"),
        service.read_message(MEMBER, "msg_a"),
        service.send_message(MEMBER, ["a@example.com"], "S", "B", ["missing.pdf"]),
        service.reply_message(MEMBER, "msg_a", "B"),
        service.forward_message(MEMBER, "msg_a", ["a@example.com"]),
        service.download_attachment(MEMBER, "msg_a", "att_a"),
    ]
    for operation in operations:
        with pytest.raises(MailPermissionError):
            await operation
    assert not client.calls


@pytest.mark.asyncio
async def test_permission_persistence_failure_does_not_grant_access(tmp_path):
    async def persist(_state):
        raise OSError("Storage unavailable")

    service = MailService(FakeClient(), tmp_path, persist)
    await service.initialize(None)
    with pytest.raises(OSError):
        await service.set_access(ADMIN, "read", MEMBER.key, "allow")
    with pytest.raises(MailPermissionError):
        service.check_access(MEMBER, "read")
    assert service.state["access"]["read"]["allow"] == []


@pytest.mark.asyncio
async def test_concurrent_updates_preserve_both_rules(tmp_path):
    stored = []

    async def persist(state):
        await asyncio.sleep(0)
        stored.append(copy.deepcopy(state))

    service = MailService(FakeClient(), tmp_path, persist)
    await service.initialize(None)
    await asyncio.gather(
        service.set_access(ADMIN, "read", MEMBER.key, "allow"),
        service.set_access(ADMIN, "send", PLUGIN.key, "allow"),
    )
    service.check_access(MEMBER, "read")
    service.check_access(PLUGIN, "send")
    assert stored[-1] == service.state


@pytest.mark.asyncio
async def test_snapshots_cannot_modify_live_permissions(setup):
    service, _, saved = setup
    await service.set_access(ADMIN, "read", MEMBER.key, "allow")
    snapshot = service.state
    snapshot["access"]["read"]["deny"].append(MEMBER.key)
    saved[0]["access"]["read"]["deny"].append(MEMBER.key)
    service.check_access(MEMBER, "read")


@pytest.mark.asyncio
async def test_malformed_reload_clears_previously_active_permissions(setup):
    service, _, _ = setup
    await service.set_access(ADMIN, "read", MEMBER.key, "allow")
    with pytest.raises(ValueError):
        await service.initialize({"access": []})
    with pytest.raises(MailPermissionError):
        service.check_access(MEMBER, "read")


@pytest.mark.asyncio
async def test_members_cannot_manage_acl_even_with_mail_permissions(setup):
    service, _, _ = setup
    for capability in ("read", "send", "notify"):
        await service.set_access(ADMIN, capability, MEMBER.key, "allow")
    with pytest.raises(MailPermissionError):
        await service.set_access(MEMBER, "send", PLUGIN.key, "allow")


@pytest.mark.asyncio
async def test_notification_requires_read_notify_and_current_session(setup):
    service, _, _ = setup
    await service.set_access(ADMIN, "notify", MEMBER.key, "allow")
    with pytest.raises(MailPermissionError):
        await service.set_notification(MEMBER, MEMBER.session, True)
    await service.set_access(ADMIN, "read", MEMBER.key, "allow")
    with pytest.raises(MailPermissionError):
        await service.set_notification(MEMBER, ADMIN.session, True)
    await service.set_notification(MEMBER, MEMBER.session, True)
    state = service.state
    assert state["notification_targets"] == [MEMBER.session]
    assert state["notification_owners"][MEMBER.session] == {
        "key": MEMBER.key,
        "platform_id": "bot",
        "user_id": "member",
    }
    await service.set_notification(MEMBER, MEMBER.session, False)
    assert service.state["notification_targets"] == []
    assert service.state["notification_owners"] == {}


@pytest.mark.asyncio
async def test_administrator_can_bind_arbitrary_session_and_reload_state(setup):
    service, _, saved = setup
    await service.set_notification(ADMIN, "other:GroupMessage:123", True)
    original = service.state
    await service.initialize(saved[-1])
    assert service.state == original
    assert (
        service.state["notification_owners"]["other:GroupMessage:123"]["key"]
        == ADMIN.key
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        [],
        {"access": []},
        {"access": {"read": {"allow": "everyone"}}},
        {"access": {"read": {"allow": ["*"]}}},
        {"notification_targets": ["bot:FriendMessage:user"]},
        {"notification_targets": "bot:FriendMessage:user"},
    ],
)
async def test_malformed_stored_state_fails_closed(tmp_path, state):
    async def persist(_state):
        pass

    service = MailService(FakeClient(), tmp_path, persist)
    with pytest.raises(ValueError):
        await service.initialize(state)
    with pytest.raises(MailPermissionError):
        service.check_access(ADMIN, "read")


@pytest.mark.asyncio
async def test_search_preserves_query_and_cursor_and_returns_envelope(setup):
    service, client, _ = setup
    client.result = {
        "ok": True,
        "data": {"data": [{"message_id": "msg_a"}], "cursor": "next"},
    }
    result = await service.search_messages(ADMIN, "季度周报", 7, "opaque cursor")
    assert result == client.result
    assert client.calls[-1][0] == [
        "message",
        "+search",
        "--q",
        "季度周报",
        "--limit",
        "7",
        "--cursor",
        "opaque cursor",
    ]


@pytest.mark.asyncio
async def test_list_unread_is_a_fixed_cli_flag(setup):
    service, client, _ = setup
    await service.list_messages(ADMIN, folder="sent", cursor="page", unread_only=True)
    args, _ = client.calls[-1]
    assert args == [
        "message",
        "+list",
        "--dir",
        "sent",
        "--limit",
        "10",
        "--cursor",
        "page",
        "--is-unread",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
async def test_boolean_flags_reject_non_booleans_before_cli(setup, value):
    service, client, _ = setup
    with pytest.raises(ValueError, match="Unread-only"):
        await service.list_messages(ADMIN, unread_only=value)
    with pytest.raises(ValueError, match="Reply-all"):
        await service.reply_message(ADMIN, "msg_original", "Reply", reply_all=value)
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 51, True, "10", 2.5])
async def test_list_rejects_invalid_limits_before_cli(setup, limit):
    service, client, _ = setup
    with pytest.raises(ValueError):
        await service.list_messages(ADMIN, limit=limit)
    assert not client.calls


@pytest.mark.asyncio
async def test_utf8_body_and_attachments_are_staged_with_relative_paths(setup):
    service, client, _ = setup
    source = service.attachment_dir / "季度 报告.pdf"
    source.write_bytes(b"pdf contents")
    result = await service.send_message(
        ADMIN,
        ["a@example.com", "a@example.com"],
        "季度总结",
        "正文\n第二行 | $()",
        [source.name],
    )
    args, workdir = client.calls[-1]
    assert args[:3] == ["message", "+send", "--confirmed"]
    assert args.count("--to") == 1
    assert "--body" not in args
    assert client.body == "正文\n第二行 | $()"
    assert client.attachments == [b"pdf contents"]
    assert not workdir.exists()
    assert result["ok"] is True
    assert source.exists()


@pytest.mark.asyncio
async def test_reply_and_forward_preserve_threads(setup):
    service, client, _ = setup
    await service.reply_message(ADMIN, "msg_original", "Reply", reply_all=True)
    args, _ = client.calls[-1]
    assert args[:3] == ["message", "+reply", "--confirmed"]
    assert args[args.index("--id") + 1] == "msg_original"
    assert "--reply-all" in args
    await service.forward_message(ADMIN, "msg_original", ["a@example.com"])
    args, _ = client.calls[-1]
    assert args[:3] == ["message", "+forward", "--confirmed"]
    assert "--include-attachments" in args


@pytest.mark.asyncio
async def test_outside_attachment_is_rejected_before_cli(setup, tmp_path):
    service, client, _ = setup
    outside = tmp_path / "private.txt"
    outside.write_text("secret", encoding="utf-8")
    for path in (str(outside), "../../private.txt"):
        with pytest.raises(ValueError):
            await service.send_message(
                ADMIN, ["a@example.com"], "Subject", "Body", [path]
            )
    assert not client.calls


@pytest.mark.asyncio
async def test_symlink_cannot_escape_attachment_directory(setup, tmp_path):
    service, client, _ = setup
    outside = tmp_path / "private.txt"
    outside.write_text("secret", encoding="utf-8")
    link = service.attachment_dir / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows does not permit symlink creation in this session")
        raise
    with pytest.raises(ValueError):
        await service.send_message(
            ADMIN, ["a@example.com"], "Subject", "Body", [link.name]
        )
    assert not client.calls


@pytest.mark.asyncio
async def test_oversize_attachment_and_header_injection_are_rejected(setup):
    service, client, _ = setup
    source = service.attachment_dir / "large.bin"
    with source.open("wb") as file:
        file.truncate(20 * 1024**2 + 1)
    with pytest.raises(ValueError):
        await service.send_message(
            ADMIN, ["a@example.com"], "Subject", "Body", [source.name]
        )
    with pytest.raises(ValueError):
        await service.send_message(
            ADMIN, ["a@example.com\r\nBcc: b@example.com"], "Subject", "Body"
        )
    with pytest.raises(ValueError):
        await service.send_message(
            ADMIN, ["a@example.com"], "Subject\nInjected", "Body"
        )
    assert not client.calls


@pytest.mark.asyncio
async def test_failed_send_is_not_retried_and_temporary_files_are_cleaned(setup):
    service, client, _ = setup

    async def fail(args, *, cwd=None):
        client.calls.append((args, cwd))
        raise RuntimeError("Outcome unknown")

    client.run = fail
    with pytest.raises(RuntimeError, match="Outcome unknown"):
        await service.send_message(ADMIN, ["a@example.com"], "Subject", "Body")
    assert len(client.calls) == 1
    assert not client.calls[0][1].exists()


@pytest.mark.asyncio
async def test_download_is_confined_and_returns_provider_saved_path(setup):
    service, client, _ = setup
    client.download_path = service.attachment_dir / "report.pdf"
    result = await service.download_attachment(ADMIN, "msg_a", "att_a")
    assert result["data"]["saved_to"] == str(client.download_path)
    args, cwd = client.calls[-1]
    assert cwd == service.data_dir
    assert args[-2:] == ["--output", "./attachments"]


@pytest.mark.asyncio
async def test_download_rejects_unexpected_saved_path(setup, tmp_path):
    service, client, _ = setup
    client.download_path = tmp_path / "outside.pdf"
    with pytest.raises(ValueError, match="escaped"):
        await service.download_attachment(ADMIN, "msg_a", "att_a")


@pytest.mark.asyncio
async def test_invalid_message_ids_do_not_invoke_cli(setup):
    service, client, _ = setup
    for message_id in ("--help", "msg_../../etc/passwd", "msg_", "msg_a\x00"):
        with pytest.raises(ValueError):
            await service.read_message(ADMIN, message_id)
    with pytest.raises(ValueError):
        await service.download_attachment(ADMIN, "msg_a", "--help")
    assert not client.calls
