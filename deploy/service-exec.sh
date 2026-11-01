#!/bin/sh
# Run one fleet service of this instance with its environment, in the foreground.
# usage: deploy/service-exec.sh <script.py>     (systemd user units exec this; OMC_HOME/OMC_DATA
# are passed by the unit, the rest is sourced exactly like deploy/run.sh does for cron jobs)
set -a
. "$OMC_DATA/.env"
. "$OMC_DATA/etc/fleet.env"
set +a
cd "$OMC_HOME" && exec python3 "scripts/$1"
