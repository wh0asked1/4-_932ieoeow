"""Discord HTTP client — quest + user endpoints for the self-token."""
import base64
import json
import re
import time
from typing import Optional

import requests

from config import API_BASE


def fetch_latest_build_number() -> int:
    FALLBACK = 504649
    try:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        )
        r = requests.get("https://discord.com/app",
                         headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200:
            return FALLBACK
        scripts = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
        if not scripts:
            return FALLBACK
        for asset_hash in scripts[-5:]:
            try:
                ar = requests.get(
                    f"https://discord.com/assets/{asset_hash}.js",
                    headers={"User-Agent": ua}, timeout=15,
                )
                m = re.search(r'buildNumber["\s:]+["\s]*(\d{5,7})', ar.text)
                if m:
                    return int(m.group(1))
            except Exception:
                continue
        return FALLBACK
    except Exception:
        return FALLBACK


def make_super_properties(build_number: int) -> str:
    obj = {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": "1.0.9175",
        "os_version": "10.0.26100",
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "browser_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "discord/1.0.9175 Chrome/128.0.6613.186 "
            "Electron/32.2.7 Safari/537.36"
        ),
        "browser_version": "32.2.7",
        "client_build_number": build_number,
        "native_build_number": 59498,
        "client_event_source": None,
    }
    return base64.b64encode(json.dumps(obj).encode()).decode()


class DiscordAPI:
    def __init__(self, token: str, build_number: int = 504649):
        self.token = token
        self.build_number = build_number
        self.session = requests.Session()
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "discord/1.0.9175 Chrome/128.0.6613.186 "
            "Electron/32.2.7 Safari/537.36"
        )
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": ua,
            "X-Super-Properties": make_super_properties(build_number),
            "X-Discord-Locale": "en-US",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
        })

    def get(self, path: str, **kw) -> requests.Response:
        return self.session.get(f"{API_BASE}{path}", **kw)

    def post(self, path: str, payload: Optional[dict] = None, **kw) -> requests.Response:
        return self.session.post(f"{API_BASE}{path}", json=payload, **kw)

    def validate(self) -> Optional[dict]:
        try:
            r = self.get("/users/@me")
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None
