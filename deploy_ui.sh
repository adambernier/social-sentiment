#!/bin/bash

# Exit on any error
set -e

echo "🚀 Pulling latest code..."
git pull

echo "🛑 Stopping all containers to free up RAM..."
docker compose stop

echo "🏗️ Rebuilding and starting the ui-service..."
docker compose up -d --build ui-service

echo "▶️ Starting all containers (recreating any with changed config)..."
docker compose up -d

echo "✅ UI successfully deployed!"
