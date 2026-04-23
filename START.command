#!/bin/bash
# Double-click this file on your Mac to start the app.
# It will open in your browser automatically.

cd "$(dirname "$0")"

echo "======================================"
echo "  Baseball Betting Dashboard"
echo "======================================"

# Check if virtual environment exists, create if not
if [ ! -d "venv" ]; then
  echo "Setting up for the first time (this takes ~1 minute)..."
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  echo "Setup complete!"
else
  source venv/bin/activate
fi

# Open browser after 2 seconds
(sleep 2 && open http://localhost:5001) &

echo "Starting server at http://localhost:5001"
echo "Press Ctrl+C to stop."
echo ""

python app.py
