"""Authenticated plugin-view endpoints for the shared Agent Mail mailbox."""

from __future__ import annotations

import asyncio
import html
import re
import uuid
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)

from .client import AgentlyError
from .service import MailActor, MailPermissionError

PLUGIN_NAME = "astrbot_plugin_agent_mail"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class MailWebUI:
    """Register a Dashboard-only management and mail interface.

    Args:
        plugin: Active mail plugin supplying the service and authorization lifecycle.
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._handlers: set = set()
        for endpoint, method, operation in (
            ("status", "GET", "status"),
            ("runtime", "GET", "runtime"),
            ("runtime/retry", "POST", "runtime_retry"),
            ("account", "GET", "account"),
            ("messages", "GET", "messages"),
            ("messages/<message_id>", "GET", "message"),
            ("send", "POST", "send"),
            ("reply", "POST", "reply"),
            ("forward", "POST", "forward"),
            ("permissions", "POST", "permissions"),
            ("notifications", "POST", "notifications"),
            ("login", "GET", "login"),
            ("login/start", "POST", "login_start"),
            ("login/cancel", "POST", "login_cancel"),
            ("logout", "POST", "logout"),
            ("attachments/upload", "POST", "upload"),
            ("attachments/download", "GET", "download"),
        ):
            handler = partial(self._dispatch, operation)
            self._handlers.add(handler)
            plugin.context.register_web_api(
                f"/{PLUGIN_NAME}/ui/{endpoint}",
                handler,
                [method],
                f"Agent Mail WebUI: {operation}",
            )

    def close(self) -> None:
        """Remove only routes still owned by this instance, preserving replacements."""
        registry = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(registry, list):
            owned = {id(handler) for handler in self._handlers}
            registry[:] = [entry for entry in registry if id(entry[1]) not in owned]
        self._handlers.clear()

    @staticmethod
    def _cli_data(result: dict) -> Any:
        """Validate envelopes shared by account, listing, details, and writes.

        Args:
            result: Complete response returned by the CLI-backed service.

        Returns:
            The response's data field.

        Raises:
            AgentlyError: If a failed or malformed envelope reaches the Web layer.
        """
        if not isinstance(result, dict) or result.get("ok") is False:
            raise AgentlyError("The mail operation did not complete successfully.")
        return result.get("data")

    async def _dispatch(self, operation: str, message_id: str = ""):
        """Authenticate a fixed operation and serialize mail with credential changes.

        Args:
            operation: Fixed operation selected during route registration.
            message_id: Message identifier extracted by AstrBot's route matcher.

        Returns:
            A plugin JSON response, or an authenticated attachment file response.
        """
        username = request.username
        if not isinstance(username, str) or not username.strip():
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        if username.startswith("api_key:"):
            return error_response(
                "邮件管理界面仅允许 Dashboard 管理员访问。", status_code=403
            )
        if not self.plugin._active:
            return error_response("Agent Mail 插件当前未启用。", status_code=503)
        actor = MailActor(
            f"user:dashboard:{quote(username, safe='')}", True, "", "dashboard", ""
        )
        if operation in {
            "account",
            "messages",
            "message",
            "send",
            "reply",
            "forward",
            "upload",
            "download",
        }:
            try:
                async with self.plugin.mail_operation():
                    return await self._execute(operation, actor, message_id)
            except MailPermissionError as exc:
                return error_response(
                    str(exc), status_code=409 if self.plugin._active else 503
                )
        return await self._execute(operation, actor, message_id)

    async def _execute(self, operation: str, actor: MailActor, message_id: str):
        """Run an authenticated route while its caller holds any required mail lock.

        Args:
            operation: Fixed operation selected by route registration.
            actor: Dashboard identity derived from the trusted request context.
            message_id: Route-matched message identifier for detail requests.

        Returns:
            A JSON response or a confined attachment download response.
        """
        if operation in {
            "account",
            "messages",
            "message",
            "send",
            "reply",
            "forward",
            "upload",
            "download",
        } and self.plugin.auth_state.get("status") in {"pending", "logged_out"}:
            return error_response(
                "邮箱授权正在变更或已退出，请先完成登录绑定。", status_code=409
            )
        try:
            if operation == "status":
                value = {
                    "active": self.plugin._active,
                    "watching": bool(
                        self.plugin._watch_task and not self.plugin._watch_task.done()
                    ),
                    "watch_error": self.plugin._watch_error,
                    "pending_notifications": len(self.plugin._delivery["pending"]),
                    "login": self.plugin.auth_state,
                    "runtime": self.plugin.runtime_state,
                    "settings": self.plugin.service.state,
                    "actor": actor.key,
                    "attachments_directory": str(self.plugin.service.attachment_dir),
                }
            elif operation == "runtime":
                value = {"runtime": self.plugin.runtime_state}
            elif operation == "runtime_retry":
                value = {"runtime": await self.plugin.retry_runtime()}
            elif operation == "account":
                generation = self.plugin.auth_generation
                account = self._cli_data(await self.plugin.service.account(actor))
                if not isinstance(account, dict):
                    raise AgentlyError(
                        "Agent Mail returned invalid account information."
                    )
                if generation != self.plugin.auth_generation:
                    return error_response(
                        "邮箱授权已变更，请重新刷新账户信息。", status_code=409
                    )
                await self.plugin.mark_authenticated(account, generation=generation)
                if generation != self.plugin.auth_generation or not self.plugin._active:
                    return error_response(
                        "邮箱授权已变更，请重新刷新账户信息。", status_code=409
                    )
                value = {"account": account}
            elif operation == "messages":
                value = await self._messages(actor)
            elif operation == "message":
                message = self._cli_data(
                    await self.plugin.service.read_message(actor, message_id)
                )
                if not isinstance(message, dict):
                    raise AgentlyError("Agent Mail returned an invalid message.")
                message = dict(message)
                body = message.get("body", "")
                if not isinstance(body, str):
                    raise AgentlyError("Agent Mail returned an invalid message body.")
                if str(message.get("body_format", "")).lower() == "html":
                    body = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", "", body)
                    body = re.sub(
                        r"(?i)<br\s*/?>|</(?:p|div|li|h[1-6])\s*>", "\n", body
                    )
                    body = html.unescape(re.sub(r"<[^>]*>", "", body))
                message["body_text"] = body
                value = {"message": message}
            elif operation == "login":
                value = {"login": self.plugin.auth_state}
            elif operation in {"login_start", "login_cancel", "logout"}:
                action = {
                    "login_start": self.plugin.start_login,
                    "login_cancel": self.plugin.cancel_login,
                    "logout": self.plugin.logout,
                }[operation]
                value = {"login": await action()}
            elif operation == "upload":
                value = await self._upload(actor)
            elif operation == "download":
                result = await self.plugin.service.download_attachment(
                    actor,
                    request.query.get("message_id", ""),
                    request.query.get("attachment_id", ""),
                )
                data = self._cli_data(result)
                saved_to = data.get("saved_to") if isinstance(data, dict) else None
                if not isinstance(saved_to, str) or not saved_to:
                    raise AgentlyError("Agent Mail did not return a saved attachment.")
                saved = Path(saved_to)
                if not saved.is_absolute():
                    saved = self.plugin.service.data_dir / saved
                saved = await asyncio.to_thread(saved.resolve, strict=True)
                root = await asyncio.to_thread(
                    self.plugin.service.attachment_dir.resolve, strict=True
                )
                if not saved.is_relative_to(root) or not await asyncio.to_thread(
                    saved.is_file
                ):
                    raise ValueError("附件路径超出插件附件目录。")
                self.plugin.service.check_access(actor, "read")
                return file_response(
                    saved, filename=saved.name, headers={"Cache-Control": "no-store"}
                )
            else:
                payload = await request.json(default=None)
                if not isinstance(payload, dict):
                    raise ValueError("请求内容必须是 JSON 对象。")
                if operation == "permissions":
                    await self.plugin.service.set_access(
                        actor,
                        payload.get("capability"),
                        payload.get("principal"),
                        payload.get("mode"),
                    )
                    await self.plugin._sync_watcher()
                    self.plugin._delivery_wakeup.set()
                    value = {"settings": self.plugin.service.state}
                elif operation == "notifications":
                    await self.plugin.service.set_notification(
                        actor, payload.get("target"), payload.get("enabled")
                    )
                    await self.plugin._sync_watcher()
                    self.plugin._delivery_wakeup.set()
                    value = {"settings": self.plugin.service.state}
                elif operation in {"send", "reply", "forward"}:
                    if operation == "send":
                        result = await self.plugin.service.send_message(
                            actor,
                            payload.get("to"),
                            payload.get("subject"),
                            payload.get("body"),
                            attachments=payload.get("attachments"),
                        )
                    elif operation == "reply":
                        result = await self.plugin.service.reply_message(
                            actor,
                            payload.get("message_id"),
                            payload.get("body"),
                            reply_all=payload.get("reply_all", False),
                            attachments=payload.get("attachments"),
                        )
                    else:
                        result = await self.plugin.service.forward_message(
                            actor,
                            payload.get("message_id"),
                            payload.get("to"),
                            body=payload.get("body", ""),
                            attachments=payload.get("attachments"),
                        )
                    self._cli_data(result)
                    value = {"result": result}
                else:
                    return error_response("未找到邮件管理操作。", status_code=404)
            return json_response(value, headers={"Cache-Control": "no-store"})
        except MailPermissionError as exc:
            return error_response(str(exc), status_code=403)
        except AgentlyError as exc:
            return error_response(
                str(exc), status_code=502, data={"exit_code": exc.exit_code}
            )
        except (ValueError, TypeError, KeyError) as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("邮件附件或设置存储不可用。", status_code=500)
        except Exception as exc:
            self.plugin.logger.error(
                "Agent Mail WebUI operation %s failed (%s).",
                operation,
                type(exc).__name__,
            )
            return error_response("邮件管理操作失败，请稍后重试。", status_code=500)

    async def _messages(self, actor: MailActor) -> dict:
        """Load one CLI page and normalize the known provider list envelopes.

        Args:
            actor: Authenticated Dashboard identity checked by the service.

        Returns:
            Message rows, an opaque next cursor, and whether another page exists.

        Raises:
            ValueError: If a query value is invalid.
            AgentlyError: If the provider returns an unknown or inconsistent shape.
        """
        limit = request.query.get("limit", "20")
        try:
            limit = int(limit)
        except (ValueError, TypeError) as exc:
            raise ValueError("邮件条数必须是 1–50 的整数。") from exc
        query = request.query.get("query", "")
        cursor = request.query.get("cursor", "")
        if query:
            result = await self.plugin.service.search_messages(
                actor, query, limit, cursor, folder=request.query.get("folder", "inbox")
            )
        else:
            result = await self.plugin.service.list_messages(
                actor, limit, cursor, request.query.get("folder", "inbox")
            )
        data = self._cli_data(result)
        if isinstance(data, list):
            messages, page = data, result
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            messages, page = data["data"], data
        elif isinstance(data, dict) and isinstance(data.get("messages"), list):
            messages, page = data["messages"], data
        else:
            raise AgentlyError("Agent Mail returned an unknown message-list format.")
        if any(not isinstance(message, dict) for message in messages):
            raise AgentlyError("Agent Mail returned invalid message-list items.")
        pagination = page.get("pagination", {})
        if not isinstance(pagination, dict):
            raise AgentlyError("Agent Mail returned invalid pagination information.")
        next_cursor = page.get("next_cursor", pagination.get("next_cursor", ""))
        if next_cursor is None:
            next_cursor = ""
        if not isinstance(next_cursor, str):
            raise AgentlyError("Agent Mail returned an invalid pagination cursor.")
        has_more = page.get("has_more", pagination.get("has_more", bool(next_cursor)))
        if not isinstance(has_more, bool) or (has_more and not next_cursor):
            raise AgentlyError(
                "Agent Mail returned inconsistent pagination information."
            )
        return {"messages": messages, "next_cursor": next_cursor, "has_more": has_more}

    async def _upload(self, actor: MailActor) -> dict:
        """Save one bounded attachment beneath plugin-owned storage.

        Args:
            actor: Dashboard identity requiring both read and send capabilities.

        Returns:
            Relative attachment path, display filename, and byte count.

        Raises:
            ValueError: If the file or filename is invalid or exceeds the size limit.
        """
        self.plugin.service.check_access(actor, "read")
        self.plugin.service.check_access(actor, "send")
        upload = (await request.files()).get("file")
        if not isinstance(upload, PluginUploadFile):
            raise ValueError("请选择一个附件文件。")
        name = (
            str(upload.filename or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
        )
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:120]
        if not name:
            raise ValueError("附件文件名无效。")
        root = await asyncio.to_thread(
            self.plugin.service.attachment_dir.resolve, strict=True
        )
        relative = f"{uuid.uuid4().hex}_{name}"
        destination = root / relative
        size = await upload.save(destination, max_bytes=MAX_UPLOAD_BYTES)
        try:
            self.plugin.service.check_access(actor, "read")
            self.plugin.service.check_access(actor, "send")
        except MailPermissionError:
            await asyncio.to_thread(destination.unlink, missing_ok=True)
            raise
        return {"path": relative, "filename": name, "size": size}
