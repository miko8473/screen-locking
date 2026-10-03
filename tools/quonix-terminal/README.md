# QUONIX Terminal

Windows-CLI zur Steuerung eines QUONIX-Android-Testgeräts über MQTT.

## Installation

PowerShell/CMD:

    cd tools\quonix-terminal
    py -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    python quonix_terminal.py

Optional per Umgebungsvariable:

    QUONIX_MQTT_BROKER
    QUONIX_MQTT_PORT
    QUONIX_MQTT_USERNAME
    QUONIX_MQTT_PASSWORD
    QUONIX_MQTT_TLS=1
    QUONIX_DEVICE_ID
    QUONIX_TIMEOUT

## Menü

    [1] Kiosk AN
    [2] Kiosk AUS
    [3] Lockscreen anzeigen
    [4] Status
    [5] Gerät neu laden
    [6] Neustart
    [10] Website öffnen
    [11] Website-Download suchen
    [12] Download durchführen
    [13] Download öffnen
    [15] Bilder & Videos exportieren
    [16] Screenshot erstellen
    [40] Zugriffsverwaltung
    [41] Android-Einstellungen
    [42] Chrome öffnen
    [43] Passwort-Manager öffnen
    [0] Beenden

## MQTT

Command:
    quonix/<device_id>/command

Response:
    quonix/<device_id>/response

Files:
    quonix/<device_id>/file

Command-JSON:
    {"request_id":"abc","command":"STATUS","args":{},"timestamp":1760000000}

Response-JSON:
    {"request_id":"abc","ok":true,"command":"STATUS","data":{}}

Für SCREENSHOT und EXPORT_MEDIA erwartet der Terminal:
    FILE_META
    FILE_CHUNK
    FILE_CHUNK
    ...
    FILE_END

FILE_CHUNK.data ist Base64. FILE_END.sha256 wird geprüft, wenn vorhanden.

Screenshots landen unter:
    %USERPROFILE%\Downloads\QUONIX\screenshots

Medienexporte landen unter:
    %USERPROFILE%\Desktop\bilder

## Android-Seite

Der Terminal ist die Windows-Seite des Protokolls. Die Android-App muss die Commands in
ihrem bestehenden MQTT-Handler verarbeiten:

    KIOSK_ON
    KIOSK_OFF
    SHOW_LOCKSCREEN
    STATUS
    RELOAD
    REBOOT
    OPEN_URL
    DOWNLOAD
    OPEN_LAST_DOWNLOAD
    EXPORT_MEDIA
    SCREENSHOT
    OPEN_SETTINGS
    OPEN_CHROME
    OPEN_PASSWORD_MANAGER

OPEN_URL enthält keep_kiosk=true.

SCREENSHOT ist ein expliziter, vom Terminal ausgelöster Dateiexport. Das Script sammelt
keine Geräte-PINs, Passwörter oder Passwort-Manager-Secrets.
