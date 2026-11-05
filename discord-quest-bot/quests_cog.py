"""Slash commands: /start /stop /login /token /removetoken /status /ping
Every reply is a branded embed carrying the founder credit.
NOW OPEN TO EVERYONE."""

import asyncio
import threading
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
import ui
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
        # 🔓 GATE RIPPED OFF — everyone is an owner now
        return True

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
                for _ in range(config.POLL_INTERVAL):
                    if self._stop.is_set():
                        break
                    threading.Event().wait(1)
        except Exception as e:
            self._log(f"worker fatal: {e}")

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # The old _deny function is removed since nobody gets denied.

    # ── commands ───────────────────────────────────────────────────────────
    @app_commands.command(name="ping", description="Check if the bot is alive.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=ui.ok(
            "Pong 🏓", f"Gateway latency: `{round(self.bot.latency * 1000)}ms`"))

    @app_commands.command(name="token", description="Set the user token used for quest farming.")
    @app_commands.describe(token="The Discord user token (keep it private!)")
    async def token_cmd(self, interaction: discord.Interaction, token: str):
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(token.strip(), self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send(
                embed=ui.err("Token rejected", "Discord said no. Check the token and retry."),
                ephemeral=True)
        self.user_token = token.strip()
        await interaction.followup.send(embed=ui.ok(
            "Token Set",
            "Quest farmer is armed and ready.",
            [("Account", f"**{user['username']}**\n`{user['id']}`", False)]),
            ephemeral=True)

    @app_commands.command(name="removetoken", description="Forget the stored user token.")
    async def removetoken(self, interaction: discord.Interaction):
        self.user_token = None
        await interaction.response.send_message(
            embed=ui.warn("Token Cleared", "Nothing on the bench. `/token` to set again."),
            ephemeral=True)

    @app_commands.command(name="login", description="Validate the current token and show the account.")
    async def login(self, interaction: discord.Interaction):
        if not self.user_token:
            return await interaction.response.send_message(
                embed=ui.warn("No Token", "Use `/token <token>` first."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(self.user_token, self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send(
                embed=ui.err("Token Rejected", "Rejected by Discord."), ephemeral=True)
        await interaction.followup.send(embed=ui.ok(
            "Logged In",
            "The account on the bench is live.",
            [("Account", f"**{user['username']}**\n`{user['id']}`", False)]),
            ephemeral=True)

    @app_commands.command(name="start", description="Start the quest auto-farmer.")
    async def start(self, interaction: discord.Interaction):
        if not self.user_token:
            return await interaction.response.send_message(
                embed=ui.warn("No Token", "Set one first: `/token <token>`"), ephemeral=True)
        if self._running():
            return await interaction.response.send_message(
                embed=ui.warn("Already Running", "The farmer's already on the floor."),
                ephemeral=True)
        await interaction.response.defer()
        self.build_number = await asyncio.to_thread(fetch_latest_build_number)
        self._stop.clear()
        self._started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        await interaction.followup.send(embed=ui.ok(
            "Quest Farmer Started",
            "Scanning, accepting, completing — hands are moving.",
            [("Client Build", f"`{self.build_number}`", True),
             ("Poll Interval", f"`{config.POLL_INTERVAL}s`", True)]))

    @app_commands.command(name="stop", description="Stop the quest auto-farmer.")
    async def stop(self, interaction: discord.Interaction):
        if not self._running():
            return await interaction.response.send_message(
                embed=ui.warn("Not Running", "Nothing to stop."), ephemeral=True)
        await interaction.response.defer()
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None
        self._runner = None
        await interaction.followup.send(embed=ui.ok(
            "Quest Farmer Stopped", "Thread closed clean. Bench tidied."))

    @app_commands.command(name="status", description="Show farmer status and recent log.")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        running = self._running()
        stats = self._runner.stats if self._runner else {}
        token_state = "set ✅" if self.user_token else "missing ❌"
        uptime = "—"
        if self._started_at and running:
            delta = datetime.now(timezone.utc) - self._started_at
            uptime = str(delta).split(".")[0]
        log_tail = "\n".join(self._log_buffer[-10:]) or "(no log yet)"

        embed = ui.card(
            title="📊 Quest Farmer Status",
            description="Live snapshot off the bench.",
            color=ui.OK_COLOR if running else ui.ERR_COLOR,
            fields=[
                ("State", "🟢 running" if running else "🔴 stopped", True),
                ("Token", token_state, True),
                ("Uptime", f"`{uptime}`", True),
                ("Client Build", f"`{self.build_number}`", True),
                ("Accepted", f"`{stats.get('accepted', 0)}`", True),
                ("Completed", f"`{stats.get('completed', 0)}`", True),
                ("Errors", f"`{stats.get('errors', 0)}`", True),
                ("Recent Log", f"```{log_tail[-1000:]}```", False),
            ],
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
