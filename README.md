# whisplay-airplay

AirPlay speaker app for Raspberry Pi + Whisplay HAT.

The app uses `shairport-sync` as the AirPlay receiver and renders a small 240x280 Whisplay UI through `whisplay-daemon`:

- Top bar: AirPlay title, Wi-Fi signal, PiSugar battery
- Center: current AirPlay connection source or waiting state
- Bottom: live volume bar when shairport-sync reports volume metadata
- Audio: defaults to the latest Whisplay unified sound card path, `ALSA_OUTPUT_DEVICE=playback`

## Install

```bash
sudo apt update
sudo apt install -y shairport-sync python3-venv python3-pip
./install.sh
```

`shairport-sync` is a system package, so installing it requires sudo access on the Pi.

If the system `shairport-sync` service is already enabled, stop it before launching this app so only one AirPlay receiver owns the service name:

```bash
sudo systemctl disable --now shairport-sync
```

## Run

```bash
./run.sh
```

When `whisplay-daemon` is available at `/tmp/whisplay-daemon.sock`, `install.sh` registers the app as `whisplay-airplay`; running the app renders into the daemon framebuffer. Without the daemon, it still runs the AirPlay receiver but skips LCD output.

## Configuration

Copy `.env.example` to `.env` and adjust as needed.

```dotenv
WHISPLAY_AIRPLAY_NAME=Whisplay AirPlay
ALSA_OUTPUT_DEVICE=playback
ALSA_MIXER_DEVICE=whisplaysound
ALSA_MIXER_CONTROL=speaker
SHAIRPORT_LOG_LEVEL=0
SHAIRPORT_INTERPOLATION=basic
SHAIRPORT_OUTPUT_RATE=44100
SHAIRPORT_OUTPUT_FORMAT=S16
SHAIRPORT_IGNORE_VOLUME_CONTROL=false
```

For the current Whisplay driver, `playback` is preferred over `hw:whisplaysound,0` because it uses the shared ALSA `dmix` route from the driver package. Legacy cards fall back to `plughw:CARD=...` automatically.

## Notes

The connected device label comes from shairport-sync metadata or logs. Some iOS/macOS versions only expose an IP or session label, so the UI may show that source instead of the friendly device name.
