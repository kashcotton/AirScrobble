#!/bin/bash
set -e

mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true
eval $(dbus-launch --sh-syntax)
export DBUS_SESSION_BUS_ADDRESS

Xvfb :99 -screen 0 1024x768x16 -ac +extension GLX +render -noreset &
export DISPLAY=:99
sleep 1

pulseaudio --start --exit-idle-time=-1 --daemonize=yes 2>/dev/null || true
sleep 1
pactl load-module module-null-sink sink_name=virtual_output sink_properties=device.description=VirtualOutput 2>/dev/null || true
pactl set-default-sink virtual_output 2>/dev/null || true

exec python app.py
