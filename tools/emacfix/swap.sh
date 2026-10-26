#!/bin/sh
# Hot-swap the phone's emac driver with the cache-fixed build, restore eth0, and
# auto-reboot to the pristine driver if the network doesn't come back. The root fs
# is read-only, so /usr/driver/emac.ko is untouched and any reboot recovers.
#
# Loads the cache-fixed driver under the module name `emacfix` (renamed), so a
# reboot reloads the pristine /usr/driver/emac.ko (root fs is read-only). Unload
# the test driver with `rmmod emacfix`.
#
# RUN DETACHED (survives the telnet drop when eth0 goes down):
#     ( sh /tmp/swap.sh </dev/null >/tmp/swap.log 2>&1 & )
# Then reconnect and, once you've confirmed the phone is healthy:
#     touch /tmp/ok        # cancels the watchdog reboot
#
# WARNING: a kernel panic from a bad module would not be caught by this userspace
# watchdog -- recovery would then need a power-cycle (which reloads pristine emac).
trap '' HUP INT TERM
IP=192.168.69.200
MASK=255.255.255.0
MAC=80:5E:C0:55:DD:8F
GW=192.168.69.1

rm -f /tmp/ok
sleep 2                       # let the launching shell return before eth0 drops
ifconfig eth0 down
rmmod emacfix 2>/dev/null     # whichever is currently loaded
rmmod emac 2>/dev/null
insmod /tmp/emacfix.ko
ifconfig eth0 hw ether $MAC 2>/dev/null   # driver usually sets the MAC itself; ignore
ifconfig eth0 $IP netmask $MASK up
route add default gw $GW 2>/dev/null

# Watchdog: wait up to ~120s for /tmp/ok; otherwise reboot into the pristine emac.
# busybox ash here has no $((...)) -> use expr.
i=0
while [ ! -f /tmp/ok ]; do
    i=$(expr $i + 1)
    [ $i -ge 60 ] && reboot
    sleep 2
done
