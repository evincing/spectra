#!/bin/bash

# Start bot in background
python app.py &
BOT_PID=$!

# Start dashboard on port from environment or default 5000
python dashboard.py

# Kill bot if dashboard exits
kill $BOT_PID 2>/dev/null || true
