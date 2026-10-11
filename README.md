# homelab

Raspberry Pi TV box: Kodi for movies and TV, Nest doorbell stills on screen, and optional audio to a 3DS remote.

Hardware target is a Pi 3 (HDMI to the TV). Media lives on a USB drive and/or an Ubuntu machine mounted over sshfs. Config is pulled from this repo after CI passes.

## Layout

```
pi/
  install.sh          # run on the Pi (packages, units, Kodi, doorbell)
  deploy.sh           # rsync pi/ to the Pi and run install.sh
  kodi/               # Estuary TV skin trim, sources, doorbell overlay
  doorbell/           # Nest Pub/Sub → event still → Kodi
  drive/              # auto-mount first USB disk at /srv/media
  samba/              # smb://pi/storage → /mnt/storage (Finder uploads)
  audio/              # stream Kodi audio to the 3DS on request
  update/             # timer: deploy main after GitHub checks pass
  tests/
3ds/
  kodi-remote/        # Nintendo 3DS homebrew: Kodi remote + PCM audio
```

## What runs on the Pi

| Piece | Role |
| --- | --- |
| **Kodi** | Movies + TV home tabs; plays from USB and storage |
| **Doorbell** | Nest chime → still image overlay (or fullscreen if idle); copies saved under storage |
| **SMB** | Upload videos from a Mac to `smb://<pi>/storage` |
| **sshfs** | `/mnt/storage` → Ubuntu `homelab@192.168.50.1:…/Videos` |
| **USB** | First external disk mounted at `/srv/media` (`Movies/`, `TV Shows/`) |
| **stream-3ds** | Serves Kodi audio when the 3DS remote connects |
| **homelab-update** | Every few minutes: if `main` CI is green, `git reset` + `install.sh` |

go2rtc is installed for Nest but left **disabled** — live WebRTC on this Pi starves playback. The doorbell path uses Nest `CameraEventImage` (HTTPS still) only.

## 3DS remote

Homebrew app in `3ds/kodi-remote/` (devkitARM / libctru). It talks JSON-RPC to Kodi and pulls PCM audio from `stream-3ds` on the Pi.

```sh
# Build on a machine with DEVKITARM set
cd 3ds/kodi-remote && make

# Host-side smoke test (no 3DS toolchain)
./test/run.sh
```

Copy `kodi-remote.3dsx` to the SD card and `kodi-remote.cfg.example` → `sdmc:/3ds/kodi-remote.cfg` (set `host` to the Pi’s IP and `password` from `~/.config/homelab/kodi-web-password` on the Pi).

## Media folders

| Path | Use |
| --- | --- |
| `/srv/media/Movies`, `/srv/media/TV Shows` | USB library |
| `/mnt/storage/Movies`, `/mnt/storage/TV Shows` | Ubuntu library (also via SMB) |
| `/mnt/storage/Doorbell` | Saved doorbell photos; Kodi **Doorbell** tab |

Kodi buffers all filesystems (including fuse.sshfs) with a larger RAM cache so network media does not hitch as easily.

## Nest doorbell

On a chime:

1. Pull the event still from the SDM API (no live stream).
2. If something is playing → notification overlay with the photo; movie keeps going.
3. If idle → show the photo fullscreen briefly.
4. Archive a copy as `/mnt/storage/Doorbell/YYYY-MM-DD_HHMMSS.jpg`.

Secrets stay on the Pi only (not in this repo), under `~/.config/homelab/`:

- `google-client-id`, `google-client-secret`, `google-refresh-token`
- `sdm-project-id`, `gcp-project-id`, `nest-device-id`
- `kodi-web-password` (JSON-RPC; created by `configure.py` if missing)

Pub/Sub subscription expected: `nest-events-sub` in the GCP project.

## Deploy

From a machine with SSH key `~/.ssh/homelab`:

```sh
./pi/deploy.sh                 # default alarcafil@10.0.0.214
./pi/deploy.sh user@host       # override
```

Or push to `main`: CI runs (`shellcheck`, unittest with Estuary `Home.xml`). The Pi timer deploys that commit once checks succeed.

Manual install on the Pi (as root, from a checked-out `pi/`):

```sh
sudo sh install.sh
```

## Notes

- Prefer a solid 5V PSU; undervoltage (`vcgencmd get_throttled`) causes stutter even with caching.
- Do not put Google OAuth secrets or the Kodi web password in the public repo.
- Wi‑Fi and Ubuntu storage deletes are intentionally out of scope for automated scripts.
