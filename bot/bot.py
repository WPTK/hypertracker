"""Hyperfixed Flight Tracker — Discord bot.

Deliberately thin: it does NOT touch the database or any flight API. It collects
a flight number (or a comma-separated chain for connections) plus a date from the
invoking user and POSTs to the backend's /api/bot/trips endpoint, which is the
single writer and does all resolution + caching. The bot just echoes the result.

Run:  python -m bot.bot   (from the project root, with the same .env)
"""
import os
import asyncio
import httpx
import discord
from discord import app_commands

# Reuse the app's config so the bot and web app read the same .env.
from app import config

GUILD = discord.Object(id=int(config.DISCORD_GUILD_ID)) if config.DISCORD_GUILD_ID else None


class FlightBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD:
            # Guild-scoped commands register instantly (no global propagation wait).
            self.tree.copy_global_to(guild=GUILD)
            await self.tree.sync(guild=GUILD)
        else:
            await self.tree.sync()


client = FlightBot()


def _parse_chain(text: str):
    """'DL1200, LH1234' -> [{'flight_no': 'DL1200'}, {'flight_no': 'LH1234'}]"""
    items = []
    for part in (text or "").split(","):
        part = part.strip()
        if part:
            items.append({"flight_no": part})
    return items


def _stamp_date(items, date):
    for it in items:
        it["date"] = date
    return items


async def _post_trip(payload: dict) -> dict:
    url = config.BACKEND_URL.rstrip("/") + "/api/bot/trips"
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(url, json=payload, headers={"X-Bot-Token": config.BOT_SHARED_TOKEN})
        r.raise_for_status()
        return r.json()


@client.tree.command(name="flight", description="Add a trip to the board (use the website for complex itineraries).")
@app_commands.describe(
    outbound="Outbound flight number(s). Comma-separate connections, e.g. 'LH455, LH1234'.",
    date="Departure date (YYYY-MM-DD), local to the departure airport.",
    return_flights="Optional return flight number(s), comma-separated for connections.",
    return_date="Optional return date (YYYY-MM-DD).",
)
async def flight(
    interaction: discord.Interaction,
    outbound: str,
    date: str,
    return_flights: str | None = None,
    return_date: str | None = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    payload = {
        "user": {"id": str(interaction.user.id),
                 "name": interaction.user.display_name or interaction.user.name},
        "out": _stamp_date(_parse_chain(outbound), date),
        "ret": _stamp_date(_parse_chain(return_flights), return_date) if (return_flights and return_date) else [],
    }

    try:
        result = await _post_trip(payload)
    except Exception as e:
        await interaction.followup.send(f"Couldn't save that trip ({e}). Try the website?", ephemeral=True)
        return

    lines = []
    for leg in result.get("legs", []):
        route = f"{leg.get('from') or '???'} → {leg.get('to') or '???'}"
        ac = leg.get("ac_model") or ""
        age = f" · {leg['ac_age']} yrs" if leg.get("ac_age") is not None else ""
        flag = "" if leg.get("resolved") else "  (unresolved — edit on the site)"
        lines.append(f"`{leg.get('flight_no','')}`  {route}" + (f"  ·  {ac}{age}" if ac else "") + flag)

    body = "**Added to the board.**\n" + "\n".join(lines) if lines else "Added to the board."
    body += f"\n\n{config.BASE_URL}/"
    await interaction.followup.send(body, ephemeral=True)


def main():
    token = config.DISCORD_BOT_TOKEN
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set — see .env.example")
    client.run(token)


if __name__ == "__main__":
    main()
