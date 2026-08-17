"""
Thin, resilient wrapper around paho-mqtt used as the hardware-software
interface bus between:
    grid meters -> edge node (Raspberry Pi) -> SOGNO-style microservices

Why MQTT: lightweight pub/sub, works well over constrained/edge networks,
and is the transport SOGNO's own device-integration layer is built around.
A Kafka-backed implementation could sit behind the same `Publisher`/
`Subscriber` interface without touching call sites.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class Publisher:
    """Publishes JSON-serializable payloads with retry/backoff.

    Used by the edge node to push MeasurementBatch payloads upstream, and by
    the grid-state microservices to publish TopologySnapshot / SE results.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        max_retries: int = 5,
        backoff_base_s: float = 0.5,
        connect_timeout_s: float = 3.0,
    ) -> None:
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        self._host, self._port = host, port
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._connect_timeout_s = connect_timeout_s
        self._connected = False
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, _client, _userdata, _flags, rc):
        self._connected = rc == 0
        if self._connected:
            logger.info("MQTT publisher connected to %s:%s", self._host, self._port)
        else:
            logger.warning("MQTT connect failed with rc=%s", rc)

    def _on_disconnect(self, _client, _userdata, rc):
        self._connected = False
        logger.warning("MQTT publisher disconnected (rc=%s)", rc)

    def connect(self) -> bool:
        """Attempt to connect with exponential backoff. Returns success flag."""
        for attempt in range(self._max_retries):
            try:
                self._client.connect(self._host, self._port, keepalive=30)
                self._client.loop_start()
                # give the network thread a moment to flip on_connect
                time.sleep(min(0.2 * (attempt + 1), self._connect_timeout_s))
                if self._connected:
                    return True
            except (OSError, ConnectionRefusedError) as exc:
                logger.warning("MQTT connect attempt %d failed: %s", attempt + 1, exc)
            sleep_s = self._backoff_base_s * (2 ** attempt)
            time.sleep(sleep_s)
        logger.error("Exhausted %d MQTT connect retries", self._max_retries)
        return False

    def publish(self, topic: str, payload: dict, qos: int = 1) -> bool:
        if not self._connected and not self.connect():
            return False
        result = self._client.publish(topic, json.dumps(payload, default=str), qos=qos)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


class Subscriber:
    """Subscribes to a topic and dispatches decoded JSON payloads to a handler."""

    def __init__(self, host: str, port: int, client_id: str, topic: str,
                 on_message: Callable[[dict], None], qos: int = 1) -> None:
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        self._topic, self._qos, self._handler = topic, qos, on_message
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._host, self._port = host, port

    def _on_connect(self, client, _userdata, _flags, rc):
        if rc == 0:
            client.subscribe(self._topic, qos=self._qos)
            logger.info("Subscribed to %s", self._topic)

    def _on_message(self, _client, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            self._handler(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Failed to decode message on %s: %s", msg.topic, exc)

    def start(self, blocking: bool = False) -> None:
        self._client.connect(self._host, self._port, keepalive=30)
        if blocking:
            self._client.loop_forever()
        else:
            self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
