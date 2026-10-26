#!/bin/sh
# telepwn cool-mode launcher (baked into /phone/telepwn). Invoked by the phone.sh
# diversion once this phone's content share is mounted.
#   $1 = provisioning server IP
#   $2 = share mountpoint (NFS export, or a USB stick carrying the same layout)
#
# The share owns everything that changes; this only hands off to its launcher. If the
# share has no usable launcher we fall back to the baked splash, so the screen is
# never blank.
SRV="$1"
MNT="${2:-/tmp/telepwn}"
DIR=/phone/telepwn

if [ -x "$MNT/run" ]; then
    exec "$MNT/run" "$SRV" "$MNT"
fi

# fallback: share mounted but unusable
while : ; do
    "$DIR/lcdfb" png "$DIR/splash.png" 60 >/dev/null 2>&1 || sleep 5
done
