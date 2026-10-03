#!/usr/bin/env python3
"""
QUONIX TERMINAL

Windows CLI for the existing Webview Kiosk MQTT protocol.

The Android app already uses:
  command topic:  wk/command
  event topic:    wk/event/<eventType>
  response topic: wk/response/<responseType>

The terminal does NOT send or receive the QUONIX local unlock password.
Remote KIOSK_OFF is an authenticated MQTT-admin action; local unlocking
with the QUONIX password continues to work on the device itself.
"""

from __future__ import annotations

import json
import os
import queue
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Fehlendes Paket. Installiere: pip install -r requirements.txt")
    raise SystemExit(1)


@dataclass
class Config:
    broker: str = os.getenv("QUONIX_MQTT_BROKER", "127.0.0.1")
    port: int = int(os.getenv("QUONIX_MQTT_PORT", "1883"))
    username: str = os.getenv("QUONIX_MQTT_USERNAME", "")
    password: str = os.getenv("QUONIX_MQTT_PASSWORD", "")
    tls: bool = os.getenv("QUONIX_MQTT_TLS", "0") == "1"
    app_instance_id: str = os.getenv("QUONIX_APP_INSTANCE_ID", "")
    timeout: float = float(os.getenv("QUONIX_TIMEOUT", "15"))
    command_topic: str = os.getenv("QUONIX_COMMAND_TOPIC", "wk/command")
    event_topic: str = os.getenv("QUONIX_EVENT_TOPIC", "wk/event/#")


class Terminal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv5,
        )
        if cfg.username:
            self.client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            self.client.tls_set()

        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        self.connected = False
        self.events: queue.Queue[dict] = queue.Queue()
        self.responses: dict[str, queue.Queue] = {}

    @property
    def command_topic(self) -> str:
        return self.cfg.command_topic

    @property
    def event_topic(self) -> str:
        return self.cfg.event_topic

    def connect(self) -> None:
        print(f"Verbinde MQTT: {self.cfg.broker}:{self.cfg.port} ...")
        self.client.connect(self.cfg.broker, self.cfg.port, 60)
        self.client.loop_start()

        deadline = time.monotonic() + self.cfg.timeout
        while not self.connected and time.monotonic() < deadline:
            time.sleep(0.05)

        if not self.connected:
            raise RuntimeError("MQTT-Verbindung fehlgeschlagen.")

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if int(reason_code) != 0:
            print(f"✗ MQTT Fehler: {reason_code}")
            return

        self.connected = True
        client.subscribe(self.event_topic, qos=1)

        print("✓ MQTT verbunden")
        if self.cfg.app_instance_id:
            print(f"✓ Zielgerät: {self.cfg.app_instance_id}")
        else:
            print("! Keine App-Instance-ID gesetzt: Befehle gehen an alle Geräte.")

    def on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False

    def on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        if msg.topic.startswith("wk/event/"):
            if self._matches_device(data):
                self.events.put(data)
            return

        request_id = data.get("requestMessageId")
        if request_id:
            q = self.responses.get(request_id)
            if q:
                q.put(data)

    def _matches_device(self, data: dict) -> bool:
        target = self.cfg.app_instance_id
        return not target or data.get("appInstanceId") == target

    def _target_fields(self) -> dict:
        if self.cfg.app_instance_id:
            return {"targetInstances": [self.cfg.app_instance_id]}
        return {}

    def command(self, command_name: str, data: dict | None = None) -> str:
        message_id = str(uuid.uuid4())
        payload = {
            "command": command_name,
            "messageId": message_id,
            "interact": True,
            "wakeScreen": True,
            **self._target_fields(),
        }
        if data is not None:
            payload["data"] = data

        info = self.client.publish(
            self.command_topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish fehlgeschlagen: {info.rc}")

        return message_id

    def wait_for_event(self, event_type: str, timeout: float | None = None) -> dict | None:
        timeout = timeout or self.cfg.timeout
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                continue
            if event.get("eventType") == event_type:
                return event

        return None

    def status(self) -> dict | None:
        request_id = str(uuid.uuid4())
        response_topic = f"quonix/terminal/response/{request_id}"

        q: queue.Queue = queue.Queue(maxsize=1)
        self.responses[request_id] = q

        self.client.subscribe(response_topic, qos=1)
        payload = {
            "requestType": "get_status",
            "messageId": request_id,
            "responseTopic": response_topic,
            "correlationData": request_id,
            **self._target_fields(),
        }

        try:
            info = self.client.publish(
                self.command_topic,
                json.dumps(payload, separators=(",", ":")),
                qos=1,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return None

            try:
                return q.get(timeout=self.cfg.timeout)
            except queue.Empty:
                return None
        finally:
            self.responses.pop(request_id, None)
            self.client.unsubscribe(response_topic)

    def lock(self) -> bool:
        self.command("lock")
        event = self.wait_for_event("lock")
        return event is not None

    def unlock(self) -> bool:
        self.command("unlock")
        event = self.wait_for_event("unlock")
        return event is not None

    def open_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            print("✗ Nur gültige http/https URLs sind erlaubt.")
            return False
        self.command("go_to_url", {"url": url})
        return True

    def launch_package(self, package_name: str) -> bool:
        self.command(
            "launch_package",
            {"packageName": package_name},
        )
        return True


def print_status(data: dict | None) -> None:
    if not data:
        print("✗ Keine Statusantwort.")
        return

    print("✓ Status:")
    print(json.dumps(data.get("data", data), indent=2, ensure_ascii=False))


def url_input() -> str:
    while True:
        value = input("URL: ").strip()
        parsed = urlparse(value)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return value
        print("Bitte eine gültige http/https URL eingeben.")


def menu(t: Terminal) -> None:
    while True:
        print()
        print("=" * 44)
        print("              QUONIX TERMINAL")
        print("=" * 44)
        print()
        print("GERÄT")
        print("[1] Kiosk AN")
        print("[2] Kiosk AUS")
        print("[3] Lockscreen anzeigen")
        print("[4] Status")
        print("[5] WebView neu laden")
        print()
        print("WEB")
        print("[10] Website öffnen")
        print()
        print("ADMIN")
        print("[40] Android-Einstellungen")
        print("[42] Chrome öffnen")
        print("[43] Passwort-Manager öffnen")
        print()
        print("[0] Beenden")
        print("=" * 44)

        choice = input("Auswahl: ").strip()

        try:
            if choice == "0":
                return

            if choice == "1":
                print("Kiosk wird aktiviert ...")
                if t.lock():
                    print("✓ Kiosk ist aktiv.")
                else:
                    print("✗ Keine Lock-Bestätigung vom Gerät.")

            elif choice == "2":
                print("Kiosk wird deaktiviert ...")
                if t.unlock():
                    print("✓ Kiosk wurde beendet.")
                else:
                    print("✗ Keine Unlock-Bestätigung vom Gerät.")

            elif choice == "3":
                print("Lockscreen/Kiosk wird aktiviert ...")
                if t.lock():
                    print("✓ Lockscreen ist aktiv.")
                else:
                    print("✗ Keine Lock-Bestätigung vom Gerät.")

            elif choice == "4":
                print_status(t.status())

            elif choice == "5":
                t.command("refresh")
                print("✓ WebView-Refresh gesendet.")

            elif choice == "10":
                url = url_input()
                if t.open_url(url):
                    print("✓ Website-Befehl gesendet.")

            elif choice == "40":
                t.launch_package("com.android.settings")
                print("✓ Android-Einstellungen geöffnet.")

            elif choice == "42":
                t.launch_package("com.android.chrome")
                print("✓ Chrome-Start gesendet.")

            elif choice == "43":
                package_name = os.getenv("QUONIX_PASSWORD_MANAGER_PACKAGE", "").strip()
                if not package_name:
                    print("! Kein Passwort-Manager-Paket konfiguriert.")
                    print("  Setze QUONIX_PASSWORD_MANAGER_PACKAGE auf dem Windows-PC.")
                else:
                    t.launch_package(package_name)
                    print("✓ Passwort-Manager-Start gesendet.")
                    print("  Entsperrung erfolgt ausschließlich auf dem Android-Gerät.")

            else:
                print("Unbekannte Auswahl.")

        except Exception as exc:
            print(f"✗ Fehler: {exc}")


def main() -> int:
    print("=" * 44)
    print("              QUONIX TERMINAL")
    print("=" * 44)

    cfg = Config()

    broker = input(f"MQTT-Server [{cfg.broker}]: ").strip()
    if broker:
        cfg.broker = broker

    app_id = input(
        f"App-Instance-ID [{cfg.app_instance_id or 'leer = alle'}]: "
    ).strip()
    if app_id:
        cfg.app_instance_id = app_id

    terminal = Terminal(cfg)

    try:
        terminal.connect()
        menu(terminal)
    except KeyboardInterrupt:
        print("\nBeendet.")
    except Exception as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        terminal.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
