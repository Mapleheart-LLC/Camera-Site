from __future__ import annotations

import json
import logging
import os
import queue
import re
import sqlite3
import ssl
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from db import get_db_connection

logger = logging.getLogger(__name__)

_PRESENCE_QUEUE_MAX_SIZE = 10000
_PRESENCE_QUEUE_TIMEOUT_SECONDS = 1.0
_MQTT_CONNECT_WAIT_SECONDS = 5.0
_MQTT_PUBLISH_ACK_WAIT_SECONDS = 2.0
_MQTT_PUBLISH_RETRY_COUNT = 3
_MQTT_OUTBOX_DEFAULT_MAX_ATTEMPTS = 12
_MQTT_OUTBOX_DEFAULT_BATCH_SIZE = 25
_MQTT_OUTBOX_DEFAULT_RETRY_BASE_SECONDS = 5
_MQTT_OUTBOX_DEFAULT_RETRY_MAX_SECONDS = 300


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_command_outbox_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS handler_command_outbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id       TEXT NOT NULL,
            payload_json    TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            max_attempts    INTEGER NOT NULL DEFAULT 12,
            last_error      TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            next_attempt_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_handler_command_outbox_device_status_next "
        "ON handler_command_outbox(device_id, status, next_attempt_at, id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_handler_command_outbox_status_next "
        "ON handler_command_outbox(status, next_attempt_at, id)"
    )


def enqueue_device_command_outbox(
    db: sqlite3.Connection,
    *,
    device_id: str,
    payload: dict[str, Any],
    max_attempts: int = _MQTT_OUTBOX_DEFAULT_MAX_ATTEMPTS,
) -> int:
    sanitized_device_id = str(device_id or "").strip()
    if not sanitized_device_id:
        raise ValueError("device_id is required")

    ensure_command_outbox_table(db)
    now = _iso_now()
    payload_json = json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False)
    row = db.execute(
        """
        INSERT INTO handler_command_outbox
            (device_id, payload_json, status, attempt_count, max_attempts, last_error, created_at, updated_at, next_attempt_at)
        VALUES (?, ?, 'pending', 0, ?, NULL, ?, ?, ?)
        """,
        (
            sanitized_device_id,
            payload_json,
            max(1, int(max_attempts)),
            now,
            now,
            now,
        ),
    )
    return int(row.lastrowid or 0)


class _MqttClientService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client = None
        self._connected = False
        self._started = False
        self._enabled = False
        self._connected_event = threading.Event()
        self._last_connect_rc: Optional[int] = None
        self._last_connect_error: str = ""

        self._command_topic_template = "tpeapp/device/{device_id}/commands"
        self._signaling_topic_template = "tpeapp/signaling/{session_id}"
        self._device_signaling_topic_template = "tpeapp/device/{device_id}/signaling"

        self._presence_enabled = True
        self._presence_heartbeat_topic = "tpeapp/device/+/heartbeat"
        self._presence_status_topic = "tpeapp/device/+/status"
        self._telemetry_topic = "tpeapp/device/+/telemetry"
        self._publish_ack_wait_seconds = _MQTT_PUBLISH_ACK_WAIT_SECONDS
        self._publish_retry_count = _MQTT_PUBLISH_RETRY_COUNT
        self._outbox_max_attempts = _MQTT_OUTBOX_DEFAULT_MAX_ATTEMPTS
        self._outbox_batch_size = _MQTT_OUTBOX_DEFAULT_BATCH_SIZE
        self._outbox_retry_base_seconds = _MQTT_OUTBOX_DEFAULT_RETRY_BASE_SECONDS
        self._outbox_retry_max_seconds = _MQTT_OUTBOX_DEFAULT_RETRY_MAX_SECONDS

        self._status_topic_re: Optional[re.Pattern[str]] = None
        self._heartbeat_topic_re: Optional[re.Pattern[str]] = None
        self._presence_queue: "queue.Queue[tuple[str, bool]]" = queue.Queue(maxsize=_PRESENCE_QUEUE_MAX_SIZE)
        self._presence_stop = threading.Event()
        self._presence_worker: Optional[threading.Thread] = None
        self._message_listeners: list[Callable[[str, str], None]] = []

    def _setting(self, db, env_key: str, db_key: str, default: str = "") -> str:
        env_val = os.environ.get(env_key)
        if env_val is not None and env_val != "":
            return env_val
        row = db.execute("SELECT value FROM settings WHERE key = ?", (db_key,)).fetchone()
        if row and row["value"] is not None and row["value"] != "":
            return str(row["value"])
        return default

    def _compile_presence_regexes(self) -> None:
        self._status_topic_re = self._compile_topic_regex(self._presence_status_topic)
        self._heartbeat_topic_re = self._compile_topic_regex(self._presence_heartbeat_topic)

    def _compile_topic_regex(self, topic_pattern: str) -> re.Pattern[str]:
        return re.compile("^" + re.escape(topic_pattern).replace(r"\+", r"([^/]+)") + "$")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def started(self) -> bool:
        return self._started

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_connect_rc(self) -> Optional[int]:
        return self._last_connect_rc

    @property
    def last_connect_error(self) -> str:
        return self._last_connect_error

    def wait_until_connected(self, timeout: float = _MQTT_CONNECT_WAIT_SECONDS) -> bool:
        if self._connected:
            return True
        return self._connected_event.wait(timeout)

    def start(self, db) -> None:
        with self._lock:
            if self._started:
                return

            host = self._setting(db, "MQTT_BROKER_HOST", "tpe_mqtt_broker_host", "")
            port_str = self._setting(db, "MQTT_BROKER_PORT", "tpe_mqtt_broker_port", "1883")
            username = self._setting(db, "MQTT_USERNAME", "tpe_mqtt_username", "")
            password = self._setting(db, "MQTT_PASSWORD", "tpe_mqtt_password", "")
            client_id = self._setting(db, "MQTT_CLIENT_ID", "tpe_mqtt_client_id", "mochii-backend")
            keepalive_str = self._setting(db, "MQTT_KEEPALIVE", "tpe_mqtt_keepalive", "60")

            tls_enabled = _as_bool(self._setting(db, "MQTT_TLS_ENABLED", "tpe_mqtt_tls_enabled", "false"))
            tls_ca = self._setting(db, "MQTT_TLS_CA_CERT", "tpe_mqtt_tls_ca_cert", "")
            tls_cert = self._setting(db, "MQTT_TLS_CLIENT_CERT", "tpe_mqtt_tls_client_cert", "")
            tls_key = self._setting(db, "MQTT_TLS_CLIENT_KEY", "tpe_mqtt_tls_client_key", "")
            tls_insecure = _as_bool(self._setting(db, "MQTT_TLS_INSECURE", "tpe_mqtt_tls_insecure", "false"))

            self._command_topic_template = self._setting(
                db,
                "MQTT_COMMAND_TOPIC_TEMPLATE",
                "tpe_mqtt_command_topic_template",
                self._command_topic_template,
            )
            self._signaling_topic_template = self._setting(
                db,
                "MQTT_SIGNALING_TOPIC_TEMPLATE",
                "tpe_mqtt_signaling_topic_template",
                self._signaling_topic_template,
            )
            self._device_signaling_topic_template = self._setting(
                db,
                "MQTT_DEVICE_SIGNALING_TOPIC_TEMPLATE",
                "tpe_mqtt_device_signaling_topic_template",
                self._device_signaling_topic_template,
            )
            self._presence_enabled = _as_bool(
                self._setting(db, "MQTT_PRESENCE_ENABLED", "tpe_mqtt_presence_enabled", "true")
            )
            self._presence_heartbeat_topic = self._setting(
                db,
                "MQTT_PRESENCE_HEARTBEAT_TOPIC",
                "tpe_mqtt_presence_heartbeat_topic",
                self._presence_heartbeat_topic,
            )
            self._presence_status_topic = self._setting(
                db,
                "MQTT_PRESENCE_STATUS_TOPIC",
                "tpe_mqtt_presence_status_topic",
                self._presence_status_topic,
            )
            self._telemetry_topic = self._setting(
                db,
                "MQTT_TELEMETRY_TOPIC",
                "tpe_mqtt_telemetry_topic",
                self._telemetry_topic,
            )
            publish_ack_wait = self._setting(
                db,
                "MQTT_PUBLISH_ACK_WAIT_SECONDS",
                "tpe_mqtt_publish_ack_wait_seconds",
                str(self._publish_ack_wait_seconds),
            )
            publish_retry_count = self._setting(
                db,
                "MQTT_PUBLISH_RETRY_COUNT",
                "tpe_mqtt_publish_retry_count",
                str(self._publish_retry_count),
            )
            outbox_max_attempts = self._setting(
                db,
                "MQTT_OUTBOX_MAX_ATTEMPTS",
                "tpe_mqtt_outbox_max_attempts",
                str(self._outbox_max_attempts),
            )
            outbox_batch_size = self._setting(
                db,
                "MQTT_OUTBOX_REPLAY_BATCH_SIZE",
                "tpe_mqtt_outbox_replay_batch_size",
                str(self._outbox_batch_size),
            )
            outbox_retry_base = self._setting(
                db,
                "MQTT_OUTBOX_RETRY_BASE_SECONDS",
                "tpe_mqtt_outbox_retry_base_seconds",
                str(self._outbox_retry_base_seconds),
            )
            outbox_retry_max = self._setting(
                db,
                "MQTT_OUTBOX_RETRY_MAX_SECONDS",
                "tpe_mqtt_outbox_retry_max_seconds",
                str(self._outbox_retry_max_seconds),
            )
            try:
                self._publish_ack_wait_seconds = max(0.2, float(publish_ack_wait))
            except Exception:
                self._publish_ack_wait_seconds = _MQTT_PUBLISH_ACK_WAIT_SECONDS
            try:
                self._publish_retry_count = max(1, int(publish_retry_count))
            except Exception:
                self._publish_retry_count = _MQTT_PUBLISH_RETRY_COUNT
            try:
                self._outbox_max_attempts = max(1, int(outbox_max_attempts))
            except Exception:
                self._outbox_max_attempts = _MQTT_OUTBOX_DEFAULT_MAX_ATTEMPTS
            try:
                self._outbox_batch_size = max(1, int(outbox_batch_size))
            except Exception:
                self._outbox_batch_size = _MQTT_OUTBOX_DEFAULT_BATCH_SIZE
            try:
                self._outbox_retry_base_seconds = max(1, int(outbox_retry_base))
            except Exception:
                self._outbox_retry_base_seconds = _MQTT_OUTBOX_DEFAULT_RETRY_BASE_SECONDS
            try:
                self._outbox_retry_max_seconds = max(self._outbox_retry_base_seconds, int(outbox_retry_max))
            except Exception:
                self._outbox_retry_max_seconds = _MQTT_OUTBOX_DEFAULT_RETRY_MAX_SECONDS
            self._compile_presence_regexes()
            ensure_command_outbox_table(db)

            if not host:
                logger.info("MQTT disabled: no broker host configured.")
                self._enabled = False
                self._started = True
                return

            try:
                import paho.mqtt.client as mqtt
            except Exception as exc:
                logger.warning("MQTT disabled: paho-mqtt unavailable: %s", exc)
                self._enabled = False
                self._started = True
                return

            try:
                port = int(port_str)
            except Exception:
                port = 1883
            try:
                keepalive = int(keepalive_str)
            except Exception:
                keepalive = 60

            client = mqtt.Client(
                client_id=client_id,
                protocol=mqtt.MQTTv311,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            )
            if username:
                client.username_pw_set(username=username, password=password or None)

            if tls_enabled:
                cert_reqs = ssl.CERT_NONE if tls_insecure else ssl.CERT_REQUIRED
                tls_protocol = getattr(ssl, "PROTOCOL_TLS_CLIENT", None)
                if tls_protocol is None:
                    tls_protocol = ssl.PROTOCOL_TLS
                    logger.warning("ssl.PROTOCOL_TLS_CLIENT unavailable; falling back to ssl.PROTOCOL_TLS")
                try:
                    client.tls_set(
                        ca_certs=tls_ca or None,
                        certfile=tls_cert or None,
                        keyfile=tls_key or None,
                        cert_reqs=cert_reqs,
                        tls_version=tls_protocol,
                    )
                    client.tls_insecure_set(tls_insecure)
                except Exception as exc:
                    logger.warning("MQTT TLS setup failed; disabling MQTT: %s", exc)
                    self._enabled = False
                    self._started = True
                    return

            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message

            try:
                client.connect_async(host=host, port=port, keepalive=keepalive)
                client.loop_start()
                self._presence_stop.clear()
                self._connected_event.clear()
                self._last_connect_rc = None
                self._last_connect_error = ""
                self._presence_worker = threading.Thread(
                    target=self._presence_worker_loop,
                    name="mqtt-presence-worker",
                    daemon=True,
                )
                self._presence_worker.start()
                self._client = client
                self._enabled = True
                self._started = True
                logger.info(
                    "MQTT client started for broker %s:%s (tls=%s, presence=%s)",
                    host,
                    port,
                    tls_enabled,
                    self._presence_enabled,
                )
            except Exception as exc:
                logger.warning("MQTT disabled: failed to connect to broker: %s", exc)
                self._enabled = False
                self._started = True

    def stop(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._connected = False
            self._enabled = False
            self._started = False
            self._connected_event.clear()
            self._last_connect_rc = None
            self._last_connect_error = ""
            self._presence_stop.set()
            worker = self._presence_worker
            self._presence_worker = None

        if worker is not None:
            try:
                self._presence_queue.put_nowait(("", False))
            except Exception:
                pass
            worker.join(timeout=2)

        if client is None:
            return
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self._connected = rc == 0
        self._last_connect_rc = int(rc)
        if rc != 0:
            self._last_connect_error = f"connect_failed_rc={rc}"
            logger.warning("MQTT connect failed with rc=%s", rc)
            return
        self._connected_event.set()
        self._last_connect_error = ""
        logger.info("MQTT connected.")
        if self._presence_enabled:
            try:
                client.subscribe(self._presence_heartbeat_topic, qos=1)
                client.subscribe(self._presence_status_topic, qos=1)
                if self._telemetry_topic:
                    client.subscribe(self._telemetry_topic, qos=1)
                logger.info(
                    "MQTT presence subscriptions active: heartbeat=%s status=%s telemetry=%s",
                    self._presence_heartbeat_topic,
                    self._presence_status_topic,
                    self._telemetry_topic,
                )
            except Exception as exc:
                logger.warning("MQTT presence subscribe failed: %s", exc)

    def _on_disconnect(self, client, userdata, rc, properties=None):
        self._connected = False
        self._connected_event.clear()
        if rc != 0:
            self._last_connect_error = f"disconnected_rc={rc}"
        self._last_connect_rc = int(rc)
        if rc != 0:
            logger.warning("MQTT disconnected unexpectedly (rc=%s)", rc)
        else:
            logger.info("MQTT disconnected.")

    def _presence_worker_loop(self) -> None:
        db = get_db_connection()
        try:
            while not self._presence_stop.is_set():
                try:
                    device_id, is_online = self._presence_queue.get(timeout=_PRESENCE_QUEUE_TIMEOUT_SECONDS)
                except queue.Empty:
                    continue
                if not device_id:
                    continue
                try:
                    now = _iso_now()
                    db.execute(
                        """
                        INSERT INTO handler_device_status
                            (device_id, is_locked, is_online, last_seen, updated_at)
                        VALUES (?, 0, ?, ?, ?)
                        ON CONFLICT(device_id) DO UPDATE SET
                            is_online = excluded.is_online,
                            last_seen = excluded.last_seen,
                            updated_at = excluded.updated_at
                        """,
                        (device_id, 1 if is_online else 0, now, now),
                    )
                    if is_online:
                        self._flush_command_outbox_for_device(db, device_id)
                    db.commit()
                except Exception as exc:
                    logger.debug("MQTT presence DB update failed for %s: %s", device_id, exc)
        finally:
            db.close()

    def _flush_command_outbox_for_device(self, db: sqlite3.Connection, device_id: str) -> None:
        ensure_command_outbox_table(db)
        now = _iso_now()
        rows = db.execute(
            """
            SELECT id, payload_json, attempt_count, max_attempts
            FROM handler_command_outbox
            WHERE device_id = ?
              AND status = 'pending'
              AND next_attempt_at <= ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (device_id, now, self._outbox_batch_size),
        ).fetchall()
        if not rows:
            return

        replayed = 0
        for row in rows:
            outbox_id = int(row["id"])
            attempt_count = int(row["attempt_count"] or 0)
            max_attempts = int(row["max_attempts"] or self._outbox_max_attempts)

            try:
                payload = json.loads(row["payload_json"] or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("payload_json is not an object")
            except Exception as exc:
                db.execute(
                    """
                    UPDATE handler_command_outbox
                    SET status = 'dead',
                        attempt_count = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (attempt_count + 1, f"payload_parse_error:{exc}", _iso_now(), outbox_id),
                )
                continue

            topic = self.topic_for_device_command(device_id)
            published = self.publish_json(topic, payload, qos=1)
            if published:
                db.execute("DELETE FROM handler_command_outbox WHERE id = ?", (outbox_id,))
                replayed += 1
                continue

            next_attempt = attempt_count + 1
            update_now = _iso_now()
            if next_attempt >= max_attempts:
                db.execute(
                    """
                    UPDATE handler_command_outbox
                    SET status = 'dead',
                        attempt_count = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (next_attempt, "mqtt_replay_failed", update_now, outbox_id),
                )
            else:
                backoff_seconds = min(
                    self._outbox_retry_max_seconds,
                    self._outbox_retry_base_seconds * (2 ** max(0, next_attempt - 1)),
                )
                next_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
                db.execute(
                    """
                    UPDATE handler_command_outbox
                    SET attempt_count = ?,
                        last_error = ?,
                        updated_at = ?,
                        next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (next_attempt, "mqtt_replay_failed", update_now, next_at, outbox_id),
                )

        if replayed:
            logger.info("MQTT outbox replay delivered %s command(s) to device=%s", replayed, device_id)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic or ""
        payload_raw = (msg.payload or b"").decode("utf-8", errors="ignore").strip()

        is_online: Optional[bool] = None
        device_id: Optional[str] = None

        if self._heartbeat_topic_re:
            m = self._heartbeat_topic_re.match(topic)
            if m:
                device_id = m.group(1)
                is_online = True

        if is_online is None and self._status_topic_re:
            m = self._status_topic_re.match(topic)
            if m:
                device_id = m.group(1)
                lowered = payload_raw.lower()
                if lowered in {"online", "1", "true", "connected"}:
                    is_online = True
                elif lowered in {"offline", "0", "false", "disconnected"}:
                    is_online = False
                else:
                    try:
                        parsed = json.loads(payload_raw)
                        state = str(parsed.get("status", "")).lower()
                        if state in {"online", "connected"}:
                            is_online = True
                        elif state in {"offline", "disconnected"}:
                            is_online = False
                    except Exception:
                        pass

        if device_id and is_online is not None:
            try:
                self._presence_queue.put_nowait((device_id, is_online))
            except queue.Full:
                logger.debug("MQTT presence queue full; dropping update for %s", device_id)

        listeners = list(self._message_listeners)
        for listener in listeners:
            try:
                listener(topic, payload_raw)
            except Exception:
                listener_name = getattr(listener, "__name__", repr(listener))
                logger.debug("MQTT listener callback failed: %s", listener_name, exc_info=True)

    def publish_json(self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False) -> bool:
        if not self._enabled:
            return False

        try:
            payload_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        except Exception as exc:
            logger.warning("MQTT payload serialization failed topic=%s: %s", topic, exc)
            return False

        qos_level = max(0, min(2, int(qos)))
        attempts = max(1, self._publish_retry_count)
        for attempt in range(1, attempts + 1):
            client = self._client
            if client is None:
                return False
            if not self._connected and not self.wait_until_connected(timeout=self._publish_ack_wait_seconds):
                logger.warning(
                    "MQTT publish skipped while disconnected topic=%s attempt=%s/%s",
                    topic,
                    attempt,
                    attempts,
                )
                continue

            try:
                info = client.publish(topic, payload_text, qos=qos_level, retain=retain)
                if info.rc != 0:
                    logger.warning(
                        "MQTT publish failed rc=%s topic=%s attempt=%s/%s",
                        info.rc,
                        topic,
                        attempt,
                        attempts,
                    )
                    continue

                if qos_level > 0:
                    try:
                        info.wait_for_publish(timeout=self._publish_ack_wait_seconds)
                    except TypeError:
                        info.wait_for_publish()
                    if not info.is_published():
                        logger.warning(
                            "MQTT publish ack timeout topic=%s attempt=%s/%s",
                            topic,
                            attempt,
                            attempts,
                        )
                        continue
                return True
            except Exception as exc:
                logger.warning(
                    "MQTT publish exception topic=%s attempt=%s/%s: %s",
                    topic,
                    attempt,
                    attempts,
                    exc,
                )

        return False

    def topic_for_device_command(self, device_id: str) -> str:
        return self._command_topic_template.format(device_id=device_id)

    def topic_for_session_signaling(self, session_id: str) -> str:
        return self._signaling_topic_template.format(session_id=session_id)

    def topic_for_device_signaling(self, device_id: str) -> str:
        return self._device_signaling_topic_template.format(device_id=device_id)

    def add_message_listener(self, callback: Callable[[str, str], None]) -> None:
        if callback in self._message_listeners:
            return
        self._message_listeners.append(callback)

    def remove_message_listener(self, callback: Callable[[str, str], None]) -> None:
        try:
            self._message_listeners.remove(callback)
        except ValueError:
            pass


mqtt_client = _MqttClientService()


def initialize_mqtt(db: sqlite3.Connection) -> None:
    mqtt_client.start(db)


def reload_mqtt(db: sqlite3.Connection) -> None:
    """Reload the MQTT client using the latest environment and DB-backed settings."""
    mqtt_client.stop()
    mqtt_client.start(db)


def shutdown_mqtt() -> None:
    mqtt_client.stop()
