# AnyMic

Turn any phone into a microphone on Linux. No phone app, no kernel module, no root.

The phone's **browser** captures the mic and streams raw PCM over a WebSocket.
A small Python server feeds that into a **PipeWire virtual source**, which every
app — Discord, OBS, Zoom, browsers — sees as an ordinary microphone.

```
phone browser ──wss──> anymic.py ──> pw-cat ──> PipeWire virtual source ──> your apps
 getUserMedia          jitter buffer         "AnyMic (Phone)"
```

## What it looks like

Start it on the PC — it creates the virtual mic and prints the URL to open:

![AnyMic starting up and a phone connecting](docs/server.png)

Open that URL on the phone, tap **Start microphone**, and it streams:

<img src="docs/phone.png" alt="AnyMic running in mobile Safari, showing the level meter, mic volume slider and live stats" width="330">

The stats are live: buffer depth, bytes sent, dropout count and link state, so
you can tell at a glance whether audio is actually flowing rather than guessing.

## Why not Wo Mic

Wo Mic needs a phone app talking a proprietary protocol to an unmaintained Linux
client, and routes audio through the `snd-aloop` kernel module. That stack has
some sharp edges this avoids:

| | Wo Mic | AnyMic |
|---|---|---|
| Phone side | App install, protocol must match client version | Any modern browser |
| Audio routing | `snd-aloop` kernel module, needs root, gone after reboot | PipeWire virtual source, user-level |
| Which loopback end? | Client writes device 0, PipeWire reads device 0 — wrong end, silent | N/A |
| Rate mismatch | No resampling; a mismatch chops audio into robot voice | Browser resamples to 48kHz |
| Network jitter | Underruns punch holes in the waveform | 120ms jitter buffer, silence-fills |
| Second device connecting | — | Refused; mic is locked to the first device |
| iPhone | Not supported | Works |

## Requirements

**On the PC:** `pactl`, `openssl`, Python 3.8+, and either PipeWire (`pw-cat`,
`pw-link`) or PulseAudio (`pacat`). All standard on a modern desktop — no pip
packages.

**On the phone:** any browser with AudioWorklet support, which in practice
means **Chrome 66+, Firefox 76+, or Safari 14.1 / iOS 14.5+** — roughly 2021
onward. Older browsers get a clear "this browser cannot capture audio" message
rather than a mysterious failure. Nothing to install.

It isn't WiFi-specific either: anything that can route IP to your PC works,
including USB tethering, ethernet, or the phone's own hotspot.

### Audio backends

| | How the mic is created | Appears as |
|---|---|---|
| **PipeWire** (default) | null sink with `media.class=Audio/Source/Virtual` | a normal microphone |
| **PulseAudio** (fallback) | plain null sink, recorded from its monitor | *Monitor of AnyMic (Phone)* |

PipeWire is preferred and chosen automatically; PulseAudio is used when
`pw-cat`/`pw-link` are absent. Force one with `--backend pipewire|pulse`.

PulseAudio has no virtual-source type, so there the mic shows up as a monitor.
Some apps hide monitors behind a "show monitor sources" toggle.

## Use

```bash
python3 anymic.py
```

It prints a URL like `https://192.168.1.190:8443/`. Open that on the phone, tap
through the certificate warning, tap **Start microphone**.

Then pick **AnyMic (Phone)** as the input in whatever app you're using.

Ctrl-C removes the virtual source cleanly.

### Mic volume

The **Mic volume** slider on the phone page scales the input from 0% to 300%,
and takes effect immediately — no need to stop and restart. The setting is
remembered on that phone.

Gain is applied on the phone, as a `GainNode` ahead of the 16-bit conversion,
so it scales the samples while they still have full float headroom. Boosting on
the PC side instead would amplify audio that had already been quantised,
raising the noise floor along with the signal.

Watch the level meter while you set it. If the meter turns amber and **CLIP**
appears, you are driving it past full scale and the peaks are being flattened —
back off until it stops. Clipping cannot be undone further down the chain.

Leave *Auto gain* on if you just want something reasonable without fiddling;
turn it off when you want the slider to be the only thing setting the level.

### One device at a time

The first device to start streaming owns the microphone. Anyone else on the
network who opens the URL gets the page but is refused the audio stream, with
*"Another device is already using the microphone"* rather than a silent failure.
Without this, a second phone tapping Start would interleave its audio into the
same buffer and garble both.

The holder is identified by a secret token, not by IP address, so nothing on
the network can impersonate it.

Losing WiFi does not cost you the microphone. On an unexpected disconnect it is
**reserved for the original device for 30 seconds** — long enough for its
automatic reconnect to win, and it is the only device that can reclaim it in
that window. Pressing **Stop** is treated differently: that frees the mic
immediately, so handing over to another phone is instant rather than a 30
second wait.

### Options

```
--port 8443                     listen port
--description "..."             name shown in app mic lists
--name AnyMic                   audio node name
--backend auto|pipewire|pulse   audio backend (default: auto)
--certdir ./certs               where the self-signed cert lives
```

## Tests

```bash
python3 tests/test_lock.py    # who owns the mic, and when
python3 tests/test_wire.py    # real server, real WSS, real audio
```

`test_wire.py` starts its own server on port 8446 under a separate device name,
so it will not disturb an AnyMic you already have running. It streams a 440Hz
tone through the whole stack and checks what comes out of the virtual
microphone is still 440Hz and unbroken. Add `--backend pulse` to exercise the
PulseAudio path.

## About that certificate warning

`getUserMedia` only works in a secure context, so plain HTTP won't do — the
browser will not offer the mic at all. The server generates a self-signed
certificate for your LAN IP on first run, which is why the phone warns once.
Tap *Advanced → Proceed*. The cert lasts 10 years and regenerates automatically
if your LAN address changes.

## Troubleshooting

**Phone says "Microphone permission denied"** — allow the mic for that site in
browser settings. Chrome remembers this per-origin, so it will stick.

**Audio stops when the screen turns off** — leave *Keep screen awake* on. Mobile
browsers suspend audio capture in the background; the page re-resumes when you
return to it, but a locked screen can still cut the stream.

**Buffer climbing, dropouts rising** — WiFi congestion. Move closer to the
router, or use the phone's hotspot. The buffer self-corrects by dropping old
audio once latency exceeds 400ms.

**Robotic or chopped audio** — should not happen; the jitter buffer fills gaps
with silence rather than stalling. If it does, check the *Dropouts* counter on
the phone: steadily climbing means packets aren't arriving in time.

**No "AnyMic (Phone)" in the app's list** — some apps cache the device list at
startup. Restart the app after starting AnyMic.

**Too quiet, or distorted** — use the Mic volume slider, and watch for the CLIP
indicator. See [Mic volume](#mic-volume).

## Run it automatically

```ini
# ~/.config/systemd/user/anymic.service
[Unit]
Description=AnyMic phone microphone
After=pipewire.service pipewire-pulse.service
Wants=pipewire.service

[Service]
ExecStart=/usr/bin/python3 %h/anymic/anymic.py
KillMode=mixed
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now anymic
journalctl --user -u anymic -f      # the URL to open is printed here
```

`KillMode=mixed` matters. By default systemd signals every process in the
cgroup, which kills the audio feed before AnyMic can shut it down in order and
leaves a spurious error in the log. `mixed` signals only the main process and
lets it tear its own children down.

## How the audio path works

Creating the virtual source is one `pactl` call:

```bash
pactl load-module module-null-sink media.class=Audio/Source/Virtual \
      sink_name=AnyMic channel_map=mono
```

Feeding it is the non-obvious part. WirePlumber's policy will not route a
playback stream onto a node whose `media.class` is `Audio/Source/Virtual` — it
treats it as a source and silently connects the stream to the default sink
instead, so your audio goes to the speakers and the virtual mic stays silent.
The fix is to start `pw-cat` with autoconnect off and link it by hand:

```bash
PIPEWIRE_PROPS='{ node.autoconnect=false node.name=anymic-feed }' \
  pw-cat --playback --format=s16 --rate=48000 --channels=1 --raw - &
pw-link anymic-feed:output_MONO AnyMic:input_MONO
```

The server does both, plus paces writes at exactly real time so the stream
neither starves nor accumulates latency.

The PulseAudio backend hits a similar snag from the other direction: `pacat -d`
is the documented way to choose a sink and works on a real PulseAudio daemon,
but under PipeWire's PulseAudio compatibility layer it is silently overridden
and the stream lands on the default sink — your speakers. So the backend passes
`-d` *and* then moves the stream into place with `pactl move-sink-input`, which
is a no-op wherever `-d` already did the job.

## Security

TLS is not optional here: `getUserMedia` only works in a secure context, and an
HTTPS page cannot open a plain `ws://` socket. That is fortunate, because the
audio crosses the network as raw uncompressed PCM — over `ws://` anyone sharing
your WiFi could dump the payload straight to a WAV file and listen. `wss://`
also covers the lock token, which travels in the query string.

The certificate is self-signed, so this is strong against passive eavesdropping
but weak against an active attacker on your LAN who could present their own
certificate and hope you tap through the warning again. There is no pinning.

Cross-origin WebSocket upgrades are refused. WebSockets are exempt from the
same-origin policy, so once a browser has accepted the certificate, any page it
later visits could otherwise open a socket and seize the lock — not to listen,
but to deny you your own microphone.

The mic lock is not authentication. It stops a second device hijacking a live
session; it does not stop someone claiming the mic when nobody holds it. On an
untrusted network, treat the URL as the only thing standing between a stranger
and the microphone.

## License

MIT — see [LICENSE](LICENSE).
