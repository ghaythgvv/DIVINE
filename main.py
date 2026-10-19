import os

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN env var is missing (set it in Railway > Variables).")

intents = discord.Intents.default()
intents.members = True         # needed to read member roles
intents.voice_states = True    # needed to track VC joins/leaves
intents.message_content = True # kept on so the bot connects exactly like before

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def setup_hook():
    await bot.load_extension("vc_staff_cog")

    # The bot has no commands anymore: wipe the / commands registered earlier
    # so they disappear from the server. (Safe to delete this block after one run.)
    try:
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
        await bot.tree.sync()
        print("Old slash commands cleared.")
    except Exception as e:
        print(f"Could not clear slash commands: {e!r}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(TOKEN)
