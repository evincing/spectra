import subprocess
import os

# Start the bot in a subprocess
bot_process = subprocess.Popen(['python', 'app.py'])
print("Bot process started (PID: {})".format(bot_process.pid))

# Start the dashboard in the main process
dashboard_process = subprocess.Popen(['python', 'dashboard.py'])
print("Dashboard process started (PID: {})".format(dashboard_process.pid))

# Wait for both to complete
try:
    dashboard_process.wait()
except KeyboardInterrupt:
    print("\nShutting down...")
    bot_process.terminate()
    dashboard_process.terminate()
    bot_process.wait()
    dashboard_process.wait()
