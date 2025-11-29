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
import sqlite3
from pathlib import Path

# ===== CONFIGURATION =====
BOT_TOKEN = os.getenv('BOT_TOKEN')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
VERIFIED_ROLE_NAME = os.getenv('VERIFIED_ROLE_NAME', 'Verified')

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

# ===== DATA STORAGE =====
pending_verifications = {}
user_messages = defaultdict(list)
user_joins = defaultdict(list)
warned_users = defaultdict(int)
automod_settings = defaultdict(lambda: {
    'anti_spam': True,
    'anti_raid': True,
    'anti_mention_spam': True,
    'anti_invite': True,
    'log_channel': None
})

vc_data_file = Path('vc_data.json')
vc_tracking = defaultdict(lambda: {
    'total_time': 0,
    'current_session_start': None,
    'username': None,
    'avatar': None
})

# ===== VC DATA FUNCTIONS =====
DB_FILE = '/opt/render/project/src/vc_data.db'  # Render's persistent path

def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS vc_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            avatar TEXT,
            total_seconds INTEGER DEFAULT 0,
            current_session_start REAL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ Database initialized")

def get_user_data(user_id):
    """Get user VC data"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT * FROM vc_users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    
    conn.close()
    
    if result:
        return {
            'user_id': result[0],
            'username': result[1],
            'avatar': result[2],
            'total_seconds': result[3],
            'current_session_start': result[4]
        }
    return None

def save_user_data(user_id, username, avatar, total_seconds, session_start):
    """Save/update user VC data"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        INSERT OR REPLACE INTO vc_users 
        (user_id, username, avatar, total_seconds, current_session_start, last_updated)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, username, avatar, total_seconds, session_start))
    
    conn.commit()
    conn.close()

def get_all_users():
    """Get all users with VC data"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT * FROM vc_users ORDER BY total_seconds DESC')
    results = c.fetchall()
    
    conn.close()
    
    return [{
        'user_id': row[0],
        'username': row[1],
        'avatar': row[2],
        'total_seconds': row[3],
        'current_session_start': row[4]
    } for row in results]

# ===== VC EVENT HANDLERS =====
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    
    user_id = member.id
    current_time = datetime.utcnow().timestamp()
    
    # User joined VC
    if before.channel is None and after.channel is not None:
        user_data = get_user_data(user_id)
        
        if user_data:
            total_seconds = user_data['total_seconds']
        else:
            total_seconds = 0
        
        save_user_data(
            user_id,
            str(member),
            str(member.display_avatar.url),
            total_seconds,
            current_time
        )
        print(f"🎤 {member} joined VC")
    
    # User left VC
    elif before.channel is not None and after.channel is None:
        user_data = get_user_data(user_id)
        
        if user_data and user_data['current_session_start']:
            session_duration = int(current_time - user_data['current_session_start'])
            new_total = user_data['total_seconds'] + session_duration
            
            save_user_data(
                user_id,
                user_data['username'],
                user_data['avatar'],
                new_total,
                None
            )
            print(f"👋 {member} left VC. Session: {session_duration}s, Total: {new_total}s")
    
    # User switched channels
    elif before.channel != after.channel:
        user_data = get_user_data(user_id)
        
        if user_data and user_data['current_session_start']:
            session_duration = int(current_time - user_data['current_session_start'])
            new_total = user_data['total_seconds'] + session_duration
            
            save_user_data(
                user_id,
                str(member),
                str(member.display_avatar.url),
                new_total,
                current_time
            )
            print(f"🔄 {member} switched channels")

# ===== VC COMMANDS =====
@bot.tree.command(name='vcstats', description='View voice channel statistics')
async def vcstats(interaction: discord.Interaction, user: discord.User = None):
    target_user = user or interaction.user
    user_id = target_user.id
    
    if user_id not in vc_tracking or vc_tracking[user_id]['total_time'] == 0:
        await interaction.response.send_message(
            f"{target_user.mention} has no voice channel time recorded yet.",
            ephemeral=True
        )
        return
    
    total_seconds = vc_tracking[user_id]['total_time']
    
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
    if not vc_tracking:
        await interaction.response.send_message("No voice channel data recorded yet.", ephemeral=True)
        return
    
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
    global vc_tracking
    vc_tracking.clear()
    save_vc_data()
    await interaction.response.send_message("All VC statistics have been reset.", ephemeral=True)

# ===== SECURITY FUNCTIONS =====
async def log_action(guild, action_type, user, moderator, reason, color=discord.Color.orange()):
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
    if not automod_settings[message.guild.id]['anti_spam']:
        return False
    
    user_id = message.author.id
    current_time = datetime.utcnow()
    
    user_messages[user_id].append(current_time)
    user_messages[user_id] = [
        msg_time for msg_time in user_messages[user_id]
        if (current_time - msg_time).total_seconds() < SPAM_TIMEFRAME
    ]
    
    return len(user_messages[user_id]) >= SPAM_THRESHOLD


async def check_raid(member):
    if not automod_settings[member.guild.id]['anti_raid']:
        return False
    
    guild_id = member.guild.id
    current_time = datetime.utcnow()
    
    user_joins[guild_id].append(current_time)
    user_joins[guild_id] = [
        join_time for join_time in user_joins[guild_id]
        if (current_time - join_time).total_seconds() < RAID_TIMEFRAME
    ]
    
    return len(user_joins[guild_id]) >= RAID_THRESHOLD


# ===== VERIFICATION BUTTON =====
class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label='Verify', style=discord.ButtonStyle.primary, custom_id='verify_btn')
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        verified_role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
        
        if verified_role and verified_role in interaction.user.roles:
            embed = discord.Embed(
                title="Already Verified",
                description="You already have the verified role.",
                color=discord.Color.green()
            )
            embed.set_image(url='https://i.imgur.com/VpMfDQ4.png')
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        oauth_url = (
            f"https://discord.com/oauth2/authorize?"
            f"client_id={CLIENT_ID}"
            f"&redirect_uri={REDIRECT_URI}"
            f"&response_type=code"
            f"&scope=identify%20email"
            f"&prompt=none"
        )
        
        pending_verifications[interaction.user.id] = {
            'guild_id': interaction.guild.id,
            'user': interaction.user
        }
        
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

# ===== BOT EVENTS =====
@bot.event
async def on_member_join(member):
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


@bot.event
async def on_error(event, *args, **kwargs):
    print(f'❌ Error in {event}')
    import traceback
    traceback.print_exc()

# ===== MODERATION COMMANDS =====
@bot.tree.command(name='setup', description='Setup verification message (Admin only)')
@app_commands.default_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
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

# ===== WEB API HANDLERS =====
async def handle_vc_leaderboard(request):
    """API endpoint for leaderboard"""
    users = get_all_users()
    current_time = datetime.utcnow().timestamp()
    
    leaderboard = []
    for user in users:
        total_seconds = user['total_seconds']
        
        # Add current session time if in VC
        if user['current_session_start']:
            session_time = int(current_time - user['current_session_start'])
            total_seconds += session_time
        
        leaderboard.append({
            'user_id': str(user['user_id']),
            'username': user['username'],
            'avatar': user['avatar'],
            'total_seconds': total_seconds,
            'in_vc': user['current_session_start'] is not None
        })
    
    leaderboard.sort(key=lambda x: x['total_seconds'], reverse=True)
    
    return web.json_response({
        'success': True,
        'leaderboard': leaderboard[:50],
        'total_users': len(leaderboard)
    })


async def handle_vc_current(request):
    """API endpoint for current VC users"""
    users = get_all_users()
    current_time = datetime.utcnow().timestamp()
    
    current_users = []
    for user in users:
        if user['current_session_start']:
            session_duration = int(current_time - user['current_session_start'])
            
            current_users.append({
                'user_id': str(user['user_id']),
                'username': user['username'],
                'avatar': user['avatar'],
                'session_duration': session_duration
            })
    
    current_users.sort(key=lambda x: x['session_duration'], reverse=True)
    
    return web.json_response({
        'success': True,
        'current_users': current_users,
        'count': len(current_users)
    })
    # ===== ADD THESE FUNCTIONS BEFORE start_web_server() =====

async def handle_health(request):
    """Health check endpoint"""
    return web.Response(text='Bot is running!', status=200)


async def handle_callback(request):
    """OAuth callback handler"""
    code = request.query.get('code')
    
    if not code:
        return web.Response(text='Error: No authorization code provided.', content_type='text/html')
    
    try:
        data = {
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI
        }
        
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        async with aiohttp.ClientSession() as session:
            async with session.post('https://discord.com/api/oauth2/token', data=data, headers=headers) as resp:
                token_data = await resp.json()
            
            if 'access_token' not in token_data:
                return web.Response(text='Error: Failed to get access token.', content_type='text/html')
            
            access_token = token_data['access_token']
            
            headers = {'Authorization': f'Bearer {access_token}'}
            async with session.get('https://discord.com/api/users/@me', headers=headers) as resp:
                user_data = await resp.json()
        
        user_id = int(user_data['id'])
        email = user_data.get('email', 'No email provided')
        username = user_data.get('username', 'Unknown')
        
        if user_id not in pending_verifications:
            return web.Response(
                text='Verification session expired. Please click the verify button again.',
                content_type='text/html'
            )
        
        guild_id = pending_verifications[user_id]['guild_id']
        guild = bot.get_guild(guild_id)
        
        if not guild:
            return web.Response(text='Error: Server not found.', content_type='text/html')
        
        member = guild.get_member(user_id)
        if not member:
            return web.Response(text='Error: Member not found in server.', content_type='text/html')
        
        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        
        if not verified_role:
            verified_role = await guild.create_role(
                name=VERIFIED_ROLE_NAME,
                color=discord.Color.green(),
                reason='Verification role'
            )
        
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
        
        await member.add_roles(verified_role)
        
        if WEBHOOK_URL and WEBHOOK_URL != 'YOUR_WEBHOOK_URL_HERE':
            try:
                webhook_embed = {
                    "embeds": [{
                        "title": "New Verification",
                        "color": 0x57F287,
                        "fields": [
                            {"name": "User", "value": f"{username} (<@{user_id}>)", "inline": True},
                            {"name": "User ID", "value": str(user_id), "inline": True},
                            {"name": "Email", "value": email, "inline": False},
                            {"name": "Server", "value": guild.name, "inline": True}
                        ],
                        "timestamp": discord.utils.utcnow().isoformat()
                    }]
                }
                
                async with aiohttp.ClientSession() as webhook_session:
                    await webhook_session.post(WEBHOOK_URL, json=webhook_embed)
            except Exception as e:
                print(f'Error sending webhook: {e}')
        
        try:
            embed = discord.Embed(color=discord.Color.green())
            embed.set_image(url='https://i.imgur.com/VpMfDQ4.png')
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='Verify', style=discord.ButtonStyle.success, disabled=True))
            
            await member.send(embed=embed, view=view)
        except:
            pass
        
        del pending_verifications[user_id]
        print(f'✅ Verified: {username} ({user_id}) - Email: {email}')
        
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


# ===== WEB SERVER =====
async def start_web_server():
    """Start the web server for OAuth callbacks and API"""
    app = web.Application()
    
    # OAuth and health routes
    app.router.add_get('/callback', handle_callback)
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    
    # VC tracking API endpoints
    app.router.add_get('/api/leaderboard', handle_vc_leaderboard)
    app.router.add_get('/api/current', handle_vc_current)
    
    # Enable CORS
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
    
    print(f'🌐 Web server started on port {port}')
    print(f'📊 API endpoints:')
    print(f'   - GET /api/leaderboard')
    print(f'   - GET /api/current')
    print(f'   - GET /health')


# ===== BOT READY EVENT =====
@bot.event
async def on_ready():
    print(f'🤖 Bot logged in as {bot.user}')
    
    # Initialize database
    init_db()
    
    # Your other on_ready code here...
    
    bot.add_view(VerifyButton())
    asyncio.create_task(start_web_server())
    
    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} slash command(s)')
    except Exception as e:
        print(f'❌ Error syncing commands: {e}')


# ===== MAIN FUNCTION =====
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
            print(f'❌ Bot crashed! Retry {retry_count}/{max_retries} in {wait_time}s')
            print(f'Error: {e}')
            await asyncio.sleep(wait_time)
    
    print('💀 Max retries reached. Bot stopped.')


# ===== RUN BOT =====
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('⛔ Bot stopped by user')
    except Exception as e:
        print(f'💥 Fatal error: {e}')
