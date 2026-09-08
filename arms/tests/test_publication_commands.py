import asyncio

import pytest
pytest.importorskip("discord")

from research_arms import Unavailable
from research_arms.gateway import ResearchGateway
from test_registry import setup
from test_publication import note


class Access:
    def __init__(self): self.denied = False
    async def authorize(self, actor, **kwargs):
        if self.denied: raise Unavailable()
        return {"space_id": "project" if kwargs.get("guild_id") else "alice"}


def test_dm_preview_shared_consent_gate_and_explicit_execution(setup):
    arms, _ = setup
    ident = note(arms)
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        client.access = Access()
        dm, shared = {"space_id": "alice"}, {"space_id": "project"}
        try:
            preview = await client.handle("publish_prepare", "1", "30", None, "100", dm,
                destination_channel_id="20", note_ids=ident)
            again = await client.handle("publish_prepare", "1", "30", None, "100", dm,
                destination_channel_id="20", note_ids=ident)
            assert preview == again
            opts = {"publication_id": preview["publication_id"], "digest": preview["digest"], "confirm": True}
            # Even the private owner cannot show an unconsented preview in a shared channel.
            with pytest.raises(Unavailable): await client.handle("publish_show", "1", "20", "10", "101", shared, **opts)
            with pytest.raises(Unavailable): await client.handle("publish_consent", "1", "20", "10", "102", shared, **opts)
            await client.handle("publish_consent", "1", "30", None, "103", dm, **opts)
            shown = await client.handle("publish_show", "1", "20", "10", "104", shared, **opts)
            assert shown["bundle"] == preview["bundle"]
            with pytest.raises(Unavailable): await client.handle("publish_approve", "1", "30", None, "105", dm, **opts)
            await client.handle("publish_approve", "1", "20", "10", "106", shared, **opts)
            with pytest.raises(ValueError): await client.handle("publish_run", "1", "20", "10", "107", shared, **{**opts, "confirm": False})
            result = await client.handle("publish_run", "1", "20", "10", "108", shared, **opts)
            assert result["state"] == "COMPLETE" and result["destination_space"] == "project"
            assert len(result["note_ids"]) == 1
        finally: await client.close()
    asyncio.run(run())


def test_unrelated_dm_and_shared_destination_cannot_inspect_preview(setup):
    arms, _ = setup
    ident = note(arms)
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        client.access = Access()
        try:
            preview = await client.handle("publish_prepare", "1", "30", None, "100", {"space_id": "alice"},
                destination_channel_id="20", note_ids=ident)
            with pytest.raises(Unavailable):
                await client.handle("publish_show", "2", "31", None, "101", {"space_id": "bob"}, publication_id=preview["publication_id"])
            client.access.denied = True
            with pytest.raises(Unavailable):
                await client.handle("publish_prepare", "1", "30", None, "102", {"space_id": "alice"}, destination_channel_id="20", note_ids=ident)
        finally: await client.close()
    asyncio.run(run())


def test_execution_rechecks_source_owners_discord_access(setup):
    from research_arms.publication import Publications
    arms, spaces = setup
    spaces.set_membership("project", "bob", "maintainer")
    service = Publications(arms)
    preview = service.prepare("1", "alice", "project", note_ids=[note(arms)])
    args = ("1", preview["publication_id"], preview["digest"])
    service.decide(*args, "consent")
    service.decide(*args, "approve")
    class OwnerRevoked(Access):
        async def authorize(self, actor, **kwargs):
            if actor == "1": raise Unavailable()
            return await super().authorize(actor, **kwargs)
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        client.access = OwnerRevoked()
        before = spaces.open("project").list_research_objects()
        try:
            with pytest.raises(Unavailable):
                await client.handle("publish_run", "2", "20", "10", "100", {"space_id": "project"},
                    publication_id=args[1], digest=args[2], confirm=True)
            assert spaces.open("project").list_research_objects() == before
        finally: await client.close()
    asyncio.run(run())
