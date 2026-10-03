#!/usr/bin/env python3
"""
QUONIX TERMINAL - Windows MQTT controller for the QUONIX Android app.

MQTT:
  command: quonix/<device_id>/command
  response: quonix/<device_id>/response
  file: quonix/<device_id>/file

File transfer:
  FILE_META, FILE_CHUNK, FILE_END
  FILE_CHUNK.data is base64.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import paho.mqtt.client as mqtt
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Fehlende Pakete. Installiere: pip install -r requirements.txt")
    raise SystemExit(1)

DOWNLOAD_DIR = Path.home() / "Downloads" / "QUONIX"
SCREENSHOT_DIR = DOWNLOAD_DIR / "screenshots"
MEDIA_DIR = Path.home() / "Desktop" / "bilder"
for p in (DOWNLOAD_DIR, SCREENSHOT_DIR, MEDIA_DIR):
    p.mkdir(parents=True, exist_ok=True)


@dataclass
class Config:
    broker: str = os.getenv("QUONIX_MQTT_BROKER", "localhost")
    port: int = int(os.getenv("QUONIX_MQTT_PORT", "1883"))
    username: str = os.getenv("QUONIX_MQTT_USERNAME", "")
    password: str = os.getenv("QUONIX_MQTT_PASSWORD", "")
    device_id: str = os.getenv("QUONIX_DEVICE_ID", "QUONIX-001")
    tls: bool = os.getenv("QUONIX_MQTT_TLS", "0") == "1"
    timeout: float = float(os.getenv("QUONIX_TIMEOUT", "12"))


class Terminal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if cfg.username:
            self.client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            self.client.tls_set()
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.connected = False
        self.responses: dict[str, queue.Queue] = {}
        self.files: dict[str, dict[str, Any]] = {}

    @property
    def command_topic(self):
        return f"quonix/{self.cfg.device_id}/command"

    @property
    def response_topic(self):
        return f"quonix/{self.cfg.device_id}/response"

    @property
    def file_topic(self):
        return f"quonix/{self.cfg.device_id}/file"

    def connect(self):
        print(f"Verbinde MQTT: {self.cfg.broker}:{self.cfg.port} ...")
        self.client.connect(self.cfg.broker, self.cfg.port, 60)
        self.client.loop_start()
        end = time.time() + self.cfg.timeout
        while not self.connected and time.time() < end:
            time.sleep(0.1)
        if not self.connected:
            raise RuntimeError("MQTT-Verbindung fehlgeschlagen.")

    def close(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if int(reason_code) == 0:
            self.connected = True
            client.subscribe(self.response_topic, qos=1)
            client.subscribe(self.file_topic, qos=1)
            print("✓ MQTT verbunden")
            print(f"✓ Gerät: {self.cfg.device_id}")
        else:
            print(f"✗ MQTT Fehler: {reason_code}")

    def on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False

    def on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return

        rid = data.get("request_id")
        if not rid:
            return

        if msg.topic == self.response_topic:
            q = self.responses.get(rid)
            if q:
                q.put(data)
            return

        if msg.topic != self.file_topic:
            return

        transfer = self.files.get(rid)
        if not transfer:
            return

        typ = data.get("type")
        if typ == "FILE_META":
            transfer["filename"] = self.safe_filename(data.get("filename", "transfer.bin"))
            transfer["size"] = int(data.get("size", 0))
        elif typ == "FILE_CHUNK":
            try:
                transfer["chunks"][int(data["seq"])] = base64.b64decode(data["data"])
            except Exception as exc:
                transfer["error"] = str(exc)
        elif typ == "FILE_END":
            transfer["sha256"] = data.get("sha256", "")
            transfer["done"] = True

    @staticmethod
    def safe_filename(name: str) -> str:
        name = Path(name).name
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
        return name or "transfer.bin"

    def command(self, name: str, args: dict | None = None, timeout: float | None = None):
        rid = uuid.uuid4().hex
        q = queue.Queue(maxsize=1)
        self.responses[rid] = q
        payload = {
            "request_id": rid,
            "command": name,
            "args": args or {},
            "timestamp": int(time.time()),
        }
        try:
            info = self.client.publish(
                self.command_topic,
                json.dumps(payload, separators=(",", ":")),
                qos=1,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return {"ok": False, "error": "MQTT publish fehlgeschlagen"}
            try:
                return q.get(timeout=timeout or self.cfg.timeout)
            except queue.Empty:
                return {"ok": False, "error": "Keine Antwort vom Gerät."}
        finally:
            self.responses.pop(rid, None)

    def receive_file(self, command: str, args: dict | None, folder: Path, timeout=180):
        rid = uuid.uuid4().hex
        transfer = {"filename": "transfer.bin", "size": 0, "chunks": {},
                    "sha256": "", "done": False, "error": None}
        self.files[rid] = transfer
        payload = {
            "request_id": rid,
            "command": command,
            "args": args or {},
            "timestamp": int(time.time()),
        }
        try:
            info = self.client.publish(
                self.command_topic,
                json.dumps(payload, separators=(",", ":")),
                qos=1,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                print("✗ MQTT publish fehlgeschlagen")
                return None

            start = time.time()
            while time.time() - start < timeout and not transfer["done"] and not transfer["error"]:
                size = transfer["size"]
                if size:
                    got = sum(len(x) for x in transfer["chunks"].values())
                    pct = min(100, int(got * 100 / size))
                    print(f"\rÜbertragung: {pct:3d}%", end="", flush=True)
                time.sleep(0.1)
            print()

            if transfer["error"]:
                print(f"✗ Dateiübertragung: {transfer['error']}")
                return None
            if not transfer["done"]:
                print("✗ Zeitüberschreitung bei der Dateiübertragung.")
                return None

            data = b"".join(transfer["chunks"][i] for i in sorted(transfer["chunks"]))
            if transfer["size"] and len(data) != transfer["size"]:
                print("✗ Unvollständige Datei.")
                return None
            if transfer["sha256"]:
                digest = hashlib.sha256(data).hexdigest()
                if digest.lower() != transfer["sha256"].lower():
                    print("✗ SHA-256-Prüfung fehlgeschlagen.")
                    return None

            folder.mkdir(parents=True, exist_ok=True)
            target = folder / self.safe_filename(transfer["filename"])
            target.write_bytes(data)
            return target
        finally:
            self.files.pop(rid, None)


def result(r):
    if r.get("ok"):
        print("✓ Erfolgreich")
        if "data" in r:
            print(json.dumps(r["data"], indent=2, ensure_ascii=False))
    else:
        print("✗ " + str(r.get("error", "Unbekannter Fehler")))


def yn(prompt):
    while True:
        a = input(prompt + " [Y/N]: ").strip().lower()
        if a in ("y", "yes", "j", "ja"):
            return True
        if a in ("n", "no", "nein"):
            return False


def url_input():
    while True:
        u = input("URL: ").strip()
        p = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(u)
        if p.scheme in ("http", "https") and p.netloc:
            return u
        print("Bitte eine gültige http/https URL eingeben.")


def scan(url):
    r = requests.get(url, timeout=20, headers={"User-Agent": "QUONIX-Terminal/1.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    extensions = (".apk",".zip",".exe",".msi",".pdf",".7z",".rar",".jpg",".jpeg",
                  ".png",".webp",".mp4",".mov",".mp3",".csv",".xlsx",".docx",".txt")
    found, seen = [], set()
    from urllib.parse import urljoin, urlparse
    for tag in soup.find_all(["a", "source", "video", "img"]):
        href = tag.get("href") or tag.get("src")
        if not href:
            continue
        absolute = urljoin(r.url, href)
        name = Path(urlparse(absolute).path).name
        if name and name.lower().endswith(extensions) and absolute not in seen:
            seen.add(absolute)
            found.append({"filename": name, "url": absolute})
    return found


def device(t, cmd, label, args=None):
    print(label + "...")
    result(t.command(cmd, args))
    result


def access_menu(t):
    commands = {
        "1": ("KIOSK_ON", "Kiosk wird aktiviert"),
        "2": ("KIOSK_OFF", "Kiosk wird deaktiviert"),
        "3": ("SHOW_LOCKSCREEN", "Lockscreen wird angezeigt"),
        "4": ("OPEN_SETTINGS", "Android-Einstellungen werden geöffnet"),
        "5": ("OPEN_CHROME", "Chrome wird geöffnet"),
        "6": ("OPEN_PASSWORD_MANAGER", "Passwort-Manager wird geöffnet"),
    }
    while True:
        print("\n----------------------------------------")
        print("        ZUGRIFFSVERWALTUNG")
        print("----------------------------------------")
        print("[1] Kiosk AN")
        print("[2] Kiosk AUS")
        print("[3] Lockscreen anzeigen")
        print("[4] Android-Einstellungen")
        print("[5] Chrome öffnen")
        print("[6] Passwort-Manager öffnen")
        print("[0] Zurück")
        print("----------------------------------------")
        c = input("Auswahl: ").strip()
        if c == "0":
            return
        if c in commands:
            cmd, label = commands[c]
            device(t, cmd, label)
            if c == "6":
                print("Die Entsperrung erfolgt direkt am Android-Gerät.")


def menu(t):
    current_url = ""
    while True:
        print("\n" + "=" * 40)
        print("          QUONIX TERMINAL")
        print("=" * 40)
        print("\nGERÄT")
        print("[1] Kiosk AN")
        print("[2] Kiosk AUS")
        print("[3] Lockscreen anzeigen")
        print("[4] Status")
        print("[5] Gerät neu laden")
        print("[6] Neustart")
        print("\nWEB")
        print("[10] Website öffnen")
        print("[11] Website-Download suchen")
        print("[12] Download durchführen")
        print("[13] Download öffnen")
        print("\nDATEIEN")
        print("[15] Bilder & Videos exportieren")
        print("[16] Screenshot erstellen")
        print("\nADMIN")
        print("[40] Zugriffsverwaltung")
        print("[41] Android-Einstellungen")
        print("[42] Chrome öffnen")
        print("[43] Passwort-Manager öffnen")
        print("\n[0] Beenden")
        print("=" * 40)
        c = input("Auswahl:\n> ").strip()

        if c == "0":
            return
        elif c == "1":
            device(t, "KIOSK_ON", "Kiosk wird aktiviert")
        elif c == "2":
            device(t, "KIOSK_OFF", "Kiosk wird deaktiviert")
        elif c == "3":
            device(t, "SHOW_LOCKSCREEN", "Lockscreen wird angezeigt")
        elif c == "4":
            device(t, "STATUS", "Status wird abgefragt")
        elif c == "5":
            device(t, "RELOAD", "WebView wird neu geladen")
        elif c == "6":
            if yn("Gerät wirklich neu starten?"):
                device(t, "REBOOT", "Neustart wird angefordert")
        elif c == "10":
            current_url = url_input()
            result(t.command("OPEN_URL", {"url": current_url, "keep_kiosk": True}))
        elif c == "11":
            current_url = url_input()
            try:
                found = scan(current_url)
                if not found:
                    print("Keine unterstützten Downloads gefunden.")
                else:
                    for i, x in enumerate(found, 1):
                        print(f"[{i}] {x['filename']}\n    {x['url']}")
            except Exception as e:
                print("✗ " + str(e))
        elif c == "12":
            url = current_url or url_input()
            try:
                found = scan(url)
                if not found:
                    print("Keine Downloads gefunden.")
                    continue
                for i, x in enumerate(found, 1):
                    print(f"[{i}] {x['filename']}")
                try:
                    i = int(input("Download auswählen: ")) - 1
                    item = found[i]
                except (ValueError, IndexError):
                    print("Ungültige Auswahl.")
                    continue
                print(f"Download gefunden: {item['filename']}")
                if yn("Herunterladen?"):
                    result(t.command("DOWNLOAD", {
                        "url": item["url"],
                        "filename": item["filename"],
                        "open_after": False,
                    }))
            except Exception as e:
                print("✗ " + str(e))
        elif c == "13":
            result(t.command("OPEN_LAST_DOWNLOAD"))
        elif c == "15":
            if yn("Bilder & Videos nach Windows übertragen?"):
                target = t.receive_file("EXPORT_MEDIA", {"destination": "bilder"}, MEDIA_DIR, 300)
                if target:
                    print(f"✓ Export gespeichert: {target}")
        elif c == "16":
            print("Screenshot wird erstellt...")
            target = t.receive_file("SCREENSHOT", {}, SCREENSHOT_DIR, 90)
            if target:
                print(f"✓ Screenshot empfangen: {target}")
        elif c == "40":
            access_menu(t)
        elif c == "41":
            device(t, "OPEN_SETTINGS", "Android-Einstellungen werden geöffnet")
        elif c == "42":
            device(t, "OPEN_CHROME", "Chrome wird geöffnet")
        elif c == "43":
            device(t, "OPEN_PASSWORD_MANAGER", "Passwort-Manager wird geöffnet")
            print("Die Entsperrung erfolgt direkt am Android-Gerät.")
        else:
            print("Unbekannte Auswahl.")


def main():
    print("=" * 40)
    print("          QUONIX TERMINAL")
    print("=" * 40)
    cfg = Config()
    broker = input(f"MQTT-Server [{cfg.broker}]: ").strip()
    if broker:
        cfg.broker = broker
    device_id = input(f"Gerät-ID [{cfg.device_id}]: ").strip()
    if device_id:
        cfg.device_id = device_id

    t = Terminal(cfg)
    try:
        t.connect()
        menu(t)
    except KeyboardInterrupt:
        print("\nBeendet.")
    except Exception as e:
        print("✗ " + str(e))
        return 1
    finally:
        t.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
