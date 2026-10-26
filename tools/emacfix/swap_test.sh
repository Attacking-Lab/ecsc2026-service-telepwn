#!/bin/sh
# Universal swap: unload any emac variant, load /tmp/test.ko, restore eth0, watchdog.
#   ( sh /tmp/swap_test.sh </dev/null >/tmp/swap.log 2>&1 & )  then  touch /tmp/ok
trap '' HUP INT TERM
rm -f /tmp/ok
sleep 2
ifconfig eth0 down
for m in emacfull emacpfix emaccpy2 emacprb2 emacprob emacrcs2 emacrcsm emaccsum emacfix emaccopy emacgate emac; do
    rmmod $m 2>/dev/null
done
insmod /tmp/test.ko
ifconfig eth0 hw ether 80:5E:C0:55:DD:8F 2>/dev/null
ifconfig eth0 192.168.69.200 netmask 255.255.255.0 up
route add default gw 192.168.69.1 2>/dev/null
i=0
while [ ! -f /tmp/ok ]; do
    i=$(expr $i + 1)
    [ $i -ge 90 ] && reboot
    sleep 2
done
