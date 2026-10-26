#!/bin/sh
# Hot-swap pristine emac -> emac_csumnone.ko (module name emaccsum), restore eth0,
# auto-reboot to pristine if the net doesn't come back. Root fs is read-only so any
# reboot recovers /usr/driver/emac.ko. Run detached:
#     ( sh /tmp/swap_csum.sh </dev/null >/tmp/swap.log 2>&1 & )
# then reconnect, verify, and: touch /tmp/ok   (cancels the watchdog reboot)
trap '' HUP INT TERM
IP=192.168.69.200
MASK=255.255.255.0
MAC=80:5E:C0:55:DD:8F
GW=192.168.69.1

rm -f /tmp/ok
sleep 2
ifconfig eth0 down
rmmod emaccsum 2>/dev/null
rmmod emac 2>/dev/null
insmod /tmp/emac_csumnone.ko
ifconfig eth0 hw ether $MAC 2>/dev/null
ifconfig eth0 $IP netmask $MASK up
route add default gw $GW 2>/dev/null

i=0
while [ ! -f /tmp/ok ]; do
    i=$(expr $i + 1)
    [ $i -ge 90 ] && reboot
    sleep 2
done
