import threading
from flask import Flask, jsonify, make_response
import os
import time

# Flask application instance
app = Flask(__name__)

# Global variable to track the bot's status
# This is required so the Flask server (running in a separate thread) can check the bot's status.
# We will assume your main bot script (app.py) updates this variable.
# Start with False, and app.py should set it to True after the client's on_ready event fires.
BOT_IS_READY = False
BOT_START_TIME = time.time()

def set_bot_ready(status):
    """Function to be called by the main Discord bot script (app.py) to set the status."""
    global BOT_IS_READY
    BOT_IS_READY = status
    print(f"Flask Server: Bot status set to: {BOT_IS_READY}")

@app.route('/')
def home():
    """Simple root endpoint, primarily for basic reachability test."""
    return "SpectraBot is running! Use /health for monitoring status."

@app.route('/health')
def health_check():
    """
    Detailed health check endpoint for status pages like Better Stack.
    Returns HTTP 200 if the bot is ready, otherwise HTTP 503.
    """
    if BOT_IS_READY:
        # Calculate uptime for detailed health report
        uptime_seconds = time.time() - BOT_START_TIME
        uptime_string = f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m {int(uptime_seconds % 60)}s"

        response_data = {
            "status": "UP",
            "bot_ready": BOT_IS_READY,
            "uptime": uptime_string,
            "timestamp": time.time()
        }
        # Return HTTP 200 OK
        return jsonify(response_data), 200
    else:
        # Return HTTP 503 Service Unavailable
        response_data = {
            "status": "DOWN",
            "message": "Discord client not yet connected or failed to start.",
            "bot_ready": BOT_IS_READY
        }
        return jsonify(response_data), 503

def run():
    """Starts the Flask server in a separate thread."""
    # Determine the port to use (Wispbyte/Render environments often use PORT env var)
    port = int(os.environ.get('PORT', 8080))
    print(f"Starting Flask server on port {port}...")
    # host='0.0.0.0' is necessary to make it externally accessible in most hosting environments
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    """Initializes and starts the Flask server thread."""
    t = threading.Thread(target=run)
    t.start()
    
# Export the set_bot_ready function for use in app.py
__all__ = ['keep_alive', 'set_bot_ready']