#!/bin/sh
# Installs everything in pi/ onto the Raspberry Pi. Run as root on the Pi; safe to re-run.
set -eu
cd "$(dirname "$0")"

USER_NAME=alarcafil
KEEP="kodi kodi-bin kodi-data kodi-repository-kodi openssh-server network-manager wpasupplicant
bluez pipewire pipewire-pulse pipewire-alsa wireplumber libspa-0.2-bluetooth p11-kit p11-kit-modules
rsync git curl avahi-daemon cron sudo raspi-config raspi-firmware linux-image-rpi-v8 raspberrypi-sys-mods
samba sshfs"

installed() {
    for p in "$@"; do
        dpkg-query -W -f='${db:Status-Abbrev}${Package}\n' "$p" 2>/dev/null | awk '/^ii/ { print substr($0, 4) }'
    done
}

echo "== packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q kodi rsync git curl sshfs
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends samba
# shellcheck disable=SC2046,SC2086
apt-mark manual $(installed $KEEP) >/dev/null

echo "== remove unneeded packages"
# shellcheck disable=SC2046
remove=$(set -f; installed $(grep -v '^#' remove-packages.txt))
if [ -n "$remove" ]; then
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get purge --autoremove -y -q $remove
fi
apt-get clean

echo "== headless boot"
systemctl set-default multi-user.target
systemctl disable --now serial-getty@ttyS0.service 2>/dev/null || true

echo "== external drive watcher"
install -m 755 drive/media-mount /usr/local/sbin/media-mount
install -m 644 drive/media-mount@.service /etc/systemd/system/media-mount@.service
install -m 644 drive/90-media-drive.rules /etc/udev/rules.d/90-media-drive.rules
mkdir -p /srv/media
# Make the empty mount point read-only so nothing lands on the boot stick by mistake.
if ! mountpoint -q /srv/media; then
    chattr +i /srv/media
fi
systemctl daemon-reload
udevadm control --reload
udevadm trigger --action=add --subsystem-match=block

echo "== file sharing (smb://<pi>/storage)"
install -m 644 samba/smb.conf /etc/samba/smb.conf
systemctl disable --now nmbd.service 2>/dev/null || true
systemctl enable smbd.service
systemctl restart smbd.service
# /mnt/storage is the Ubuntu machine over sshfs; skip if it is switched off.
if ! timeout 20 runuser -u "$USER_NAME" -- mkdir -p /mnt/storage/Movies "/mnt/storage/TV Shows"; then
    echo "storage not reachable, skipped creating Movies and TV Shows folders"
fi

echo "== kodi"
install -m 644 kodi/kodi.service /etc/systemd/system/kodi.service
systemctl daemon-reload
systemctl enable kodi.service
systemctl stop kodi.service
if [ ! -f "/home/$USER_NAME/.kodi/userdata/guisettings.xml" ]; then
    # First run creates Kodi's settings and databases.
    systemctl start kodi.service
    sleep 60
    systemctl stop kodi.service
fi
runuser -u "$USER_NAME" -- python3 kodi/configure.py
systemctl start kodi.service

echo "== auto-deploy from GitHub"
install -m 755 update/homelab-update /usr/local/sbin/homelab-update
install -m 644 update/homelab-update.service /etc/systemd/system/homelab-update.service
install -m 644 update/homelab-update.timer /etc/systemd/system/homelab-update.timer
systemctl daemon-reload
systemctl enable --now homelab-update.timer

echo "== done"
