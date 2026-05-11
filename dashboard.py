import os
import json
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
import requests
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from flask_session import Session
import firebase_admin
from firebase_admin import credentials, firestore
import uuid
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# CONFIGURATION
# ==============================================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('DISCORD_TOKEN', 'dev-secret-key')
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# Discord OAuth2
DISCORD_CLIENT_ID = os.environ.get('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.environ.get('DISCORD_REDIRECT_URI', 'http://localhost:6884/callback')

# Initialize Firestore
DB = None
json_creds_string = os.environ.get('FIREBASE_CREDENTIALS')
if json_creds_string:
    try:
        creds_dict = json.loads(json_creds_string)
        cred = credentials.Certificate(creds_dict)
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(cred)
        DB = firestore.client()
    except Exception as e:
        print(f"ERROR: Could not initialize Firebase: {e}")

BOT_OWNER_ID = 1436238952389410837
HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', 5000))

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def get_discord_user():
    """Gets the current Discord user from the session."""
    return session.get('user')

def require_login(f):
    """Decorator to require Discord login."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def require_admin(f):
    """Decorator to require admin permissions for guild."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_discord_user()
        if not user:
            return redirect(url_for('login'))
        
        guild_id = kwargs.get('guild_id')
        if not guild_id:
            return {"error": "Guild ID required"}, 400
        
        # Check if user has manage_guild permission in this guild
        if not has_guild_permission(user['id'], guild_id, 'manage_guild'):
            return {"error": "Insufficient permissions"}, 403
        
        return f(*args, **kwargs)
    return decorated_function

def require_owner(f):
    """Decorator to require bot owner."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_discord_user()
        if not user:
            return redirect(url_for('login'))
        
        if int(user['id']) != BOT_OWNER_ID:
            return "You do not have permission to access this page.", 403
        
        return f(*args, **kwargs)
    return decorated_function

def has_guild_permission(user_id, guild_id, permission):
    """Check if user has a specific permission in a guild."""
    # This would require more complex permission checking
    # For now, just check if user is admin or owner
    return True  # Simplified for this implementation

def discord_oauth_url():
    """Generates the Discord OAuth2 URL."""
    import urllib.parse
    redirect_uri_encoded = urllib.parse.quote(DISCORD_REDIRECT_URI, safe='')
    url = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={redirect_uri_encoded}&response_type=code&scope=identify%20guilds"
    print(f"DEBUG: Generated OAuth URL: {url}")
    return url

def exchange_code_for_token(code):
    """Exchanges OAuth2 code for access token."""
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'scope': 'identify guilds'
    }
    
    r = requests.post('https://discord.com/api/oauth2/token', data=data)
    if r.status_code != 200:
        return None
    return r.json()

def get_user_info(access_token):
    """Gets user info from Discord API."""
    headers = {'Authorization': f'Bearer {access_token}'}
    r = requests.get('https://discord.com/api/users/@me', headers=headers)
    if r.status_code != 200:
        return None
    return r.json()

def get_user_guilds(access_token):
    """Gets user's guilds from Discord API."""
    headers = {'Authorization': f'Bearer {access_token}'}
    r = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
    if r.status_code != 200:
        return []
    return r.json()

def get_guild_config(guild_id):
    """Gets guild configuration from Firestore."""
    if not DB:
        return {}
    try:
        doc = DB.collection('guild_configs').document(str(guild_id)).get()
        return doc.to_dict() if doc.exists else {}
    except Exception as e:
        print(f"Error loading guild config: {e}")
        return {}

def save_guild_config(guild_id, config):
    """Saves guild configuration to Firestore."""
    if not DB:
        return False
    try:
        DB.collection('guild_configs').document(str(guild_id)).set(config)
        return True
    except Exception as e:
        print(f"Error saving guild config: {e}")
        return False

def get_automod_rules(guild_id):
    """Gets AutoMod rules for a guild."""
    config = get_guild_config(guild_id)
    return config.get('automod', {}).get('rules', [])

def save_automod_rules(guild_id, rules):
    """Saves AutoMod rules to Firestore."""
    config = get_guild_config(guild_id)
    if 'automod' not in config:
        config['automod'] = {}
    config['automod']['rules'] = rules
    return save_guild_config(guild_id, config)

def get_active_giveaways():
    """Gets all active giveaways from Firestore."""
    if not DB:
        return []
    try:
        docs = DB.collection('giveaways').stream()
        giveaways = []
        for doc in docs:
            giveaway = doc.to_dict()
            giveaway['id'] = doc.id
            giveaways.append(giveaway)
        return sorted(giveaways, key=lambda x: x.get('end_time', 0), reverse=True)
    except Exception as e:
        print(f"Error loading giveaways: {e}")
        return []

def create_giveaway(prize, duration_minutes, winner_count, channel_id, host_id):
    """Creates a new giveaway in Firestore."""
    if not DB:
        return False
    try:
        giveaway_id = str(uuid.uuid4())
        end_time = time.time() + (duration_minutes * 60)
        
        giveaway_data = {
            'prize': prize,
            'duration_minutes': duration_minutes,
            'winner_count': winner_count,
            'channel_id': channel_id,
            'host_id': host_id,
            'created_at': time.time(),
            'end_time': end_time,
            'entries': []
        }
        
        DB.collection('giveaways').document(giveaway_id).set(giveaway_data)
        return True
    except Exception as e:
        print(f"Error creating giveaway: {e}")
        return False

def generate_license(months=0, lifetime=False):
    """Generates a new premium license key."""
    if not DB:
        return None
    
    if not lifetime and months <= 0:
        return None
    
    license_key = str(uuid.uuid4()).upper().replace('-', '')[:16]
    
    if lifetime:
        expires_at = "LIFETIME"
    else:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30 * months)).timestamp()
    
    license_data = {
        'months': months if not lifetime else None,
        'lifetime': lifetime,
        'created_at': time.time(),
        'expires_at': expires_at,
        'is_used': False,
        'used_by_guild': None,
        'used_by_user': None
    }
    
    try:
        DB.collection('licenses').document(license_key).set(license_data)
        return license_key
    except Exception as e:
        print(f"Error generating license: {e}")
        return None

# ==============================================================================
# ROUTES - Authentication
# ==============================================================================

@app.route('/login')
def login():
    return redirect(discord_oauth_url())

@app.route('/callback')
def callback():
    """Discord OAuth2 callback."""
    code = request.args.get('code')
    if not code:
        return "No authorization code received", 400
    
    token_data = exchange_code_for_token(code)
    if not token_data:
        return "Failed to exchange code for token", 400
    
    user_data = get_user_info(token_data['access_token'])
    if not user_data:
        return "Failed to fetch user data", 400
    
    guilds_data = get_user_guilds(token_data['access_token'])
    
    # Store in session
    session['user'] = {
        'id': user_data['id'],
        'username': user_data['username'],
        'avatar': user_data.get('avatar'),
        'guilds': guilds_data,
        'access_token': token_data['access_token']
    }
    
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# ==============================================================================
# ROUTES - Main Pages
# ==============================================================================

@app.route('/')
def home():
    """Home page."""
    user = get_discord_user()
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Spectra Bot Dashboard</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: #fff; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
            .container { text-align: center; max-width: 600px; }
            h1 { font-size: 3em; margin-bottom: 20px; color: #7289da; }
            p { font-size: 1.2em; margin-bottom: 30px; color: #b9bbbe; }
            .btn { display: inline-block; padding: 12px 30px; background-color: #7289da; color: white; text-decoration: none; border-radius: 4px; font-size: 1.1em; transition: all 0.3s; border: none; cursor: pointer; }
            .btn:hover { background-color: #5a77c4; transform: scale(1.05); }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>✨ Spectra Bot Dashboard</h1>
            <p>Manage your server's Spectra bot settings</p>
            """
    if user:
        html += f"""
            <p>Welcome, {user['username']}!</p>
            <a href="{url_for('dashboard')}" class="btn">Go to Dashboard</a>
            <a href="{url_for('logout')}" class="btn" style="background-color: #f04747; margin-left: 10px;">Logout</a>
            """
    else:
        html += f"""
            <a href="{url_for('login')}" class="btn">Login with Discord</a>
            """
    html += """
        </div>
    </body>
    </html>
    """
    return html

@app.route('/dashboard')
@require_login
def dashboard():
    """Dashboard page with server selection."""
    user = get_discord_user()
    guilds = user.get('guilds', [])
    is_owner = int(user['id']) == BOT_OWNER_ID
    
    guild_cards = ""
    for guild in guilds:
        guild_id = guild['id']
        guild_name = guild['name']
        guild_icon = guild.get('icon')
        admin = guild.get('owner', False)
        
        if admin or (guild.get('permissions') & 0x8):  # Check for admin permission
            guild_cards += f"""
            <a href="{url_for('guild_settings', guild_id=guild_id)}" class="guild-card">
                <div class="guild-icon">{'👑' if admin else '⚙️'}</div>
                <div class="guild-name">{guild_name}</div>
            </a>
            """
    
    owner_section = ""
    if is_owner:
        owner_section = f"""
        <div class="owner-alert">
            <span>👑 Owner Mode</span>
            <a href="{url_for('owner_panel')}" class="btn-owner">Owner Panel</a>
        </div>
        """
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard - Spectra Bot</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #1e1e2e 0%, #2d2d44 100%); color: #dcddde; min-height: 100vh; }
            .navbar { background: rgba(45, 45, 68, 0.95); backdrop-filter: blur(10px); padding: 15px 30px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(114, 137, 218, 0.2); }
            .navbar h1 { color: #7289da; font-size: 1.8em; font-weight: 700; }
            .navbar-right { display: flex; align-items: center; gap: 20px; }
            .navbar a { color: #b9bbbe; text-decoration: none; transition: color 0.3s; font-size: 0.95em; }
            .navbar a:hover { color: #7289da; }
            .container { max-width: 1400px; margin: 40px auto; padding: 0 20px; }
            h2 { color: #7289da; margin-bottom: 30px; font-size: 2em; font-weight: 700; }
            .owner-alert { background: linear-gradient(135deg, #f47fff 0%, #7289da 100%); padding: 15px 20px; border-radius: 8px; margin-bottom: 30px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 15px rgba(244, 127, 255, 0.3); }
            .owner-alert span { font-weight: 600; font-size: 1.1em; }
            .btn-owner { background: white; color: #7289da; padding: 8px 16px; border-radius: 4px; text-decoration: none; font-weight: 600; transition: all 0.3s; }
            .btn-owner:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
            .guilds-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
            .guild-card { background: linear-gradient(135deg, #2f3136 0%, #2c2f33 100%); padding: 20px; border-radius: 12px; text-align: center; text-decoration: none; color: #dcddde; transition: all 0.3s; border: 2px solid transparent; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
            .guild-card:hover { background: linear-gradient(135deg, #3b3e44 0%, #383b42 100%); border-color: #7289da; transform: translateY(-8px); box-shadow: 0 8px 24px rgba(114, 137, 218, 0.2); }
            .guild-icon { font-size: 3em; margin-bottom: 10px; }
            .guild-name { font-size: 1.05em; font-weight: 600; }
        </style>
    </head>
    <body>
        <div class="navbar">
            <h1>✨ Spectra Dashboard</h1>
            <div class="navbar-right">
                <span>Welcome, """ + user['username'] + """</span>
                <a href=\"""" + url_for('logout') + """\">Logout</a>
            </div>
        </div>
        <div class="container">
            """ + owner_section + """
            <h2>Select a Server</h2>
            <div class="guilds-grid">
                """ + guild_cards + """
            </div>
        </div>
    </body>
    </html>
    """
    return html

@app.route('/owner')
@require_owner
def owner_panel():
    """Owner-only panel for managing licenses and giveaways."""
    user = get_discord_user()
    active_giveaways = get_active_giveaways()
    
    giveaway_rows = ""
    for ga in active_giveaways:
        prize = ga.get('prize', 'Unknown')
        winners = ga.get('winner_count', 1)
        end_time = ga.get('end_time', 0)
        entries = len(ga.get('entries', []))
        time_left = max(0, end_time - time.time())
        hours_left = int(time_left / 3600)
        minutes_left = int((time_left % 3600) / 60)
        
        giveaway_rows += f"""
        <tr>
            <td>{prize}</td>
            <td>{winners}</td>
            <td>{entries}</td>
            <td>{hours_left}h {minutes_left}m</td>
        </tr>
        """
    
    if not giveaway_rows:
        giveaway_rows = "<tr><td colspan='4' style='text-align: center; color: #99aab5;'>No active giveaways</td></tr>"
    
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Owner Panel - Spectra Dashboard</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #1e1e2e 0%, #2d2d44 100%); color: #dcddde; }}
            
            .navbar {{ background: rgba(45, 45, 68, 0.95); backdrop-filter: blur(10px); padding: 15px 30px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(114, 137, 218, 0.2); }}
            .navbar h1 {{ color: #f47fff; font-size: 1.8em; font-weight: 700; }}
            .navbar a {{ color: #b9bbbe; text-decoration: none; margin-left: 20px; transition: color 0.3s; }}
            .navbar a:hover {{ color: #7289da; }}
            
            .wrapper {{ display: flex; min-height: calc(100vh - 60px); }}
            
            .sidebar {{ width: 250px; background: rgba(45, 45, 68, 0.9); padding: 20px 0; box-shadow: 2px 0 10px rgba(0,0,0,0.3); overflow-y: auto; border-right: 1px solid rgba(114, 137, 218, 0.2); }}
            .sidebar-item {{ padding: 12px 20px; color: #b9bbbe; cursor: pointer; transition: all 0.3s; display: flex; align-items: center; text-decoration: none; }}
            .sidebar-item:hover {{ background: rgba(114, 137, 218, 0.2); color: #7289da; }}
            .sidebar-item.active {{ background: linear-gradient(90deg, #7289da 0%, rgba(114, 137, 218, 0.3) 100%); color: #fff; border-left: 4px solid #f47fff; }}
            .sidebar-item-icon {{ margin-right: 10px; font-size: 1.2em; }}
            
            .content {{ flex: 1; overflow-y: auto; padding: 40px; }}
            
            .content h2 {{ color: #f47fff; margin-bottom: 20px; font-size: 2em; font-weight: 700; }}
            .content h3 {{ color: #7289da; margin-bottom: 15px; margin-top: 20px; }}
            
            .section {{ background: linear-gradient(135deg, #2f3136 0%, #2c2f33 100%); padding: 25px; border-radius: 12px; margin-bottom: 20px; border-left: 4px solid #7289da; box-shadow: 0 4px 15px rgba(0,0,0,0.3); }}
            .section p {{ color: #b9bbbe; margin-bottom: 15px; }}
            
            .form-group {{ margin-bottom: 20px; }}
            .form-group label {{ display: block; color: #b9bbbe; margin-bottom: 8px; font-weight: 500; }}
            .form-group input, .form-group textarea {{ width: 100%; padding: 12px; background: #36393f; color: #dcddde; border: 1px solid #4f545c; border-radius: 6px; font-family: inherit; transition: all 0.3s; }}
            .form-group input:focus, .form-group textarea:focus {{ outline: none; border-color: #7289da; box-shadow: 0 0 0 3px rgba(114, 137, 218, 0.1); }}
            
            .form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }}
            
            .btn {{ display: inline-block; padding: 12px 24px; background: linear-gradient(135deg, #7289da 0%, #5a77c4 100%); color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 1em; transition: all 0.3s; font-weight: 600; }}
            .btn:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px rgba(114, 137, 218, 0.4); }}
            .btn-success {{ background: linear-gradient(135deg, #43b581 0%, #2fb467 100%); }}
            .btn-success:hover {{ box-shadow: 0 4px 12px rgba(67, 181, 129, 0.4); }}
            
            .status {{ padding: 12px 15px; border-radius: 6px; margin-bottom: 15px; display: none; }}
            .status.success {{ background: #43b581; color: white; display: block; }}
            .status.error {{ background: #f04747; color: white; display: block; }}
            
            .license-display {{ background: #36393f; padding: 15px; border-radius: 6px; margin-top: 10px; word-break: break-all; font-family: 'Courier New', monospace; color: #00d166; border: 1px solid #4f545c; }}
            
            table {{ width: 100%; border-collapse: collapse; background: #36393f; border-radius: 6px; overflow: hidden; }}
            th, td {{ padding: 12px 15px; text-align: left; border-bottom: 1px solid #4f545c; }}
            th {{ background: linear-gradient(90deg, #7289da 0%, rgba(114, 137, 218, 0.8) 100%); color: white; font-weight: 600; }}
            tr:hover {{ background: #3b3e44; }}
            
            #license-tab, #giveaway-tab {{ display: none; }}
            #license-tab.active, #giveaway-tab.active {{ display: block; }}
        </style>
    </head>
    <body>
        <div class="navbar">
            <h1>👑 Owner Panel</h1>
            <div>
                <span>Welcome, {user['username']} (Owner)</span>
                <a href="{url_for('dashboard')}">← Dashboard</a>
                <a href="{url_for('logout')}">Logout</a>
            </div>
        </div>
        
        <div class="wrapper">
            <div class="sidebar">
                <a onclick="switchTab('license')" class="sidebar-item active">
                    <span class="sidebar-item-icon">🔑</span> Generate Keys
                </a>
                <a onclick="switchTab('giveaway')" class="sidebar-item">
                    <span class="sidebar-item-icon">🎁</span> Giveaways
                </a>
            </div>
            
            <div class="content">
                <!-- License Tab -->
                <div id="license-tab" class="active">
                    <h2>🔑 Premium License Generator</h2>
                    <div class="section">
                        <h3>Generate New License Key</h3>
                        <div class="status" id="licenseStatus"></div>
                        <div class="form-row">
                            <div class="form-group">
                                <label>Duration (Months)</label>
                                <input type="number" id="licenseDuration" value="1" min="1">
                            </div>
                            <div class="form-group">
                                <label style="margin-bottom: 30px;"></label>
                                <label><input type="checkbox" id="lifetimeCheck"> Lifetime License</label>
                            </div>
                        </div>
                        <button class="btn btn-success" onclick="generateLicense()">Generate License Key</button>
                        <div id="licenseKeyDisplay"></div>
                    </div>
                </div>
                
                <!-- Giveaway Tab -->
                <div id="giveaway-tab">
                    <h2>🎁 Active Giveaways</h2>
                    <div class="section">
                        <h3>Create New Giveaway</h3>
                        <div class="status" id="giveawayStatus"></div>
                        <div class="form-group">
                            <label>Prize</label>
                            <input type="text" id="giveawayPrize" placeholder="e.g., $50 Gift Card, Nitro Boost">
                        </div>
                        <div class="form-row">
                            <div class="form-group">
                                <label>Duration (Minutes)</label>
                                <input type="number" id="giveawayDuration" value="60" min="1">
                            </div>
                            <div class="form-group">
                                <label>Number of Winners</label>
                                <input type="number" id="giveawayWinners" value="1" min="1">
                            </div>
                        </div>
                        <button class="btn btn-success" onclick="createGiveaway()">Start Giveaway</button>
                    </div>
                    
                    <div class="section">
                        <h3>Active Giveaways ({len(active_giveaways)})</h3>
                        <table>
                            <thead>
                                <tr>
                                    <th>Prize</th>
                                    <th>Winners</th>
                                    <th>Entries</th>
                                    <th>Time Left</th>
                                </tr>
                            </thead>
                            <tbody>
                                {giveaway_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            function switchTab(tabName) {{
                document.querySelectorAll('[id$="-tab"]').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.sidebar-item').forEach(i => i.classList.remove('active'));
                
                document.getElementById(tabName + '-tab').classList.add('active');
                event.target.closest('.sidebar-item').classList.add('active');
            }}
            
            function generateLicense() {{
                const lifetime = document.getElementById('lifetimeCheck').checked;
                const duration = parseInt(document.getElementById('licenseDuration').value);
                
                const statusEl = document.getElementById('licenseStatus');
                statusEl.className = '';
                
                fetch('/api/owner/generate-license', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        lifetime: lifetime,
                        months: lifetime ? 0 : duration
                    }})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{
                        statusEl.className = 'status success';
                        statusEl.textContent = 'License generated successfully!';
                        document.getElementById('licenseKeyDisplay').innerHTML = `
                            <div class="license-display">
                                License Key: ${{data.key}}
                            </div>
                        `;
                    }} else {{
                        statusEl.className = 'status error';
                        statusEl.textContent = 'Error: ' + data.error;
                    }}
                }})
                .catch(err => {{
                    statusEl.className = 'status error';
                    statusEl.textContent = 'Error: ' + err;
                }});
            }}
            
            function createGiveaway() {{
                const prize = document.getElementById('giveawayPrize').value.trim();
                const duration = parseInt(document.getElementById('giveawayDuration').value);
                const winners = parseInt(document.getElementById('giveawayWinners').value);
                
                const statusEl = document.getElementById('giveawayStatus');
                
                if (!prize) {{
                    statusEl.className = 'status error';
                    statusEl.textContent = 'Please enter a prize';
                    return;
                }}
                
                statusEl.className = '';
                
                fetch('/api/owner/create-giveaway', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        prize: prize,
                        duration_minutes: duration,
                        winner_count: winners
                    }})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{
                        statusEl.className = 'status success';
                        statusEl.textContent = 'Giveaway created successfully!';
                        document.getElementById('giveawayPrize').value = '';
                        document.getElementById('giveawayDuration').value = '60';
                        document.getElementById('giveawayWinners').value = '1';
                        setTimeout(() => location.reload(), 2000);
                    }} else {{
                        statusEl.className = 'status error';
                        statusEl.textContent = 'Error: ' + data.error;
                    }}
                }})
                .catch(err => {{
                    statusEl.className = 'status error';
                    statusEl.textContent = 'Error: ' + err;
                }});
            }}
        </script>
    </body>
    </html>
    """
    return html

@app.route('/guild/<guild_id>')
@require_login
def guild_settings(guild_id):
    """Guild settings page with navigation tabs."""
    user = get_discord_user()
    
    # Verify user is in guild
    guild = next((g for g in user.get('guilds', []) if g['id'] == guild_id), None)
    if not guild:
        return "Guild not found", 404
    
    config = get_guild_config(guild_id)
    is_premium = config.get('premium', {}).get('active', False)
    premium_type = "LIFETIME" if config.get('premium', {}).get('expires_at') == "LIFETIME" else "Temporary"
    premium_expires = config.get('premium', {}).get('expires_at', 0)
    
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{guild['name']} - Spectra Dashboard</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #36393f; color: #dcddde; }}
            
            .navbar {{ background: #2c2f33; padding: 15px 30px; box-shadow: 0 2px 10px rgba(0,0,0,0.3); display: flex; justify-content: space-between; align-items: center; }}
            .navbar h1 {{ color: #7289da; font-size: 1.5em; }}
            .navbar a {{ color: #dcddde; text-decoration: none; margin-left: 20px; transition: color 0.3s; cursor: pointer; }}
            .navbar a:hover {{ color: #7289da; }}
            
            .wrapper {{ display: flex; height: calc(100vh - 60px); }}
            
            .sidebar {{ width: 250px; background: #2c2f33; padding: 20px 0; box-shadow: 2px 0 10px rgba(0,0,0,0.3); overflow-y: auto; }}
            .sidebar-item {{ padding: 12px 20px; color: #b9bbbe; cursor: pointer; transition: all 0.3s; display: flex; align-items: center; text-decoration: none; }}
            .sidebar-item:hover {{ background: #3b3e44; color: #7289da; }}
            .sidebar-item.active {{ background: #7289da; color: #fff; border-left: 4px solid #fff; }}
            .sidebar-item-icon {{ margin-right: 10px; font-size: 1.2em; }}
            
            .content {{ flex: 1; overflow-y: auto; padding: 40px; }}
            
            .content h2 {{ color: #7289da; margin-bottom: 20px; font-size: 2em; }}
            
            .premium-badge {{ display: inline-block; background: #f47fff; color: #fff; padding: 5px 10px; border-radius: 4px; font-size: 0.9em; margin-bottom: 20px; }}
            
            .section {{ background: #2f3136; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
            .section h3 {{ color: #7289da; margin-bottom: 15px; }}
            .section p {{ color: #b9bbbe; margin-bottom: 10px; }}
            
            .form-group {{ margin-bottom: 15px; }}
            .form-group label {{ display: block; color: #b9bbbe; margin-bottom: 5px; }}
            .form-group input, .form-group textarea, .form-group select {{ width: 100%; padding: 10px; background: #36393f; color: #dcddde; border: 1px solid #4f545c; border-radius: 4px; font-family: inherit; }}
            .form-group input:focus, .form-group textarea:focus, .form-group select:focus {{ outline: none; border-color: #7289da; box-shadow: 0 0 0 3px rgba(114, 137, 218, 0.1); }}
            
            .btn {{ display: inline-block; padding: 10px 20px; background: #7289da; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; transition: all 0.3s; }}
            .btn:hover {{ background: #5a77c4; }}
            .btn-danger {{ background: #f04747; }}
            .btn-danger:hover {{ background: #d83c3c; }}
            
            .status {{ padding: 10px 15px; border-radius: 4px; margin-bottom: 15px; }}
            .status.success {{ background: #43b581; color: white; }}
            .status.error {{ background: #f04747; color: white; }}
            .status.info {{ background: #5a77c4; color: white; }}
            
            .word-list {{ background: #36393f; padding: 10px; border-radius: 4px; margin-top: 10px; max-height: 200px; overflow-y: auto; }}
            .word-list-item {{ padding: 5px 10px; background: #2f3136; margin: 5px 0; border-radius: 3px; display: flex; justify-content: space-between; align-items: center; }}
            .word-list-item button {{ padding: 5px 10px; background: #f04747; color: white; border: none; border-radius: 3px; cursor: pointer; font-size: 0.9em; }}
            
            #automod-tab, #giveaway-tab, #premium-tab, #leveling-tab {{ display: none; }}
            #automod-tab.active, #giveaway-tab.active, #premium-tab.active, #leveling-tab.active {{ display: block; }}
        </style>
    </head>
    <body>
        <div class="navbar">
            <h1>Spectra Dashboard - {guild['name']}</h1>
            <div>
                <a href="{url_for('dashboard')}">← Back to Dashboard</a>
                <a href="{url_for('logout')}">Logout</a>
            </div>
        </div>
        
        <div class="wrapper">
            <div class="sidebar">
                <a onclick="switchTab('automod')" class="sidebar-item active">
                    <span class="sidebar-item-icon">🛡️</span> AutoMod
                </a>
                <a onclick="switchTab('giveaway')" class="sidebar-item">
                    <span class="sidebar-item-icon">🎁</span> Giveaways
                </a>
                <a onclick="switchTab('premium')" class="sidebar-item">
                    <span class="sidebar-item-icon">⭐</span> Premium
                </a>
                <a onclick="switchTab('leveling')" class="sidebar-item">
                    <span class="sidebar-item-icon">📊</span> Leveling
                </a>
            </div>
            
            <div class="content">
                <!-- AutoMod Tab -->
                <div id="automod-tab" class="active">
                    <h2>🛡️ AutoMod Configuration</h2>
                    <div class="section">
                        <h3>Blocked Words</h3>
                        <p>Add words or phrases to auto-block in this server.</p>
                        <div class="form-group">
                            <label>Add Word or Phrase</label>
                            <input type="text" id="newWord" placeholder="Enter word to block...">
                            <button class="btn" onclick="addBlockedWord('{guild_id}')">Add Word</button>
                        </div>
                        <div class="word-list" id="wordList"></div>
                    </div>
                </div>
                
                <!-- Giveaway Tab -->
                <div id="giveaway-tab">
                    <h2>🎁 Giveaway Settings</h2>
                    <div class="section">
                        <h3>Configure Giveaways</h3>
                        <p>Use <code>/giveaway_start</code> command in Discord to create giveaways.</p>
                        <div class="form-group">
                            <label>Default Winner Count</label>
                            <input type="number" id="defaultWinners" value="1" min="1">
                            <button class="btn" onclick="saveGiveawaySettings('{guild_id}')">Save</button>
                        </div>
                    </div>
                </div>
                
                <!-- Premium Tab -->
                <div id="premium-tab">
                    <h2>⭐ Premium Status</h2>
                    <div class="section">
                        <h3>Current Status</h3>
                        {'<span class="premium-badge">✨ ' + premium_type + ' Premium Active ✨</span>' if is_premium else '<span class="premium-badge" style="background: #72767d;">Standard Access</span>'}
                        <p>Server ID: <code>{guild_id}</code></p>
                        <p>Use <code>/license_activate</code> command in Discord to activate a premium license.</p>
                    </div>
                </div>
                
                <!-- Leveling Tab -->
                <div id="leveling-tab">
                    <h2>📊 Leveling System</h2>
                    <div class="section">
                        <h3>Leveling Configuration</h3>
                        <p>Customize how users earn XP in your server.</p>
                        <div class="form-group">
                            <label>XP per Message (15-25 default)</label>
                            <input type="number" id="xpMin" value="15" min="1"> - <input type="number" id="xpMax" value="25" min="1">
                            <button class="btn" onclick="saveLevelingSettings('{guild_id}')">Save</button>
                        </div>
                        <div class="form-group">
                            <label>XP Needed per Level (100 default)</label>
                            <input type="number" id="xpPerLevel" value="100" min="1">
                            <button class="btn" onclick="saveLevelingSettings('{guild_id}')">Save</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            function switchTab(tabName) {{
                // Hide all tabs
                document.querySelectorAll('[id$="-tab"]').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.sidebar-item').forEach(i => i.classList.remove('active'));
                
                // Show selected tab
                document.getElementById(tabName + '-tab').classList.add('active');
                event.target.closest('.sidebar-item').classList.add('active');
            }}
            
            function addBlockedWord(guildId) {{
                const word = document.getElementById('newWord').value.trim();
                if (!word) {{
                    alert('Please enter a word');
                    return;
                }}
                
                fetch(`/api/guild/${{guildId}}/automod/add-word`, {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{word: word}})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{
                        document.getElementById('newWord').value = '';
                        loadBlockedWords(guildId);
                    }} else {{
                        alert('Error: ' + data.error);
                    }}
                }});
            }}
            
            function removeBlockedWord(guildId, word) {{
                fetch(`/api/guild/${{guildId}}/automod/remove-word`, {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{word: word}})
                }})
                .then(() => loadBlockedWords(guildId));
            }}
            
            function loadBlockedWords(guildId) {{
                fetch(`/api/guild/${{guildId}}/automod/words`)
                .then(r => r.json())
                .then(data => {{
                    const list = document.getElementById('wordList');
                    list.innerHTML = '';
                    data.words.forEach(word => {{
                        const item = document.createElement('div');
                        item.className = 'word-list-item';
                        item.innerHTML = `<span>${{word}}</span><button onclick="removeBlockedWord('{guild_id}', '${{word}}')"">Remove</button>`;
                        list.appendChild(item);
                    }});
                }});
            }}
            
            function saveGiveawaySettings(guildId) {{
                const winners = document.getElementById('defaultWinners').value;
                fetch(`/api/guild/${{guildId}}/giveaway/settings`, {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{defaultWinners: winners}})
                }})
                .then(() => alert('Giveaway settings saved!'));
            }}
            
            function saveLevelingSettings(guildId) {{
                const xpMin = document.getElementById('xpMin').value;
                const xpMax = document.getElementById('xpMax').value;
                const xpPerLevel = document.getElementById('xpPerLevel').value;
                fetch(`/api/guild/${{guildId}}/leveling/settings`, {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{xpMin, xpMax, xpPerLevel}})
                }})
                .then(() => alert('Leveling settings saved!'));
            }}
            
            // Load initial data
            loadBlockedWords('{guild_id}');
        </script>
    </body>
    </html>
    """
    return html

# ==============================================================================
# API Routes - AutoMod
# ==============================================================================

@app.route('/api/guild/<guild_id>/automod/words', methods=['GET'])
@require_login
def get_automod_words(guild_id):
    """Get blocked words for a guild."""
    rules = get_automod_rules(guild_id)
    words = []
    for rule in rules:
        if isinstance(rule, dict) and 'keywords' in rule:
            words.extend(rule['keywords'])
    return jsonify({'words': words})

@app.route('/api/guild/<guild_id>/automod/add-word', methods=['POST'])
@require_login
def add_automod_word(guild_id):
    """Add a blocked word."""
    data = request.json
    word = data.get('word', '').strip().lower()
    
    if not word:
        return jsonify({'success': False, 'error': 'Invalid word'}), 400
    
    rules = get_automod_rules(guild_id)
    if not rules:
        rules = [{'keywords': []}]
    
    if word not in rules[0]['keywords']:
        rules[0]['keywords'].append(word)
        save_automod_rules(guild_id, rules)
    
    return jsonify({'success': True})

@app.route('/api/guild/<guild_id>/automod/remove-word', methods=['POST'])
@require_login
def remove_automod_word(guild_id):
    """Remove a blocked word."""
    data = request.json
    word = data.get('word', '').strip().lower()
    
    rules = get_automod_rules(guild_id)
    if rules and 'keywords' in rules[0]:
        if word in rules[0]['keywords']:
            rules[0]['keywords'].remove(word)
            save_automod_rules(guild_id, rules)
    
    return jsonify({'success': True})

# ==============================================================================
# API Routes - Giveaway
# ==============================================================================

@app.route('/api/guild/<guild_id>/giveaway/settings', methods=['POST'])
@require_login
def save_giveaway_settings(guild_id):
    """Save giveaway settings."""
    data = request.json
    config = get_guild_config(guild_id)
    if 'giveaway' not in config:
        config['giveaway'] = {}
    config['giveaway']['default_winners'] = int(data.get('defaultWinners', 1))
    save_guild_config(guild_id, config)
    return jsonify({'success': True})

# ==============================================================================
# API Routes - Leveling
# ==============================================================================

@app.route('/api/guild/<guild_id>/leveling/settings', methods=['POST'])
@require_login
def save_leveling_settings(guild_id):
    """Save leveling settings."""
    data = request.json
    config = get_guild_config(guild_id)
    if 'leveling' not in config:
        config['leveling'] = {}
    config['leveling']['xp_min'] = int(data.get('xpMin', 15))
    config['leveling']['xp_max'] = int(data.get('xpMax', 25))
    config['leveling']['xp_per_level'] = int(data.get('xpPerLevel', 100))
    save_guild_config(guild_id, config)
    return jsonify({'success': True})

# ==============================================================================
# API Routes - Owner Panel
# ==============================================================================

@app.route('/api/owner/generate-license', methods=['POST'])
@require_owner
def api_generate_license():
    """Generate a new premium license (owner only)."""
    data = request.json
    lifetime = data.get('lifetime', False)
    months = int(data.get('months', 0))
    
    if not lifetime and months <= 0:
        return jsonify({'success': False, 'error': 'Invalid duration'}), 400
    
    license_key = generate_license(months=months, lifetime=lifetime)
    
    if license_key:
        return jsonify({'success': True, 'key': license_key})
    else:
        return jsonify({'success': False, 'error': 'Failed to generate license'}), 500

@app.route('/api/owner/create-giveaway', methods=['POST'])
@require_owner
def api_create_giveaway():
    """Create a new giveaway (owner only)."""
    data = request.json
    prize = data.get('prize', '').strip()
    duration = int(data.get('duration_minutes', 60))
    winners = int(data.get('winner_count', 1))
    
    if not prize or duration <= 0 or winners <= 0:
        return jsonify({'success': False, 'error': 'Invalid giveaway data'}), 400
    
    # Create in Firestore with a default channel (can be updated later)
    success = create_giveaway(
        prize=prize,
        duration_minutes=duration,
        winner_count=winners,
        channel_id=0,  # Dashboard giveaways have no channel
        host_id=int(get_discord_user()['id'])
    )
    
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Failed to create giveaway'}), 500

# ==============================================================================
# Error Handlers
# ==============================================================================

@app.errorhandler(404)
def not_found(e):
    return "Page not found", 404

@app.errorhandler(500)
def server_error(e):
    return "Server error", 500

# ==============================================================================
# Main
# ==============================================================================

if __name__ == '__main__':
    print(f"--- Starting Spectra Dashboard ---")
    print(f"Open http://{HOST}:{PORT} in your browser")
    print(f"Ensure DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET are set in .env")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)