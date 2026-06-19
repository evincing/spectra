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
# ROUTES - Main Pages (STYLING UPDATED)
# ==============================================================================

@app.route('/')
def home():
    """Home page."""
    user = get_discord_user()
    invite_bot_url = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&permissions=8&integration_type=0&scope=bot+applications.commands"
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Spectra Bot Dashboard</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { 
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
                background-color: #0f111a; 
                color: #ffffff; 
                min-height: 100vh; 
                display: flex; 
                align-items: center; 
                justify-content: center; 
                background-image: radial-gradient(circle at 50% -20%, #1c2339 0%, #0f111a 100%);
            }
            .container { text-align: center; max-width: 800px; padding: 40px; }
            h1 { font-size: 3.5rem; font-weight: 800; margin-bottom: 16px; letter-spacing: -1px; }
            .accent { color: #5865F2; }
            p { font-size: 1.25rem; margin-bottom: 40px; color: #94a3b8; line-height: 1.6; }
            .btn-group { display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; }
            .btn { 
                display: inline-flex; 
                align-items: center; 
                gap: 10px;
                padding: 14px 28px; 
                background-color: #5865F2; 
                color: white; 
                text-decoration: none; 
                border-radius: 8px; 
                font-size: 1rem; 
                font-weight: 600; 
                transition: all 0.2s ease; 
                border: none; 
                cursor: pointer; 
            }
            .btn:hover { background-color: #4752c4; transform: translateY(-2px); box-shadow: 0 4px 20px rgba(88, 101, 242, 0.4); }
            .btn-secondary { background-color: #2b2d31; color: #ffffff; }
            .btn-secondary:hover { background-color: #3b3e44; box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3); }
            .btn-danger { background-color: #da373c; }
            .btn-danger:hover { background-color: #a12829; }
            .discord-logo { width: 24px; height: 24px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1><span class="accent">Spectra</span> Bot</h1>
            <p>The next generation of server management. Fast, reliable, and completely customizable via your own personal dashboard.</p>
            <div class="btn-group">
            """
    if user:
        html += f"""
                <a href="{url_for('dashboard')}" class="btn">Dashboard</a>
                <a href="{url_for('premium')}" class="btn btn-secondary">Premium</a>
                <a href="{url_for('logout')}" class="btn btn-danger">Logout</a>
            """
    else:
        html += f"""
                <a href="{url_for('login')}" class="btn">
                    <svg class="discord-logo" viewBox="0 0 127.14 96.36" xmlns="http://www.w3.org/2000/svg" fill="currentColor">
                        <path d="M107.7,8.07A105.15,105.15,0,0,0,81.47,0a72.06,72.06,0,0,0-3.36,6.83A99.68,99.68,0,0,0,49,6.83,72.37,72.37,0,0,0,45.64,0A105.89,105.89,0,0,0,19.39,8.09C2.79,32.65-1.71,56.6.54,80.21h0A105.73,105.73,0,0,0,32.71,96.36,77.7,77.7,0,0,0,39.6,85.25a68.42,68.42,0,0,1-10.85-5.18c.91-.66,1.8-1.34,2.66-2a77.15,77.15,0,0,0,64.32,0c.87.71,1.76,1.39,2.66,2a68.68,68.68,0,0,1-10.87,5.22,77,77,0,0,0,6.89,11.1A105.25,105.25,0,0,0,126.6,80.22h0C129.24,52.84,122.09,29.11,107.7,8.07ZM42.45,65.69C36.18,65.69,31,60.55,31,53.88s5-11.81,11.47-11.81S54,47.16,53.89,53.88,48.84,65.69,42.45,65.69Zm42.24,0C78.41,65.69,73.25,60.55,73.25,53.88s5-11.81,11.44-11.81S96.23,47.16,96.12,53.88,91.08,65.69,84.69,65.69Z"/>
                    </svg>
                    Login with Discord
                </a>
                <a href="{url_for('premium')}" class="btn btn-secondary">Premium</a>
                <a href="{url_for('status')}" class="btn btn-secondary">Status</a>
                <a href="{invite_bot_url}" class="btn btn-secondary">Invite Bot</a>
            """
    html += """
            </div>
        </div>
    </body>
    </html>
    """
    return html

@app.route('/premium')
def premium():
    """Premium features page."""
    user = get_discord_user()
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Spectra Premium - Unlock More</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { 
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
                background-color: #0f111a; 
                color: #ffffff; 
                min-height: 100vh;
                background-image: radial-gradient(circle at 50% -20%, #1c2339 0%, #0f111a 100%);
            }
            
            header {
                background: linear-gradient(135deg, #5865F2 0%, #4752c4 100%);
                padding: 40px 20px;
                text-align: center;
                border-bottom: 1px solid rgba(88, 101, 242, 0.3);
            }
            
            header h1 { font-size: 2.5rem; font-weight: 800; margin-bottom: 10px; }
            header p { font-size: 1.1rem; color: rgba(255, 255, 255, 0.9); }
            
            nav {
                display: flex;
                justify-content: center;
                gap: 20px;
                padding: 20px;
                background-color: #1a1d27;
                border-bottom: 1px solid #2b2d31;
            }
            
            nav a {
                color: #94a3b8;
                text-decoration: none;
                font-weight: 600;
                transition: color 0.2s;
            }
            
            nav a:hover { color: #5865F2; }
            
            .container { max-width: 1200px; margin: 0 auto; padding: 60px 20px; }
            
            .hero {
                text-align: center;
                margin-bottom: 80px;
            }
            
            .hero h2 { font-size: 2.5rem; margin-bottom: 16px; }
            .hero p { font-size: 1.25rem; color: #94a3b8; margin-bottom: 32px; }
            
            .features-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
                gap: 24px;
                margin-bottom: 60px;
            }
            
            .feature-card {
                background: linear-gradient(135deg, #1a1d27 0%, #141820 100%);
                border: 1px solid #2b2d31;
                border-radius: 12px;
                padding: 32px;
                transition: all 0.3s ease;
                cursor: pointer;
            }
            
            .feature-card:hover {
                border-color: #5865F2;
                transform: translateY(-4px);
                box-shadow: 0 12px 24px rgba(88, 101, 242, 0.15);
            }
            
            .feature-icon {
                font-size: 2.5rem;
                margin-bottom: 16px;
            }
            
            .feature-card h3 {
                font-size: 1.5rem;
                margin-bottom: 12px;
            }
            
            .feature-card p {
                color: #94a3b8;
                line-height: 1.6;
                margin-bottom: 16px;
            }
            
            .feature-list {
                list-style: none;
                padding-left: 0;
            }
            
            .feature-list li {
                padding: 8px 0;
                color: #cbd5e1;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            
            .feature-list li:before {
                content: "✓";
                color: #5865F2;
                font-weight: bold;
                font-size: 1.2rem;
            }
            
            .category-title {
                font-size: 2rem;
                font-weight: 700;
                margin-top: 60px;
                margin-bottom: 30px;
                color: #e2e8f0;
                padding-bottom: 12px;
                border-bottom: 2px solid #5865F2;
                display: inline-block;
            }
            
            .comparison-section {
                margin-top: 80px;
                padding: 40px;
                background: linear-gradient(135deg, #1a1d27 0%, #141820 100%);
                border: 1px solid #2b2d31;
                border-radius: 12px;
            }
            
            .comparison-grid {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 40px;
                margin-top: 30px;
            }
            
            .comparison-box h4 {
                font-size: 1.3rem;
                margin-bottom: 20px;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            
            .comparison-box ul {
                list-style: none;
            }
            
            .comparison-box li {
                padding: 10px 0;
                border-bottom: 1px solid #2b2d31;
                color: #cbd5e1;
            }
            
            .btn-group {
                display: flex;
                gap: 16px;
                justify-content: center;
                flex-wrap: wrap;
                margin-top: 40px;
            }
            
            .btn {
                display: inline-flex;
                align-items: center;
                gap: 10px;
                padding: 14px 28px;
                background-color: #5865F2;
                color: white;
                text-decoration: none;
                border-radius: 8px;
                font-size: 1rem;
                font-weight: 600;
                transition: all 0.2s ease;
                border: none;
                cursor: pointer;
            }
            
            .btn:hover {
                background-color: #4752c4;
                transform: translateY(-2px);
                box-shadow: 0 4px 20px rgba(88, 101, 242, 0.4);
            }
            
            .btn-secondary {
                background-color: #2b2d31;
                color: #ffffff;
            }
            
            .btn-secondary:hover {
                background-color: #3b3e44;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
            }
            
            @media (max-width: 768px) {
                header h1 { font-size: 1.8rem; }
                .hero h2 { font-size: 1.8rem; }
                .comparison-grid { grid-template-columns: 1fr; gap: 20px; }
                nav { flex-wrap: wrap; gap: 10px; }
            }
        </style>
    </head>
    <body>
        <header>
            <h1>✨ Spectra Premium</h1>
            <p>Unlock powerful features to take your server to the next level</p>
        </header>
        
        <nav>
            <a href="/">← Back to Home</a>
            """
    if user:
        html += f'<a href="{url_for("dashboard")}">Dashboard</a>'
    html += """
        </nav>
        
        <div class="container">
            <div class="hero">
                <h2>Level Up Your Server</h2>
                <p>Premium membership grants access to exclusive features designed to enhance your server's capabilities.</p>
            </div>
            
            <!-- Giveaways Section -->
            <div class="category-title">🎁 Advanced Giveaways</div>
            <div class="features-grid">
                <div class="feature-card">
                    <div class="feature-icon">📊</div>
                    <h3>Unlimited Giveaways</h3>
                    <p>Run as many simultaneous giveaways as you need without restrictions.</p>
                    <ul class="feature-list">
                        <li>No giveaway limits</li>
                        <li>Run concurrent events</li>
                        <li>Schedule future giveaways</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">🎯</div>
                    <h3>Advanced Filtering</h3>
                    <p>Target specific members for more meaningful giveaways.</p>
                    <ul class="feature-list">
                        <li>Filter by roles</li>
                        <li>Minimum account age requirement</li>
                        <li>Exclude specific members</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">📈</div>
                    <h3>Giveaway Analytics</h3>
                    <p>Track and analyze giveaway performance with detailed insights.</p>
                    <ul class="feature-list">
                        <li>Participation history</li>
                        <li>Winner statistics</li>
                        <li>Engagement metrics</li>
                    </ul>
                </div>
            </div>
            
            <!-- Leveling Section -->
            <div class="category-title">⭐ Enhanced Leveling</div>
            <div class="features-grid">
                <div class="feature-card">
                    <div class="feature-icon">🚀</div>
                    <h3>XP Multipliers</h3>
                    <p>Customize XP rates and apply role-based multipliers.</p>
                    <ul class="feature-list">
                        <li>Global XP multipliers</li>
                        <li>Role-specific multipliers</li>
                        <li>Time-based boost events</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">🏆</div>
                    <h3>Custom Level Rewards</h3>
                    <p>Assign custom rewards at specific levels.</p>
                    <ul class="feature-list">
                        <li>Auto-assign roles</li>
                        <li>Custom badges</li>
                        <li>Prize pools</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">📊</div>
                    <h3>Prestige System</h3>
                    <p>Let members reset and advance with prestige levels.</p>
                    <ul class="feature-list">
                        <li>Prestige ranks</li>
                        <li>Exclusive prestige rewards</li>
                        <li>Leaderboard tracking</li>
                    </ul>
                </div>
            </div>
            
            <!-- Moderation Section -->
            <div class="category-title">🛡️ Advanced Moderation</div>
            <div class="features-grid">
                <div class="feature-card">
                    <div class="feature-icon">🚨</div>
                    <h3>Smart Automod</h3>
                    <p>Intelligent automatic moderation with customizable triggers.</p>
                    <ul class="feature-list">
                        <li>Regex pattern matching</li>
                        <li>Custom word lists</li>
                        <li>Spam detection</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">🔐</div>
                    <h3>Raid Protection</h3>
                    <p>Automatically detect and protect against raids.</p>
                    <ul class="feature-list">
                        <li>Member join threshold alerts</li>
                        <li>Auto-lockdown mode</li>
                        <li>Suspicious activity logs</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">📜</div>
                    <h3>Advanced Logging</h3>
                    <p>Comprehensive moderation logs with custom filtering.</p>
                    <ul class="feature-list">
                        <li>Detailed action logs</li>
                        <li>Custom log channels</li>
                        <li>Searchable history</li>
                    </ul>
                </div>
            </div>
            
            <!-- Customization Section -->
            <div class="category-title">🎨 Customization & Branding</div>
            <div class="features-grid">
                <div class="feature-card">
                    <div class="feature-icon">🎭</div>
                    <h3>Custom Messages</h3>
                    <p>Fully customize bot responses and embeds.</p>
                    <ul class="feature-list">
                        <li>Custom welcome messages</li>
                        <li>Branded embeds</li>
                        <li>Custom command responses</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">🌈</div>
                    <h3>Theme Customization</h3>
                    <p>Personalize the look and feel of bot interactions.</p>
                    <ul class="feature-list">
                        <li>Custom embed colors</li>
                        <li>Logo customization</li>
                        <li>Branded dashboard</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">⚙️</div>
                    <h3>Advanced Settings</h3>
                    <p>Fine-tune every aspect of bot behavior.</p>
                    <ul class="feature-list">
                        <li>Module toggles</li>
                        <li>Per-channel settings</li>
                        <li>Advanced permissions</li>
                    </ul>
                </div>
            </div>
            
            <!-- Analytics Section -->
            <div class="category-title">📊 Analytics & Insights</div>
            <div class="features-grid">
                <div class="feature-card">
                    <div class="feature-icon">📈</div>
                    <h3>Server Analytics</h3>
                    <p>Comprehensive server statistics and insights.</p>
                    <ul class="feature-list">
                        <li>Member growth trends</li>
                        <li>Activity heatmaps</li>
                        <li>Demographic data</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">🔗</div>
                    <h3>API Access</h3>
                    <p>Programmatic access to bot data and functions.</p>
                    <ul class="feature-list">
                        <li>RESTful API</li>
                        <li>Webhooks support</li>
                        <li>Custom integrations</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">📋</div>
                    <h3>Custom Reports</h3>
                    <p>Generate detailed reports on server activity.</p>
                    <ul class="feature-list">
                        <li>Scheduled reports</li>
                        <li>Export data (CSV/JSON)</li>
                        <li>Custom metrics</li>
                    </ul>
                </div>
            </div>
            
            <!-- Support Section -->
            <div class="category-title">💬 Premium Support</div>
            <div class="features-grid">
                <div class="feature-card">
                    <div class="feature-icon">⚡</div>
                    <h3>Priority Support</h3>
                    <p>Get faster response times from our support team.</p>
                    <ul class="feature-list">
                        <li>24/7 support response</li>
                        <li>Dedicated support channel</li>
                        <li>Direct access to team</li>
                    </ul>
                </div>
                
                <div class="feature-card">
                    <div class="feature-icon">🎓</div>
                    <h3>Setup Assistance</h3>
                    <p>Get personalized help setting up your premium features.</p>
                    <ul class="feature-list">
                        <li>Configuration help</li>
                        <li>Best practices guide</li>
                        <li>Optimization tips</li>
                    </ul>
                </div>
            </div>
            
            <!-- Comparison Section -->
            <div class="comparison-section">
                <h2 style="text-align: center; margin-bottom: 10px;">Free vs Premium</h2>
                <p style="text-align: center; color: #94a3b8; margin-bottom: 30px;">Compare features and see what Premium offers</p>
                
                <div class="comparison-grid">
                    <div class="comparison-box">
                        <h4>📱 Free Plan</h4>
                        <ul>
                            <li>✓ Basic giveaways (limited)</li>
                            <li>✓ Basic leveling system</li>
                            <li>✓ Standard automod</li>
                            <li>✓ Basic dashboard</li>
                            <li>✓ Community support</li>
                            <li>✗ No XP multipliers</li>
                            <li>✗ No advanced analytics</li>
                            <li>✗ No API access</li>
                        </ul>
                    </div>
                    
                    <div class="comparison-box">
                        <h4 style="color: #5865F2;">✨ Premium Plan</h4>
                        <ul>
                            <li>✓ Unlimited giveaways</li>
                            <li>✓ Advanced leveling with multipliers</li>
                            <li>✓ Smart automod with regex</li>
                            <li>✓ Advanced dashboard</li>
                            <li>✓ Priority support 24/7</li>
                            <li>✓ XP multipliers & prestige</li>
                            <li>✓ Complete analytics suite</li>
                            <li>✓ Full API access</li>
                        </ul>
                    </div>
                </div>
            </div>
            
            <!-- CTA -->
            <div style="text-align: center; margin-top: 60px;">
                <h2 style="margin-bottom: 20px;">Ready to go Premium?</h2>
                <p style="color: #94a3b8; margin-bottom: 30px; font-size: 1.1rem;">Coming soon! Pricing details will be announced shortly.</p>
                <div class="btn-group">
                    <a href="/" class="btn">Back to Home</a>
                    """
    if user:
        html += f'<a href="{url_for("dashboard")}" class="btn btn-secondary">Dashboard</a>'
    html += """
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

@app.route('/status')
def status_page():
    """Public status page showing bot clusters and shards."""
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Spectra - Bot Status</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
                color: #333;
            }
            
            .container {
                max-width: 1200px;
                margin: 0 auto;
            }
            
            header {
                text-align: center;
                color: white;
                margin-bottom: 40px;
            }
            
            h1 {
                font-size: 2.5em;
                margin-bottom: 10px;
                text-shadow: 2px 2px 4px rgba(0,0,0,0.2);
            }
            
            .subtitle {
                font-size: 1.1em;
                opacity: 0.9;
                margin-bottom: 20px;
            }
            
            .refresh-info {
                font-size: 0.9em;
                opacity: 0.8;
            }
            
            .search-section {
                background: white;
                padding: 30px;
                border-radius: 12px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                margin-bottom: 30px;
            }
            
            .search-box {
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
            }
            
            .search-box input {
                flex: 1;
                padding: 12px 15px;
                border: 2px solid #e0e0e0;
                border-radius: 8px;
                font-size: 1em;
                transition: border-color 0.3s;
            }
            
            .search-box input:focus {
                outline: none;
                border-color: #667eea;
            }
            
            .search-box button {
                padding: 12px 30px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-weight: 600;
                transition: transform 0.2s, box-shadow 0.2s;
            }
            
            .search-box button:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 20px rgba(102, 126, 234, 0.4);
            }
            
            .search-box button:active {
                transform: translateY(0);
            }
            
            .search-results {
                margin-top: 20px;
                min-height: 40px;
            }
            
            .guild-result {
                background: #f5f5f5;
                padding: 15px;
                border-radius: 8px;
                border-left: 4px solid #667eea;
                margin-bottom: 10px;
            }
            
            .guild-result.not-found {
                border-left-color: #ff6b6b;
                color: #d32f2f;
            }
            
            .guild-result.found {
                border-left-color: #4caf50;
            }
            
            .guild-name {
                font-weight: 600;
                margin-bottom: 5px;
            }
            
            .guild-cluster {
                color: #666;
                font-size: 0.95em;
            }
            
            .cluster-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
                margin-top: 20px;
            }
            
            .cluster-card {
                background: white;
                border-radius: 12px;
                padding: 25px;
                box-shadow: 0 5px 20px rgba(0,0,0,0.1);
                transition: transform 0.3s, box-shadow 0.3s;
            }
            
            .cluster-card:hover {
                transform: translateY(-5px);
                box-shadow: 0 10px 30px rgba(0,0,0,0.15);
            }
            
            .cluster-header {
                display: flex;
                align-items: center;
                margin-bottom: 20px;
                gap: 10px;
            }
            
            .cluster-number {
                font-size: 1.8em;
                font-weight: 700;
                color: #667eea;
                width: 50px;
                height: 50px;
                display: flex;
                align-items: center;
                justify-content: center;
                background: #f0f0ff;
                border-radius: 50%;
            }
            
            .cluster-title {
                flex: 1;
            }
            
            .cluster-title h2 {
                font-size: 1.3em;
                margin-bottom: 5px;
            }
            
            .cluster-status {
                display: flex;
                align-items: center;
                gap: 6px;
                font-size: 0.9em;
                font-weight: 600;
            }
            
            .status-indicator {
                width: 12px;
                height: 12px;
                border-radius: 50%;
                animation: pulse 2s infinite;
            }
            
            .status-indicator.online {
                background: #4caf50;
            }
            
            .status-indicator.offline {
                background: #ff6b6b;
                animation: none;
            }
            
            .status-indicator.partial {
                background: #ffa726;
            }
            
            @keyframes pulse {
                0%, 100% {
                    opacity: 1;
                }
                50% {
                    opacity: 0.5;
                }
            }
            
            .cluster-stats {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 15px;
                margin-bottom: 20px;
                padding-bottom: 20px;
                border-bottom: 1px solid #e0e0e0;
            }
            
            .stat {
                text-align: center;
            }
            
            .stat-value {
                font-size: 1.8em;
                font-weight: 700;
                color: #667eea;
                display: block;
            }
            
            .stat-label {
                font-size: 0.85em;
                color: #999;
                margin-top: 5px;
            }
            
            .shards-list {
                background: #f9f9f9;
                padding: 15px;
                border-radius: 8px;
            }
            
            .shards-label {
                font-weight: 600;
                margin-bottom: 10px;
                color: #333;
            }
            
            .shard-item {
                display: inline-block;
                background: white;
                padding: 6px 12px;
                margin: 4px;
                border-radius: 6px;
                border: 1px solid #e0e0e0;
                font-size: 0.85em;
                color: #555;
            }
            
            .footer {
                text-align: center;
                color: white;
                margin-top: 40px;
                opacity: 0.8;
            }
            
            .error {
                background: #ffebee;
                color: #c62828;
                padding: 15px;
                border-radius: 8px;
                margin-top: 10px;
                border-left: 4px solid #c62828;
            }
            
            .success {
                background: #e8f5e9;
                color: #2e7d32;
                padding: 15px;
                border-radius: 8px;
                margin-top: 10px;
                border-left: 4px solid #4caf50;
            }
            
            @media (max-width: 768px) {
                h1 {
                    font-size: 1.8em;
                }
                
                .search-box {
                    flex-direction: column;
                }
                
                .cluster-grid {
                    grid-template-columns: 1fr;
                }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>⚡ Spectra Bot Status</h1>
                <p class="subtitle">Check bot clusters, shards, and server status</p>
                <p class="refresh-info">This page refreshes every 15 seconds</p>
            </header>
            
            <div class="search-section">
                <h2 style="margin-bottom: 20px;">Search Your Server</h2>
                <p style="color: #666; margin-bottom: 15px;">Enter your Discord server ID to see which cluster it's running on</p>
                
                <div class="search-box">
                    <input type="text" id="guildIdInput" placeholder="Enter your Discord server ID..." />
                    <button onclick="searchGuild()">Search</button>
                </div>
                
                <div id="searchResults" class="search-results"></div>
            </div>
            
            <div style="margin-bottom: 30px;">
                <h2 style="color: white; margin-bottom: 20px;">📊 Overview</h2>
                <div id="clustersContainer" class="cluster-grid">
                    <div style="text-align: center; padding: 40px; color: white;">
                        <p>Loading cluster information...</p>
                    </div>
                </div>
            </div>
            
            <footer class="footer">
                <p>&copy; 2026 Spectra Bot | Status Page</p>
            </footer>
        </div>
        
        <script>
            let statusData = {};
            
            async function loadStatus() {
                try {
                    const response = await fetch('/api/status');
                    statusData = await response.json();
                    renderClusters();
                } catch (error) {
                    console.error('Error loading status:', error);
                    document.getElementById('clustersContainer').innerHTML = 
                        '<div class="error">Failed to load status information</div>';
                }
            }
            
            function renderClusters() {
                const container = document.getElementById('clustersContainer');
                container.innerHTML = '';
                
                if (!statusData.clusters || statusData.clusters.length === 0) {
                    container.innerHTML = '<div class="error">No cluster information available</div>';
                    return;
                }
                
                statusData.clusters.forEach(cluster => {
                    const card = document.createElement('div');
                    card.className = 'cluster-card';
                    
                    const status = cluster.status || 'online';
                    const statusClass = status === 'online' ? 'online' : (status === 'offline' ? 'offline' : 'partial');
                    
                    card.innerHTML = `
                        <div class="cluster-header">
                            <div class="cluster-number">${cluster.id}</div>
                            <div class="cluster-title">
                                <h2>${cluster.name}</h2>
                                <div class="cluster-status">
                                    <div class="status-indicator ${statusClass}"></div>
                                    <span>${status.toUpperCase()}</span>
                                </div>
                            </div>
                        </div>
                        
                        <div class="cluster-stats">
                            <div class="stat">
                                <span class="stat-value">${cluster.shard_count}</span>
                                <div class="stat-label">Shards</div>
                            </div>
                            <div class="stat">
                                <span class="stat-value">${cluster.guild_count || 0}</span>
                                <div class="stat-label">Servers</div>
                            </div>
                        </div>
                        
                        <div class="shards-list">
                            <div class="shards-label">Shards (${cluster.shard_ids.length})</div>
                            <div>${cluster.shard_ids.map(id => `<span class="shard-item">Shard ${id}</span>`).join('')}</div>
                        </div>
                    `;
                    
                    container.appendChild(card);
                });
            }
            
            async function searchGuild() {
                const guildId = document.getElementById('guildIdInput').value.trim();
                const resultsDiv = document.getElementById('searchResults');
                
                if (!guildId) {
                    resultsDiv.innerHTML = '<div class="error">Please enter a server ID</div>';
                    return;
                }
                
                resultsDiv.innerHTML = '<p style="color: #666;">Searching...</p>';
                
                try {
                    const response = await fetch(`/api/status/guild/${guildId}`);
                    const data = await response.json();
                    
                    if (data.success) {
                        resultsDiv.innerHTML = `
                            <div class="guild-result found">
                                <div class="guild-name">✓ ${data.guild_name}</div>
                                <div class="guild-cluster">
                                    Running on <strong>Cluster ${data.cluster_id}</strong> (Shard ${data.shard_id})
                                </div>
                            </div>
                        `;
                    } else {
                        resultsDiv.innerHTML = `
                            <div class="guild-result not-found">
                                <div class="guild-name">✗ Server not found</div>
                                <div class="guild-cluster">
                                    Make sure you entered the correct server ID and the bot is in your server.
                                </div>
                            </div>
                        `;
                    }
                } catch (error) {
                    resultsDiv.innerHTML = '<div class="error">Error searching for server</div>';
                    console.error('Search error:', error);
                }
            }
            
            // Allow Enter key to search
            document.getElementById('guildIdInput').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    searchGuild();
                }
            });
            
            // Load status on page load and refresh every 15 seconds
            loadStatus();
            setInterval(loadStatus, 15000);
        </script>
    </body>
    </html>
    """
    return render_template_string(html)

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
        admin = guild.get('owner', False)
        guild_icon = guild.get('icon')
        
        # Build icon HTML
        if guild_icon:
            icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{guild_icon}.png?size=128"
            icon_html = f'<img src="{icon_url}" alt="{guild_name}" style="width: 100%; height: 100%; border-radius: 16px; object-fit: cover;">'
        else:
            icon_html = f'<span>{guild_name[0].upper()}</span>'
        
        if admin or (guild.get('permissions') & 0x8):
            guild_cards += f"""
            <a href="{url_for('guild_settings', guild_id=guild_id)}" class="guild-card">
                <div class="guild-avatar">
                    {icon_html}
                </div>
                <div class="guild-info">
                    <div class="guild-name">{guild_name}</div>
                    <div class="guild-role">{'👑 Server Owner' if admin else 'Administrator'}</div>
                </div>
                <div class="guild-arrow">→</div>
            </a>
            """
    
    owner_section = ""
    if is_owner:
        owner_section = f"""
        <div class="owner-panel">
            <div class="owner-text">
                <strong>Spectra Administrator</strong>
                <p>Global management tools enabled.</p>
            </div>
            <a href="{url_for('owner_panel')}" class="btn-owner">Open Panel</a>
        </div>
        """
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard | Spectra</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { 
                font-family: 'Inter', sans-serif; 
                background-color: #0f111a; 
                color: #dcddde; 
                min-height: 100vh; 
            }
            .nav { 
                padding: 20px 40px; 
                display: flex; 
                justify-content: space-between; 
                align-items: center; 
                background: #11131e;
                border-bottom: 1px solid #1e2235;
            }
            .nav h2 { font-size: 1.5rem; color: #fff; font-weight: 700; }
            .nav-user { display: flex; align-items: center; gap: 15px; }
            .logout-link { color: #94a3b8; text-decoration: none; font-size: 0.9rem; }
            .logout-link:hover { color: #fff; }

            .container { max-width: 1100px; margin: 60px auto; padding: 0 20px; }
            .header-area { margin-bottom: 40px; }
            .header-area h1 { font-size: 2.2rem; color: #fff; margin-bottom: 10px; }
            .header-area p { color: #94a3b8; }

            .owner-panel { 
                background: linear-gradient(90deg, #5865F2, #7289da); 
                padding: 20px; 
                border-radius: 12px; 
                margin-bottom: 40px; 
                display: flex; 
                justify-content: space-between; 
                align-items: center;
                color: white;
            }
            .btn-owner { background: white; color: #5865F2; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem; }

            .guilds-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            .guild-card { 
                background: #161925; 
                border: 1px solid #1e2235;
                padding: 24px; 
                border-radius: 12px; 
                display: flex; 
                align-items: center; 
                text-decoration: none; 
                color: inherit; 
                transition: all 0.2s ease;
            }
            .guild-card:hover { border-color: #5865F2; background: #1c2030; transform: translateY(-2px); }
            .guild-avatar { 
                width: 60px; height: 60px; background: #2b2d31; border-radius: 16px; 
                display: flex; align-items: center; justify-content: center; 
                font-size: 1.5rem; font-weight: bold; color: #5865F2; margin-right: 20px;
            }
            .guild-info { flex: 1; }
            .guild-name { font-size: 1.1rem; font-weight: 600; color: #fff; margin-bottom: 4px; }
            .guild-role { font-size: 0.85rem; color: #94a3b8; }
            .guild-arrow { color: #3f4461; font-size: 1.2rem; }

            @media (max-width: 768px) { .guilds-grid { grid-template-columns: 1fr; } }
        </style>
    </head>
    <body>
        <div class="nav">
            <h2>Spectra</h2>
            <div class="nav-user">
                <span>""" + user['username'] + """</span>
                <a href=\"""" + url_for('logout') + """\" class="logout-link">Sign Out</a>
            </div>
        </div>
        <div class="container">
            """ + owner_section + """
            <div class="header-area">
                <h1>Select a Server</h1>
                <p>Choose the server you want to configure and manage.</p>
            </div>
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
            body {{ 
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
                background-color: #0f111a; 
                color: #dcddde; 
                min-height: 100vh; 
            }}
            
            .nav {{ 
                padding: 20px 40px; 
                display: flex; 
                justify-content: space-between; 
                align-items: center; 
                background: #11131e;
                border-bottom: 1px solid #1e2235;
            }}
            .nav h2 {{ font-size: 1.5rem; color: #fff; font-weight: 700; }}
            .nav-user {{ display: flex; align-items: center; gap: 15px; }}
            .nav-link {{ color: #94a3b8; text-decoration: none; font-size: 0.9rem; cursor: pointer; }}
            .nav-link:hover {{ color: #fff; }}

            .wrapper {{ display: flex; height: calc(100vh - 65px); }}
            
            .sidebar {{ 
                width: 250px; 
                background: #11131e;
                padding: 20px 0; 
                border-right: 1px solid #1e2235;
                overflow-y: auto; 
            }}
            .sidebar-item {{ 
                padding: 12px 20px; 
                color: #94a3b8; 
                cursor: pointer; 
                transition: all 0.3s; 
                display: flex; 
                align-items: center; 
                text-decoration: none; 
            }}
            .sidebar-item:hover {{ background: rgba(88, 101, 242, 0.1); color: #5865F2; }}
            .sidebar-item.active {{ 
                background: rgba(88, 101, 242, 0.2); 
                color: #5865F2; 
                border-left: 3px solid #5865F2; 
                padding-left: 17px;
            }}
            .sidebar-item-icon {{ margin-right: 10px; font-size: 1.2em; }}
            
            .content {{ flex: 1; overflow-y: auto; padding: 40px; }}
            
            .content h2 {{ color: #fff; margin-bottom: 20px; font-size: 2em; font-weight: 700; }}
            
            .premium-badge {{ display: inline-block; background: #f47fff; color: #fff; padding: 8px 16px; border-radius: 6px; font-size: 0.9em; margin-bottom: 20px; font-weight: 600; }}
            
            .section {{ 
                background: #161925; 
                padding: 24px; 
                border-radius: 12px; 
                margin-bottom: 20px; 
                border: 1px solid #1e2235;
            }}
            .section h3 {{ color: #fff; margin-bottom: 15px; font-weight: 600; }}
            .section p {{ color: #94a3b8; margin-bottom: 10px; }}
            .section code {{ background: #0f111a; padding: 2px 6px; border-radius: 4px; color: #5865F2; }}
            
            .form-group {{ margin-bottom: 15px; }}
            .form-group label {{ display: block; color: #94a3b8; margin-bottom: 8px; font-weight: 500; }}
            .form-group input, .form-group textarea, .form-group select {{ 
                width: 100%; 
                padding: 10px 12px; 
                background: #0f111a; 
                color: #dcddde; 
                border: 1px solid #1e2235; 
                border-radius: 6px; 
                font-family: inherit; 
                transition: all 0.2s;
            }}
            .form-group input:focus, .form-group textarea:focus, .form-group select:focus {{ 
                outline: none; 
                border-color: #5865F2; 
                box-shadow: 0 0 0 3px rgba(88, 101, 242, 0.1); 
            }}
            
            .form-row {{ display: flex; gap: 10px; }}
            .form-row input {{ flex: 1; }}
            
            .btn {{ 
                display: inline-block; 
                padding: 10px 20px; 
                background: #5865F2; 
                color: white; 
                border: none; 
                border-radius: 6px; 
                cursor: pointer; 
                font-size: 1em; 
                font-weight: 600;
                transition: all 0.3s; 
            }}
            .btn:hover {{ background: #4752c4; transform: translateY(-1px); }}
            .btn-danger {{ background: #da373c; }}
            .btn-danger:hover {{ background: #a12829; }}
            .btn-small {{ padding: 8px 12px; font-size: 0.9em; }}
            
            .status {{ padding: 10px 15px; border-radius: 6px; margin-bottom: 15px; display: none; }}
            .status.show {{ display: block; }}
            .status.success {{ background: #43b581; color: white; }}
            .status.error {{ background: #f04747; color: white; }}
            
            .word-list {{ background: #0f111a; padding: 12px; border-radius: 6px; margin-top: 10px; max-height: 300px; overflow-y: auto; border: 1px solid #1e2235; }}
            .word-list-item {{ 
                padding: 8px 12px; 
                background: #161925; 
                margin: 6px 0; 
                border-radius: 4px; 
                display: flex; 
                justify-content: space-between; 
                align-items: center; 
                border: 1px solid #1e2235;
            }}
            .word-list-item button {{ 
                padding: 4px 8px; 
                background: #f04747; 
                color: white; 
                border: none; 
                border-radius: 3px; 
                cursor: pointer; 
                font-size: 0.85em;
            }}
            
            .giveaway-list {{ background: #0f111a; padding: 12px; border-radius: 6px; margin-top: 10px; max-height: 300px; overflow-y: auto; border: 1px solid #1e2235; }}
            .giveaway-item {{ 
                padding: 12px; 
                background: #161925; 
                margin: 8px 0; 
                border-radius: 6px; 
                border: 1px solid #1e2235;
            }}
            .giveaway-prize {{ font-weight: 600; color: #fff; }}
            .giveaway-info {{ font-size: 0.9em; color: #94a3b8; margin-top: 4px; }}
            
            #automod-tab, #giveaway-tab, #premium-tab, #leveling-tab {{ display: none; }}
            #automod-tab.active, #giveaway-tab.active, #premium-tab.active, #leveling-tab.active {{ display: block; }}
        </style>
    </head>
    <body>
        <div class="nav">
            <h2>Spectra - {guild['name']}</h2>
            <div class="nav-user">
                <a href="{url_for('dashboard')}" class="nav-link">← Back to Dashboard</a>
                <a href="{url_for('logout')}" class="nav-link">Sign Out</a>
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
                        <p>Add words or phrases that should be automatically blocked in this server.</p>
                        <div class="form-group">
                            <label>Add Word or Phrase</label>
                            <div style="display: flex; gap: 10px;">
                                <input type="text" id="newWord" placeholder="Enter word to block..." style="flex: 1;">
                                <button class="btn" onclick="addBlockedWord('{guild_id}')">Add</button>
                            </div>
                        </div>
                        <div class="word-list" id="wordList"></div>
                    </div>
                </div>
                
                <!-- Giveaway Tab -->
                <div id="giveaway-tab">
                    <h2>🎁 Giveaway Manager</h2>
                    <div class="section">
                        <h3>Create Guild Giveaway</h3>
                        <p>Create and manage giveaways for this specific guild.</p>
                        <div class="status" id="giveawayStatus"></div>
                        <div class="form-group">
                            <label>Prize</label>
                            <input type="text" id="giveawayPrize" placeholder="e.g., $50 Gift Card, Nitro Boost">
                        </div>
                        <div class="form-row">
                            <div class="form-group" style="flex: 1;">
                                <label>Duration (Minutes)</label>
                                <input type="number" id="giveawayDuration" value="60" min="1">
                            </div>
                            <div class="form-group" style="flex: 1;">
                                <label>Number of Winners</label>
                                <input type="number" id="giveawayWinners" value="1" min="1">
                            </div>
                        </div>
                        <button class="btn" onclick="createGuildGiveaway('{guild_id}')">Start Giveaway</button>
                    </div>
                    
                    <div class="section">
                        <h3>Active Giveaways</h3>
                        <div class="giveaway-list" id="giveawayList">
                            <p style="color: #94a3b8; text-align: center; padding: 20px;">No active giveaways</p>
                        </div>
                    </div>
                </div>
                
                <!-- Premium Tab -->
                <div id="premium-tab">
                    <h2>⭐ Premium Status</h2>
                    <div class="section">
                        <h3>Current Status</h3>
                        {'<span class="premium-badge">✨ ' + premium_type + ' Premium Active ✨</span>' if is_premium else '<span class="premium-badge" style="background: #72767d;">Standard Access</span>'}
                        <p style="margin-top: 15px;">Server ID: <code>{guild_id}</code></p>
                        <p>Use the <code>/license_activate</code> command in Discord to activate a premium license.</p>
                    </div>
                </div>
                
                <!-- Leveling Tab -->
                <div id="leveling-tab">
                    <h2>📊 Leveling System</h2>
                    <div class="section">
                        <h3>Leveling Configuration</h3>
                        <p>Customize how users earn XP in your server.</p>
                        <div class="form-group">
                            <label>XP per Message</label>
                            <div class="form-row">
                                <input type="number" id="xpMin" value="15" min="1"> 
                                <span style="padding: 10px; color: #94a3b8;">to</span>
                                <input type="number" id="xpMax" value="25" min="1">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>XP Needed per Level</label>
                            <input type="number" id="xpPerLevel" value="100" min="1">
                        </div>
                        <button class="btn" onclick="saveLevelingSettings('{guild_id}')">Save Settings</button>
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
                
                // Load giveaways when switching to that tab
                if (tabName === 'giveaway') {{
                    loadGuildGiveaways('{guild_id}');
                }}
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
                    if (data.words.length === 0) {{
                        list.innerHTML = '<p style="color: #94a3b8; text-align: center; padding: 20px;">No blocked words yet</p>';
                    }} else {{
                        data.words.forEach(word => {{
                            const item = document.createElement('div');
                            item.className = 'word-list-item';
                            item.innerHTML = `<span>${{word}}</span><button class="btn-small" onclick="removeBlockedWord('{guild_id}', '${{word}}')">Remove</button>`;
                            list.appendChild(item);
                        }});
                    }}
                }});
            }}
            
            function createGuildGiveaway(guildId) {{
                const prize = document.getElementById('giveawayPrize').value.trim();
                const duration = parseInt(document.getElementById('giveawayDuration').value);
                const winners = parseInt(document.getElementById('giveawayWinners').value);
                
                const statusEl = document.getElementById('giveawayStatus');
                
                if (!prize) {{
                    statusEl.className = 'status show error';
                    statusEl.textContent = 'Please enter a prize';
                    return;
                }}
                
                statusEl.className = '';
                
                fetch(`/api/guild/${{guildId}}/giveaway/create`, {{
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
                        statusEl.className = 'status show success';
                        statusEl.textContent = 'Giveaway created successfully!';
                        document.getElementById('giveawayPrize').value = '';
                        document.getElementById('giveawayDuration').value = '60';
                        document.getElementById('giveawayWinners').value = '1';
                        setTimeout(() => loadGuildGiveaways(guildId), 1000);
                    }} else {{
                        statusEl.className = 'status show error';
                        statusEl.textContent = 'Error: ' + data.error;
                    }}
                }})
                .catch(err => {{
                    statusEl.className = 'status show error';
                    statusEl.textContent = 'Error: ' + err;
                }});
            }}
            
            function loadGuildGiveaways(guildId) {{
                fetch(`/api/guild/${{guildId}}/giveaway/list`)
                .then(r => r.json())
                .then(data => {{
                    const list = document.getElementById('giveawayList');
                    list.innerHTML = '';
                    if (data.giveaways.length === 0) {{
                        list.innerHTML = '<p style="color: #94a3b8; text-align: center; padding: 20px;">No active giveaways</p>';
                    }} else {{
                        data.giveaways.forEach(ga => {{
                            const item = document.createElement('div');
                            item.className = 'giveaway-item';
                            item.innerHTML = `
                                <div class="giveaway-prize">${{ga.prize}}</div>
                                <div class="giveaway-info">👥 ${{ga.winner_count}} winner(s) • 📝 ${{ga.entries}} entries • ⏱️ ${{ga.time_left}}</div>
                            `;
                            list.appendChild(item);
                        }});
                    }}
                }});
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

@app.route('/api/guild/<guild_id>/giveaway/create', methods=['POST'])
@require_login
def create_guild_giveaway(guild_id):
    """Create a giveaway for a specific guild."""
    data = request.json
    prize = data.get('prize', '').strip()
    duration = int(data.get('duration_minutes', 60))
    winners = int(data.get('winner_count', 1))
    
    if not prize or duration <= 0 or winners <= 0:
        return jsonify({'success': False, 'error': 'Invalid giveaway data'}), 400
    
    # Verify user has admin access to this guild
    user = get_discord_user()
    guild = next((g for g in user.get('guilds', []) if g['id'] == guild_id), None)
    if not guild:
        return jsonify({'success': False, 'error': 'Guild not found'}), 404
    
    admin = guild.get('owner', False) or (guild.get('permissions') & 0x8)
    if not admin:
        return jsonify({'success': False, 'error': 'Insufficient permissions'}), 403
    
    # Create in Firestore with guild_id stored
    if not DB:
        return jsonify({'success': False, 'error': 'Database not available'}), 500
    
    try:
        giveaway_id = str(uuid.uuid4())
        end_time = time.time() + (duration * 60)
        
        giveaway_data = {
            'prize': prize,
            'duration_minutes': duration,
            'winner_count': winners,
            'channel_id': 0,
            'guild_id': guild_id,
            'host_id': int(user['id']),
            'created_at': time.time(),
            'end_time': end_time,
            'entries': []
        }
        
        DB.collection('giveaways').document(giveaway_id).set(giveaway_data)
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error creating guild giveaway: {e}")
        return jsonify({'success': False, 'error': 'Failed to create giveaway'}), 500

@app.route('/api/guild/<guild_id>/giveaway/list', methods=['GET'])
@require_login
def get_guild_giveaways(guild_id):
    """Get active giveaways for a specific guild."""
    if not DB:
        return jsonify({'giveaways': []})
    
    try:
        docs = DB.collection('giveaways').where('guild_id', '==', guild_id).stream()
        giveaways = []
        for doc in docs:
            ga = doc.to_dict()
            end_time = ga.get('end_time', 0)
            time_left = max(0, end_time - time.time())
            
            if time_left > 0:  # Only include active giveaways
                hours_left = int(time_left / 3600)
                minutes_left = int((time_left % 3600) / 60)
                
                giveaways.append({
                    'prize': ga.get('prize', 'Unknown'),
                    'winner_count': ga.get('winner_count', 1),
                    'entries': len(ga.get('entries', [])),
                    'time_left': f"{hours_left}h {minutes_left}m"
                })
        
        return jsonify({'giveaways': sorted(giveaways, key=lambda x: x['prize'])})
    except Exception as e:
        print(f"Error fetching guild giveaways: {e}")
        return jsonify({'giveaways': []})

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
# Helper Functions - Status & Cluster Management
# ==============================================================================

def get_cluster_info(bot_token):
    """Get current cluster/shard information by fetching bot's guilds."""
    headers = {'Authorization': f'Bot {bot_token}'}
    try:
        r = requests.get('https://discord.com/api/v10/users/@me/guilds', headers=headers)
        r.raise_for_status()
        guilds = r.json()
        guild_count = len(guilds)
    except Exception as e:
        print(f"Warning: Could not fetch guild count for status page. Error: {e}")
        guild_count = 0

    cluster_info = {
        'id': 0,
        'name': 'Default Cluster',
        'status': 'online',
        'shard_ids': [0],
        'shard_count': 1,
        'guild_count': guild_count
    }
    return cluster_info

def load_guild_cache():
    """Load guild names from guild_cache.json file."""
    guild_cache = {}
    try:
        if os.path.exists('guild_cache.json'):
            with open('guild_cache.json', 'r') as f:
                guild_cache = {int(k): v for k, v in json.load(f).items()}
    except Exception as e:
        print(f"Error loading guild cache: {e}")
    return guild_cache

def calculate_shard_for_guild(guild_id):
    """Calculate which shard a guild belongs to.
    
    Args:
        guild_id: Discord guild ID
        
    Returns:
        dict: {'cluster_id': int, 'shard_id': int} or None if not found
    """
    guild_id_int = int(guild_id)
    # Single shard formula: shard_id = (guild_id >> 22) % shard_count
    # With 1 shard, all guilds are on shard 0
    shard_id = (guild_id_int >> 22) % 1
    cluster_id = 0
    
    return {'cluster_id': cluster_id, 'shard_id': shard_id}

# ==============================================================================
# API Routes - Status (Public - No Authentication)
# ==============================================================================

@app.route('/api/status')
def api_status():
    """Get all cluster and shard information (public endpoint)."""
    bot_token = os.environ.get('DISCORD_TOKEN')
    if not bot_token:
        return jsonify({'success': False, 'error': 'Bot token not configured on server'}), 500
        
    try:
        cluster = get_cluster_info(bot_token)
        return jsonify({
            'success': True,
            'clusters': [cluster],
            'total_shards': cluster['shard_count'],
            'total_guilds': cluster['guild_count'],
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
@app.route('/api/status/guild/<guild_id>')
def api_status_guild(guild_id):
    """Search for a guild and return its cluster information (public endpoint)."""
    bot_token = os.environ.get('DISCORD_TOKEN')
    if not bot_token:
        return jsonify({'success': False, 'error': 'Bot token not configured on server'}), 500

    try:
        guild_id_int = int(guild_id)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid guild ID format'}), 400

    headers = {'Authorization': f'Bot {bot_token}'}
    try:
        # Check if the bot is in the guild by trying to fetch it
        r = requests.get(f'https://discord.com/api/v10/guilds/{guild_id_int}', headers=headers)
        
        if r.status_code == 404:
            return jsonify({'success': False, 'error': 'Guild not found or bot is not a member'}), 404
        
        r.raise_for_status()
        guild_data = r.json()
        guild_name = guild_data.get('name', f"Guild {guild_id}")

        shard_info = calculate_shard_for_guild(guild_id_int)
        
        return jsonify({
            'success': True,
            'guild_id': guild_id,
            'guild_name': guild_name,
            'cluster_id': shard_info['cluster_id'],
            'shard_id': shard_info['shard_id'],
            'status': 'online'
        })
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return jsonify({'success': False, 'error': 'Guild not found or bot is not a member'}), 404
        return jsonify({'success': False, 'error': f'Discord API error: {e.response.text}'}), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

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