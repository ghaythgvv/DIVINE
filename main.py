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

    # Register the / slash commands. Set GUILD_ID in Railway for instant updates
    # in your server; without it they sync globally (can take a while to appear).
    guild_id = os.getenv("GUILD_ID")
    try:
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash commands to guild {guild_id}: {[c.name for c in synced]}")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} slash commands globally: {[c.name for c in synced]}")
    except discord.Forbidden:
        print("SLASH SYNC FAILED (403 Missing Access): re-invite the bot with BOTH the "
              "'bot' and 'applications.commands' scopes, and check GUILD_ID is the right server.")
    except Exception as e:
        print(f"SLASH SYNC FAILED: {e!r}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(TOKEN)
