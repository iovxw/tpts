from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml


@dataclass(frozen=True)
class CameraTarget:
    name: str
    base_url: str
    username: str
    password: str


@dataclass(frozen=True)
class MqttConfig:
    enabled: bool
    host: str
    port: int
    topic_prefix: str
    username: str | None
    password: str | None


def load_frigate_config(config_path: str | Path) -> dict[str, Any]:
    config_file = Path(config_path)
    with config_file.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle)

    if not isinstance(raw_config, dict):
        raise ValueError(f"Frigate config {config_file} is not a YAML mapping")

    return raw_config


def load_camera_targets(config_path: str | Path) -> dict[str, CameraTarget]:
    config_file = Path(config_path)
    raw_config = load_frigate_config(config_file)
    cameras = raw_config.get("cameras")
    if not isinstance(cameras, dict):
        raise ValueError(f"Frigate config {config_file} does not define cameras")

    targets: dict[str, CameraTarget] = {}
    for camera_name, camera_config in cameras.items():
        if not isinstance(camera_name, str) or not isinstance(camera_config, dict):
            continue
        targets[camera_name] = parse_camera_target(camera_name, camera_config)

    return targets


def load_mqtt_config(config_path: str | Path) -> MqttConfig:
    config_file = Path(config_path)
    raw_config = load_frigate_config(config_file)
    mqtt_config = raw_config.get("mqtt")
    if not isinstance(mqtt_config, dict):
        raise ValueError(f"Frigate config {config_file} does not define mqtt settings")

    enabled = mqtt_config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"Frigate config {config_file} has invalid mqtt.enabled")

    host = mqtt_config.get("host")
    if not isinstance(host, str) or not host:
        raise ValueError(f"Frigate config {config_file} has invalid mqtt.host")

    port = mqtt_config.get("port", 1883)
    if not isinstance(port, int):
        raise ValueError(f"Frigate config {config_file} has invalid mqtt.port")

    topic_prefix = mqtt_config.get("topic_prefix", "frigate")
    if not isinstance(topic_prefix, str) or not topic_prefix:
        raise ValueError(f"Frigate config {config_file} has invalid mqtt.topic_prefix")

    username = mqtt_config.get("user")
    if username is not None and not isinstance(username, str):
        raise ValueError(f"Frigate config {config_file} has invalid mqtt.user")

    password = mqtt_config.get("password")
    if password is not None and not isinstance(password, str):
        raise ValueError(f"Frigate config {config_file} has invalid mqtt.password")

    return MqttConfig(
        enabled=enabled,
        host=host,
        port=port,
        topic_prefix=topic_prefix,
        username=username,
        password=password,
    )


def parse_camera_target(camera_name: str, camera_config: dict[str, Any]) -> CameraTarget:
    username, password = resolve_credentials(camera_config)
    base_url = resolve_base_url(camera_config)
    return CameraTarget(
        name=camera_name,
        base_url=base_url,
        username=username,
        password=password,
    )


def resolve_credentials(camera_config: dict[str, Any]) -> tuple[str, str]:
    onvif_config = camera_config.get("onvif")
    if isinstance(onvif_config, dict):
        user = onvif_config.get("user")
        password = onvif_config.get("password")
        if isinstance(user, str) and user and isinstance(password, str):
            return user, password

    for stream_url in iter_rtsp_urls(camera_config):
        parsed = urlparse(stream_url)
        if parsed.username and parsed.password:
            return unquote(parsed.username), unquote(parsed.password)

    raise ValueError("camera credentials not found in onvif or rtsp inputs")


def resolve_base_url(camera_config: dict[str, Any]) -> str:
    webui_url = camera_config.get("webui_url")
    if isinstance(webui_url, str) and webui_url:
        return normalize_http_url(webui_url)

    onvif_config = camera_config.get("onvif")
    if isinstance(onvif_config, dict):
        host = onvif_config.get("host")
        port = onvif_config.get("port")
        if isinstance(host, str) and host:
            port_value = port if isinstance(port, int) else None
            return normalize_http_url(host, port_value)

    for stream_url in iter_rtsp_urls(camera_config):
        parsed = urlparse(stream_url)
        if parsed.hostname:
            return normalize_http_url(parsed.hostname)

    raise ValueError("camera host not found in webui_url, onvif, or rtsp inputs")


def iter_rtsp_urls(camera_config: dict[str, Any]) -> list[str]:
    ffmpeg_config = camera_config.get("ffmpeg")
    if not isinstance(ffmpeg_config, dict):
        return []

    inputs = ffmpeg_config.get("inputs")
    if not isinstance(inputs, list):
        return []

    stream_urls: list[str] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path.startswith("rtsp://"):
            stream_urls.append(path)
    return stream_urls


def normalize_http_url(value: str, default_port: int | None = None) -> str:
    parsed = urlparse(value if "://" in value else f"http://{value}")
    if not parsed.hostname:
        raise ValueError(f"invalid camera host: {value}")

    port = parsed.port if parsed.port is not None else default_port
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    netloc = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme}://{netloc}"
