"""Branded embeds — every card carries the founder."""
import discord

FOUNDER = "Founder @tryacidbacon"
BRAND_COLOR = discord.Color.from_rgb(88, 101, 242)   # blurple
OK_COLOR = discord.Color.from_rgb(87, 242, 135)
WARN_COLOR = discord.Color.from_rgb(254, 231, 92)
ERR_COLOR = discord.Color.from_rgb(237, 66, 69)
MUTED_COLOR = discord.Color.from_rgb(114, 118, 125)


def card(title: str, description: str = "", color: discord.Color = BRAND_COLOR,
         fields: list[tuple[str, str, bool]] | None = None) -> discord.Embed:
    """Build a branded embed with the founder footer baked in."""
    e = discord.Embed(title=title, description=description, color=color,
                      timestamp=discord.utils.utcnow())
    for name, value, inline in (fields or []):
        e.add_field(name=name, value=value, inline=inline)
    e.set_footer(text=f"🏷️ {FOUNDER}")
    return e


def ok(title, desc="", fields=None): return card(f"✅ {title}", desc, OK_COLOR, fields)
def err(title, desc="", fields=None): return card(f"❌ {title}", desc, ERR_COLOR, fields)
def warn(title, desc="", fields=None): return card(f"⚠️ {title}", desc, WARN_COLOR, fields)
def info(title, desc="", fields=None): return card(f"ℹ️ {title}", desc, BRAND_COLOR, fields)
