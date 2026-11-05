"""Slash commands: /start /stop /login /logout /status /ping"""

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
from quests import QuestRunner, get_quest_name, get_seconds_needed, get_seconds_done, get_user_status


def make_bar(current, total, length=10):
    if total <= 0:
        return "[----------] 0%"
    percent = min(1.0, max(0.0, current / total))
    filled = int(length * percent)
    empty = length - filled
    bar = "[" + ("■" * filled) + ("-" * empty) + "]"
    pct = int(percent * 100)
    return bar + " " + str(pct) + "%"


class StatusPaginator(discord.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=180)
        self.embeds = embeds
        self.current = 0
        if len(self.embeds) <= 1:
            self.clear_items()
        else:
            self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.current == 0)
        self.next_button.disabled = (self.current >= len(self.embeds) - 1)
        self.page_label.label = "Page " + str(self.current + 1) + " / " + str(len(self.embeds))

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
        self.user_token = config.DEFAULT_USER_TOKEN or None
        self.build_number = config.DEFAULT_BUILD_NUMBER
        self._thread = None
        self._stop = threading.Event()
        self._runner = None
        self._started_at = None
        self._api_cache = None

    def _log(self, msg):
        print(msg)

    def _worker(self, owner_interaction: discord.Interaction):
        try:
            api = DiscordAPI(self.user_token, self.build_number)
            user = api.validate()
            if not user:
                self._log("token invalid — worker aborting")
                return
            
            self._api_cache = api
            self._log("logged in as " + user['username'])
            self._runner = QuestRunner(api, on_log=self._log, stop_event=self._stop)

            while not self._stop.is_set():
                try:
                    self._runner.run()
                except Exception as e:
                    self._log("loop err: " + str(e))
                
                if self._stop.is_set():
                    break
                
                for _ in range(config.POLL_INTERVAL):
                    if self._stop.is_set():
                        break
                    threading.Event().wait(1)

            self._send_summary_dm(owner_interaction, user)

        except Exception as e:
            self._log("worker fatal: " + str(e))

    def _send_summary_dm(self, interaction: discord.Interaction, user_data: dict):
        if not self._runner:
            return
        
        stats = self._runner.stats
        fields = []
        fields.append(("Account", "**" + user_data['username'] + "**", True))
        fields.append(("Total Accepted", "`" + str(stats.get('accepted', 0)) + "`", True))
        fields.append(("Total Completed", "`" + str(stats.get('completed', 0)) + "`", True))
        
        embed = ui.ok("🏁 Quest Farming Complete", "The bench is clean. Here is your final haul.", fields)
        
        async def send_dm():
            try:
                await interaction.user.send(embed=embed)
            except discord.Forbidden:
                self._log("Could not DM user (DMs closed).")
        
        asyncio.run_coroutine_threadsafe(send_dm(), self.bot.loop)

    def _running(self):
        return self._thread is not None and self._thread.is_alive()

    @app_commands.command(name="ping", description="Check if the bot is alive.")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(embed=ui.ok("Pong 🏓", "Gateway latency: `" + str(latency) + "ms`"))

    @app_commands.command(name="login", description="Log in with your user token.")
    @app_commands.describe(token="The user token to log in with")
    async def login(self, interaction: discord.Interaction, token: str):
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(token.strip(), self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send(embed=ui.err("Token Rejected", "Rejected by Discord. Invalid token."), ephemeral=True)
        
        self.user_token = token.strip()
        acc_str = "**" + user['username'] + "**\n`" + user['id'] + "`"
        await interaction.followup.send(
            embed=ui.ok("Logged In", "The account on the bench is live and saved.", [("Account", acc_str, False)]),
            ephemeral=True
        )

    @app_commands.command(name="logout", description="Forget the stored user token.")
    async def logout(self, interaction: discord.Interaction):
        self.user_token = None
        await interaction.response.send_message(embed=ui.warn("Logged Out", "Token cleared. The bench is empty."), ephemeral=True)

    @app_commands.command(name="start", description="Start the quest auto-farmer.")
    async def start(self, interaction: discord.Interaction):
        if self._running():
            return await interaction.response.send_message(embed=ui.warn("Already Running", "The farmer's already on the floor."), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(self.user_token or "", self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send(embed=ui.err("Token Invalid", "Missing or invalid token. Use `/login` first."), ephemeral=True)

        self.build_number = await asyncio.to_thread(fetch_latest_build_number)
        self._stop.clear()
        self._started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._worker, args=(interaction,), daemon=True)
        self._thread.start()

        user_created_at = datetime.fromtimestamp(discord.utils.snowflake_time(int(user['id'])).timestamp())
        
        acc_str = "**" + user['username'] + "**\n`" + user['id'] + "`"
        fields = [
            ("Account", acc_str, True),
            ("Created", user_created_at.strftime("%Y-%m-%d"), True),
            ("Client Build", "`" + str(self.build_number) + "`", True),
        ]
        
        embed = ui.ok("🚀 Quest Farmer Started", "Connected and ready. The bench is cooking.", fields)
        
        if user.get('avatar'):
            avatar_url = "https://cdn.discordapp.com/avatars/" + user['id'] + "/" + user['avatar'] + ".png"
            embed.set_thumbnail(url=avatar_url)
            
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Stop the quest auto-farmer.")
    async def stop(self, interaction: discord.Interaction):
        if not self._running():
            return await interaction.response.send_message(embed=ui.warn("Not Running", "Nothing to stop."), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None
        await interaction.followup.send(embed=ui.ok("Quest Farmer Stopped", "Thread closed clean. Bench tidied."), ephemeral=True)

    @app_commands.command(name="status", description="Show farmer status and active quests.")
    async def status(self, interaction: discord.Interaction):
        if not self.user_token:
            return await interaction.response.send_message(embed=ui.warn("Missing Token", "Missing token, please insert it via `/login`"), ephemeral=True)
        if not self._running():
            return await interaction.response.send_message(embed=ui.warn("Not Running", "The bot is not running. Start it with `/start`."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        stats = self._runner.stats if self._runner else {}
        uptime = "—"
        if self._started_at:
            delta = datetime.now(timezone.utc) - self._started_at
            uptime = str(delta).split(".")[0]

        all_quests = []
        if self._api_cache:
            try:
                all_quests = self._api_cache.get("/quests/@me").json().get("quests", [])
            except Exception:
                pass

        accepted_quests = []
        for q in all_quests:
            us = get_user_status(q)
            if us.get("enrolledAt") and not us.get("completedAt"):
                accepted_quests.append(q)

        embeds = []
        per_page = 5
        total_pages = max(1, math.ceil(len(accepted_quests) / per_page))

        for page in range(total_pages):
            start = page * per_page
            chunk = accepted_quests[start:start + per_page]
            
            status_desc = "**State:** 🟢 Running\n"
            status_desc += "**Uptime:** `" + uptime + "`\n"
            status_desc += "**Accepted:** `" + str(stats.get('accepted', 0)) + "` | "
            status_desc += "**Completed:** `" + str(stats.get('completed', 0)) + "` | "
            status_desc += "**Errors:** `" + str(stats.get('errors', 0)) + "`"

            embed = ui.card(title="📊 Quest Farmer Status", description=status_desc, color=ui.OK_COLOR)
            
            quest_text = ""
            if not chunk:
                quest_text = "*No accepted quests pending.*"
            for q in chunk:
                name = get_quest_name(q)
                val = get_seconds_done(q)
                target = get_seconds_needed(q)
                
                bar_str = make_bar(val, target)
                
                quest_text += "**" + name + "**\n"
                quest_text += "`" + str(int(val)) + "/" + str(target) + "` " + bar_str + "\n\n"

            embed.add_field(name="Active Quests", value=quest_text.strip() or "None", inline=False)
            embed.set_footer(text="🏷️ Founder @tryacidbacon | Page " + str(page + 1) + " / " + str(total_pages))
            embeds.append(embed)

        view = StatusPaginator(embeds) if len(embeds) > 1 else None
        await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
