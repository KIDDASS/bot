import discord
from discord.ext import commands
from discord import app_commands
from aiohttp import web
import aiohttp
import asyncio
import os
from datetime import datetime, timedelta
from collections import defaultdict
import json
from pathlib import Path

# ===== CONFIGURATION =====
# Using environment variables for Render deployment
BOT_TOKEN = os.getenv('BOT_TOKEN')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
VERIFIED_ROLE_NAME = os.getenv('VERIFIED_ROLE_NAME', 'Verified')

# Debug: Check if token is being read (remove after testing)
print(f"BOT_TOKEN exists: {BOT_TOKEN is not None}")
print(f"BOT_TOKEN length: {len(BOT_TOKEN) if BOT_TOKEN else 0}")
if BOT_TOKEN:
    print(f"BOT_TOKEN starts with: {BOT_TOKEN[:10]}...")
else:
    print("ERROR: BOT_TOKEN is None or empty!")

# Security settings
SPAM_THRESHOLD = 5
SPAM_TIMEFRAME = 5
RAID_THRESHOLD = 10
RAID_TIMEFRAME = 60
MENTION_SPAM_LIMIT = 5

# ===== BOT SETUP =====
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Store pending verifications
pending_verifications = {}

# Security tracking
user_messages = defaultdict(list)
user_joins = defaultdict(list)
warned_users = defaultdict(int)

# Auto-mod settings per guild
automod_settings = defaultdict(lambda: {
    'anti_spam': True,
    'anti_raid': True,
    'anti_mention_spam': True,
    'anti_invite': True,
    'log_channel': None
})
vc_data_file = Path('vc_data.json')
vc_tracking = defaultdict(lambda: {
    'total_time': 0,  # in seconds
    'current_session_start': None,
    'username': None,
    'avatar': None
})

# Load existing VC data
def load_vc_data():
    global vc_tracking
    if vc_data_file.exists():
        try:
            with open(vc_data_file, 'r') as f:
                data = json.load(f)
                for user_id, user_data in data.items():
                    vc_tracking[int(user_id)] = user_data
            print(f"Loaded VC data for {len(vc_tracking)} users")
        except Exception as e:
            print(f"Error loading VC data: {e}")

def save_vc_data():
    try:
        with open(vc_data_file, 'w') as f:
            json.dump(dict(vc_tracking), f, indent=2)
    except Exception as e:
        print(f"Error saving VC data: {e}")

# Auto-save every 5 minutes
async def auto_save_vc_data():
    while True:
        await asyncio.sleep(300)  # 5 minutes
        save_vc_data()
        print("VC data auto-saved")


# ===== VC EVENT HANDLERS =====
@bot.event
async def on_voice_state_update(member, before, after):
    """Track voice channel join/leave times"""
    if member.bot:
        return
    
    user_id = member.id
    current_time = datetime.utcnow()
    
    # User joined a VC
    if before.channel is None and after.channel is not None:
        vc_tracking[user_id]['current_session_start'] = current_time.isoformat()
        vc_tracking[user_id]['username'] = str(member)
        vc_tracking[user_id]['avatar'] = str(member.display_avatar.url)
        print(f"{member} joined VC")
    
    # User left a VC
    elif before.channel is not None and after.channel is None:
        if vc_tracking[user_id]['current_session_start']:
            start_time = datetime.fromisoformat(vc_tracking[user_id]['current_session_start'])
            session_duration = (current_time - start_time).total_seconds()
            vc_tracking[user_id]['total_time'] += session_duration
            vc_tracking[user_id]['current_session_start'] = None
            save_vc_data()
            print(f"{member} left VC after {session_duration/60:.1f} minutes")
    
    # User switched channels (end old session, start new)
    elif before.channel != after.channel and before.channel is not None and after.channel is not None:
        if vc_tracking[user_id]['current_session_start']:
            start_time = datetime.fromisoformat(vc_tracking[user_id]['current_session_start'])
            session_duration = (current_time - start_time).total_seconds()
            vc_tracking[user_id]['total_time'] += session_duration
        
        vc_tracking[user_id]['current_session_start'] = current_time.isoformat()
        vc_tracking[user_id]['username'] = str(member)
        vc_tracking[user_id]['avatar'] = str(member.display_avatar.url)
        save_vc_data()


# ===== VC COMMANDS =====
@bot.tree.command(name='vcstats', description='View your voice channel statistics')
async def vcstats(interaction: discord.Interaction, user: discord.User = None):
    """Show VC stats for a user"""
    target_user = user or interaction.user
    user_id = target_user.id
    
    if user_id not in vc_tracking or vc_tracking[user_id]['total_time'] == 0:
        await interaction.response.send_message(
            f"{target_user.mention} has no voice channel time recorded yet.",
            ephemeral=True
        )
        return
    
    total_seconds = vc_tracking[user_id]['total_time']
    
    # Add current session time if in VC
    if vc_tracking[user_id]['current_session_start']:
        start_time = datetime.fromisoformat(vc_tracking[user_id]['current_session_start'])
        current_session = (datetime.utcnow() - start_time).total_seconds()
        total_seconds += current_session
    
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    
    embed = discord.Embed(
        title=f"🎙️ Voice Channel Statistics",
        description=f"Stats for {target_user.mention}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Total Time", value=f"{hours}h {minutes}m", inline=False)
    
    if vc_tracking[user_id]['current_session_start']:
        embed.add_field(name="Status", value="🟢 Currently in VC", inline=False)
    else:
        embed.add_field(name="Status", value="⚫ Not in VC", inline=False)
    
    embed.set_thumbnail(url=target_user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='vcleaderboard', description='View top 10 voice channel users')
async def vcleaderboard(interaction: discord.Interaction):
    """Show VC leaderboard"""
    if not vc_tracking:
        await interaction.response.send_message("No voice channel data recorded yet.", ephemeral=True)
        return
    
    # Calculate total time including current sessions
    leaderboard_data = []
    for user_id, data in vc_tracking.items():
        total_seconds = data['total_time']
        
        if data['current_session_start']:
            start_time = datetime.fromisoformat(data['current_session_start'])
            current_session = (datetime.utcnow() - start_time).total_seconds()
            total_seconds += current_session
        
        if total_seconds > 0:
            leaderboard_data.append({
                'user_id': user_id,
                'username': data.get('username', 'Unknown'),
                'total_seconds': total_seconds,
                'in_vc': data['current_session_start'] is not None
            })
    
    # Sort by total time
    leaderboard_data.sort(key=lambda x: x['total_seconds'], reverse=True)
    
    embed = discord.Embed(
        title="🏆 Voice Channel Leaderboard",
        description="Top voice channel users",
        color=discord.Color.gold()
    )
    
    medals = ["🥇", "🥈", "🥉"]
    for i, entry in enumerate(leaderboard_data[:10]):
        hours = int(entry['total_seconds'] // 3600)
        minutes = int((entry['total_seconds'] % 3600) // 60)
        
        medal = medals[i] if i < 3 else f"{i+1}."
        status = "🟢" if entry['in_vc'] else "⚫"
        
        embed.add_field(
            name=f"{medal} {entry['username']} {status}",
            value=f"{hours}h {minutes}m",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='resetvcstats', description='Reset all VC statistics (Admin only)')
@app_commands.default_permissions(administrator=True)
async def resetvcstats(interaction: discord.Interaction):
    """Reset all VC stats"""
    global vc_tracking
    vc_tracking.clear()
    save_vc_data()
    await interaction.response.send_message("All VC statistics have been reset.", ephemeral=True)


# ===== WEB API ENDPOINTS FOR LEADERBOARD =====
async def handle_vc_leaderboard(request):
    """API endpoint for leaderboard data"""
    # Calculate total time including current sessions
    leaderboard_data = []
    for user_id, data in vc_tracking.items():
        total_seconds = data['total_time']
        
        if data['current_session_start']:
            start_time = datetime.fromisoformat(data['current_session_start'])
            current_session = (datetime.utcnow() - start_time).total_seconds()
            total_seconds += current_session
        
        if total_seconds > 0:
            leaderboard_data.append({
                'user_id': str(user_id),
                'username': data.get('username', 'Unknown'),
                'avatar': data.get('avatar', ''),
                'total_seconds': total_seconds,
                'in_vc': data['current_session_start'] is not None
            })
    
    # Sort by total time
    leaderboard_data.sort(key=lambda x: x['total_seconds'], reverse=True)
    
    return web.json_response({
        'success': True,
        'leaderboard': leaderboard_data[:50],  # Top 50
        'total_users': len(leaderboard_data)
    })


async def handle_vc_current(request):
    """API endpoint for current VC users"""
    current_users = []
    for user_id, data in vc_tracking.items():
        if data['current_session_start']:
            start_time = datetime.fromisoformat(data['current_session_start'])
            session_duration = (datetime.utcnow() - start_time).total_seconds()
            
            current_users.append({
                'user_id': str(user_id),
                'username': data.get('username', 'Unknown'),
                'avatar': data.get('avatar', ''),
                'session_duration': session_duration
            })
    
    # Sort by session duration
    current_users.sort(key=lambda x: x['session_duration'], reverse=True)
    
    return web.json_response({
        'success': True,
        'current_users': current_users,
        'count': len(current_users)
    })


# MODIFY the start_web_server function to add new routes:
async def start_web_server():
    """Start the web server for OAuth callbacks and API"""
    app = web.Application()
    app.router.add_get('/callback', handle_callback)
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    
    # Add VC tracking API endpoints
    app.router.add_get('/api/leaderboard', handle_vc_leaderboard)
    app.router.add_get('/api/current', handle_vc_current)
    
    # Enable CORS for web access
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    app.middlewares.append(cors_middleware)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f'Web server started on port {port}')


# MODIFY the on_ready event to load VC data and start auto-save:
# Add these lines after bot.add_view(VerifyButton()) in on_ready:
@bot.event
async def on_ready():
    print(f'Bot logged in as {bot.user}')
    
    # Register the persistent view
    bot.add_view(VerifyButton())
    
    # Load VC tracking data
    load_vc_data()
    
    # Start auto-save task
    asyncio.create_task(auto_save_vc_data())
    
    # Start web server for OAuth callback and API
    asyncio.create_task(start_web_server())
    
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} command(s)')
    except Exception as e:
        print(f'Error syncing commands: {e}')

# ===== SECURITY FUNCTIONS =====
async def log_action(guild, action_type, user, moderator, reason, color=discord.Color.orange()):
    """Log moderation actions to the log channel"""
    settings = automod_settings[guild.id]
    if settings['log_channel']:
        channel = guild.get_channel(settings['log_channel'])
        if channel:
            embed = discord.Embed(
                title=action_type,
                description=f"**User:** {user.mention} ({user.id})\n**Moderator:** {moderator}\n**Reason:** {reason}",
                color=color,
                timestamp=datetime.utcnow()
            )
            await channel.send(embed=embed)


async def check_spam(message):
    """Check for spam messages"""
    if not automod_settings[message.guild.id]['anti_spam']:
        return False
    
    user_id = message.author.id
    current_time = datetime.utcnow()
    
    user_messages[user_id].append(current_time)
    
    user_messages[user_id] = [
        msg_time for msg_time in user_messages[user_id]
        if (current_time - msg_time).total_seconds() < SPAM_TIMEFRAME
    ]
    
    if len(user_messages[user_id]) >= SPAM_THRESHOLD:
        return True
    return False


async def check_raid(member):
    """Check for raid (mass joins)"""
    if not automod_settings[member.guild.id]['anti_raid']:
        return False
    
    guild_id = member.guild.id
    current_time = datetime.utcnow()
    
    user_joins[guild_id].append(current_time)
    
    user_joins[guild_id] = [
        join_time for join_time in user_joins[guild_id]
        if (current_time - join_time).total_seconds() < RAID_TIMEFRAME
    ]
    
    if len(user_joins[guild_id]) >= RAID_THRESHOLD:
        return True
    return False


class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label='Verify', style=discord.ButtonStyle.primary, custom_id='verify_btn')
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user already has the verified role
        verified_role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
        
        if verified_role and verified_role in interaction.user.roles:
            # User already has the verified role
            embed = discord.Embed(
                title="Already Verified",
                description="You already have the verified role.",
                color=discord.Color.green()
            )
            embed.set_image(url='https://i.imgur.com/VpMfDQ4.png')
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        # Generate OAuth2 URL with email access - using prompt=none for faster flow
        oauth_url = (
            f"https://discord.com/oauth2/authorize?"
            f"client_id={CLIENT_ID}"
            f"&redirect_uri={REDIRECT_URI}"
            f"&response_type=code"
            f"&scope=identify%20email"
            f"&prompt=none"
        )
        
        # Store user info for callback
        pending_verifications[interaction.user.id] = {
            'guild_id': interaction.guild.id,
            'user': interaction.user
        }
        
        # Direct link button - opens in Discord app on mobile, browser on desktop
        verify_button = discord.ui.Button(
            label='Verify',
            url=oauth_url,
            style=discord.ButtonStyle.link
        )
        
        view = discord.ui.View()
        view.add_item(verify_button)
        
        embed = discord.Embed(color=discord.Color.blue())
        embed.set_image(url='https://i.imgur.com/VpMfDQ4.png')
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.event
async def on_ready():
    print(f'Bot logged in as {bot.user}')
    
    # Register the persistent view
    bot.add_view(VerifyButton())
    
    # Start web server for OAuth callback
    asyncio.create_task(start_web_server())
    
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} command(s)')
    except Exception as e:
        print(f'Error syncing commands: {e}')


@bot.event
async def on_member_join(member):
    """Handle new member joins - check for raids"""
    is_raid = await check_raid(member)
    
    if is_raid:
        try:
            verified_role = discord.utils.get(member.guild.roles, name=VERIFIED_ROLE_NAME)
            if not verified_role:
                verified_role = await member.guild.create_role(
                    name=VERIFIED_ROLE_NAME,
                    color=discord.Color.green()
                )
            
            for channel in member.guild.channels:
                try:
                    await channel.set_permissions(
                        member.guild.default_role,
                        send_messages=False,
                        reason="Raid protection activated"
                    )
                    await channel.set_permissions(
                        verified_role,
                        send_messages=True,
                        reason="Raid protection - verified users"
                    )
                except:
                    pass
            
            await log_action(
                member.guild,
                "RAID DETECTED",
                member,
                "AutoMod",
                f"Server locked down - {RAID_THRESHOLD} joins in {RAID_TIMEFRAME}s",
                discord.Color.red()
            )
        except Exception as e:
            print(f"Error activating raid protection: {e}")
    
    account_age = (datetime.utcnow() - member.created_at).days
    if account_age < 7:
        await log_action(
            member.guild,
            "NEW ACCOUNT",
            member,
            "AutoMod",
            f"Account created {account_age} days ago",
            discord.Color.yellow()
        )


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    if await check_spam(message):
        warned_users[message.author.id] += 1
        
        try:
            await message.delete()
            
            if warned_users[message.author.id] >= 3:
                await message.author.timeout(timedelta(minutes=10), reason="Spam (3 warnings)")
                await log_action(
                    message.guild,
                    "AUTO-TIMEOUT",
                    message.author,
                    "AutoMod",
                    "Spam detected - 10 minute timeout",
                    discord.Color.red()
                )
                warned_users[message.author.id] = 0
            else:
                warning_msg = await message.channel.send(
                    f"{message.author.mention} Please slow down! Warning {warned_users[message.author.id]}/3"
                )
                await asyncio.sleep(5)
                await warning_msg.delete()
        except:
            pass
    
    if automod_settings[message.guild.id]['anti_mention_spam']:
        if len(message.mentions) >= MENTION_SPAM_LIMIT:
            try:
                await message.delete()
                await message.author.timeout(timedelta(minutes=5), reason="Mention spam")
                await log_action(
                    message.guild,
                    "AUTO-TIMEOUT",
                    message.author,
                    "AutoMod",
                    f"Mention spam ({len(message.mentions)} mentions)",
                    discord.Color.red()
                )
            except:
                pass
    
    if automod_settings[message.guild.id]['anti_invite']:
        if 'discord.gg/' in message.content.lower() or 'discord.com/invite/' in message.content.lower():
            if not message.author.guild_permissions.manage_messages:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{message.author.mention} Invite links are not allowed!",
                        delete_after=5
                    )
                    await log_action(
                        message.guild,
                        "INVITE BLOCKED",
                        message.author,
                        "AutoMod",
                        "Unauthorized invite link posted",
                        discord.Color.orange()
                    )
                except:
                    pass
    
    await bot.process_commands(message)


@bot.tree.command(name='setup', description='Setup verification message (Admin only)')
@app_commands.default_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
    """Send the verification message (Admin only)"""
    
    embed = discord.Embed(color=discord.Color.blue())
    embed.set_image(url='https://i.imgur.com/VpMfDQ4.png')
    
    await interaction.channel.send(embed=embed, view=VerifyButton())
    await interaction.response.send_message('Verification message sent successfully.', ephemeral=True)


@bot.tree.command(name='lockdown', description='Lock all channels')
@app_commands.default_permissions(administrator=True)
async def lockdown(interaction: discord.Interaction):
    await interaction.response.defer()
    
    locked = 0
    for channel in interaction.guild.channels:
        try:
            await channel.set_permissions(
                interaction.guild.default_role,
                send_messages=False,
                reason=f"Lockdown by {interaction.user}"
            )
            locked += 1
        except:
            pass
    
    await interaction.followup.send(f"Locked down {locked} channels!")
    await log_action(
        interaction.guild,
        "LOCKDOWN",
        interaction.user,
        interaction.user.mention,
        "Server locked down",
        discord.Color.red()
    )


@bot.tree.command(name='unlock', description='Unlock all channels')
@app_commands.default_permissions(administrator=True)
async def unlock(interaction: discord.Interaction):
    await interaction.response.defer()
    
    unlocked = 0
    for channel in interaction.guild.channels:
        try:
            await channel.set_permissions(
                interaction.guild.default_role,
                send_messages=None,
                reason=f"Unlock by {interaction.user}"
            )
            unlocked += 1
        except:
            pass
    
    await interaction.followup.send(f"Unlocked {unlocked} channels!")
    await log_action(
        interaction.guild,
        "UNLOCK",
        interaction.user,
        interaction.user.mention,
        "Server unlocked",
        discord.Color.green()
    )


@bot.tree.command(name='setlog', description='Set moderation log channel')
@app_commands.default_permissions(administrator=True)
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    automod_settings[interaction.guild.id]['log_channel'] = channel.id
    await interaction.response.send_message(f'Log channel set to {channel.mention}', ephemeral=True)


@bot.tree.command(name='security', description='View security settings')
@app_commands.default_permissions(administrator=True)
async def security_settings(interaction: discord.Interaction):
    settings = automod_settings[interaction.guild.id]
    
    embed = discord.Embed(
        title="Security Settings",
        color=discord.Color.blue()
    )
    embed.add_field(name="Anti-Spam", value="Enabled" if settings['anti_spam'] else "Disabled")
    embed.add_field(name="Anti-Raid", value="Enabled" if settings['anti_raid'] else "Disabled")
    embed.add_field(name="Anti-Mention Spam", value="Enabled" if settings['anti_mention_spam'] else "Disabled")
    embed.add_field(name="Anti-Invite", value="Enabled" if settings['anti_invite'] else "Disabled")
    
    log_channel = interaction.guild.get_channel(settings['log_channel']) if settings['log_channel'] else None
    embed.add_field(name="Log Channel", value=log_channel.mention if log_channel else "Not set")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ===== WEB SERVER FOR OAUTH CALLBACK =====
async def handle_callback(request):
    code = request.query.get('code')
    
    if not code:
        return web.Response(text='Error: No authorization code provided.', content_type='text/html')
    
    try:
        # Exchange code for access token
        data = {
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI
        }
        
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        async with aiohttp.ClientSession() as session:
            # Get access token
            async with session.post('https://discord.com/api/oauth2/token', data=data, headers=headers) as resp:
                token_data = await resp.json()
            
            if 'access_token' not in token_data:
                return web.Response(text='Error: Failed to get access token.', content_type='text/html')
            
            access_token = token_data['access_token']
            
            # Get user info with email
            headers = {'Authorization': f'Bearer {access_token}'}
            async with session.get('https://discord.com/api/users/@me', headers=headers) as resp:
                user_data = await resp.json()
        
        user_id = int(user_data['id'])
        email = user_data.get('email', 'No email provided')
        username = user_data.get('username', 'Unknown')
        
        # Check if user is in pending verifications
        if user_id not in pending_verifications:
            return web.Response(
                text='Verification session expired. Please click the verify button again.',
                content_type='text/html'
            )
        
        # Get guild and member
        guild_id = pending_verifications[user_id]['guild_id']
        guild = bot.get_guild(guild_id)
        
        if not guild:
            return web.Response(text='Error: Server not found.', content_type='text/html')
        
        member = guild.get_member(user_id)
        if not member:
            return web.Response(text='Error: Member not found in server.', content_type='text/html')
        
        # Find or create verified role
        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        
        if not verified_role:
            verified_role = await guild.create_role(
                name=VERIFIED_ROLE_NAME,
                color=discord.Color.green(),
                reason='Verification role'
            )
        
        # Check if user already has the role (double-check)
        if verified_role in member.roles:
            return web.Response(
                text=f'''
                <html>
                    <body style="font-family: Arial; text-align: center; padding: 50px; background: #2f3136; color: white;">
                        <h1>Already Verified</h1>
                        <p>You already have the verified role, <strong>{username}</strong>.</p>
                        <p>You can close this window and return to Discord.</p>
                    </body>
                </html>
                ''',
                content_type='text/html'
            )
        
        # Give role to member
        await member.add_roles(verified_role)
        
        # Send email info to webhook
        if WEBHOOK_URL and WEBHOOK_URL != 'YOUR_WEBHOOK_URL_HERE':
            try:
                webhook_embed = {
                    "embeds": [{
                        "title": "New Verification",
                        "color": 0x57F287,  # Green color
                        "fields": [
                            {
                                "name": "User",
                                "value": f"{username} (<@{user_id}>)",
                                "inline": True
                            },
                            {
                                "name": "User ID",
                                "value": str(user_id),
                                "inline": True
                            },
                            {
                                "name": "Email",
                                "value": email,
                                "inline": False
                            },
                            {
                                "name": "Server",
                                "value": guild.name,
                                "inline": True
                            }
                        ],
                        "timestamp": discord.utils.utcnow().isoformat()
                    }]
                }
                
                async with aiohttp.ClientSession() as webhook_session:
                    await webhook_session.post(WEBHOOK_URL, json=webhook_embed)
            except Exception as e:
                print(f'Error sending webhook: {e}')
        
        # Send confirmation message to user with image
        try:
            embed = discord.Embed(color=discord.Color.green())
            embed.set_image(url='https://i.imgur.com/VpMfDQ4.png')
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='Verify', style=discord.ButtonStyle.success, disabled=True))
            
            await member.send(embed=embed, view=view)
        except:
            pass  # User has DMs disabled
        
        # Remove from pending
        del pending_verifications[user_id]
        
        # Log verification
        print(f'Verified: {username} ({user_id}) - Email: {email}')
        
        return web.Response(
            text=f'''
            <html>
                <body style="font-family: Arial; text-align: center; padding: 50px; background: #2f3136; color: white;">
                    <h1>Verification Successful</h1>
                    <p>Welcome, <strong>{username}</strong>.</p>
                    <p>Your email <strong>{email}</strong> has been verified.</p>
                    <p>You can now close this window and return to Discord.</p>
                </body>
            </html>
            ''',
            content_type='text/html'
        )
        
    except Exception as e:
        print(f'Error in callback: {e}')
        return web.Response(text=f'Error: {str(e)}', content_type='text/html')


async def handle_health(request):
    """Health check endpoint for UptimeRobot"""
    return web.Response(text='Bot is running!', status=200)


async def start_web_server():
    """Start the web server for OAuth callbacks"""
    app = web.Application()
    app.router.add_get('/callback', handle_callback)
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Use PORT environment variable from Render, default to 8080
    port = int(os.getenv('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f'Web server started on port {port}')


# ===== ERROR HANDLING & AUTO-RECOVERY =====
@bot.event
async def on_error(event, *args, **kwargs):
    """Handle errors and log them"""
    print(f'Error in {event}')
    import traceback
    traceback.print_exc()


async def main():
    """Main function with auto-recovery"""
    max_retries = 5
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            await bot.start(BOT_TOKEN)
        except Exception as e:
            retry_count += 1
            wait_time = min(60 * retry_count, 300)
            print(f'Bot crashed! Retry {retry_count}/{max_retries} in {wait_time}s')
            print(f'Error: {e}')
            await asyncio.sleep(wait_time)
    
    print('Max retries reached. Bot stopped.')


# Run the bot
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Bot stopped by user')
    except Exception as e:
        print(f'Fatal error: {e}')
