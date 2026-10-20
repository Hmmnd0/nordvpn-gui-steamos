#!/bin/bash
# Adds NordVPN to the Plasma app menu so it can also be added to Steam
# (Steam → Add a Non-Steam Game) and launched from Game Mode.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$DIR/nordvpn_gui.py"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/nordvpn-gui.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=NordVPN
Comment=Connect to NordVPN through NetworkManager
Exec=/usr/bin/python3 "$DIR/nordvpn_gui.py"
Icon=network-vpn
Categories=Network;
Terminal=false
DESKTOP
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Installed. Find \"NordVPN\" in the app menu, or add it to Steam as a Non-Steam Game."
