#!/bin/sh
# Create the IBKR adapter environment from IBKR's official TWS API package (ADR-0014).
# The ibapi source is GPL-3.0-or-later; it is installed on this host only and never committed.
set -eu
VERSION="1051.01"
SHA256="50a459ed09f0030df25125a177e57b69bdf576ec3a89fcb1f81fdff95bbf6316"   # verified 2026-10-22
URL="https://interactivebrokers.github.io/downloads/twsapi_macunix.${VERSION}.zip"
HOME_DIR="${UNKNOWN_HOME:-$HOME/.unknown}"
VENV="$HOME_DIR/venvs/ibkr"
PY="${PYTHON:-/opt/homebrew/bin/python3.12}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
curl -fsSL -A "Mozilla/5.0" -o "$WORK/twsapi.zip" "$URL"
echo "$SHA256  $WORK/twsapi.zip" | shasum -a 256 -c -
(cd "$WORK" && unzip -q twsapi.zip 'IBJts/source/pythonclient/*' 'IBJts/API_VersionNum.txt' 'IBJts/LICENSE')
mkdir -p "$HOME_DIR/venvs"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "protobuf==5.29.5"
"$VENV/bin/python" -m pip install --quiet "$WORK/IBJts/source/pythonclient"
"$VENV/bin/python" -c "import ibapi; print('ibapi', ibapi.get_version_string(), 'installed in $VENV')"
