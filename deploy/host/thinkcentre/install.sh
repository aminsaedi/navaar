#!/bin/bash
# Install/refresh the Navaar host integration on thinkcentre (idempotent).
#   sudo ./install.sh
# Optional Telegram notifications: write BOT_TOKEN=... and CHAT_ID=... to
# /etc/navaar-usb/notify.env (mode 600) — the script never prints them.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

LIB=/home/amin/music/navaar

# 1. The library folder the navaar pod writes (hostPath) and Navidrome reads.
install -d -o 1000 -g 1000 -m 755 /home/amin/music "$LIB"
semanage fcontext -a -t container_file_t "/home/amin/music(/.*)?" 2>/dev/null \
    || semanage fcontext -m -t container_file_t "/home/amin/music(/.*)?"
restorecon -R /home/amin/music

# 2. Read-only NFS export for Music Assistant on mx (over Tailscale), via a bind
#    mount under /srv so the export exposes nothing of /home/amin.
install -d -m 755 /srv/navaar-music
install -m 644 'srv-navaar\x2dmusic.mount' /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now 'srv-navaar\x2dmusic.mount'
cat > /etc/exports.d/navaar-music.exports <<EXPORTS
# Navaar music library -> Music Assistant on mx (read-only, over Tailscale)
/srv/navaar-music 100.114.59.86(ro,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000,fsid=7301)
EXPORTS
systemctl enable --now nfs-server
exportfs -ra

# 3. USB stick sync: script, udev rule, units.
# /usr/local/bin (bin_t), not /usr/local/lib: systemd then runs the script as
# unconfined_service_t. From lib_t it stays init_t, and the rsync it spawns
# transitions into the confined rsync_t daemon domain, which is denied (silently,
# dontaudit) access to the vfat mount -> "cannot stat destination: Permission denied".
install -m 755 navaar-usb-sync /usr/local/bin/navaar-usb-sync
restorecon /usr/local/bin/navaar-usb-sync
install -m 644 99-navaar-usb.rules /etc/udev/rules.d/99-navaar-usb.rules
install -m 644 navaar-usb-sync@.service navaar-usb-sync.timer navaar-library.path /etc/systemd/system/
install -d -m 700 /etc/navaar-usb
systemctl daemon-reload
systemctl enable --now navaar-usb-sync.timer navaar-library.path
udevadm control --reload-rules
# Pick up a stick that is already plugged in.
udevadm trigger --action=add --subsystem-match=block --property-match=ID_FS_LABEL=NAVAAR || true
echo "installed"
