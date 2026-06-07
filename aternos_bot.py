"""
Aternos Discord Bot — with automatic ad/queue handling via Selenium
Commands:
  /start  <server>  — starts the named Aternos server, handles ads/queue automatically
  /status <server>  — shows current status, software, version, player count

Requirements:
  pip install discord.py selenium webdriver-manager

Setup:
  1. Install Google Chrome (or Chromium) on the host machine
  2. Set env vars:  DISCORD_TOKEN  ATERNOS_USERNAME  ATERNOS_PASSWORD
  3. Run:  python aternos_bot.py
"""

import os
import asyncio
import logging
from contextlib import contextmanager

import discord
from discord import app_commands
from discord.ext import commands

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("aternos-bot")

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ATERNOS_USERNAME = os.getenv("ATERNOS_USERNAME", "Aternos_bot00001")
ATERNOS_PASSWORD = os.getenv("ATERNOS_PASSWORD", "ainmeezan")

ATERNOS_BASE     = "https://aternos.org"
LOGIN_URL        = f"{ATERNOS_BASE}/go/"
SERVERS_URL      = f"{ATERNOS_BASE}/servers/"

# How long (seconds) to wait for page elements before giving up
DEFAULT_WAIT     = 20
# How long to poll for the server to go online after start (seconds)
ONLINE_TIMEOUT   = 300
POLL_INTERVAL    = 10
# ─────────────────────────────────────────────────────────────────────────────


# ── Selenium driver factory ───────────────────────────────────────────────────
def _make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    # Suppress most console noise from Chrome
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


@contextmanager
def driver_session():
    """Context manager: create a driver, yield it, always quit."""
    driver = _make_driver()
    try:
        yield driver
    finally:
        driver.quit()
# ─────────────────────────────────────────────────────────────────────────────


# ── Aternos browser automation ────────────────────────────────────────────────
def _login(driver: webdriver.Chrome):
    """Log in to Aternos. Raises RuntimeError on failure."""
    log.info("Navigating to login page …")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, DEFAULT_WAIT)

    # Accept cookie banner if present
    try:
        cookie_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button.btn-confirm, #accept-choices"))
        )
        cookie_btn.click()
        log.info("Dismissed cookie banner.")
    except TimeoutException:
        pass  # No banner — that's fine

    # Fill username & password
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='username']")))
    driver.find_element(By.CSS_SELECTOR, "input[name='username']").send_keys(ATERNOS_USERNAME)
    driver.find_element(By.CSS_SELECTOR, "input[name='password']").send_keys(ATERNOS_PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']").click()

    # Wait until redirected away from login
    try:
        wait.until(EC.url_changes(LOGIN_URL))
    except TimeoutException:
        raise RuntimeError("Login failed — check your Aternos username/password.")

    log.info("Logged in successfully.")


def _get_server_list(driver: webdriver.Chrome) -> list[dict]:
    """Return a list of dicts with keys: name, url, status."""
    driver.get(SERVERS_URL)
    wait = WebDriverWait(driver, DEFAULT_WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".server")))

    servers = []
    for card in driver.find_elements(By.CSS_SELECTOR, ".server"):
        try:
            name = card.find_element(By.CSS_SELECTOR, ".server-name").text.strip()
            href = card.get_attribute("href") or card.find_element(By.TAG_NAME, "a").get_attribute("href")
            try:
                status = card.find_element(By.CSS_SELECTOR, ".server-status").text.strip().lower()
            except NoSuchElementException:
                status = "unknown"
            servers.append({"name": name, "url": href, "status": status})
        except Exception as e:
            log.warning(f"Skipping a server card due to: {e}")
    return servers


def _find_server(servers: list[dict], fragment: str) -> dict | None:
    fragment = fragment.lower()
    for s in servers:
        if fragment in s["name"].lower():
            return s
    return None


def _handle_ads_and_confirm(driver: webdriver.Chrome, wait: WebDriverWait):
    """
    Aternos sometimes shows:
      1. An ad overlay that must be watched/waited through
      2. A 'Confirm start' button after the ad
    This function handles both automatically.
    """
    # ── Step 1: Wait for and dismiss ad overlay ──────────────────────────────
    ad_selectors = [
        # Aternos own ad-overlay confirm button (appears after timer)
        ".btn-start-ad",
        "#start-server-ad-button",
        # Generic 'confirm start' that appears after ad
        ".btn-orange.btn-start",
        # Sometimes a skip/close button appears
        ".skip-ad-btn",
    ]

    log.info("Checking for ad overlay …")
    ad_found = False
    for sel in ad_selectors:
        try:
            btn = WebDriverWait(driver, 30).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            log.info(f"Ad/confirm button found ({sel}), clicking …")
            driver.execute_script("arguments[0].click();", btn)
            ad_found = True
            break
        except TimeoutException:
            continue

    if not ad_found:
        log.info("No ad overlay detected — proceeding directly.")

    # ── Step 2: Look for a final 'Start' / 'Confirm' button ──────────────────
    confirm_selectors = [
        ".btn-start-server",
        "#start-btn",
        "button.btn-success[data-id]",
        ".server-start",
    ]
    for sel in confirm_selectors:
        try:
            btn = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            log.info(f"Confirm-start button found ({sel}), clicking …")
            driver.execute_script("arguments[0].click();", btn)
            break
        except TimeoutException:
            continue

    log.info("Start sequence complete.")


def _get_status_on_page(driver: webdriver.Chrome) -> str:
    """Read the current status text from the server page."""
    try:
        el = driver.find_element(By.CSS_SELECTOR, ".status-label, .server-status, #status")
        return el.text.strip().lower()
    except NoSuchElementException:
        return "unknown"


# ── High-level tasks run in a thread pool (keeps Discord async loop happy) ────
def _task_start_server(server_fragment: str) -> dict:
    """
    Synchronous task: log in, find server, start it, handle ads, return result dict.
    Keys: ok (bool), server_name (str), message (str)
    """
    with driver_session() as driver:
        try:
            _login(driver)
            servers = _get_server_list(driver)
            srv = _find_server(servers, server_fragment)

            if srv is None:
                return {"ok": False, "server_name": server_fragment,
                        "message": f"No server found matching **{server_fragment}**."}

            name   = srv["name"]
            status = srv["status"]

            if status in ("online", "starting", "loading", "preparing"):
                return {"ok": True, "server_name": name,
                        "message": f"⚠️ **{name}** is already `{status}` — no action taken."}

            # Navigate to the server's page
            log.info(f"Opening server page: {srv['url']}")
            driver.get(srv["url"])
            wait = WebDriverWait(driver, DEFAULT_WAIT)

            # Click the initial Start button
            start_btn_selectors = [
                ".btn-start-server",
                "#start-btn",
                "button.btn-success",
                ".server-start",
                "[data-id='start']",
            ]
            clicked_start = False
            for sel in start_btn_selectors:
                try:
                    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                    log.info(f"Clicking start button ({sel}) …")
                    driver.execute_script("arguments[0].click();", btn)
                    clicked_start = True
                    break
                except TimeoutException:
                    continue

            if not clicked_start:
                return {"ok": False, "server_name": name,
                        "message": f"❌ Could not find a Start button for **{name}**. "
                                   "The Aternos UI may have changed."}

            # Handle any ad / confirm flow
            _handle_ads_and_confirm(driver, wait)

            return {
                "ok": True,
                "server_name": name,
                "message": (
                    f"🚀 **{name}** is starting up!\n"
                    f"Address: `{name.lower()}.aternos.me`\n"
                    f"Ads and confirmations were handled automatically.\n"
                    f"Use `/status server:{server_fragment}` to check progress."
                ),
            }

        except RuntimeError as e:
            return {"ok": False, "server_name": server_fragment, "message": f"❌ {e}"}
        except Exception as e:
            log.exception("Unexpected error in _task_start_server")
            return {"ok": False, "server_name": server_fragment, "message": f"❌ Unexpected error: {e}"}


def _task_get_status(server_fragment: str) -> dict:
    """
    Synchronous task: log in, find server, return its live status.
    """
    with driver_session() as driver:
        try:
            _login(driver)
            servers = _get_server_list(driver)
            srv = _find_server(servers, server_fragment)

            if srv is None:
                return {"ok": False, "server_name": server_fragment,
                        "message": f"No server found matching **{server_fragment}**."}

            name = srv["name"]

            # Open the server page for richer status info
            driver.get(srv["url"])
            WebDriverWait(driver, DEFAULT_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".status-label, .server-status, #status"))
            )

            status = _get_status_on_page(driver)

            # Try to grab extra info
            def _text(sel):
                try:
                    return driver.find_element(By.CSS_SELECTOR, sel).text.strip()
                except NoSuchElementException:
                    return "Unknown"

            software = _text(".software-version, .server-software")
            version  = _text(".version-label, .server-version")
            players  = _text(".player-count, #players")

            return {
                "ok": True,
                "server_name": name,
                "status":   status,
                "software": software,
                "version":  version,
                "players":  players,
            }

        except RuntimeError as e:
            return {"ok": False, "server_name": server_fragment, "message": f"❌ {e}"}
        except Exception as e:
            log.exception("Unexpected error in _task_get_status")
            return {"ok": False, "server_name": server_fragment, "message": f"❌ Unexpected error: {e}"}


# ── Discord bot ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

STATUS_EMOJI = {
    "online":    "🟢",
    "offline":   "🔴",
    "starting":  "🟡",
    "stopping":  "🟠",
    "loading":   "🔵",
    "preparing": "🔵",
    "saving":    "🟠",
    "unknown":   "⚪",
}


@bot.event
async def on_ready():
    await tree.sync()
    log.info(f"✅ Logged in as {bot.user} — slash commands synced.")
    print(f"✅ Bot ready: {bot.user}")


@tree.command(name="start", description="Start an Aternos server (handles ads automatically)")
@app_commands.describe(server="Part of the server name to identify it")
async def cmd_start(interaction: discord.Interaction, server: str):
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(
        f"⏳ Logging in to Aternos and starting **{server}** … "
        f"(ads will be handled automatically — this may take ~30 s)"
    )

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _task_start_server, server)

    color = discord.Color.green() if result["ok"] else discord.Color.red()
    embed = discord.Embed(description=result["message"], color=color)
    embed.set_footer(text="Aternos Bot • ad-aware auto-start")
    await interaction.followup.send(embed=embed)


@tree.command(name="status", description="Check the live status of an Aternos server")
@app_commands.describe(server="Part of the server name to identify it")
async def cmd_status(interaction: discord.Interaction, server: str):
    await interaction.response.defer(thinking=True)

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _task_get_status, server)

    if not result["ok"]:
        await interaction.followup.send(embed=discord.Embed(
            description=result["message"], color=discord.Color.red()
        ))
        return

    status = result.get("status", "unknown")
    emoji  = STATUS_EMOJI.get(status, "⚪")
    color  = discord.Color.green() if status == "online" else discord.Color.orange()

    embed = discord.Embed(
        title=f"{emoji} {result['server_name']}",
        color=color,
    )
    embed.add_field(name="Status",   value=status.capitalize(),       inline=True)
    embed.add_field(name="Software", value=result.get("software", "?"), inline=True)
    embed.add_field(name="Version",  value=result.get("version",  "?"), inline=True)
    if status == "online":
        embed.add_field(name="Players", value=result.get("players", "?"), inline=True)
    embed.set_footer(text="Aternos Bot • live browser check")

    await interaction.followup.send(embed=embed)


bot.run(DISCORD_TOKEN)
