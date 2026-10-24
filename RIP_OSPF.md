# Настройка PC-CU-EDU ↔ PC-CU-IT

## Включить демоны RIP & OSPF

```
sed -i 's/ospfd=no/ospfd=yes/' /etc/frr/daemons
sed -i 's/ripd=no/ripd=yes/' /etc/frr/daemons
kill $(pidof watchfrr)
/usr/lib/frr/docker-start &
```

**RIP (Routing Information Protocol)** – протокол дистанционно-векторной маршрутизации. Он выбирает маршрут по количеству хопов (переходов между маршрутизаторами).  
Работа *RIP*:  
Каждые 30 секунд отправляет полную таблицу маршрутизации соседям. Использует метрику "количество переходов", максимум 15. При получении маршрута увеличивает метрику на 1.  

**OSPF (Open Shortest Path First)** – протокол маршрутизации по состоянию каналов. Он строит полную карту топологии сети и рассчитывает кратчайшие пути алгоритмом Дейкстры.  
Работа *OSPF*:
Маршрутизаторы отправляют Hello-пакеты каждые 10 секунд. При получении Hello устанавливается соседство. Обмениваются *LSA* (Link State Advertisements) — информацией о топологии.
Каждый маршрутизатор строит полную карту сети. Вычисляет SPF алгоритмом Дейкстры.  

**Redistribution** – обмен маршрутами между разными протоколами маршрутизации.  

## R-CU-RIP-EDU (маршрутизатор RIP зоны)
### Настройка IP-адресов
```
configure terminal
interface eth0
 ip address 172.20.84.2/30
 no shutdown
exit
interface eth1
 ip address 172.20.84.9/30
 no shutdown
exit
interface eth2
 ip address 172.24.84.1/24
 no shutdown
exit
end
write memory
```

### Настройка RIP
```
configure terminal
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
Настройка *passive-interface* отключает *RIP*-обновлений на интерфейсе *eth2*. Т.к. конечные устройства не понимают *RIP*, настройка снижает сетевой трафик и повышает безопасность.  

## R-CU-RIP-CORE (маршрутизатор RIP зоны)
### Настройка IP-адресов
```
configure terminal
interface eth0
 ip address 172.20.84.1/30
 no shutdown
exit
interface eth1
 ip address 172.20.84.5/30
 no shutdown
exit
interface eth2
 ip address 172.22.84.1/30
 no shutdown
exit
end
write memory
```

### Настройка RIP с redistribution
```
configure terminal
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
*R-CU-RIP-CORE* является точкой взаимодействия *RIP* и *OSPF* зон. Он получает маршруты *OSPF* и передает их в *RIP* зону. Обратный процесс настраивается на *R-CU-OSPF-CORE*.  

## R-CU-OSPF-CORE (маршрутизатор OSPF зоны)
### Настройка IP-адресов
```
configure terminal
interface eth0
 ip address 172.21.84.1/30
 no shutdown
exit
interface eth1
 ip address 172.21.84.5/30
 no shutdown
exit
interface eth2
 ip address 172.22.84.2/30
 no shutdown
exit
end
write memory
```

### Настройка OSPF и RIP c redistribution
```
configure terminal
router ospf
 ospf router-id 10.255.0.10
 network 172.21.84.0/30 area 0
 network 172.21.84.4/30 area 0
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

На *R-CU-OSPF-CORE* настроены оба направления *redistribution*, потому что он является **ASBR** (Autonomous System Boundary Router) — граничным маршрутизатором между автономными системами.  

## R-CU-OSPF-IT (маршрутизатор OSPF зоны)
### Настройка IP-адресов
```
configure terminal
interface eth0
 ip address 172.21.84.2/30
 no shutdown
exit
interface eth1
 ip address 172.21.84.9/30
 no shutdown
exit
interface eth2
 ip address 172.28.84.1/24
 no shutdown
exit
end
write memory
```

### Настройка OSPF
```
configure terminal
router ospf
 ospf router-id 10.255.0.11
 network 172.21.84.0/30 area 0
 network 172.21.84.8/30 area 0
 network 172.28.84.0/24 area 0
 passive-interface eth2
exit
end
write memory
```

## PC-CU-EDU (устройство в RIP зоне)
### Настройка IP-адреса
```
ip 172.24.84.10/24 172.24.84.1
save
```

## PC-CU-IT (устройство в OSPF зоне)
### Настройка IP-адреса
```
ip 172.28.84.10/24 172.28.84.1
save
```

## Проверка
## R-CU-RIP-EDU
```
show ip route
show ip rip
```

## R-CU-OSPF-CORE
```
show ip route
show ip ospf neighbor
```

## PC-CU-EDU
```
ping 172.28.84.10
trace 172.28.84.10
```

