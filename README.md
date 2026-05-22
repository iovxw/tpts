# tpts

`tpts` watches Frigate's MQTT camera status topics and synchronizes TP-Link camera time when a camera comes online.

The service reads its runtime settings from the Frigate config file automatically, including:

- MQTT host, port, topic prefix, username, and password
- Camera inventory
- Camera login credentials and device addresses

Camera credentials are resolved from each camera definition in this order:

1. `onvif.user` / `onvif.password`
2. RTSP input URLs in `ffmpeg.inputs[*].path`

The device base URL is resolved from:

1. `webui_url`
2. `onvif.host` and `onvif.port`
3. The hostname in an RTSP input URL

## Usage

### Run locally with uv

```bash
uv sync
uv run tpts
```

By default, `tpts` reads `/frigate-config/config.yml`. Override the path if needed:

```bash
uv run tpts --frigate-config-path /path/to/config.yml
```

You can also adjust the duplicate-sync cooldown:

```bash
uv run tpts --cooldown-seconds 300
```

### Run the one-shot sync helper

```bash
uv run tpts-sync-camera-time USERNAME PASSWORD http://CAMERA_IP
```

### Run in the container stack

Mount the Frigate config directory at `/frigate-config` and start the image.

See <https://github.com/iovxw/my-frigate>
