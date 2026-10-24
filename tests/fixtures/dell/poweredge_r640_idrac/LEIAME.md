Saídas reais do iDRAC9 do R640 de desenvolvimento (01/10/2026), só comandos de leitura pelo IPMI local:
`ipmitool user list 1`, `ipmitool lan print 1` e `ipmitool delloem lan get` (este último falha neste iDRAC:
"Error in getting nic selection (Unknown (0x80))", rc 1). IP e MAC trocados por endereços de documentação
(192.0.2.0/24, 00:00:5e:00:53:01); o nome do usuário 2 é o padrão de fábrica do iDRAC (`root`).
Ficam fora da fixture principal (`poweredge_r640`) para não mudar a detecção do Redfish no replay.

`ipmitool delloem lan get active` (02/10/2026, mesmo R640, só leitura): `dedicated` — o iDRAC estava em Dedicated com o
cabo na porta dedicada (link). Pela fonte do ipmitool (`ipmi_lan_get_active_nic`), sem link na porta escolhida a saída é
`None`; em modo compartilhado com link, `LOM1` a `LOM4`.
