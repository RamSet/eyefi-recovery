# eyefi-recovery

A single-file, dependency-free Python tool for working with Eye-Fi SD cards after
Eye-Fi's servers went away. Its main job is to **recover the per-card upload key**
that a replacement server (for example an eyefi-server or Eye-Fi emulator) needs
in order to accept uploads from the card. It also reads firmware details, repairs
a card whose control files were wiped by a format, and configures the card's Wi-Fi.

Everything lives in `eyefi_diagnostic.py`. Copy that one file to any Linux machine
with Python 3 and an SD card slot and run it.

> Not affiliated with or endorsed by Eye-Fi. Use only on cards you own or are
> authorised to examine. Dumps and recovered keys are secrets: do not share them.

---

## Table of contents

- [Background](#background)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [How the card is detected](#how-the-card-is-detected)
- [Commands](#commands)
  - [recover](#recover) (start here)
  - [info](#info)
  - [firmware](#firmware)
  - [scan](#scan)
  - [extract](#extract)
  - [reboot-dump](#reboot-dump)
  - [restore-channels](#restore-channels)
  - [wifi](#wifi)
  - [crash](#crash) (experimental)
  - [sweep](#sweep) (experimental)
  - [read / log](#read--log)
- [Output and `--save-keys`](#output-and---save-keys)
- [Which method works on which firmware](#which-method-works-on-which-firmware)
- [After a format: why the EyeFi folder is gone](#after-a-format-why-the-eyefi-folder-is-gone)
- [Troubleshooting](#troubleshooting)
- [How it talks to the card](#how-it-talks-to-the-card)
- [Safety notes](#safety-notes)
- [Credits](#credits)

---

## Background

An Eye-Fi card uploads photos to a server and authenticates with a 128-bit
**upload key** that was set at the factory and registered with Eye-Fi's cloud.
The official software is gone, and so is the only place that key was ever shown.
Without it, a self-hosted server cannot verify the card's uploads.

The key is still on the card. Depending on firmware, it can be obtained by:

1. **Asking the card directly.** Firmware 5.2 and later answers a plain query with
   the key. This is the same query the classic `eyefi-config -u` sends.
2. **Reading a crash dump the card already left behind.** The card keeps up to a
   few internal dump slots. If it has ever crashed, a slot holds a memory image
   that contains the board-data record, and the keys are at fixed offsets inside it.
3. **Making the card write a fresh dump.** On some firmware (traced on 5.0018)
   the ordinary reboot command leaves a dump behind. On others, a specific
   command byte checkpoints one.

`recover` tries those in that order, least intrusive first, and stops at the
first success.

## Requirements

- Linux. The tool reads `/proc/mounts` and uses `O_DIRECT` for uncached I/O.
- Python 3.8 or newer. Standard library only, no `pip install`.
- A card reader. A USB reader or a laptop's built-in SD slot both work.
- The card mounted read-write as a normal FAT volume (your desktop usually does
  this automatically under `/media/<user>/...` or `/run/media/<user>/...`).
- `blkid` is used when available to confirm a card's volume ID. It is optional
  except for `restore-channels`, where the check protects you from writing to
  the wrong device. Root (or `sudo blkid`) may be needed for that one check.

No root is needed for the normal recovery path.

## Quick start

```sh
# 1. Insert the card and let it mount.
# 2. Run the all-in-one recovery:
python3 eyefi_diagnostic.py recover
```

Typical output:

```
Card MAC: 001856xxxxxx   Upload key: 0123456789abcdef0123456789abcdef   direct query
```

Add `--save-keys` to append the result to `eyefi-keys.json` next to the script,
and `-v` to see every step.

If the card has no `EyeFi` folder (it was formatted), auto-detect has nothing to
find. Pass the mount point explicitly and the tool will recreate the folder first:

```sh
python3 eyefi_diagnostic.py recover /media/you/EYE-FI
```

## How the card is detected

Most commands take an optional `drive` argument, the card's mount point. If you
omit it, the tool reads `/proc/mounts` and looks for a mounted volume that has an
`EYEFI`, `EyeFi` or `eyefi` directory. Exactly one match is required; with zero
or two or more it refuses and asks you to name the mount point.

Mount points with spaces in the volume label (for example a camera-formatted
`NIKON D7000`) are handled; the octal escapes in `/proc/mounts` are decoded.

Auto-detect keys off the `EyeFi` folder, so a freshly formatted card is
invisible to it. See [restore-channels](#restore-channels).

## Commands

Global flag on every subcommand: `-v` / `--verbose` prints per-slot and per-step
progress. Without it, card operations print only the final MAC and key line.
Colour is used only when stdout is a terminal and `NO_COLOR` is unset.

### recover

```
recover [drive] [--output-dir DIR] [--bytes N] [--wait-timeout S] [--mac MAC]
        [--no-reboot] [--force] [--save-keys [FILE]]
```

The one command most people need. Runs the ladder:

1. Direct query (`info`). Works on firmware 5.2 and later.
2. Existing dump slots (`scan` over slots 1 to 3, then `extract`).
3. Reboot-dump (`reboot-dump`): sends the reboot command, waits for the card to
   remount, scans again.

Stops at the first method that yields a key. `--no-reboot` stops after step 2,
which makes the whole run read-only. `--output-dir` names where the reboot-dump
backup goes; it must not already exist.

If the `EyeFi` folder is missing, `recover <drive>` first runs the equivalent of
`restore-channels` on it. That step verifies the volume ID; `--force` skips the
verification if you have confirmed the card yourself.

The experimental `crash` method is deliberately **not** part of `recover`. Run it
by hand if the ladder fails.

### info

```
info [drive]
```

Safe queries only, no reboot, no crash. Prints firmware version, MAC, card key,
and the upload key when the firmware exposes it (5.2+). On older firmware the
upload key field comes back empty and you need a dump-based method.

### firmware

```
firmware [drive]
```

Prints just the firmware version string, for example
`5.2011 May 14 2014 23:35:48`. Useful before deciding which method to try.

### scan

```
scan [drive] [--output-dir DIR] [--first-index N] [--last-index N] [--bytes N] [--mac MAC]
```

Reads the card's internal dump slots to files in a new directory (default: a
timestamped folder next to the script), then searches each for board-data
records and prints any keys found. Defaults read slot 1 only with 327680 bytes;
`--last-index 3` covers all slots the cards are known to keep. Reading a slot
that was never written just returns zeros, which is harmless.

### extract

```
extract FILE [--mac MAC]
```

Offline. Searches an already-saved dump file for board-data records and prints
MAC and keys. Use this on dumps you collected earlier or on another machine.
No card needed.

### reboot-dump

```
reboot-dump [drive] [--output-dir DIR] [--first-index N] [--last-index N]
            [--bytes N] [--wait-timeout S] [--mac MAC]
```

Sends the ordinary reboot command (the same one `eyefi-config -b` sends), waits
up to `--wait-timeout` seconds (default 60) for the card to drop and remount,
then scans the dump slots. Traced to leave a dump on firmware 5.0018. Do not run
it while the card is busy writing photos.

The card will disappear from the system for a few seconds. On some built-in
readers the whole reader detaches when the card resets; if the card does not
come back on its own, pull it and reinsert.

### restore-channels

```
restore-channels DRIVE [--force]
```

Recreates the `EyeFi` folder and its four control files (`REQM`, `REQC`, `RSPM`,
`RSPC`, each 16384 zero bytes). You need this after formatting the card in a
camera or computer, because the firmware does **not** recreate them itself.

`DRIVE` is required. Before writing anything, the tool reads the device's volume
ID and refuses unless it is `AA52-6922`, the factory ID every Eye-Fi card
carries. `--force` skips that check if `blkid` is unavailable and you have
verified the card another way.

### wifi

```
wifi scan   [drive]
wifi list   [drive]
wifi add    [drive] ESSID [--password PW] [--force]
wifi remove [drive] ESSID
wifi test   [drive] ESSID [--password PW]
```

Ported from eyefi-config's network commands. `add` tests the connection first,
like the original tool, and saves the network only if the test passes; `--force`
saves regardless. Passwords may be a WPA passphrase or a raw 64/26/10 hex-digit
key; omit for an open network.

A test that gets Wi-Fi association and a DHCP lease but then stalls at "reaching
Eye-Fi's cloud" is counted as a pass, because that cloud no longer exists.

### crash

```
crash [drive] [--command BYTE] [--output-dir DIR] [--first-index N] [--last-index N] [--bytes N]
```

**Experimental.** Backs up the current dump slots to a new directory, then sends
one host command byte intended to make the firmware write a fresh dump. With no
`--command`, it picks a confirmed trigger if the exact firmware is in the
tool's known-trigger table (currently 5.2010, byte `0x01`, which checkpoints a
dump without even dropping the card), otherwise it falls back to `?` (`0x3f`),
which was traced on firmware 4.5166 only.

The card may reset, drop off USB, or do nothing. Photos on the FAT volume are
not touched, but take a backup anyway.

### sweep

```
sweep [drive] [--min-byte B] [--max-byte B] [--max-drops N] [--settle-timeout S]
      [--scan-every N] [--include-wifi-bytes] [--include-o-subcodes]
      [--include-bounds-matrix] [--output-dir DIR] [--mac MAC] ...
```

**Experimental, slow.** Sends every command byte in a range, one at a time,
waits for the card to settle after each, and scans for a new dump. Stops at the
first hit. This is how the 5.2010 trigger was found. Bytes with known meanings
(Wi-Fi operations, `o` sub-commands) are skipped unless you opt in. Expect many
resets; `--max-drops` bounds them.

### read / log

```
read [drive] --output FILE --dump-index N [--offset N] [--bytes N] [--mac MAC]
log  [drive] --output FILE [--offset N] [--bytes N]
```

Low-level. `read` copies a byte range of one dump slot to a file; `log` copies a
range of the card's internal text log. `--output` must be a new file on another
volume, never on the card.

## Output and `--save-keys`

Quiet mode prints one line per distinct card and key:

```
Card MAC: 001856xxxxxx   Upload key: <32 hex digits>   <method>
```

With `-v`, dump-based methods also print the board record offset and the
individual fields:

```
Board offset: 0x1a2b0; MAC: 001856xxxxxx; Desktop UploadKey: <32 hex>
```

`--save-keys [FILE]` on `recover` (and other key-producing commands) appends to
a JSON file shaped as `{ "<mac>": ["<key>", ...] }`. With no path it writes
`eyefi-keys.json` beside the script. It never writes to the card.

Treat the output and the dump directories as credentials. They are
`.gitignore` material, not something to commit.

## Which method works on which firmware

Observed on real cards. Your mileage may vary by card model.

| Firmware | Direct query (`info`) | Existing dump (`scan`) | `reboot-dump` | `crash` default |
|---|---|---|---|---|
| 4.5166 | no | if a dump exists | not traced | `?` traced |
| 5.0018 | no | if a dump exists | yes | untested |
| 5.0019 | no | if a dump exists | yes | untested |
| 5.2010 | yes | if a dump exists | varies | `0x01`, no drop |
| 5.2011 | yes | if a dump exists | varies, card may hang | untested |

"Varies" means it worked on some runs and on other runs the card stayed mounted
but stopped answering until reinserted. On 5.2 firmware you never need it,
because the direct query already returns the key.

Inside a dump, the board-data record (marker `c0decafeaa526922`) holds three
keys: a unique key at +0x00, a cloud key at +0x20 (used by firmware 5.0 to sign
in to Eye-Fi's API), and the desktop upload key at +0x40. The MAC follows at
+0x89. The desktop upload key is the one a self-hosted server wants.

## After a format: why the EyeFi folder is gone

Formatting a card, in a camera or on a computer, wipes the FAT volume, including
the `EyeFi` folder. The firmware never puts it back. It only reacts to the
control files once a host has created them, which is what the official desktop
software did silently on every insert. The volume serial `AA52-6922` survives a
format, which is how `restore-channels` can still prove it is an Eye-Fi card.

Copying a backed-up `EyeFi` folder from another card does not reliably work.
Freshly zeroed files created by `restore-channels` do.

## Troubleshooting

**"Found 0 cards" / auto-detect fails.** The card is not mounted, or has no
`EyeFi` folder. Check `mount | grep -i media`, then either mount it or run
`restore-channels <mountpoint>`.

**"Found 2 cards".** Two Eye-Fi volumes are mounted, or a stale mount is lingering
after a reset. Pass the mount point explicitly.

**The card stops answering mid-run.** Known to happen after a reboot or crash
command, especially in built-in Realtek readers that detach when the card
resets. Pull the card, wait a few seconds, reinsert. If it still ignores
requests, run `firmware` once; the tool seeds a fresh request sequence number
and that usually unsticks it.

**"File exists" for `--output-dir`.** Output directories must be new. Point it at
a path that does not exist yet.

**`restore-channels` refuses the volume ID.** `blkid` could not read the device,
usually because it needs root. Run `sudo blkid /dev/sdX1` yourself; if it shows
`AA52-6922`, rerun with `--force`.

**Direct query returns an empty key.** Firmware is older than 5.2. Use
`recover`, which falls through to the dump methods.

**Key recovered, but the server still rejects uploads.** Make sure you used the
desktop upload key (+0x40), not the cloud key (+0x20). With `-v` the tool labels
them.

## How it talks to the card

The card exposes a tiny request/response channel through four files in the
`EyeFi` folder, each one 16384-byte block:

| File | Role |
|---|---|
| `REQM` | request body, written by the host |
| `REQC` | request sequence number; writing it tells the card a request is ready |
| `RSPC` | response sequence number, polled by the host |
| `RSPM` | response body, read by the host |

Because the firmware writes `RSPM` behind the operating system's back, the host
must bypass the page cache: the tool opens the files with `O_DIRECT` and falls
back to `posix_fadvise(DONTNEED)` where `O_DIRECT` is not supported. This is the
same trick eyefi-config uses with unbuffered I/O. Wire bytes for every command
were taken from eyefi-config's C source, not guessed.

## Safety notes

- `info`, `firmware`, `scan`, `extract`, `read`, `log`, `wifi list` and
  `recover --no-reboot` never change card state beyond the request channel.
- `reboot-dump` resets the card. `crash` and `sweep` send bytes whose effect is
  firmware-specific and may hang the card until reinserted. All three take a
  dump backup first; none of them touch your photos, but back those up too.
- Never run reset-type commands while the card is writing.
- Dumps contain keys. Keep the output directories and `eyefi-keys.json` out of
  version control.

## Credits

Protocol knowledge, command bytes and the control-file scheme come from Dave
Hansen's [eyefi-config](https://github.com/hansendc/eyefi-config). Dump-slot
layout and the board-data offsets were worked out by reading real cards.
