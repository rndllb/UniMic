# UniMic

Turn any phone into a microphone, on Linux, Windows or macOS. No phone app.

If you just want it working, read [INSTRUCTIONS.txt](INSTRUCTIONS.txt) instead.
It's a plain step-by-step walkthrough for each platform. The rest of this file
is how it works and why.

The phone's browser captures the mic and streams raw PCM over a WebSocket. A
small Python server feeds that into a virtual audio device, so any app (Discord,
OBS, Zoom, browsers) sees an ordinary microphone.

```
phone browser ──wss──> unimic.py ──> virtual audio device ──> your apps
 getUserMedia          jitter buffer
```

Where that virtual device comes from is the only thing that changes by platform:

| | Virtual mic | Needs installing |
|---|---|---|
| **Linux** | UniMic creates a PipeWire virtual source itself | nothing |
| **Windows** | plays into [VB-CABLE](https://vb-audio.com/Cable/), apps record its other end | VB-CABLE, once |
| **macOS** | plays into [BlackHole](https://existential.audio/blackhole/), apps record the same device | BlackHole, once |

Linux is the only one that can make a microphone from scratch. Windows and macOS
need a driver for any capture device, so there UniMic plays into a free virtual
audio cable you install once and forget about. Everything above that layer (the
page, the jitter buffer, the mic lock, TLS) is the same code on all three.

## Why not Wo Mic

Wo Mic needs a phone app talking a proprietary protocol to an unmaintained Linux
client, and routes audio through the `snd-aloop` kernel module. Things that stack
gets wrong and this one doesn't:

| | Wo Mic | UniMic |
|---|---|---|
| Phone side | App install, protocol must match client version | Any modern browser |
| Audio routing | `snd-aloop` kernel module, needs root, gone after reboot | PipeWire virtual source, user-level (Linux); a cable you already chose (Windows, macOS) |
| Which loopback end? | Client writes device 0, PipeWire reads device 0, so it stays silent | N/A |
| Rate mismatch | No resampling, and a mismatch chops audio into robot voice | Browser resamples to 48kHz |
| Network jitter | Underruns punch holes in the waveform | 120ms jitter buffer, silence-fills |
| Second device connecting | Not handled | Refused; mic is locked to the first device |
| iPhone | Not supported | Works |

You need a driver on Windows and macOS either way. The difference is that UniMic
uses a general-purpose one you can point at anything, instead of a proprietary
one that only its own client knows how to talk to.

## Requirements

**On the PC:** Python 3.8+, and no pip packages on any platform.

- **Linux:** `pactl`, plus either PipeWire (`pw-cat`, `pw-link`) or PulseAudio
  (`pacat`). All standard on a modern desktop.
- **Windows:** a virtual audio cable. [VB-CABLE](https://vb-audio.com/Cable/) is
  free and about 2 MB. Unzip it and run `VBCABLE_Setup_x64.exe` as
  administrator. VoiceMeeter and Virtual Audio Cable also work if you have one.
- **macOS:** [BlackHole](https://existential.audio/blackhole/), via
  `brew install blackhole-2ch`, the installer, or `unimic.command`. You'll need
  to approve the system extension in *System Settings > Privacy & Security*, then
  log out and back in before it shows up.

`openssl` gets used when it's present but isn't required. Without it UniMic
writes its own certificate.

**On the phone:** any browser with AudioWorklet support. In practice that means
Chrome 66+, Firefox 76+, or Safari 14.1 / iOS 14.5+, so roughly 2021 onward.
Older browsers get a "this browser cannot capture audio" message instead of a
mysterious failure. Nothing to install.

WiFi isn't required either. Anything that routes IP to your PC works, including
USB tethering, ethernet, or the phone's own hotspot.

### Audio backends

The right one is picked automatically. Force one with `--backend`.

| Backend | Platform | How the mic is created | Appears as |
|---|---|---|---|
| `pipewire` | Linux | null sink with `media.class=Audio/Source/Virtual` | a normal microphone |
| `pulse` | Linux | plain null sink, recorded from its monitor | *Monitor of UniMic (Phone)* |
| `winmm` | Windows | plays into an installed cable via `waveOut` | *CABLE Output (VB-Audio Virtual Cable)* |
| `coreaudio` | macOS | plays into BlackHole via an AudioQueue | *BlackHole 2ch* |
| `ffmpeg` | macOS | same, through `ffmpeg -f audiotoolbox` | *BlackHole 2ch* |

On Linux, PipeWire is preferred and PulseAudio is the fallback when `pw-cat` and
`pw-link` are missing. PulseAudio has no virtual-source type, so there the mic
turns up as a monitor, and some apps hide monitors behind a "show monitor
sources" toggle.

On macOS the in-process `coreaudio` path is tried first, with `ffmpeg` as the
automatic fallback. Same cable either way, only the route to it differs.

Windows and macOS reach the audio device through `ctypes`, so neither needs
anything from pip. On Windows that's `waveOut`, the one playback API you can use
without COM boilerplate. Windows maps it onto WASAPI internally, which costs a
few milliseconds of latency and nothing else.

## Use

On Windows, double-click **`unimic.bat`**. On macOS, double-click
**`unimic.command`**. Both check for Python and a virtual audio cable, offer to
install whichever is missing, then start the server. Arguments pass straight
through, so `unimic.bat --port 9000` and `./unimic.command --port 9000` work too.

Everywhere else, and on Windows or macOS once you're set up:

```bash
python3 unimic.py
```

It prints a URL like `https://192.168.1.190:8443/`. Open that on the phone, tap
through the certificate warning, then tap **Start microphone**.

The page shows a level meter, a mic volume slider, and live stats: buffer depth,
bytes sent, dropout count and link state. Enough to tell at a glance whether
audio is actually flowing.

Then pick the microphone it names (**UniMic (Phone)** on Linux, **CABLE Output**
on Windows, **BlackHole 2ch** on macOS) as the input in whatever app you're
using. UniMic prints the exact name to select at startup.

Ctrl-C shuts the audio path down cleanly. On Linux that also removes the virtual
source. On Windows and macOS the cable is yours and stays installed.

### Choosing a cable

If you have more than one virtual cable, or UniMic picks the wrong one:

```bash
python3 unimic.py --list-devices     # what is available
python3 unimic.py --device "CABLE"   # part of the name is enough
```

`--device default` plays out of your speakers instead. Nothing can record from
that, but it's a quick way to check the phone half works before you go
installing a driver.

### Is this machine ready?

```bash
python3 unimic.py --check
```

Reports what's present and what's missing, prints install instructions for
anything absent, and exits 0 only when UniMic can actually run. It changes
nothing: on Linux it looks for the tools without creating a sink, and elsewhere
it only resolves the device. Both launchers call it instead of duplicating the
detection.

### First run on macOS

The first double-click of `unimic.command` will be refused, with a message about
an unidentified developer or about the file being blocked to protect your Mac.
Nothing is wrong. Anything extracted from a zip you downloaded in a browser
carries a `com.apple.quarantine` flag, and Gatekeeper will not run a quarantined
script that isn't signed by a paid Apple developer account.

Clearing it is a one-time thing:

1. Double-click `unimic.command` and let it get blocked. The button in step 3
   does not appear until macOS has something to offer it for.
2. Open **System Settings > Privacy & Security**.
3. Scroll to the **Security** section. There's a line about `unimic.command`
   being blocked, with an **Open Anyway** button. Click it, and authenticate.
4. Double-click `unimic.command` again and confirm.

It opens normally from then on. Two other routes if that button doesn't show up:

- Control-click the file, choose **Open**, then **Open** again in the dialog.
- Strip the flag directly: `xattr -d com.apple.quarantine unimic.command`

Or sidestep it entirely by running `python3 unimic.py`. Quarantine only blocks
executing the file itself, and this hands the script to Python as an argument
instead, so Gatekeeper is never consulted. A quarantined `unimic.py` runs fine
this way.

The proper fix is signing and notarising the launcher with a paid Apple
Developer account, which is the same thing BlackHole does and the reason its
installer never prompts like this.

### First run on Windows

Windows Firewall will ask whether to let Python accept connections. Allow it on
**private** networks. If you dismiss that prompt the phone simply can't reach the
page, and nothing tells you why.

### Mic volume

The **Mic volume** slider on the phone page scales the input from 0% to 300% and
takes effect immediately, with no need to stop and restart. The setting is
remembered on that phone.

Gain is applied on the phone, as a `GainNode` ahead of the 16-bit conversion, so
it scales the samples while they still have full float headroom. Boosting on the
PC side instead would amplify audio that had already been quantised, raising the
noise floor along with the signal.

Watch the level meter while you set it. If the meter turns amber and **CLIP**
appears, you're driving it past full scale and the peaks are getting flattened.
Back off until it stops, because nothing further down the chain can undo
clipping.

Leave *Auto gain* on if you just want something reasonable without fiddling.
Turn it off when you want the slider to be the only thing setting the level.

### One device at a time

The first device to start streaming owns the microphone. Anyone else on the
network who opens the URL gets the page but is refused the audio stream, with
*"Another device is already using the microphone"* rather than a silent failure.
Without that, a second phone tapping Start would interleave its audio into the
same buffer and garble both.

The holder is identified by a secret token rather than by IP address, so nothing
on the network can impersonate it.

Losing WiFi doesn't cost you the microphone. On an unexpected disconnect it's
**reserved for the original device for 30 seconds**, which is long enough for its
automatic reconnect to win, and it's the only device that can reclaim it in that
window. Pressing **Stop** is treated differently and frees the mic immediately,
so handing over to another phone doesn't mean waiting out the reservation.

### Options

```
--port 8443            listen port
--description "..."    name shown in app mic lists (Linux only)
--name UniMic          audio node name (Linux only)
--backend auto|pipewire|pulse|winmm|coreaudio|ffmpeg
--device NAME          which virtual cable to play into (Windows, macOS);
                       'default' plays to your speakers instead
--list-devices         show what --device can pick from
--check                report whether this machine is ready, exit 0 if so
--certdir ./certs      where the self-signed cert lives
--version              print the version and exit
--update               install the newest release over this copy
--no-update-check      never contact GitHub to look for a newer release
```

`--name` and `--description` only mean anything on Linux, where UniMic creates
the device. Elsewhere the cable already has a name and that's what apps show.

## Releases and updating

Releases live on [GitHub](https://github.com/rndllb/UniMic/releases). Each one
is a tag and a zip of the source. Download it, unzip it anywhere, and run the
launcher for your platform.

Once installed, UniMic keeps itself current:

```bash
python3 unimic.py --version   # what you have
python3 unimic.py --update    # fetch and install the newest release
```

Startup looks for a newer release at most once a day and prints a line if it
finds one. That is all it does. Nothing is downloaded or replaced until you run
`--update` yourself, because software that rewrites itself from the network
without being asked turns one compromised account into everybody's problem. The
check runs on a background thread, caches its answer for 24 hours, and stays
quiet about every failure, so being offline costs nothing and delays nothing.
`--no-update-check` stops it contacting GitHub at all.

`--update` replaces only the files a release owns: `unimic.py`, `unimic.bat`,
`unimic.command`, `index.html`, `README.md`, `INSTRUCTIONS.txt` and `LICENSE`. Your
certificates and their private key, an edited launcher, a systemd unit, or
anything else you put in the directory are left alone. The version being
replaced is kept in `.backup-<version>/`, so a bad update is one `cp` away from
undone.

The check lives in `unimic.py` rather than in the launchers because Python is
the only runtime all three platforms are guaranteed to have. A `.bat` could
only ever update Windows users, a `.command` only macOS ones, and Linux has no
launcher at all.

### Cutting a release

GitHub builds the source zip for every tag, so there is nothing to package or
upload:

```bash
# bump __version__ in unimic.py first, then commit
gh release create v1.1.0 --title "UniMic 1.1.0" --notes "What changed"
```

The updater reads `tag_name` from the GitHub API and compares it against
`__version__`, so the tag has to be a plain `vX.Y.Z`. It prefers an uploaded
`.zip` asset if a release carries one, which leaves room to ship a trimmed
archive later without changing any code.

## Tests

```bash
python3 tests/test_lock.py    # who owns the mic, and when
python3 tests/test_cert.py    # the built-in certificate generator
python3 tests/test_wire.py    # real server, real WSS, real audio
```

All three run on all three platforms.

`test_wire.py` starts its own server on port 8446 under a separate device name,
so it won't disturb a UniMic you already have running. It streams a 440Hz tone
through the whole stack and checks that what comes out of the virtual microphone
is still 440Hz and unbroken.

It also times the audio path directly: four seconds of audio should take four
seconds to drain, which catches a device that accepts writes without really
playing them. That check needs no cable, so it runs even on a bare machine.

The round-trip needs something to record from, so on Windows and macOS it gets
skipped when no cable is installed. The server is pointed at the speakers and
everything else still runs. On Linux it always runs. Add `--backend pulse` to
exercise the PulseAudio path.

## About that certificate warning

`getUserMedia` only works in a secure context, so plain HTTP is out and the
browser won't offer the mic at all. The server generates a self-signed
certificate for your LAN IP on first run, which is why the phone warns you once.
Tap *Advanced > Proceed*. The cert lasts 10 years and regenerates automatically
if your LAN address changes.

It uses `openssl` when that's on the PATH, and otherwise writes the X.509 DER
itself. `hashlib` and three-argument `pow()` are enough for RSA, and the
alternative was making Windows users install a toolchain to get one file. The
address the certificate covers is recorded next to it in `certs/issued-for`, so
nothing has to parse a certificate back to notice your IP moved.

## Troubleshooting

**Phone says "Microphone permission denied"**
Allow the mic for that site in browser settings. Chrome remembers this
per-origin, so it will stick.

**Audio stops when the screen turns off**
Leave *Keep screen awake* on. Mobile browsers suspend audio capture in the
background. The page resumes when you return to it, but a locked screen can
still cut the stream.

**Buffer climbing, dropouts rising**
WiFi congestion. Move closer to the router, or use the phone's hotspot. The
buffer self-corrects by dropping old audio once latency passes 400ms.

**Robotic or chopped audio**
Shouldn't happen, since the jitter buffer fills gaps with silence rather than
stalling. If it does, check the *Dropouts* counter on the phone. Steadily
climbing means packets aren't arriving in time.

**No "UniMic (Phone)" in the app's list**
Some apps cache the device list at startup. Restart the app after starting
UniMic.

**Windows: the phone cannot load the page**
Almost always the firewall. Windows asks once whether to let Python accept
connections, and if that prompt was dismissed the block is silent afterwards.
Allow `python.exe` on private networks in Windows Defender Firewall.

**Windows: "no virtual audio cable installed"**
Install VB-CABLE and run `--list-devices` to confirm it shows up. It appears to
UniMic as *CABLE Input*, even though newer driver packs also register a
16-channel endpoint.

**macOS: BlackHole is installed but not found**
The system extension has to be approved in *System Settings > Privacy &
Security*, and macOS only picks it up after you log out and back in.
`--list-devices` shows what UniMic can see.

**macOS: it falls back to ffmpeg**
The in-process CoreAudio path failed for some reason and UniMic carried on
through ffmpeg. Audio still works, and the log line above it says what went
wrong. Force one or the other with `--backend coreaudio` or `--backend ffmpeg`.

**Too quiet, or distorted**
Use the Mic volume slider and watch for the CLIP indicator. See
[Mic volume](#mic-volume).

## Run it automatically

### Linux (systemd)

```ini
# ~/.config/systemd/user/unimic.service
[Unit]
Description=UniMic phone microphone
After=pipewire.service pipewire-pulse.service
Wants=pipewire.service

[Service]
ExecStart=/usr/bin/python3 %h/unimic/unimic.py
KillMode=mixed
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now unimic
journalctl --user -u unimic -f      # the URL to open is printed here
```

`KillMode=mixed` matters. By default systemd signals every process in the cgroup,
which kills the audio feed before UniMic can shut it down in order and leaves a
spurious error in the log. `mixed` signals only the main process and lets it tear
its own children down.

### Windows (Task Scheduler)

```powershell
schtasks /create /tn UniMic /sc onlogon /rl highest ^
  /tr "pythonw.exe C:\path\to\unimic\unimic.py"
```

`pythonw.exe` rather than `python.exe` keeps a console window from appearing at
every login. The URL then has nowhere to print, so either pass `--port` and
remember it, or redirect stdout to a file.

### macOS (launchd)

```xml
<!-- ~/Library/LaunchAgents/com.local.unimic.plist -->
<plist version="1.0"><dict>
  <key>Label</key>            <string>com.local.unimic</string>
  <key>ProgramArguments</key> <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/unimic/unimic.py</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>StandardOutPath</key>  <string>/tmp/unimic.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.local.unimic.plist
```

## How the audio path works

### Linux

Creating the virtual source is one `pactl` call:

```bash
pactl load-module module-null-sink media.class=Audio/Source/Virtual \
      sink_name=UniMic channel_map=mono
```

Feeding it is where it gets awkward. WirePlumber's policy won't route a playback
stream onto a node whose `media.class` is `Audio/Source/Virtual`. It treats the
node as a source and quietly connects the stream to the default sink instead, so
your audio goes to the speakers and the virtual mic stays silent. The fix is to
start `pw-cat` with autoconnect off and link it by hand:

```bash
PIPEWIRE_PROPS='{ node.autoconnect=false node.name=unimic-feed }' \
  pw-cat --playback --format=s16 --rate=48000 --channels=1 --raw - &
pw-link unimic-feed:output_MONO UniMic:input_MONO
```

The server does both, and paces writes at exactly real time so the stream neither
starves nor accumulates latency.

The PulseAudio backend runs into the same thing from the other side. `pacat -d`
is the documented way to choose a sink and works on a real PulseAudio daemon, but
under PipeWire's PulseAudio compatibility layer it's silently overridden and the
stream lands on the default sink, meaning your speakers. So the backend passes
`-d` and then also moves the stream into place with `pactl move-sink-input`,
which is a no-op wherever `-d` already did the job.

### Windows and macOS

There's nothing to create. The cable already exists, and its two ends are the
same device seen from opposite sides. UniMic opens the playback end and plays
into it, apps open the capture end and record from it.

Finding it is a name match. waveOut reports *CABLE Input* and CoreAudio reports
*BlackHole 2ch*, while the name UniMic tells you to select in your apps is the
matching capture end, which isn't always the same string. Windows truncates
device names to 31 characters at this layer, which is why the matching is on a
prefix.

Pacing is the part that differs. On Linux, `pw-cat` is a pipe and the server
writes to it on a real-time schedule. Here the device is the clock instead: a
fixed ring of buffers is handed to the driver, and the write call blocks until
one comes back. Playback then sets the rate rather than `time.monotonic()`, so
the feed can't slowly drift against the sound card the way two independent clocks
would. The test suite checks exactly this, and four seconds of audio must take
four seconds to drain, ±10%.

Neither path needs a pip package. `ctypes` reaches `waveOut` on Windows and
AudioQueue on macOS directly.

## Security

TLS isn't optional here. `getUserMedia` only works in a secure context, and an
HTTPS page can't open a plain `ws://` socket. That's just as well, because the
audio crosses the network as raw uncompressed PCM, and over `ws://` anyone
sharing your WiFi could dump the payload straight to a WAV file and listen.
`wss://` also covers the lock token, which travels in the query string.

The certificate is self-signed, which is strong against passive eavesdropping and
weak against an active attacker on your LAN who presents their own certificate
and hopes you tap through the warning again. There's no pinning.

Cross-origin WebSocket upgrades are refused. WebSockets are exempt from the
same-origin policy, so once a browser has accepted the certificate, any page it
later visits could otherwise open a socket and seize the lock. Not to listen, but
to deny you your own microphone.

The mic lock isn't authentication. It stops a second device hijacking a live
session, but it doesn't stop someone claiming the mic when nobody holds it. On an
untrusted network, treat the URL as the only thing standing between a stranger
and the microphone.

## License

MIT. See [LICENSE](LICENSE).
