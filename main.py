import os

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN env var is missing (set it in Railway > Variables).")

intents = discord.Intents.default()
intents.members = True         # needed to read member roles
intents.voice_states = True    # needed to track VC joins/leaves
intents.message_content = True # needed for ! prefix commands

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def setup_hook():
    await bot.load_extension("vc_staff_cog")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(TOKEN)
