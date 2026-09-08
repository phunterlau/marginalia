import asyncio
import json

import pytest

from research_arms import Unavailable
from research_arms.discord_io import assemble_question, answer_payload, attachment_url, DiscordSender, read_answer_message
from test_registry import setup, shared, turn


def test_answer_file_uses_exact_bytes_not_delivery_wrapper():
    data = ("Research answer " * 10000).encode()
    async def chunks(url):
        yield data[:80000]
        yield data[80000:]
    message = {"content": "WRAPPER_NOT_THE_ANSWER", "attachments": [
        {"filename": "answer.md", "size": len(data), "url": "https://cdn.discordapp.com/attachments/1/2/answer.md"}]}
    assert asyncio.run(read_answer_message(message, chunks)) == data.decode()


@pytest.mark.parametrize("data,size,name", [(b"x" * 200001, 200000, "answer.md"), (b"x", 2, "answer.md"),
    (b"\xff", 1, "answer.md"), (b"\x00", 1, "answer.md"), (b"x", 1, "other.md")])
def test_invalid_answer_files_fail_closed(data, size, name):
    async def chunks(url): yield data
    message = {"attachments": [{"filename": name, "size": size, "url": "https://cdn.discordapp.com/attachments/1/2/answer.md"}]}
    with pytest.raises(ValueError): asyncio.run(read_answer_message(message, chunks))


def attachment(**changes):
    return {"filename": "question.md", "size": 3,
            "url": "https://cdn.discordapp.com/attachments/1/2/question.md", **changes}


def test_question_utf8_and_actual_combined_bounds():
    async def run():
        async def chunks(url):
            yield "数学".encode()[:2]
            yield "数学".encode()[2:]
        assert await assemble_question("Question", [attachment()], chunks) == "Question\n\n数学"
        with pytest.raises(ValueError): await assemble_question("x" * 20000, [attachment()], chunks)
        async def oversized(url): yield b"x" * 80001
        with pytest.raises(ValueError): await assemble_question("", [attachment()], oversized)
        async def invalid(url): yield b"\xff"
        with pytest.raises(ValueError): await assemble_question("", [attachment()], invalid)
    asyncio.run(run())


@pytest.mark.parametrize("url", ["http://cdn.discordapp.com/attachments/1/2/q.md",
    "https://localhost/attachments/1/2/q.md", "https://cdn.discordapp.com.evil/attachments/1/2/q.md",
    "https://x@cdn.discordapp.com/attachments/1/2/q.md", "https://cdn.discordapp.com/other"])
def test_only_discord_attachment_urls(url):
    with pytest.raises(ValueError): attachment_url(attachment(url=url))


def test_answer_mentions_and_unicode_attachment():
    payload, data = answer_payload("@everyone <@1> <@&2>", "123")
    assert payload["allowed_mentions"]["parse"] == []
    assert payload["allowed_mentions"]["replied_user"] is False
    assert payload["enforce_nonce"] is True and data is None
    payload, data = answer_payload("😀" * 1000, "123")
    assert data.decode() == "😀" * 1000
    assert payload["attachments"][0]["filename"] == "answer.md"


@pytest.mark.parametrize("outcome", ["ok", "timeout", "wrong_author", "denied", "revoked"])
def test_delivery_one_shot_scope_check_and_uncertainty(setup, outcome):
    arms, spaces = setup
    ident = turn(arms, shared(arms))
    arms.claim()
    arms.save_answer(ident, "@everyone " + "answer" * 500, "entry")
    calls = []
    class Client:
        async def post(self, route, **kwargs):
            calls.append((route, kwargs))
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT state FROM outbox").fetchone()[0] == "SENDING"
            if outcome == "timeout": raise asyncio.TimeoutError()
            payload = json.loads(kwargs["data"]["payload_json"])
            class Response:
                status_code = 200
                def json(self): return {"channel_id": "21", "id": "333", "nonce": payload["nonce"],
                    "author": {"id": "999" if outcome == "wrong_author" else "123"}}
            return Response()
    async def authorize(guild, channel):
        assert (guild, channel) == ("10", "21")
        if outcome == "revoked": spaces.set_membership("project", "bob", None)
        return outcome != "denied"
    async def run():
        sender = DiscordSender(arms, Client(), "123", authorize)
        if outcome == "ok":
            assert await sender.deliver(ident) == "333"
            assert await sender.deliver(ident) is None
        else:
            with pytest.raises((Unavailable, asyncio.TimeoutError)): await sender.deliver(ident)
            if outcome != "revoked": assert await sender.deliver(ident) is None
        assert len(calls) == (0 if outcome in {"denied", "revoked"} else 1)
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT state FROM outbox").fetchone()[0] == ("DELIVERED" if outcome == "ok" else "UNKNOWN")
    asyncio.run(run())


def test_uncertain_delivery_reconciliation_reads_without_resending(setup):
    arms, _ = setup
    ident = turn(arms, shared(arms))
    arms.claim()
    arms.save_answer(ident, "answer", "entry")
    delivery = arms.begin_delivery(ident)
    arms.delivery_unknown(delivery["delivery_id"])
    class Client:
        async def get(self, route):
            assert route == "channels/21/messages/333"
            class Response:
                status_code = 200
                def json(self): return {"id": "333", "channel_id": "21", "author": {"id": "123"}, "nonce": delivery["nonce"]}
            return Response()
    async def authorize(*args): return True
    async def run():
        sender = DiscordSender(arms, Client(), "123", authorize)
        assert await sender.reconcile(delivery["delivery_id"], "333") == "333"
        assert await sender.deliver(ident) is None
    asyncio.run(run())
