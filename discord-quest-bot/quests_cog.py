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
    rewards = cfg.get("rewards", {})
    
    if isinstance(rewards, dict):
        if "orb_quantity" in rewards:
            return rewards["orb_quantity"]
        if "quantity" in rewards:
            return rewards["quantity"]
        rewards = [rewards]
    
    orbs = 0
    if isinstance(rewards, list):
        for r in rewards:
            if isinstance(r, dict):
                if "orb_quantity" in r:
                    orbs += r["orb_quantity"]
                elif r.get("type") == "ORB" or "orb" in str(r.get("type", "")).lower():
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
        self.accounts = {}
        self._workers = {}
        self.build_number = config.DEFAULT_BUILD_NUMBER
        self.notifications_enabled = True

    def _log(self, msg):
        print(msg)

    def _update_tracker(self, api, selfbot_user, dm_user, silent=False):
        user_id = selfbot_user['id']
        try:
            quests = api.get("/quests/@me").json().get("quests", [])
            tracker = load_tracker()
            if user_id not in tracker:
                tracker[user_id] = {}
            changed = False
            
            newly_completed = []
            for q in quests:
                if is_completed(q):
                    name = get_quest_name(q)
                    if name not in tracker[user_id]:
                        tracker[user_id][name] = datetime.now(timezone.utc).isoformat()
                        changed = True
                        if not silent:
                            newly_completed.append(q)
            
            if changed:
                save_tracker(tracker)
                
            if self.notifications_enabled and dm_user and not silent:
                for q in newly_completed:
                    name = get_quest_name(q)
                    orbs = get_orbs(q)
                    self._send_completion_dm(dm_user, selfbot_user, name, orbs)
        except Exception:
            pass

    def _send_completion_dm(self, dm_user, selfbot_user, name, orbs):
        fields = []
        fields.append(("Quest", "**" + name + "**", False))
        if orbs > 0:
            fields.append(("Orbs", "`" + str(orbs) + "`", True))
        
        embed = ui.ok("✅ Quest Completed", "A quest just finished on the bench.", fields)
        
        if selfbot_user and selfbot_user.get('avatar'):
            avatar_url = "https://cdn.discordapp.com/avatars/" + selfbot_user['id'] + "/" + selfbot_user['avatar'] + ".png"
            embed.set_thumbnail(url=avatar_url)
        
        async def send():
            try:
                await dm_user.send(embed=embed)
            except Exception:
                pass
        
        asyncio.run_coroutine_threadsafe(send(), self.bot.loop)

    def _monitor_loop(self, token, selfbot_user, dm_user, stop_event):
        api = DiscordAPI(token, self.build_number)
        self._update_tracker(api, selfbot_user, dm_user, silent=True)
        while not stop_event.is_set():
            self._update_tracker(api, selfbot_user, dm_user)
            for _ in range(15):
                if stop_event.is_set():
                    return
                threading.Event().wait(1)

    def _worker(self, api, selfbot_user, dm_user, stop_event):
        user_id = selfbot_user['id']
        runner = QuestRunner(api, on_log=self._log, stop_event=stop_event)
        if user_id in self._workers:
            self._workers[user_id]["runner"] = runner
            
        try:
            self._update_tracker(api, selfbot_user, dm_user, silent=True)
            while not stop_event.is_set():
                try:
                    runner.run()
                    self._update_tracker(api, selfbot_user, dm_user)
                except Exception as e:
                    self._log("loop err: " + str(e))
                
                if stop_event.is_set():
                    break
                
                for _ in range(config.POLL_INTERVAL):
                    if stop_event.is_set():
                        break
                    threading.Event().wait(1)

            self._update_tracker(api, selfbot_user, dm_user)
            self._send_summary_dm(dm_user, selfbot_user, runner)

        except Exception as e:
            self._log("worker fatal: " + str(e))

    def _send_summary_dm(self, dm_user, selfbot_user, runner):
        if not runner:
            return
        
        stats = runner.stats
        fields = []
        fields.append(("Account", "**" + selfbot_user['username'] + "**", True))
        fields.append(("Total Accepted", "`" + str(stats.get('accepted', 0)) + "`", True))
        fields.append(("Total Completed", "`" + str(stats.get('completed', 0)) + "`", True))
        
        embed = ui.ok("🏁 Quest Farming Complete", "The bench is clean. Here is your final haul.", fields)
        
        async def send_dm():
            try:
                await dm_user.send(embed=embed)
            except discord.Forbidden:
                self._log("Could not DM user (DMs closed).")
        
        asyncio.run_coroutine_threadsafe(send_dm(), self.bot.loop)

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
        
        self.accounts[interaction.user.id] = {
            "token": token.strip(),
            "user": user
        }
        
        acc_str = "**" + user['username'] + "**\n`" + user['id'] + "`"
        await interaction.followup.send(
            embed=ui.ok("Logged In", "The account on the bench is live and saved.", [("Account", acc_str, False)]),
            ephemeral=True
        )

    @app_commands.command(name="logout", description="Forget the stored user token.")
    async def logout(self, interaction: discord.Interaction):
        if interaction.user.id in self.accounts:
            del self.accounts[interaction.user.id]
            
        await interaction.response.send_message(embed=ui.warn("Logged Out", "Token cleared. The bench is empty."), ephemeral=True)

    @app_commands.command(name="start", description="Start the quest auto-farmer.")
    async def start(self, interaction: discord.Interaction):
        if interaction.user.id not in self.accounts:
            return await interaction.response.send_message(embed=ui.err("Not Logged In", "Use `/login` first."), ephemeral=True)
            
        acc = self.accounts[interaction.user.id]
        user = acc["user"]
        user_id = user['id']
        
        if user_id in self._workers and self._workers[user_id]["thread"].is_alive():
            return await interaction.response.send_message(embed=ui.warn("Already Running", "This account is already on the floor."), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        api = DiscordAPI(acc["token"], self.build_number)
        
        self.build_number = await asyncio.to_thread(fetch_latest_build_number)
        stop_event = threading.Event()
        
        self._workers[user_id] = {
            "thread": None,
            "monitor_thread": None,
            "stop": stop_event,
            "runner": None,
            "start_time": time.time(),
            "total_seconds": 0,
            "api": api,
            "last_quests": []  # Fallback cache
        }

        t = threading.Thread(target=self._worker, args=(api, user, interaction.user, stop_event), daemon=True)
        m = threading.Thread(target=self._monitor_loop, args=(acc["token"], user, interaction.user, stop_event), daemon=True)
        
        self._workers[user_id]["thread"] = t
        self._workers[user_id]["monitor_thread"] = m
        
        t.start()
        m.start()

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
        if interaction.user.id not in self.accounts:
            return await interaction.response.send_message(embed=ui.warn("No Account", "Use `/login` first."), ephemeral=True)
            
        user_id = self.accounts[interaction.user.id]["user"]["id"]
        
        if user_id not in self._workers or not self._workers[user_id]["thread"].is_alive():
            return await interaction.response.send_message(embed=ui.warn("Not Running", "Your farmer is not running."), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        worker = self._workers[user_id]
        worker["stop"].set()
        worker["total_seconds"] += time.time() - worker["start_time"]
        
        del self._workers[user_id]
        
        await interaction.followup.send(embed=ui.ok("Quest Farmer Stopped", "Thread closed clean. Bench tidied."), ephemeral=True)

    @app_commands.command(name="status", description="Show live farmer status and active quests.")
    async def status(self, interaction: discord.Interaction):
        if interaction.user.id not in self.accounts:
            return await interaction.response.send_message(embed=ui.warn("No Account", "Use `/login` first."), ephemeral=True)
            
        user_id = self.accounts[interaction.user.id]["user"]["id"]
        
        if user_id not in self._workers or not self._workers[user_id]["thread"].is_alive():
            return await interaction.response.send_message(embed=ui.warn("Not Running", "Your farmer is not running."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        worker = self._workers[user_id]
        token = self.accounts[interaction.user.id]["token"]
        
        view = await self._build_status_view(token, user_id, worker)
        msg = await interaction.followup.send(embed=view.embeds[0], view=view, ephemeral=True)

        for _ in range(30):
            await asyncio.sleep(10)
            if user_id not in self._workers or not self._workers[user_id]["thread"].is_alive() or msg.flags.ephemeral is False:
                break
            try:
                new_view = await self._build_status_view(token, user_id, self._workers[user_id])
                await msg.edit(embed=new_view.embeds[0], view=new_view)
            except Exception:
                break

    async def _build_status_view(self, token, user_id, worker_data):
        total = worker_data["total_seconds"] + (time.time() - worker_data["start_time"])
        h = int(total // 3600)
        m = int((total % 3600) // 60)
        s = int(total % 60)
        uptime = str(h) + "h " + str(m) + "m " + str(s) + "s"
        
        all_quests = []
        try:
            def fetch():
                temp_api = DiscordAPI(token, self.build_number)
                return temp_api.get("/quests/@me").json().get("quests", [])
            all_quests = await asyncio.to_thread(fetch)
            if all_quests:
                worker_data["last_quests"] = all_quests
        except Exception:
            pass
            
        if not all_quests:
            all_quests = worker_data.get("last_quests", [])

        enrolled_quests = []
        accepted_count = 0
        completed_count = 0
        
        for q in all_quests:
            if is_enrolled(q):
                enrolled_quests.append(q)
                if is_completed(q):
                    completed_count += 1
                else:
                    accepted_count += 1

        enrolled_quests.sort(key=lambda x: is_completed(x))

        embeds = []
        per_page = 5
        total_pages = max(1, math.ceil(len(enrolled_quests) / per_page))

        for page in range(total_pages):
            start = page * per_page
            chunk = enrolled_quests[start:start + per_page]
            
            status_desc = "**State:** 🟢 Running\n"
            status_desc += "**Total Hours:** `" + uptime + "`\n"
            status_desc += "**Accepted:** `" + str(accepted_count) + "` | "
            status_desc += "**Completed:** `" + str(completed_count) + "`\n"

            embed = ui.card(title="📊 Quest Farmer Status", description=status_desc, color=ui.OK_COLOR)
            
            quest_text = ""
            if not chunk:
                quest_text = "*No enrolled quests found.*"
            for q in chunk:
                name = get_quest_name(q)
                if is_completed(q):
                    quest_text += "**" + name + "**\n✅ `Completed`\n\n"
                else:
                    val = get_seconds_done(q)
                    target = get_seconds_needed(q)
                    bar_str = make_bar(val, target)
                    quest_text += "**" + name + "**\n"
                    quest_text += "`" + str(int(val)) + "/" + str(target) + "` " + bar_str + "\n\n"

            embed.add_field(name="Your Quests", value=quest_text.strip() or "None", inline=False)
            embed.set_footer(text="🏷️ Founder @tryacidbacon | Page " + str(page + 1) + " / " + str(total_pages))
            embeds.append(embed)

        return StatusPaginator(embeds)

async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
