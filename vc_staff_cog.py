"""
vc_staff_cog.py - ONE live staff leaderboard, ONE slash command (/leaderboard).

What it does:
  - Tracks how long STAFF + MODERATORS spend in the public voice channels.
  - Keeps a single embed in the staff-leaderboard channel and edits it every
    30 seconds (podium, rankings, top this week, team totals).
  - Uses the bot's own profile picture as the embed thumbnail.

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

import json
import os
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

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
BAR_LEN = 8
REFRESH_SECONDS = 30
EMBED_COLOR = 0x8A2BE2

# Your custom emojis (the bot must share the server they live in)
EMOJI_STAR = "<a:Elt_emoji_51:1552029199215100004>"
EMOJI_KITTY = "<a:Elt_Hellokitty:1544075596806623392>"
EMOJI_ICON = "<:Elt_emoji_89:1552239138697121802>"

# Optional big banner image at the bottom of the board (direct image/gif link).
BANNER_URL = os.getenv("LEADERBOARD_BANNER_URL", "")

# ---- anti-farming ----
AFK_CHANNEL_ID = 1513904300013785241
ANTI_FARM = os.getenv("ANTI_FARM", "1") != "0"   # set ANTI_FARM=0 in Railway to switch off
DEAF_MINUTES = 5     # staff/mod deafened this long in a public VC = farming
ALONE_MINUTES = 20   # staff/mod with no regular members (only staff/bots) this long = farming
EXEMPT_USER_IDS: set[int] = set()   # user IDs that are never moved
FARM_LOG_CHANNEL_ID = int(os.getenv("FARM_LOG_CHANNEL_ID", "0") or 0)  # optional log channel


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
        self._warned_move = False
        self.deaf_since: dict[int, float] = {}
        self.alone_since: dict[int, float] = {}
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
            self._add(uid, now - last)
            if still_in:
                self.active[uid] = (guild_id, now)
            else:
                del self.active[uid]

    @tasks.loop(seconds=REFRESH_SECONDS)
    async def ticker(self):
        self._flush()
        if ANTI_FARM:
            try:
                await self._anti_farm()
            except Exception as e:
                print(f"[vc_staff] anti-farm check failed: {e!r}")
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
        if before.channel != after.channel:
            self.deaf_since.pop(member.id, None)
            self.alone_since.pop(member.id, None)
        elif before.self_deaf and not after.self_deaf:
            self.deaf_since.pop(member.id, None)

        was = self.is_public_vc(before.channel)
        now_in = self.is_public_vc(after.channel)

        if now_in and not was:
            self.active[member.id] = (member.guild.id, time.time())
        elif was and not now_in:
            session = self.active.pop(member.id, None)
            if session:
                self._add(member.id, time.time() - session[1])

    # ---------- anti-farming ----------
    def _deduct(self, uid: int, seconds: float) -> None:
        """Take farmed time back off the member's recent days."""
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

            if uid in self.deaf_since and now - self.deaf_since[uid] >= DEAF_MINUTES * 60:
                await self._move_to_afk(member, f"deafened for {DEAF_MINUTES}+ minutes", self.deaf_since[uid])
            elif uid in self.alone_since and now - self.alone_since[uid] >= ALONE_MINUTES * 60:
                await self._move_to_afk(member, f"alone in the VC for {ALONE_MINUTES}+ minutes", self.alone_since[uid])

    async def _move_to_afk(self, member: discord.Member, reason: str, since: float) -> None:
        afk = member.guild.get_channel(AFK_CHANNEL_ID)
        if afk is None:
            print(f"[vc_staff] AFK channel {AFK_CHANNEL_ID} not found.")
            return

        self._flush()  # credit up to this second so the deduction below is exact
        farmed = time.time() - since
        try:
            await member.move_to(afk, reason=f"Farming in public VC: {reason}")
        except discord.Forbidden:
            if not self._warned_move:
                print("[vc_staff] can't move members - give the bot Move Members and "
                      "Connect/View Channel on the AFK channel.")
                self._warned_move = True
            return
        except discord.HTTPException as e:
            print(f"[vc_staff] move failed: {e!r}")
            return

        self._deduct(member.id, farmed)
        self.deaf_since.pop(member.id, None)
        self.alone_since.pop(member.id, None)
        self._save(DATA_FILE, self.data)

        try:
            await member.send(
                f"You were moved to AFK because you were farming in the public VC ({reason}). "
                "The points from that time were removed."
            )
        except discord.HTTPException:
            pass  # DMs closed

        if FARM_LOG_CHANNEL_ID:
            log = self.bot.get_channel(FARM_LOG_CHANNEL_ID)
            if log:
                try:
                    await log.send(
                        f"{member.mention} was moved to AFK - {reason}.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

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

    def _build_embed(self, guild: discord.Guild) -> discord.Embed:
        week_keys = {today_key(i) for i in range(7)}
        day_key = today_key()

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
            "Earn points by staying active in the public voice channels.\n"
            f"`{PTS_PER_HOUR} pts / hour in VC` · `staff & mods only`"
        )
        if total_all <= 0:
            desc += "\n\n*Tracking is live - hours appear as staff & mods join the public VCs.*"

        embed = discord.Embed(description=desc, color=EMBED_COLOR, timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        if BANNER_URL:
            embed.set_image(url=BANNER_URL)

        # ---- podium (side by side, like the ELITE SYSTEM board)
        medals = ("🥇", "🥈", "🥉")
        places = ("1st place", "2nd place", "3rd place")
        for medal, place, (m, total, _, _) in zip(medals, places, rows[:3]):
            dot = "🟢 " if m.id in live_ids else ""
            embed.add_field(
                name=f"{medal} {place}",
                value=f"{dot}{m.mention}\n**{pts(total):,}** pts\n`{fmt(total)}` in voice",
                inline=True,
            )

        # ---- rankings #4+
        if len(rows) > 3:
            top = rows[0][1] or 1
            lines, size = [], 0
            for i, (m, total, _, _) in enumerate(rows[3:], start=4):
                filled = round(total / top * BAR_LEN)
                if total > 0:
                    filled = max(1, filled)
                bar = "■" * filled + "□" * (BAR_LEN - filled)
                dot = "🟢" if m.id in live_ids else "⚫"
                line = f"{dot} `#{i:>2}` {m.mention} `{bar}` **{pts(total):,}** pts"
                if size + len(line) + 1 > 1000:
                    lines.append("*...and more*")
                    break
                lines.append(line)
                size += len(line) + 1
            embed.add_field(name=f"{EMOJI_ICON} Rankings", value="\n".join(lines), inline=False)

        # ---- top this week / top today (side by side)
        weekly = sorted((r for r in rows if r[2] > 0), key=lambda r: r[2], reverse=True)[:3]
        if weekly:
            embed.add_field(
                name=f"{EMOJI_KITTY} Top this week",
                value="\n".join(
                    f"`#{i}` {m.mention} **{pts(w):,}** pts" for i, (m, _, w, _) in enumerate(weekly, 1)
                ),
                inline=True,
            )
        daily = sorted((r for r in rows if r[3] > 0), key=lambda r: r[3], reverse=True)[:3]
        if daily:
            embed.add_field(
                name="🔥 Top today",
                value="\n".join(
                    f"`#{i}` {m.mention} **{pts(d):,}** pts" for i, (m, _, _, d) in enumerate(daily, 1)
                ),
                inline=True,
            )

        # ---- who is in voice right now
        if live_members:
            by_ch: dict = {}
            for ch, m in live_members:
                by_ch.setdefault(ch, []).append(m)
            lines = [
                f"🎙️ {ch.mention} - " + " ".join(m.mention for m in ms) for ch, ms in by_ch.items()
            ]
            embed.add_field(name="🟢 Live in voice", value="\n".join(lines)[:1024], inline=False)

        # ---- team totals
        embed.add_field(
            name=f"{EMOJI_STAR} Team totals",
            value=(
                f"`{len(rows)}` staff/mods · `{pts(total_all):,}` pts · `{fmt(total_all)}` in voice\n"
                f"🟢 `{len(live_ids)}` in public VC right now"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Live · updates every {REFRESH_SECONDS}s")
        return embed

    async def _update_panel(self) -> None:
        ch = self._find_channel()
        if ch is None:
            if not self._warned:
                print("[vc_staff] leaderboard channel not found - set LEADERBOARD_CHANNEL_ID "
                      f"or name a text channel '{CHANNEL_NAME_HINT}'.")
                self._warned = True
            return

        embed = self._build_embed(ch.guild)
        no_ping = discord.AllowedMentions.none()

        # find the existing message (cached, else by saved id)
        if self._msg is None and self.panel.get("channel_id") == ch.id and self.panel.get("message_id"):
            try:
                self._msg = await ch.fetch_message(self.panel["message_id"])
            except discord.NotFound:
                self._msg = None

        if self._msg is not None:
            try:
                await self._msg.edit(embed=embed, allowed_mentions=no_ping)
                return
            except discord.NotFound:
                self._msg = None  # deleted - post a fresh one below

        self._msg = await ch.send(embed=embed, allowed_mentions=no_ping)
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
        try:
            self._msg = await ch.send(
                embed=self._build_embed(ch.guild),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't post here - give me View Channel, Send Messages and Embed Links in this channel.",
                ephemeral=True,
            )
            return
        self.panel = {"channel_override": ch.id, "channel_id": ch.id, "message_id": self._msg.id}
        self._save(PANEL_FILE, self.panel)
        await interaction.followup.send("Live leaderboard posted here. It updates by itself.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VCStaff(bot))
