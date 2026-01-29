import discord
from discord.ext import commands
from discord import app_commands
from aiohttp import web
import aiohttp
import asyncio
import os
from datetime import datetime, timedelta
from collections import defaultdict
import logging

# ===== LOGGING SETUP =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('discord_bot')
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('discord.http').setLevel(logging.INFO)

# ===== ENVIRONMENT VALIDATION =====
REQUIRED_ENV_VARS = ['BOT_TOKEN', 'CLIENT_ID', 'CLIENT_SECRET', 'REDIRECT_URI']
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]

if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    exit(1)

# ===== CONFIGURATION =====
BOT_TOKEN = os.getenv('BOT_TOKEN')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
VERIFIED_ROLE_NAME = os.getenv('VERIFIED_ROLE_NAME', 'Verified')
PORT = int(os.getenv('PORT', 8080))

# Anti-raid settings
RAID_THRESHOLD = 10  # Number of joins
RAID_TIMEFRAME = 60  # Seconds

# ===== BOT SETUP =====
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    max_messages=100,
    chunk_guilds_at_startup=False
)

# ===== DATA STORAGE =====
pending_verifications = {}
user_joins = defaultdict(list)
bot_ready = False

# ===== BACKGROUND TASKS =====
async def cleanup_pending_verifications():
    """Remove expired verification sessions"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(300)  # Every 5 minutes
            expired = []
            current_time = datetime.utcnow()
            
            for user_id, data in list(pending_verifications.items()):
                if 'timestamp' in data:
                    if (current_time - data['timestamp']).total_seconds() > 600:  # 10 min timeout
                        expired.append(user_id)
            
            for user_id in expired:
                del pending_verifications[user_id]
            
            if expired:
                logger.info(f"🧹 Cleaned up {len(expired)} expired verifications")
        except Exception as e:
            logger.error(f"Error in cleanup: {e}")

# ===== ANTI-RAID DETECTION =====
async def check_raid(member):
    """Check if server is being raided"""
    try:
        guild_id = member.guild.id
        current_time = datetime.utcnow()
        
        # Add join time
        user_joins[guild_id].append(current_time)
        
        # Remove old join times
        user_joins[guild_id] = [
            join_time for join_time in user_joins[guild_id]
            if (current_time - join_time).total_seconds() < RAID_TIMEFRAME
        ]
        
        # Check if threshold exceeded
        return len(user_joins[guild_id]) >= RAID_THRESHOLD
    except Exception as e:
        logger.error(f"Error in raid check: {e}")
        return False

# ===== BOT EVENTS =====
@bot.event
async def on_member_join(member):
    """Handle new member joins and detect raids"""
    try:
        is_raid = await check_raid(member)
        
        if is_raid:
            logger.warning(f"🚨 RAID DETECTED in {member.guild.name}!")
            
            # Get or create verified role
            verified_role = discord.utils.get(member.guild.roles, name=VERIFIED_ROLE_NAME)
            if not verified_role:
                verified_role = await member.guild.create_role(
                    name=VERIFIED_ROLE_NAME,
                    color=discord.Color.green(),
                    reason="Raid protection"
                )
                logger.info(f"✅ Created {VERIFIED_ROLE_NAME} role")
            
            # Lock down text channels
            locked = 0
            for channel in member.guild.channels:
                if isinstance(channel, discord.TextChannel):
                    try:
                        # Prevent @everyone from sending messages
                        await channel.set_permissions(
                            member.guild.default_role,
                            send_messages=False,
                            reason="RAID PROTECTION: Auto-lockdown"
                        )
                        # Allow verified users to send messages
                        await channel.set_permissions(
                            verified_role,
                            send_messages=True,
                            reason="RAID PROTECTION: Verified users can still chat"
                        )
                        locked += 1
                    except Exception as e:
                        logger.error(f"Failed to lock {channel.name}: {e}")
            
            logger.info(f"🔒 Locked {locked} channels due to raid")
            
            # Try to notify server owner
            try:
                owner = member.guild.owner
                if owner:
                    await owner.send(
                        f"🚨 **RAID DETECTED** in {member.guild.name}!\n\n"
                        f"Locked {locked} channels automatically.\n"
                        f"Only users with `{VERIFIED_ROLE_NAME}` role can chat.\n"
                        f"Use `/unlock` to restore normal permissions."
                    )
            except:
                pass
        
        # Log new account joins (suspicious)
        account_age = (datetime.utcnow() - member.created_at).days
        if account_age < 7:
            logger.warning(f"⚠️  New account joined: {member} (created {account_age} days ago)")
            
    except Exception as e:
        logger.error(f"Error in on_member_join: {e}")

# ===== VERIFICATION SYSTEM =====
class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label='✅ Verify', style=discord.ButtonStyle.primary, custom_id='verify_btn')
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            verified_role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
            
            # Check if already verified
            if verified_role and verified_role in interaction.user.roles:
                await interaction.response.send_message(
                    "✅ You're already verified!",
                    ephemeral=True
                )
                return
            
            # Generate OAuth URL
            oauth_url = (
                f"https://discord.com/oauth2/authorize?"
                f"client_id={CLIENT_ID}"
                f"&redirect_uri={REDIRECT_URI}"
                f"&response_type=code"
                f"&scope=identify%20email"
                f"&prompt=none"
            )
            
            # Store pending verification
            pending_verifications[interaction.user.id] = {
                'guild_id': interaction.guild.id,
                'user': interaction.user,
                'timestamp': datetime.utcnow()
            }
            
            # Send verification link
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label='🔗 Click to Verify',
                url=oauth_url,
                style=discord.ButtonStyle.link
            ))
            
            await interaction.response.send_message(
                "👋 Click the button below to verify your account:",
                view=view,
                ephemeral=True
            )
            logger.info(f"Verification initiated for {interaction.user}")
            
        except Exception as e:
            logger.error(f"Verify button error: {e}")
            await interaction.response.send_message(
                "❌ Error starting verification. Contact an admin.",
                ephemeral=True
            )

# ===== COMMANDS =====
@bot.tree.command(name='ping', description='Check if bot is online')
async def ping(interaction: discord.Interaction):
    """Simple ping command"""
    await interaction.response.send_message(
        f'🏓 Pong! Latency: {round(bot.latency * 1000)}ms'
    )

@bot.tree.command(name='setup', description='Setup verification button (Admin only)')
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    """Setup verification message in current channel"""
    try:
        embed = discord.Embed(
            title="🔐 Server Verification",
            description=(
                "To access this server, please verify your account.\n\n"
                "Click the button below to get started."
            ),
            color=discord.Color.blue()
        )
        embed.set_footer(text="This helps keep our server safe from bots and raids")
        
        await interaction.channel.send(embed=embed, view=VerifyButton())
        await interaction.response.send_message(
            "✅ Verification message posted!",
            ephemeral=True
        )
        logger.info(f"Verification setup by {interaction.user} in {interaction.channel}")
        
    except Exception as e:
        logger.error(f"Setup error: {e}")
        await interaction.response.send_message(
            "❌ Error setting up verification.",
            ephemeral=True
        )

@bot.tree.command(name='lockdown', description='Lock all channels (Admin only)')
@app_commands.default_permissions(administrator=True)
async def lockdown(interaction: discord.Interaction):
    """Manually lock down all channels"""
    try:
        await interaction.response.defer()
        
        locked = 0
        for channel in interaction.guild.channels:
            if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                try:
                    await channel.set_permissions(
                        interaction.guild.default_role,
                        send_messages=False,
                        reason=f"Manual lockdown by {interaction.user}"
                    )
                    locked += 1
                except Exception as e:
                    logger.error(f"Failed to lock {channel.name}: {e}")
        
        await interaction.followup.send(f"🔒 **Lockdown complete!** Locked {locked} channels.")
        logger.info(f"Manual lockdown by {interaction.user} - {locked} channels locked")
        
    except Exception as e:
        logger.error(f"Lockdown error: {e}")
        await interaction.followup.send("❌ Error during lockdown.")

@bot.tree.command(name='unlock', description='Unlock all channels (Admin only)')
@app_commands.default_permissions(administrator=True)
async def unlock(interaction: discord.Interaction):
    """Unlock all channels after lockdown"""
    try:
        await interaction.response.defer()
        
        unlocked = 0
        for channel in interaction.guild.channels:
            if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                try:
                    await channel.set_permissions(
                        interaction.guild.default_role,
                        send_messages=None,  # Reset to default
                        reason=f"Unlock by {interaction.user}"
                    )
                    unlocked += 1
                except Exception as e:
                    logger.error(f"Failed to unlock {channel.name}: {e}")
        
        await interaction.followup.send(f"🔓 **Unlock complete!** Unlocked {unlocked} channels.")
        logger.info(f"Unlock by {interaction.user} - {unlocked} channels unlocked")
        
    except Exception as e:
        logger.error(f"Unlock error: {e}")
        await interaction.followup.send("❌ Error during unlock.")

# ===== WEB SERVER (REQUIRED FOR RENDER) =====
async def handle_health(request):
    """Health check endpoint"""
    status = "ready" if bot_ready else "starting"
    guilds = len(bot.guilds) if bot_ready else 0
    
    return web.json_response({
        'status': status,
        'guilds': guilds,
        'latency': round(bot.latency * 1000) if bot_ready else 0
    })

async def handle_callback(request):
    """OAuth callback for verification"""
    code = request.query.get('code')
    if not code:
        return web.Response(
            text='<h1>Error</h1><p>No authorization code provided.</p>',
            content_type='text/html',
            status=400
        )
    
    try:
        # Exchange code for token
        async with aiohttp.ClientSession() as session:
            data = {
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': REDIRECT_URI
            }
            
            async with session.post(
                'https://discord.com/api/oauth2/token',
                data=data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            ) as resp:
                token_data = await resp.json()
            
            if 'access_token' not in token_data:
                raise Exception("Failed to get access token")
            
            # Get user info
            async with session.get(
                'https://discord.com/api/users/@me',
                headers={'Authorization': f"Bearer {token_data['access_token']}"}
            ) as resp:
                user_data = await resp.json()
        
        user_id = int(user_data['id'])
        username = user_data.get('username', 'Unknown')
        email = user_data.get('email', 'No email')
        
        # Check if verification is pending
        if user_id not in pending_verifications:
            return web.Response(
                text='<h1>Session Expired</h1><p>Please click the verify button again.</p>',
                content_type='text/html',
                status=400
            )
        
        # Get guild and member
        guild = bot.get_guild(pending_verifications[user_id]['guild_id'])
        if not guild:
            return web.Response(text='<h1>Error</h1><p>Server not found.</p>', content_type='text/html', status=404)
        
        member = guild.get_member(user_id)
        if not member:
            return web.Response(text='<h1>Error</h1><p>You are not in the server.</p>', content_type='text/html', status=404)
        
        # Get or create verified role
        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if not verified_role:
            verified_role = await guild.create_role(
                name=VERIFIED_ROLE_NAME,
                color=discord.Color.green(),
                reason='Verification system'
            )
        
        # Check if already verified
        if verified_role in member.roles:
            return web.Response(
                text=f'''
                <html>
                    <head>
                        <style>
                            body {{ font-family: Arial; text-align: center; padding: 50px; background: #2f3136; color: white; }}
                            h1 {{ color: #57F287; }}
                        </style>
                    </head>
                    <body>
                        <h1>✅ Already Verified</h1>
                        <p>You already have the verified role, <strong>{username}</strong>.</p>
                        <p>You can close this window.</p>
                    </body>
                </html>
                ''',
                content_type='text/html'
            )
        
        # Give verified role
        await member.add_roles(verified_role)
        
        # Send webhook notification (if configured)
        if WEBHOOK_URL and WEBHOOK_URL != 'YOUR_WEBHOOK_URL_HERE':
            try:
                masked_email = email[:2] + '***' if len(email) > 2 else '***'
                
                webhook_data = {
                    "embeds": [{
                        "title": "✅ New Verification",
                        "color": 0x57F287,
                        "fields": [
                            {"name": "User", "value": f"{username} (<@{user_id}>)", "inline": True},
                            {"name": "User ID", "value": str(user_id), "inline": True},
                            {"name": "Email", "value": masked_email, "inline": False},
                            {"name": "Server", "value": guild.name, "inline": True}
                        ],
                        "timestamp": datetime.utcnow().isoformat()
                    }]
                }
                
                async with aiohttp.ClientSession() as webhook_session:
                    await webhook_session.post(WEBHOOK_URL, json=webhook_data)
            except Exception as e:
                logger.error(f"Webhook error: {e}")
        
        # Cleanup
        del pending_verifications[user_id]
        logger.info(f"✅ Verified: {username} ({user_id})")
        
        # Success page
        return web.Response(
            text=f'''
            <html>
                <head>
                    <style>
                        body {{ font-family: Arial; text-align: center; padding: 50px; background: #2f3136; color: white; }}
                        h1 {{ color: #57F287; }}
                        .success {{ font-size: 80px; }}
                    </style>
                </head>
                <body>
                    <div class="success">✅</div>
                    <h1>Verification Successful!</h1>
                    <p>Welcome, <strong>{username}</strong>!</p>
                    <p>You now have access to the server.</p>
                    <p style="color: #888; margin-top: 40px;">You can close this window.</p>
                </body>
            </html>
            ''',
            content_type='text/html'
        )
        
    except Exception as e:
        logger.error(f"Callback error: {e}")
        return web.Response(
            text=f'<h1>Error</h1><p>{str(e)}</p>',
            content_type='text/html',
            status=500
        )

async def start_web_server():
    """Start web server - REQUIRED for Render"""
    app = web.Application()
    
    # Routes
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/callback', handle_callback)
    
    # Start server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f'🌐 Web server started on port {PORT}')
    logger.info(f'✅ Health: http://0.0.0.0:{PORT}/health')
    logger.info('✅ Render will detect this as healthy!')

# ===== BOT READY EVENT =====
@bot.event
async def on_ready():
    global bot_ready
    logger.info(f'🤖 Logged in as {bot.user}')
    logger.info(f'📊 Connected to {len(bot.guilds)} guilds')
    
    # Add persistent view
    bot.add_view(VerifyButton())
    
    # Start cleanup task
    bot.loop.create_task(cleanup_pending_verifications())
    
    # Sync commands with retry
    for attempt in range(3):
        try:
            synced = await bot.tree.sync()
            logger.info(f'✅ Synced {len(synced)} commands')
            break
        except discord.HTTPException as e:
            if e.status == 429:
                wait = 30 * (attempt + 1)
                logger.warning(f'Rate limited syncing, waiting {wait}s')
                await asyncio.sleep(wait)
            else:
                logger.error(f'Sync error: {e}')
                break
    
    bot_ready = True
    logger.info('✅ Bot is fully ready!')
    logger.info('🔐 Verification system active')
    logger.info('🛡️  Anti-raid protection enabled')

# ===== MAIN FUNCTION =====
async def main():
    """Main function with rate limit handling"""
    
    # Start web server FIRST
    logger.info('🚀 Starting web server...')
    await start_web_server()
    logger.info('✅ Web server running!')
    
    # Connect bot with exponential backoff
    max_retries = 10
    for retry in range(max_retries):
        try:
            if retry > 0:
                wait = min(60 * (2 ** retry), 1800)  # Max 30 min
                logger.info(f'⏳ Waiting {wait}s before retry {retry + 1}/{max_retries}')
                logger.info(f'⏰ Will retry at: {datetime.now() + timedelta(seconds=wait)}')
                await asyncio.sleep(wait)
            
            logger.info(f'🔌 Connecting to Discord (attempt {retry + 1}/{max_retries})...')
            await bot.start(BOT_TOKEN)
            
        except discord.LoginFailure:
            logger.error('❌ Invalid token! Check BOT_TOKEN')
            break
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = float(e.response.headers.get('Retry-After', 60))
                logger.error(f'❌ Rate limited! Discord says retry after {retry_after}s')
                await asyncio.sleep(retry_after + 60)
            else:
                logger.error(f'HTTP error: {e}')
                await asyncio.sleep(30)
        except Exception as e:
            logger.error(f'Error: {e}')
            await asyncio.sleep(30)
    
    logger.error('❌ Max retries reached')
    logger.info('⚠️  Web server still running for health checks')
    
    # Keep alive
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('⛔ Stopped by user')
