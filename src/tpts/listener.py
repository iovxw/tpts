from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from .frigate_config import CameraTarget, load_camera_targets, load_mqtt_config
from .sync_camera_time import sync_camera_time


LOGGER = logging.getLogger(__name__)
DEFAULT_COOLDOWN_SECONDS = 300.0


@dataclass
class ListenerConfig:
    mqtt_host: str
    mqtt_port: int
    mqtt_topic_prefix: str
    mqtt_username: str | None
    mqtt_password: str | None
    frigate_config_path: str
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS


@dataclass
class CameraSyncService:
    config: ListenerConfig
    last_synced_at: dict[str, float] = field(default_factory=dict)
    role_statuses: dict[str, dict[str, str]] = field(default_factory=dict)

    def handle_camera_online(self, camera_name: str, role: str) -> None:
        now = time.monotonic()
        last_synced = self.last_synced_at.get(camera_name)
        if last_synced is not None and now - last_synced < self.config.cooldown_seconds:
            LOGGER.info(
                "Skipping %s online from role %s because it was synced %.1fs ago",
                camera_name,
                role,
                now - last_synced,
            )
            return

        camera_target = self.load_target(camera_name)
        LOGGER.info(
            "Synchronizing %s (%s) after %s became online",
            camera_name,
            camera_target.base_url,
            role,
        )
        sync_camera_time(
            username=camera_target.username,
            password=camera_target.password,
            base_url=camera_target.base_url,
        )
        self.last_synced_at[camera_name] = now
        LOGGER.info("Time synchronized for %s", camera_name)

    def handle_camera_status(self, camera_name: str, role: str, status: str) -> None:
        if status not in {"online", "offline", "disabled"}:
            LOGGER.warning("Ignoring unexpected status %r for %s/%s", status, camera_name, role)
            return

        camera_roles = self.role_statuses.setdefault(camera_name, {})
        previous_role_status = camera_roles.get(role)
        if previous_role_status == status:
            return

        camera_was_online = any(value == "online" for value in camera_roles.values())
        camera_roles[role] = status
        camera_is_online = any(value == "online" for value in camera_roles.values())

        LOGGER.info(
            "Camera %s role %s changed from %s to %s",
            camera_name,
            role,
            previous_role_status or "unknown",
            status,
        )

        if not camera_was_online and camera_is_online:
            self.handle_camera_online(camera_name, role)
        elif camera_was_online and not camera_is_online:
            LOGGER.info("Camera %s is no longer online", camera_name)

    def load_target(self, camera_name: str) -> CameraTarget:
        camera_targets = load_camera_targets(self.config.frigate_config_path)
        if camera_name not in camera_targets:
            raise ValueError(
                f"camera {camera_name!r} is not present in {self.config.frigate_config_path}"
            )
        return camera_targets[camera_name]


def parse_args() -> ListenerConfig:
    parser = argparse.ArgumentParser(
        description="Listen for Frigate camera online events and sync TP-Link camera time."
    )
    parser.add_argument(
        "--frigate-config-path",
        default=os.getenv("FRIGATE_CONFIG_PATH", "/frigate-config/config.yml"),
        help="Path to the Frigate config.yml file",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=float(os.getenv("ONLINE_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS))),
        help="Minimum seconds between syncs for the same camera",
    )
    args = parser.parse_args()
    mqtt_config = load_mqtt_config(args.frigate_config_path)
    if not mqtt_config.enabled:
        raise ValueError(f"MQTT is disabled in {args.frigate_config_path}")

    mqtt_host = normalize_mqtt_host(mqtt_config.host)

    return ListenerConfig(
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_config.port,
        mqtt_topic_prefix=mqtt_config.topic_prefix,
        mqtt_username=mqtt_config.username,
        mqtt_password=mqtt_config.password,
        frigate_config_path=args.frigate_config_path,
        cooldown_seconds=args.cooldown_seconds,
    )


def normalize_mqtt_host(host: str) -> str:
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror:
        LOGGER.warning(
            "MQTT host %s from Frigate config is not resolvable from the listener container; using 127.0.0.1",
            host,
        )
        return "127.0.0.1"

    return host


def build_client(config: ListenerConfig, service: CameraSyncService) -> mqtt.Client:
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if config.mqtt_username:
        client.username_pw_set(config.mqtt_username, config.mqtt_password)
    client.enable_logger(LOGGER)

    def on_connect(
        client: mqtt.Client,
        _userdata: object,
        _connect_flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            raise RuntimeError(f"MQTT connection failed: {reason_code}")

        topic = f"{config.mqtt_topic_prefix}/+/status/+"
        LOGGER.info("Connected to MQTT broker, subscribing to %s", topic)
        client.subscribe(topic)

    def on_message(
        _client: mqtt.Client,
        _userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        payload = message.payload.decode("utf-8").strip().lower()

        topic_parts = message.topic.split("/")
        prefix_parts = config.mqtt_topic_prefix.split("/")
        if len(topic_parts) != len(prefix_parts) + 3:
            LOGGER.warning("Ignoring unexpected topic %s", message.topic)
            return

        if topic_parts[: len(prefix_parts)] != prefix_parts:
            LOGGER.warning("Ignoring topic outside prefix %s", message.topic)
            return

        camera_name, status_segment, role = topic_parts[len(prefix_parts) :]
        if status_segment != "status":
            LOGGER.warning("Ignoring unexpected status topic %s", message.topic)
            return

        try:
            service.handle_camera_status(camera_name, role, payload)
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.error("Failed to sync time for %s: %s", camera_name, exc)

    client.on_connect = on_connect
    client.on_message = on_message
    return client


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = parse_args()
    service = CameraSyncService(config)
    client = build_client(config, service)

    LOGGER.info("Connecting to MQTT broker %s:%s", config.mqtt_host, config.mqtt_port)
    client.connect(config.mqtt_host, config.mqtt_port, keepalive=60)
    client.loop_forever()
    return 0
