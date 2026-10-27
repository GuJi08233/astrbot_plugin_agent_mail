import test from "node:test";
import assert from "node:assert/strict";
import { parseRecipients, senderText, messageBody, formatDate, formatSize } from "../views/mail/app.js";

test("recipient entry accepts common separators without silently duplicating recipients", () => {
  assert.deepEqual(parseRecipients("alice@example.com；bob@example.com, alice@example.com\ncarol@example.com"), ["alice@example.com", "bob@example.com", "carol@example.com"]);
  assert.deepEqual(parseRecipients(" ; ， \n"), []);
});

test("mail body uses backend safe text and never interprets raw HTML as DOM", () => {
  assert.equal(messageBody({ body_text: "合同正文", body: '<img src="https://example.com/track" onerror="send()">' }), "合同正文");
  assert.equal(messageBody({ body: '<script>alert("x")</script>' }), '<script>alert("x")</script>');
  assert.equal(messageBody({ body_text: "", body: "Do not fall back to external HTML" }), "");
  assert.equal(messageBody(null), "");
});

test("sender labels preserve external names as plain strings and handle address-only mail", () => {
  assert.equal(senderText({ name: "<script>external</script>", email: "sender@example.com" }), "<script>external</script>");
  assert.equal(senderText({ email: "sender@example.com" }), "sender@example.com");
  assert.equal(senderText(null), "未知发件人");
});

test("unusual server dates and attachment sizes do not break mail rendering", () => {
  assert.equal(formatDate("invalid server date"), "invalid server date");
  assert.equal(formatDate(null), "");
  assert.match(formatDate("2026-10-27T10:00:00Z"), /2026/);
  assert.equal(formatSize("1048576"), "1.0 MiB");
  assert.equal(formatSize(-1), "");
  assert.equal(formatSize("invalid"), "");
});
