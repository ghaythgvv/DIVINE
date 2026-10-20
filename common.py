"""
common.py - shared settings imported by main.py and the cogs.

Put this file in the repo root, next to main.py.

Railway variables:
  GUILD_ID        (REQUIRED) your server's ID. Discord: Settings > Advanced > Developer Mode,
                  then right-click the server icon > Copy Server ID.
  ADMIN_ROLE_IDS  (optional) comma-separated role IDs that count as "high rank" for is_admin().
                  Members with the Administrator permission always count.
"""
import os

import discord

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- server -------------------------------------------------------------------------------------
_raw_guild_id = (os.getenv("GUILD_ID") or "").strip()
if not _raw_guild_id.isdigit():
    raise SystemExit(
        "GUILD_ID is not set (or is not a number). In Railway open the service > Variables, "
        "add GUILD_ID = your server ID, then redeploy."
    )

GUILD_ID = int(_raw_guild_id)
GUILD = discord.Object(id=GUILD_ID)   # used as: bot.tree.sync(guild=GUILD) / bot.add_cog(..., guild=GUILD)

# ---- storage ------------------------------------------------------------------------------------
# Railway volume is mounted at /data (see README). Falls back to a local folder when running on a PC.
DATA_DIR = "/data" if os.path.isdir("/data") else os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---- banners ------------------------------------------------------------------------------------
BANNER_DIR = os.path.join(HERE, "banners")
BANNERS = {
    "studycard": os.path.join(BANNER_DIR, "studycard.gif"),
    "welcome": os.path.join(BANNER_DIR, "welcome.gif"),
    "verify": os.path.join(BANNER_DIR, "verify.gif"),
    "support": os.path.join(BANNER_DIR, "support.gif"),
    "staffapps": os.path.join(BANNER_DIR, "staffapps.gif"),
    "blacklist": os.path.join(BANNER_DIR, "blacklist_banner.jpg"),
    "xp": os.path.join(BANNER_DIR, "xp.gif"),
    "boost": os.path.join(BANNER_DIR, "boost_bg.jpg"),
    "giveaway": os.path.join(BANNER_DIR, "giveaway.gif"),
    "esport": os.path.join(BANNER_DIR, "esport.gif"),
    "tickets": os.path.join(BANNER_DIR, "tickets.gif"),
    "tempvc": os.path.join(BANNER_DIR, "tempvc.gif"),
}

# ---- permissions --------------------------------------------------------------------------------
ADMIN_ROLE_IDS = {int(x) for x in (os.getenv("ADMIN_ROLE_IDS") or "").replace(" ", "").split(",") if x.isdigit()}


def is_admin(member) -> bool:
    """High rank = Administrator permission, or one of ADMIN_ROLE_IDS."""
    if member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    return any(r.id in ADMIN_ROLE_IDS for r in getattr(member, "roles", []))
