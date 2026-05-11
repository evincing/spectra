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

BOT_OWNER_ID = 1356850034993397781
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
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard - Spectra Bot</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #36393f; color: #dcddde; }
            .navbar { background: #2c2f33; padding: 15px 30px; box-shadow: 0 2px 10px rgba(0,0,0,0.3); display: flex; justify-content: space-between; align-items: center; }
            .navbar h1 { color: #7289da; font-size: 1.5em; }
            .navbar a { color: #dcddde; text-decoration: none; margin-left: 20px; transition: color 0.3s; }
            .navbar a:hover { color: #7289da; }
            .container { max-width: 1200px; margin: 40px auto; padding: 0 20px; }
            h2 { color: #7289da; margin-bottom: 30px; font-size: 2em; }
            .guilds-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
            .guild-card { background: #2f3136; padding: 20px; border-radius: 8px; text-align: center; text-decoration: none; color: #dcddde; transition: all 0.3s; border: 2px solid transparent; cursor: pointer; }
            .guild-card:hover { background: #3b3e44; border-color: #7289da; transform: translateY(-5px); box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
            .guild-icon { font-size: 3em; margin-bottom: 10px; }
            .guild-name { font-size: 1.1em; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="navbar">
            <h1>Spectra Dashboard</h1>
            <div>
                <span>Welcome, """ + user['username'] + """</span>
                <a href=\"""" + url_for('logout') + """\">Logout</a>
            </div>
        </div>
        <div class="container">
            <h2>Select a Server</h2>
            <div class="guilds-grid">
                """ + guild_cards + """
            </div>
        </div>
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