"""
vc_staff_cog.py - ONE live staff leaderboard, ONE slash command (/leaderboard).

What it does:
  - Tracks how long STAFF + MODERATORS spend in the public voice channels.
  - Keeps a single embed in the staff-leaderboard channel and edits it every
    30 seconds (podium, rankings, top this week, team totals).
  - Uses the bot's own profile picture as the embed thumbnail.
  - Temp channels: between 7:00 and 19:00 (BOT_TIMEZONE) a staff/mod who sits in a temp
    VC for 10 minutes is moved to a public VC; if muted/deafened there they are moved
    to the AFK channel instead. Outside those hours they can rest there freely.
  - Anti-farming: if a staff/mod is deafened too long, or alone in a VC too long,
    the bot takes that idle time back and stops counting until they are active
    again. It does NOT move anyone and does NOT send any DM.

Use:
  - Run /leaderboard in the channel where you want the board (needs Manage Server).
    It posts the live board there and moves it if it was somewhere else.

Setup:
  - Enable the Server Members intent (code + Developer Portal).
  - The bot needs View Channel, Send Messages, Embed Links in the leaderboard channel.
  - Optional Railway variable LEADERBOARD_CHANNEL_ID=<channel id>. If you skip it,
    the bot finds the text channel whose name contains "staff-leaderboard".
  - On Railway, attach a Volume and set VC_HOURS_FILE=/data/vc_staff_hours.json
    so hours (and the leaderboard message id) survive redeploys.
"""

import asyncio
import io
import json
import os
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import podium as podium_art   # podium.py + Poppins-Bold.ttf next to this file
except Exception as _e:            # bot still works without the image
    podium_art = None
    print(f"[vc_staff] podium.py not loaded: {_e!r}")

# Staff + Moderator roles - only these members are tracked.
STAFF_ROLE_IDS = {
    1513904127837736992,  # staff
    1513904125086011402,  # moderator
}

# Public voice channels to track (a category ID here also covers every VC inside it).
PUBLIC_VC_IDS = {
    1518264682413166682,
    1513904295316029450,
    1513904296419135638,
    1513904297736147085,
    1513904299057352879,
}

DATA_FILE = os.getenv("VC_HOURS_FILE", "vc_staff_hours.json")
PANEL_FILE = os.path.splitext(DATA_FILE)[0] + "_panel.json"
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "0") or 0)
CHANNEL_NAME_HINT = "staff-leaderboard"

PTS_PER_HOUR = 10
REFRESH_SECONDS = 30
EMBED_COLOR = 0xB066FF   # soft purple

LIVE_DOT = "🟣"   # in a public VC right now
EMOJI_BUTTERFLY = "<a:156218darkpurplesparklybutterfly:1553826933890879518>"  # circle before each rank

# Your custom emojis (the bot must share the server they live in)
EMOJI_STAR = "<a:Elt_emoji_51:1552029199215100004>"
EMOJI_KITTY = "<a:Elt_Hellokitty:1544075596806623392>"
EMOJI_ICON = "<:Elt_emoji_89:1552239138697121802>"

# Optional big banner image at the bottom of the board (direct image/gif link).
BANNER_URL = os.getenv("LEADERBOARD_BANNER_URL", "")

# ---- anti-farming (no moving, no DMs - it only stops counting idle time) ----
AFK_CHANNEL_ID = 1513904300013785241   # still never counted as a public VC
ANTI_FARM = os.getenv("ANTI_FARM", "1") != "0"   # set ANTI_FARM=0 in Railway to switch off
DEAF_MINUTES = 5     # staff/mod deafened this long in a public VC = not counted
ALONE_MINUTES = 20   # staff/mod with no regular members (only staff/bots) this long = not counted
EXEMPT_USER_IDS: set[int] = {
    1552366484016205824,   # never moved / never checked
    392770501229019136,    # never moved / never checked
    1482254360137175072,   # never moved / never checked
}
FARM_LOG_CHANNEL_ID = int(os.getenv("FARM_LOG_CHANNEL_ID", "0") or 0)  # optional log channel


# ---- temp channels (the "JOIN TO CREATE" voice channels) ----
TEMP_CHECK = os.getenv("TEMP_CHECK", "1") != "0"   # TEMP_CHECK=0 in Railway switches it off
TEMP_CATEGORY_ID = int(os.getenv("TEMP_CATEGORY_ID", "0") or 0)  # optional: exact category ID
TEMP_CATEGORY_HINT = "temp channels"    # otherwise: any category whose name contains this
TEMP_STAY_MINUTES = 10    # staff/mod in a temp VC this long -> moved to a public VC
TEMP_MUTED_MINUTES = 5    # staff/mod muted or deafened in a temp VC this long -> moved to AFK
TEMP_RULES_START_HOUR = int(os.getenv("TEMP_RULES_START_HOUR", "7"))    # 7 = 7 AM
TEMP_RULES_END_HOUR = int(os.getenv("TEMP_RULES_END_HOUR", "19"))      # 19 = 7 PM
# Outside those hours staff can rest in temp VCs (muted/deafened) and are never moved.
# Set BOT_TIMEZONE in Railway to your timezone, e.g. Africa/Tunis, Europe/Paris, America/New_York
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "UTC")
try:
    LOCAL_TZ = ZoneInfo(BOT_TIMEZONE)
except Exception:
    print(f"[vc_staff] unknown BOT_TIMEZONE {BOT_TIMEZONE!r} - using UTC")
    LOCAL_TZ = timezone.utc
MOVE_TO_VC_ID = int(os.getenv("PUBLIC_MOVE_VC_ID", "0") or 0)  # optional: always move to this VC


def _norm(text: str) -> str:
    """lowercase + undo fancy unicode fonts (bold/script letters) so name checks still match."""
    return unicodedata.normalize("NFKD", text or "").casefold()


def _is_join_channel(channel) -> bool:
    n = _norm(channel.name)
    return "join" in n and "create" in n


def temp_rules_active() -> bool:
    """True during the work hours (default 7:00 - 19:00 in BOT_TIMEZONE)."""
    h = datetime.now(LOCAL_TZ).hour
    if TEMP_RULES_START_HOUR <= TEMP_RULES_END_HOUR:
        return TEMP_RULES_START_HOUR <= h < TEMP_RULES_END_HOUR
    return h >= TEMP_RULES_START_HOUR or h < TEMP_RULES_END_HOUR   # window that crosses midnight


def fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m"


def pts(seconds: float) -> int:
    return round(seconds / 3600 * PTS_PER_HOUR)


def today_key(offset_days: int = 0) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=offset_days)
    return d.strftime("%Y-%m-%d")


class VCStaff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data: dict[str, dict[str, float]] = self._load(DATA_FILE)
        self.panel: dict = self._load(PANEL_FILE)
        # member_id -> (guild_id, last_tick_timestamp)
        self.active: dict[int, tuple[int, float]] = {}
        self._msg: discord.Message | None = None
        self._warned = False
        self.deaf_since: dict[int, float] = {}
        self.alone_since: dict[int, float] = {}
        self.farming: set[int] = set()   # members whose time is currently NOT being counted
        self.temp_since: dict[int, float] = {}        # staff in a temp VC since
        self.temp_idle_since: dict[int, float] = {}   # ...and muted/deafened since
        self._warned_move = False
        self._podium_png: bytes | None = None
        self._podium_key = None        # what the cached image was drawn for
        self._podium_sent_key = None   # what the live message currently shows
        self.ticker.start()

    # ---------- storage ----------
    @staticmethod
    def _load(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _save(path: str, obj: dict) -> None:
        tmp = path + ".tmp"
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f)
            os.replace(tmp, path)
        except OSError as e:
            print(f"[vc_staff] save failed ({path}): {e}")

    def _add(self, uid: int, seconds: float) -> None:
        if seconds <= 0:
            return
        days = self.data.setdefault(str(uid), {})
        k = today_key()
        days[k] = round(days.get(k, 0) + seconds, 1)

    # ---------- helpers ----------
    @staticmethod
    def is_staff(member: discord.Member) -> bool:
        return (not member.bot) and any(r.id in STAFF_ROLE_IDS for r in member.roles)

    @staticmethod
    def is_public_vc(channel) -> bool:
        """Public = one of PUBLIC_VC_IDS (or inside a category listed there)."""
        if channel is None or channel.id == AFK_CHANNEL_ID:
            return False
        return channel.id in PUBLIC_VC_IDS or getattr(channel, "category_id", None) in PUBLIC_VC_IDS

    @staticmethod
    def is_temp_vc(channel) -> bool:
        """Temp VC = a voice channel in the Temp Channels category (the one holding JOIN TO CREATE)."""
        if channel is None or channel.id == AFK_CHANNEL_ID:
            return False
        if VCStaff.is_public_vc(channel) or _is_join_channel(channel):
            return False
        if TEMP_CATEGORY_ID:
            return getattr(channel, "category_id", None) == TEMP_CATEGORY_ID
        cat = getattr(channel, "category", None)
        if cat is None:
            return False
        if "temp" in _norm(cat.name):
            return True
        # fallback: the category that contains the "JOIN TO CREATE" channel
        return any(_is_join_channel(c) for c in cat.voice_channels)

    def _live_members(self, guild: discord.Guild):
        """Yield (channel, member) for staff/mods currently in public VCs."""
        for ch in list(guild.voice_channels) + list(guild.stage_channels):
            if not self.is_public_vc(ch):
                continue
            for m in ch.members:
                if self.is_staff(m):
                    yield ch, m

    # ---------- lifecycle ----------
    async def cog_unload(self):
        self.ticker.cancel()
        self._flush()
        self._save(DATA_FILE, self.data)

    @commands.Cog.listener()
    async def on_ready(self):
        # pick up people already sitting in public VCs (restart / redeploy)
        now = time.time()
        for guild in self.bot.guilds:
            for _, m in self._live_members(guild):
                self.active.setdefault(m.id, (guild.id, now))
            n_temp = len([c for c in guild.voice_channels if self.is_temp_vc(c)])
            print(f"[vc_staff] watching {n_temp} temp VC(s) in {guild.name} "
                  f"(Move Members: {guild.me.guild_permissions.move_members})")

    def _flush(self) -> None:
        """Credit elapsed time to everyone still active and drop stale sessions."""
        now = time.time()
        for uid in list(self.active):
            guild_id, last = self.active[uid]
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(uid) if guild else None
            still_in = (
                member is not None
                and member.voice is not None
                and self.is_staff(member)
                and self.is_public_vc(member.voice.channel)
            )
            if uid not in self.farming:   # idle/farming time is not credited
                self._add(uid, now - last)
            if still_in:
                self.active[uid] = (guild_id, now)
            else:
                del self.active[uid]
                self.farming.discard(uid)

    @tasks.loop(seconds=REFRESH_SECONDS)
    async def ticker(self):
        self._flush()
        if ANTI_FARM:
            try:
                await self._anti_farm()
            except Exception as e:
                print(f"[vc_staff] anti-farm check failed: {e!r}")
        if TEMP_CHECK:
            try:
                await self._temp_check()
            except Exception as e:
                print(f"[vc_staff] temp-vc check failed: {e!r}")
        self._save(DATA_FILE, self.data)
        try:
            await self._update_panel()
        except discord.Forbidden:
            print("[vc_staff] can't post/edit in the leaderboard channel - give the bot "
                  "View Channel, Send Messages and Embed Links there.")
        except Exception as e:
            print(f"[vc_staff] panel update failed: {e!r}")

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    # ---------- tracking ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not self.is_staff(member):
            return
        if not self.is_temp_vc(after.channel):
            self.temp_since.pop(member.id, None)
            self.temp_idle_since.pop(member.id, None)
        if before.channel != after.channel:
            self.deaf_since.pop(member.id, None)
            self.alone_since.pop(member.id, None)
            self.farming.discard(member.id)
        elif before.self_deaf and not after.self_deaf:
            self.deaf_since.pop(member.id, None)

        was = self.is_public_vc(before.channel)
        now_in = self.is_public_vc(after.channel)

        if now_in and not was:
            self.active[member.id] = (member.guild.id, time.time())
        elif was and not now_in:
            session = self.active.pop(member.id, None)
            if session and member.id not in self.farming:
                self._add(member.id, time.time() - session[1])
            self.farming.discard(member.id)

    # ---------- anti-farming (no move, no DM) ----------
    def _deduct(self, uid: int, seconds: float) -> None:
        """Take idle time back off the member's recent days."""
        days = self.data.get(str(uid), {})
        left = seconds
        for i in range(3):
            k = today_key(i)
            have = days.get(k, 0)
            take = min(have, left)
            if take > 0:
                days[k] = round(have - take, 1)
                left -= take
            if left <= 0:
                break

    async def _anti_farm(self) -> None:
        now = time.time()
        for uid in list(self.active):
            if uid in EXEMPT_USER_IDS:
                continue
            guild = self.bot.get_guild(self.active[uid][0])
            member = guild.get_member(uid) if guild else None
            if member is None or member.voice is None or not self.is_public_vc(member.voice.channel):
                self.deaf_since.pop(uid, None)
                self.alone_since.pop(uid, None)
                self.farming.discard(uid)
                continue

            vs = member.voice
            # deafened = not listening to anyone
            if vs.self_deaf:
                self.deaf_since.setdefault(uid, now)
            else:
                self.deaf_since.pop(uid, None)
            # alone = nobody but staff/bots in the channel, so no members being helped
            regulars = [m for m in vs.channel.members if not m.bot and not self.is_staff(m)]
            if regulars:
                self.alone_since.pop(uid, None)
            else:
                self.alone_since.setdefault(uid, now)

            reason, since = None, None
            if uid in self.deaf_since and now - self.deaf_since[uid] >= DEAF_MINUTES * 60:
                reason, since = f"deafened for {DEAF_MINUTES}+ minutes", self.deaf_since[uid]
            elif uid in self.alone_since and now - self.alone_since[uid] >= ALONE_MINUTES * 60:
                reason, since = f"alone in the VC for {ALONE_MINUTES}+ minutes", self.alone_since[uid]

            if reason:
                if uid not in self.farming:
                    await self._start_not_counting(member, reason, since)
            else:
                # no longer deafened / alone -> counting resumes
                self.farming.discard(uid)

    async def _start_not_counting(self, member: discord.Member, reason: str, since: float) -> None:
        """Take back the idle time once, then stop counting until the member is active again."""
        self._flush()  # credit up to this second so the deduction below is exact
        self._deduct(member.id, time.time() - since)
        self.farming.add(member.id)
        self._save(DATA_FILE, self.data)
        await self._dm(
            member,
            f"Hey 💜 you were {reason} in a public VC, so I took those points off and I'm not counting "
            "your time right now. Be active with us and I'll count you again ✨",
        )

        if FARM_LOG_CHANNEL_ID:
            log = self.bot.get_channel(FARM_LOG_CHANNEL_ID)
            if log:
                try:
                    await log.send(
                        f"💜 {member.mention} isn't being counted - {reason}.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

    # ---------- temp channels ----------
    def _pick_public_vc(self, guild: discord.Guild):
        """Where to send staff: PUBLIC_MOVE_VC_ID if set, else the public VC with the most regular members."""
        if MOVE_TO_VC_ID:
            ch = guild.get_channel(MOVE_TO_VC_ID)
            if ch:
                return ch
        best, best_n = None, -1
        for ch in guild.voice_channels:
            if not self.is_public_vc(ch):
                continue
            if ch.user_limit and len(ch.members) >= ch.user_limit:
                continue
            n = len([m for m in ch.members if not m.bot and not self.is_staff(m)])
            if n > best_n:
                best, best_n = ch, n
        return best

    async def _dm(self, member: discord.Member, text: str) -> None:
        try:
            await member.send(text)
        except discord.HTTPException:
            pass  # DMs closed

    async def _move_member(self, member: discord.Member, dest, reason: str, dm: str | None = None) -> bool:
        try:
            await member.move_to(dest, reason=reason)
        except discord.Forbidden:
            if not self._warned_move:
                print("[vc_staff] can't move members - give the bot Move Members and "
                      "View Channel/Connect on the target channels.")
                self._warned_move = True
            return False
        except discord.HTTPException as e:
            print(f"[vc_staff] move failed: {e!r}")
            return False
        self.temp_since.pop(member.id, None)
        self.temp_idle_since.pop(member.id, None)
        if dm:
            await self._dm(member, dm)
        if FARM_LOG_CHANNEL_ID:
            log = self.bot.get_channel(FARM_LOG_CHANNEL_ID)
            if log:
                try:
                    await log.send(
                        f"💜 {member.mention} moved to {dest.mention} - {reason}.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        return True

    async def _temp_check(self) -> None:
        if not temp_rules_active():
            # resting hours: staff can sit muted/deafened in temp VCs, timers start fresh at the next shift
            self.temp_since.clear()
            self.temp_idle_since.clear()
            return
        now = time.time()
        seen: set[int] = set()
        for guild in self.bot.guilds:
            for ch in guild.voice_channels:
                if not self.is_temp_vc(ch):
                    continue
                for m in list(ch.members):
                    if not self.is_staff(m) or m.id in EXEMPT_USER_IDS:
                        continue
                    seen.add(m.id)
                    self.temp_since.setdefault(m.id, now)
                    vs = m.voice
                    if vs and (vs.self_mute or vs.self_deaf or vs.mute or vs.deaf):
                        self.temp_idle_since.setdefault(m.id, now)
                    else:
                        self.temp_idle_since.pop(m.id, None)

                    # muted / deafened -> AFK
                    if m.id in self.temp_idle_since and now - self.temp_idle_since[m.id] >= TEMP_MUTED_MINUTES * 60:
                        afk = guild.get_channel(AFK_CHANNEL_ID)
                        if afk is None:
                            print(f"[vc_staff] AFK channel {AFK_CHANNEL_ID} not found.")
                        else:
                            await self._move_member(
                                m, afk, f"muted/deafened {TEMP_MUTED_MINUTES}+ min in a temp VC",
                                dm=(f"Hey 💜 you were muted/deafened in a temp VC for {TEMP_MUTED_MINUTES}+ minutes, "
                                    "so I moved you to AFK. Join a public VC when you're ready to help ✨"),
                            )
                        continue

                    # too long in a temp VC -> public VC
                    if now - self.temp_since[m.id] >= TEMP_STAY_MINUTES * 60:
                        dest = self._pick_public_vc(guild)
                        if dest is None:
                            print("[vc_staff] no public VC to move staff to.")
                            continue
                        await self._move_member(
                            m, dest, f"{TEMP_STAY_MINUTES}+ min in a temp VC",
                            dm=(f"Hey 💜 you were in a temp VC for {TEMP_STAY_MINUTES}+ minutes, "
                                f"so I moved you to {dest.name}. Come help us in the public VCs ✨"),
                        )
        for uid in list(self.temp_since):
            if uid not in seen:
                self.temp_since.pop(uid, None)
                self.temp_idle_since.pop(uid, None)

    # ---------- the live leaderboard ----------
    def _find_channel(self):
        override = self.panel.get("channel_override")
        if override:
            ch = self.bot.get_channel(override)
            if ch:
                return ch
        if LEADERBOARD_CHANNEL_ID:
            ch = self.bot.get_channel(LEADERBOARD_CHANNEL_ID)
            if ch:
                return ch
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                if CHANNEL_NAME_HINT in ch.name.lower():
                    return ch
        return None

    def _staff_members(self, guild: discord.Guild) -> dict[int, discord.Member]:
        """Everyone holding the staff or moderator role (shown even with 0 time)."""
        found: dict[int, discord.Member] = {}
        for rid in STAFF_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                for m in role.members:
                    if not m.bot:
                        found[m.id] = m
        return found

    def _top3(self, guild: discord.Guild):
        rows = []
        for m in self._staff_members(guild).values():
            total = sum(self.data.get(str(m.id), {}).values())
            rows.append((m, total))
        rows.sort(key=lambda r: (-r[1], r[0].display_name.lower()))
        return [m for m, _ in rows[:3]]

    async def _podium_image(self, guild: discord.Guild):
        """PNG bytes of the podium banner (cached; redrawn only when the top 3 change)."""
        if podium_art is None:
            return None
        try:
            top = self._top3(guild)
            if podium_art.USE_BUTTERFLY:
                key = "static"
            else:
                key = tuple((m.id, m.display_avatar.key) for m in top)
            if self._podium_png is None or key != self._podium_key:
                avatars = []
                for m in top:
                    try:
                        avatars.append(await m.display_avatar.replace(size=256, format="png").read())
                    except Exception:
                        avatars.append(None)
                self._podium_png = await asyncio.to_thread(podium_art.render_podium, avatars)
                self._podium_key = key
            return self._podium_png
        except Exception as e:
            print(f"[vc_staff] podium render failed: {e!r}")
            return None

    def _build_embed(self, guild: discord.Guild, podium: bool = False) -> discord.Embed:
        week_keys = {today_key(i) for i in range(7)}
        day_key = today_key()
        name = self.bot.user.display_name

        live_members = list(self._live_members(guild))
        live_ids = {m.id for _, m in live_members}

        rows = []  # (member, total_secs, week_secs, today_secs)
        for m in self._staff_members(guild).values():
            per_day = self.data.get(str(m.id), {})
            total = sum(per_day.values())
            week = sum(v for k, v in per_day.items() if k in week_keys)
            rows.append((m, total, week, per_day.get(day_key, 0)))
        rows.sort(key=lambda r: (-r[1], r[0].display_name.lower()))
        total_all = sum(r[1] for r in rows)

        desc = (
            f"## {EMOJI_STAR} STAFF LEADERBOARD {EMOJI_STAR}\n"
            f"{EMOJI_KITTY} *{name} counts every minute you spend helping in voice* ✨\n"
            f"-# `{PTS_PER_HOUR} pts / hour in VC` · staff & mods only\n"
            "✦ ━━━━━━━━━━━━━━━━━━━━ ✦"
        )
        if total_all <= 0:
            desc += "\n\n*💜 Tracking is live - hours appear as soon as you join the public VCs.*"

        embed = discord.Embed(description=desc, color=EMBED_COLOR, timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        if podium:
            embed.set_image(url="attachment://podium.png")
        elif BANNER_URL:
            embed.set_image(url=BANNER_URL)

        # ---- podium
        medals = ("🥇", "🥈", "🥉")
        places = ("1st place", "2nd place", "3rd place")
        for medal, place, (m, total, _, _) in zip(medals, places, rows[:3]):
            dot = f"{LIVE_DOT} " if m.id in live_ids else ""
            embed.add_field(
                name=f"{medal} {place}",
                value=f"{dot}{m.mention}\n**{pts(total):,}** pts\n`{fmt(total)}` in voice",
                inline=True,
            )

        # ---- rankings #4+  (butterfly in the circle, no bars)
        if len(rows) > 3:
            lines, size = [], 0
            for i, (m, total, _, _) in enumerate(rows[3:], start=4):
                live = f" {LIVE_DOT}" if m.id in live_ids else ""
                line = f"{EMOJI_BUTTERFLY} `#{i:>2}` {m.mention} ・ **{pts(total):,}** pts{live}"
                if size + len(line) + 1 > 880:
                    lines.append("*...and more* 💜")
                    break
                lines.append(line)
                size += len(line) + 1
            embed.add_field(name=f"{EMOJI_ICON} ʀᴀɴᴋɪɴɢs", value="\n".join(lines), inline=False)

        # ---- top this week / top today
        weekly = sorted((r for r in rows if r[2] > 0), key=lambda r: r[2], reverse=True)[:3]
        if weekly:
            embed.add_field(
                name=f"{EMOJI_KITTY} ᴛᴏᴘ ᴛʜɪs ᴡᴇᴇᴋ",
                value="\n".join(
                    f"{medals[i]} {m.mention} **{pts(w):,}** pts" for i, (m, _, w, _) in enumerate(weekly)
                ),
                inline=True,
            )
        daily = sorted((r for r in rows if r[3] > 0), key=lambda r: r[3], reverse=True)[:3]
        if daily:
            embed.add_field(
                name="✨ ᴛᴏᴘ ᴛᴏᴅᴀʏ",
                value="\n".join(
                    f"{medals[i]} {m.mention} **{pts(d):,}** pts" for i, (m, _, _, d) in enumerate(daily)
                ),
                inline=True,
            )

        # ---- who is in voice right now
        if live_members:
            by_ch: dict = {}
            for ch, m in live_members:
                by_ch.setdefault(ch, []).append(m)
            lines = [f"🎙️ {ch.mention} - " + " ".join(m.mention for m in ms) for ch, ms in by_ch.items()]
            embed.add_field(name=f"{LIVE_DOT} ʟɪᴠᴇ ɪɴ ᴠᴏɪᴄᴇ", value="\n".join(lines)[:1024], inline=False)

        # ---- team totals
        embed.add_field(
            name=f"{EMOJI_STAR} ᴛᴇᴀᴍ ᴛᴏᴛᴀʟs",
            value=(
                f"`{len(rows)}` staff/mods · `{pts(total_all):,}` pts · `{fmt(total_all)}` in voice\n"
                f"{LIVE_DOT} `{len(live_ids)}` in public VC right now"
            ),
            inline=False,
        )
        embed.set_footer(text=f"{name} · live · updates every {REFRESH_SECONDS}s 💜")
        return embed

    async def _update_panel(self) -> None:
        ch = self._find_channel()
        if ch is None:
            if not self._warned:
                print("[vc_staff] leaderboard channel not found - set LEADERBOARD_CHANNEL_ID "
                      f"or name a text channel '{CHANNEL_NAME_HINT}'.")
                self._warned = True
            return

        png = await self._podium_image(ch.guild)
        embed = self._build_embed(ch.guild, podium=png is not None)
        no_ping = discord.AllowedMentions.none()

        # find the existing message (cached, else by saved id)
        if self._msg is None and self.panel.get("channel_id") == ch.id and self.panel.get("message_id"):
            try:
                self._msg = await ch.fetch_message(self.panel["message_id"])
            except discord.NotFound:
                self._msg = None

        if self._msg is not None:
            try:
                kwargs = {"embed": embed, "allowed_mentions": no_ping}
                if png is not None and (self._podium_sent_key != self._podium_key or not self._msg.attachments):
                    kwargs["attachments"] = [discord.File(io.BytesIO(png), "podium.png")]
                    self._podium_sent_key = self._podium_key
                self._msg = await self._msg.edit(**kwargs)
                return
            except discord.NotFound:
                self._msg = None  # deleted - post a fresh one below

        kwargs = {"embed": embed, "allowed_mentions": no_ping}
        if png is not None:
            kwargs["file"] = discord.File(io.BytesIO(png), "podium.png")
            self._podium_sent_key = self._podium_key
        self._msg = await ch.send(**kwargs)
        self.panel.update({"channel_id": ch.id, "message_id": self._msg.id})
        self._save(PANEL_FILE, self.panel)

    # ---------- the one command ----------
    @app_commands.command(name="leaderboard", description="Post the live staff leaderboard in this channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        # remove the old board (cached message, else the saved ids)
        if self._msg is not None:
            try:
                await self._msg.delete()
            except discord.HTTPException:
                pass
        elif self.panel.get("channel_id") and self.panel.get("message_id"):
            try:
                old = self.bot.get_partial_messageable(self.panel["channel_id"]).get_partial_message(self.panel["message_id"])
                await old.delete()
            except discord.HTTPException:
                pass
        self._msg = None

        ch = interaction.channel
        self._flush()
        png = await self._podium_image(ch.guild)
        kwargs = {"embed": self._build_embed(ch.guild, podium=png is not None),
                  "allowed_mentions": discord.AllowedMentions.none()}
        if png is not None:
            kwargs["file"] = discord.File(io.BytesIO(png), "podium.png")
            self._podium_sent_key = self._podium_key
        try:
            self._msg = await ch.send(**kwargs)
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't post here - give me View Channel, Send Messages and Embed Links in this channel.",
                ephemeral=True,
            )
            return
        self.panel = {"channel_override": ch.id, "channel_id": ch.id, "message_id": self._msg.id}
        self._save(PANEL_FILE, self.panel)
        await interaction.followup.send("Live leaderboard posted here. It updates by itself.", ephemeral=True)

    # ---------- debug command ----------
    @app_commands.command(name="tempcheck", description="Show what the bot sees in the temp VCs (debug)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def tempcheck(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
            return
        guild, now = interaction.guild, time.time()
        temp = [c for c in guild.voice_channels if self.is_temp_vc(c)]
        afk = guild.get_channel(AFK_CHANNEL_ID)
        dest = self._pick_public_vc(guild)
        lines = [
            f"Move Members permission: **{'yes' if guild.me.guild_permissions.move_members else 'NO - give it to the bot'}**",
            f"Temp VCs found: **{len(temp)}**",
            f"AFK channel: {afk.mention if afk else '**NOT FOUND**'}",
            f"Public VC target: {dest.mention if dest else '**NONE FOUND**'}",
            f"Rules: public VC after {TEMP_STAY_MINUTES} min, AFK after {TEMP_MUTED_MINUTES} min muted/deafened",
            f"Active hours: {TEMP_RULES_START_HOUR}:00-{TEMP_RULES_END_HOUR}:00 ({BOT_TIMEZONE}) - "
            f"now {datetime.now(LOCAL_TZ).strftime('%H:%M')} -> rules **{'ON' if temp_rules_active() else 'OFF (resting hours)'}**",
        ]
        if not temp:
            cats = ", ".join(f"`{c.name}`" for c in guild.categories)
            lines.append("No temp VCs detected. Set Railway variable `TEMP_CATEGORY_ID` to the ID of your temp "
                         f"category. Categories I can see: {cats}")
        for c in temp:
            for m in c.members:
                if not self.is_staff(m):
                    continue
                mins = (now - self.temp_since.get(m.id, now)) / 60
                flag = " muted/deaf" if m.id in self.temp_idle_since else ""
                ex = " (exempt)" if m.id in EXEMPT_USER_IDS else ""
                lines.append(f"{c.mention}: {m.mention} - {mins:.0f} min{flag}{ex}")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(VCStaff(bot))
