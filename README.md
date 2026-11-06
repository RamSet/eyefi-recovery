# eyefi-key

A single standalone Python file for Eye-Fi SD cards: reads diagnostics, recovers upload keys (by direct query where the firmware exposes one, otherwise from a crash dump), and configures Wi-Fi — over the card's `EYEFI` request channel. No separate `eyefi-config` binary is required; its Wi-Fi protocol and this tool's key-recovery methods are all in this one file.

Forked from [attikov/eyefi-uploadkey](https://github.com/attikov/eyefi-uploadkey), which was written for Windows. Not affiliated with or endorsed by Eye-Fi. Use it only on cards you own or are authorized to examine.

Dumps and extracted keys are sensitive. Do not publish them.

## Status

| Part | Status |
| --- | --- |
| Linux port of the same commands | Verified against live hardware: `scan`/`read`/`log`/`firmware` round-trip correctly over `O_DIRECT` on a real card. |
| `crash` (`?` / `0x3f` assertion trigger) | Traced on firmware 4.5166 only. Unverified against live hardware on any OS; the opcode did nothing on a 5.0018 card. |
| Firmware 5.0 cards | `scan`/`read`/`log`/`firmware`/`reboot-dump` verified on one card reporting `5.0018`. The `crash` opcode does not apply to this firmware. |
| `info` (direct upload-key query) | Ported from eyefi-config's real source. Confirmed to read firmware/MAC/card-key correctly; the upload-key query returns a real, working key on firmware 5.2010, 5.2011 and 5.3008 (Mobi), and legitimately returns empty on 5.0018/5.0019 (those need a dump-based method). |
| `wifi` (scan/list/add/remove/test) | Ported from eyefi-config's real source (`network_action`/`scan_nets`/etc., not guessed), and the WPA key derivation matches a fresh `hashlib.pbkdf2_hmac` call exactly. **Not yet exercised against live hardware** — review the protocol yourself before relying on it to reconfigure a card you can't easily recover. |
| Firmware `5.2010`/`5.2011` cards | `sweep --scan-every 1` found command byte `0x01` as a trigger on two cards, reporting `5.2010` and `5.2011` respectively: it checkpointed a dump *without the card ever dropping*, caught only by the periodic in-memory peek (not the drop-watch). Confirms that peek path matters in practice, not just in theory. `0x01` is recorded per exact firmware version, not assumed to generalize past these two, even though both happened to match. `sweep` now tries a firmware's known byte first (see below) rather than walking the full range to reach it. |
| `restore-channels` | Creates `EyeFi/{reqc,reqm,rspc,rspm}` after a format wipes them (same mechanism as eyefi-config's `create_control_files`) and recovered a real card. Its first version's volume-ID gate refused on a genuine Eye-Fi card — it checked `/dev/disk/by-uuid/AA52-6922`, which every Eye-Fi card shares, so that symlink can point at the wrong card; fixed to ask the mounted device directly via `blkid`, with `--force` as an escape hatch if detection is still wrong. `recover` now calls this automatically when the channel directory is missing. |
| `recover` | Runs the direct-query / existing-dump / reboot-dump ladder from "Reading the firmware version" automatically, stopping at the first success; auto-heals a missing `EYEFI` dir via `restore-channels` first. Verified on real cards: direct query on 5.2010/5.2011/5.3008, reboot-dump on 5.0018/5.0019, and the missing-directory path on a camera-formatted card. |

### Tested firmware

Dump-based key recovery was tested on X2 cards only; the Mobi Pro was tested with the direct query. Anything about other firmware (including the 4.x builds the `?` trigger byte was originally traced on) is inherited from upstream analysis and has not been verified here.

| Firmware | Card | What worked |
| --- | --- | --- |
| 5.0018 | X2 | `scan`, `read`, `log`, `firmware`, `info` (upload key empty), `reboot-dump` recovers the key |
| 5.0019 | X2 | same as 5.0018; `reboot-dump` recovers the key |
| 5.2010 | X2 | `info` returns the key directly; `sweep` found `0x01` as a dump trigger |
| 5.2011 | X2 | `info` returns the key directly; `sweep` found `0x01` as a dump trigger here too |
| 5.3008 | Mobi Pro | `info` returns the upload key directly, so no dump method is needed. `scan` of slots 0-3 gets no data for the `k` dump command, and the card's own log shows unknown bytes raise a non-fatal `eyefi_host_control.c:2234: Assertion failed!` with no reset, while `b` and `0x42` reboot it without saving anything. So neither the assertion route nor `reboot-dump` yields a dump on this firmware. First request after mount can time out once; the automatic retry handles it. |

A card that stops answering right after any reset was, until this fix, the tool's own fault: a freshly reset card reads `RSPC` as `0xffffffff`, the next sequence wrapped to `0`, and the firmware ignores a zero sequence. The tool now reseeds to `0x1234` in that case, the same seed `eyefi-config` uses.

Treat a result as unconfirmed on hardware you have not tried it on yourself.

## Requirements

- Python 3, standard library only. No third-party packages.
- Linux for direct card access through a USB SD reader.
- Card mounted **writable**. The request channel is written to.
- Close any other Eye-Fi configuration program first, so they don't use the channel at the same time.

`extract` works on a saved dump file on any OS with Python 3. It does not touch the card.

**On an Eye-Fi Mobi**, the very first query after mounting can time out even though the card is fine — seen in practice on firmware `5.3008`, and plausibly a Wi-Fi radio that only wakes on the write, though that's not confirmed. The tool now retries automatically: if a `Channel`'s *first* request ever times out, it resubmits once from scratch (not just waits longer on the stale one) before giving up. Once any request has succeeded, later timeouts are treated as real again — this does not mask `sweep`'s own drop detection, which depends on a timeout meaning exactly that. If the very first retry also fails, that's worth taking seriously rather than retrying indefinitely yourself.

## Usage

Run from the directory containing `eyefi_diagnostic.py`. Every command below takes the card's mount point as an argument; **omit it and the tool auto-detects** the single mounted filesystem with an `EYEFI`/`EyeFi`/`eyefi` directory, in any of those three casings (scanning `/proc/mounts`). If none is mounted, or more than one is, it refuses rather than guessing — pass the path explicitly in that case. Auto-detection never mounts anything itself. An explicit path is checked the same way: passing the mount point looks for all three casings beneath it, and passing the directory itself works in any casing at all.

Scan the known dump slots and extract keys from whichever card is plugged in:

```bash
python3 eyefi_diagnostic.py
```

With no arguments at all this auto-detects the card and runs `scan`. Give a path explicitly (`python3 eyefi_diagnostic.py /media/user/EYEFI`) if you have more than one card mounted, or just to be specific. It saves dump slot 1 into a new timestamped directory beside the script, then searches it for board-data records. The firmware mirrors the same crash record into all three slots, so reading one is enough; pass `--last-index 3` to read all three if you ever hit a card where slot 1 is empty but the others are not.

Choose the output directory and filter by MAC:

```bash
python3 eyefi_diagnostic.py scan /media/user/EYEFI --output-dir ~/eyefi-backups/scan-001 --mac 00:11:22:33:44:55
```

Extract keys from a saved dump, without the card:

```bash
python3 eyefi_diagnostic.py extract ~/eyefi-backups/scan-001/dump3.response.bin
```

Read one dump range to a file:

```bash
python3 eyefi_diagnostic.py read /media/user/EYEFI --dump-index 3 --bytes 327680 --output ~/eyefi-backups/dump3.bin
```

Read a range of the card log (first 16 KiB by default):

```bash
python3 eyefi_diagnostic.py log /media/user/EYEFI --output ~/eyefi-backups/card-log.bin
```

Run `python3 eyefi_diagnostic.py --help` for every option. `--offset` and `--bytes` must be multiples of 16,384.

## Output

By default the card commands (`scan`, `extract`, `read`, `reboot-dump`, `crash`, `sweep`) are quiet: they print only the final result, one line per distinct card, with the upload key highlighted in green on a terminal:

```
Card MAC: 0018560000aa   Upload key: 00112233445566778899aabbccddeeff
```

`sweep` appends where it was found (e.g. `found at byte cmd=0x4b`, or `already present on card`). Colour is applied only to a real terminal and is suppressed if `NO_COLOR` is set or output is piped.

Pass `-v`/`--verbose` for the full detail — each slot read, every board-data record with its offset, the saved-scan path, and the per-byte sweep chatter. A scan still always writes `dump1.response.bin` (and `dump2`/`dump3` only with `--last-index 3`) plus `results.json` to its output directory regardless of verbosity, so the raw data is kept either way. In verbose mode each record shows its offset, MAC, and the key at board-data offset `+0x40` (the one that worked in desktop mode on the tested card).

Keys are read from the card's saved memory. They are not calculated from the MAC address. If two different keys appear for the same MAC, both are reported rather than one being chosen.

### Keeping a local record of recovered keys

```bash
python3 eyefi_diagnostic.py scan --save-keys
```

Add `--save-keys` to `extract`, `scan`, `read`, `reboot-dump`, `crash`, or `sweep` and any key found is appended to a local `{mac: [keys...]}` JSON file (`eyefi-keys.json` beside the script by default; give it a path, `--save-keys other.json`, to use a different one). It's off by default — nothing is saved unless you ask. A MAC that already has a different key on file keeps both rather than picking one, and the tool tells you about the conflict; it never silently overwrites. **This never writes anything to the card** — there is no verified command to assign an upload key onto a card, only to read one. Keep this file private; it's gitignored for exactly that reason.

## Reading the firmware version

```bash
python3 eyefi_diagnostic.py firmware /media/user/EYEFI
```

Prints the card's reported firmware version (command `o`). Nothing is written to the card.

For firmware/MAC/card-key/upload-key together in one safe, read-only pass (no crash, no reboot):

```bash
python3 eyefi_diagnostic.py info /media/user/EYEFI
```

The upload key here comes from a direct query (`o`, sub-command `0xfd` — the same one `eyefi-config -u` uses), not from a dump. If it reads back empty, `info` prints the firmware-aware explanation (does this firmware expose it via query at all, per the same threshold eyefi-server uses) plus a concrete next-steps order, since an empty query reading is **not** proof the key doesn't exist:

1. `scan` — read-only; the key may already be sitting in an existing dump slot (this has happened more than once in practice; always try this first).
2. `reboot-dump` — an ordinary reboot; confirmed to leave the key behind on firmware 5.0018/5.0019.
3. `sweep` — find a trigger byte for this specific firmware (or `crash --command 0x<byte>` once you already know one).
4. `scan` again after step 2 or 3, to read whatever was left behind.

Or run that order automatically:

```bash
python3 eyefi_diagnostic.py recover /media/user/EYEFI
```

`recover` tries, in order, stopping at the first that finds a key: `[1/3]` direct query (`upload_key`), `[2/3]` existing dump slots (read-only peek, nothing written to disk), `[3/3]` `reboot-dump` (writes a real backup, same as running it directly; skip with `--no-reboot`). It does *not* run `sweep` — that still needs a human watching for a card that can't afford to lose.

If the card's `EYEFI`/`EyeFi` directory is missing entirely (a reformatted card — see `restore-channels` below), `recover` detects that and runs `restore-channels` itself first, then continues the ladder above. That case needs an explicit mount path; there's nothing for auto-detect to find until the directory exists.

## Wi-Fi configuration

```bash
python3 eyefi_diagnostic.py wifi scan /media/user/EYEFI
python3 eyefi_diagnostic.py wifi list /media/user/EYEFI
python3 eyefi_diagnostic.py wifi add /media/user/EYEFI "My Network" --password "passphrase"
python3 eyefi_diagnostic.py wifi test /media/user/EYEFI "My Network" --password "passphrase"
python3 eyefi_diagnostic.py wifi remove /media/user/EYEFI "My Network"
```

Ported from [hansendc/eyefi-config](https://github.com/hansendc/eyefi-config)'s own `network_action`/`scan_nets`/`fetch_configured_nets`/`make_network_key` — read against that project's real source, not guessed, so this needs no separate binary. `add`/`test` accept a WPA passphrase (run through the standard WPA/WPA2 PBKDF2-HMAC-SHA1 passphrase-to-PSK derivation, not anything Eye-Fi-specific), or a 64/26/10-hex-digit raw key (WPA/WEP-104/WEP-40); omit `--password` for an open network. `test` polls the card's connection-test state until it reports success or gives up, printing each state change.

**`add` tests the connection before committing it** — matching `eyefi-config`'s own `-a` behavior exactly: a failed test (wrong or missing password against a secured network) means nothing is added, rather than silently landing as a broken open-network entry. `--force` skips the test and adds regardless, same as `eyefi-config`'s `-a --force`. `wifi scan` shows each network's actual security type beforehand (`WPA`/`WPA2`/`WEP`/`open`) — check that before deciding whether `--password` is needed.

The connection test's final stage checks reachability to Eye-Fi's own cloud service (`api.eye.fi`, read via `o` sub-command `4`) — which has been offline since Eye-Fi's 2016 shutdown and can never respond. There is no documented way to point the card at a different server instead (that field has no write path in `eyefi-config`'s own source, same as the upload key). Reaching that specific stage (`testing connection to Eye-Fi server`) only happens *after* the Wi-Fi key itself verifies, so `test`/`add` treat getting stuck there — not literal `success` — as the credentials being correct, rather than waiting out the full poll budget for a server that no longer exists.

## Experimental: crash

```bash
python3 eyefi_diagnostic.py crash /media/user/EYEFI
```

**This deliberately requests an assertion/reset. Live crash behavior is untested on any OS. Do not run it while the card is writing photos.**

A card with no saved crash dump has no board data to read, so there is no key to recover. `crash` exists to make the firmware save a fresh dump. With no `--command`, it reads the card's exact firmware version and auto-picks a confirmed trigger byte if one is known for that exact build (`KNOWN_CRASH_TRIGGERS` in the script — currently just `5.2010` → `0x01`, found via `sweep`); otherwise it falls back to `?` (`0x3f`), which static analysis traced to assertion handling, crash-state saving and reset **on firmware 4.5166 specifically** — it is not a general-purpose trigger, and a card on other firmware may ignore it entirely (as 5.0018 does) or respond differently. Pass `--command` yourself to override either way (see `sweep` below if your firmware isn't in the known-trigger list yet).

The command:

1. Reads the firmware version, auto-picks a known trigger byte for that exact build if one exists, and otherwise falls back to `?` with a warning if the firmware isn't 4.5166 — but never refuses to proceed; the trigger is always sent, on any firmware.
2. Backs up the dump slots into a new `eyefi-before-crash-*` directory. A backup I/O failure aborts before anything is sent.
3. Sends the command byte once, with no retry (a reset may prevent an acknowledgement).
4. Records progress in `crash-request.json` (`prepared_not_sent` → `send_started_outcome_unverified` → `request_signalled_crash_unverified`), fsynced so an abrupt reset mid-send is not mistaken for "never sent".

A signalled request is not a confirmed crash or dump. After the card recovers, read its log and scan again to verify. The `?` opcode and the `o` firmware-read command come from independent static analysis of 4.5166 and are unverified against live hardware.

## Recovering a key without crash: reboot-dump

```bash
python3 eyefi_diagnostic.py reboot-dump /media/user/EYEFI
```

On a card reporting firmware **5.0018**, the `?` assertion trigger above does nothing: that opcode is specific to 4.5166. But on that card, an ordinary **reboot** (command `b`, the same one `eyefi-config -b` sends) was found to checkpoint the board data into a dump slot before the card resets — no assertion or crash required.

`reboot-dump`:

1. Sends the reboot command once. A card may drop the connection immediately; that is expected, not an error.
2. Waits for the card to respond again (`--wait-timeout`, default 60s), retrying every couple of seconds. Raises if it never comes back — check the mount and remount, after which a plain `scan` may still find the dump.
3. Once the card answers, scans the dump slots exactly like `scan` and reports any board data found.

This is a normal, documented card operation (reboot), not a fuzzed or out-of-range request — lower risk than `crash`, but it still disconnects the card and should not be run while it is writing photos. It was confirmed to recover a working key on one card reporting firmware 5.0018; whether a reboot checkpoints board data on other firmware versions is untested.

## Finding the trigger yourself: sweep

```bash
python3 eyefi_diagnostic.py sweep /media/user/EYEFI
```

`crash`'s default byte (`?` / `0x3f`) only applies to firmware traced as 4.5166; it is not a general answer for "how do I get a dump out of this card." `reboot-dump`'s `b` byte is how the 5.0018 trigger was actually found — not by firmware analysis, but by trying command bytes against a real card and watching for one that made it drop and come back with a dump. `sweep` is that process, formalized:

1. Tries every command byte `0x00`–`0xff` (narrow with `--min-byte`/`--max-byte`; equal values retest one specific byte with a fresh, possibly longer `--settle-timeout`), except `a`/`d`/`t` (see below), as a bare, argument-less host command. **If the card's exact firmware is already in `KNOWN_CRASH_TRIGGERS`** (the same table `crash` auto-picks from), that byte is moved to the front of this list and tried first, rather than walking the full range to reach it — a repeat sweep on a firmware version already worked out before is typically a one-byte probe, not a 253-byte walk. It's still tried through the ordinary loop below, so a stale or wrong table entry just falls through to the rest of the sweep instead of failing outright.
2. Watches for the card to stop acknowledging — a sign it reset.
3. After each drop, waits for the card to come back (`--settle-timeout`, default 90s) and scans the dump slots.
4. **Checks for a dump up front, and again every `--scan-every` bytes (default 1 — after every byte tried), even without a drop** — because a trigger byte can make the firmware save a dump *while the card keeps responding normally*, which the drop-watch in step 2 would sail right past. These checks are in-memory peeks that write nothing unless they find something, so this default doesn't spray a dump directory per byte. The up-front check also means a card that already has a dump is reported immediately with no bytes sent at all. Raise `--scan-every` (or set it to `0` to disable) if you'd rather trade that precision for fewer peek reads. Confirmed to matter in practice, not just in theory: on cards reporting firmware `5.2010`/`5.2011`, this caught byte `0x01` as a trigger that checkpointed a dump with the card never dropping at all — a plain drop-watch sweep would have walked right past it, which is why scanning after every byte is the default rather than an opt-in.
5. **Stops at the first check that finds board data** — from a drop or a checkpoint — and does not grind through the rest. At the default `--scan-every 1` it names the exact trigger byte; with a larger value it reports which batch the trigger is in.
6. Gives up after `--max-drops` resets (default 25) with nothing found.

A live `trying cmd=0x..` line shows the current byte throughout, so stopping with Ctrl-C always tells you exactly where it was.

Pressing Ctrl-C mid-sweep is safe: a byte that stalls the card can leave a dump behind even as you interrupt, so on Ctrl-C the sweep waits for the card to settle and scans the slots one last time before exiting (reporting and saving any board data it finds), rather than throwing the dump away with a bare traceback. If it reports nothing but the card was still recovering, just run `scan` again a moment later.

**`a`/`d`/`t` are skipped by default.** Reading the real `eyefi-config` source (see Wi-Fi below) confirmed these are its own wire-protocol bytes for add/remove/test-network, not crash triggers — sending them bare means an empty ESSID and no key, which just pollutes the card's Wi-Fi config for nothing. (`d` is the exact byte that dropped a card during an earlier sweep run, before this was known; `b`, reboot, stays in deliberately — it's the one byte that's actually useful to find, and how `reboot-dump`'s trigger was discovered.) Pass `--include-wifi-bytes` for a genuinely exhaustive sweep.

**Beyond that, this is not limited to known-safe commands.** It can issue whatever else the firmware exposes for an untried byte — in principle including something destructive, not merely a reset. Only run it against a card you can afford to lose, and watch it run — it is not a fire-and-forget tool. Once it finds a working byte for your card's firmware, you don't need to sweep again: use `crash --command 0x<byte>` (or `reboot-dump` directly, if the byte turns out to be the ordinary reboot) from then on.

**Two further phases exist but are off by default**, from an earlier, ad hoc version of this sweep that tried more than just command bytes:

- `--include-o-subcodes`: `o` sub-commands `0x20`–`0xff`, beyond the known-safe query range (firmware/MAC/card-key/upload-key etc. all sit at `0x1f` or below, or the known `0xfd`).
- `--include-bounds-matrix`: `k`/`m` with deliberately out-of-range indices and offsets, hoping to trip a bounds check.

Neither has ever produced a dump in the runs that found `b` and tripped on `d`/`a`/`t` — both hits came from the plain byte sweep. They're kept available for completeness, not because they're known to be useful; the bounds matrix in particular sends requests no normal client would construct.

To re-check a specific byte with more patience — for example if an earlier run's wait wasn't long enough to see whether a dump actually appeared — retest just that byte:

```bash
python3 eyefi_diagnostic.py sweep /media/user/EYEFI --min-byte 0x50 --max-byte 0x50 --settle-timeout 180
```

## Changes from the original

- Card access uses uncached I/O (`O_DIRECT`, with a `POSIX_FADV_DONTNEED` fallback where a filesystem rejects it). The firmware only responds to uncached transfers, so without this the poll loop reads a stale sequence number.
- The code runs on Linux and needs no Windows tools.
- The firmware-version check is removed. Every command runs on any firmware, so check the firmware version yourself before relying on the output.

## License

See [LICENSE](LICENSE).
