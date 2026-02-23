#!/usr/bin/env bash
set -euo pipefail

# Trusted Web Activity scaffold helper for Roznamcha
# Requires: internet access, Java 17+, Node/NPM.

APP_URL="${APP_URL:-https://roznamcha.app}"
MANIFEST_URL="${MANIFEST_URL:-$APP_URL/manifest.webmanifest}"
APP_ID="${APP_ID:-com.roznamcha.app}"
TWA_DIR="${TWA_DIR:-android-twa}"
NPM_CACHE_DIR="${NPM_CACHE_DIR:-$(pwd)/.npm-cache}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    return 1
  fi
}

need_cmd node
need_cmd npm
need_cmd npx
need_cmd java

if ! java -version >/dev/null 2>&1; then
  echo "Java runtime detected but not usable. Install JDK 17+ and retry."
  exit 1
fi

mkdir -p "$TWA_DIR"
mkdir -p "$NPM_CACHE_DIR"
cd "$TWA_DIR"

echo "Using manifest: $MANIFEST_URL"
echo "Using app id:   $APP_ID"

echo "Initializing TWA project..."
# bubblewrap init is interactive by design. Keep values aligned with manifest/app id.
NPM_CONFIG_CACHE="$NPM_CACHE_DIR" npx -y @bubblewrap/cli init --manifest "$MANIFEST_URL"

echo "Building Android project..."
NPM_CONFIG_CACHE="$NPM_CACHE_DIR" npx -y @bubblewrap/cli build

echo "Done. Next: open generated Android project, configure signing, and produce release AAB."
