#!/bin/bash
# Double-click this file in Finder to start NYC Job Scout.
cd "$(dirname "$0")"
PY=python3
for v in python3.13 python3.12 python3.11 python3.10; do command -v $v >/dev/null && PY=$v && break; done
exec $PY app.py
