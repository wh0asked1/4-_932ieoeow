"""Quest helpers + core auto-completer logic."""
import random
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from api import DiscordAPI
from config import HEARTBEAT_INTERVAL, DEBUG, SUPPORTED_TASKS


def _get(d: Optional[dict], *keys):
    if d is None:
        return None
    for k in keys:
        if k in d:
            return d[k]
    return None


def get_task_config(q): return q.get("config", {}) and _get(q.get("config", {}), "taskConfig", "task_config", "taskConfigV2", "task_config_v2")
def get_quest_name(q):
    cfg = q.get("config", {})
    msgs = cfg.get("messages", {})
    return (_get(msgs, "questName", "quest_name")
            or _get(msgs, "gameTitle", "game_title")
            or (cfg.get("application", {}) or {}).get("name")
            or f"Quest#{q.get('id', '?')}").strip()


def get_expires_at(q): return _get(q.get("config", {}), "expiresAt", "expires_at")
def get_user_status(q):
    us = _get(q, "userStatus", "user_status")
    return us if isinstance(us, dict) else {}


def is_completable(q):
    expires = get_expires_at(q)
    if expires:
        try:
            dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if dt <= datetime.now(timezone.utc):
                return False
        except Exception:
            pass
    tc = get_task_config(q)
    if not tc or "tasks" not in tc:
        return False
    return any(tc["tasks"].get(t) is not None for t in SUPPORTED_TASKS)


def is_enrolled(q): return bool(_get(get_user_status(q), "enrolledAt", "enrolled_at"))
def is_completed(q): return bool(_get(get_user_status(q), "completedAt", "completed_at"))
def get_enrolled_at(q): return _get(get_user_status(q), "enrolledAt", "enrolled_at")


def get_task_type(q):
    tc = get_task_config(q)
    if not tc or "tasks" not in tc:
        return None
    for t in SUPPORTED_TASKS:
        if tc["tasks"].get(t) is not None:
            return t
    return None


def get_seconds_needed(q):
    tc, t = get_task_config(q), get_task_type(q)
    if not tc or not t:
        return 0
    return tc["tasks"][t].get("target", 0)


def get_seconds_done(q):
    t = get_task_type(q)
    if not t:
        return 0
    return get_user_status(q).get("progress", {}).get(t, {}).get("value", 0)


class QuestRunner:
    """Runs the full quest loop against a given DiscordAPI instance."""

    def __init__(self, api: DiscordAPI, on_log=None, stop_event=None):
        self.api = api
        self.on_log = on_log or (lambda *a, **k: None)
        self.stop_event = stop_event
        self.completed_ids: set = set()
        self.stats = {"accepted": 0, "completed": 0, "errors": 0}

    def _stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def fetch_quests(self):
        r = self.api.get("/quests/@me")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                return data.get("quests", [])
            if isinstance(data, list):
                return data
        return []

    def enroll(self, quest):
        qid, name = quest["id"], get_quest_name(quest)
        try:
            r = self.api.post(f"/quests/{qid}/enroll", {
                "location": 11,
                "is_targeted": False,
                "metadata_raw": None,
                "metadata_sealed": None,
                "traffic_metadata_raw": quest.get("traffic_metadata_raw"),
                "traffic_metadata_sealed": quest.get("traffic_metadata_sealed"),
            })
            if r.status_code in (200, 201, 204):
                self.on_log(f"Accepted: {name}")
                self.stats["accepted"] += 1
                return True
            if r.status_code == 429:
                time.sleep(r.json().get("retry_after", 5) + 1)
            return False
        except Exception as e:
            self.on_log(f"Enroll error {name}: {e}")
            return False

    def complete_video(self, quest):
        qid, name = quest["id"], get_quest_name(quest)
        need, done = get_seconds_needed(quest), get_seconds_done(quest)
        enrolled_at = get_enrolled_at(quest)
        enrolled_ts = (datetime.fromisoformat(enrolled_at.replace("Z", "+00:00")).timestamp()
                       if enrolled_at else time.time())
        max_future, speed, interval = 10, 7, 1
        while done < need and not self._stopped():
            max_allowed = (time.time() - enrolled_ts) + max_future
            if max_allowed - done >= speed:
                ts = done + speed
                try:
                    r = self.api.post(f"/quests/{qid}/video-progress",
                                      {"timestamp": min(need, ts + random.random())})
                    if r.status_code == 200:
                        if r.json().get("completed_at"):
                            break
                        done = min(need, ts)
                        self.on_log(f"[video] {name} {done:.0f}/{need}s")
                    elif r.status_code == 429:
                        time.sleep(r.json().get("retry_after", 5) + 1)
                        continue
                except Exception as e:
                    self.on_log(f"Video err: {e}")
            if ts >= need:
                break
            time.sleep(interval)
        try:
            self.api.post(f"/quests/{qid}/video-progress", {"timestamp": need})
        except Exception:
            pass
        self.on_log(f"Completed: {name}")
        self.stats["completed"] += 1

    def complete_heartbeat(self, quest, task_type):
        qid, name = quest["id"], get_quest_name(quest)
        need, done = get_seconds_needed(quest), get_seconds_done(quest)
        stream_key = f"call:0:{random.randint(1000, 30000)}" if task_type != "PLAY_ACTIVITY" else "call:0:1"
        while done < need and not self._stopped():
            try:
                r = self.api.post(f"/quests/{qid}/heartbeat",
                                  {"stream_key": stream_key, "terminal": False})
                if r.status_code == 200:
                    body = r.json()
                    prog = body.get("progress", {})
                    if task_type in prog:
                        done = prog[task_type].get("value", done)
                    self.on_log(f"[{task_type}] {name} {done:.0f}/{need}s")
                    if body.get("completed_at") or done >= need:
                        break
                elif r.status_code == 429:
                    time.sleep(r.json().get("retry_after", 10) + 1)
                    continue
            except Exception as e:
                self.on_log(f"Heartbeat err: {e}")
            time.sleep(HEARTBEAT_INTERVAL)
        try:
            self.api.post(f"/quests/{qid}/heartbeat",
                          {"stream_key": stream_key, "terminal": True})
        except Exception:
            pass
        self.on_log(f"Completed: {name}")
        self.stats["completed"] += 1

    def process(self, quest):
        qid, name, ttype = quest.get("id"), get_quest_name(quest), get_task_type(quest)
        if not ttype or qid in self.completed_ids:
            return
        try:
            if ttype in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"):
                self.complete_video(quest)
            elif ttype in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY"):
                self.complete_heartbeat(quest, ttype)
            self.completed_ids.add(qid)
        except Exception as e:
            self.stats["errors"] += 1
            self.on_log(f"Process err {name}: {e}")
            if DEBUG:
                traceback.print_exc()

    def run(self):
        """One full pass — fetch, accept, complete."""
        quests = self.fetch_quests()
        self.on_log(f"Fetched {len(quests)} quest(s)")

        # auto-accept
        unaccepted = [q for q in quests
                      if not is_enrolled(q) and not is_completed(q) and is_completable(q)]
        for q in unaccepted:
            if self._stopped():
                return
            self.enroll(q)
            time.sleep(3)
        if unaccepted:
            time.sleep(2)
            quests = self.fetch_quests()

        # complete
        for q in quests:
            if self._stopped():
                return
            if is_completed(q) or q.get("id") in self.completed_ids:
                continue
            if not is_enrolled(q) or not is_completable(q):
                continue
            self.process(q)
