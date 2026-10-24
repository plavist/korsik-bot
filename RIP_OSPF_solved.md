# RIP ↔ OSPF: полная настройка всех узлов проекта

Топология — из `RIP-OSPF.gns3project`, команды и схема адресации — продолжение `RIP_OSPF.md` (там описаны только 4 маршрутизатора и 2 ПК, здесь — все 10 маршрутизаторов и 8 ПК).

## Включить демоны RIP & OSPF (на каждом маршрутизаторе)
```
sed -i 's/ospfd=no/ospfd=yes/' /etc/frr/daemons
sed -i 's/ripd=no/ripd=yes/' /etc/frr/daemons
kill $(pidof watchfrr)
/usr/lib/frr/docker-start &
```

## Схема адресации

| Сеть | Узлы |
|---|---|
| 172.20.40.0/30 | R-CU-RIP-CORE eth0 (172.20.40.1)<br>R-CU-RIP-EDU eth0 (172.20.40.2) |
| 172.20.40.4/30 | R-CU-RIP-CORE eth1 (172.20.40.5)<br>R-CU-RIP-ADMIN eth0 (172.20.40.6) |
| 172.20.40.8/30 | R-CU-RIP-EDU eth1 (172.20.40.9)<br>R-CU-RIP-EXAMS eth0 (172.20.40.10) |
| 172.20.40.12/30 | R-CU-RIP-ADMIN eth1 (172.20.40.13)<br>R-CU-RIP-EXAMS eth1 (172.20.40.14) |
| 172.20.40.16/30 | R-CU-RIP-ADMIN eth2 (172.20.40.17)<br>R-CU-RIP-ADMISSIONS eth0 (172.20.40.18) |
| 172.21.40.0/30 | R-CU-OSPF-CORE eth0 (172.21.40.1)<br>R-CU-OSPF-IT eth0 (172.21.40.2) |
| 172.21.40.4/30 | R-CU-OSPF-CORE eth1 (172.21.40.5)<br>R-CU-OSPF-LIB eth0 (172.21.40.6) |
| 172.21.40.8/30 | R-CU-OSPF-IT eth1 (172.21.40.9)<br>R-CU-OSPF-PORTAL eth0 (172.21.40.10) |
| 172.21.40.12/30 | R-CU-OSPF-LIB eth1 (172.21.40.13)<br>R-CU-OSPF-PORTAL eth1 (172.21.40.14) |
| 172.21.40.16/30 | R-CU-OSPF-LIB eth2 (172.21.40.17)<br>R-CU-OSPF-SECURITY eth0 (172.21.40.18) |
| 172.22.40.0/30 | R-CU-RIP-CORE eth2 (172.22.40.1)<br>R-CU-OSPF-CORE eth2 (172.22.40.2) |
| 172.24.40.0/24 | R-CU-RIP-EDU eth2 (172.24.40.1) |
| 172.25.40.0/24 | R-CU-RIP-ADMIN eth3 (172.25.40.1) |
| 172.26.40.0/24 | R-CU-RIP-EXAMS eth2 (172.26.40.1) |
| 172.27.40.0/24 | R-CU-RIP-ADMISSIONS eth1 (172.27.40.1) |
| 172.28.40.0/24 | R-CU-OSPF-IT eth2 (172.28.40.1) |
| 172.29.40.0/24 | R-CU-OSPF-LIB eth3 (172.29.40.1) |
| 172.30.40.0/24 | R-CU-OSPF-PORTAL eth2 (172.30.40.1) |
| 172.31.40.0/24 | R-CU-OSPF-SECURITY eth1 (172.31.40.1) |

## R-CU-RIP-CORE (RIP зона)
```
configure terminal
interface eth0
 ip address 172.20.40.1/30
 no shutdown
exit
interface eth1
 ip address 172.20.40.5/30
 no shutdown
exit
interface eth2
 ip address 172.22.40.1/30
 no shutdown
exit
router rip
 version 2
 network eth0
 network eth1
 network eth2
 redistribute ospf
exit
end
write memory
```

## R-CU-RIP-EDU (RIP зона)
```
configure terminal
interface eth0
 ip address 172.20.40.2/30
 no shutdown
exit
interface eth1
 ip address 172.20.40.9/30
 no shutdown
exit
interface eth2
 ip address 172.24.40.1/24
 no shutdown
exit
router rip
 version 2
 network eth0
 network eth1
 network eth2
 passive-interface eth2
exit
end
write memory
```

## R-CU-RIP-ADMIN (RIP зона)
```
configure terminal
interface eth0
 ip address 172.20.40.6/30
 no shutdown
exit
interface eth1
 ip address 172.20.40.13/30
 no shutdown
exit
interface eth2
 ip address 172.20.40.17/30
 no shutdown
exit
interface eth3
 ip address 172.25.40.1/24
 no shutdown
exit
router rip
 version 2
 network eth0
 network eth1
 network eth2
 network eth3
 passive-interface eth3
exit
end
write memory
```

## R-CU-RIP-EXAMS (RIP зона)
```
configure terminal
interface eth0
 ip address 172.20.40.10/30
 no shutdown
exit
interface eth1
 ip address 172.20.40.14/30
 no shutdown
exit
interface eth2
 ip address 172.26.40.1/24
 no shutdown
exit
router rip
 version 2
 network eth0
 network eth1
 network eth2
 passive-interface eth2
exit
end
write memory
```

## R-CU-RIP-ADMISSIONS (RIP зона)
```
configure terminal
interface eth0
 ip address 172.20.40.18/30
 no shutdown
exit
interface eth1
 ip address 172.27.40.1/24
 no shutdown
exit
router rip
 version 2
 network eth0
 network eth1
 passive-interface eth1
exit
end
write memory
```

## R-CU-OSPF-CORE (OSPF зона)
```
configure terminal
interface eth0
 ip address 172.21.40.1/30
 no shutdown
exit
interface eth1
 ip address 172.21.40.5/30
 no shutdown
exit
interface eth2
 ip address 172.22.40.2/30
 no shutdown
exit
router ospf
 ospf router-id 10.255.0.10
 network 172.21.40.0/30 area 0
 network 172.21.40.4/30 area 0
 redistribute rip
exit
router rip
 version 2
 network eth2
 redistribute ospf
exit
end
write memory
```

## R-CU-OSPF-IT (OSPF зона)
```
configure terminal
interface eth0
 ip address 172.21.40.2/30
 no shutdown
exit
interface eth1
 ip address 172.21.40.9/30
 no shutdown
exit
interface eth2
 ip address 172.28.40.1/24
 no shutdown
exit
router ospf
 ospf router-id 10.255.0.11
 network 172.21.40.0/30 area 0
 network 172.21.40.8/30 area 0
 network 172.28.40.0/24 area 0
 passive-interface eth2
exit
end
write memory
```

## R-CU-OSPF-LIB (OSPF зона)
```
configure terminal
interface eth0
 ip address 172.21.40.6/30
 no shutdown
exit
interface eth1
 ip address 172.21.40.13/30
 no shutdown
exit
interface eth2
 ip address 172.21.40.17/30
 no shutdown
exit
interface eth3
 ip address 172.29.40.1/24
 no shutdown
exit
router ospf
 ospf router-id 10.255.0.12
 network 172.21.40.4/30 area 0
 network 172.21.40.12/30 area 0
 network 172.21.40.16/30 area 0
 network 172.29.40.0/24 area 0
 passive-interface eth3
exit
end
write memory
```

## R-CU-OSPF-PORTAL (OSPF зона)
```
configure terminal
interface eth0
 ip address 172.21.40.10/30
 no shutdown
exit
interface eth1
 ip address 172.21.40.14/30
 no shutdown
exit
interface eth2
 ip address 172.30.40.1/24
 no shutdown
exit
router ospf
 ospf router-id 10.255.0.13
 network 172.21.40.8/30 area 0
 network 172.21.40.12/30 area 0
 network 172.30.40.0/24 area 0
 passive-interface eth2
exit
end
write memory
```

## R-CU-OSPF-SECURITY (OSPF зона)
```
configure terminal
interface eth0
 ip address 172.21.40.18/30
 no shutdown
exit
interface eth1
 ip address 172.31.40.1/24
 no shutdown
exit
router ospf
 ospf router-id 10.255.0.14
 network 172.21.40.16/30 area 0
 network 172.31.40.0/24 area 0
 passive-interface eth1
exit
end
write memory
```

## ПК
```
# PC-CU-EDU
ip 172.24.40.10/24 172.24.40.1
save
# PC-CU-ADMIN
ip 172.25.40.10/24 172.25.40.1
save
# PC-CU-EXAMS
ip 172.26.40.10/24 172.26.40.1
save
# PC-CU-ADMISSIONS
ip 172.27.40.10/24 172.27.40.1
save
# PC-CU-IT
ip 172.28.40.10/24 172.28.40.1
save
# PC-CU-LIB
ip 172.29.40.10/24 172.29.40.1
save
# PC-CU-PORTAL
ip 172.30.40.10/24 172.30.40.1
save
# PC-CU-SECURITY
ip 172.31.40.10/24 172.31.40.1
save
```

## Важно: rp_filter

Из-за равных по стоимости маршрутов в RIP-зоне трафик может идти туда и обратно разными путями. Если на интерфейсах включён строгий `rp_filter`, такие пакеты отбрасываются (например, PC-CU-LIB → PC-CU-EXAMS). На каждом маршрутизаторе выполните (в проекте `RIP-OSPF-solved.gns3project` это уже встроено в start_command):

```
for f in /proc/sys/net/ipv4/conf/*/rp_filter; do echo 0 > $f; done
```

Сходимость занимает 1–2 минуты: маршруты в OSPF-зоне до RIP-сетей появляются после redistribution.

## Проверка
```
# R-CU-RIP-EDU
show ip route
show ip rip

# R-CU-OSPF-CORE
show ip route
show ip ospf neighbor

# PC-CU-EDU
ping 172.28.40.10
trace 172.28.40.10

# PC-CU-SECURITY
ping 172.25.40.10
trace 172.25.40.10
```
