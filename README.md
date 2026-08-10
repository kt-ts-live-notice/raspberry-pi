# Subway Announcement Raspberry Pi Audio Client

역사 안내방송 실시간 시각화 PoC의 **Raspberry Pi 음성 수집·전송 클라이언트**입니다.

ReSpeaker Lite 또는 USB 마이크에서 음성을 연속 수집하고, 하나의 안내방송을 세션으로 묶어 **2초 단위 PCM WAV 청크**로 생성한 뒤 서버의 `POST /api/v1/audio-chunks` API에 전송합니다.

PoC 1단계에서는 방송 시작과 종료를 운영자가 수동으로 지정합니다. 자동 VAD/음향 기반 방송 경계 감지는 후속 단계입니다.

```text
ReSpeaker Lite / USB microphone
            │
            ▼
      Raspberry Pi
      audio_agent.py
            │
            ├─ manual start → new UUID session
            │
            ├─ PCM 16 kHz / mono / 16-bit
            │
            ├─ 2 s WAV chunks
            │
            ├─ local queue until HTTP 202 ACK
            │
            └─ HTTP multipart/form-data
                        │
                        ▼
        POST /api/v1/audio-chunks
                        │
                        ▼
                STT / classification server
```

## Implemented contract

Each request contains exactly these six multipart parts:

| Field | Value |
|---|---|
| `audio` | one WAV file, max 128 KiB |
| `session_id` | a new UUID for every announcement |
| `chunk_index` | integer starting from `0` without gaps |
| `is_final` | `true` or `false` |
| `device_id` | device ID registered by the server |
| `recorded_at` | UTC RFC3339 timestamp ending in `Z` |

Audio format:

```text
PCM
16,000 Hz
mono
16-bit signed little-endian
normal chunk: exactly 2 seconds = 64,000 PCM bytes
final chunk: > 0 seconds and <= 2 seconds
```

Transmission behavior:

- `Authorization: Bearer <device-token>`
- a chunk is deleted locally **only after `202 Accepted`**
- retries use the exact same WAV bytes and metadata
- retry delays: `0.5 s → 1 s → 2 s`
- an unacknowledged chunk is never skipped
- `409` is logged with `expected_chunk_index`; the client does not regenerate or skip audio
- queued WAV and JSON metadata remain on the Raspberry Pi when the server/network is unavailable

## Repository structure

```text
subway-audio-rpi/
├── src/
│   └── audio_agent.py
├── scripts/
│   ├── install.sh
│   └── subway-audioctl
├── systemd/
│   └── subway-audio.service
├── queue/
│   └── .gitkeep
├── control/
│   └── .gitkeep
├── runtime/
│   ├── .gitkeep
│   └── raw/
│       └── .gitkeep
├── config.env.example
├── requirements.txt
├── .gitignore
└── README.md
```

## Hardware used in the PoC

- Raspberry Pi 4
- Raspberry Pi OS Lite 64-bit
- ReSpeaker Lite via USB
- microSD card
- Raspberry Pi power adapter or suitable power bank for field testing
- Wi-Fi / smartphone hotspot / LTE-connected network

A normal USB microphone can be used while developing. Only `AUDIO_DEVICE` needs to change.

---

# 1. Raspberry Pi base setup

This repository assumes Raspberry Pi OS Lite 64-bit is already installed and SSH works.

Update the Pi:

```bash
sudo apt update
sudo apt full-upgrade -y
```

The Linux username expected by this repository is:

```text
pi
```

The installation path is:

```text
/home/pi/subway-audio
```

---

# 2. Check the microphone

Connect ReSpeaker Lite or another USB microphone.

```bash
lsusb
arecord -l
arecord -L
```

Example ALSA device name:

```text
plughw:CARD=Lite,DEV=0
```

Do not assume the numeric card number is stable. A name-based ALSA device is preferred when available.

Test recording for 5 seconds:

```bash
arecord \
  -D plughw:CARD=Lite,DEV=0 \
  -f S16_LE \
  -r 16000 \
  -c 1 \
  -d 5 \
  test.wav
```

Check the file:

```bash
file test.wav
ls -lh test.wav
```

If needed, copy it to a Windows PC:

```powershell
scp pi@<Raspberry-Pi-address>:/home/pi/test.wav .
```

---

# 3. Clone or copy this repository

Recommended target:

```bash
cd /home/pi
git clone <repository-url> subway-audio
cd /home/pi/subway-audio
```

If the source was copied manually instead, make sure the final repository path is still:

```text
/home/pi/subway-audio
```

---

# 4. Run the installer

```bash
cd /home/pi/subway-audio
chmod +x scripts/install.sh
./scripts/install.sh
```

The installer:

1. installs `alsa-utils`, Python, `venv`, `pip`, and `curl`
2. creates `.venv`
3. installs Python dependencies
4. creates runtime directories
5. installs `subway-audioctl` to `/usr/local/bin`
6. installs the systemd unit
7. enables the service for boot

The installer **does not start the service before configuration is completed**.

---

# 5. Configure the device

Copy the example if `config.env` does not already exist:

```bash
cd /home/pi/subway-audio
cp config.env.example config.env
chmod 600 config.env
```

Edit it:

```bash
nano /home/pi/subway-audio/config.env
```

Example:

```ini
SERVER_URL=http://<IP-address>:8787/api/v1/audio-chunks
DEVICE_ID=yeongdeungpo-01
DEVICE_TOKEN=<device-token>
AUDIO_DEVICE=plughw:CARD=Lite,DEV=0
BASE_DIR=/home/pi/subway-audio
```

`<IP-address>` and `<device-token>` are placeholders. Replace them only on the Raspberry Pi's local `config.env`.

`config.env` is ignored by Git and must not be committed.

### Local PC server

During local development:

```ini
SERVER_URL=http://<IP-address>:8787/api/v1/audio-chunks
```

`<IP-address>` is the PC's current IPv4 address. The Raspberry Pi and PC must be able to reach each other over the local network.

### Cloud server

For deployment, only the server URL needs to change, for example:

```ini
SERVER_URL=https://<cloud-domain>/api/v1/audio-chunks
```

The Raspberry Pi can then use any Internet connection such as Wi-Fi or a smartphone hotspot; it does not need to be on the same LAN as the cloud server.

The backend must register `DEVICE_ID` and `<device-token>` in its `DEVICE_AUTH_TOKENS` configuration. Use the backend repository's `.env.example` for the exact `DEVICE_AUTH_TOKENS` syntax.

---

# 6. Start the systemd service

Reload systemd after changing the unit file:

```bash
sudo systemctl daemon-reload
```

Start the service:

```bash
sudo systemctl start subway-audio.service
```

Check it:

```bash
sudo systemctl status subway-audio.service --no-pager -l
```

Expected state:

```text
Active: active (running)
```

The installer already enables boot startup. To enable it manually:

```bash
sudo systemctl enable subway-audio.service
```

To enable and start at the same time:

```bash
sudo systemctl enable --now subway-audio.service
```

---

# 7. Start an announcement session

The agent starts in `idle` state. It does **not** automatically record immediately after boot.

Check status:

```bash
subway-audioctl status
```

Start a new announcement:

```bash
subway-audioctl start
```

The agent generates a new UUID and resets `chunk_index` to `0`.

Example status:

```json
{
  "state": "recording",
  "device_id": "yeongdeungpo-01",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "next_chunk_index": 2,
  "updated_at": "2026-08-10T14:30:00.000Z"
}
```

While the session is active, the capture pipeline continuously reads raw PCM from `arecord`. The latest complete 2-second block is briefly held until the next audio arrives so a manual stop exactly on a 2-second boundary can still mark the correct final chunk.

---

# 8. Stop the announcement session

```bash
subway-audioctl stop
```

If a partial tail exists, it is written as the final WAV:

```text
chunk 0: 2.0 s, is_final=false
chunk 1: 2.0 s, is_final=false
chunk 2: 1.3 s, is_final=true
```

If the stop happens exactly on a 2-second boundary, the held complete chunk is sent as:

```text
2.0 s, is_final=true
```

The next `start` command creates a completely new UUID and starts again at `chunk_index=0`.

---

# 9. Follow logs

```bash
subway-audioctl logs
```

Equivalent command:

```bash
sudo journalctl -u subway-audio.service -f
```

Example successful flow:

```text
session started: <UUID>
queued session=<UUID> index=0 final=False duration=2.000s
upload session=<UUID> index=0 attempt=1
202 accepted session=<UUID> index=0 duplicate=False finalized=False
...
queued session=<UUID> index=3 final=True duration=1.242s
202 accepted session=<UUID> index=3 duplicate=False finalized=True
```

`finalized=true` means the server accepted the final input chunk and sealed the session. It does not mean STT/classification has already finished.

---

# 10. Offline queue and retransmission

Each unsent chunk is stored as a pair:

```text
queue/
├── <session-id>__000000.wav
├── <session-id>__000000.json
├── <session-id>__000001.wav
└── <session-id>__000001.json
```

The JSON preserves the exact metadata used for retries:

```json
{
  "session_id": "<session-id>",
  "chunk_index": 0,
  "is_final": false,
  "device_id": "yeongdeungpo-01",
  "recorded_at": "2026-08-10T14:30:00.000Z",
  "wav_filename": "<session-id>__000000.wav",
  "queued_at": "2026-08-10T14:30:02.010Z"
}
```

Check pending files:

```bash
ls -lh /home/pi/subway-audio/queue
```

If the server is unavailable, the client keeps the files and retries the same request after:

```text
0.5 seconds
1.0 second
2.0 seconds
```

After the retry burst, the file remains queued and a later retry cycle starts. The client does not delete the WAV until it receives HTTP `202`.

If an earlier chunk is still unacknowledged, later queued chunks are not skipped.

---

# 11. Verify generated WAV files

To inspect queued audio, temporarily make the server unavailable so files stay in `queue/`, then record a test session.

Install SoX if desired:

```bash
sudo apt install -y sox
```

Inspect WAV files:

```bash
soxi /home/pi/subway-audio/queue/*.wav
```

Normal chunks should show:

```text
Channels       : 1
Sample Rate    : 16000
Precision      : 16-bit
Duration       : 00:00:02.00
```

The normal WAV file is typically about 64 KiB plus its WAV header and remains below the 128 KiB server limit.

---

# 12. Network changes / smartphone hotspot

Raspberry Pi OS does not need to be reinstalled when Wi-Fi changes.

List Wi-Fi networks:

```bash
nmcli device wifi list
```

Connect to a smartphone hotspot:

```bash
sudo nmcli device wifi connect "<hotspot-ssid>" password "<hotspot-password>"
```

The SSH connection may disconnect when the Pi changes networks.

For a local PC server, update `SERVER_URL` if the PC's IP changes:

```bash
nano /home/pi/subway-audio/config.env
sudo systemctl restart subway-audio.service
```

For a public cloud endpoint, normally only Internet access is required and the URL does not change when the Pi switches between Wi-Fi and hotspot.

---

# 13. Useful systemd commands

```bash
sudo systemctl start subway-audio.service
sudo systemctl stop subway-audio.service
sudo systemctl restart subway-audio.service
sudo systemctl status subway-audio.service --no-pager -l
sudo systemctl enable subway-audio.service
sudo systemctl disable subway-audio.service
sudo journalctl -u subway-audio.service -n 100 --no-pager
```

---

# 14. Troubleshooting

## `No route to host`

The Raspberry Pi cannot reach the configured server address.

Check:

```bash
hostname -I
ping -c 4 <IP-address>
```

For a local PC server, confirm the PC's IPv4 address and firewall settings.

## `Connection refused`

The host is reachable but no server is listening at the configured host/port, or the backend only listens on loopback.

## HTTP `401`

Check:

```text
DEVICE_ID
DEVICE_TOKEN
server DEVICE_AUTH_TOKENS configuration
```

The device ID and token must match the backend configuration.

## HTTP `409`

The server and Raspberry Pi disagree about the next chunk index.

The client intentionally keeps the chunk and logs `expected_chunk_index`. Do not delete or recreate chunks blindly.

## HTTP `422`

Check the WAV format. The expected format is:

```text
16 kHz / mono / 16-bit PCM
normal chunk exactly 2 seconds
final chunk >0 and <=2 seconds
```

## `arecord` cannot open the device

```bash
arecord -l
arecord -L
```

Update `AUDIO_DEVICE` in `config.env`, then:

```bash
sudo systemctl restart subway-audio.service
```

## View full service errors

```bash
sudo systemctl status subway-audio.service --no-pager -l
sudo journalctl -xeu subway-audio.service --no-pager
```

---

# 15. Current PoC scope

Implemented on Raspberry Pi:

- ReSpeaker Lite / USB microphone capture
- continuous PCM capture during one announcement session
- exact 2-second normal chunks
- <=2-second final chunk
- UUID session generation
- sequential chunk indexes
- UTC RFC3339 `recorded_at`
- HTTP multipart upload
- Bearer device authentication
- HTTP 202 ACK handling
- duplicate-safe identical retransmission behavior on the client side
- local WAV + metadata queue
- 0.5 / 1 / 2 second retry schedule
- 409 sequence conflict preservation
- manual announcement start/stop
- systemd boot service
- Wi-Fi/hotspot compatible networking

Not implemented here:

- automatic VAD / announcement boundary detection
- STT
- announcement classification
- passenger WebSocket delivery
- passenger web UI
- cloud reverse proxy / TLS termination
- durable server-side exactly-once/outbox infrastructure

Those belong to the backend/web or later PoC phases.
