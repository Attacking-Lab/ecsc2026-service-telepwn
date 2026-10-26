#!/bin/sh
# Timekeeping for the phone, which has no RTC (it boots at epoch+uptime). A clock file on
# the stick carries wall-clock across reboots: on start, if it exists, set the system date
# from its mtime; otherwise create it. Then re-touch it every minute so its mtime tracks
# "now" for the next boot. busybox 1.10.3 here has no `touch -t/-d`, so the mtime is read
# back with `date -r`.
CLOCK=/tmp/telepwn_data/.clock

if [ -e "$CLOCK" ]; then
    date "$(date -r "$CLOCK" +%m%d%H%M%Y.%S)" >/dev/null 2>&1
else
    touch "$CLOCK"
fi

while :; do
    sleep 60
    touch "$CLOCK"
done
