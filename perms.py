def member_capabilities(member):
    caps = {
        "is_owner_of_bot": False,
        "is_guild_owner": False,
        "guild_name": None,
        "guild_id": None,
        "roles": [],
        "highest_role_position": -1,
        "ban_members": False,
        "kick_members": False,
        "manage_roles": False,
        "manage_channels": False,
        "manage_guild": False,
        "manage_messages": False,
        "administrator": False,
        "view_channels": False,
        "mention_everyone": False,
        "requested_by": str(getattr(member, "id", "?")),
        "display": str(getattr(member, "display_name", getattr(member, "name", "?"))),
    }
    try:
        guild = getattr(member, "guild", None)
    except Exception:
        guild = None
    if guild is not None:
        caps["guild_id"] = str(getattr(guild, "id", None))
        caps["guild_name"] = getattr(guild, "name", None)
        caps["is_guild_owner"] = str(getattr(guild, "owner_id", None)) == str(getattr(member, "id", None))
        try:
            perm = member.guild_permissions
        except Exception:
            perm = None
        if perm is None:
            try:
                perm = member.top_role.permissions
            except Exception:
                perm = None
if perm is not None:
            try:
                caps["ban_members"] = bool(perm.ban_members)
                caps["kick_members"] = bool(perm.kick_members)
                caps["manage_roles"] = bool(perm.manage_roles)
                caps["manage_channels"] = bool(perm.manage_channels)
                caps["manage_guild"] = bool(perm.manage_guild)
                caps["manage_messages"] = bool(perm.manage_messages)
                caps["administrator"] = bool(perm.administrator)
                caps["view_channels"] = bool(perm.view_channel)
                caps["mention_everyone"] = bool(perm.mention_everyone)
            except Exception:
                pass
        try:
            caps["roles"] = [str(r.id) for r in member.roles]
            caps["highest_role_position"] = member.top_role.position if member.roles else -1
        except Exception:
            pass
    return caps


def hierarchy_can(requester, target_member):
    try:
        r_id = str(getattr(requester, "id", None))
        t_id = str(getattr(target_member, "id", None))
        if r_id == t_id:
            return False
        r_owner = str(getattr(getattr(requester, "guild", None), "owner_id", None)) == r_id
        t_owner = str(getattr(getattr(target_member, "guild", None), "owner_id", None)) == t_id
        if t_owner:
            return False
        r_pos = requester.top_role.position if getattr(requester, "roles", []) else 0
        t_pos = target_member.top_role.position if getattr(target_member, "roles", []) else 0
        if r_owner:
            return True
        return r_pos > t_pos
    except Exception:
        return False


def describe(caps):
    if not caps:
        return "none (no guild context)"
    bits = []
    bits.append("requester=%s" % caps["display"])
    bits.append("guild=%s" % (caps["guild_name"] or caps["guild_id"]))
    if caps["is_owner_of_bot"]:
        bits.append("is_bot_owner=yes")
    if caps["is_guild_owner"]:
        bits.append("is_guild_owner=yes")
    for key in ("administrator", "manage_guild", "manage_channels", "manage_roles", "ban_members", "kick_members", "manage_messages", "mention_everyone"):
        if caps.get(key):
            bits.append("%s=yes" % key)
    bits.append("highest_role_position=%s" % caps.get("highest_role_position"))
    return ", ".join(bits)