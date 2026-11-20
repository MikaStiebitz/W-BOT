"""One-time WiFi setup of the FluidNC board: this module only PRINTS the console commands.

Nothing is sent to a board and no password is handled: the user types the commands once over the
serial console (COM3) and fills in the WLAN password themself. Setting names that could not be
verified for FluidNC v3.9.9 are marked ``UNVERIFIED``; the board can tell: ``$S`` /
``$Settings/List`` lists every setting that really exists in the running firmware.
"""
from __future__ import annotations

import re

_SSID_OK = re.compile(r"^[^\r\n]{1,32}$")
_HOST_OK = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,30}[A-Za-z0-9])?$")


def wifi_commands(ssid: str, hostname: str = "wbot") -> str:
    """Text with the console commands (German explanations, FluidNC commands verbatim)."""
    if not _SSID_OK.match(ssid or ""):
        raise ValueError("SSID muss 1..32 Zeichen lang sein und darf keinen Zeilenumbruch enthalten.")
    if not _HOST_OK.match(hostname or ""):
        raise ValueError("Hostname: nur Buchstaben, Ziffern und '-' (max. 32 Zeichen, kein Punkt).")
    return f"""\
# W-BOT: Board einmalig ins WLAN bringen  (nur AUSGABE - es wird nichts gesendet)
#
# So gehts: Board per USB-Kabel anschliessen, serielle Konsole oeffnen (115200 Baud, z. B. in der
# W-BOT-Konsole oder einem Terminalprogramm, Zeilenende LF), die Zeilen unten EINZELN eintippen.
# Das Passwort setzt DU selbst ein - es steht nirgends im Projekt.
# Zeilen mit '# UNVERIFIED' sind fuer FluidNC v3.9.9 nicht belegt: erst `$S` tippen und pruefen,
# ob die Einstellung so heisst (Alternativname steht daneben), dann erst setzen.

$S                                   # listet ALLE echten Einstellungsnamen (Alternative: $Settings/List)
$Wifi/ListAPs                        # optional: zeigt die sichtbaren WLANs (Board muss ein 2,4-GHz-WLAN sehen)

$Sta/SSID={ssid}
$Sta/Password=<DEIN-WLAN-PASSWORT>   # selbst einsetzen!
$Sta/IPMode=DHCP                     # UNVERIFIED fuer v3.9.9 (Wiki-Spiegel aelterer Version: DHCP|Static)

$System/Hostname={hostname}          # UNVERIFIED, Alternative je nach Version: $Hostname={hostname}
$WiFi/Mode=STA                       # UNVERIFIED, Alternative je nach Version: $Radio/Mode=STA   (STA = ins Heimnetz)
                                     # Werte pruefen mit: $WiFi/Mode=*  bzw. $Radio/Mode=*

$Telnet/Enable=On                    # UNVERIFIED (Name belegt, Wert On/Off nicht). Port 23 ist Standard ($Telnet/Port)
# WebSocket: in v3.x laeuft er auf Port 81 (Quelle: Community-Wiki, nicht aus v3.9.9 bestaetigt).
# Ein eigener Schalter ist nicht belegt - in `$S` nach 'WebSocket' suchen, z. B.:
#   $WebSocket/Enable=On             # UNVERIFIED, evtl. gibt es ihn nicht (dann haengt er an $Http/Enable)

# Danach Board neu starten (Reset-Taste oder Strom). Beim Start meldet es sich im Log:
#   [MSG:INFO: Connected - IP is 192.168.x.y]     <- DAS ist die IP-Adresse
#   [MSG:INFO: Telnet Started on port 23]
#   [MSG:INFO: Start mDNS with hostname:http://{hostname}.local/]
# IP ohne Neustart: `$I` bzw. `$Sta` (UNVERIFIED, ob die IP dort steht - Passwoerter werden nie angezeigt);
# sonst in der Geraeteliste des Routers nachsehen (Name '{hostname}').
#
# Pruefen vom PC aus (Board und PC im selben WLAN):
#   python -m wbot probe --board-host {hostname}.local        # nur lesen, bewegt nichts
#   python -m wbot serve --connect wifi --board-host {hostname}.local
# Klappt der Name nicht (mDNS/Bonjour fehlt auf dem PC), die IP-Adresse aus dem Log statt {hostname}.local nehmen.
#
# SICHERHEIT: FluidNC-Telnet und -WebSocket haben KEINE Anmeldung. Jeder im selben Netz kann das
# Board fahren. Nur in einem vertrauenswuerdigen Netz betreiben, Telnet im Zweifel wieder abschalten.
"""
