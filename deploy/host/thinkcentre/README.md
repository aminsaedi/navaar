# thinkcentre host integration

The navaar pod (pinned to thinkcentre) mirrors every channel song into
`/home/amin/music/navaar` as tagged MP3s plus a `Navaar.m3u8` playlist
(`NAVAAR_LIBRARY_DIR`, see `src/navaar/library/`). Everything else reads that folder:

| Consumer | How |
|---|---|
| **Navidrome** (`deploy/k8s/navidrome.yaml`) | hostPath, read-only → https://music.aminsaedi.com |
| **Music Assistant** (docker on mx) | NFS `/srv/navaar-music` (read-only bind of the library), Tailscale only; mounted by a Docker NFS volume |
| **Car USB stick** | udev + systemd units in this directory |

Install / update: `sudo ./install.sh` (idempotent).

## USB stick

Any FAT32 stick **labelled `NAVAAR`** gets the library copied to `<stick>/navaar/`:

- **plug it in** → udev starts `navaar-usb-sync@attach` immediately (desktop
  auto-mount is suppressed for this label).
- **leave it plugged in** → `navaar-library.path` re-syncs ~30 s after songs are
  added/removed, and `navaar-usb-sync.timer` checks every 10 min as a safety net
  (skipped cheaply when nothing changed for that stick's UUID).
- The stick is mounted **only while copying** (`/run/navaar-usb/mnt`, `flush`) and
  unmounted after, so it is safe to pull out any other time. A dirty stick (pulled
  out of a car mid-write) is repaired with `fsck.vfat -a` first.
- Each copy is retried 5× with backoff while the stick stays attached; a full stick
  fails fast. With `/etc/navaar-usb/notify.env` (`BOT_TOKEN=`, `CHAT_ID=`) you get a
  Telegram message when it is done ("safe to unplug") or failed.

Preparing a new stick (destroys its contents — check the device name!):

```bash
sudo wipefs -a /dev/sdX
sudo parted -s /dev/sdX mklabel msdos mkpart primary fat32 1MiB 100% set 1 lba on
sudo mkfs.vfat -F 32 -n NAVAAR /dev/sdX1
```

MBR + FAT32 is what car head units read most reliably (many reject GPT or exFAT);
files are MP3 with ID3v2.3 tags and a ≤500 px baseline JPEG cover for the same reason.

Operate:

```bash
journalctl -u 'navaar-usb-sync@*' -f            # what happened
sudo systemctl start navaar-usb-sync@manual     # force a copy now
systemctl list-timers navaar-usb-sync.timer
```
