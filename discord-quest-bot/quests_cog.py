"""Slash commands: /start /stop /login /logout /status /ping /notification"""

import asyncio
import threading
import math
import json
import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
import ui
from api import DiscordAPI, fetch_latest_build_number
from quests import QuestRunner, get_quest_name, get_seconds_needed, get_seconds_done, get_user_status, is_enrolled, is_completed

TRACKER_FILE = "tracker.json"

def load_tracker():
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_tracker(data):
    try:
        with open(TRACKER_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass

def get_orbs(quest):
    cfg = quest.get("config", {})
    rewards = cfg.get("rewards", [])
    if isinstance(rewards, dict):
        rewards = [rewards]
    orbs = 0
    for r in rewards:
        if r.get("type") == "ORB" or "orb" in r.get("type", "").lower():
            orbs += r.get("quantity", 0)
    return orbs

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
        self.user_id = None
        self._selfbot_user = None
        self._dm_user = None
        self.build_number = config.DEFAULT_BUILD_NUMBER
        self.notifications_enabled = True
        
        self._thread = None
        self._monitor_thread = None
        self._stop = threading.Event()
        self._runner = None
        self._api_cache = None
        
        self._total_seconds = 0
        self._last_start_time = None

    def _log(self, msg):
        print(msg)

    @property
    def _uptime_str(self):
        total = self._total_seconds
        if self._running() and self._last_start_time:
            total += (time.time() - self._last_start_time)
        h = int(total // 3600)
        m = int((total % 3600) // 60)
        s = int(total % 60)
        return str(h) + "h " + str(m) + "m " + str(s) + "s"

    def _update_tracker(self):
        if not self._api_cache or not self.user_id:
            return
        try:
            quests = self._api_cache.get("/quests/@me").json().get("quests", [])
            tracker = load_tracker()
            if self.user_id not in tracker:
                tracker[self.user_id] = {}
            changed = False
            
            newly_completed = []
            for q in quests:
                if is_completed(q):
                    name = get_quest_name(q)
                    if name not in tracker[self.user_id]:
                        tracker[self.user_id][name] = datetime.now(timezone.utc).isoformat()
                        changed = True
                        newly_completed.append(q)
            
            if changed:
                save_tracker(tracker)
                
            if self.notifications_enabled and self._dm_user:
                for q in newly_completed:
                    name = get_quest_name(q)
                    orbs = get_orbs(q)
                    self._send_completion_dm(name, orbs)
        except Exception:
            pass

    def _send_completion_dm(self, name, orbs):
        fields = []
        fields.append(("Quest", "**" + name + "**", False))
        if orbs > 0:
            fields.append(("Orbs", "`" + str(orbs) + "`", True))
        
        embed = ui.ok("✅ Quest Completed", "A quest just finished on the bench.", fields)
        
        if self._selfbot_user and self._selfbot_user.get('avatar'):
            avatar_url = "https://cdn.discordapp.com/avatars/" + self._selfbot_user['id'] + "/" + self._selfbot_user['avatar'] + ".png"
            embed.set_thumbnail(url=avatar_url)
        
        async def send():
            try:
                await self._dm_user.send(embed=embed)
            except Exception:
                pass
        
        asyncio.run_coroutine_threadsafe(send(), self.bot.loop)

    def _monitor_loop(self):
        while not self._stop.is_set():
            self._update_tracker()
            for _ in range(15):
                if self._stop.is_set():
                    return
                threading.Event().wait(1)

    def _worker(self, owner_interaction: discord.Interaction):
        try:
            api = DiscordAPI(self.user_token, self.build_number)
            user = api.validate()
            if not user:
                self._log("token invalid — worker aborting")
                return
            
            self._api_cache = api
            self._selfbot_user = user
            self.user_id = user['id']
            self._log("logged in as " + user['username'])
            self._runner = QuestRunner(api, on_log=self._log, stop_event=self._stop)

            while not self._stop.is_set():
                try:
                    self._runner.run()
                    self._update_tracker()
                except Exception as e:
                    self._log("loop err: " + str(e))
                
                if self._stop.is_set():
                    break
                
                for _ in range(config.POLL_INTERVAL):
                    if self._stop.is_set():
                        break
                    threading.Event().wait(1)

            self._update_tracker()
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

    @app_commands.command(name="notification", description="Toggle quest completion DMs.")
    @app_commands.describe(enabled="True to enable, False to disable")
    async def notification(self, interaction: discord.Interaction, enabled: bool):
        self.notifications_enabled = enabled
        status_str = "enabled" if enabled else "disabled"
        await interaction.response.send_message(embed=ui.ok("Notifications", "Quest completion DMs are now **" + status_str + "**."), ephemeral=True)

    @app_commands.command(name="login", description="Log in with your user token.")
    @app_commands.describe(token="The user token to log in with")
    async def login(self, interaction: discord.Interaction, token: str):
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(token.strip(), self.build_number)
        user = api.validate()
        if not user:
            return await interaction.followup.send(embed=ui.err("Token Rejected", "Rejected by Discord. Invalid token."), ephemeral=True)
        
        self.user_token = token.strip()
        self.user_id = user['id']
        self._selfbot_user = user
        acc_str = "**" + user['username'] + "**\n`" + user['id'] + "`"
        await interaction.followup.send(
            embed=ui.ok("Logged In", "The account on the bench is live and saved.", [("Account", acc_str, False)]),
            ephemeral=True
        )

    @app_commands.command(name="logout", description="Forget the stored user token.")
    async def logout(self, interaction: discord.Interaction):
        self.user_token = None
        self.user_id = None
        self._selfbot_user = None
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

        self.user_id = user['id']
        self._selfbot_user = user
        self._dm_user = interaction.user
        self._last_start_time = time.time()
        
        self.build_number = await asyncio.to_thread(fetch_latest_build_number)
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, args=(interaction,), daemon=True)
        self._thread.start()
        
        if not getattr(self, "_monitor_thread", None) or not self._monitor_thread.is_alive():
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()

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
        
        if self._last_start_time:
            self._total_seconds += time.time() - self._last_start_time
            self._last_start_time = None
            
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
        
        uptime = self._uptime_str

        all_quests = []
        if self._api_cache:
            try:
                def fetch():
                    return self._api_cache.get("/quests/@me").json().get("quests", [])
                all_quests = await asyncio.to_thread(fetch)
            except Exception:
                pass

        accepted_quests = []
        completed_quests = []
        for q in all_quests:
            if is_enrolled(q) and not is_completed(q):
                accepted_quests.append(q)
            elif is_completed(q):
                completed_quests.append(q)

        embeds = []
        per_page = 5
        total_pages = max(1, math.ceil(len(accepted_quests) / per_page))

        for page in range(total_pages):
            start = page * per_page
            chunk = accepted_quests[start:start + per_page]
            
            status_desc = "**State:** 🟢 Running\n"
            status_desc += "**Total Hours:** `" + uptime + "`\n"
            status_desc += "**Accepted:** `" + str(len(accepted_quests)) + "` | "
            status_desc += "**Completed:** `" + str(len(completed_quests)) + "`\n"

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

        view = StatusPaginator(embeds)
        await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
