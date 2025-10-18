import json
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, render_template_string
from io import BytesIO
import base64
from collections import Counter
import time
from datetime import datetime

# --- Configuration ---
LEVELS_FILE = 'levels.json'
GIVEAWAYS_FILE = 'giveaways.json'
CONFIG_FILE = 'config.json'
# --- NEW FILE NAME ---
USER_CACHE_FILE = 'user_cache.json'
# ---------------------
HOST = '127.0.0.1' # Localhost
PORT = 5000

app = Flask(__name__)

# ==============================================================================
# DATA LOADING & STATS FUNCTIONS (MODIFIED)
# ==============================================================================

def load_local_data():
    """Loads all data from JSON files for dashboard use."""
    stats = {
        'total_users': 0,
        'total_xp': 0,
        'active_giveaways': 0,
        'total_giveaway_entries': 0,
        'top_5_xp': [],
        'top_5_entries': [],
        'giveaway_durations': []
    }
    
    # --- Load User Cache ---
    user_cache = {}
    if os.path.exists(USER_CACHE_FILE):
        try:
            with open(USER_CACHE_FILE, 'r') as f:
                user_cache = json.load(f)
        except Exception:
            pass # Ignore if file is corrupted/empty
            
    stats['user_cache'] = user_cache
    # -----------------------

    # Load Levels Data
    if os.path.exists(LEVELS_FILE):
        with open(LEVELS_FILE, 'r') as f:
            levels_db = json.load(f)
            stats['total_users'] = len(levels_db)
            
            # Calculate total XP and get top users
            xp_data = {uid: data.get('xp', 0) for uid, data in levels_db.items()}
            stats['total_xp'] = sum(xp_data.values())
            
            top_xp_items = sorted(xp_data.items(), key=lambda item: item[1], reverse=True)[:5]
            stats['top_5_xp'] = [(uid, xp) for uid, xp in top_xp_items]

    # Load Giveaways Data
    if os.path.exists(GIVEAWAYS_FILE):
        # ... (Giveaway loading logic remains the same) ...
        with open(GIVEAWAYS_FILE, 'r') as f:
            giveaways_db = json.load(f)
            stats['active_giveaways'] = len(giveaways_db)
            
            all_entries = []
            durations = []

            for g_id, g_data in giveaways_db.items():
                stats['total_giveaway_entries'] += len(g_data['entries'])
                all_entries.extend(g_data['entries'])

                duration_seconds = g_data['end_time'] - time.time()
                if duration_seconds > 0: 
                     durations.append(duration_seconds / 60)
            
            stats['giveaway_durations'] = durations

            entry_counts = Counter(all_entries)
            top_entry_items = entry_counts.most_common(5)
            stats['top_5_entries'] = [(uid, count) for uid, count in top_entry_items]
    
    return stats

# ==============================================================================
# PLOTTING FUNCTIONS (MODIFIED)
# ==============================================================================

def create_xp_bar_chart(stats):
    """Creates a bar chart for the top 5 XP users."""
    uids = [item[0] for item in stats['top_5_xp']]
    xps = [item[1] for item in stats['top_5_xp']]
    user_cache = stats['user_cache']

    if not xps:
        return ""
    
    # --- Use the cached names for labels ---
    labels = [user_cache.get(uid, f"User {uid}") for uid in uids] 
    # ---------------------------------------

    plt.figure(figsize=(6, 4))
    plt.bar(range(len(labels)), xps, color='purple')
    plt.xticks(range(len(labels)), labels, rotation=45, ha='right')
    plt.ylabel("Total XP")
    plt.title("Top 5 XP Leaders")
    plt.tight_layout()

    # Save to a buffer
    buf = BytesIO()
    plt.savefig(buf, format='png')
    plt.close()
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def create_giveaway_histogram(stats):
    """Creates a histogram for the duration of active giveaways."""
    durations = stats['giveaway_durations']
    
    if not durations:
        return ""

    # Convert durations from seconds (how they are calculated in load_local_data) to minutes/hours for plotting
    # Assuming durations are stored in minutes in the stats dictionary for simplicity here.
    # If your original code was storing seconds, you need to adjust the axis label.
    
    plt.figure(figsize=(6, 4))
    
    # Calculate the number of bins. Using the square root rule for a simple estimate.
    num_bins = int(len(durations)**0.5) if len(durations) > 0 else 5 

    plt.hist(durations, bins=num_bins, color='skyblue', edgecolor='black', alpha=0.7)
    
    plt.xlabel("Remaining Giveaway Duration (Minutes)")
    plt.ylabel("Number of Giveaways")
    plt.title("Distribution of Active Giveaway Durations")
    plt.tight_layout()

    # Save to a buffer
    buf = BytesIO()
    plt.savefig(buf, format='png')
    plt.close()
    return base64.b64encode(buf.getvalue()).decode('utf-8')

# ==============================================================================
# FLASK ROUTE (MODIFIED)
# ==============================================================================

@app.route('/')
def dashboard():
    """Main route for the bot dashboard."""
    stats = load_local_data()
    
    xp_chart_data = create_xp_bar_chart(stats)
    # Ensure chart function is updated to use the cache
    giveaway_chart_data = create_giveaway_histogram(stats) 
    
    last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_cache = stats['user_cache'] # Get the cache loaded from the file

    # Convert top 5 lists to HTML tables
    def format_leaderboard(board):
        rows = []
        for i, (uid, value) in enumerate(board):
            # --- Use the cached name here ---
            display_name = user_cache.get(uid, f"User {uid}") 
            # --------------------------------
            rows.append(f"<tr><td>{i+1}.</td><td>{display_name}</td><td>{value}</td></tr>")
        return "".join(rows)

    xp_table = format_leaderboard(stats['top_5_xp'])
    entry_table = format_leaderboard(stats['top_5_entries'])


    # HTML Template for the dashboard (No changes needed here as it uses variables)
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Discord Bot Dashboard</title>
        <meta http-equiv="refresh" content="60"> <style>
            body { font-family: Arial, sans-serif; background-color: #36393f; color: #dcddde; margin: 0; padding: 20px; }
            .container { max-width: 1200px; margin: auto; }
            h1 { color: #7289da; border-bottom: 2px solid #7289da; padding-bottom: 10px; }
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
            .stat-card { background-color: #2f3136; padding: 20px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }
            .stat-card h2 { margin-top: 0; font-size: 1em; color: #99aab5; }
            .stat-card p { font-size: 2em; margin: 5px 0 0 0; color: #ffffff; }
            .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 30px; margin-bottom: 30px; }
            .charts-grid img { width: 100%; height: auto; background-color: white; border-radius: 8px; }
            .leaderboard-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 30px; }
            table { width: 100%; border-collapse: collapse; background-color: #2f3136; border-radius: 8px; overflow: hidden; }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #4f545c; }
            th { background-color: #7289da; color: white; }
            tr:hover { background-color: #3b3e44; }
            .footer { text-align: center; margin-top: 40px; font-size: 0.8em; color: #99aab5; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📊 Discord Bot Statistics Dashboard</h1>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <h2>Total Users Tracked</h2>
                    <p>{{ total_users }}</p>
                </div>
                <div class="stat-card">
                    <h2>Total XP Earned</h2>
                    <p>{{ total_xp }}</p>
                </div>
                <div class="stat-card">
                    <h2>Active Giveaways</h2>
                    <p>{{ active_giveaways }}</p>
                </div>
                <div class="stat-card">
                    <h2>Total Giveaway Entries</h2>
                    <p>{{ total_giveaway_entries }}</p>
                </div>
            </div>

            <h2>Leaderboards & Distribution</h2>

            <div class="leaderboard-grid">
                <div class="leaderboard-card">
                    <h3>🏆 Top 5 Users by XP</h3>
                    <table>
                        <thead>
                            <tr><th>Rank</th><th>User Name</th><th>XP</th></tr>
                        </thead>
                        <tbody>
                            {{ xp_table | safe }}
                        </tbody>
                    </table>
                </div>
                <div class="leaderboard-card">
                    <h3>🎁 Top 5 Users by Giveaway Entries</h3>
                    <table>
                        <thead>
                            <tr><th>Rank</th><th>User Name</th><th>Entries</th></tr>
                        </thead>
                        <tbody>
                            {{ entry_table | safe }}
                        </tbody>
                    </table>
                </div>
            </div>
            
            <br><br>

            <div class="charts-grid">
                 {% if xp_chart_data %}
                    <img src="data:image/png;base64,{{ xp_chart_data }}" alt="XP Leaderboard Chart">
                 {% else %}
                    <div class="stat-card">No XP data available to plot.</div>
                 {% endif %}
                 
                 {% if giveaway_chart_data %}
                    <img src="data:image/png;base64,{{ giveaway_chart_data }}" alt="Giveaway Duration Histogram">
                 {% else %}
                    <div class="stat-card">No active giveaway data available to plot.</div>
                 {% endif %}
            </div>

            <p class="footer">
                Last updated: {{ last_updated }} | Dashboard running on http://{{ host }}:{{ port }}
            </p>
        </div>
    </body>
    </html>
    """

    return render_template_string(
        html_template, 
        host=HOST,
        port=PORT,
        last_updated=last_updated,
        **stats, 
        xp_table=xp_table,
        entry_table=entry_table,
        xp_chart_data=xp_chart_data,
        giveaway_chart_data=giveaway_chart_data
    )

if __name__ == '__main__':
    print(f"--- Starting Dashboard. Open http://{HOST}:{PORT} in your browser. ---")
    app.run(host=HOST, port=PORT, debug=True, use_reloader=False)