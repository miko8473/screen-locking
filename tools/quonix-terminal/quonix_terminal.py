#!/usr/bin/env python3
"""
QUONIX TERMINAL

Windows CLI for the QUONIX Android kiosk MQTT protocol.

Implemented:
  - Kiosk lock/unlock
  - Status / refresh
  - Open URL
  - Android Settings / Chrome / configured password manager
  - Screenshot transfer Android -> Windows
  - Images/videos export Android -> Windows

File transfer:
  - metadata is published as the normal QUONIX file_transfer event
  - binary chunks use <event-topic-prefix>/file_chunk/<transferId>/<index>
  - SHA-256 and size are verified before a file is accepted

The QUONIX local unlock password and Android device PIN are never sent
to the terminal.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
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
    file_timeout: float = float(os.getenv("QUONIX_FILE_TIMEOUT", "120"))
    command_topic: str = os.getenv("QUONIX_COMMAND_TOPIC", "wk/command")
    event_topic: str = os.getenv("QUONIX_EVENT_TOPIC", "wk/event/#")
    download_root: Path = Path(
        os.getenv(
            "QUONIX_DOWNLOAD_ROOT",
            str(Path.home() / "Downloads" / "QUONIX"),
        )
    )


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

        self.transfers: dict[str, dict] = {}
        self.completed_transfers: queue.Queue[dict] = queue.Queue()
        self.transfer_lock = __import__("threading").Lock()

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

    @staticmethod
    def _user_property(properties, key: str) -> str | None:
        if properties is None:
            return None
        for item in getattr(properties, "UserProperty", []) or []:
            try:
                if item[0] == key:
                    return item[1]
            except (IndexError, TypeError):
                continue
        return None

    def on_message(self, client, userdata, msg):
        if "/file_chunk/" in msg.topic:
            self._handle_file_chunk(msg)
            return

        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        if msg.topic.startswith("wk/event/"):
            if not self._matches_device(data):
                return

            event_type = data.get("eventType")
            if event_type == "file_transfer":
                self._handle_file_metadata(data)
            elif event_type == "file_transfer_error":
                err = data.get("data", {})
                print(
                    f"✗ Android-Dateitransfer: "
                    f"{err.get('operation', 'unknown')}: "
                    f"{err.get('error', 'unbekannter Fehler')}"
                )
            else:
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

    def _matches_chunk_device(self, msg) -> bool:
        target = self.cfg.app_instance_id
        return (
            not target
            or self._user_property(msg.properties, "appInstanceId") == target
        )

    def _handle_file_metadata(self, data: dict) -> None:
        info = data.get("data", {})
        transfer_id = info.get("transferId")
        if not transfer_id:
            return

        with self.transfer_lock:
            transfer = self.transfers.setdefault(
                transfer_id,
                {"chunks": {}, "metadata": None},
            )
            transfer["metadata"] = info
            pending = list(transfer["chunks"].items())

        if pending:
            for index, payload in pending:
                self._try_complete_transfer(transfer_id, index, payload)

    def _handle_file_chunk(self, msg) -> None:
        if not self._matches_chunk_device(msg):
            return

        parts = msg.topic.rstrip("/").split("/")
        if len(parts) < 3:
            return

        transfer_id = parts[-2]
        try:
            index = int(parts[-1])
        except ValueError:
            return

        payload = bytes(msg.payload)
        with self.transfer_lock:
            transfer = self.transfers.setdefault(
                transfer_id,
                {"chunks": {}, "metadata": None},
            )
            transfer["chunks"][index] = payload

        self._try_complete_transfer(transfer_id, index, payload)

    def _try_complete_transfer(
        self,
        transfer_id: str,
        index: int,
        payload: bytes,
    ) -> None:
        with self.transfer_lock:
            transfer = self.transfers.get(transfer_id)
            if not transfer or transfer.get("completed"):
                return

            metadata = transfer.get("metadata")
            if not metadata:
                return

            total_chunks = int(metadata.get("totalChunks", 0))
            chunks = transfer["chunks"]
            if total_chunks <= 0 or len(chunks) < total_chunks:
                return
            if any(i not in chunks for i in range(total_chunks)):
                return

            raw = b"".join(chunks[i] for i in range(total_chunks))
            expected_size = int(metadata.get("size", -1))
            expected_sha = str(metadata.get("sha256", "")).lower()

            if len(raw) != expected_size:
                print(
                    f"✗ Transfer {transfer_id}: Größe stimmt nicht "
                    f"({len(raw)} != {expected_size})."
                )
                transfer["completed"] = True
                return

            actual_sha = hashlib.sha256(raw).hexdigest().lower()
            if expected_sha and actual_sha != expected_sha:
                print(f"✗ Transfer {transfer_id}: SHA-256 stimmt nicht.")
                transfer["completed"] = True
                return

            transfer["completed"] = True

        path = self._save_transfer(metadata, raw)
        result = {**metadata, "path": str(path)}
        self.completed_transfers.put(result)

        print(
            f"✓ Datei empfangen: {metadata.get('fileName')} "
            f"({len(raw) / 1024 / 1024:.2f} MB)"
        )
        print(f"  → {path}")

    def _save_transfer(self, metadata: dict, raw: bytes) -> Path:
        category = str(metadata.get("category", "other")).lower()
        if category == "screenshot":
            folder = self.cfg.download_root / "screenshots"
        elif category == "image":
            folder = self.cfg.download_root / "bilder"
        elif category == "video":
            folder = self.cfg.download_root / "videos"
        else:
            folder = self.cfg.download_root / "sonstiges"

        folder.mkdir(parents=True, exist_ok=True)
        name = str(metadata.get("fileName") or "quonix-file.bin")
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        name = Path(name).name or "quonix-file.bin"

        path = folder / name
        if path.exists():
            path = folder / f"{path.stem}_{int(time.time())}{path.suffix}"

        path.write_bytes(raw)
        return path

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

    def wait_for_file(
        self,
        category: str | None = None,
        timeout: float | None = None,
    ) -> dict | None:
        deadline = time.monotonic() + (timeout or self.cfg.file_timeout)
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                item = self.completed_transfers.get(timeout=remaining)
            except queue.Empty:
                continue
            if category is None or item.get("category") == category:
                return item
        return None

    def collect_media_transfers(self) -> int:
        count = 0
        deadline = time.monotonic() + self.cfg.file_timeout
        last_file = time.monotonic()

        while time.monotonic() < deadline:
            remaining = min(8.0, max(0.05, deadline - time.monotonic()))
            try:
                item = self.completed_transfers.get(timeout=remaining)
            except queue.Empty:
                if count > 0 and time.monotonic() - last_file >= 8:
                    break
                continue

            if item.get("category") in ("image", "video"):
                count += 1
                last_file = time.monotonic()

        return count

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
        return self.wait_for_event("lock") is not None

    def unlock(self) -> bool:
        self.command("unlock")
        return self.wait_for_event("unlock") is not None

    def open_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            print("✗ Nur gültige http/https URLs sind erlaubt.")
            return False
        self.command("go_to_url", {"url": url})
        return True

    def screenshot(self) -> dict | None:
        self.command("screenshot")
        print("Screenshot wird erstellt und übertragen ...")
        return self.wait_for_file("screenshot", self.cfg.file_timeout)

    def export_media(self) -> int:
        print("Bilder/Videos werden gesucht und übertragen ...")
        self.command(
            "export_media",
            {"includeImages": True, "includeVideos": True},
        )
        count = self.collect_media_transfers()
        print(f"✓ {count} Mediendatei(en) übertragen.")
        return count

    def launch_package(self, package_name: str) -> bool:
        self.command("launch_package", {"packageName": package_name})
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
        print("=" * 52)
        print("                 QUONIX TERMINAL")
        print("=" * 52)
        print()
        print("GERÄT")
        print("[1]  Kiosk AN")
        print("[2]  Kiosk AUS")
        print("[3]  Lockscreen anzeigen")
        print("[4]  Status")
        print("[5]  WebView neu laden")
        print()
        print("WEB")
        print("[10] Website öffnen")
        print()
        print("DATEIEN")
        print("[15] Bilder & Videos exportieren")
        print("[16] Screenshot erstellen")
        print()
        print("ADMIN")
        print("[40] Android-Einstellungen")
        print("[42] Chrome öffnen")
        print("[43] Passwort-Manager öffnen")
        print()
        print("[0]  Beenden")
        print("=" * 52)

        choice = input("Auswahl: ").strip()

        try:
            if choice == "0":
                return

            if choice == "1":
                print("Kiosk wird aktiviert ...")
                print("✓ Kiosk ist aktiv." if t.lock() else "✗ Keine Lock-Bestätigung.")

            elif choice == "2":
                print("Kiosk wird deaktiviert ...")
                print("✓ Kiosk wurde beendet." if t.unlock() else "✗ Keine Unlock-Bestätigung.")

            elif choice == "3":
                print("Lockscreen/Kiosk wird aktiviert ...")
                print("✓ Lockscreen ist aktiv." if t.lock() else "✗ Keine Lock-Bestätigung.")

            elif choice == "4":
                print_status(t.status())

            elif choice == "5":
                t.command("refresh")
                print("✓ WebView-Refresh gesendet.")

            elif choice == "10":
                url = url_input()
                if t.open_url(url):
                    print("✓ Website-Befehl gesendet.")

            elif choice == "15":
                t.export_media()

            elif choice == "16":
                if not t.screenshot():
                    print("✗ Kein Screenshot empfangen.")

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
    print("=" * 52)
    print("                 QUONIX TERMINAL")
    print("=" * 52)

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
