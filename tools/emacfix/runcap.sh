#!/bin/sh
# Self-contained DMAC-wedge capture: swap to the gate driver, start mserver + a
# dmacpeek one-shot loop logging ch2 register state to the persistent USB, then a
# lenient watchdog. Run detached:  ( sh /tmp/udisk1/runcap.sh </dev/null >/dev/null 2>&1 & )
# Apply load from the laptop; when the gate wedges, dmac.log's tail = the stuck state.
# Watchdog reboots to pristine after ~600s if /tmp/ok never appears.
trap '' HUP INT TERM
rm -f /tmp/ok /tmp/udisk1/gate.log /tmp/udisk1/dmac.log
cp /tmp/udisk1/emacfix_gate2.ko /tmp/emacgate.ko
cp /tmp/udisk1/dmacpeek /tmp/dmacpeek
cp /tmp/udisk1/mserver /tmp/mserver
chmod +x /tmp/emacgate.ko /tmp/dmacpeek /tmp/mserver
sleep 2
echo DOWN >/tmp/udisk1/gate.log
ifconfig eth0 down 2>>/tmp/udisk1/gate.log
rmmod emacgate 2>>/tmp/udisk1/gate.log
rmmod emac 2>>/tmp/udisk1/gate.log
insmod /tmp/emacgate.ko 2>>/tmp/udisk1/gate.log
cat /proc/modules | grep emac >>/tmp/udisk1/gate.log
ifconfig eth0 192.168.69.200 netmask 255.255.255.0 up 2>>/tmp/udisk1/gate.log
route add default gw 192.168.69.1 2>>/tmp/udisk1/gate.log
echo DONE >>/tmp/udisk1/gate.log
sync
( cd /tmp && PORT=22000 ./mserver >/tmp/mserver.log 2>&1 ) &
( while true; do /tmp/dmacpeek >> /tmp/udisk1/dmac.log; sync; sleep 1; done ) &
i=0
while [ ! -f /tmp/ok ]; do
  i=$(expr $i + 1)
  [ $i -ge 300 ] && reboot
  sleep 2
done
