import asyncio
import logging
import sys

import discord
from discord.ext import commands

import config

# ── Better Logging ─────────────────────────────────────────────────────────────
# Force logs to standard out so Railway captures every line.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("questbot")


class QuestBot(commands.Bot):
    def __init__(self):
        # Discord requires specific intents. 
        # We use default, but if you ever need members/message content, enable them here.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        log.info("Running setup_hook...")
        
        # 1. Load the Cog
        try:
            await self.load_extension("quests_cog")
            log.info("✅ Successfully loaded quests_cog")
        except Exception as e:
            log.error(f"❌ Failed to load quests_cog: {e}")
            raise e  # Stop booting if the cog is broken

        # 2. Sync Slash Commands
        log.info("Syncing slash commands...")
        try:
            if config.GUILD_ID:
                guild = discord.Object(id=int(config.GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info(f"✅ Synced commands to Guild ID: {config.GUILD_ID}")
            else:
                await self.tree.sync()
                log.info("✅ Synced commands globally (may take up to 1 hour to appear)")
        except Exception as e:
            log.error(f"❌ Failed to sync commands: {e}")

    async def on_ready(self):
        log.info(f"🚀 Logged in as {self.user} (ID: {self.user.id})")
        log.info("Bench is warm. Ready to farm.")


async def main():
    log.info("Booting up quest bot...")
    
    # Check for Token
    if not config.DISCORD_BOT_TOKEN or config.DISCORD_BOT_TOKEN == "your_bot_token_here":
        log.critical("❌ CRITICAL: DISCORD_BOT_TOKEN is missing from your Railway variables!")
        sys.exit(1)

    bot = QuestBot()
    
    try:
        log.info("Attempting to connect to Discord...")
        async with bot:
            await bot.start(config.DISCORD_BOT_TOKEN)
            
    except discord.LoginFailure:
        log.critical("❌ Login Failure: The bot token is invalid or malformed.")
        log.critical("Check your Railway variables and paste the token again. No quotes.")
        sys.exit(1)
        
    except discord.PrivilegedIntentsRequired:
        log.critical("❌ Intents Failure: You need to enable Privileged Intents in the Discord Developer Portal.")
        log.critical("Go to https://discord.com/developers/applications -> Bot -> Enable Presence, Members, and Message Content.")
        sys.exit(1)
        
    except Exception as e:
        log.critical(f"❌ Fatal crash during startup: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
