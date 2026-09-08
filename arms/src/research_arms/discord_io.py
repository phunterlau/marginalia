"""Bounded Discord IO primitives; Gateway authentication is supplied by the caller.

No default transport is started, and no model calls occur in this module.
"""
import json
import asyncio
import hashlib
import os
import re
from urllib.parse import urlsplit

from .registry import Unavailable, snowflake


MAX_QUESTION = 20_000
MAX_ATTACHMENT_BYTES = 80_000


def attachment_url(attachment, *, max_bytes=MAX_ATTACHMENT_BYTES):
    name, url, size = attachment["filename"], attachment["url"], attachment["size"]
    if not isinstance(name, str) or len(name) > 255 or not name.lower().endswith((".txt", ".md")):
        raise ValueError("Only UTF-8 .txt and .md questions are supported")
    if type(size) is not int or not 0 <= size <= max_bytes:
        raise ValueError("Attachment exceeds byte limit")
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("Invalid attachment URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not re.fullmatch(r"/attachments/[0-9]{1,20}/[0-9]{1,20}/[^/]+", parsed.path)):
        raise ValueError("Only Discord attachment URLs are permitted")
    return url


async def assemble_question(text, attachments, chunks):
    """Call only after event authorization. chunks(url) yields untrusted bytes."""
    if not isinstance(text, str) or len(text) > MAX_QUESTION or len(attachments) > 4:
        raise ValueError("Question exceeds limit")
    urls = [attachment_url(item) for item in attachments]
    if sum(item["size"] for item in attachments) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Combined attachments exceed byte limit")
    parts, consumed = [text] if text else [], 0
    for url in urls:
        data = bytearray()
        async for chunk in chunks(url):
            consumed += len(chunk)
            if consumed > MAX_ATTACHMENT_BYTES:
                raise ValueError("Downloaded attachments exceed byte limit")
            data.extend(chunk)
        try:
            parts.append(data.decode("utf-8", errors="strict"))
        except UnicodeDecodeError:
            raise ValueError("Attachments must be valid UTF-8") from None
        if len("\n\n".join(parts)) > MAX_QUESTION:
            raise ValueError("Combined question exceeds 20000 characters")
    result = "\n\n".join(parts)
    if not result.strip() or "\x00" in result:
        raise ValueError("Question is empty or contains NUL characters")
    return result


async def download_chunks(url):
    """Separate no-auth CDN client: bot credentials never accompany downloads."""
    import httpx
    attachment_url({"filename": "question.md", "url": url, "size": 0})
    async with asyncio.timeout(30), httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError("Attachment download unavailable")
            async for chunk in response.aiter_bytes(8192):
                yield chunk


async def read_answer_message(message, chunks=download_chunks):
    """After bot-author verification, read our bounded answer.md, not its wrapper."""
    attachments = message.get("attachments", [])
    if not isinstance(attachments, list): raise ValueError("Invalid answer attachments")
    if attachments:
        if len(attachments) != 1 or not isinstance(attachments[0], dict) or attachments[0].get("filename") != "answer.md":
            raise ValueError("Expected exactly one answer.md attachment")
        item = attachments[0]
        url = attachment_url(item, max_bytes=200000)
        data = bytearray()
        async for chunk in chunks(url):
            data.extend(chunk)
            if len(data) > 200000: raise ValueError("Answer exceeds byte bound")
        if len(data) != item["size"]: raise ValueError("Answer attachment length mismatch")
        try: content = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError: raise ValueError("Answer attachment must be UTF-8") from None
    else:
        content = message.get("content")
    if not isinstance(content, str) or not content.strip() or "\x00" in content or len(content.encode()) > 200000:
        raise ValueError("Invalid answer content")
    return content


def answer_payload(answer, nonce):
    if not isinstance(answer, str) or not answer.strip() or len(answer.encode()) > 200_000:
        raise ValueError("Invalid answer size")
    snowflake(nonce)
    payload = {"allowed_mentions": {"parse": [], "users": [], "roles": [], "replied_user": False},
               "flags": 4 | 4096, "nonce": nonce, "enforce_nonce": True}
    # UTF-16 units are a conservative bound for Discord message length.
    if len(answer.encode("utf-16-le")) // 2 <= 1800:
        payload["content"] = answer
        return payload, None
    payload["content"] = "Research answer attached as Markdown. Review source and review-state qualifications before relying on it."
    payload["attachments"] = [{"id": 0, "filename": "answer.md"}]
    return payload, answer.encode("utf-8")


class DiscordSender:
    """One-shot REST sender. Caller must supply a current audience permission check.

    Failed/ambiguous sends require remote reconciliation, not automatic retries.
    The check callback must verify Discord guild/channel/thread/DM identity and
    permissions, not merely repeat local registry checks.
    """
    def __init__(self, registry, client, bot_user_id, authorize_destination):
        self.registry, self.client = registry, client
        self.bot_user_id = snowflake(bot_user_id)
        self.authorize_destination = authorize_destination

    @staticmethod
    def environment_client():
        import httpx
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is required in the backend environment")
        return httpx.AsyncClient(base_url="https://discord.com/api/v10/", trust_env=False,
            follow_redirects=False, timeout=30, headers={"Authorization": "Bot " + token})

    async def deliver(self, turn_id):
        delivery = self.registry.begin_delivery(turn_id)
        if delivery is None:
            return None
        ident = delivery["delivery_id"]
        try:
            if not await self.authorize_destination(delivery["guild_id"], delivery["channel_id"]):
                raise Unavailable()
            self.registry.validate_delivery(ident)
            payload, attachment = answer_payload(delivery["answer"], delivery["nonce"])
            if attachment is None:
                response = await self.client.post(f"channels/{delivery['channel_id']}/messages", json=payload)
            else:
                response = await self.client.post(f"channels/{delivery['channel_id']}/messages",
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": ("answer.md", attachment, "text/markdown; charset=utf-8")})
            if response.status_code != 200:
                raise Unavailable()
            message = response.json()
            if (message.get("channel_id") != delivery["channel_id"]
                    or message.get("author", {}).get("id") != self.bot_user_id
                    or str(message.get("nonce")) != delivery["nonce"]):
                raise Unavailable()
            message_id = snowflake(message.get("id"))
            self.registry.confirm_delivery(ident, message_id)
            return message_id
        except BaseException:
            self.registry.delivery_unknown(ident)
            raise

    async def reconcile(self, delivery_id, message_id):
        """Read one exact remote message; absence never authorizes a resend."""
        snowflake(message_id)
        with self.registry.connect(readonly=True) as db:
            outbox = db.execute("SELECT * FROM outbox WHERE id=?", (delivery_id,)).fetchone()
            if outbox is None or outbox["state"] not in {"UNKNOWN", "SENDING"}:
                raise Unavailable()
            turn, _ = self.registry._turn_scope(db, outbox["turn_id"])
            destination = db.execute("SELECT guild_id,channel_id FROM conversations WHERE id=?", (turn["conversation_id"],)).fetchone()
        if not await self.authorize_destination(destination["guild_id"], destination["channel_id"]):
            raise Unavailable()
        response = await self.client.get(f"channels/{destination['channel_id']}/messages/{message_id}")
        if response.status_code != 200:
            raise Unavailable()
        message = response.json()
        nonce = str(int(hashlib.sha256(delivery_id.encode()).hexdigest()[:16], 16))
        if (message.get("id") != message_id or message.get("channel_id") != destination["channel_id"]
                or message.get("author", {}).get("id") != self.bot_user_id or str(message.get("nonce")) != nonce):
            raise Unavailable()
        # Remote permission checks can become stale while the GET is in flight.
        # Unlike recording a just-dispatched send, operator reconciliation must
        # still be authorized when it adopts the remote outcome.
        if not await self.authorize_destination(destination["guild_id"], destination["channel_id"]):
            raise Unavailable()
        with self.registry.connect(readonly=True) as db:
            self.registry._turn_scope(db, outbox["turn_id"])
        self.registry.confirm_delivery(delivery_id, message_id, reconciled=True)
        return message_id
