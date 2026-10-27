"""Permission-checked mail operations shared by LLM tools and other plugins."""

from __future__ import annotations

import asyncio
import copy
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import AgentlyClient


@dataclass(frozen=True)
class MailActor:
    """Identify the caller using trusted event or registered-plugin metadata.

    Args:
        key: Exact user or plugin principal, never supplied by an LLM argument.
        is_admin: Current administrator status obtained from AstrBot.
        session: Caller's current unified message origin.
        platform_id: Platform instance ID for later notification reauthorization.
        user_id: Sender ID for later notification reauthorization.
    """

    key: str
    is_admin: bool = False
    session: str = ""
    platform_id: str = ""
    user_id: str = ""


class MailPermissionError(PermissionError):
    """Indicate that an actor lacks an operation's required capability."""


class MailService:
    """Expose fixed mail operations with persisted, deny-first access control.

    Args:
        client: Official CLI adapter, with no access-control responsibility.
        data_dir: Plugin-owned storage directory supplied by AstrBot.
        persist: Async callback that durably saves the complete service state.
        config: Optional limits for body length and individual attachment size.
    """

    def __init__(
        self,
        client: AgentlyClient,
        data_dir: Path,
        persist: Callable[[dict], Awaitable[None]],
        config: dict | None = None,
    ) -> None:
        self.client = client
        self.data_dir = Path(data_dir)
        self.attachment_dir = self.data_dir / "attachments"
        self._persist = persist
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        limits = config or {}
        self._max_body_chars = int(limits.get("max_body_chars", 50000))
        self._max_attachment_bytes = int(limits.get("max_attachment_mb", 20)) * 1024**2
        if (
            self._max_body_chars < 1
            or not 0 < self._max_attachment_bytes <= 20 * 1024**2
        ):
            raise ValueError(
                "Mail limits must be positive; attachments cannot exceed 20 MiB."
            )

    @property
    def state(self) -> dict:
        """Return an independent snapshot so callers cannot mutate permissions.

        Returns:
            A deep copy of persisted access and notification settings.
        """
        return copy.deepcopy(self._state)

    async def initialize(self, state: dict | None) -> None:
        """Validate stored settings before making any mail operation available.

        Args:
            state: Stored settings, or None for administrator-only defaults.

        Raises:
            ValueError: If stored permissions or notification owners are malformed.
        """
        self._state = {}
        if state is not None and not isinstance(state, dict):
            raise ValueError("Stored mail settings must be an object.")
        candidate = copy.deepcopy(state or {})
        access = candidate.setdefault("access", {})
        if not isinstance(access, dict):
            raise ValueError("Stored mail access settings must be an object.")
        for capability in ("read", "send", "notify"):
            rule = access.setdefault(capability, {"allow": [], "deny": []})
            if not isinstance(rule, dict):
                raise ValueError(f"Invalid access rule: {capability}.")
            for mode in ("allow", "deny"):
                principals = rule.setdefault(mode, [])
                if not isinstance(principals, list):
                    raise ValueError(f"Invalid {mode} list: {capability}.")
                for principal in principals:
                    self._validate_principal(principal)
                rule[mode] = list(dict.fromkeys(principals))
        targets = candidate.setdefault("notification_targets", [])
        owners = candidate.setdefault("notification_owners", {})
        if (
            not isinstance(targets, list)
            or any(not isinstance(item, str) or not item.strip() for item in targets)
            or not isinstance(owners, dict)
        ):
            raise ValueError("Stored notification settings are malformed.")
        for target in targets:
            owner = owners.get(target)
            if not isinstance(owner, dict):
                raise ValueError("Each notification target must have an owner.")
            self._validate_principal(owner.get("key"))
            if any(
                not isinstance(owner.get(key), str)
                for key in ("platform_id", "user_id")
            ):
                raise ValueError("Notification owner identity is malformed.")
        candidate["notification_targets"] = list(dict.fromkeys(targets))
        candidate["notification_owners"] = {
            target: owners[target] for target in targets
        }
        self.attachment_dir.mkdir(parents=True, exist_ok=True)
        self._state = candidate

    @staticmethod
    def _validate_principal(principal: str) -> None:
        """Validate a principal used in actors, persisted rules, and subscriptions.

        Args:
            principal: Exact platform user identity or registered plugin identity.

        Raises:
            ValueError: If the principal is empty, malformed, or contains whitespace.
        """
        if (
            not isinstance(principal, str)
            or len(principal) > 1024
            or any(char.isspace() or ord(char) < 32 for char in principal)
            or not (
                re.fullmatch(r"user:[^:]+:.+", principal)
                or re.fullmatch(r"plugin:[A-Za-z0-9_.-]+", principal)
            )
        ):
            raise ValueError(
                "Use user:<platform_id>:<user_id> or plugin:<registered_name>."
            )

    def check_access(self, actor: MailActor, capability: str) -> None:
        """Require an explicit allow rule or current admin status, with deny first.

        Args:
            actor: Trusted caller identity.
            capability: One of read, send, or notify.

        Raises:
            MailPermissionError: If access is denied or settings are not initialized.
            ValueError: If the requested capability or actor identity is invalid.
        """
        self._validate_principal(actor.key)
        if capability not in ("read", "send", "notify"):
            raise ValueError("Unknown mail capability.")
        rule = self._state.get("access", {}).get(capability)
        if not rule or actor.key in rule["deny"]:
            raise MailPermissionError(
                f"Mail {capability} permission denied for {actor.key}."
            )
        if not actor.is_admin and actor.key not in rule["allow"]:
            raise MailPermissionError(
                f"Mail {capability} permission denied for {actor.key}."
            )

    async def set_access(
        self, admin: MailActor, capability: str, principal: str, mode: str
    ) -> None:
        """Persist an administrator's allow, deny, or revoke decision atomically.

        Args:
            admin: Current administrator, even if a mail capability is denied.
            capability: One of read, send, or notify.
            principal: Exact user or registered-plugin principal.
            mode: allow, deny, or revoke. Revoke removes both explicit entries.

        Raises:
            MailPermissionError: If the caller is not an administrator.
            ValueError: If the rule is malformed or the service is uninitialized.
        """
        if not admin.is_admin:
            raise MailPermissionError(
                "Only administrators can change mail permissions."
            )
        self._validate_principal(admin.key)
        self._validate_principal(principal)
        if capability not in ("read", "send", "notify") or mode not in (
            "allow",
            "deny",
            "revoke",
        ):
            raise ValueError("Use read/send/notify and allow/deny/revoke.")
        async with self._lock:
            if not self._state:
                raise ValueError("Mail service has not been initialized.")
            candidate = copy.deepcopy(self._state)
            rule = candidate["access"][capability]
            for entries in (rule["allow"], rule["deny"]):
                if principal in entries:
                    entries.remove(principal)
            if mode != "revoke":
                rule[mode].append(principal)
            await self._persist(copy.deepcopy(candidate))
            self._state = candidate

    async def set_notification(
        self, actor: MailActor, target: str, enabled: bool
    ) -> None:
        """Subscribe an authorized current session, or any target for an admin.

        Args:
            actor: Caller with both read and notify capabilities.
            target: Unified message origin to receive mail notifications.
            enabled: Whether to add or remove the target.

        Raises:
            MailPermissionError: If the caller lacks access or targets another session.
            ValueError: If the session identifier is invalid.
        """
        self.check_access(actor, "read")
        self.check_access(actor, "notify")
        if not isinstance(enabled, bool):
            raise ValueError("Notification enabled must be a boolean.")
        if (
            not isinstance(target, str)
            or len(target) > 2048
            or target.count(":") < 2
            or any(char.isspace() or ord(char) < 32 for char in target)
        ):
            raise ValueError("A valid unified message origin is required.")
        if not actor.is_admin and (not actor.session or target != actor.session):
            raise MailPermissionError("Members can only manage their current session.")
        async with self._lock:
            self.check_access(actor, "read")
            self.check_access(actor, "notify")
            candidate = copy.deepcopy(self._state)
            targets = candidate["notification_targets"]
            owners = candidate["notification_owners"]
            if enabled:
                if target not in targets:
                    targets.append(target)
                owners[target] = {
                    "key": actor.key,
                    "platform_id": actor.platform_id,
                    "user_id": actor.user_id,
                }
            else:
                if target in targets:
                    targets.remove(target)
                owners.pop(target, None)
            await self._persist(copy.deepcopy(candidate))
            self._state = candidate

    async def account(self, actor: MailActor) -> dict:
        """Read public mailbox identity through +me, never auth status or credentials.

        Args:
            actor: Caller requiring read capability.

        Returns:
            CLI response containing the current account and aliases.
        """
        self.check_access(actor, "read")
        return await self.client.run(["+me"])

    async def list_messages(
        self,
        actor: MailActor,
        limit: int = 10,
        cursor: str = "",
        folder: str = "inbox",
        unread_only: bool = False,
    ) -> dict:
        """List one mailbox page without implicitly reading message bodies.

        Args:
            actor: Caller requiring read capability.
            limit: Page size between 1 and 50.
            cursor: Opaque pagination cursor returned by the previous request.
            folder: inbox, sent, trash, or spam.
            unread_only: Whether to request unread messages only.

        Returns:
            Unmodified CLI envelope with messages and pagination information.

        Raises:
            ValueError: If the page size, folder, or cursor is invalid.
        """
        self.check_access(actor, "read")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Message limit must be an integer between 1 and 50.")
        if folder not in ("inbox", "sent", "trash", "spam"):
            raise ValueError("Folder must be inbox, sent, trash, or spam.")
        if not isinstance(unread_only, bool):
            raise ValueError("Unread-only must be a boolean.")
        if not isinstance(cursor, str) or len(cursor) > 8192 or "\x00" in cursor:
            raise ValueError("Invalid pagination cursor.")
        args = ["message", "+list", "--dir", folder, "--limit", str(limit)]
        if cursor:
            args.extend(["--cursor", cursor])
        if unread_only:
            args.append("--is-unread")
        return await self.client.run(args)

    async def search_messages(
        self,
        actor: MailActor,
        query: str,
        limit: int = 10,
        cursor: str = "",
        folder: str = "",
    ) -> dict:
        """Search a mailbox, retaining the query when requesting later pages.

        Args:
            actor: Caller requiring read capability.
            query: User-provided search terms.
            limit: Page size between 1 and 50.
            cursor: Opaque cursor from a previous search using the same query.
            folder: Optional inbox, sent, trash, or spam filter.

        Returns:
            Unmodified CLI search envelope.

        Raises:
            ValueError: If search terms, cursor, or page size are invalid.
        """
        self.check_access(actor, "read")
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 4096
            or "\x00" in query
        ):
            raise ValueError("A search query of at most 4096 characters is required.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Message limit must be an integer between 1 and 50.")
        if not isinstance(cursor, str) or len(cursor) > 8192 or "\x00" in cursor:
            raise ValueError("Invalid pagination cursor.")
        if folder not in ("", "inbox", "sent", "trash", "spam"):
            raise ValueError("Folder must be inbox, sent, trash, or spam.")
        args = ["message", "+search", "--q", query, "--limit", str(limit)]
        if folder:
            args.extend(["--dir", folder])
        if cursor:
            args.extend(["--cursor", cursor])
        return await self.client.run(args)

    @staticmethod
    def _validate_id(value: str, prefix: str) -> None:
        """Validate identifiers reused by read, reply, forward, and attachment calls.

        Args:
            value: Provider-generated opaque identifier.
            prefix: Expected msg or att prefix.

        Raises:
            ValueError: If the identifier is malformed.
        """
        if not isinstance(value, str) or not re.fullmatch(
            rf"{prefix}_[A-Za-z0-9_-]{{1,256}}", value
        ):
            raise ValueError(f"A valid {prefix}_ identifier is required.")

    async def read_message(self, actor: MailActor, message_id: str) -> dict:
        """Read a single message, keeping its content as untrusted external data.

        Args:
            actor: Caller requiring read capability.
            message_id: Provider-generated msg_ identifier.

        Returns:
            Unmodified CLI envelope containing body and attachment metadata.
        """
        self.check_access(actor, "read")
        self._validate_id(message_id, "msg")
        return await self.client.run(["message", "+read", "--id", message_id])

    async def send_message(
        self,
        actor: MailActor,
        to: list[str],
        subject: str,
        body: str,
        attachments: list[str] | None = None,
    ) -> dict:
        """Send exactly the email the authorized caller explicitly requested.

        Args:
            actor: Caller requiring send capability, plus read when attaching files.
            to: Recipient email addresses.
            subject: Email subject, without line breaks.
            body: Plain-text content; a draft-only request must not call this method.
            attachments: Files already staged under the plugin attachment directory.

        Returns:
            CLI send result, without retrying a potentially completed send.
        """
        return await self._write_message(
            actor, "send", to=to, subject=subject, body=body, attachments=attachments
        )

    async def reply_message(
        self,
        actor: MailActor,
        message_id: str,
        body: str,
        reply_all: bool = False,
        attachments: list[str] | None = None,
    ) -> dict:
        """Reply in the original thread after an authorized caller requests it.

        Args:
            actor: Caller requiring both read and send capabilities.
            message_id: Original msg_ identifier.
            body: Plain-text reply content.
            reply_all: Whether to include all original recipients.
            attachments: Files staged under the plugin attachment directory.

        Returns:
            CLI reply result.
        """
        return await self._write_message(
            actor,
            "reply",
            message_id=message_id,
            body=body,
            reply_all=reply_all,
            attachments=attachments,
        )

    async def forward_message(
        self,
        actor: MailActor,
        message_id: str,
        to: list[str],
        body: str = "",
        attachments: list[str] | None = None,
    ) -> dict:
        """Forward a message only to recipients explicitly requested by the caller.

        Args:
            actor: Caller requiring both read and send capabilities.
            message_id: Original msg_ identifier.
            to: New recipient email addresses.
            body: Optional plain-text introduction.
            attachments: Additional files from the plugin attachment directory.

        Returns:
            CLI forward result, including the original message's attachments.
        """
        return await self._write_message(
            actor,
            "forward",
            message_id=message_id,
            to=to,
            body=body,
            attachments=attachments,
        )

    async def _write_message(
        self,
        actor: MailActor,
        operation: str,
        *,
        to: list[str] | None = None,
        subject: str = "",
        message_id: str = "",
        body: str,
        reply_all: bool = False,
        attachments: list[str] | None = None,
    ) -> dict:
        """Stage UTF-8 text and confined files for the three fixed write operations.

        Args:
            actor: Caller requiring send capability before any file access.
            operation: send, reply, or forward.
            to: New recipients for send or forward.
            subject: New-message subject.
            message_id: Original message for reply or forward.
            body: Plain-text message content.
            reply_all: Whether a reply addresses all recipients.
            attachments: Paths confined to the plugin's attachment directory.

        Returns:
            Unmodified CLI envelope for the requested operation.

        Raises:
            ValueError: If input is invalid or an attachment escapes its directory.
        """
        self.check_access(actor, "send")
        if operation not in ("send", "reply", "forward"):
            raise ValueError("Unsupported mail write operation.")
        if operation == "reply" and not isinstance(reply_all, bool):
            raise ValueError("Reply-all must be a boolean.")
        if operation != "send" or attachments:
            self.check_access(actor, "read")
        if (
            not isinstance(body, str)
            or len(body) > self._max_body_chars
            or "\x00" in body
        ):
            raise ValueError(
                f"Body must contain at most {self._max_body_chars} characters."
            )
        if operation != "forward" and not body.strip():
            raise ValueError("A non-empty message body is required.")
        args = ["message", f"+{operation}", "--confirmed"]
        if operation != "reply":
            if not isinstance(to, list) or not 1 <= len(to) <= 50:
                raise ValueError("Provide between 1 and 50 recipient email addresses.")
            recipients: set[str] = set()
            for address in to:
                if (
                    not isinstance(address, str)
                    or len(address) > 254
                    or not re.fullmatch(
                        r"[A-Za-z0-9_+][A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]*@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",
                        address,
                    )
                ):
                    raise ValueError(
                        "Each recipient must be a valid plain email address."
                    )
                if address in recipients:
                    continue
                recipients.add(address)
                args.extend(["--to", address])
        if operation == "send":
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or len(subject) > 998
                or any(char in subject for char in ("\r", "\n", "\x00"))
            ):
                raise ValueError(
                    "A subject of 1 to 998 characters without newlines is required."
                )
            args.extend(["--subject", subject])
        else:
            self._validate_id(message_id, "msg")
            args.extend(["--id", message_id])
        if operation == "reply" and reply_all:
            args.append("--reply-all")
        if operation == "forward":
            args.append("--include-attachments")
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list) or len(attachments) > 50:
            raise ValueError("Provide at most 50 attachment paths.")
        root = self.attachment_dir.resolve(strict=True)
        sources: list[Path] = []
        for filename in attachments:
            if not isinstance(filename, str) or not filename or "\x00" in filename:
                raise ValueError("Each attachment must be a file path.")
            path = Path(filename)
            source = (path if path.is_absolute() else root / path).resolve(strict=True)
            if not source.is_relative_to(root) or not source.is_file():
                raise ValueError(
                    "Attachments must be files inside the plugin attachment directory."
                )
            if source.stat().st_size > self._max_attachment_bytes:
                raise ValueError("Attachment exceeds the configured size limit.")
            sources.append(source)
        with tempfile.TemporaryDirectory(
            prefix="outgoing-", dir=self.data_dir
        ) as directory:
            workdir = Path(directory)
            (workdir / "body.txt").write_text(body, encoding="utf-8")
            args.extend(["--body-file", "./body.txt", "--body-format", "plain"])
            for index, source in enumerate(sources):
                staged = workdir / f"{index}_{source.name}"
                await asyncio.to_thread(shutil.copyfile, source, staged)
                args.extend(["--attachment", f"./{staged.name}"])
            self.check_access(actor, "send")
            if operation != "send" or attachments:
                self.check_access(actor, "read")
            return await self.client.run(args, cwd=workdir)

    async def download_attachment(
        self, actor: MailActor, message_id: str, attachment_id: str
    ) -> dict:
        """Download an attachment to plugin-owned storage after a read-access check.

        Args:
            actor: Caller requiring read capability.
            message_id: Parent msg_ identifier.
            attachment_id: Ordinary attachment's att_ identifier.

        Returns:
            CLI result with a verified saved_to path under the attachment directory.

        Raises:
            ValueError: If identifiers or the CLI's saved path are invalid.
        """
        self.check_access(actor, "read")
        self._validate_id(message_id, "msg")
        self._validate_id(attachment_id, "att")
        result = await self.client.run(
            [
                "attachment",
                "+download",
                "--msg",
                message_id,
                "--att",
                attachment_id,
                "--output",
                "./attachments",
            ],
            cwd=self.data_dir,
        )
        data = result.get("data")
        saved_to = data.get("saved_to") if isinstance(data, dict) else None
        if not isinstance(saved_to, str) or not saved_to:
            raise ValueError("CLI did not return an attachment saved_to path.")
        saved = Path(saved_to)
        saved = (saved if saved.is_absolute() else self.data_dir / saved).resolve(
            strict=True
        )
        if (
            not saved.is_relative_to(self.attachment_dir.resolve(strict=True))
            or not saved.is_file()
        ):
            raise ValueError(
                "CLI attachment path escaped the plugin attachment directory."
            )
        return result
