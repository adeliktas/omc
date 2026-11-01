#!/bin/sh
# Run one fleet script of this instance with its environment, a singleton lock and its log.
# usage: deploy/run.sh <script.py>     (OMC_HOME and OMC_DATA come from the crontab)
set -a
. "$OMC_DATA/.env"
. "$OMC_DATA/etc/fleet.env"
set +a
name=$(basename "$1" .py)
cd "$OMC_HOME" && exec flock -n "$OMC_DATA/run/$name.lock" python3 "scripts/$1" >> "$OMC_DATA/log/$name.log" 2>&1
