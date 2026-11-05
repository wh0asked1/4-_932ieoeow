"""Slash commands: /start /stop /login /token /removetoken /status /ping
Ephemeral everywhere except /ping."""

import asyncio
import threading
import math
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
import ui
from api import DiscordAPI, fetch_latest_build_number
from quests import QuestRunner

MISSING_TOKEN_MSG = "Missing token, please insert it via `/token` or `/login`."

# ── Pagination View ────────────────────────────────────────────────────────────
class StatusPaginator(discord.ui.View):
    def __init__(self, embeds: list[discord.Embed]):
        super().__init__(timeout=180)
        self.embeds = embeds
        self.current = 0
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.current == 0)
        self.next_button.disabled = (self.current >= len(self.embeds) - 1)
        self.page_label.label = f"Page {self.current + 1} / {len(self.embeds)}"

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_label(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)


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
        self._api_cache: DiscordAPI | None = None

    # ── helpers ────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        self._log_buffer.append(line)
        self._log_buffer = self._log_buffer[-50:]
        print(line)

    def _worker(self, owner_interaction: discord.Interaction):
        try:
            api = DiscordAPI(self.user_token, self.build_number)
            user = api.validate()
            if not user:
                self._log("token invalid — worker aborting")
                return
            
            self._api_cache = api
            self._log(f"logged in as {user['username']} ({user['id']})")
            self._runner = QuestRunner(api, on_log=self._log, stop_event=self._stop)

            while not self._stop.is_set():
                try:
                    self._runner.run()
                except Exception as e:
                    self._log(f"loop err: {e}")
                
                if self._stop.is_set():
                    break
                
                for _ in range(config.POLL_INTERVAL):
                    if self._stop.is_set():
                        break
                    threading.Event().wait(1)

            self._send_summary_dm(owner_interaction, user)

        except Exception as e:
            self._log(f"worker fatal: {e}")

    def _send_summary_dm(self, interaction: discord.Interaction, user_data: dict):
        if not self._runner:
            return
        
        stats = self._runner.stats
        embed = ui.ok(
            "🏁 Quest Farming Complete",
            "The bench is clean. Here is your final haul.",
            [
                ("Account", f"**{user_data['username']}**", True),
                ("Total Accepted", f"`{stats.get('accepted', 0)}`", True),
                ("Total Completed", f"`{stats.get('completed', 0)}`", True),
            ]
        )
        
        async def send_dm():
            try:
                await interaction.user.send(embed=embed)
            except discord.Forbidden:
                self._log("Could not DM user (DMs closed).")
        
        asyncio.run_coroutine_threadsafe(send_dm(), self.bot.loop)

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── commands ───────────────────────────────────────────────────────────
    @app_commands.command(name="ping", description="Check if the bot is alive.")
    async def ping(self, interaction: discord.Interaction):
        # NOT ephemeral
        await interaction.response.send_message(embed=ui.ok(
            "Pong 🏓", f"Gateway latency: `{round(self.bot.latency * 1000)}ms`"))

    @app_commands.command(name="token", description="Set the user token used for quest farming.")
    @app_commands.describe(user_id="Your Discord User ID", token="The user token")
    async def token_cmd(self, interaction: discord.Interaction, user_id: str, token: str):
        await interaction.response.defer(ephemeral=True)
        
        if not user_id.isdigit():
            return await interaction.followup.send(
                embed=ui.err("Invalid ID", "User ID must be a number."), ephemeral=True)

        api = DiscordAPI(token.strip(), self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send(
                embed=ui.err("Token rejected", "Discord said no. Check the token and retry."),
                ephemeral=True)
        
        if str(user["id"]) != user_id:
            return await interaction.followup.send(
                embed=ui.err("Mismatch", "The User ID provided doesn't match the token."), ephemeral=True)

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
        # No missing-token warning here, just let the API validate it
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(self.user_token or "", self.build_number)
        user = api.validate()
        
        if not user:
            return await interaction.followup.send(
                embed=ui.err("Token Rejected", "Rejected by Discord. Missing or invalid token."), 
                ephemeral=True)
        
        await interaction.followup.send(embed=ui.ok(
            "Logged In",
            "The account on the bench is live.",
            [("Account", f"**{user['username']}**\n`{user['id']}`", False)]),
            ephemeral=True)

    @app_commands.command(name="start", description="Start the quest auto-farmer.")
    async def start(self, interaction: discord.Interaction):
        # No missing-token warning here, just let the API validate it
        if self._running():
            return await interaction.response.send_message(
                embed=ui.warn("Already Running", "The farmer's already on the floor."),
                ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        api = DiscordAPI(self.user_token or "", self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send(
                embed=ui.err("Token Invalid", "Missing or invalid token. Use `/token` first."), 
                ephemeral=True)

        self.build_number = await asyncio.to_thread(fetch_latest_build_number)
        self._stop.clear()
        self._started_at = datetime.now(timezone.utc)
        
        self._thread = threading.Thread(target=self._worker, args=(interaction,), daemon=True)
        self._thread.start()

        user_created_at = datetime.fromtimestamp(discord.utils.snowflake_time(int(user['id'])).timestamp())

        embed = ui.ok(
            "🚀 Quest Farmer Started",
            "Connected and ready. The bench is cooking.",
            [
                ("Account", f"**{user['username']}**\n`{user['id']}`", True),
                ("Created", user_created_at.strftime("%Y-%m-%d"), True),
                ("Client Build", f"`{self.build_number}`", True),
            ]
        )
        embed.set_thumbnail(url=f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png" if user.get('avatar') else None)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Stop the quest auto-farmer.")
    async def stop(self, interaction: discord.Interaction):
        if not self.user_token:
            return await interaction.response.send_message(
                embed=ui.warn("Missing Token", MISSING_TOKEN_MSG), ephemeral=True)
        if not self._running():
            return await interaction.response.send_message(
                embed=ui.warn("Not Running", "Nothing to stop."), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None
        
        await interaction.followup.send(embed=ui.ok(
            "Quest Farmer Stopped", "Thread closed clean. Bench tidied."), ephemeral=True)

    @app_commands.command(name="status", description="Show farmer status and recent log.")
    async def status(self, interaction: discord.Interaction):
        if not self.user_token:
            return await interaction.response.send_message(
                embed=ui.warn("Missing Token", MISSING_TOKEN_MSG), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        running = self._running()
        stats = self._runner.stats if self._runner else {}
        token_state = "🟢 Set"
        uptime = "—"
        if self._started_at and running:
            delta = datetime.now(timezone.utc) - self._started_at
            uptime = str(delta).split(".")[0]

        quests = []
        if self._api_cache:
            try:
                quests = self._api_cache.get("/quests/@me").json().get("quests", [])
            except Exception:
                pass

        embeds = []
        per_page = 5
        total_pages = max(1, math.ceil(len(quests) / per_page))

        for page in range(total_pages):
            start = page * per_page
            chunk = quests[start:start + per_page]
            
            status_desc = (
                f"**State:** {'🟢 Running' if running else '🔴 Stopped'}\n"
                f"**Token:** {token_state}\n"
                f"**Uptime:** `{uptime}`\n"
                f"**Accepted:** `{stats.get('accepted', 0)}` | "
                f"**Completed:** `{stats.get('completed', 0)}` | "
                f"**Errors:** `{stats.get('errors', 0)}`"
            )

            embed = ui.card(
                title="📊 Quest Farmer Status",
                description=status_desc,
                color=ui.OK_COLOR if running else ui.ERR_COLOR,
            )
            
            quest_text = ""
            if not chunk:
                quest_text = "*No active quests found.*"
            for q in chunk:
                name = q.get("config", {}).get("messages", {}).get("questName", "Unknown Quest")
                us = q.get("userStatus", {})
                enrolled = bool(us.get("enrolledAt"))
                completed = bool(us.get("completedAt"))
                
                progress_str = "0/0"
                if enrolled and not completed:
                    prog = us.get("progress", {})
                    for task_type, data in prog.items():
                        val = data.get("value", 0)
                        target = q.get("config", {}).get("taskConfig", {}).get("tasks", {}).get(task_type, {}).get("target", 0)
                        progress_str = f"{int(val)}/{target}"
                        break
                elif completed:
                    progress_str = "Completed ✅"
                
                status_icon = "🟢" if completed else ("🟡" if enrolled else "⚪")
                quest_text += f"{status_icon} **{name}**\n└ `{progress_str}`\n\n"

            embed.add_field(name="Active Quests", value=quest_text.strip() or "None", inline=False)
            embed.set_footer(text=f"🏷️ Founder @tryacidbacon | Page {page + 1} / {total_pages}")
            embeds.append(embed)

        view = StatusPaginator(embeds) if len(embeds) > 1 else None
        await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
