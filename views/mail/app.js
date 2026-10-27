const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;
const CAPABILITIES = { read: "读信", send: "发信", notify: "通知" };

export function parseRecipients(value) {
  return [...new Set(String(value).split(/[,;，；\n]+/).map((item) => item.trim()).filter(Boolean))];
}

export function senderText(value) {
  if (typeof value === "string") return value;
  return String(value?.name || value?.email || "未知发件人");
}

export function messageBody(message) {
  if (typeof message?.body_text === "string") return message.body_text;
  return typeof message?.body === "string" ? message.body : "";
}

export function formatDate(value, compact = false) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", compact
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

export function formatSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "";
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MiB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KiB`;
  return `${size} B`;
}

const state = {
  section: "inbox", folder: "inbox", messages: [], selectedId: "", detail: null,
  cursor: "", hasMore: false, query: "", listRequest: 0, detailRequest: 0,
  settings: { access: {}, notification_targets: [], notification_owners: {} },
  login: { status: "idle" }, account: null, composeMode: "send", replyId: "",
  attachments: [], uploading: false, sending: false, loginTimer: null,
};
let bridge;
const $ = (id) => document.getElementById(id);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${name}`);
  svg.append(use);
  return svg;
}

function actionButton(label, iconName, className = "button button-tonal") {
  const button = element("button", className);
  button.type = "button";
  if (iconName) button.append(icon(iconName));
  button.append(document.createTextNode(label));
  return button;
}

function feedback(message, error = false) {
  $("feedback-text").textContent = message;
  $("feedback").classList.toggle("error", error);
  $("feedback").hidden = false;
}

async function action(button, callback) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    await callback();
  } catch (error) {
    feedback(error?.message || "操作失败，请稍后重试。", true);
  } finally {
    button.disabled = button.id === "login-start" && state.login.status === "pending";
  }
}

function emptyState(title, description, compact = false, iconName = "mail") {
  const wrapper = element("div", `empty-state${compact ? " list-empty" : ""}`);
  const mark = element("span", "empty-icon");
  mark.append(icon(iconName));
  wrapper.append(mark, element("h2", "", title), element("p", "", description));
  return wrapper;
}

function accountAddress() {
  if (!state.account) return "尚未绑定或登录已失效";
  const alias = state.account.aliases?.[0];
  return String(state.account.email || state.account.address || (typeof alias === "string" ? alias : alias?.email) || "邮箱已授权");
}

function renderAccount() {
  $("account-address").textContent = accountAddress();
  $("settings-account").textContent = accountAddress();
  $("sidebar-status").textContent = state.account ? "邮箱已连接" : "等待邮箱授权";
  $("sidebar-connection").classList.toggle("connected", Boolean(state.account));
}

function navigate(section) {
  state.section = section;
  const mailbox = section === "inbox" || section === "sent";
  $("mailbox-section").hidden = !mailbox;
  $("compose-section").hidden = section !== "compose";
  $("settings-section").hidden = section !== "settings";
  $("page-title").textContent = { inbox: "收件箱", sent: "已发送", compose: "写邮件", settings: "登录与管理" }[section];
  document.querySelectorAll("#mail-nav [data-section]").forEach((button) => {
    const selected = button.dataset.section === section;
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (mailbox) {
    state.folder = section;
    state.query = "";
    $("search-query").value = "";
    $("mailbox-grid").classList.remove("detail-open");
    void loadMessages();
  } else if (section === "settings") {
    void loadStatus().catch((error) => feedback(error.message, true));
  }
}

async function loadMessages(append = false) {
  const request = ++state.listRequest;
  const folder = state.folder;
  const query = state.query;
  const cursor = append ? state.cursor : "";
  $("message-list").setAttribute("aria-busy", "true");
  $("load-more-button").disabled = true;
  $("mailbox-summary").textContent = append ? "正在加载更多邮件…" : "正在加载邮件…";
  if (!append) {
    state.messages = [];
    state.cursor = "";
    state.hasMore = false;
    state.selectedId = "";
    state.detail = null;
    state.detailRequest += 1;
    $("message-list").replaceChildren(emptyState("正在读取邮件", "请稍候，正在连接邮箱服务。", true));
    $("message-detail").replaceChildren(emptyState("选择一封邮件", "查看正文、下载附件，或在原邮件线程中回复。"));
  }
  try {
    const result = await bridge.apiGet("ui/messages", { folder, query, cursor, limit: 20 });
    if (request !== state.listRequest) return;
    const messages = Array.isArray(result.messages) ? result.messages : [];
    const existing = new Set(state.messages.map((message) => message.message_id));
    state.messages = append ? [...state.messages, ...messages.filter((message) => !existing.has(message.message_id))] : messages;
    state.cursor = typeof result.next_cursor === "string" ? result.next_cursor : "";
    state.hasMore = Boolean(result.has_more && state.cursor);
    renderMessageList();
    $("mailbox-summary").textContent = query ? `${folder === "sent" ? "已发送" : "收件箱"}搜索结果 · 已加载 ${state.messages.length} 封` : `已加载 ${state.messages.length} 封邮件`;
  } catch (error) {
    if (request !== state.listRequest) return;
    feedback(error.message || "无法读取邮件。", true);
    if (!append) $("message-list").replaceChildren(emptyState("暂时无法读取邮件", "请检查邮箱登录状态，或点击右上角刷新后重试。", true));
    $("mailbox-summary").textContent = "邮件读取失败";
  } finally {
    if (request === state.listRequest) {
      $("message-list").setAttribute("aria-busy", "false");
      $("load-more-button").hidden = !state.hasMore;
      $("load-more-button").disabled = false;
    }
  }
}

function renderMessageList() {
  const list = $("message-list");
  list.replaceChildren();
  $("folder-label").textContent = state.query ? "当前文件夹搜索结果" : state.folder === "sent" ? "已发送" : "收件箱";
  $("page-count").textContent = `${state.messages.length} 封`;
  $("inbox-count").hidden = true;
  if (!state.messages.length) {
    list.append(emptyState(state.query ? "没有找到相关邮件" : "这里还没有邮件", state.query ? "试试其他关键词，或清空搜索查看邮件。" : "邮箱准备好了，新邮件到达后可在这里查看。", true));
    return;
  }
  for (const message of state.messages) {
    const row = element("button", `mail-row${message.message_id === state.selectedId ? " selected" : ""}`);
    row.type = "button";
    row.dataset.messageId = message.message_id || "";
    row.setAttribute("aria-pressed", String(message.message_id === state.selectedId));
    const top = element("span", "mail-row-top");
    const sender = element("span", "mail-sender");
    if (message.is_read === false) sender.append(element("i", "unread-dot"));
    sender.append(element("span", "", state.folder === "sent" && !state.query ? (message.to || []).map(senderText).join("、") || "收件人" : senderText(message.from)));
    const time = element("time", "", formatDate(message.created_at, true));
    top.append(sender, time);
    row.append(top, element("span", "mail-subject", message.subject || "（无主题）"), element("span", "mail-snippet", message.snippet || "点击阅读完整邮件"));
    row.addEventListener("click", () => { void openMessage(message.message_id); });
    list.append(row);
  }
}

async function openMessage(messageId) {
  if (!messageId) return;
  const request = ++state.detailRequest;
  state.selectedId = messageId;
  state.detail = null;
  renderMessageList();
  $("mailbox-grid").classList.add("detail-open");
  $("message-detail").replaceChildren(emptyState("正在读取正文", "邮件内容会以纯文本安全展示。"));
  try {
    const result = await bridge.apiGet(`ui/messages/${encodeURIComponent(messageId)}`);
    if (request !== state.detailRequest) return;
    state.detail = result.message;
    renderDetail();
  } catch (error) {
    if (request !== state.detailRequest) return;
    const wrapper = emptyState("邮件读取失败", error.message || "请重试。 ");
    const back = actionButton("返回邮件列表", "arrow", "button button-tonal");
    back.addEventListener("click", () => $("mailbox-grid").classList.remove("detail-open"));
    wrapper.append(back);
    $("message-detail").replaceChildren(wrapper);
    feedback(error.message || "邮件读取失败。", true);
  }
}

function renderDetail() {
  const message = state.detail;
  if (!message || typeof message !== "object") return;
  const detail = $("message-detail");
  detail.replaceChildren();
  const topline = element("div", "detail-topline");
  const back = actionButton("返回列表", "arrow", "button button-text detail-back");
  back.addEventListener("click", () => $("mailbox-grid").classList.remove("detail-open"));
  topline.append(back, element("span", "detail-badge", state.query ? "搜索结果" : state.folder === "sent" ? "已发送" : "收件箱"));
  detail.append(topline, element("h2", "detail-subject", message.subject || "（无主题）"));
  const sender = element("div", "sender-block");
  const name = senderText(message.from);
  const info = element("div", "sender-info");
  info.append(element("strong", "", name));
  if (typeof message.from?.email === "string" && message.from.email !== name) info.append(element("p", "", message.from.email));
  info.append(element("p", "", `收件人：${(message.to || []).map((item) => typeof item === "string" ? item : item.email || item.name || "").join("、") || "—"}`));
  sender.append(element("span", "sender-avatar", name.slice(0, 1).toUpperCase()), info, element("time", "", formatDate(message.created_at)));
  detail.append(sender, element("pre", "message-body", messageBody(message) || "（此邮件没有正文）"));
  if (Array.isArray(message.attachments) && message.attachments.length) {
    const files = element("div", "detail-attachments");
    files.append(element("span", "field-caption", `${message.attachments.length} 个附件`));
    for (const attachment of message.attachments) {
      const row = element("div", "attachment-file");
      const fileInfo = element("div");
      fileInfo.append(element("strong", "", attachment.filename || "附件"), element("span", "", formatSize(attachment.size)));
      row.append(icon("attach"), fileInfo);
      if (attachment.attachment_id) {
        const download = actionButton("下载", "download", "button button-text");
        download.addEventListener("click", () => action(download, async () => {
          await bridge.download("ui/attachments/download", { message_id: message.message_id || state.selectedId, attachment_id: attachment.attachment_id }, attachment.filename || "attachment");
        }));
        row.append(download);
      } else row.append(element("span", "", "请在邮件服务中查看"));
      files.append(row);
    }
    detail.append(files);
  }
  const actions = element("div", "detail-actions");
  for (const [id, label, mode, iconName] of [["reply-button", "回复", "reply", "reply"], ["reply-all-button", "回复全部", "reply-all", "reply"], ["forward-button", "转发", "forward", "forward"]]) {
    const button = actionButton(label, iconName, "button button-tonal");
    button.id = id;
    button.addEventListener("click", () => composeFromMessage(mode));
    actions.append(button);
  }
  detail.append(actions);
}

function resetCompose() {
  $("compose-form").reset();
  state.composeMode = "send";
  state.replyId = "";
  state.attachments = [];
  $("compose-to-row").hidden = false;
  $("compose-subject-row").hidden = false;
  $("compose-to").required = true;
  $("compose-subject").required = true;
  $("compose-body").required = true;
  $("compose-heading").textContent = "写一封新邮件";
  $("compose-description").textContent = "通过当前绑定的 Agent Mail 邮箱发送。草稿仅保留在当前页面。";
  $("send-label").textContent = "发送邮件";
  $("compose-context").hidden = true;
  renderAttachments();
}

function composeFromMessage(mode) {
  if (!state.detail || state.sending || state.uploading) return;
  const message = state.detail;
  resetCompose();
  state.composeMode = mode;
  state.replyId = message.message_id || state.selectedId;
  const replying = mode !== "forward";
  $("compose-to-row").hidden = replying;
  $("compose-to").required = !replying;
  $("compose-subject-row").hidden = true;
  $("compose-subject").required = false;
  $("compose-body").required = replying;
  $("compose-heading").textContent = mode === "reply-all" ? "回复全部收件人" : replying ? "回复邮件" : "转发邮件";
  $("compose-description").textContent = `原主题：${message.subject || "（无主题）"}`;
  $("send-label").textContent = replying ? "发送回复" : "转发邮件";
  $("compose-context").textContent = `${replying ? "回复将保留原邮件线程" : "转发将包含原邮件及其附件"} · 原发件人：${senderText(message.from)} · ${formatDate(message.created_at)}`;
  $("compose-context").hidden = false;
  navigate("compose");
  $("compose-body").focus();
}

function renderAttachments() {
  $("compose-attachments").replaceChildren();
  for (const attachment of state.attachments) {
    const chip = element("span", "attachment-chip");
    const remove = actionButton("", "close", "button button-icon button-text");
    remove.setAttribute("aria-label", `移除附件 ${attachment.filename}`);
    remove.disabled = state.sending || state.uploading;
    remove.addEventListener("click", () => {
      state.attachments = state.attachments.filter((item) => item !== attachment);
      renderAttachments();
    });
    chip.append(element("span", "", attachment.filename), remove);
    $("compose-attachments").append(chip);
  }
}

async function uploadAttachments() {
  const files = [...$("compose-files").files];
  $("compose-files").value = "";
  if (!files.length || state.uploading || state.sending) return;
  if (state.attachments.length + files.length > 50) {
    feedback("最多添加 50 个附件。", true);
    return;
  }
  state.uploading = true;
  $("send-button").disabled = true;
  $("attach-button").disabled = true;
  renderAttachments();
  try {
    for (const file of files) {
      if (file.size > MAX_ATTACHMENT_BYTES) throw new Error(`附件“${file.name}”超过 20 MiB，请移除或缩小后重新选择。`);
      $("attachment-status").textContent = `正在上传：${file.name}`;
      const uploaded = await bridge.upload("ui/attachments/upload", file);
      if (typeof uploaded?.path !== "string" || !uploaded.path) throw new Error("附件上传未返回有效文件路径，请重新选择文件。");
      state.attachments.push({ path: uploaded.path, filename: uploaded.filename || file.name, size: uploaded.size ?? file.size });
    }
  } catch (error) {
    feedback(error.message || "附件上传失败。已成功上传的附件会保留。", true);
  } finally {
    state.uploading = false;
    $("send-button").disabled = false;
    $("attach-button").disabled = false;
    $("attachment-status").textContent = "每个附件最大 20 MiB";
    renderAttachments();
  }
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.sending || state.uploading || !$("compose-form").reportValidity()) return;
  const mode = state.composeMode;
  const endpoint = mode === "send" ? "send" : mode === "forward" ? "forward" : "reply";
  const payload = { body: $("compose-body").value, attachments: state.attachments.map((item) => item.path) };
  if (mode === "send" || mode === "forward") {
    payload.to = parseRecipients($("compose-to").value);
    if (!payload.to.length) { feedback("请填写至少一个收件人。", true); return; }
  }
  if (mode === "send") payload.subject = $("compose-subject").value;
  else payload.message_id = state.replyId;
  if (endpoint === "reply") payload.reply_all = mode === "reply-all";
  state.sending = true;
  $("send-button").disabled = true;
  $("attach-button").disabled = true;
  $("discard-draft").disabled = true;
  for (const input of $("compose-form").querySelectorAll("input, textarea")) input.disabled = true;
  $("send-label").textContent = "正在发送…";
  renderAttachments();
  try {
    const response = await bridge.apiPost(`ui/${endpoint}`, payload);
    if (!response?.result || typeof response.result !== "object") throw new Error("邮件服务未返回有效发送结果，请先检查已发送列表后再重试。");
    if (response?.result?.ok === false) throw new Error(response.result.error?.message || "邮件未发送成功。");
    resetCompose();
    feedback("邮件已发送，可在“已发送”中查看。");
    navigate("sent");
  } catch (error) {
    feedback(`发送失败：${error.message || "请稍后重试。"} 草稿与附件已保留。`, true);
    $("send-label").textContent = endpoint === "reply" ? "发送回复" : endpoint === "forward" ? "转发邮件" : "发送邮件";
  } finally {
    state.sending = false;
    $("send-button").disabled = false;
    $("attach-button").disabled = false;
    $("discard-draft").disabled = false;
    for (const input of $("compose-form").querySelectorAll("input, textarea")) input.disabled = false;
    renderAttachments();
  }
}

async function loadStatus() {
  const status = await bridge.apiGet("ui/status");
  state.settings = status.settings || state.settings;
  $("actor-key").textContent = status.actor || "";
  $("watch-status").textContent = status.watching ? "正在监听" : status.watch_error ? "监听已暂停" : "未启用";
  $("watch-error").textContent = status.watch_error || "";
  $("watch-error").hidden = !status.watch_error;
  renderSettings();
  renderLogin(status.login || state.login);
  return status;
}

async function checkAccount(quiet = false) {
  try {
    const result = await bridge.apiGet("ui/account");
    state.account = result.account || null;
    renderAccount();
    if (!quiet) feedback("邮箱登录状态已更新。");
    return Boolean(state.account);
  } catch (error) {
    state.account = null;
    renderAccount();
    if (!quiet) feedback(`无法读取邮箱：${error.message || "请先完成登录。"}`, true);
    return false;
  }
}

function renderSettings() {
  $("permissions-list").replaceChildren();
  for (const [capability, label] of Object.entries(CAPABILITIES)) {
    const group = element("div", "permission-group");
    group.append(element("h4", "", `${label} · ${capability}`));
    const rule = state.settings.access?.[capability] || { allow: [], deny: [] };
    let count = 0;
    for (const mode of ["allow", "deny"]) {
      for (const principal of rule[mode] || []) {
        count += 1;
        const row = element("div", "permission-rule");
        const restore = actionButton("", "close", "button button-text button-icon");
        restore.title = "移除此规则，恢复默认权限";
        restore.setAttribute("aria-label", `恢复 ${principal} 的 ${capability} 默认权限`);
        restore.addEventListener("click", () => action(restore, async () => {
          const result = await bridge.apiPost("ui/permissions", { capability, principal, mode: "revoke" });
          state.settings = result.settings;
          renderSettings();
          feedback("规则已移除，已恢复默认权限。");
        }));
        row.append(element("i", `rule-badge${mode === "deny" ? " deny" : ""}`, mode === "allow" ? "允许" : "拒绝"), element("span", "", principal), restore);
        group.append(row);
      }
    }
    if (!count) group.append(element("p", "no-rules", "无显式规则\n仅管理员默认可用"));
    $("permissions-list").append(group);
  }
  const targets = state.settings.notification_targets || [];
  $("notification-count").textContent = `${targets.length} 个会话`;
  $("notification-list").replaceChildren();
  if (!targets.length) $("notification-list").append(element("p", "no-rules", "尚未绑定通知会话。添加目标后开始监听新邮件。"));
  for (const target of targets) {
    const row = element("div", "notification-row");
    const info = element("div");
    info.append(element("strong", "", target), element("span", "", `设置者：${state.settings.notification_owners?.[target]?.key || "—"}`));
    const disable = actionButton("关闭通知", "", "button button-text danger-text");
    disable.addEventListener("click", () => action(disable, async () => {
      const result = await bridge.apiPost("ui/notifications", { target, enabled: false });
      state.settings = result.settings;
      renderSettings();
      feedback("已关闭该会话的新邮件通知。");
      await loadStatus();
    }));
    row.append(icon("bell"), info, disable);
    $("notification-list").append(row);
  }
}

function renderLogin(login) {
  state.login = login;
  const pending = login.status === "pending";
  $("login-progress").hidden = !pending;
  $("login-progress-label").textContent = login.url ? "等待微信扫码授权，完成后会自动更新。" : "正在生成授权链接…";
  $("login-start").disabled = pending;
  $("logout-button").disabled = pending;
  $("login-cancel").hidden = !pending;
  $("login-badge").textContent = { idle: "等待检查", pending: "授权中", authenticated: "已授权", failed: "授权失败", cancelled: "已取消", logged_out: "已退出" }[login.status] || "等待检查";
  $("login-badge").classList.toggle("success", login.status === "authenticated");
  const url = typeof login.url === "string" ? login.url : "";
  $("login-link-box").hidden = !pending || !url;
  $("login-url").value = url;
  if (/^https:\/\//i.test(url)) $("login-link").href = url;
  else $("login-link").removeAttribute("href");
  $("login-error").textContent = login.error || "";
  $("login-error").hidden = !login.error;
  if (pending) scheduleLoginPoll();
  else {
    window.clearTimeout(state.loginTimer);
    state.loginTimer = null;
  }
}

function scheduleLoginPoll() {
  window.clearTimeout(state.loginTimer);
  state.loginTimer = window.setTimeout(async () => {
    try {
      const result = await bridge.apiGet("ui/login");
      const wasPending = state.login.status === "pending";
      renderLogin(result.login);
      if (wasPending && result.login.status === "authenticated") {
        await checkAccount(true);
        await loadStatus();
        feedback("Agent Mail 授权成功，可以开始收发邮件了。");
      }
    } catch (error) {
      feedback(`授权状态更新失败：${error.message || "请点击刷新重试。"}`, true);
    }
  }, 2000);
}

function bindEvents() {
  document.querySelectorAll("[data-section]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.section === "compose" && state.composeMode !== "send" && !state.sending && !state.uploading) resetCompose();
    navigate(button.dataset.section);
  }));
  $("feedback-dismiss").addEventListener("click", () => { $("feedback").hidden = true; });
  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    state.query = $("search-query").value.trim();
    void loadMessages();
  });
  $("search-query").placeholder = "搜索当前文件夹的主题或正文…";
  $("load-more-button").addEventListener("click", () => { void loadMessages(true); });
  $("refresh-button").addEventListener("click", () => action($("refresh-button"), async () => {
    await loadStatus();
    await checkAccount(true);
    if (state.section === "inbox" || state.section === "sent") await loadMessages();
  }));
  $("compose-form").addEventListener("submit", sendMessage);
  $("discard-draft").addEventListener("click", () => { if (!state.sending && !state.uploading) resetCompose(); });
  $("attach-button").addEventListener("click", () => $("compose-files").click());
  $("compose-files").addEventListener("change", uploadAttachments);
  $("login-start").addEventListener("click", () => action($("login-start"), async () => {
    const result = await bridge.apiPost("ui/login/start", {});
    renderLogin(result.login);
  }));
  $("login-cancel").addEventListener("click", () => action($("login-cancel"), async () => {
    const result = await bridge.apiPost("ui/login/cancel", {});
    renderLogin(result.login);
    feedback("已取消本次授权。");
  }));
  $("select-login-link").addEventListener("click", () => { $("login-url").focus(); $("login-url").select(); });
  $("account-check").addEventListener("click", () => action($("account-check"), async () => { await checkAccount(); await loadStatus(); }));
  $("logout-button").addEventListener("click", () => {
    $("logout-dialog").returnValue = "cancel";
    $("logout-dialog").showModal();
  });
  $("logout-dialog").addEventListener("close", () => {
    if ($("logout-dialog").returnValue !== "logout") return;
    void action($("logout-button"), async () => {
      const result = await bridge.apiPost("ui/logout", {});
      state.account = null;
      renderAccount();
      renderLogin(result.login);
      await loadStatus();
      feedback("共享邮箱已退出登录。");
    });
  });
  $("permission-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const submit = $("permission-form").querySelector('button[type="submit"]');
    void action(submit, async () => {
      const result = await bridge.apiPost("ui/permissions", { capability: $("permission-capability").value, principal: $("permission-principal").value.trim(), mode: $("permission-mode").value });
      state.settings = result.settings;
      renderSettings();
      feedback("访问规则已保存。");
    });
  });
  $("notification-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const submit = $("notification-form").querySelector('button[type="submit"]');
    void action(submit, async () => {
      const result = await bridge.apiPost("ui/notifications", { target: $("notification-target").value.trim(), enabled: true });
      state.settings = result.settings;
      $("notification-target").value = "";
      renderSettings();
      await loadStatus();
      feedback("通知会话已绑定，将按当前权限投递新邮件。");
    });
  });
  window.addEventListener("beforeunload", () => window.clearTimeout(state.loginTimer));
}

async function initialize() {
  bridge = window.AstrBotPluginView;
  if (!bridge) {
    $("connection-error").hidden = false;
    $("mailbox-section").hidden = true;
    $("account-address").textContent = "未连接 AstrBot";
    $("sidebar-status").textContent = "请从插件详情打开";
    $("refresh-button").disabled = true;
    return;
  }
  bindEvents();
  try {
    const context = await bridge.ready();
    if (context?.isDark !== undefined) document.documentElement.dataset.theme = context.isDark ? "dark" : "light";
    if (typeof bridge.onContext === "function") bridge.onContext((context) => { document.documentElement.dataset.theme = context.isDark ? "dark" : "light"; });
    await loadStatus();
    if (await checkAccount(true)) await loadMessages();
    else {
      $("mailbox-summary").textContent = "请先检查邮箱登录";
      const empty = emptyState("连接你的 Agent Mail", "登录共享邮箱后，就能在这里收信、阅读和回复邮件。", true, "lock");
      const login = actionButton("前往登录与管理", "", "button button-tonal");
      login.addEventListener("click", () => navigate("settings"));
      empty.append(login);
      $("message-list").replaceChildren(empty);
      $("message-list").setAttribute("aria-busy", "false");
    }
  } catch (error) {
    feedback(error.message || "无法连接插件管理接口。", true);
    $("mailbox-summary").textContent = "工作台连接失败";
    $("message-list").replaceChildren(emptyState("连接失败", "请确认插件已启用，刷新后重试。", true));
  }
}

if (typeof document !== "undefined") void initialize();
