"""Destination-aware Discord publication controls; private previews stay in DMs."""
import asyncio
import re

from .publication import Publications
from .registry import Unavailable, snowflake


def selection(text, prefix):
    if not isinstance(text, str) or len(text) > 1200: raise ValueError("Selection exceeds bound")
    values = text.split()
    if len(values) > 10 or any(not re.fullmatch(prefix + r"_[A-Za-z0-9_-]{1,90}", value) for value in values):
        raise ValueError("Use at most ten whitespace-separated exact IDs")
    return values


async def handle(client, command, actor, channel, guild, message_id, destination, **options):
    registry = client.registry
    service = Publications(registry)
    if client.publication_stopping: raise Unavailable()
    if command == "publish_prepare":
        if guild is not None: raise Unavailable()
        target = snowflake(options["destination_channel_id"])
        with registry.connect(readonly=True) as db:
            binding = db.execute("SELECT * FROM channels WHERE channel_id=?", (target,)).fetchone()
        if binding is None: raise Unavailable()
        await client.access.authorize(actor, channel_id=target, guild_id=binding["guild_id"], expected_space=binding["space_id"])
        return await asyncio.to_thread(service.prepare, actor, destination["space_id"], binding["space_id"],
            note_ids=selection(options.get("note_ids", ""), "obj"),
            paper_block_ids=selection(options.get("paper_block_ids", ""), "block"), request_id=message_id)

    ident = options["publication_id"]
    if not isinstance(ident, str) or not re.fullmatch(r"publication_[a-f0-9]{32}", ident): raise Unavailable()
    with registry.connect(readonly=True) as db:
        row = db.execute("SELECT * FROM publications WHERE id=?", (ident,)).fetchone()
        if row is None: raise Unavailable()
        row = dict(row)
        principal = registry._principal(db, actor)["principal"]
    if guild is None:
        if principal != row["owner"] or row["source_space"] != destination["space_id"]: raise Unavailable()
        if command not in {"publish_show", "publish_consent", "publish_cancel"}: raise Unavailable()
    else:
        if row["destination_space"] != destination["space_id"] or row["state"] not in {"CONSENTED", "APPROVED", "RUNNING", "NEEDS_ATTENTION", "COMPLETE"}:
            raise Unavailable()
        if command not in {"publish_show", "publish_approve", "publish_run"}: raise Unavailable()
    # The service checks owner/maintainer role and policy, independently of Discord.
    if command == "publish_show": return await asyncio.to_thread(service.show, actor, ident)
    digest = options["digest"]
    if options.get("confirm") is not True: raise ValueError("Explicit publication confirmation required")
    if command != "publish_run":
        return await asyncio.to_thread(service.decide, actor, ident, digest, command.removeprefix("publish_"))
    if client.publication_tasks: raise ValueError("Another publication is active; inspect status before retrying")
    loop = asyncio.get_running_loop()

    async def verify():
        if client.publication_stopping: raise Unavailable()
        with registry.connect(readonly=True) as db:
            actors = {actor}
            for principal in (row["owner"], row["approval_actor"]):
                person = db.execute("SELECT discord_user FROM principals WHERE principal=?", (principal,)).fetchone()
                if person is None: raise Unavailable()
                actors.add(person[0])
        for person in sorted(actors):
            await client.access.authorize(person, channel_id=channel, guild_id=guild, expected_space=destination["space_id"])

    def authorize():
        future = asyncio.run_coroutine_threadsafe(verify(), loop)
        try: future.result(timeout=30)
        except BaseException:
            future.cancel()
            raise

    task = asyncio.create_task(asyncio.to_thread(service.execute, actor, ident, digest,
        retry=options.get("retry") is True, authorize=authorize))
    client.publication_tasks.add(task)
    def finished(done):
        client.publication_tasks.discard(done)
        if not done.cancelled(): done.exception()  # Failure is retained in the publication ledger.
    task.add_done_callback(finished)
    # Shield the local writer if Discord disconnects. Shutdown joins it before
    # releasing registry ownership; later calls reconcile through its receipt.
    return await asyncio.shield(task)
