import discord
from discord.ext import commands
from discord import app_commands
from aiohttp import web
import aiohttp
import asyncio

BOT_TOKEN = 'MTQ0MzAyMzY0NTk0NDU3ODE4MQ.GW-gfJ.mGq2MV_R5RXfWs4_5W9v9H32kgQmRsJFOPkRTA'  # From Bot section
CLIENT_ID = '1443023645944578181'  # From OAuth2 section (same as Application ID)
CLIENT_SECRET = 'nUFIHKTbENS0RndS6Q9YP8iExRlFeZNs'  # From OAuth2 section, click "Reset Secret"
REDIRECT_URI = 'http://localhost:8080/callback'  # Add this in OAuth2 > Redirects

# Webhook URL for logging emails to private channel
WEBHOOK_URL = 'https://discord.com/api/webhooks/1443059223092269127/akPU_Gz2Y7y6kdq-1heiEJ7ci_j0sXq_C_c0G0okxCGwvMaDR70qduxJ6go-CAGlQ7tF'  # Right-click channel > Edit Channel > Integrations > Webhooks

# Role to give after verification
VERIFIED_ROLE_NAME = 'Verified'  # Change this to your role name

# ===== BOT SETUP =====
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Store pending verifications
pending_verifications = {}


class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label='Verify', style=discord.ButtonStyle.primary, emoji='🔐', custom_id='verify_btn')
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Generate OAuth2 URL with email access
        oauth_url = (
            f"https://discord.com/api/oauth2/authorize?"
            f"client_id={CLIENT_ID}"
            f"&redirect_uri={REDIRECT_URI}"
            f"&response_type=code"
            f"&scope=identify%20email"
        )
        
        # Store user info for callback
        pending_verifications[interaction.user.id] = {
            'guild_id': interaction.guild.id,
            'user': interaction.user
        }
        
        # Direct link button - opens in browser directly
        verify_button = discord.ui.Button(
            label='Verify',
            url=oauth_url,
            style=discord.ButtonStyle.link,
            emoji='🔐'
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


@bot.tree.command(name='setup', description='Setup verification message (Admin only)')
@app_commands.default_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
    """Send the verification message (Admin only)"""
    
    embed = discord.Embed(color=discord.Color.blue())
    embed.set_image(url='https://i.imgur.com/VpMfDQ4.png')
    
    await interaction.channel.send(embed=embed, view=VerifyButton())
    await interaction.response.send_message('✅ Verification message sent!', ephemeral=True)


# ===== WEB SERVER FOR OAUTH CALLBACK =====
async def handle_callback(request):
    code = request.query.get('code')
    
    if not code:
        return web.Response(text='❌ No authorization code provided!', content_type='text/html')
    
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
                return web.Response(text='❌ Failed to get access token!', content_type='text/html')
            
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
                text='❌ Verification session expired. Please click the verify button again.',
                content_type='text/html'
            )
        
        # Get guild and member
        guild_id = pending_verifications[user_id]['guild_id']
        guild = bot.get_guild(guild_id)
        
        if not guild:
            return web.Response(text='❌ Server not found!', content_type='text/html')
        
        member = guild.get_member(user_id)
        if not member:
            return web.Response(text='❌ Member not found in server!', content_type='text/html')
        
        # Find or create verified role
        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        
        if not verified_role:
            verified_role = await guild.create_role(
                name=VERIFIED_ROLE_NAME,
                color=discord.Color.green(),
                reason='Verification role'
            )
        
        # Give role to member
        await member.add_roles(verified_role)
        
        # Send email info to webhook
        if WEBHOOK_URL and WEBHOOK_URL != 'YOUR_WEBHOOK_URL_HERE':
            try:
                webhook_embed = {
                    "embeds": [{
                        "title": "✅ New Verification",
                        "color": 0x57F287,  # Green color
                        "fields": [
                            {
                                "name": "👤 User",
                                "value": f"{username} (<@{user_id}>)",
                                "inline": True
                            },
                            {
                                "name": "🆔 User ID",
                                "value": str(user_id),
                                "inline": True
                            },
                            {
                                "name": "📧 Email",
                                "value": email,
                                "inline": False
                            },
                            {
                                "name": "🏠 Server",
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
            view.add_item(discord.ui.Button(label='Verify', emoji='🔐', style=discord.ButtonStyle.success, disabled=True))
            
            await member.send(embed=embed, view=view)
        except:
            pass  # User has DMs disabled
        
        # Remove from pending
        del pending_verifications[user_id]
        
        # Log verification
        print(f'✅ Verified: {username} ({user_id}) - Email: {email}')
        
        return web.Response(
            text=f'''
            <html>
                <body style="font-family: Arial; text-align: center; padding: 50px; background: #2f3136; color: white;">
                    <h1>✅ Verification Successful!</h1>
                    <p>Welcome, <strong>{username}</strong>!</p>
                    <p>Your email <strong>{email}</strong> has been verified.</p>
                    <p>You can now close this window and return to Discord.</p>
                </body>
            </html>
            ''',
            content_type='text/html'
        )
        
    except Exception as e:
        print(f'Error in callback: {e}')
        return web.Response(text=f'❌ Error: {str(e)}', content_type='text/html')


async def start_web_server():
    """Start the web server for OAuth callbacks"""
    app = web.Application()
    app.router.add_get('/callback', handle_callback)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
    await site.start()
    print('Web server started on http://localhost:8080')


# Run the bot
bot.run(BOT_TOKEN)
