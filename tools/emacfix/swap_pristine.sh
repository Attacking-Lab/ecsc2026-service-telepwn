#!/bin/sh
# Swap current emac variant -> pristine /usr/driver/emac.ko, restore eth0. Run detached:
#   ( sh /tmp/swap_pristine.sh </dev/null >/tmp/swap.log 2>&1 & )  then  touch /tmp/ok
trap '' HUP INT TERM
rm -f /tmp/ok
sleep 2
ifconfig eth0 down
rmmod emacrcsm 2>/dev/null
rmmod emaccsum 2>/dev/null
rmmod emacfix 2>/dev/null
rmmod emac 2>/dev/null
insmod /usr/driver/emac.ko
ifconfig eth0 hw ether 80:5E:C0:55:DD:8F 2>/dev/null
ifconfig eth0 192.168.69.200 netmask 255.255.255.0 up
route add default gw 192.168.69.1 2>/dev/null
i=0
while [ ! -f /tmp/ok ]; do
    i=$(expr $i + 1)
    [ $i -ge 90 ] && reboot
    sleep 2
done
