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
        if channel is None:
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
        was = self.is_public_vc(before.channel)
        now_in = self.is_public_vc(after.channel)

        if now_in and not was:
            self.active[member.id] = (member.guild.id, time.time())
        elif was and not now_in:
            session = self.active.pop(member.id, None)
            if session:
                self._add(member.id, time.time() - session[1])

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

    def _build_embed(self, guild: discord.Guild) -> discord.Embed:
        week_keys = {today_key(i) for i in range(7)}

        rows = []  # (member, total_secs, week_secs)
        for uid, per_day in self.data.items():
            m = guild.get_member(int(uid))
            if m is None or not self.is_staff(m):
                continue
            total = sum(per_day.values())
            if total <= 0:
                continue
            week = sum(v for k, v in per_day.items() if k in week_keys)
            rows.append((m, total, week))
        rows.sort(key=lambda r: r[1], reverse=True)

        desc = (
            "Earn points by staying active in the public voice channels.\n"
            f"`{PTS_PER_HOUR} pts / hour in VC`"
        )
        if not rows:
            desc += "\n\nNo staff/mod VC time recorded yet."

        embed = discord.Embed(
            title="STAFF LEADERBOARD",
            description=desc,
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        # podium
        for label, (m, total, _) in zip(("1st place", "2nd place", "3rd place"), rows[:3]):
            embed.add_field(
                name=label,
                value=f"{m.mention}\n**{pts(total):,}** pts\n`{fmt(total)}` in voice",
                inline=True,
            )

        # rankings 4-15
        if len(rows) > 3:
            top = rows[0][1]
            lines = []
            for i, (m, total, _) in enumerate(rows[3:15], start=4):
                filled = max(1, round(total / top * BAR_LEN))
                bar = "■" * filled + "□" * (BAR_LEN - filled)
                lines.append(f"`#{i:>2}` {m.mention} `{bar}` **{pts(total):,}** pts")
            embed.add_field(name="Rankings", value="\n".join(lines)[:1024], inline=False)

        # top this week
        weekly = sorted((r for r in rows if r[2] > 0), key=lambda r: r[2], reverse=True)[:3]
        if weekly:
            lines = [f"`#{i}` {m.mention} **{pts(w):,}** pts" for i, (m, _, w) in enumerate(weekly, 1)]
            embed.add_field(name="Top this week", value="\n".join(lines), inline=False)

        # team totals
        total_all = sum(r[1] for r in rows)
        live = sum(1 for _ in self._live_members(guild))
        embed.add_field(
            name="Team totals",
            value=(
                f"`{len(rows)}` staff/mods · `{pts(total_all):,}` pts · `{fmt(total_all)}` in voice\n"
                f"`{live}` in public VC right now"
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
