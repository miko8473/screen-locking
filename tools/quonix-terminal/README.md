# QUONIX Terminal

Windows-CLI zur Steuerung der bestehenden QUONIX-Android-App über deren MQTT-Protokoll.

## Installation

PowerShell:

    cd tools\quonix-terminal
    py -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    python quonix_terminal.py

Umgebungsvariablen:

    QUONIX_MQTT_BROKER
    QUONIX_MQTT_PORT
    QUONIX_MQTT_USERNAME
    QUONIX_MQTT_PASSWORD
    QUONIX_MQTT_TLS=1
    QUONIX_APP_INSTANCE_ID
    QUONIX_TIMEOUT
    QUONIX_COMMAND_TOPIC
    QUONIX_EVENT_TOPIC
    QUONIX_PASSWORD_MANAGER_PACKAGE

## MQTT-Protokoll

Die Android-App verwendet standardmäßig:

    Command:  wk/command
    Events:   wk/event/<eventType>
    Responses:wk/response/<responseType>

Das Terminal verwendet dieselben Topics und das vorhandene QUONIX-Protokoll.
Die App-Instance-ID wird als targetInstances verwendet, wenn sie eingetragen ist.

## Kiosk-Sicherheit

[1] Kiosk AN sendet den bestehenden MQTT-Befehl `lock`.

[2] Kiosk AUS sendet den bestehenden MQTT-Befehl `unlock`.

Die lokale QUONIX-Passwortprüfung bleibt davon unabhängig bestehen.
Das Windows-Terminal liest, speichert oder überträgt das lokale QUONIX-Passwort nicht.

Der Passwort-Manager wird niemals automatisch entsperrt. Falls ein Paket über
QUONIX_PASSWORD_MANAGER_PACKAGE konfiguriert wird, wird nur die App gestartet;
die Authentifizierung erfolgt lokal auf Android.

## Status

[4] Status verwendet eine eindeutige Antwort-Topic und den bestehenden
`get_status`-Request der Android-App.

## Noch nicht im MQTT-Kern verbunden

Website-Download, Screenshot-Export und Medienexport aus der früheren Prototyp-Version
sind bewusst nicht als funktionierende Android-Befehle ausgegeben. Das verhindert,
dass das Terminal Erfolg meldet, obwohl die Android-App keinen entsprechenden Handler hat.


<!-- final build verification -->
