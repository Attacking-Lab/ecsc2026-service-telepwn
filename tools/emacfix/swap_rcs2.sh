#!/bin/sh
# Swap current emac variant -> emac_ring_csum2.ko (256-ring + ip_summed@0x69=NONE, mod
# emacrcs2), restore eth0, watchdog. Detached:
#   ( sh /tmp/swap_rcs2.sh </dev/null >/tmp/swap.log 2>&1 & )  then  touch /tmp/ok
trap '' HUP INT TERM
rm -f /tmp/ok
sleep 2
ifconfig eth0 down
rmmod emacrcs2 2>/dev/null
rmmod emacprob 2>/dev/null
rmmod emacrcsm 2>/dev/null
rmmod emaccsum 2>/dev/null
rmmod emac 2>/dev/null
insmod /tmp/emac_ring_csum2.ko
ifconfig eth0 hw ether 80:5E:C0:55:DD:8F 2>/dev/null
ifconfig eth0 192.168.69.200 netmask 255.255.255.0 up
route add default gw 192.168.69.1 2>/dev/null
i=0
while [ ! -f /tmp/ok ]; do
    i=$(expr $i + 1)
    [ $i -ge 90 ] && reboot
    sleep 2
done
