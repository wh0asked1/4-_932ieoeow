"""Slash commands: /start /stop /login /token /removetoken /status /ping"""
import asyncio
import threading
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from api import DiscordAPI, fetch_latest_build_number
from quests import QuestRunner


class QuestCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_token: str | None = config.DEFAULT_USER_TOKEN or None
        self.build_number: int = config.DEFAULT_BUILD_NUMBER
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._runner: QuestRunner | None = None
        self._log_buffer: list[str] = []
        self._started_at: datetime | None = None

    # ── helpers ────────────────────────────────────────────────────────────
    def _is_owner(self, interaction: discord.Interaction) -> bool:
        if not config.OWNER_IDS:
            return True
        return interaction.user.id in config.OWNER_IDS

    def _log(self, msg: str):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        self._log_buffer.append(line)
        self._log_buffer = self._log_buffer[-50:]
        print(line)

    def _worker(self):
        try:
            api = DiscordAPI(self.user_token, self.build_number)
            user = api.validate()
            if not user:
                self._log("token invalid — worker aborting")
                return
            self._log(f"logged in as {user['username']} ({user['id']})")
            self._runner = QuestRunner(api, on_log=self._log, stop_event=self._stop)

            while not self._stop.is_set():
                try:
                    self._runner.run()
                except Exception as e:
                    self._log(f"loop err: {e}")
                # sleep between passes, but break if stopped
                for _ in range(config.POLL_INTERVAL):
                    if self._stop.is_set():
                        break
                    asyncio.run(asyncio.sleep(0)) if False else None
                    threading.Event().wait(1)
        except Exception as e:
            self._log(f"worker fatal: {e}")

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── commands ───────────────────────────────────────────────────────────
    @app_commands.command(name="ping", description="Check if the bot is alive.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"pong 🏓 — {round(self.bot.latency * 1000)}ms"
        )

    @app_commands.command(name="token", description="Set the user token used for quest farming.")
    @app_commands.describe(token="The Discord user token (keep it private!)")
    async def token_cmd(self, interaction: discord.Interaction, token: str):
        if not self._is_owner(interaction):
            return await interaction.response.send_message("not yours to touch", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(token.strip(), self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send("❌ token invalid — try again", ephemeral=True)

        self.user_token = token.strip()
        await interaction.followup.send(
            f"✅ token set — logged in as **{user['username']}** (`{user['id']}`)",
            ephemeral=True,
        )

    @app_commands.command(name="removetoken", description="Forget the stored user token.")
    async def removetoken(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            return await interaction.response.send_message("not yours to touch", ephemeral=True)
        self.user_token = None
        await interaction.response.send_message("🧽 token cleared", ephemeral=True)

    @app_commands.command(name="login", description="Validate the current token and show the account.")
    async def login(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            return await interaction.response.send_message("not yours to touch", ephemeral=True)
        if not self.user_token:
            return await interaction.response.send_message(
                "no token set — use `/token <token>`", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(self.user_token, self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send("❌ token rejected", ephemeral=True)
        await interaction.followup.send(
            f"✅ logged in as **{user['username']}** (`{user['id']}`)", ephemeral=True)

    @app_commands.command(name="start", description="Start the quest auto-farmer.")
    async def start(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            return await interaction.response.send_message("not yours to touch", ephemeral=True)
        if not self.user_token:
            return await interaction.response.send_message(
                "set a token first: `/token <token>`", ephemeral=True)
        if self._running():
            return await interaction.response.send_message("already running 🏃", ephemeral=True)

        await interaction.response.defer()
        self.build_number = await asyncio.to_thread(fetch_latest_build_number)
        self._stop.clear()
        self._started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        await interaction.followup.send(
            f"🚀 quest farmer started — build `{self.build_number}`")

    @app_commands.command(name="stop", description="Stop the quest auto-farmer.")
    async def stop(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            return await interaction.response.send_message("not yours to touch", ephemeral=True)
        if not self._running():
            return await interaction.response.send_message("not running", ephemeral=True)
        await interaction.response.defer()
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None
        self._runner = None
        await interaction.followup.send("🛑 stopped")

    @app_commands.command(name="status", description="Show farmer status and recent log.")
    async def status(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            return await interaction.response.send_message("not yours to touch", ephemeral=True)
        await interaction.response.defer()
        running = self._running()
        uptime = ""
        if self._started_at and running:
            delta = datetime.now(timezone.utc) - self._started_at
            uptime = f"\n**uptime:** `{str(delta).split('.')[0]}`"
        stats = self._runner.stats if self._runner else {}
        token_state = "set ✅" if self.user_token else "missing ❌"
        log_tail = "\n".join(self._log_buffer[-10:]) or "(no log yet)"
        embed = discord.Embed(
            title="Quest Farmer Status",
            color=discord.Color.green() if running else discord.Color.red(),
        )
        embed.add_field(name="running", value="yes" if running else "no", inline=True)
        embed.add_field(name="token", value=token_state, inline=True)
        embed.add_field(name="build", value=str(self.build_number), inline=True)
        embed.add_field(name="stats", value=(
            f"accepted: `{stats.get('accepted', 0)}`\n"
            f"completed: `{stats.get('completed', 0)}`\n"
            f"errors: `{stats.get('errors', 0)}`"
        ), inline=False)
        embed.add_field(name="recent log",
                        value=f"```{log_tail[-1000:]}```", inline=False)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
