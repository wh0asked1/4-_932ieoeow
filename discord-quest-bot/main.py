import asyncio
import logging

import discord
from discord.ext import commands

import config
import quests_cog  # <-- direct import of the flattened cog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("questbot")


class QuestBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        # Load the flattened cog
        await self.load_extension("quests_cog")
        
        # Sync slash commands
        if config.GUILD_ID:
            guild = discord.Object(id=int(config.GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"synced slash commands to guild {config.GUILD_ID}")
        else:
            await self.tree.sync()
            log.info("synced slash commands globally")

    async def on_ready(self):
        log.info(f"logged in as {self.user} ({self.user.id})")


async def main():
    if not config.DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN missing in env")
    bot = QuestBot()
    async with bot:
        await bot.start(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
