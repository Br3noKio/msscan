# Compatibilidade

Cada recurso escolhe o método pelo que o servidor **tem** (Redfish, IPMI, ferramentas), não só pelo modelo. Nenhuma
geração é "compatível" ou "incompatível" por inteiro.

## Fabricantes

| Fabricante | Diagnóstico | Atualização de firmware |
|---|---|---|
| Dell PowerEdge | Completo | Sim |
| HPE (iLO), Lenovo (XCC) | Cadeia genérica (Redfish, IPMI, sistema operacional) | Não (só pacote manual pelo navegador) |
| Outros (Supermicro, Cisco, Fujitsu...) | Cadeia genérica | Não |

## Gerações Dell

| Geração | iDRAC | Redfish | Conexão | Situação do firmware na Dell |
|---|---|---|---|---|
| 11ª (R610, R710…) | iDRAC6 | Não | IPMI e racadm local | Fim da manutenção (12/2018) |
| 12ª (R620, R720…) | iDRAC7 | A partir da 2.40.40.40 | Modos de criptografia de compatibilidade | Fim da manutenção (02/2020) |
| 13ª (R630, R730…) | iDRAC8 | A partir da 2.40.40.40 | Idem (a 2.30 usa DH de 1024 bits) | Fim da manutenção (02/2024) |
| 14ª (R640, R740…) | iDRAC9 | Sim | TLS 1.2/1.3 | Só correções de segurança |
| 15ª e 16ª | iDRAC9 | Sim | TLS 1.2/1.3 | Em manutenção |
| 17ª | iDRAC10 | Sim | TLS 1.2/1.3 | Em manutenção |

Conexão com o iDRAC, nesta ordem: Redfish pela rede (configuração moderna → cifras sem DHE → nível de segurança do
OpenSSL reduzido → TLS 1.0/1.1, só para o iDRAC, nunca para a internet), racadm local, IPMI local.

## Validado em hardware real

| Servidor | O que foi validado |
|---|---|
| R640 (14ª, iDRAC9) | Diagnóstico completo, credenciais, catálogo, IA, boot (Rufus ISO/DD, BIOS, UEFI, Secure Boot, mídia virtual), **ciclo completo de atualização: placas de rede, iDRAC e BIOS** |
| R620 (12ª, iDRAC7) | Boot, internet, diagnóstico, credenciais, RAID pelo Redfish e pelo BMC, atualização do iDRAC (1.40 → 2.65.65.65), usuário temporário criado e desfeito |
| T620 (12ª), R530 e R430 (13ª, iDRAC8) | Boot, interface, internet e diagnóstico; racadm local no R530 |

O restante foi testado com coletas reais gravadas, simuladores e mockups, mas não em hardware.

## Limitações conhecidas

- **RAID de hardware pelas ferramentas do fabricante** (perccli/storcli, ssacli, arcconf): as saídas são coletadas, mas
  ainda não interpretadas, por falta de coletas reais. Sem credenciais, o RAID vem dos sensores de baia e bateria do BMC;
  sem problema indicado, aparece como não verificado. RAID por software (`mdadm`) funciona.
- **Atualização em iDRAC7/8:** BIOS, placas de rede e exportação de configuração ainda sem validação física.
- **Repositório pelo iDRAC:** no R640 o iDRAC não buscou o repositório (RED110); o MSscan troca sozinho para o envio
  direto, que é o padrão até 2 componentes. Exige licença Enterprise ou Datacenter; o iDRAC8 não faz pelo racadm local.
- **Mapa e 3D exatos** só no R640; demais modelos com "posições aproximadas". Regras de montagem só do R640.
- **Pendrive GPT** não inicia em servidor configurado em BIOS legado (use MBR). Mídia virtual do iDRAC e gravação DD sem
  partição `MSSCAN` perdem os dados ao desligar.
- O registro de eventos não traz o serial da peça: troca de peça não é detectável só pelo log.
- A IA depende de internet e de chave do técnico; planos gratuitos têm limites.
- No modo texto, um bloqueio por saúde do hardware não pode ser assumido (só pela interface gráfica).

## Ampliar a cobertura

O MSscan só interpreta formatos que já viu em equipamento real. Coletas mais úteis hoje: controladoras PERC
(perccli/storcli/racadm), servidores HPE e Lenovo, e Dell da 13ª geração. Como coletar em
[Desenvolvimento](desenvolvimento.md#coletas).
