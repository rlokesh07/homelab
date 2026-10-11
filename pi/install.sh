#!/bin/sh
# Installs everything in pi/ onto the Raspberry Pi. Run as root on the Pi; safe to re-run.
set -eu
cd "$(dirname "$0")"

USER_NAME=alarcafil
KEEP="kodi kodi-bin kodi-data kodi-repository-kodi openssh-server network-manager wpasupplicant
bluez pipewire pipewire-pulse pipewire-alsa wireplumber libspa-0.2-bluetooth p11-kit p11-kit-modules
rsync git curl ffmpeg avahi-daemon cron sudo raspi-config raspi-firmware linux-image-rpi-v8 raspberrypi-sys-mods
samba sshfs"

installed() {
    for p in "$@"; do
        dpkg-query -W -f='${db:Status-Abbrev}${Package}\n' "$p" 2>/dev/null | awk '/^ii/ { print substr($0, 4) }'
    done
}

echo "== packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q kodi rsync git curl sshfs ffmpeg
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

echo "== Ubuntu media over sshfs (/mnt/storage)"
install -d -m 755 /mnt/storage
FSTAB_LINE="homelab@192.168.50.1:/home/homelab/Videos /mnt/storage fuse.sshfs noauto,x-systemd.automount,_netdev,nofail,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,IdentityFile=/home/$USER_NAME/.ssh/id_ed25519,allow_other,default_permissions,uid=$(id -u "$USER_NAME"),gid=$(id -g "$USER_NAME"),cache=yes,kernel_cache,compression=no,attr_timeout=60,entry_timeout=60,negative_timeout=60 0 0"
if grep -q '[[:space:]]/mnt/storage[[:space:]]' /etc/fstab; then
    # shellcheck disable=SC2016
    sed -i '\|[[:space:]]/mnt/storage[[:space:]]|d' /etc/fstab
fi
printf '%s\n' "$FSTAB_LINE" >> /etc/fstab
systemctl daemon-reload
# Remount only when nothing holds the mount; otherwise the new options apply next automount.
if mountpoint -q /mnt/storage && ! fuser -m /mnt/storage >/dev/null 2>&1; then
    umount /mnt/storage 2>/dev/null || umount -l /mnt/storage 2>/dev/null || true
fi
systemctl restart mnt-storage.automount 2>/dev/null || true
# /mnt/storage is the Ubuntu machine over sshfs; skip if it is switched off.
if ! timeout 20 runuser -u "$USER_NAME" -- mkdir -p /mnt/storage/Movies "/mnt/storage/TV Shows"; then
    echo "storage not reachable, skipped creating Movies and TV Shows folders"
fi

echo "== 3ds audio"
SINK_CONF=/etc/pipewire/pipewire.conf.d/90-3ds-sink.conf
if ! cmp -s audio/90-3ds-sink.conf "$SINK_CONF"; then
    install -D -m 644 audio/90-3ds-sink.conf "$SINK_CONF"
    # Kodi is restarted below, so it picks up the new sink.
    runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$USER_NAME")" \
        systemctl --user restart pipewire.service
fi
install -m 755 audio/stream_3ds.py /usr/local/bin/stream-3ds
install -m 644 audio/stream-3ds.service /etc/systemd/system/stream-3ds.service
systemctl daemon-reload
systemctl enable stream-3ds.service
systemctl restart stream-3ds.service

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

echo "== nest doorbell"
GO2RTC_VER=1.9.14
need_go2rtc=1
if [ -x /usr/local/bin/go2rtc ] && /usr/local/bin/go2rtc -version 2>&1 | grep -Fq "$GO2RTC_VER"; then
    need_go2rtc=0
fi
if [ "$need_go2rtc" -eq 1 ]; then
    curl -fsSL -o /usr/local/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VER}/go2rtc_linux_arm64"
    chmod 755 /usr/local/bin/go2rtc
fi
install -d -m 755 /usr/local/lib/homelab
install -m 644 doorbell/doorbell.py /usr/local/lib/homelab/doorbell.py
install -m 644 doorbell/write_go2rtc_config.py /usr/local/lib/homelab/write_go2rtc_config.py
install -m 644 doorbell/go2rtc.service /etc/systemd/system/go2rtc.service
install -m 644 doorbell/doorbell.service /etc/systemd/system/doorbell.service
NEST_CONF="/home/$USER_NAME/.config/homelab"
if [ -f "$NEST_CONF/google-refresh-token" ] && [ -f "$NEST_CONF/sdm-project-id" ]; then
    runuser -u "$USER_NAME" -- env GO2RTC_CONFIG=/tmp/go2rtc.yaml \
        python3 /usr/local/lib/homelab/write_go2rtc_config.py
    # go2rtc runs as $USER_NAME and must be able to read its own secrets.
    install -m 600 -o "$USER_NAME" -g "$USER_NAME" /tmp/go2rtc.yaml /etc/go2rtc.yaml
    rm -f /tmp/go2rtc.yaml
    systemctl daemon-reload
    # Event stills only — live WebRTC via go2rtc starves Kodi on the Pi 3.
    systemctl disable --now go2rtc.service 2>/dev/null || true
    systemctl enable doorbell.service
    systemctl restart doorbell.service
else
    echo "Nest secrets missing in $NEST_CONF, skipped doorbell"
    systemctl disable --now go2rtc.service doorbell.service 2>/dev/null || true
fi

echo "== auto-deploy from GitHub"
install -m 755 update/homelab-update /usr/local/sbin/homelab-update
install -m 644 update/homelab-update.service /etc/systemd/system/homelab-update.service
install -m 644 update/homelab-update.timer /etc/systemd/system/homelab-update.timer
systemctl daemon-reload
systemctl enable --now homelab-update.timer

echo "== done"
