# Histórico de versões

Formato inspirado no [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/). Datas no formato AAAA-MM-DD.

## 1.0.13 — 2026-10-21

- **Pacotes Linux (`.BIN`) da Dell sem iDRAC acessível:** além do catálogo do Lifecycle Controller (só `.EXE`), o MSscan
  baixa o catálogo "Update Catalog for Enterprise Servers" da Dell (`downloads.dell.com/catalog/Catalog.gz`, o do Dell
  System Update), que traz o `.BIN` de cada componente com o mesmo release ID e a mesma versão do `.EXE` (26.10.15: 838
  `.BIN`, 834 com SHA-256; da 14ª geração em diante). Sem iDRAC acessível, a instalação usa esse `.BIN` (caminho f:
  SHA-256 conferido, `-c` e depois `-q`) em vez de parar na Atualização manual; o aviso da 1.0.12 só aparece para o que
  continua sem `.BIN` (13ª geração para trás). BIOS e iDRAC lidos sem Redfish passam a casar pelo release ID.

## 1.0.12 — 2026-10-20

- **Atualização sem iDRAC acessível:** quando o iDRAC não é acessível daqui e o catálogo da Dell só traz o pacote de
  Windows (`.EXE`) de um componente (nenhum `.BIN` oficial da mesma versão), a tela de Atualizações avisa antes do
  ATUALIZAR quais componentes estão nessa situação, com a página de drivers do modelo e o passo a passo da Atualização
  manual pelo pacote Linux (`.BIN`); sem `.BIN` publicado, o Lifecycle Controller (F10). O texto final da instalação
  ("o que faltou") também traz a página de drivers. Em todos os recortes do catálogo nos testes (12ª a 14ª geração),
  os pacotes são só `.EXE`.

## 1.0.11 — 2026-10-20

- **iDRAC6 (11ª geração):** com o iDRAC respondendo pela rede sem Redfish, a mensagem mandava atualizar o firmware do iDRAC
  para ter Redfish; o iDRAC6 não tem Redfish em nenhuma versão. Agora diz isso e segue pelo acesso interno.
- **Testes:** a frota simulada da 11ª, 12ª e 13ª geração parte da coleta real do R620 (iDRAC7) em vez da do R640 (iDRAC9),
  com o Redfish ausente onde a geração/firmware não o tem (404, como no R620 real com a 1.40). Foi assim que o item acima
  apareceu. Layout real da estrutura OEM 208 do R620 nos testes.

## 1.0.10 — 2026-10-20

Compatibilidade das mecânicas por geração (`docs/mecanicas.md`), a partir da coleta real do R620 (12ª, iDRAC7 2.65.65.65,
PERC H710) e dos logs da bancada de 19/10.

- **RAID sem credenciais e sem perccli:** o estado atual das baias de disco e da bateria da controladora, lido do BMC
  (`ipmitool sensor`), entra na cadeia do RAID. No R620, "Drive 0: In Failed Array" dá Falha, o mesmo veredito que o Redfish
  do iDRAC dava com credenciais ("Virtual Disk 0: Critical"). Só a presença dos discos não vira "Normal": continua
  "não verificado".
- **Log do BMC:** bateria "Low" (da PERC ou do CMOS) passa a contar como problema. Pelo Redfish, o iDRAC7 entrega só os 50
  eventos mais recentes; os mais antigos agora vêm do IPMI e o relatório diz que o Redfish foi parcial.
- **Memória na 12ª geração:** os slots "DIMM_A1" eram lidos como "_A1", e um evento de memória num pente instalado aparecia
  como "peça não está mais instalada". Bateria da PERC (ROMB) localizada na controladora.
- **Exportar a configuração antes de instalar:** o formato vem do que o iDRAC anuncia; sem anúncio, JSON e, se o iDRAC
  recusar (400, como no iDRAC7), XML. A mensagem do iDRAC vai para o relatório.
- **Coleta:** "Coletar dados" passa a incluir o racadm já preparado na sessão (as saídas da RAID pelo racadm nunca eram
  coletadas). O anonimizador troca o IP do iDRAC do `ipmitool lan print`; o log técnico não esconde mais versões de firmware.
- **Resumo:** a geração e o iDRAC aparecem no "Servidor detectado" (ex.: "PowerEdge R620 (12ª geração, iDRAC7)").
- **Testes:** todas as mecânicas sobre todas as coletas reais (`tests/test_coletas_reais.py`), com o resultado conferido na
  bancada; recorte sem edição do catálogo 22.12.30 com os pacotes do R620.

## 1.0.9 — 2026-10-19

Teste no R620 com a 1.0.8: o iDRAC foi atualizado (1.40.40 → 2.65.65.65, pelo racadm local com a imagem do .EXE). A BIOS
(1.6.0 → 2.9.0) dizia "aceito pelo iDRAC, possivelmente agendado", mas nada ficava na fila e o reinício não aplicava.

- **Envio do pacote ao iDRAC7/8 pelo Redfish:** o envio era cortado em 15 s ("The write operation timed out"): a biblioteca
  HTTP usa o tempo de conexão também para mandar o arquivo, e o iDRAC7 para de receber por mais que isso. Agora o envio tem o
  mesmo tempo do resto da operação (15 min). Vale também para o envio direto do iDRAC9.
- **racadm sem tarefa na fila:** com a fila do iDRAC lida e sem nenhuma tarefa nova em 2 min, o item fica como **falhou**
  ("NADA foi agendado e reiniciar não aplica este firmware"), e não mais como "possivelmente agendado". Só quando a fila não
  pode ser lida o resultado continua incerto.

## 1.0.8 — 2026-10-16

Teste no R620 (iDRAC7 1.40.40) com a 1.0.7: atualização do iDRAC falhou pelos dois caminhos.

- **Pacote Linux (.BIN) da Dell:** saía com código 127 sem rodar. Sem terminal, o pacote tentava se reabrir num `xterm`,
  que a ISO não tem. Agora roda direto (checagem `-c` e instalação `-q`), em todos os servidores e componentes.
- **iDRAC7/8 pelo racadm local, automático:** se o iDRAC recusar o .EXE (RAC989, "arquivo inválido", como no R620 com iDRAC
  1.40.40), o MSscan tira a imagem do iDRAC (`firmimg.d7`) de dentro do mesmo .EXE já conferido pelo catálogo e manda essa.
  É a mesma imagem que o .BIN da Dell grava (SHA-256 idêntico). Sem baixar nada à mão.

## 1.0.7 — 2026-10-16

Teste no R430 (iDRAC8 2.86.86.86) com a 1.0.6: "Informar credenciais do iDRAC" dava HTTP 405 com usuário e senha certos.

- **Credenciais do iDRAC7/iDRAC8:** o login vai na coleção de sessões que o próprio iDRAC anuncia (`/redfish/v1/Sessions`);
  antes ia sempre em `/redfish/v1/SessionService/Sessions`, que o iDRAC7/8 recusa (405). Se o BMC recusar o endereço (404/405),
  tenta o outro sozinho. Vale para todos os BMCs (iDRAC7/8/9, iLO, XCC).
- **Tentativas:** erro que não é de senha (405, 404, sem resposta) não gasta uma das 3 tentativas; só usuário ou senha recusados
  contam (risco de bloqueio do endereço).

## 1.0.6 — 2026-10-16

Correções dos relatórios da bancada de 16/10/2026 (R620, T620, R530, R430 com a 1.0.5).

- **Atualização do iDRAC pelo racadm local:** o pacote vai por um caminho curto (o racadm recusava caminho com 64 caracteres ou
  mais: RAC923). Se nenhum caminho funcionar, "O que faltou" mostra a recusa real do racadm.
- **Controladora RAID sem driver:** RAID e Discos ficam "não verificado" com o motivo (antes: "não aplicável"). Servidor sem discos
  diz isso em vez de "sem dados".
- **Discos atrás da PERC:** identificados pelo ID na controladora, modelo e serial; disco listado que não responde vem explicado.
- **Processadores:** CPU "Populated, Idle" no SMBIOS, mas em uso pelo sistema, deixa de ser alerta.
- **Placas de rede:** firmware Broadcom (FFV) e Intel comparados pela versão da família (antes "não foi possível determinar").
- **Relatório:** caixa "Acesso ao iDRAC" (modo da porta, conexão, IP, racadm); "Onde está o problema" com a ocorrência mais recente
  e sem repetir o mesmo evento.
- **Coleta:** leituras do racadm (armazenamento e inventário) e o `dmesg`.

## 1.0.5 — 2026-10-16

Teste no R620 (16/10/2026): a interface gráfica não abriu e o servidor ficou no modo texto.

- **Sem modo texto no boot:** o MSscan vai sempre para a interface. Até 90 s por tentativa (antes, 30 s e uma tentativa só);
  se não abrir, tenta de novo com outro driver de vídeo (autodetecção, `fbdev`, `vesa`) e recomeça; se cair depois de aberta,
  abre de novo. O parâmetro `msscan.texto=1` não existe mais.
- **Logs das tentativas no pendrive:** Xorg, navegador e vídeo de cada tentativa que falhou vão junto do relatório
  (`interface_falhou/`) ou, se nenhum vídeo abrir, para `MSSCAN/logs/interface_<data>/`.

## 1.0.4 — 2026-10-16

- **Coleta pela tela:** aba Relatórios → "Coletar dados deste servidor" (o mesmo `--coletar`, somente leitura, sem terminal).
  O pacote é montado na RAM e gravado conferido em `MSSCAN/coletas/`.
- **Interface:** avisos sem ponto duplicado ("após o diagnóstico..").

## 1.0.3 — 2026-10-15

Correções do teste no R530 e no R620 (15/10/2026) e catálogos para servidores antigos.

- **Catálogos antigos da Dell:** a Dell tirou a 9ª–13ª geração dos catálogos novos. Modelo sem BIOS no catálogo atual usa o
  último catálogo oficial que ainda o inclui (13ª: 26.06.01; 12ª: 22.12.30), com SHA-256 conferido; a tela diz que é o último
  catálogo da Dell para o modelo. 11ª geração continua na Atualização manual.
- **iDRAC8 abaixo de 2.40.40.40:** primeiro o 2.40.40.40 (pré-requisito publicado pela Dell), depois a última versão.
- **Catálogo atual:** pacotes da seção `<Prerequisites>` passam a ser lidos; nomes com a marca do catálogo ("Enterprise Servers
  R530") e com barra ("R320/NX400") aceitos, com o System ID exato.
- **Acesso ao iDRAC:** HTTP 404 no Redfish (iDRAC7 sem Redfish) não é mais "inacessível pela rede".
- **Atualizações:** "fora de suporte" sem repetição; "Próxima etapa" coerente depois da verificação.
- **Navegador integrado:** liberados `www.google.com`, `www.gstatic.com` e `recaptcha.net` (verificação "não sou um robô").

## 1.0.2 — 2026-10-14

Lapidação da interface (auditoria de UI/UX).

Reorganização visual, sem função nova e sem mudança em comandos, rotas da API, regras de firmware ou proteções.

- **Design system consolidado:** escalas de raio e de tipografia como variáveis (`--ms-raio-*`, `--ms-fs-*`) no início do
  `app.css`; as regras duplicadas da interface 1.0.0 (body, `.cartao`, `.faixa-seguranca`, `.tela`, `.faixa`) ficaram uma só.
- **Situação nunca só pela cor:** as pílulas da barra (temperatura, fontes, iDRAC, IA) ganham o ícone de atenção ou de
  falha; "Atualização disponível" passa a ser informação (azul, ícone de download), e não "Atenção".
- **Servidor sempre identificado:** abaixo de 1400 px o modelo continua na barra (sai o relógio).
- **Correções:** barras dos sensores ao vivo sumiam em 800x600; cartões do Terminal sem margem interna; data do catálogo
  em inglês ("August 11, 2026") na tabela de firmware, agora 11/08/2026; resumo repetido em Atualizações; "Prioridade
  media" sem acento; prefixo "origem:" repetido na coluna Origem; autor das mensagens da IA sem parênteses aninhados.
- **Lista de componentes por gravidade** na tela Diagnóstico: Falha, Atenção, Não verificado, Normal, Não aplicável (empate na
  ordem do diagnóstico). Durante a coleta as linhas não mudam de lugar; relatórios e JSON mantêm a ordem do núcleo.
- **Veredito mais claro** (tela, notificação, modo texto e relatórios): cobertura e resultado separados, sem "(s)" —
  "Servidor diagnosticado com sucesso (todos os componentes verificados). Resultado: 4 alertas e nenhuma falha." Os termos
  "diagnosticado com sucesso"/"parcialmente diagnosticado" (CLAUDE.md 9) continuam e falam só da cobertura.
- **Atalho para a tabela de firmware:** com a verificação feita, o cartão "Verificar atualizações de firmware?" recolhe
  sozinho (resumo de uma linha) e a tabela sobe para o topo; expandir de novo vale para a sessão.
- **Telas de 768 px:** doca mais baixa, mais altura útil para o painel central.
- **Análise por IA:** a nota "Interpretação gerada por IA" em caixa informativa, separando hipóteses das medições.

## 1.0.1 — 2026-10-14

Internet e iDRAC em todos os Dell da 11ª à 17ª geração (caso real: PowerEdge R530 com iDRAC8 2.30 e o relógio do BIOS 6
meses atrasado).

- **Relógio antes de qualquer HTTPS:** NTP (o do DHCP primeiro), cabeçalho `Date` de sites HTTP (dois concordando), hora
  do iDRAC e, por último, o técnico. Só o relógio do sistema; o do BIOS nunca (a ISO não tem mais o `systemd-timesyncd`).
  Diferença de um dia ou mais: aviso para corrigir a data no BIOS.
- **Sem internet o MSscan continua** (diagnóstico, iDRAC, relatórios, pacotes no pendrive), com a faixa "Sem internet" e
  a causa em linguagem simples (relógio, cabo, driver, DHCP, DNS, proxy, firewall); reconecta sozinho e, quando a
  internet chega, libera IA e atualizações e baixa o iDRAC Tools.
- **Consertos automáticos da rede:** DNS público quando o da rede falha; com várias placas, a que alcança a internet vira
  a rota padrão; IPv4 preferido; DHCP com espera de 90 s; firmware de mais placas de rede na ISO.
- **Interface com o relógio acertado:** a espera de 30 s pela interface gráfica passa a contar pelo tempo desde o boot; antes,
  o acerto de meses no relógio fazia a ISO cair no modo texto (achado no QEMU). O aviso para corrigir a data no BIOS
  aparece no Início.
- **Mais servidores Dell:** System ID da 11ª à 13ª geração (antes nenhuma atualização era oferecida no R530); volume da
  PERC não conta mais como disco não verificado; PERC 12 (`mpi3mr`, `perccli2`); modelos do catálogo escritos com espaço
  ou maiúscula ("R650 XS"); geração correta de C4130/C4140/XE7100/XR11/XR12; IA que recusa por tamanho recebe os dados
  enxutos.
- **Proxy:** o `config/proxy.conf` agora vale também na interface gráfica e é relido em "Tentar novamente" (pode ser
  criado com o MSscan aberto); rede sem DNS externo sugere o proxy; proxy que pede senha é dito com essas palavras.
- **iDRAC antigos:** modos de criptografia só para o iDRAC (sem DHE, segurança reduzida, TLS 1.0), guardados por iDRAC;
  mensagens por camada (nunca "não acessível" com racadm ou IPMI respondendo); recomendação do firmware da geração.
  Matriz em `docs/compatibilidade.md`.

## 1.0.0 — 2026-10-13

Primeira versão completa para servidores Dell: diagnóstico, análise por IA, conversa, atualização de firmware (primeiro
ciclo completo validado no PowerEdge R640: placas de rede, iDRAC e BIOS) e ações corretivas.

- **Terminal e consoles de texto desligados por padrão:** a aba Terminal pede "Ativar terminal" (ou o parâmetro de boot
  `msscan.terminal=1`); os consoles tty2 a tty6 só aparecem com `msscan.terminal=1`. O acesso SSH continua desligado por
  padrão.
- **Revisão final independente:** firmware da controladora RAID ou de discos só com o RAID conferido agora e sem
  problema (não verificado ou releitura que falhou passa a ser bloqueio fixo, que "ASSUMO OS RISCOS" não libera); fim do
  modo texto sem shell de root, a não ser com `msscan.terminal=1`; Ctrl+C no tty1 não encerra mais o MSscan durante
  uma gravação.
- **Licenças dos componentes:** `docs/licencas.md` e a lista dos pacotes da ISO (`docs/licencas-iso.md`, gerada por
  `iso/licencas.py`).

- **Volta do iDRAC depois do firmware dele** (teste de 09/10/2026 no R640): conferida sem a sessão antiga (ping,
  `/redfish/v1` sem login, sessão nova com as credenciais em memória, versão pelo FirmwareInventory do Redfish), espera de
  até 30 min com o que está sendo conferido; passou disso → plano parado, e "Continuar de onde parou" (na mesma sessão ou
  depois de reiniciar) confere o iDRAC e retoma a partir da BIOS sem reenviar.
- **Liberação** da gravação real para qualquer servidor Dell (fabricante conferido), depois do primeiro ciclo completo no
  R640 (placas de rede, iDRAC e BIOS).
- **Escrita corporativa no modo texto, nos relatórios e no motor de atualização:** textos de `main.py`, de
  `msscan/relatorio/` e de `automatica.py`, `aplicacao.py` e `verificacao.py` em `msscan/textos/pt_BR.json`, com o
  glossário (controlador de gerenciamento, fila de tarefas, registro de eventos, acesso interno ao iDRAC, modo de
  demonstração, verificação de integridade). Relatórios com datas no padrão brasileiro, rodapé "Gerado pelo MSscan
  <versão>" também no TXT e a seção **Riscos assumidos pelo técnico** (TXT e HTML).
- **Nova interface em mosaico:** visão do servidor, sensores ao vivo, painel rápido, doca, gavetas em telas estreitas,
  papel de parede próprio e `msscan-fetch` no terminal.
- **Documentação:** `README.md`, `docs/` com uma página por parte do sistema (diagramas Mermaid), "Limitações conhecidas",
  "Testes e qualidade" e `MSscan-Documentacao.pdf` (`docs/pdf/gerar_pdf.py`).

## 0.18.1 — 2026-10-06

Revisão independente dos caminhos de escrita (subagente sem histórico, só leitura) e liberação restrita da gravação real.

- **Nunca grava duas vezes por dúvida** (crítico C2): no envio direto (MultipartUpload) e no SimpleUpdate do iDRAC7/8, só
  uma recusa explícita do iDRAC (4xx, sem "já existe job"/RED014) passa para o racadm ou o `.BIN`. Timeout, conexão
  cortada, 5xx ou 2xx sem `Location` param a instalação ("confira o Job Queue"), sem reenviar.
- **"Não consegui ler o iDRAC" não é mais "nada foi feito"** (crítico C1): cancelamento de job só conta com o iDRAC
  confirmando (404 ou Cancelled/Killed); Job Queue ilegível = desconhecido; depois do pedido de instalar pelo repositório,
  qualquer dúvida para e não troca de caminho. O job de instalar não é cancelado se já criou jobs de componente (M1) e não
  vira mudança temporária a cancelar. No boot seguinte, job sem resposta fica registrado para a próxima sessão.
- **Pass-through nunca desfeito às cegas** (M4 e pedido do usuário): antes de restaurar, o MSscan lê o estado real; só
  restaura se estiver exatamente como ele deixou (ligado, USB). Diferente: "alterado fora do MSscan (estado anterior
  desconhecido)", nada é mexido.
- **Liberação restrita** (pedido do usuário): gravação real só neste R640 (SHA-256 do service tag, nunca o tag em texto) e
  só placas de rede (categoria `Network`). BIOS, iDRAC e o resto ficam bloqueados; o "Plano de atualização" mostra
  "Liberação nesta fase" (liberado × bloqueado). Conferido de novo no motor antes de qualquer mudança no iDRAC.

## 0.18.0 — 2026-10-06

Correções do teste da v0.17 no R640 (repositório parado 13 min com a interface do pass-through sumida; `CLAUDE.md` 22).

- **Caminho do repositório conferido antes da tarefa**: interface com IP na rede do iDRAC e ping no iDRAC; sem isso, a
  tarefa não é criada. **Rede local primeiro** (iDRAC na mesma rede do servidor: repositório no IP do servidor, porta só
  para o IP do iDRAC), depois o pass-through, depois o online da Dell (só com DNS no iDRAC). **Até 2 componentes: envio
  direto** (MultipartUpload) por padrão.
- **Troca de caminho**: nenhum pedido ao repositório em 2 min, interface sumida ou 5 min sem progresso real → o job é
  **cancelado no iDRAC** (`DeleteJobQueue` de um job só) e o MSscan segue; o aviso fica na tela. Cada pedido recebido pelo
  repositório é registrado (horário, IP, arquivo, resultado).
- **Restauração**: jobs do repositório criados pelo MSscan entram nas mudanças temporárias (cancelados no fim ou no próximo
  boot, também pelo `racadm jobqueue delete`); job travado não adia mais o pass-through; o desfazer espera o racadm ficar
  pronto; o que foi desfeito aparece na caixa "Acesso ao iDRAC"; pass-through ligado sem registro → aviso.
- **Log técnico**: passo "tarefa criada no iDRAC: job X", gravação a cada minuto durante esperas longas, registro das
  mudanças temporárias.
- **Acesso SSH/SFTP** (desligado por padrão): botão na aba Terminal ou `msscan.ssh=1`; usuário `tecnico` com senha
  aleatória da sessão (nunca gravada) ou chaves de `MSSCAN/config/ssh_authorized_keys`; porta 22 só para a rede local e só
  enquanto ativo; faixa "Acesso SSH ativo" com "Desativar"; relatório com horário e IP de quem conectou.
- ISO: `openssh-server` (serviço desligado, sem chaves de servidor embutidas), `sshd_config.d/10-msscan.conf`, firewall com
  `repositorio_lan` e `ssh_redes`.

## 0.17.0 — 2026-10-06

Correções do T1 com a ISO 0.16.1 (o MSscan ficou parado em "Pacote conferido" e o servidor foi desligado no meio).

- **Progresso visível**: barra por componente e geral, etapas com o tempo de cada uma (baixando %, conferido, enviando
  ao iDRAC % dos bytes, iDRAC validando, agendado, instalando % do job, concluído/falhou com o motivo), tempo restante,
  "última resposta do iDRAC há X s" e, sem mudança por 3 min, "sem progresso há X min" com o que o MSscan espera e o que
  conferir. iDRAC que não busca o repositório no pendrive em 2 min: aviso na hora e segue pelo envio direto.
- **Faixa fixa** durante toda a instalação, em qualquer aba: verde "Seguro desligar agora" / vermelha "NÃO desligue:
  gravando firmware (componente)". Botão **"Parar depois do componente atual"** e, na próxima vez, **"Continuar de onde
  parou"** (nunca continua sozinho).
- **Ordem e reinícios**: firmware do iDRAC sozinho e primeiro, esperando o iDRAC voltar pelo Redfish; os itens que
  dependem de reinício ficam agendados juntos para **um único** reinício. Plano com tempo e reinício por componente.
- **Jobs do iDRAC**: fila lida ao iniciar (tela, relatório e log), códigos traduzidos (ex.: RED115); job ativo ou
  agendado de antes bloqueia instalação nova (sem duplicar tarefas); RED115/reinício do iDRAC → espera e refaz **uma
  vez**; pass-through não é ligado nem desligado com job em andamento (fica para o próximo boot); a retomada espera os
  jobs terminarem. Boot pelo pendrive por `UefiTarget` quando o iDRAC não anuncia `Usb`, senão aviso do F11 antes.
- **Acesso ao iDRAC**: a caixa separa cabo desconectado × DHCP sem resposta (com link). Nova ação avulsa **"Colocar um IP
  fixo no iDRAC"** (`idrac.ip_fixo`, com `CORRIGIR`, IP livre testado por ping/ARP, antes × depois e Desfazer); a
  atualização automática continua sem mexer na rede do iDRAC.
- **Log técnico automático** no pendrive a cada mudança de etapa (`MSSCAN/logs/log_tecnico_*_auto.json`); o desfazer do
  boot seguinte tenta o racadm 3 vezes. Versão e origem do iDRAC Tools no relatório (`idrac_tools`).
- ISO: `iputils-ping` e `iputils-arping`.

## 0.16.1 — 2026-10-02

- **Download automático do "iDRAC Tools for Linux"** (11.4.1.0) do site oficial da Dell, em segundo plano, logo que há
  internet e não há racadm nem o pacote no pendrive: SHA-256 conferido com o publicado pela Dell (diferente → arquivo
  apagado e segue sem racadm), guardado em `MSSCAN/vendor/` (o pendrive fica pronto para clientes sem internet) e racadm
  preparado sozinho. Endereço e hash em `ferramentas_dell.yaml`, atualizável sem mudar o código.
- Atualização que precisa do racadm espera o download em andamento terminar.
- Caixa "Acesso ao iDRAC": "racadm pronto (versão X)", "baixando…" ou "não disponível (motivo)"; aviso dos termos da Dell
  ao baixar; versão e origem do iDRAC Tools no relatório.
- A ISO não leva mais `libcrypto++8t64` (o racadm do pacote real só precisa de `libargtable2`).

## 0.16.0 — 2026-10-02

Correções do primeiro teste real no R640 (log técnico da ISO 0.15.0: nenhum caminho conseguiu instalar a placa de rede).

- **Caixa "Acesso ao iDRAC"** no Início (e no modo texto): por onde o MSscan alcança o iDRAC e, se não alcança, o que
  fazer — porta dedicada sem cabo, modo compartilhado com a porta do servidor sem cabo (o MSscan nunca mexe nessa
  porta), IP do iDRAC em outra rede, iDRAC sem IP, "iDRAC encontrado, mas não respondeu" com o motivo provável. Botão
  **"Tentar de novo"** refaz a checagem sem reiniciar; quando o iDRAC passa a responder, as credenciais são pedidas.
- **racadm local a partir do pacote "iDRAC Tools for Linux"** da Dell em `MSSCAN/vendor/` (opção A): extraído e
  preparado sozinho na memória do sistema live, com os passos do pós-instalação oficial; usado para ler a rede e a versão
  completa do iDRAC, ligar o OS to iDRAC Pass-through temporário e instalar pelo `racadm update` (inclusive `.EXE`).
- Ordem automática: Redfish pela rede (reconsultado antes de desistir, para quando o cabo foi ligado depois da detecção)
  → racadm local e pass-through → explicação do que faltou. O caminho do usuário temporário agora sempre aparece no
  relatório, com o motivo quando não se aplica.
- **Placas de rede**: corrigido o cruzamento com o catálogo da Dell, que lia só o primeiro ID PCI de cada dispositivo (a
  X710 do R640 ficava "sem pacote"); funções do mesmo chip e portas que recebem o mesmo pacote viram um item só (o rNDC
  X710/I350 é instalado uma vez).
- **Versão completa do iDRAC** (ex.: 7.00.00.173) pelo racadm; sem ele, continua "não foi possível determinar" com o
  "7.00" do IPMI.
- **3D e 2D**: botão para alternar entre o modelo 3D e o mapa 2D, lembrando a escolha; modelo genérico para os PowerEdge
  R4xx, R5xx, R6xx, R7xx e T4xx a T6xx (gerações 12 a 16) sem posições conferidas no manual, sempre com o aviso
  "posições aproximadas".

## 0.15.0 — 2026-10-02

- **Visão geral do servidor** na aba Diagnóstico, ao fim de todo diagnóstico: indicador grande do estado geral
  (**Tudo OK** só com todos os componentes verificados e sem problemas; **Atenção** com alertas, eventos históricos, itens
  não verificados ou avisos de configuração; **Problema** com qualquer falha atual), com a contagem e um atalho para a
  lista, ao lado do modelo 3D. Muda na hora ao refazer o diagnóstico. Ícones SVG, sem depender de emoji.
- Modelo 3D (e mapa 2D) com a **cor do estado de cada peça**: verde OK, amarelo atenção, vermelho problema (pulsando =
  atual), cinza não verificado; clique mostra os detalhes da peça e o status do componente dela.
- Corrigido: com o primeiro quadro 3D lento, o recuo para o mapa 2D mostrava um erro de JavaScript no lugar do motivo.

## 0.14.0 — 2026-10-01

- **Atualização automática depois de um único `ATUALIZAR`**: tela única "Plano de atualização" (o que, ordem, tempo,
  reinícios, hardware conferido na hora e as mudanças TEMPORÁRIAS no iDRAC); depois, acesso ao iDRAC, instalação,
  reinícios pelo pendrive, retomada (continua sozinha os itens pendentes, relendo o hardware), restauração e relatório
  sem novas perguntas. Sem simulação na interface (`--simular` ficou só como opção interna de desenvolvimento).
- **Atualização por repositório pelo próprio iDRAC** (como o Dell Repository Manager): catálogo oficial filtrado e
  pacotes no pendrive, servidos por HTTP só na interface do OS to iDRAC Pass-through; `InstallFromRepository` /
  `GetRepoBasedUpdateList`, jobs acompanhados, boot único pelo pendrive antes do reinício. Alternativa: repositório
  online da Dell pelo iDRAC.
- **Caminhos alternativos em ordem**, cada tentativa no relatório: Redfish com as credenciais; administrador temporário
  criado pelo IPMI interno; pass-through temporário; `racadm update` local; pacote Linux `.BIN` (`-c`/`-q`); outro formato
  oficial do mesmo componente no catálogo; e, por fim, a explicação exata do que faltou.
- **Mudanças temporárias no iDRAC** registradas no pendrive antes de feitas e desfeitas sozinhas (fim, antes de cada
  reinício e no próximo boot se o MSscan cair). A rede do iDRAC nunca é alterada (ação removida).
- **Dados do cliente protegidos**: o executor recusa montar/ler partições dos discos do cliente, alterar volumes RAID e
  copiar arquivos do sistema operacional dele.
- **Modelo 3D do chassi** na localização física (R640): girar, zoom e mover com mouse e teclado, peça com problema atual
  em vermelho pulsando e histórico em amarelo, clique mostra o diagnóstico; sem WebGL ou lento, volta ao mapa 2D.
- **Exportar log técnico** (sem senhas, chaves nem dados do cliente) na Instalação e em Relatórios.
- Testes de ponta a ponta com simuladores: `ipmi_sim` (OpenIPMI), iDRAC9 falso por HTTPS, `racadm` e DUPs falsos.

## 0.13.0 — 2026-10-01

- Diagnóstico: eventos do log do BMC (SEL) separados em **atual** × **histórico**, cruzados com o hardware presente
  (peça retirada não bloqueia mais nada); só evidência atual e crítica bloqueia a atualização; evento antigo crítico pede
  "Estou ciente de que é um evento antigo".
- **Configuração do servidor** (processadores, pentes por processador/canal, fontes, discos) com as regras de montagem do
  R640 conferidas no manual da Dell.
- Atualizações: fluxo guiado em 6 etapas com "Próximo passo", download e conferência antes do aviso, versão de `.BIN` sem
  `package.xml` pelo catálogo ou pela checagem do próprio pacote, mensagens com a causa real, correção do "item
  desconhecido / 0 min".
- **Configurar o iDRAC pelo MSscan**: ver configuração, rede (DHCP/IP fixo), OS to iDRAC Pass-through e usuário
  administrador temporário (apagado ao terminar), com Desfazer.
- IA: Gemini Flash-Lite como principal, "Cancelar envio" e "Remover chave".
- Interface cabe na tela de 800x600 a 1920x1080 (o diálogo de instalação passava da tela com a escala automática).

## 0.12.0 — 2026-09-30

- IA mais rápida: streaming (Anthropic, Gemini, compatíveis com OpenAI), contexto compacto no chat, menos esperas.
- Navegador integrado: bloqueio imediato ("Site bloqueado pelo MSscan"), proxy sem esperas de IPv6, cache de DNS.
- Atualizações: pacote aplicável mais recente, pré-requisitos declarados pela Dell, três estados por componente
  (disponível / não é necessária / não foi possível determinar), pasta de downloads com apagar confirmado.
- Localização física: "Onde está o problema" (fonte, DIMM, cabo SAS, slot PCIe...) e mapa do chassi do R640.
- Interface: "Mostrar menos/mais", visual com painéis translúcidos, medição de fps e modo leve automático.
- Aba **Terminal** (xterm.js local, só na máquina) e botões **Desligar/Reiniciar** com desmontagem segura do pendrive.
- **Atualização real**: modo padrão real com "Simular sem gravar"; **tela de AVISO obrigatória** (ordem com BIOS por
  último, tempo e reinícios, hardware conferido na hora, rollback, "Li os avisos" + `ATUALIZAR`); progresso por
  componente; antes × depois.
- Revisão independente de segurança dos caminhos que gravam firmware; correções (gravação nunca interrompida pelo
  próprio MSscan, job possivelmente agendado tratado, hash reconferido antes de enviar).

## 0.11.1 — 2026-09-29

- Foco na Dell; aviso de modelo fora de suporte do fabricante; novas tentativas da IA em erros temporários.

## 0.11.0 — 2026-09-29

- Atualização manual: navegador integrado restrito aos sites oficiais, assistente com SHA-256 antes de qualquer
  execução, checagem `DUP -c`; Dell antigos (iDRAC7/8) pelo `.EXE` via Redfish.

## 0.10.1 — 2026-09-29

- Ajustes das ações corretivas (SEL exportado e conferido antes de limpar; NTP com desfazer) e da interface.

## 0.10.0 — 2026-09-28

- Ações corretivas de catálogo fechado: acertar o NTP do BMC e exportar/limpar o SEL, com prévia e `CORRIGIR`.

## 0.9.0 — 2026-09-28

- Aplicação de firmware pelo Redfish do iDRAC9 (envio multipart, tarefa, boot único pelo USB, retomada após o
  reinício), pré-checagens e confirmação `ATUALIZAR`.

## 0.8.x — 2026-09-25 a 2026-09-28

- 0.8.4: IA gratuitas × pagas, chave pelo celular (QR local), refazer diagnóstico, rede contínua, nome MSscan.
- 0.8.3: setas nas abas da interface.
- 0.8.2: vários provedores de IA com reserva e plano de solução.
- 0.8.1: resolução de vídeo automática (EDID) e zoom.
- 0.8.0: atualizações somente informativas (inventário × catálogo Dell).

## 0.7.0 — 2026-09-25

- Análise por IA opcional com sanitização e validação de evidências.

## 0.6.0 — 2026-09-25

- Redfish com credenciais digitadas na hora; banco de compatibilidade (R410, R640).

## 0.5.x — 2026-09-24 a 2026-09-25

- Interface gráfica local (quiosque); ISO universal (BIOS/UEFI/Secure Boot, qualquer forma de gravação).

## 0.1.0 a 0.4.0 — 2026-09-24

- Núcleo, detecção, fallback, parsers com fixtures reais, sensores, fontes, ventoinhas, CPU, memória, SMART, RAID,
  rede obrigatória, relatórios TXT/JSON/HTML e a primeira ISO.
