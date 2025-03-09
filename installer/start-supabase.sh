#!/bin/bash

# Run from repo root
source ./scripts/detect-quick-command-args.sh
eval "$(detect_command_args "$@")" || exit 1

echo "Starting Supabase services with Buildx..."

# Copy environment file
cp installer/supabase-docker/.env.example installer/supabase-docker/.env

# Ensure Buildx is enabled
docker buildx create --name supabase-builder --use || echo "Buildx already exists"

# Build multi-platform images using Buildx
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --push \
  -t my-supabase-image \
  -f installer/supabase-docker/Dockerfile .

# Start Supabase using pre-built images
docker compose up --force-recreate

