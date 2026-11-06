# EyeFi Diagnostic Utility
#
# Independent diagnostic utility for EyeFi SD cards.
#
# Intended for use only with devices owned by the user
# or devices the user is authorized to examine.
#
# This project is not affiliated with or endorsed by EyeFi.
#
# Diagnostic dumps and extracted authentication keys may
# contain sensitive information. Do not share them publicly.

import argparse
from datetime import datetime
import errno
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

BLOCK = 16384
BOARD_MARKER = bytes.fromhex("c0decafeaa526922")

# Non-verbose by default: card operations print only the final MAC/key result.
# --verbose restores the full per-slot/per-byte progress output. Set in main().
VERBOSE = False

_ANSI = {"green": "\033[32m", "cyan": "\033[36m", "bold": "\033[1m",
         "dim": "\033[2m", "yellow": "\033[33m", "reset": "\033[0m"}


def _color_enabled():
    # Colour only a real terminal, and honour the NO_COLOR convention.
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def c(text, *codes):
    if not codes or not _color_enabled():
        return str(text)
    return "".join(_ANSI[k] for k in codes) + str(text) + _ANSI["reset"]


def vprint(*args, **kwargs):
    """print() only when --verbose; used for the progress/diagnostic chatter."""
    if VERBOSE:
        print(*args, **kwargs)


def print_key_summary(records, context=None):
    """One coloured line per distinct (MAC, key): the quiet-mode result."""
    seen = []
    for _, mac, key in records:
        if (mac, key) not in seen:
            seen.append((mac, key))
    for mac, key in seen:
        line = f"Card MAC: {c(mac, 'cyan', 'bold')}   Upload key: {c(key, 'green', 'bold')}"
        if context:
            line += f"   {c(context, 'dim')}"
        print(line, flush=True)

# Directory-name casings seen in the wild (EYEFI, EyeFi, and lowercase eyefi
# all exist on real cards). Checked explicitly rather than relying on the
# filesystem to case-fold the lookup, since that's not guaranteed across
# vfat/exfat drivers and mount options.
EYEFI_DIR_NAMES = ("EYEFI", "EyeFi", "eyefi")


def _unescape_mtab(field):
    """/proc/mounts (and /etc/mtab) escape space/tab/backslash/newline in each
    field as octal (space -> '\\040', etc) -- the same scheme glibc's
    getmntent uses. A mount point containing a space (e.g. "NIKON D7000")
    appears here as the four literal characters '\\040', not a real space, so
    this must be undone before the path is used for anything on disk.
    """
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def find_card():
    """Locate a single already-mounted card with an EYEFI/EyeFi directory.

    Scans /proc/mounts (Linux only, matching the rest of this port); refuses
    to guess when none or more than one candidate is found, rather than
    silently picking the wrong card. Does not mount anything itself.
    """
    candidates = []
    try:
        with open("/proc/mounts") as mounts:
            for line in mounts:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mount_point = _unescape_mtab(parts[1])
                if any(os.path.isdir(os.path.join(mount_point, name))
                       for name in EYEFI_DIR_NAMES):
                    candidates.append(mount_point)
    except OSError as error:
        raise OSError(f"Could not read /proc/mounts to auto-detect a card: {error}")
    candidates = sorted(set(candidates))
    if not candidates:
        raise OSError("No mounted card with an EYEFI directory found. "
                       "Mount it, or pass its path explicitly.")
    if len(candidates) > 1:
        raise OSError("Multiple mounted cards found (" + ", ".join(candidates) +
                       "); pass the one you want explicitly.")
    return Path(candidates[0])


# Every Eye-Fi card ships with this same FAT volume ID, baked in at the
# factory (confirmed in hansendc/eyefi-config's eyefi-linux.c: EYEFI_VOLUME_ID
# / dev_has_eyefi_vol_id()). A format wipes the EYEFI/ directory and its four
# channel files -- the firmware does not recreate them, the *host tool* does
# -- but the volume ID survives a normal format, so it's what that C code
# uses to decide "this is really an Eye-Fi card with no control files yet",
# as opposed to fabricating protocol files on an arbitrary blank SD card.
EYEFI_VOLUME_ID = "AA52-6922"


def _device_for_mount(mount_point):
    """Return the device node /proc/mounts lists for an already-unescaped
    mount point, or None if it's not currently mounted."""
    try:
        with open("/proc/mounts") as mounts:
            for line in mounts:
                parts = line.split()
                if len(parts) >= 2 and _unescape_mtab(parts[1]) == str(mount_point):
                    return parts[0]
    except OSError:
        pass
    return None


def device_has_eyefi_volume_id(device):
    """True if `device` (whatever /proc/mounts names it, e.g. /dev/sdd1)
    itself carries the Eye-Fi volume ID.

    Asks the device directly with `blkid -s UUID -o value <device>` first --
    not eyefi-config's own dev_has_eyefi_vol_id() approach of resolving
    /dev/disk/by-uuid/<EYEFI_VOLUME_ID> and checking whether it points back
    at this device. That symlink approach is structurally unreliable for
    this one volume ID specifically: every Eye-Fi card ships the identical
    UUID, but udev's by-uuid/ directory can only hold one symlink per UUID,
    so it may point at a different (possibly disconnected) Eye-Fi card than
    the one actually mounted here, wrongly failing this check on a real
    Eye-Fi card. Confirmed as a real failure: a user's restore-channels run
    refused on a genuine Eye-Fi card where the by-uuid symlink didn't
    resolve back to it, while `blkid` on the device directly (what they then
    did by hand) read the right UUID straight away.

    Falls back to the by-uuid symlink only if blkid isn't available at all
    (not installed, or errors out) -- better than refusing outright, but
    the caller should still offer --force given the above.
    """
    try:
        result = subprocess.run(["blkid", "-s", "UUID", "-o", "value", str(device)],
                                 capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip().upper() == EYEFI_VOLUME_ID
    except (OSError, subprocess.TimeoutExpired):
        pass
    link = Path("/dev/disk/by-uuid") / EYEFI_VOLUME_ID
    try:
        return os.path.realpath(link) == os.path.realpath(device)
    except OSError:
        return False


def recreate_channel_files(mount_point, force=False):
    """Port of eyefi-linux.c's create_control_files()/zero_file(): bootstrap
    a missing EYEFI/ directory and its four channel files on a card that was
    reformatted (which wipes them, since they're ordinary FAT files) but is
    still provably an Eye-Fi card by its volume ID.

    Refuses unless the volume ID matches or force=True -- this must never
    fabricate protocol files on an arbitrary blank SD card by default. force
    exists because device_has_eyefi_volume_id() can still be wrong (see its
    docstring); pass it only after confirming the ID yourself, e.g. with
    `blkid` or `sudo blkid` directly on the device. Unlike the C version
    (which recreates all four unconditionally via O_CREAT without O_TRUNC,
    so it would also happily overwrite the start of a file that already has
    real content), this only creates files that are actually missing; it
    never touches one that already exists, even partially.
    """
    mount_point = Path(mount_point)
    device = _device_for_mount(mount_point)
    if device is None and not force:
        raise OSError(f"{mount_point} is not currently mounted")
    if device is not None and not device_has_eyefi_volume_id(device) and not force:
        raise OSError(
            f"{mount_point} ({device}) does not carry the Eye-Fi volume ID "
            f"({EYEFI_VOLUME_ID}); refusing to fabricate channel files on a "
            "card this tool cannot confirm is actually an Eye-Fi card. If "
            f"you've verified it yourself (e.g. `blkid {device}` or `sudo "
            "blkid` reads AA52-6922), pass --force.")
    matches = [mount_point / name for name in EYEFI_DIR_NAMES if (mount_point / name).is_dir()]
    # eyefi-config's eyefi_file_on() hardcodes "EyeFi" literally (sprintf("%s/EyeFi/%s", ...))
    # when there's no existing directory to find -- confirmed by running the actual compiled
    # binary with debug output against real hardware, not just reading the source. Match that
    # casing exactly rather than an all-caps EYEFI some factory-written cards happen to use.
    directory = matches[0] if matches else mount_point / "EyeFi"
    directory.mkdir(exist_ok=True)
    created = []
    zeros = bytes(BLOCK)
    # Same source: eyefi_file_name() returns these lowercase, and the user's debug log of the
    # real binary creating them on real hardware confirms it writes lowercase names. transfer()
    # still finds them afterward since vfat long-name lookup is case-insensitive, but this should
    # write exactly what the verified real tool writes, not a merely-equivalent casing.
    for name in ("reqc", "reqm", "rspc", "rspm"):
        path = directory / name
        if path.exists():
            continue
        with path.open("xb") as stream:
            stream.write(zeros)
            stream.flush()
            os.fsync(stream.fileno())
        created.append(path)
    return directory, created


class Channel:
    """Request/response protocol over the card's four EYEFI channel files.

    FILE access must bypass the OS cache: the firmware only reacts to
    uncached block transfers, and the poll loop below would otherwise read a
    stale sequence number forever. O_DIRECT is the analog of Windows'
    FILE_FLAG_NO_BUFFERING; where a filesystem rejects it (some vfat mounts),
    fall back to a cached open and drop the page with POSIX_FADV_DONTNEED
    before each read so the firmware's latest block is fetched.
    """

    def __init__(self, drive=None):
        path = Path(drive) if drive else find_card()
        # Accept the card mount point (any EYEFI/EyeFi/eyefi casing beneath
        # it) or the EYEFI-style directory itself, in any casing at all.
        if path.name.upper() == "EYEFI":
            self.root = path
        else:
            matches = [path / name for name in EYEFI_DIR_NAMES if (path / name).is_dir()]
            self.root = matches[0] if matches else path / "EYEFI"
        if not self.root.is_dir():
            raise OSError(f"EYEFI directory not found: {self.root} (tried "
                           f"{', '.join(EYEFI_DIR_NAMES)} under {path})")
        self._warmed_up = False

    def transfer(self, name, data=None):
        if name not in {"REQM", "REQC", "RSPM", "RSPC"}:
            raise ValueError("Unknown diagnostic channel")
        write = data is not None
        if write and (name not in {"REQM", "REQC"} or len(data) != BLOCK):
            raise ValueError("Only complete request-channel blocks can be written")
        path = str(self.root / name)
        base = os.O_WRONLY | getattr(os, "O_SYNC", 0) if write else os.O_RDONLY
        direct = getattr(os, "O_DIRECT", 0)
        # OPEN_EXISTING equivalent: no O_CREAT, so a missing channel is an error.
        try:
            fd = os.open(path, base | direct) if direct else os.open(path, base)
            uncached = bool(direct)
        except OSError as error:
            if not direct or error.errno != errno.EINVAL:
                raise
            fd = os.open(path, base)  # O_DIRECT unsupported on this filesystem.
            uncached = False
        try:
            if not uncached and hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, 0, BLOCK, os.POSIX_FADV_DONTNEED)
            # Page-aligned buffer; O_DIRECT requires alignment, others tolerate it.
            buffer = mmap.mmap(-1, BLOCK)
            try:
                if write:
                    buffer[:] = data
                    done = os.pwritev(fd, [buffer], 0)
                    if done != BLOCK:
                        raise OSError(f"Short channel transfer: {done}/{BLOCK}")
                    os.fsync(fd)
                    return bytes(buffer)
                done = os.preadv(fd, [buffer], 0)
                if done != BLOCK:
                    raise OSError(f"Short channel transfer: {done}/{BLOCK}")
                return bytes(buffer)
            finally:
                buffer.close()
        finally:
            os.close(fd)

    def request(self, command, index=0, offset=0, timeout=20):
        if command not in {"k", "m"}:
            raise ValueError("Only dump/log read commands k/m are allowed")
        for value in (index, offset):
            if not 0 <= value <= 0xffffffff:
                raise ValueError("Index and offset must fit unsigned 32 bits")
        request = bytearray(BLOCK)
        request[0] = ord(command)
        if command == "k":
            struct.pack_into(">II", request, 1, index, offset)
        else:
            struct.pack_into(">I", request, 1, offset)
        return self._submit_and_wait(bytes(request), timeout)

    def _submit_and_wait(self, request, timeout=20):
        """Submit a request and wait for its ack, retrying with a fresh
        submission (new sequence, rewritten REQM/REQC) once if this
        Channel's very first exchange times out.

        Some cards (seen: an Eye-Fi Mobi) can be slow to answer the first
        query right after being mounted -- possibly a Wi-Fi radio that only
        wakes on the write. Waiting longer on the same stale request is not
        what recovered it when this was first seen: re-running the whole
        command (a fresh submission) did. Once any exchange has succeeded,
        a later timeout is treated as real again and is not retried here --
        this must not mask sweep's own drop-detection, which depends on a
        timeout meaning exactly what it says once the channel is warmed up.
        """
        try:
            response = self._wait_response(self._submit(request), timeout)
        except TimeoutError:
            if self._warmed_up:
                raise
            response = self._wait_response(self._submit(request), timeout)
        self._warmed_up = True
        return response

    def _submit(self, request):
        """Write one request block and signal it; return the awaited sequence."""
        if len(request) != BLOCK:
            raise ValueError("Request must be one full channel block")
        sequence = (struct.unpack_from("<I", self.transfer("RSPC"))[0] + 1) & 0xffffffff
        self.transfer("REQM", request)
        control = bytearray(BLOCK)
        struct.pack_into("<I", control, 0, sequence)
        self.transfer("REQC", bytes(control))
        return sequence

    def _wait_response(self, sequence, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if struct.unpack_from("<I", self.transfer("RSPC"))[0] == sequence:
                return self.transfer("RSPM")
            time.sleep(0.2)
        raise TimeoutError("Card did not acknowledge the diagnostic request")

    # 'o' sub-commands, from hansendc/eyefi-config's card_info_subcommand enum
    # (verified against that source, not guessed): 1 MAC, 2 firmware, 3 card's
    # own unique key, 0xfd upload key. All are plain queries -- nothing here
    # writes to the card or can trigger a reset.
    def card_info(self, subcommand, timeout=20):
        """Send 'o' <subcommand> and return the raw response block."""
        request = bytes([ord("o"), subcommand]) + bytes(BLOCK - 2)
        return self._submit_and_wait(request, timeout)

    @staticmethod
    def _pascal_string(response):
        """Decode a length-byte-prefixed ASCII field (eyefi-config's pascal_string)."""
        length = response[0]
        return response[1:1 + length].rstrip(b"\0").decode("ascii", "replace")

    def firmware(self, timeout=20):
        """Read the card's reported firmware version string ('o' 2)."""
        return self._pascal_string(self.card_info(2, timeout))

    def mac_address(self):
        """Read the card's MAC address as lowercase hex, no separators ('o' 1)."""
        response = self.card_info(1)
        length = response[0]
        return response[1:1 + length].hex().lower()

    def card_key(self):
        """Read the card's own unique key (not the upload key) ('o' 3)."""
        return self._pascal_string(self.card_info(3))

    def upload_key(self):
        """Try reading the upload key directly ('o' 0xfd); '' if unsupported/unset.

        This is the same safe query eyefi-config's -u uses. On firmware that
        doesn't implement it, or on a card with no key assigned, this comes
        back empty -- that is NOT proof the key doesn't exist in RAM, only
        that this read-only query doesn't expose it. crash/reboot-dump/sweep
        exist for exactly that case.
        """
        return self._pascal_string(self.card_info(0xfd))

    def signal_assertion(self, opcode=ord("?")):
        # 4.5166: '?' -> 0x96cf6 -> 0x18dc0 -> crash save -> reset (traced statically).
        # Deliberately no acknowledgement wait or retry: the reset may prevent an ACK.
        return self._submit(bytes([opcode]) + bytes(BLOCK - 1))

    def noarg(self, command):
        """Send a bare one-byte command ('g' scan, 'l' list, 's' test-state poll)."""
        return self._submit_and_wait(bytes([ord(command)]) + bytes(BLOCK - 1))

    def net_request(self, req, essid, key_len=0, key_bytes=b""):
        """Send a network add/test/remove request (hansendc/eyefi-config's
        net_request: req byte, essid_len, essid[32], key{len, bytes[32]}),
        verified against that source's exact struct layout.
        """
        essid_bytes = essid.encode("ascii")[:32]
        block = bytearray(BLOCK)
        block[0] = ord(req)
        block[1] = len(essid_bytes)
        block[2:2 + len(essid_bytes)] = essid_bytes
        block[34] = key_len
        block[35:35 + len(key_bytes)] = key_bytes[:32]
        return self._submit_and_wait(bytes(block))


# Firmware >= this version is expected to expose the upload key via the
# direct 'o' 0xfd query; older firmware keeps the key but has no command to
# read it back. Same threshold as eyefi-server's cardconfig.py _FW_READS_KEY,
# kept in sync deliberately so both tools draw the same conclusion from a
# version string.
_FW_EXPOSES_UPLOAD_KEY = (5, 2)


def firmware_exposes_upload_key(firmware):
    """True/False/None (version unparseable) per _FW_EXPOSES_UPLOAD_KEY."""
    match = re.match(r"(\d+)\.(\d)", firmware or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2))) >= _FW_EXPOSES_UPLOAD_KEY


# Trigger bytes confirmed, on real hardware, to make `crash`'s mechanism (one
# bare host command byte, no reboot) produce a dump -- keyed on the exact
# leading version token channel.firmware() reports (e.g. "5.2010"), not a
# major.minor range: a byte confirmed on one exact build is not assumed to
# carry to a different build just because the version number looks close.
# '?' (0x3f) for 4.5166 is NOT listed here -- it's the function's own
# fallback default (traced via static analysis, predates this table), kept
# as the literal default rather than duplicated as an entry.
KNOWN_CRASH_TRIGGERS = {
    "5.2010": ord("\x01"),  # found via `sweep --scan-every 1`; checkpoints a dump with no drop
}


def _firmware_version_token(firmware):
    match = re.match(r"\S+", firmware or "")
    return match.group(0) if match else None


def _nearest_existing(path):
    path = path.resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def on_card_volume(output, card_root):
    """True if output would land on the same volume (device) as the card."""
    try:
        return os.stat(_nearest_existing(output)).st_dev == os.stat(card_root).st_dev
    except OSError:
        return False  # Card not mounted; the transfer will fail with a clear error.


def find_keys(data, mac=None):
    data = bytes(data)
    if mac:
        text = re.sub(r"[:-]", "", mac)
        if not re.fullmatch(r"[0-9a-fA-F]{12}", text):
            raise ValueError("MAC must contain exactly 12 hexadecimal digits")
        starts = [m.start() - 0x89 for m in re.finditer(re.escape(bytes.fromhex(text)), data)]
    else:
        starts = [m.start() - 0x80 for m in re.finditer(re.escape(BOARD_MARKER), data)]
    results = []
    for start in starts:
        if start < 0 or start + 0x94 > len(data):
            continue
        fields = [data[start + i:start + i + 32] for i in (0, 0x20, 0x40)]
        address = data[start + 0x89:start + 0x8f]
        if (data[start + 0x80:start + 0x88] != BOARD_MARKER
                or address in {b"\0" * 6, b"\xff" * 6}
                or not all(re.fullmatch(rb"[0-9a-fA-F]{32}", f) for f in fields)):
            continue
        results.append((start, address.hex(), fields[2].decode("ascii").lower()))
    return results


def save_keys(path, records):
    """Merge (offset, mac, key) records into a local {mac: [keys...]} JSON file.

    Keeps every distinct key seen for a MAC rather than overwriting -- a
    conflict here is a real finding (see find_keys/show_keys), not something
    to silently resolve by picking one. Returns (added, conflicted_macs).
    """
    store = {}
    if path.exists():
        store = json.loads(path.read_text() or "{}")
    added = 0
    for _, mac, key in records:
        existing = store.setdefault(mac, [])
        if key not in existing:
            existing.append(key)
            added += 1
    path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    conflicted = sorted(mac for mac, keys in store.items() if len(keys) > 1)
    return added, conflicted


def show_keys(data, mac, keys_file=None, summary=True):
    records = find_keys(data, mac)
    if not records:
        print("No matching board data found in this range.")
        return 1
    if VERBOSE:
        for offset, address, key in records:
            print(f"Board offset: 0x{offset:x}; MAC: {address}; "
                  f"Desktop UploadKey: {c(key, 'green', 'bold')}")
    elif summary:
        print_key_summary(records)
    if keys_file is not None:
        added, conflicted = save_keys(keys_file, records)
        if added:
            vprint(f"Saved {added} new key(s) to {keys_file}")
        if conflicted:
            print(f"Conflicting stored keys for: {', '.join(conflicted)} -- see {keys_file}", file=sys.stderr)
    by_mac = {}
    for _, address, key in records:
        by_mac.setdefault(address, set()).add(key)
    if any(len(keys) > 1 for keys in by_mac.values()):
        print("Conflicting keys for the same MAC; no unique result.", file=sys.stderr)
        return 2
    return 0


def scan_dumps(channel, output, first, last, size, mac=None, keys_file=None, show=True):
    """Save every requested range; do not mistake an unchanged response for a dump.

    show=False suppresses the per-slot key summary (the caller prints its own,
    e.g. sweep with the trigger-byte context); the save-to-keys-file still runs.
    """
    if on_card_volume(output, channel.root):
        raise ValueError("Output must be on a different volume from the card")
    output.mkdir(parents=True, exist_ok=False)
    summary = []
    all_records = []
    for index in range(first, last + 1):
        print(f"Reading dump {index}: {size} bytes...", flush=True)
        baseline = channel.transfer("RSPM")
        changed = False
        data = bytearray()
        target = output / f"dump{index}.response.bin"
        with target.open("xb") as stream:
            for offset in range(0, size, BLOCK):
                block = channel.request("k", index, offset)
                changed |= block != baseline
                stream.write(block)
                data.extend(block)
        # Always search: an "unchanged" response can still carry board data,
        # and the marker check is a strong enough signal on its own.
        records = find_keys(data, mac)
        summary.append({
            "dump_index": index, "file": target.name,
            "requested_bytes": size, "response_changed": changed,
            "status": "board_data_found" if records else
                      "changed_response_no_board_data" if changed else
                      "unchanged_response_unconfirmed",
            "records": [{"offset": offset, "mac": address, "desktop_upload_key": key}
                        for offset, address, key in records],
        })
        # Retain partial scan results if a later slot cannot be read.
        (output / "results.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        if records:
            show_keys(data, mac, keys_file, summary=show)
            all_records.extend(records)
        elif not changed:
            vprint(f"Dump {index}: unchanged response; no board data found.")
        else:
            vprint(f"Dump {index}: response saved, no matching board data.")
    vprint(f"Scan saved in: {output}")
    vprint("Reads slot 1 by default (the crash mirrors the same record into all slots); "
           "--last-index 3 reads all three. No dump listing or EOF status is available.")
    if not all_records:
        # Silence here is indistinguishable from a hang -- always say something,
        # even in quiet mode; the detail (which slots, changed vs unchanged) is
        # still behind -v via the vprint lines above.
        if show:
            print("No board data found.")
        return 1
    keys = {}
    for _, address, key in all_records:
        keys.setdefault(address, set()).add(key)
    if any(len(values) > 1 for values in keys.values()):
        print("Conflicting Desktop keys across dumps; see results.json.", file=sys.stderr)
        return 2
    return 0


def peek_board_data(channel, first, last, size, mac=None):
    """Read the dump slots into memory and return the first slot's board
    records, or None -- writing nothing to disk.

    For fast, repeated checkpoint scanning during a sweep: a full scan_dumps
    writes a dump directory every time, which is wasteful when scanning after
    every byte. Narrow the slots read with --last-index (e.g. just slot 1) to
    make each peek cheaper.
    """
    for index in range(first, last + 1):
        data = bytearray()
        for offset in range(0, size, BLOCK):
            data.extend(channel.request("k", index, offset))
        records = find_keys(bytes(data), mac)
        if records:
            return records
    return None


def _wait_for_card(channel, timeout, delay=2):
    """Poll for the card to respond again, printing a live countdown.

    Returns the elapsed seconds on success, or None if it never responded
    within `timeout` seconds -- the caller decides what that means (raise,
    continue a sweep, etc).

    Each liveness probe uses a short timeout of its own (<= delay), not the
    normal 20s ack-wait: without that, a single unresponsive probe can block
    silently for up to 20s while the displayed countdown number sits frozen,
    which looks exactly like a hang even though it's waiting as designed.
    """
    retries = max(1, int(timeout // delay))
    deadline = retries * delay
    probe_timeout = max(0.2, min(delay, 2))
    print(f"Waiting up to {deadline:.0f}s for the card to come back...", flush=True)
    for attempt in range(retries):
        remaining = max(0, round(deadline - attempt * delay))
        print(f"\r  {remaining:3d}s left...  ", end="", flush=True)
        try:
            channel.firmware(timeout=probe_timeout)
            elapsed = (attempt + 1) * delay
            print(f"\r  card responded again after ~{elapsed:.0f}s.{' ' * 10}")
            return elapsed
        except OSError:
            pass
        time.sleep(max(0, delay - probe_timeout))
    print(f"\r  card did not respond within {deadline:.0f}s.{' ' * 10}")
    return None


def reboot_and_dump(channel, output, first, last, size, mac=None, wait_timeout=60, delay=2, keys_file=None):
    """Reboot the card ('b'), wait for it to come back, then scan for a dump.

    On firmware traced as 5.0018, a plain reboot checkpoints the board data
    into a dump slot before the card resets -- no assertion/crash trigger
    needed. This is the ordinary reboot command (the same one eyefi-config -b
    sends), not an unsupported or out-of-range request. Still: don't run it
    while the card is writing photos, and expect the card to disconnect and
    remount during the wait.
    """
    if on_card_volume(output, channel.root):
        raise ValueError("Output must be on a different volume from the card")
    print("Sending reboot request ('b')...", flush=True)
    try:
        channel.signal_assertion(ord("b"))
    except OSError as error:
        # A reset during the write is the expected case, not a failure.
        print(f"I/O ended while signalling reboot (expected if the card reset immediately): {error}")
    if _wait_for_card(channel, wait_timeout, delay) is None:
        raise OSError(f"Card did not respond again within {wait_timeout}s after reboot. "
                       "Check the mount; once it remounts, 'scan' may still find the dump.")
    print("Scanning dump slots for board data left behind by the reboot...", flush=True)
    return scan_dumps(channel, output, first, last, size, mac, keys_file)


def recover(drive, output, mac=None, keys_file=None, no_reboot=False, wait_timeout=60, size=0x50000, force=False):
    """Try every non-destructive-first key-recovery method here, in order,
    stopping at the first that finds a key:

      1. Direct query (channel.upload_key(), the 'o' 0xfd query -- same as
         eyefi-config -u). Only exposes a real key on firmware >= 5.2
         (firmware_exposes_upload_key); empty elsewhere is not proof of no key.
      2. Existing dump slots, read-only (peek_board_data -- writes nothing to
         disk; a key may already be sitting there from a previous crash/reboot).
      3. reboot-dump: an ordinary reboot, confirmed to checkpoint board data
         on firmware 5.0018/5.0019. Writes a real backup via reboot_and_dump,
         same as running 'reboot-dump' directly. Skipped with --no-reboot.

    If the EYEFI channel directory itself is missing (a reformatted card --
    the firmware does not recreate it, only the host does; see
    restore-channels), this recreates it first via recreate_channel_files
    and then proceeds with the ladder above. That needs an explicit `drive`:
    there is no EYEFI dir yet for auto-detection to key off of.
    """
    try:
        channel = Channel(drive)
    except OSError as error:
        if drive is None:
            raise
        print(f"Channel open failed ({error}); trying restore-channels...", flush=True)
        directory, created = recreate_channel_files(drive, force=force)
        if created:
            print(f"Created in {directory}: {', '.join(p.name for p in created)}")
        else:
            print(f"{directory}: channel files already present; the problem is something else.")
        channel = Channel(drive)

    try:
        print(f"Firmware: {channel.firmware()}", flush=True)
    except (OSError, ValueError) as error:
        print(f"Firmware: unknown ({error})", flush=True)

    print("\n[1/3] Direct query (upload_key)", flush=True)
    try:
        key = channel.upload_key()
    except (OSError, ValueError) as error:
        key = None
        print(f"  failed: {error}")
    if key:
        records = [(0, channel.mac_address(), key)]
        print_key_summary(records, "direct query")
        if keys_file is not None:
            save_keys(keys_file, records)
        return 0
    print("  empty -- not proof the key is missing, only that this query doesn't expose it here")

    print("\n[2/3] Existing dump slots (read-only; nothing written to disk)", flush=True)
    try:
        records = peek_board_data(channel, 1, 3, size, mac)
    except (OSError, ValueError) as error:
        records = None
        print(f"  failed: {error}")
    if records:
        print_key_summary(records, "existing dump")
        if keys_file is not None:
            save_keys(keys_file, records)
        return 0
    print("  no board data found in slots 1-3")

    if no_reboot:
        print("\n[3/3] reboot-dump: skipped (--no-reboot)", flush=True)
        print("\nFAILED: no method recovered a key.")
        return 1
    print("\n[3/3] reboot-dump (the card will disconnect for a few seconds; "
          "not while it is writing photos)", flush=True)
    rc = reboot_and_dump(channel, output, 1, 1, size, mac, wait_timeout, keys_file=keys_file)
    if rc != 0:
        print("\nFAILED: no method recovered a key.")
    return rc


# Wire bytes confirmed (from hansendc/eyefi-config's real source -- see the
# Wi-Fi section) to be legitimate, state-changing network commands rather
# than anything that could produce a crash dump. Sending them bare, as sweep
# does, means an empty/garbage ESSID and a zero-length key -- 'd' (remove)
# sent this way is what dropped a card during an earlier, less careful run.
# Skipped by default; --include-wifi-bytes overrides this for an exhaustive
# sweep. 'b' (reboot) is deliberately NOT in this set: it's the one byte
# that's actually useful to find, and it's how reboot-dump's trigger was
# discovered in the first place.
WIFI_MUTATING_BYTES = {ord("a"), ord("d"), ord("t")}  # add, remove, test network


# Phase C/D's deliberately out-of-range k/m parameters -- never found anything
# in practice, but kept available (opt-in) since the original ad hoc script
# tried them. Same values as that script, not re-derived.
_BOUNDS_K_INDICES = (0xffffffff, 0x80000000, 0x40000000, 0xdeadbeef, 0x00010000, 0x00000100, 0x5, 0x4)
_BOUNDS_K_OFFSETS = (0x1, 0x3fff, 0x4001, 0x00080000, 0x40000000, 0x7fffffff, 0xfffffff1, 0xffffffff)
_BOUNDS_M_OFFSETS = (0x1, 0x3fff, 0x00080000, 0x40000000, 0x7fffffff, 0xfffffff1, 0xffffffff)


def _sweep_candidates(min_byte, max_byte, skip_bytes, include_o_subcodes, include_bounds_matrix):
    """Build the [(label, 16384-byte request)] list sweep_for_dump tries, in order.

    Phase A (always): every command byte min_byte..max_byte, minus skip_bytes.
    Phase B (--include-o-subcodes): 'o' sub-commands 0x20..0xff -- beyond the
    known-safe query range (firmware/MAC/card-key/upload-key/etc. are <= 0x1f
    or the known 0xfd), unexplored.
    Phase C/D (--include-bounds-matrix): 'k'/'m' with deliberately
    out-of-range indices/offsets, hoping to trip a bounds check. Neither
    phase has ever produced a dump in practice; both can issue requests no
    normal client would send.
    """
    candidates = []
    for byte in range(min_byte, max_byte + 1):
        if byte in skip_bytes:
            continue
        candidates.append((f"cmd=0x{byte:02x}", bytes([byte]) + bytes(BLOCK - 1)))
    if include_o_subcodes:
        for sub in range(0x20, 0x100):
            candidates.append((f"o sub=0x{sub:02x}", bytes([ord("o"), sub]) + bytes(BLOCK - 2)))
    if include_bounds_matrix:
        for idx in _BOUNDS_K_INDICES:
            for off in _BOUNDS_K_OFFSETS:
                label = f"k idx=0x{idx:08x} off=0x{off:08x}"
                candidates.append((label, b"k" + struct.pack(">II", idx, off) + bytes(BLOCK - 9)))
        for off in _BOUNDS_M_OFFSETS:
            candidates.append((f"m off=0x{off:08x}", b"m" + struct.pack(">I", off) + bytes(BLOCK - 5)))
    return candidates


def sweep_for_dump(channel, output, first, last, size, mac=None, min_byte=0x00,
                    max_byte=0xff, max_drops=25, settle_timeout=90, delay=2,
                    skip_bytes=WIFI_MUTATING_BYTES, include_o_subcodes=False,
                    include_bounds_matrix=False, keys_file=None, scan_every=25):
    """Sweep command bytes looking for one that makes the firmware save a dump.

    Tries every byte min_byte..max_byte (min_byte == max_byte retests a single
    byte) as a bare, unargumented host command, watching for the card to stop
    acknowledging -- a sign it reset. After each drop it waits for the card to
    come back and scans the dump slots, stopping as soon as board data
    actually appears rather than grinding through the rest. This is how the
    'b' (reboot) trigger on firmware 5.0018/5.0019 was actually found; the
    default crash opcode '?' is a single byte traced on 4.5166 specifically,
    while this has no firmware assumption at all.

    skip_bytes (default WIFI_MUTATING_BYTES) are never sent -- known
    legitimate commands with a real effect that isn't a crash dump, so
    trying them would just pollute the card's Wi-Fi config for nothing.

    include_o_subcodes and include_bounds_matrix add two further,
    off-by-default phases (unexplored 'o' sub-commands, and deliberately
    out-of-range 'k'/'m' parameters) that an earlier, ad hoc version of this
    sweep tried; neither has ever produced a dump, and the bounds matrix in
    particular sends requests no normal client would.

    This is NOT limited to known-safe commands otherwise: it can issue
    whatever else the firmware exposes, including -- in principle --
    something destructive rather than merely a reset. Only run it against a
    card you can afford to lose, and watch it run rather than treating it as
    fire-and-forget.
    """
    if on_card_volume(output, channel.root):
        raise ValueError("Output must be on a different volume from the card")
    output.mkdir(parents=True, exist_ok=False)
    print("Starting sweep...", flush=True)
    try:
        print(f"Firmware before sweep: {channel.firmware()}", flush=True)
    except OSError as error:
        print(f"Card not responding before the sweep started: {error}", file=sys.stderr)
        raise
    candidates = _sweep_candidates(min_byte, max_byte, skip_bytes, include_o_subcodes, include_bounds_matrix)
    skipped = sorted(b for b in skip_bytes if min_byte <= b <= max_byte)
    if skipped:
        vprint(f"Skipping known Wi-Fi command bytes: {', '.join(f'0x{b:02x}' for b in skipped)} "
               "(pass --include-wifi-bytes to try them too)", flush=True)

    def scan_after_event(label):
        """Persist a scan; return (dir, records) if board data found, else None."""
        safe_label = re.sub(r"[^A-Za-z0-9_.=-]+", "_", label)
        attempt_dir = output / f"after-{safe_label}"
        scan_dumps(channel, attempt_dir, first, last, size, mac, keys_file, show=VERBOSE)
        slots = json.loads((attempt_dir / "results.json").read_text())
        records = [(r["offset"], r["mac"], r["desktop_upload_key"])
                   for slot in slots for r in slot["records"]]
        return (attempt_dir, records) if records else None

    def check_present(label):
        """Fast in-memory peek; on a hit, persist + save. Returns (dir, records) or None."""
        if peek_board_data(channel, first, last, size, mac) is None:
            return None
        return scan_after_event(label)

    def report(hit, context, clear_line=False):
        """Print the result for a hit: the coloured MAC/key summary (quiet) or
        the detail already printed plus the location (verbose)."""
        attempt_dir, records = hit
        if clear_line and not VERBOSE:
            print()  # move off the live '\r trying ...' line
        if VERBOSE:
            print(f"!!! Board data found ({context}): {attempt_dir}", flush=True)
        else:
            print_key_summary(records, context)

    # A trigger byte can make the firmware save a dump WITHOUT the card ever
    # dropping -- invisible to the drop-detection below. So peek up front (a
    # card that already has a dump needs no sweep at all) and again every
    # scan_every bytes, not only after a drop. Peeks are in-memory, so per-byte
    # scanning (--scan-every 1) does not spray a dump directory per byte.
    print("Checking for board data already present on the card...", flush=True)
    hit = check_present("baseline")
    if hit:
        report(hit, "already present on card")
        return 0

    total = len(candidates)
    drops = []
    last_label = None
    try:
        for position, (label, request) in enumerate(candidates, 1):
            last_label = label
            print(f"\r  trying {label} ({position}/{total})   ", end="", flush=True)
            try:
                channel._submit(request)
                channel.firmware()  # liveness probe
                dropped = False
            except Exception as error:
                print(f"\n>>> Card stopped responding after [{label}] ({type(error).__name__})", flush=True)
                drops.append(label)
                dropped = True

            if dropped:
                if _wait_for_card(channel, settle_timeout, delay) is None:
                    raise OSError(f"Card did not come back after [{label}] within "
                                   f"{settle_timeout}s. Check the mount; it may need a manual remount.")
                print(f"Scanning for a dump left by [{label}]...", flush=True)
                hit = scan_after_event(label)
                if hit:
                    report(hit, f"found after byte {label}", clear_line=True)
                    return 0
                print(f"No board data after [{label}]; continuing sweep.", flush=True)
                if len(drops) >= max_drops:
                    print(f"\n{max_drops} drops with no board data found; stopping sweep early.", file=sys.stderr)
                    return 1
            elif scan_every and position % scan_every == 0:
                # Checkpoint: catch a byte that saved a dump without dropping the
                # card. With scan_every == 1 the trigger is exactly this byte;
                # otherwise it is somewhere in this batch.
                hit = check_present(f"checkpoint-{position:04d}")
                if hit:
                    context = (f"found at byte {label}" if scan_every == 1
                               else f"found by a byte up to {label} (checkpoint after {position})")
                    report(hit, context, clear_line=True)
                    return 0
    except KeyboardInterrupt:
        # The byte being tried when interrupted may have just produced a dump.
        # Wait for the card to settle and scan, so an interrupt mid-trigger does
        # not throw away a dump that is now sitting in the slots.
        print(f"\nInterrupted (last byte tried: {last_label}). Checking for a dump "
              "it may have left behind...", flush=True)
        if _wait_for_card(channel, settle_timeout, delay) is not None:
            hit = scan_after_event(f"interrupted-{last_label}")
            if hit:
                report(hit, f"found; last byte tried was {last_label}")
                return 0
            print("No board data in the slots. If the card is still recovering, "
                  "re-run 'scan' in a moment.", file=sys.stderr)
        else:
            print("Card did not respond after the interrupt; once it remounts, "
                  "run 'scan' manually to check for a dump.", file=sys.stderr)
        return 130
    if total == 0:
        print("Nothing to try: every candidate in that range is a skipped Wi-Fi command. "
              "Pass --include-wifi-bytes to retest one of them anyway.", file=sys.stderr)
    else:
        print(f"\nSwept all {total} candidate(s); none produced a dump.", flush=True)
    return 1


def parse_opcode(text):
    """Accept a single character (e.g. x) or a hex byte (e.g. 0x1b)."""
    if re.fullmatch(r"0[xX][0-9a-fA-F]{1,2}", text):
        value = int(text, 16)
    elif len(text) == 1:
        value = ord(text)
    else:
        raise argparse.ArgumentTypeError("Use a single character or a hex byte like 0x1b")
    if not 0 <= value <= 0xff:
        raise argparse.ArgumentTypeError("Command byte must be in 0x00..0xff")
    return value


def crash_card(channel, output, first, last, size, opcode=None, keys_file=None):
    """Back up dump slots, then send a trigger byte to the plain crash mechanism.

    opcode=None (the CLI default) auto-picks: if the card's exact firmware
    version matches KNOWN_CRASH_TRIGGERS, that confirmed byte is used;
    otherwise it falls back to '?' (0x3f), traced via static analysis on
    firmware 4.5166 only -- not gated on version, sent regardless, same as
    always. Passing --command overrides either way. A backup I/O failure
    aborts before any send; the send is never retried, since a reset may
    prevent an acknowledgement.
    """
    if on_card_volume(output, channel.root):
        raise ValueError("Output must be on a different volume from the card")
    try:
        firmware = channel.firmware()
        print(f"Firmware: {firmware}", flush=True)
    except (OSError, ValueError) as error:
        firmware = None
        print(f"Firmware: unknown ({error})", flush=True)
    token = _firmware_version_token(firmware)
    known_byte = KNOWN_CRASH_TRIGGERS.get(token)
    if opcode is None:
        if known_byte is not None:
            opcode = known_byte
            print(f"Firmware {token} matches a known trigger byte 0x{opcode:02x} "
                  "(confirmed via sweep on this exact version); using it. "
                  "Pass --command to override.", flush=True)
        else:
            opcode = ord("?")
    elif known_byte is not None and opcode != known_byte:
        print(f"Warning: firmware {token} has a known trigger byte 0x{known_byte:02x}, but "
              f"0x{opcode:02x} was passed explicitly; proceeding with 0x{opcode:02x} as requested.",
              file=sys.stderr)
    if opcode == ord("?") and firmware and not re.match(r"^4\.5166(?:\s|$)", firmware):
        print(f"Warning: '?' was traced on firmware 4.5166; this card reports {firmware!r}. "
              "Behavior is undefined, but proceeding as requested.", file=sys.stderr)
    print("Backing up dump slots before the assertion/reset request...", flush=True)
    # A backup I/O failure must prevent the trigger: let OSError propagate.
    scan_dumps(channel, output, first, last, size, keys_file=keys_file)
    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "firmware": firmware,
        "command_byte": opcode,
        "backup_dir": str(output),
        "mechanism": "Unsupported '?' command -> assertion/save/reset (traced on firmware 4.5166)",
        "live_crash_behavior_verified": False,
        "status": "prepared_not_sent",
    }
    report_path = output / "crash-request.json"

    def save_report():
        # fsync so the staged status survives an abrupt card/bus reset mid-trigger.
        with report_path.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    save_report()
    print(f"Sending assertion/reset request once (0x{opcode:02x}). A fresh dump may replace dump3.", flush=True)
    # Persist uncertainty before the trigger write: a reset during the write must
    # not later be mistaken for "never sent".
    report["status"] = "send_started_outcome_unverified"
    save_report()
    try:
        report["sequence"] = channel.signal_assertion(opcode)
    except OSError as error:
        report["status"] = "send_error_outcome_unknown"
        report["error"] = str(error)
        save_report()
        print("I/O failed during signalling; the card may already have reset. Do not retry blindly.",
              file=sys.stderr)
        raise
    report["status"] = "request_signalled_crash_unverified"
    save_report()
    print("Request signalled. This does not confirm a crash or a new dump.")
    print("After the card recovers, read its log and scan again to verify the result.")
    print(f"Pre-trigger backups and request report: {output}")
    return 0


# -- Wi-Fi configuration -----------------------------------------------------
# Ported from hansendc/eyefi-config's net_request/network_action/scan_nets/
# fetch_configured_nets/make_network_key, read against that project's actual
# source rather than guessed, so the card's own USB-side eyefi-config binary
# is no longer needed for this.

_NET_TYPES = ("none", "WEP", "WPA", "unknown1", "WPA2")
_NET_TEST_STATES = ("not scanning", "locating network", "verifying network key",
                    "waiting for DHCP", "testing connection to Eye-Fi server", "success")


def derive_network_key(essid, passphrase):
    """Mirror make_network_key(): (key_len, 32-byte key buffer) for a password.

    A 64-hex-digit or WEP-length (26/10 hex digit) password is used as a raw
    key as-is. Anything else is treated as a WPA ASCII passphrase and run
    through PBKDF2-HMAC-SHA1 with the ESSID as salt, 4096 iterations, 256-bit
    output -- the ordinary WPA/WPA2 passphrase-to-PSK derivation (RFC 2898 /
    802.11i), not anything Eye-Fi-specific.
    """
    is_hex = bool(passphrase) and re.fullmatch(r"[0-9a-fA-F]+", passphrase) is not None
    if is_hex and len(passphrase) in (64, 26, 10):
        raw = bytes.fromhex(passphrase)
        return len(raw), raw.ljust(32, b"\0")
    psk = hashlib.pbkdf2_hmac("sha1", passphrase.encode("ascii"), essid.encode("ascii"), 4096, dklen=32)
    return 32, psk


def scan_networks(channel):
    """Scan for nearby Wi-Fi networks ('g'). Returns [(essid, strength, type)]."""
    response = channel.noarg("g")
    count = response[0]
    nets = []
    for i in range(count):
        entry = response[1 + i * 34: 1 + (i + 1) * 34]
        essid = entry[:32].rstrip(b"\0").decode("ascii", "replace")
        strength = struct.unpack_from("b", entry, 32)[0]
        net_type = entry[33]
        nets.append((essid, strength, _NET_TYPES[net_type] if net_type < len(_NET_TYPES) else "unknown"))
    return nets


def list_networks(channel):
    """List networks already configured on the card ('l'). Returns [essid]."""
    response = channel.noarg("l")
    count = response[0]
    return [response[1 + i * 32: 1 + (i + 1) * 32].rstrip(b"\0").decode("ascii", "replace")
            for i in range(count)]


def add_network(channel, essid, password=None, force=False):
    """Test the connection first and only add if it succeeds, matching
    eyefi-config's own 'add' (-a) behavior exactly: it does the same test
    before committing an add, and only skips that test under --force. This
    is what stops a missing/wrong password from silently landing as a
    broken open-network entry. Returns True if the network was actually
    added, False if the test failed and force wasn't given.
    """
    if not force and not test_network(channel, essid, password):
        return False
    key_len, key_bytes = derive_network_key(essid, password) if password else (0, b"")
    channel.net_request("a", essid, key_len, key_bytes)
    return True


def remove_network(channel, essid):
    channel.net_request("d", essid)


CLOUD_CHECK_STATE = "testing connection to Eye-Fi server"
CLOUD_CHECK_GRACE_POLLS = 6  # ~3s at the default poll_delay


def test_network(channel, essid, password=None, max_polls=200, poll_delay=0.5):
    """Test a connection, printing state changes. Returns True on 'success',
    or if the card gets stuck specifically at CLOUD_CHECK_STATE.

    That state is only reached after the Wi-Fi key verifies, so the
    credentials are already proven correct by the time it's hit. Eye-Fi's
    own cloud service has been offline since their 2016 shutdown, so this
    exact final-validation step against it can never complete -- and there
    is no documented way to point the card at a different server instead
    (API_URL, 'o' sub-command 4, has no write path anywhere in eyefi-
    config's own source, same as the upload key). Waiting out the rest of
    max_polls for a server that no longer exists is pointless, so this
    treats getting stuck there as a pass rather than a failure.
    """
    key_len, key_bytes = derive_network_key(essid, password) if password else (0, b"")
    channel.net_request("t", essid, key_len, key_bytes)
    last_state = None
    stuck_at_cloud_check = 0
    for _ in range(max_polls):
        response = channel.noarg("s")
        code = response[0]
        state = _NET_TEST_STATES[code] if code < len(_NET_TEST_STATES) else "unknown"
        if state != last_state:
            print(f"Testing '{essid}': {state}", flush=True)
            last_state = state
            stuck_at_cloud_check = 0
        if state == "success":
            return True
        if state in ("not scanning", "unknown"):
            return False
        if state == CLOUD_CHECK_STATE:
            stuck_at_cloud_check += 1
            if stuck_at_cloud_check >= CLOUD_CHECK_GRACE_POLLS:
                print("Wi-Fi key verified; stuck only on reaching Eye-Fi's own cloud service, "
                      "which has been offline since their 2016 shutdown and can never complete "
                      "this step. Treating this as a pass.", flush=True)
                return True
        time.sleep(poll_delay)
    print("Gave up waiting for a final test state.", file=sys.stderr)
    return False


DEFAULT_KEYS_FILE = Path(__file__).parent / "eyefi-keys.json"


def _add_save_keys_arg(parser):
    parser.add_argument("--save-keys", nargs="?", type=Path, default=None, const=DEFAULT_KEYS_FILE,
                        metavar="FILE",
                        help=f"Append any key(s) found to a local {{mac: [keys]}} JSON file "
                             f"(default if given with no path: {DEFAULT_KEYS_FILE.name} beside the script). "
                             "Never writes anything to the card.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    extract = actions.add_parser("extract", help="Find keys in an existing local dump")
    extract.add_argument("file", type=Path)
    extract.add_argument("--mac", help="Optional MAC filter; default: detect board records")
    _add_save_keys_arg(extract)
    scan = actions.add_parser("scan", help="Read dump slots (defaults match firmware 4.5166; any firmware works)")
    scan.add_argument("drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    scan.add_argument("--output-dir", type=Path, help="New directory; default: timestamped folder beside script")
    scan.add_argument("--first-index", type=int, default=1)
    scan.add_argument("--last-index", type=int, default=1,
                        help="Last dump slot to read; default 1 (the crash mirrors the same record into all slots, so one is enough -- set 3 to read all three)")
    scan.add_argument("--bytes", type=int, default=0x50000,
                      help="Bytes per slot; default 327680 (firmware 4.5166 layout), override for others")
    scan.add_argument("--mac", help="Optional MAC filter")
    _add_save_keys_arg(scan)
    actions.add_parser("firmware", help="Read and print the card's firmware version").add_argument(
        "drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    actions.add_parser("info", help="Read firmware/MAC/card key/upload key via safe queries (no crash/reboot)").add_argument(
        "drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    restore = actions.add_parser("restore-channels",
        help="Recreate a missing EYEFI dir/REQM/REQC/RSPM/RSPC (e.g. after a format); refuses unless the card's "
             "volume ID proves it's really an Eye-Fi card")
    restore.add_argument("drive", help="Card mount point (required -- there's no EYEFI dir yet for auto-detect to key off of)")
    restore.add_argument("--force", action="store_true",
                         help="Skip the volume-ID check (e.g. if you've confirmed it yourself with "
                              "`blkid`/`sudo blkid` and this tool's own check still refuses)")
    wifi = actions.add_parser("wifi", help="Configure the card's Wi-Fi (scan/list/add/remove/test)")
    wifi_actions = wifi.add_subparsers(dest="wifi_action", required=True)
    wifi_actions.add_parser("scan", help="Scan for nearby networks").add_argument(
        "drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    wifi_actions.add_parser("list", help="List networks already configured on the card").add_argument(
        "drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    wifi_add = wifi_actions.add_parser("add", help="Add (and configure) a network")
    wifi_add.add_argument("drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    wifi_add.add_argument("essid", help="Network name")
    wifi_add.add_argument("--password", help="WPA passphrase, or a 64/26/10-hex-digit raw key; omit for an open network")
    wifi_add.add_argument("--force", action="store_true",
                          help="Add even if the connection test fails (matches eyefi-config's -a --force)")
    wifi_remove = wifi_actions.add_parser("remove", help="Remove a configured network")
    wifi_remove.add_argument("drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    wifi_remove.add_argument("essid", help="Network name")
    wifi_test = wifi_actions.add_parser("test", help="Test a connection without saving it")
    wifi_test.add_argument("drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
    wifi_test.add_argument("essid", help="Network name")
    wifi_test.add_argument("--password", help="WPA passphrase, or a 64/26/10-hex-digit raw key; omit for an open network")
    reboot = actions.add_parser("reboot-dump", help="Reboot the card, then scan for a dump left behind (traced on firmware 5.0018)")
    reboot.add_argument("drive", nargs="?", default=None, help="Card mount (omit to auto-detect); do not use while the card is writing photos")
    reboot.add_argument("--output-dir", type=Path, help="New directory; default: timestamped folder beside script")
    reboot.add_argument("--first-index", type=int, default=1)
    reboot.add_argument("--last-index", type=int, default=1,
                        help="Last dump slot to read; default 1 (the crash mirrors the same record into all slots, so one is enough -- set 3 to read all three)")
    reboot.add_argument("--bytes", type=int, default=0x50000, help="Bytes per slot; default 327680")
    reboot.add_argument("--wait-timeout", type=int, default=60, help="Max seconds to wait for the card to remount")
    reboot.add_argument("--mac", help="Optional MAC filter")
    _add_save_keys_arg(reboot)
    recover_parser = actions.add_parser("recover",
        help="Try every non-destructive-first key-recovery method in order: direct query, existing dumps, reboot-dump")
    recover_parser.add_argument("drive", nargs="?", default=None,
                         help="Card mount; omit to auto-detect -- but required if the EYEFI dir itself is missing, "
                              "since auto-detect has nothing to key off of then")
    recover_parser.add_argument("--output-dir", type=Path, help="New directory for reboot-dump's backup; default: timestamped folder beside script")
    recover_parser.add_argument("--bytes", type=int, default=0x50000, help="Bytes per slot; default 327680")
    recover_parser.add_argument("--wait-timeout", type=int, default=60, help="Max seconds to wait for the card to remount after reboot-dump")
    recover_parser.add_argument("--mac", help="Optional MAC filter")
    recover_parser.add_argument("--no-reboot", action="store_true", help="Stop after existing dumps; never send the reboot trigger")
    recover_parser.add_argument("--force", action="store_true",
                         help="If the EYEFI dir is missing, skip restore-channels' volume-ID check (see restore-channels --force)")
    _add_save_keys_arg(recover_parser)
    crash = actions.add_parser("crash", help="Experimental: back up dumps, then send one host command byte (any firmware)")
    crash.add_argument("drive", nargs="?", default=None, help="Card mount (omit to auto-detect); do not use while the card is writing photos")
    crash.add_argument("--command", type=parse_opcode, default=None,
                       help="Command byte to send; default: auto-pick a confirmed trigger if the card's exact "
                            "firmware is in KNOWN_CRASH_TRIGGERS, else fall back to '?' (0x3f, traced on firmware "
                            "4.5166 only). Pass a byte to override either way.")
    crash.add_argument("--output-dir", type=Path, help="New backup directory; default: eyefi-before-crash-* beside script")
    crash.add_argument("--first-index", type=int, default=1)
    crash.add_argument("--last-index", type=int, default=1,
                        help="Last dump slot to read; default 1 (the crash mirrors the same record into all slots, so one is enough -- set 3 to read all three)")
    crash.add_argument("--bytes", type=int, default=0x50000, help="Bytes per slot for the pre-crash backup")
    _add_save_keys_arg(crash)
    sweep = actions.add_parser("sweep", help="Try every command byte looking for one that leaves a dump (any firmware; stops at the first hit)")
    sweep.add_argument("drive", nargs="?", default=None, help="Card mount (omit to auto-detect); do not use while the card is writing photos")
    sweep.add_argument("--output-dir", type=Path, help="New directory; default: eyefi-sweep-* beside script")
    sweep.add_argument("--first-index", type=int, default=1)
    sweep.add_argument("--last-index", type=int, default=1,
                        help="Last dump slot to read; default 1 (the crash mirrors the same record into all slots, so one is enough -- set 3 to read all three)")
    sweep.add_argument("--bytes", type=int, default=0x50000, help="Bytes per slot checked after each drop")
    sweep.add_argument("--min-byte", type=lambda s: int(s, 0),
                       default=0x00, help="Lowest command byte to try (default 0x00)")
    sweep.add_argument("--max-byte", type=lambda s: int(s, 0),
                       default=0xff, help="Highest command byte to try; equal to --min-byte retests one byte")
    sweep.add_argument("--max-drops", type=int, default=25, help="Stop after this many resets with no dump found")
    sweep.add_argument("--settle-timeout", type=int, default=90, help="Max seconds to wait for the card after each drop")
    sweep.add_argument("--scan-every", type=int, default=1,
                       help="Scan the slots every N bytes even without a drop, to catch a byte that "
                            "saves a dump but keeps the card alive (default 1 -- scan after every byte; "
                            "the 5.2010/0x01 trigger was only found this way, since it never dropped the "
                            "card. 0 disables). Also scans once up front.")
    sweep.add_argument("--include-wifi-bytes", action="store_true",
                       help="Also try 'a'/'d'/'t' (add/remove/test network) -- skipped by default, "
                            "since they're confirmed Wi-Fi commands, not a path to a crash dump")
    sweep.add_argument("--include-o-subcodes", action="store_true",
                       help="Also try 'o' sub-commands 0x20-0xff (unexplored; never found a dump so far)")
    sweep.add_argument("--include-bounds-matrix", action="store_true",
                       help="Also try 'k'/'m' with deliberately out-of-range indices/offsets "
                            "(never found a dump so far; sends requests no normal client would)")
    sweep.add_argument("--mac", help="Optional MAC filter")
    _add_save_keys_arg(sweep)
    for action in ("read", "log"):
        command = actions.add_parser(action, help="Read a dump range" if action == "read" else "Read a log range")
        command.add_argument("drive", nargs="?", default=None, help="Card mount; omit to auto-detect (fails if 0 or 2+ cards are mounted)")
        command.add_argument("--output", required=True, type=Path, help="New file on another volume")
        command.add_argument("--offset", type=int, default=0)
        command.add_argument("--bytes", type=int, default=1048576 if action == "read" else BLOCK)
        if action == "read":
            command.add_argument("--dump-index", type=int, required=True, help="Existing dump number from card log")
            command.add_argument("--mac", help="Optional MAC filter")
            _add_save_keys_arg(command)
    for _sub in actions.choices.values():
        _sub.add_argument("-v", "--verbose", action="store_true",
                          help="Show full per-slot/per-byte progress, not just the final result")
    arguments = sys.argv[1:]
    # No arguments at all: scan whichever single card is auto-detected.
    if not arguments:
        arguments = ["scan"]
    # Shorthand: a leading non-option that is not a subcommand is a scan target.
    elif arguments[0] not in {"extract", "scan", "read", "log", "crash", "firmware",
                              "reboot-dump", "sweep", "info", "wifi", "restore-channels", "recover"} \
            and not arguments[0].startswith("-"):
        arguments = ["scan"] + arguments
    args = parser.parse_args(arguments)
    global VERBOSE
    VERBOSE = getattr(args, "verbose", False)
    try:
        if args.action == "extract":
            return show_keys(args.file.read_bytes(), args.mac, args.save_keys)
        if args.action == "scan":
            if not 0 <= args.first_index <= args.last_index <= 0xffffffff:
                parser.error("Invalid dump index range")
            if args.bytes <= 0 or args.bytes % BLOCK or args.bytes > 0x100000000:
                parser.error("Size must be a positive multiple of 16384, within 32-bit offsets")
            channel = Channel(args.drive)
            output = args.output_dir or Path(__file__).parent / ("eyefi-scan-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            return scan_dumps(channel, output.resolve(), args.first_index,
                              args.last_index, args.bytes, args.mac, args.save_keys)
        if args.action == "firmware":
            print(Channel(args.drive).firmware())
            return 0
        if args.action == "info":
            channel = Channel(args.drive)
            firmware = channel.firmware()
            print(f"Firmware:   {firmware}")
            print(f"MAC:        {channel.mac_address()}")
            print(f"Card key:   {channel.card_key()}")
            key = channel.upload_key()
            if key:
                print(f"Upload key: {c(key, 'green', 'bold')}")
                return 0
            reads = firmware_exposes_upload_key(firmware)
            if reads is False:
                print("Upload key: (empty -- this firmware does NOT expose the upload key via direct "
                      "query. That is not evidence the key is unassigned; it's very likely still in RAM.)")
            elif reads is True:
                print("Upload key: (empty -- this firmware should expose the upload key directly, and "
                      "genuinely read back empty. This card may be unactivated, but check the steps "
                      "below before concluding that.)")
            else:
                print("Upload key: (empty -- could not parse the firmware version to judge which case this is.)")
            print()
            print("To look for it instead:")
            print("  1. scan              -- read-only; the key may already be sitting in an existing dump.")
            print("  2. reboot-dump       -- an ordinary reboot; confirmed to leave the key behind on 5.0018/5.0019.")
            print("  3. sweep             -- find a trigger byte for THIS firmware; 'crash' auto-uses one later "
                  "if it's in KNOWN_CRASH_TRIGGERS, or pass --command 0x.. yourself.")
            print("  Then scan again after 2 or 3 to read whatever was left behind.")
            return 0
        if args.action == "restore-channels":
            directory, created = recreate_channel_files(args.drive, force=args.force)
            if not created:
                print(f"{directory}: all four channel files already present; nothing to do.")
                return 0
            print(f"Created in {directory}: {', '.join(p.name for p in created)}")
            print("This only recreates the host-side files eyefi-config itself creates on demand; "
                  "it does not reset or configure the firmware. If the card still doesn't respond "
                  "to 'info', try reseating it (ideally through an actual camera) before concluding "
                  "the chip itself is wedged.")
            return 0
        if args.action == "wifi":
            channel = Channel(args.drive)
            if args.wifi_action == "scan":
                nets = scan_networks(channel)
                if not nets:
                    print("No networks found.")
                for essid, strength, net_type in nets:
                    display = "open" if net_type == "none" else net_type
                    print(f"{display:8s} strength {strength:4d}  '{essid}'")
                return 0
            if args.wifi_action == "list":
                nets = list_networks(channel)
                if not nets:
                    print("No networks configured on this card.")
                for essid in nets:
                    print(f"'{essid}'")
                return 0
            if args.wifi_action == "add":
                if args.force:
                    print(f"--force: skipping the connection test, adding '{args.essid}' regardless.", flush=True)
                else:
                    if not args.password:
                        print(f"No --password given; testing '{args.essid}' as an OPEN network before adding.",
                              file=sys.stderr)
                    print(f"Testing '{args.essid}' before adding (use --force to skip this)...", flush=True)
                if add_network(channel, args.essid, args.password, args.force):
                    print(f"Added '{args.essid}'. Verify with 'wifi list'.")
                    return 0
                print(f"Could not connect to '{args.essid}' with the given credentials; not adding. "
                      "Check the password, or pass --force to add anyway.", file=sys.stderr)
                return 1
            if args.wifi_action == "remove":
                remove_network(channel, args.essid)
                print(f"Sent remove request for '{args.essid}'.")
                return 0
            if args.wifi_action == "test":
                if not args.password:
                    print(f"No --password given; testing '{args.essid}' as an OPEN network. "
                          "Pass --password \"...\" if it actually needs one.", file=sys.stderr)
                return 0 if test_network(channel, args.essid, args.password) else 1
        if args.action == "reboot-dump":
            if not 0 <= args.first_index <= args.last_index <= 0xffffffff:
                parser.error("Invalid dump index range")
            if args.bytes <= 0 or args.bytes % BLOCK or args.bytes > 0x100000000:
                parser.error("Size must be a positive multiple of 16384, within 32-bit offsets")
            channel = Channel(args.drive)
            output = args.output_dir or Path(__file__).parent / ("eyefi-reboot-dump-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            return reboot_and_dump(channel, output.resolve(), args.first_index, args.last_index, args.bytes,
                                   mac=args.mac, wait_timeout=args.wait_timeout, keys_file=args.save_keys)
        if args.action == "recover":
            if args.bytes <= 0 or args.bytes % BLOCK or args.bytes > 0x100000000:
                parser.error("Size must be a positive multiple of 16384, within 32-bit offsets")
            output = args.output_dir or Path(__file__).parent / ("eyefi-recover-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            return recover(args.drive, output.resolve(), mac=args.mac, keys_file=args.save_keys,
                          no_reboot=args.no_reboot, wait_timeout=args.wait_timeout, size=args.bytes, force=args.force)
        if args.action == "crash":
            if not 0 <= args.first_index <= args.last_index <= 0xffffffff:
                parser.error("Invalid dump index range")
            if args.bytes <= 0 or args.bytes % BLOCK or args.bytes > 0x100000000:
                parser.error("Size must be a positive multiple of 16384, within 32-bit offsets")
            channel = Channel(args.drive)
            output = args.output_dir or Path(__file__).parent / ("eyefi-before-crash-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            return crash_card(channel, output.resolve(), args.first_index, args.last_index, args.bytes,
                              opcode=args.command, keys_file=args.save_keys)
        if args.action == "sweep":
            if not 0 <= args.first_index <= args.last_index <= 0xffffffff:
                parser.error("Invalid dump index range")
            if args.bytes <= 0 or args.bytes % BLOCK or args.bytes > 0x100000000:
                parser.error("Size must be a positive multiple of 16384, within 32-bit offsets")
            if not 0 <= args.min_byte <= args.max_byte <= 0xff:
                parser.error("Need 0 <= --min-byte <= --max-byte <= 0xff")
            channel = Channel(args.drive)
            output = args.output_dir or Path(__file__).parent / ("eyefi-sweep-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            skip = set() if args.include_wifi_bytes else WIFI_MUTATING_BYTES
            return sweep_for_dump(channel, output.resolve(), args.first_index, args.last_index, args.bytes,
                                  mac=args.mac, min_byte=args.min_byte, max_byte=args.max_byte,
                                  max_drops=args.max_drops, settle_timeout=args.settle_timeout,
                                  skip_bytes=skip, include_o_subcodes=args.include_o_subcodes,
                                  include_bounds_matrix=args.include_bounds_matrix, keys_file=args.save_keys,
                                  scan_every=args.scan_every)
        if args.bytes <= 0 or args.bytes % BLOCK or args.offset < 0 or args.offset % BLOCK:
            parser.error("Size and offset must be multiples of 16384; size must be positive")
        if args.offset + args.bytes > 0x100000000:
            parser.error("Requested range exceeds the 32-bit offset limit")
        channel = Channel(args.drive)
        output = args.output.resolve()
        if on_card_volume(output, channel.root):
            parser.error("Output must be on a different volume from the card")
        if output.exists():
            parser.error("Output already exists; choose a new filename")
        data = bytearray()
        # xb also protects against a file appearing between the check and open.
        with output.open("xb") as stream:
            for offset in range(args.offset, args.offset + args.bytes, BLOCK):
                block = channel.request("k" if args.action == "read" else "m",
                                        getattr(args, "dump_index", 0), offset)
                stream.write(block)
                data.extend(block)
                if len(data) % (16 * BLOCK) == 0:
                    print(f"Read {len(data)}/{args.bytes} bytes", flush=True)
        print(f"Saved {len(data)} bytes from offset {args.offset}: {output}")
        print("The protocol has no verified EOF/error status; an acknowledged request alone")
        print("does not prove that the dump exists or that the complete dump was read.")
        if args.action == "read":
            if args.offset:
                print("Board offsets below are relative to the saved range.")
            return show_keys(data, args.mac, args.save_keys)
        return 0
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
