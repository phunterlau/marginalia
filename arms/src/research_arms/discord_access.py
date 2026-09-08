"""Fresh Discord permission checks, independent from Brain membership checks."""
from datetime import datetime, timezone

from .registry import Unavailable, snowflake

VIEW = 1 << 10
SEND = 1 << 11
ATTACH = 1 << 15
HISTORY = 1 << 16
ADMIN = 1 << 3
MANAGE_THREADS = 1 << 34
THREAD_SEND = 1 << 38
ALL = (1 << 64) - 1


def channel_permissions(guild_id, owner_id, member, roles, overwrites):
    """Discord's everyone -> role aggregate -> member overwrite order."""
    user_id = member["user"]["id"]
    if user_id == owner_id:
        return ALL
    by_id = {role["id"]: int(role["permissions"]) for role in roles}
    assigned = set(member["roles"])
    if guild_id not in by_id or assigned - by_id.keys():
        raise Unavailable()
    bits = by_id[guild_id]
    for role in assigned:
        bits |= by_id[role]
    if bits & ADMIN:
        return ALL
    everyone = [o for o in overwrites if o["type"] == 0 and o["id"] == guild_id]
    matching_roles = [o for o in overwrites if o["type"] == 0 and o["id"] in assigned and o["id"] != guild_id]
    matching_member = [o for o in overwrites if o["type"] == 1 and o["id"] == user_id]
    for group in (everyone, matching_roles, matching_member):
        allow = deny = 0
        for overwrite in group:
            allow |= int(overwrite["allow"])
            deny |= int(overwrite["deny"])
        bits = (bits & ~deny) | allow
    until = member.get("communication_disabled_until")
    if until and datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.now(timezone.utc):
        bits &= VIEW | HISTORY
    return bits


class DiscordAccess:
    """No permission cache. REST client must be authenticated as the bot.

    Checks channel identity, configured project, personal ownership, fresh roles
    and members, and inherited thread permissions. A thread name/private label
    is never used to choose a Brain space.
    """
    def __init__(self, registry, client, bot_user_id):
        self.registry, self.client = registry, client
        self.bot_user_id = snowflake(bot_user_id)

    async def _get(self, route):
        response = await self.client.get(route)
        if response.status_code != 200 or len(response.content) > 262144:
            raise Unavailable()
        return response.json()

    async def authorize(self, actor, *, channel_id, guild_id=None, expected_space=None):
        snowflake(actor), snowflake(channel_id)
        if guild_id is not None: snowflake(guild_id)
        try:
            with self.registry.connect(readonly=True) as db:
                principal = dict(self.registry._principal(db, actor))
            channel = await self._get(f"channels/{channel_id}")
            if channel["id"] != channel_id or channel.get("guild_id") != guild_id:
                raise Unavailable()
            parent_id = None
            if guild_id is None:
                if channel["type"] != 1 or [u["id"] for u in channel.get("recipients", [])] != [actor]:
                    raise Unavailable()
                space_id = principal["personal_space"]
            else:
                if channel["type"] in (10, 11, 12):
                    parent_id = snowflake(channel["parent_id"])
                    parent = await self._get(f"channels/{parent_id}")
                    if parent["id"] != parent_id or parent.get("guild_id") != guild_id or parent["type"] not in (0, 5, 15, 16):
                        raise Unavailable()
                    if channel.get("thread_metadata", {}).get("archived", True) or channel.get("thread_metadata", {}).get("locked", True):
                        raise Unavailable()
                elif channel["type"] in (0, 5):
                    parent = channel
                else:
                    raise Unavailable()
                with self.registry.connect(readonly=True) as db:
                    binding = db.execute("SELECT space_id FROM channels WHERE guild_id=? AND channel_id=?", (guild_id, parent["id"])).fetchone()
                    if binding is None: raise Unavailable()
                    space_id = binding[0]
                guild = await self._get(f"guilds/{guild_id}")
                if guild["id"] != guild_id: raise Unavailable()
                roles = await self._get(f"guilds/{guild_id}/roles")
                for user in (actor, self.bot_user_id):
                    member = await self._get(f"guilds/{guild_id}/members/{user}")
                    if member["user"]["id"] != user: raise Unavailable()
                    permissions = channel_permissions(guild_id, guild["owner_id"], member, roles, parent["permission_overwrites"])
                    required = VIEW | HISTORY | (THREAD_SEND if parent_id else SEND)
                    if user == self.bot_user_id: required |= ATTACH
                    if permissions & required != required: raise Unavailable()
                    if channel["type"] == 12 and not permissions & MANAGE_THREADS:
                        membership = await self._get(f"channels/{channel_id}/thread-members/{user}")
                        if membership.get("user_id") != user or membership.get("id") != channel_id:
                            raise Unavailable()
            if expected_space is not None and expected_space != space_id:
                raise Unavailable()
            scope = self.registry.spaces.scope(principal["principal"], conversation_id="discord-access-check", writable_space=space_id)
            return {"actor": actor, "channel_id": channel_id, "guild_id": guild_id,
                    "parent_channel_id": parent_id, "space_id": space_id, "audience": scope.audience}
        except Exception:
            # No raw response text, membership metadata or private names escape.
            raise Unavailable() from None

    async def authorize_turn(self, turn_id):
        with self.registry.connect(readonly=True) as db:
            turn, scope = self.registry._turn_scope(db, turn_id)
            actor = db.execute("SELECT discord_user FROM principals WHERE principal=?", (turn["author"],)).fetchone()
            conv = db.execute("SELECT guild_id,channel_id FROM conversations WHERE id=?", (turn["conversation_id"],)).fetchone()
            if actor is None: raise Unavailable()
        return await self.authorize(actor[0], channel_id=conv["channel_id"], guild_id=conv["guild_id"], expected_space=scope.writable_space)
