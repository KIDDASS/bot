import discord
from discord.ext import commands
from discord import app_commands
from aiohttp import web
import aiohttp
import asyncio
import os
from datetime import datetime
from collections import defaultdict
import logging

# ===== LOGGING SETUP =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('discord_bot')

# ===== ENVIRONMENT VALIDATION =====
REQUIRED_ENV_VARS = ['BOT_TOKEN', 'CLIENT_ID', 'CLIENT_SECRET', 'REDIRECT_URI']
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]

if missing_vars:
    logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
    exit(1)

# ===== CONFIGURATION =====
BOT_TOKEN = os.getenv('BOT_TOKEN')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
VERIFIED_ROLE_NAME = os.getenv('VERIFIED_ROLE_NAME', 'Verified')
PORT = int(os.getenv('PORT', 8080))

# ===== BOT SETUP =====
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

pending_verifications = {}
user_joins = defaultdict(list)
bot_ready = False

# ===== ANTI-RAID SETTINGS =====
RAID_THRESHOLD = 10
RAID_TIMEFRAME = 60

async def check_raid(member):
    guild_id = member.guild.id
    now = datetime.utcnow()

    user_joins[guild_id].append(now)
    user_joins[guild_id] = [
        t for t in user_joins[guild_id]
        if (now - t).total_seconds() < RAID_TIMEFRAME
    ]

    return len(user_joins[guild_id]) >= RAID_THRESHOLD

# ===== EVENTS =====
@bot.event
async def on_ready():
    global bot_ready
    logger.info(f"🤖 Logged in as {bot.user}")
    bot_ready = True
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Command sync failed: {e}")

@bot.event
async def on_member_join(member):
    if await check_raid(member):
        logger.warning(f"🚨 RAID detected in {member.guild.name}")

# ===== BASIC COMMAND =====
@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Pong! {round(bot.latency*1000)}ms"
    )

# ===== VERIFICATION BUTTON =====
class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):

        oauth_url = (
            f"https://discord.com/oauth2/authorize?"
            f"client_id={CLIENT_ID}"
            f"&redirect_uri={REDIRECT_URI}"
            f"&response_type=code"
            f"&scope=identify%20email"
        )

        pending_verifications[interaction.user.id] = {
            "guild_id": interaction.guild.id,
            "timestamp": datetime.utcnow()
        }

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Verification", url=oauth_url))

        await interaction.response.send_message(
            "Click below to verify:",
            view=view,
            ephemeral=True
        )

# ===== SETUP COMMAND =====
@bot.tree.command(name="setup")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Server Verification",
        description="Click below to verify and gain access."
    )
    await interaction.channel.send(embed=embed, view=VerifyButton())
    await interaction.response.send_message("Verification panel created.", ephemeral=True)

# ===== OAUTH CALLBACK =====
async def handle_callback(request):
    code = request.query.get("code")
    if not code:
        logger.warning("Callback received without code")
        return web.Response(text="No code provided.", status=400)

    try:
        # Use context manager - automatically closes session
        async with aiohttp.ClientSession() as session:
            # Exchange code for token
            data = {
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': REDIRECT_URI
            }
            
            async with session.post("https://discord.com/api/oauth2/token", data=data) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Token exchange failed: {resp.status} - {error_text}")
                    return web.Response(text="OAuth failed. Please try again.", status=400)
                token_data = await resp.json()

            access_token = token_data.get("access_token")
            if not access_token:
                logger.error("No access token in response")
                return web.Response(text="OAuth failed.", status=400)

            async with session.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"}
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"User fetch failed: {resp.status} - {error_text}")
                    return web.Response(text="Failed to fetch user info.", status=400)
                user = await resp.json()

        # Session automatically closed here

        user_id = int(user["id"])
        logger.info(f"Processing verification for user {user_id}")

        if user_id not in pending_verifications:
            logger.warning(f"Verification expired for user {user_id}")
            return web.Response(text="Verification expired or not initiated. Please try again.", status=400)

        guild = bot.get_guild(pending_verifications[user_id]["guild_id"])
        if not guild:
            logger.error(f"Guild not found for user {user_id}")
            return web.Response(text="Server not found.", status=400)
            
        member = guild.get_member(user_id)
        if not member:
            logger.error(f"Member {user_id} not found in guild {guild.id}")
            return web.Response(text="You are not a member of this server.", status=400)

        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if not verified_role:
            logger.info(f"Creating verified role in guild {guild.id}")
            verified_role = await guild.create_role(name=VERIFIED_ROLE_NAME)

        await member.add_roles(verified_role)
        logger.info(f"✅ User {user_id} verified successfully in guild {guild.id}")
        
        del pending_verifications[user_id]

        return web.Response(text="✅ Verification successful! You may close this tab.")
        
    except aiohttp.ClientError as e:
        logger.error(f"HTTP request error during verification: {e}", exc_info=True)
        return web.Response(text="Network error occurred. Please try again.", status=500)
    except discord.errors.Forbidden as e:
        logger.error(f"Permission error: {e}", exc_info=True)
        return web.Response(text="Bot lacks permissions to assign roles.", status=500)
    except Exception as e:
        logger.error(f"Unexpected verification error: {e}", exc_info=True)
        return web.Response(text="An error occurred during verification. Please contact an administrator.", status=500)

# ===== HEALTH CHECK =====
async def handle_health(request):
    return web.json_response({
        "status": "ready" if bot_ready else "starting",
        "guilds": len(bot.guilds),
        "latency": round(bot.latency * 1000) if bot_ready else 0
    })

# ===== WEB SERVER =====
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/callback", handle_callback)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Web server running on port {PORT}")

# ===== MAIN =====
async def main():
    try:
        await start_web_server()
        await bot.start(BOT_TOKEN)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
