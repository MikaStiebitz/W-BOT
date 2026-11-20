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
    """Text with the console commands (explanations in English, FluidNC commands verbatim)."""
    if not _SSID_OK.match(ssid or ""):
        raise ValueError("The SSID must be 1..32 characters long and must not contain a line break.")
    if not _HOST_OK.match(hostname or ""):
        raise ValueError("Hostname: only letters, digits and '-' (max. 32 characters, no dot).")
    return f"""\
# W-BOT: bring the board onto WiFi once  (OUTPUT only - nothing is sent)
#
# How: connect the board by USB cable, open the serial console (115200 baud, e.g. in the
# W-BOT console or a terminal program, line ending LF), and type the lines below ONE AT A TIME.
# YOU enter the password yourself - it is stored nowhere in the project.
# Lines marked '# UNVERIFIED' are not documented for FluidNC v3.9.9: first type `$S` and check
# whether the setting has that name (an alternative name is given next to it), then set it.

$S                                   # lists ALL real setting names (alternative: $Settings/List)
$Wifi/ListAPs                        # optional: shows the visible WiFi networks (the board must see a 2.4 GHz network)

$Sta/SSID={ssid}
$Sta/Password=<YOUR-WIFI-PASSWORD>   # fill in yourself!
$Sta/IPMode=DHCP                     # UNVERIFIED for v3.9.9 (wiki mirror of an older version: DHCP|Static)

$System/Hostname={hostname}          # UNVERIFIED, alternative depending on version: $Hostname={hostname}
$WiFi/Mode=STA                       # UNVERIFIED, alternative depending on version: $Radio/Mode=STA   (STA = join the home network)
                                     # check the values with: $WiFi/Mode=*  or  $Radio/Mode=*

$Telnet/Enable=On                    # UNVERIFIED (name documented, value On/Off not). Port 23 is the default ($Telnet/Port)
# WebSocket: in v3.x it runs on port 81 (source: community wiki, not confirmed for v3.9.9).
# A separate switch is not documented - search `$S` for 'WebSocket', e.g.:
#   $WebSocket/Enable=On             # UNVERIFIED, it may not exist (then it hangs off $Http/Enable)

# Then restart the board (reset button or power). At startup it reports in the log:
#   [MSG:INFO: Connected - IP is 192.168.x.y]     <- THAT is the IP address
#   [MSG:INFO: Telnet Started on port 23]
#   [MSG:INFO: Start mDNS with hostname:http://{hostname}.local/]
# IP without a restart: `$I` or `$Sta` (UNVERIFIED whether the IP is shown there - passwords are never displayed);
# otherwise look in the router's device list (name '{hostname}').
#
# Check from the PC (board and PC on the same WiFi):
#   python -m wbot probe --board-host {hostname}.local        # read only, moves nothing
#   python -m wbot serve --connect wifi --board-host {hostname}.local
# If the name does not resolve (mDNS/Bonjour missing on the PC), use the IP address from the log instead of {hostname}.local.
#
# SAFETY: FluidNC Telnet and WebSocket have NO authentication. Anyone on the same network can
# drive the board. Only use it on a trusted network, and switch Telnet off again if in doubt.
"""
