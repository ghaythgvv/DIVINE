"""
vc_staff_cog.py - counts staff & mods in PUBLIC voice channels (live count + hours).

Commands (prefix !):
  !vclive              -> who is in public VCs right now (staff + mods)
  !vchours [member]    -> hours for one staff/mod (today / 7d / 30d / total)
  !vctop [days]        -> leaderboard of staff/mod VC hours (default 7 days)

Setup:
  - Enable the Server Members + Voice States intents (code and Dev Portal).
  - bot.load_extension("vc_staff_cog")  (or await bot.load_extension in setup_hook)
  - On Railway, attach a Volume and set VC_HOURS_FILE=/data/vc_staff_hours.json
    so hours survive redeploys.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

# Staff + Moderator roles - only these members are counted / can use commands.
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
EMBED_COLOR = 0x8A2BE2


def fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def today_key(offset_days: int = 0) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=offset_days)
    return d.strftime("%Y-%m-%d")


class VCStaff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data: dict[str, dict[str, float]] = self._load()
        # member_id -> (guild_id, last_tick_timestamp)
        self.active: dict[int, tuple[int, float]] = {}
        self.ticker.start()

    # ---------- storage ----------
    def _load(self) -> dict:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        tmp = DATA_FILE + ".tmp"
        try:
            folder = os.path.dirname(DATA_FILE)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, DATA_FILE)
        except OSError as e:
            print(f"[vc_staff] save failed: {e}")

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

    def _has_access(self, ctx: commands.Context) -> bool:
        return self.is_staff(ctx.author)

    # ---------- lifecycle ----------
    async def cog_unload(self):
        self.ticker.cancel()
        self._flush()
        self._save()

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

    @tasks.loop(seconds=60)
    async def ticker(self):
        self._flush()
        self._save()

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

    # ---------- commands ----------
    @commands.command(name="vclive")
    @commands.guild_only()
    async def vclive(self, ctx: commands.Context):
        if not self._has_access(ctx):
            return
        by_channel: dict[discord.abc.GuildChannel, list[discord.Member]] = {}
        for ch, m in self._live_members(ctx.guild):
            by_channel.setdefault(ch, []).append(m)

        total = sum(len(v) for v in by_channel.values())
        embed = discord.Embed(
            title="Staff & Mods in Public VC",
            description=f"**{total}** online in public voice right now",
            color=EMBED_COLOR,
        )
        for ch, members in by_channel.items():
            embed.add_field(
                name=f"{ch.name} ({len(members)})",
                value="\n".join(m.mention for m in members)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.command(name="vchours")
    @commands.guild_only()
    async def vchours(self, ctx: commands.Context, member: discord.Member = None):
        if not self._has_access(ctx):
            return
        member = member or ctx.author
        self._flush()  # include the running session
        days = self.data.get(str(member.id), {})

        def span(n):
            keys = {today_key(i) for i in range(n)}
            return sum(v for k, v in days.items() if k in keys)

        embed = discord.Embed(title=f"VC hours - {member.display_name}", color=EMBED_COLOR)
        embed.add_field(name="Today", value=fmt(span(1)))
        embed.add_field(name="7 days", value=fmt(span(7)))
        embed.add_field(name="30 days", value=fmt(span(30)))
        embed.add_field(name="Total", value=fmt(sum(days.values())))
        await ctx.send(embed=embed)

    @commands.command(name="vctop")
    @commands.guild_only()
    async def vctop(self, ctx: commands.Context, days: int = 7):
        if not self._has_access(ctx):
            return
        days = max(1, min(days, 365))
        self._flush()
        keys = {today_key(i) for i in range(days)}

        rows = []
        for uid, per_day in self.data.items():
            m = ctx.guild.get_member(int(uid))
            if m is None or not self.is_staff(m):
                continue
            secs = sum(v for k, v in per_day.items() if k in keys)
            if secs > 0:
                rows.append((m, secs))
        rows.sort(key=lambda r: r[1], reverse=True)

        lines = [
            f"`{i}.` {m.mention} - **{fmt(s)}**" for i, (m, s) in enumerate(rows[:10], 1)
        ] or ["No staff/mod VC time recorded yet."]
        embed = discord.Embed(
            title=f"Staff & Mod VC Hours - last {days}d",
            description="\n".join(lines),
            color=EMBED_COLOR,
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(VCStaff(bot))
