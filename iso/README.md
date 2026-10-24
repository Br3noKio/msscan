# MSscan — construção e teste da ISO

A ISO é um Debian 13 "trixie" live (amd64, boot BIOS legado **e** UEFI) que
inicia o MSscan sozinho no primeiro terminal (tty1), depois da rede.

> Build, gravação de pendrive e particionamento alteram discos. **Confira três vezes o dispositivo antes de `dd`, `fdisk` e `mkfs`.**

## 1. Construir a ISO

Num Debian 13 (máquina ou VM de build, com ~10 GB livres e internet):

```bash
sudo apt install live-build
sudo ./iso/build.sh
```

Resultado: `iso/msscan-<versão>-amd64.iso` e o `.sha256`. O log completo fica
em `iso/trabalho/build.log`. Para usar um espelho local:
`sudo ESPELHO=http://espelho.local/debian/ ./iso/build.sh`.

O que o build faz:

| Arquivo | Função |
|---|---|
| `iso/build.sh` | `lb config` + cópia de `iso/config/` + app em `/opt/msscan` + `lb build` |
| `iso/config/package-lists/msscan.list.chroot` | pacotes (ferramentas, rede, firmware, console) |
| `iso/config/includes.chroot/etc/systemd/system/msscan.service` | roda o MSscan no tty1 após `network-online.target` |
| `iso/config/includes.chroot/usr/local/sbin/msscan-iniciar` | executa `main.py` e, ao final, abre um terminal |
| `iso/config/includes.chroot/etc/modules-load.d/msscan-ipmi.conf` | carrega `ipmi_si` e `ipmi_devintf` no boot |
| `iso/config/hooks/normal/9000-msscan.hook.chroot` | habilita o serviço, mascara `getty@tty1`, NTP, fonte do console |
| `iso/config/hooks/normal/9010-verificar-integridade.hook.binary` | "Verificar integridade do pendrive" no menu do BIOS legado |
| `iso/config/hooks/normal/9020-tempo-menu.hook.binary` | menus de boot (BIOS e UEFI) entram sozinhos em 10 s |
| `iso/config/includes.chroot/etc/systemd/system/msscan-pendrive.service` | ao desligar: `sync`, pendrive somente leitura, desmonta os dados |

Detalhes importantes:

- **Rótulo do ISO9660 é `MSSCAN_BOOT`**, não `MSSCAN`: esse rótulo é reservado
  à partição de dados, e dois sistemas de arquivos com o mesmo rótulo quebrariam
  a montagem por `/dev/disk/by-label/MSSCAN`.
- Nenhuma ferramenta proprietária e nenhum firmware de servidor entram na ISO.
- O console usa a fonte `Uni2-Terminus16`: ela não tem ✓ nem ⚠, por isso no tty1
  o MSscan mostra `√` (OK), `▲` (alerta), `✗` (falha), `?` e `N/A`.
- `systemd-timesyncd` acerta o relógio pela rede; relógio errado quebra HTTPS.
- Secure Boot: `--uefi-secure-boot enable` exige o shim e o GRUB assinados da Debian (o build
  falha sem eles). Conferido na v0.5.0: `EFI/boot/bootx64.efi` é o shim, e no QEMU com Secure Boot
  ligado o kernel sobe em lockdown com "Debian Secure Boot CA". Teste também no servidor.
- O `build.sh` recusa ISO com arquivo de 4 GiB ou mais (limite do FAT32 no modo ISO do Rufus).

## 2. Gravar no pendrive

A ISO funciona **gravada de qualquer forma** e dá boot em **BIOS legado e UEFI, inclusive com
Secure Boot ligado** (shim e GRUB assinados pela Debian). O MSscan detecta sozinho como o
pendrive foi gravado e mostra isso na tela inicial e no relatório.

| Forma de gravação | Onde os dados ficam (relatórios, coletas, `vendor/`, `config/`...) |
|---|---|
| **Rufus, modo ISO, MBR (recomendado)** | pasta `MSSCAN/` no próprio pendrive (FAT32) — nada a preparar |
| Rufus, modo ISO, GPT | idem; **só dá boot em UEFI** (ver abaixo) |
| Rufus modo DD, `dd` no Linux, balenaEtcher | partição `MSSCAN` se você criá-la (seção 3); senão `/tmp` (perdido ao desligar) |
| Ventoy (a `.iso` copiada para o pendrive do Ventoy) | pasta `MSSCAN/` na partição de dados do Ventoy (exFAT) |

Ordem de escolha do local dos dados (automática; o MSscan **nunca formata nem cria partições**):

1. partição com rótulo `MSSCAN`, se existir;
2. pasta `MSSCAN/` no pendrive de boot, se ele for gravável (FAT32/exFAT: Rufus modo ISO);
3. pasta `MSSCAN/` na partição de dados do Ventoy;
4. `/tmp/msscan`, com aviso claro de que **nada será salvo no pendrive**.

> **Pendrive preparado com o nome antigo (ServerDiag):** se não houver `MSSCAN`, a partição com rótulo
> `SERVERDIAG` e a pasta `SERVERDIAG/` continuam sendo usadas (com a chave da IA, `config/` e `vendor/` que
> já estão lá). A tela inicial e o relatório mostram um aviso sugerindo renomear para `MSSCAN` (no Windows:
> botão direito > Renomear). Se existirem as duas, vale `MSSCAN`.

O pendrive de boot fica somente leitura e só ganha escrita durante cada gravação (com `sync` no
fim); ao desligar, o serviço `msscan-pendrive` faz `sync`, volta tudo para somente leitura e
desmonta as partições de dados. Mesmo assim, **desligue pelo menu ou `poweroff` antes de tirar o pendrive**.

> **Limitação de hardware (única exceção aceita):** pendrive gravado com esquema **GPT** (sistema de
> destino "UEFI (não CSM)" no Rufus) **não dá boot em servidor configurado em BIOS legado**. Por isso
> a recomendação é **MBR**, que funciona nos dois modos.

### 2.1 Recomendado: Rufus em modo ISO + MBR (Windows)

Rufus 4.x. Opção por opção, de cima para baixo:

| Campo do Rufus | Valor |
|---|---|
| **Dispositivo** | o pendrive (confira tamanho e nome: tudo nele será apagado) |
| **Seleção de boot** | "Disco ou imagem ISO" → **SELECIONAR** → `msscan-<versão>-amd64.iso` |
| **Esquema de partição** | **MBR** |
| **Sistema de destino** | **BIOS ou UEFI** (aparece sozinho ao escolher MBR) |
| Mostrar opções avançadas da unidade | não precisa mexer ("Listar discos USB" só se o pendrive não aparecer) |
| **Rótulo do volume** | deixe o que vem da ISO: `MSSCAN_BOOT` (nunca `MSSCAN`) |
| **Sistema de arquivos** | **FAT32** (padrão) — não use NTFS: o sistema live não dá boot de NTFS |
| **Tamanho do cluster** | padrão |
| Verificar blocos ruins | opcional (recomendado em pendrive velho: 1 passada) |

Clique **INICIAR**. Janelas que aparecem, na ordem:

1. **"Imagem ISOHybrid detectada"** → escolha **"Gravar no modo Imagem ISO (Recomendado)"** e OK.
   (Escolher "modo Imagem DD" também funciona, mas aí os dados ficam em `/tmp` — ver tabela acima.)
2. **"Download necessário"** (Syslinux 6.04: `ldlinux.sys`/`ldlinux.bss`) → **Sim**. É o carregador do
   BIOS legado; o Rufus baixa uma vez e guarda. Com MBR + "BIOS ou UEFI" **essa pergunta deve
   aparecer** na primeira gravação; se não apareceu e o sistema de destino ficou "UEFI (não CSM)",
   o pendrive não terá boot BIOS.
3. Aviso de que os dados do pendrive serão destruídos → OK.
4. Ao terminar ("PRONTO"), **remova com segurança** pelo Windows antes de tirar o pendrive
   (sair antes pode deixar o `filesystem.squashfs` incompleto).

Depois, opcionalmente, crie no pendrive (pelo próprio Windows) a pasta `MSSCAN\` com
`config\proxy.conf` e `vendor\` (ferramentas proprietárias). Sem ela, o MSscan cria o que faltar.

### 2.2 Alternativas

**Rufus modo DD / `dd` no Linux / balenaEtcher** (imagem bit a bit; o pendrive fica ISO9660, somente leitura):

```bash
lsblk -o NAME,SIZE,TRAN,MODEL      # o pendrive aparece com TRAN=usb
sudo dd if=iso/msscan-<versão>-amd64.iso of=/dev/sdX bs=4M conv=fsync status=progress
```

Para guardar dados nesse caso, crie a partição `MSSCAN` (seção 3).

**Ventoy:** copie `msscan-<versão>-amd64.iso` para a partição de dados do pendrive do Ventoy (exFAT,
rótulo "Ventoy") e escolha-a no menu do Ventoy. Os dados vão para `MSSCAN/` nessa mesma partição.
Com Secure Boot ligado, o Ventoy pede o cadastro da chave dele (MOK) no primeiro boot: siga a tela do Ventoy.

### 2.3 Se aparecer `Can not mount /dev/loop0 ... filesystem.squashfs`

O kernel e o initrd carregaram, mas o sistema de arquivos principal (`live/filesystem.squashfs`,
~530 MB) não pôde ser lido. Nos testes no QEMU (FAT32 MBR e GPT, UEFI com e sem Secure Boot, e
DD em BIOS) **o boot completo funcionou** com uma cópia íntegra da v0.5.0, então a causa mais
provável é cópia incompleta/corrompida ou leitura com erro. Para descobrir:

1. No prompt `(initramfs)` que aparece depois do erro, rode e fotografe:
   ```
   dmesg | tail -n 40
   ls -l /run/live/medium/live/
   ```
   Tamanho esperado de `filesystem.squashfs`: o mesmo de `live/filesystem.squashfs` dentro da ISO.
2. Dê boot de novo e escolha **"Verificar integridade do pendrive"** (BIOS: menu *Utilities*;
   UEFI: *Utilities → Verify integrity of the boot medium*). Qualquer `FAILED` = regrave o pendrive.
3. Desconecte outras mídias com o MSscan (outro pendrive, **mídia virtual do iDRAC/iLO**): o
   live-boot usa a primeira mídia que encontrar.
4. Teste outro pendrive/porta USB (as traseiras do servidor, não o hub do monitor).

## 3. Criar a partição de dados `MSSCAN` (opcional; útil no modo DD)

**Só é necessária quando o pendrive foi gravado em modo DD** (ou para separar os dados do boot).
O MSscan nunca cria nem formata partições: faça você, com cuidado.

No espaço livre que sobra depois da imagem, crie uma partição e formate com o
rótulo `MSSCAN` (o mesmo procedimento da persistência do Debian live).
**Antes, rode `sudo fdisk -l /dev/sdX`** e confira quais números de partição a ISO
já ocupa (a ISO v0.5.0 gravada em DD tem as partições 1 e 2):

```bash
sudo fdisk /dev/sdX
#   n   (nova partição)
#   p   (primária)
#   3   (número 3; a ISO híbrida já usa 1 e 2)
#   Enter, Enter (usa todo o espaço livre)
#   t, 3, 7   (tipo HPFS/NTFS/exFAT)
#   w   (grava)
sudo mkfs.exfat -L MSSCAN /dev/sdX3
```

Estrutura dos dados (na raiz da partição `MSSCAN`, ou dentro da pasta `MSSCAN/` do
pendrive do Rufus/Ventoy; o MSscan cria as pastas que faltarem):

```text
MSSCAN/
├── config/
│   ├── proxy.conf      # opcional (formato abaixo)
│   └── ia.key          # opcional, fase 7 (chave da API; nunca no git)
├── vendor/             # ferramentas proprietárias: perccli64, storcli64, ssacli, ... e o pacote
│                       # "iDRAC Tools for Linux" da Dell (Dell-iDRACTools-Web-LX-*.tar.gz, versão 11 ou mais nova)
├── relatorios/         # relatórios gerados
├── coletas/            # modo --coletar
└── estado/             # retomada de atualização (fase 9)
```

Nenhum arquivo da ISO chega perto do limite de 4 GiB do FAT32 (o maior é o
`filesystem.squashfs`, ~530 MB na v0.5.0); o `build.sh` recusa gerar ISO com arquivo de 4 GiB ou mais.

### Matriz de testes (preencha conforme for testando)

Legenda: ✓ funcionou · ✗ falhou (anote a mensagem) · — não se aplica · vazio = não testado.
"QEMU ✓" = testado em máquina virtual, com o pendrive **simulado** (imagem com FAT32
copiada como o Rufus faz, ou a ISO crua como no `dd`), boot pelo shim/GRUB da ISO; "QEMU ✓\*" =
kernel carregado direto pelo QEMU (testa o live-boot e a detecção, não o carregador de boot).
Nada disso substitui o teste com o Rufus de verdade no servidor.

| Forma de gravação | BIOS legado | UEFI, Secure Boot desligado | UEFI, Secure Boot ligado | Local dos dados mostrado | Observações |
|---|---|---|---|---|---|
| Rufus modo ISO, MBR, FAT32 | | | QEMU ✓ | `MSSCAN/` no pendrive (QEMU ✓) | |
| Rufus modo ISO, GPT, FAT32 | — (limitação de hardware) | QEMU ✓ | | `MSSCAN/` no pendrive | |
| Rufus modo DD | QEMU ✓\* (como `dd`) | | | `/tmp` (QEMU ✓\*) | R640: boot ✓ na v0.5.0 (modo de boot não informado) |
| `dd` no Linux | QEMU ✓\* | | | `/tmp` (ou partição `MSSCAN`) | |
| balenaEtcher | | | | `/tmp` (ou partição `MSSCAN`) | |
| Ventoy (exFAT) | | | | `MSSCAN/` na partição do Ventoy | |
| Mídia virtual do iDRAC/iLO | | | | `/tmp` | |

Em cada teste, anote também o que a tela inicial mostrou em "Pendrive de boot" e "Dados do MSscan".

### `config/proxy.conf`

```text
# uma URL vale para HTTP e HTTPS:
http://usuario:senha@proxy.empresa:3128
# ou, separadamente:
https_proxy=http://proxy.empresa:3128
no_proxy=localhost,10.0.0.0/8
```

A senha nunca é exibida (aparece como `***`) e o arquivo nunca entra em coletas.

## 4. Usar no servidor

1. Boot pelo pendrive (BIOS legado ou UEFI).
2. Abre a **interface gráfica** em tela cheia (quiosque: Xorg + Firefox ESR, no vt7), operável só
   pelo mouse ou só pelo teclado (`?` mostra os atalhos). **Não há modo texto no boot:**
   - se a interface não estiver pronta em 90 s, tenta de novo com outro driver de vídeo (autodetecção, `fbdev`, `vesa`)
     e recomeça, sem fim; se cair depois de aberta, abre de novo;
   - os logs de cada tentativa vão para o pendrive (`MSSCAN/logs/interface_<data>/`, ou junto do relatório em
     `interface_falhou/`).
3. A rede é verificada em 3 níveis: **cabo → IP (DHCP) → internet (HTTPS)**.
   - Sem rede: a tela explica o problema (sem cabo / sem DHCP / sem internet) e oferece
     **Tentar novamente**. O diagnóstico só começa com rede.
4. Depois vêm detecção, diagnóstico e relatório.
5. `Ctrl+Alt+F2` abre um console de texto (só com `msscan.terminal=1`); a aba Terminal da interface também.

### Interface gráfica: como funciona e onde olhar se algo der errado

- O backend (`main.py --ui`) roda como root e escuta **só em 127.0.0.1**, com token por sessão.
- O navegador roda como o usuário sem privilégios `msscan-ui`, cuja rede é restrita a
  127.0.0.1 pelo firewall `/etc/msscan/quiosque.nft` (o Firefox tenta falar com serviços da
  Mozilla mesmo com tudo desligado nas preferências; o firewall impede). Sem firewall, a interface
  não sobe (o MSscan tenta aplicá-lo de novo a cada 5 s).
- Um gerenciador mínimo (`matchbox`) é necessário: sem ele o Firefox não ocupa a tela toda.
- Logs em `/run/msscan/`: `ui.log` (backend), `xorg-sessao.log` (X e navegador), `nft.log`;
  o Xorg em `/var/log/Xorg.0.log`.
- Vídeo: driver `modesetting` (KMS), com `fbdev`/`vesa` de reserva. Em Matrox G200 e ASPEED (vídeo
  dos BMCs) o cursor é desenhado por software, para aparecer no console virtual do iDRAC/iLO.

### Acesso ao BMC (Redfish): credenciais digitadas na hora

Quando o MSscan encontra um BMC com Redfish (iDRAC, iLO, XCC...), ele pergunta usuário e senha
do BMC **deste** servidor, explicando o ganho (fontes, sensores, armazenamento e placas de rede mais
completos, inclusive RAID de hardware sem ferramenta do fabricante). Nada é salvo; nunca há senha padrão.

- **Pular é normal:** no modo texto, deixe o usuário em branco e dê Enter; na interface, botão
  **Pular**. O diagnóstico continua por IPMI, ferramentas do fabricante e sistema operacional, e o
  relatório diz "Credenciais do BMC não informadas pelo técnico" e o que foi verificado por outro método.
- **No máximo 3 tentativas** (iDRAC/iLO bloqueiam o endereço depois de várias senhas erradas).
- Para diagnóstico, prefira um usuário **somente leitura** no BMC; o MSscan mostra o perfil validado.
  (Para atualizar firmware não é preciso digitar um administrador: sem ele, o MSscan cria um administrador
  TEMPORÁRIO pelo acesso interno e o apaga no fim — ver "Atualizações de firmware".)
- Quem pulou pode informar depois: **Diagnóstico → Informar credenciais do BMC** refaz só os itens do Redfish.
- Onde o MSscan procura o Redfish: Redfish Host Interface (SMBIOS tipo 42, se habilitada no BMC) ou o
  IP do BMC informado pelo IPMI (`ipmitool lan print 1`). Nesse caso, a **rede do MSscan precisa
  alcançar o IP do BMC** (porta dedicada do iDRAC na mesma rede, ou porta compartilhada).
- `python3 main.py --sem-credenciais` não pergunta nada (diagnóstico sem Redfish).
- `--coletar` também pergunta: com credenciais, grava as respostas Redfish (sem senha nem token) para
  virarem fixtures. Anonimize antes de enviar: `python3 -m msscan.coleta.anonimizar <pasta>`.

### Análise por IA (opcional)

Depois do relatório, o MSscan pode enviar o resultado para uma interpretação por IA, com **plano de
solução por problema** (passo a passo, prioridade, se exige ir até o servidor ou trocar peça). **O padrão é
NÃO**: nada é enviado sem o técnico confirmar.

**Configuração:** na interface, **Análise por IA → Configurar IA**: escolha a IA principal e a reserva (gratuitas e
pagas, com limites e avisos de privacidade de cada uma), abra o link de criação da chave pelo QR Code (ou envie a chave
pelo celular), cole a chave, **Testar** e **Salvar no pendrive**. Padrão: Google Gemini gratuito (Flash-Lite) como
principal e Groq como reserva sugerida. Se a principal der limite (429), erro ou demorar, a reserva assume sozinha.

Pelo Bloco de Notas do Windows: `MSSCAN/config/ia.conf` (criado como modelo comentado no primeiro uso; aceita fim de
linha do Windows):

```ini
[ia]
principal = gemini_gratuito
reserva = groq

[gemini_gratuito]
chave = AIza...

[groq]
chave = gsk_...
```

- A chave fica só no `ia.conf` do pendrive (nunca variável de ambiente, nunca no git); na tela aparecem no máximo os
  4 últimos caracteres. **Remover chave** apaga do pendrive.
- **Planos gratuitos podem usar os dados para treinamento**: o MSscan avisa antes do envio. Serial,
  service tag, UUID, asset tag, MAC, IP, hostname e WWN são removidos e dá para **ver exatamente o que será
  enviado** antes.
- A IA **não altera nenhum status** e não executa nada. Cada problema cita um trecho dos dados (se não existir:
  "sugestão da IA não confirmada pelos dados"). Ela pode **recomendar** uma ação do catálogo fechado do MSscan
  (ex.: `bmc.sel_exportar_limpar`), só pelo identificador; identificador inexistente é ignorado e sinalizado.
  A ação só roda se o técnico confirmar (`CORRIGIR`).
- O relatório registra **qual provedor e modelo** gerou a análise e as tentativas que falharam.
- Precisa de internet (usa o `config/proxy.conf`, se houver). Com `msscan.semrede=1` ou `--sem-ia`, a IA fica
  desativada. Se todos os provedores falharem, o relatório sai sem a seção de IA.

### Atualizações de firmware (Dell)

> **Atenção.** Uma atualização de firmware interrompida (falta de energia, defeito de hardware) pode
> inutilizar o servidor. Use nobreak, tenha backup e faça com o servidor fora de operação.

Depois do diagnóstico, **Atualizações** (padrão NÃO) compara o firmware instalado com o **catálogo oficial da Dell**
(guardado por 7 dias em `MSSCAN/firmware/cache/`); o modelo precisa bater exatamente. Fluxo em 6 etapas, com a faixa
"Próximo passo": escolher (nada vem marcado) → baixar → verificação automática (hash, modelo, versão) → **Plano de
atualização** (o que, ordem com BIOS por último, tempo, reinícios, hardware conferido na hora e as mudanças
TEMPORÁRIAS no iDRAC) → digitar `ATUALIZAR` → instalação automática. Depois do `ATUALIZAR` nada mais é perguntado:
acesso ao iDRAC, reinícios pelo pendrive (boot único), retomada, restauração das mudanças temporárias e relatório
antes × depois.

- Caminhos, em ordem, até um funcionar (cada tentativa vai para o relatório): repositório pelo próprio iDRAC9 (como o
  Dell Repository Manager); Redfish com as credenciais informadas; Redfish com um administrador temporário criado pelo
  IPMI interno; OS to iDRAC Pass-through temporário; `racadm` local (se estiver em `MSSCAN/vendor/`); pacote Linux
  `.BIN`; outro formato oficial do mesmo componente. Se nenhum servir, a tela explica exatamente o que faltou.
- **Acesso ao iDRAC** (caixa no Início, com "Tentar de novo"): o MSscan usa (B) o Redfish pela rede, se a porta do
  iDRAC tiver cabo e IP alcançável, ou (A) o `racadm` local, preparado sozinho a partir do pacote "iDRAC Tools for
  Linux" da Dell em `MSSCAN/vendor/` — por dentro do servidor, sem cabo no iDRAC. Com internet, o MSscan baixa esse
  pacote sozinho de `dl.dell.com` (SHA-256 conferido; endereço e hash em `ferramentas_dell.yaml`); sem internet, copie-o
  (sem descompactar) para `MSSCAN/vendor/`. A caixa
  mostra porta dedicada/compartilhada, link, IP e o que fazer (ex.: "Cabo não detectado na porta dedicada do iDRAC").
- Mudanças temporárias no iDRAC ficam registradas em `MSSCAN/estado/mudancas_temporarias.json` e são desfeitas no fim,
  antes de cada reinício e, se o MSscan cair, no próximo boot pelo pendrive. A rede do iDRAC e a senha de usuários
  existentes nunca são alteradas.
- Bloqueiam: fonte com defeito real (ou classificação incerta), erro de memória não corrigível ATUAL, RAID degradado ou
  em reconstrução, temperatura/ventoinha crítica, hardware não verificado, hash divergente, modelo errado e versão não
  confirmada. Fonte sem energia pede a confirmação extra `SEM REDUNDANCIA`; evento crítico só no histórico do log pede
  "Estou ciente de que é um evento antigo".
- Sem pacote no catálogo: **Atualização manual** abre o navegador integrado na página de drivers da Dell (só sites
  oficiais); o pacote baixado é conferido (SHA-256 da página da Dell, modelo e versão) antes de habilitar a instalação.
- Algo deu errado? **Relatórios → Exportar log técnico** grava `MSSCAN/logs/log_tecnico_<data>.json` sem senhas, chaves
  nem dados do cliente.

### Resolução de vídeo

- A interface usa a **resolução preferida do monitor** (EDID). Se a placa recusar, desce sozinha:
  maior modo até 1920x1080 → 1280x1024 → 1024x768 → 800x600. Sem EDID (monitor antigo, KVM, console
  virtual do iDRAC/iLO): modo seguro 1024x768.
- A escala da interface acompanha a resolução; `Ctrl +` / `Ctrl −` ajustam o zoom e `Ctrl 0` volta ao automático.
- **Atalhos / Sobre → Tela e resolução**: mostra a resolução em uso e permite trocar (volta sozinha em 15 s se não
  for confirmada, caso a imagem fique ruim).
- Parâmetro de boot `msscan.resolucao=1280x1024` força uma resolução (acrescente como o `semrede`).
- No UEFI, o GRUB e o console já usam a resolução nativa; a fonte do modo texto cresce em telas grandes.
- Registro do ajuste: `/run/msscan/tela.log`.

### Menu de boot

Os menus (syslinux no BIOS legado, GRUB no UEFI) mostram uma contagem e entram sozinhos em
"Live system" depois de **10 s** (para reboot remoto pelo console do iDRAC/iLO). Qualquer tecla de
seta interrompe a contagem, para escolher outra opção ou editar a linha de boot.

> Os parâmetros antigos `serverdiag.semrede=1` e `serverdiag.resolucao=LxA` continuam
> aceitos durante a transição de nome; se houver os dois, vale o `msscan.*`.

### Parâmetro `msscan.texto=1` (removido em 16/10/2026)

Não existe mais: o boot vai sempre para a interface gráfica.

### Parâmetro oculto `msscan.semrede=1` (só técnicos)

Libera o diagnóstico sem rede e **desativa IA e atualizações**. Para usar, no menu de boot:

- **BIOS (syslinux):** dentro dos 10 s do menu, selecione a entrada, pressione `Tab`, acrescente ` msscan.semrede=1` e Enter.
- **UEFI (GRUB):** dentro dos 10 s do menu, selecione a entrada, pressione `e`, acrescente ` msscan.semrede=1`
  no fim da linha que começa com `linux`, e `Ctrl+X`.

## 5. Testar no QEMU (sem servidor)

Pacotes: `sudo apt install qemu-system-x86 ovmf`. As opções `ipmi-bmc-sim` e
`isa-ipmi-kcs` simulam um BMC com IPMI via KCS (como o de servidores reais), para o
MSscan detectar IPMI dentro da VM.

**Sem tela (servidor de build):** `iso/testar_qemu.py` sobe a ISO em BIOS, UEFI ou UEFI com Secure Boot, com o modelo Dell e
o relógio atrasado que quiser, tira capturas e manda teclas pelo monitor do QEMU (saída em `iso/trabalho/qemu/`):

```bash
python3 iso/testar_qemu.py iniciar iso/msscan-1.0.4-amd64.iso --modo uefi-sb --modelo "PowerEdge R530" --atraso-dias 183
python3 iso/testar_qemu.py tela inicio          # ~90 s depois do início
python3 iso/testar_qemu.py teclas g a tab ret   # o mouse do QEMU não chega à sessão gráfica sem tela: use o teclado
python3 iso/testar_qemu.py parar
```

### BIOS legado

```bash
qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 \
  -device ipmi-bmc-sim,id=bmc0 -device isa-ipmi-kcs,bmc=bmc0 \
  -netdev user,id=net0 -device e1000,netdev=net0 \
  -vga std -usb -device usb-tablet \
  -cdrom iso/msscan-<versão>-amd64.iso -boot d \
  -monitor stdio
```

`-vga std` (bochs-drm, KMS) exercita o driver `modesetting`; `-usb -device usb-tablet` imita o
mouse absoluto do console virtual do iDRAC/iLO.

### UEFI

```bash
cp /usr/share/OVMF/OVMF_VARS_4M.fd /tmp/ovmf_vars.fd
qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 -machine q35 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=/tmp/ovmf_vars.fd \
  -device ipmi-bmc-sim,id=bmc0 -device isa-ipmi-kcs,bmc=bmc0 \
  -netdev user,id=net0 -device e1000,netdev=net0 \
  -vga std -usb -device usb-tablet \
  -cdrom iso/msscan-<versão>-amd64.iso -boot d \
  -monitor stdio
```

`-vga std` (bochs-drm, KMS) exercita o driver `modesetting`; `-usb -device usb-tablet` imita o
mouse absoluto do console virtual do iDRAC/iLO.

Para testar Secure Boot, troque por `OVMF_CODE_4M.secboot.fd` e `OVMF_VARS_4M.ms.fd`.

### Simular o pendrive (com partição de dados)

```bash
cp iso/msscan-<versão>-amd64.iso /tmp/pendrive.img
truncate -s 4G /tmp/pendrive.img
# crie a partição 3 e formate em /tmp/pendrive.img (via losetup -P) como na seção 3
# e troque "-cdrom ... -boot d" por:
  -drive if=none,id=usb,format=raw,file=/tmp/pendrive.img \
  -device qemu-xhci -device usb-storage,drive=usb,bootindex=0
```

### Roteiro de testes da rede

| Cenário | Como simular | Esperado |
|---|---|---|
| Rede OK | comandos acima | `[1/3] √  [2/3] √  [3/3] √` e segue |
| Sem cabo | no monitor do QEMU: `set_link net0 off` | "SEM CABO"; volte com `set_link net0 on` e Enter |
| Sem internet | `-netdev user,id=net0,restrict=on` | "SEM INTERNET" (DHCP funciona, sem saída) |
| Sem DHCP | `-netdev socket,id=net0,listen=:12345` (link sem DHCP; a confirmar) | "SEM DHCP" após ~30 s |
| Sem rede por parâmetro | boot com `msscan.semrede=1` | aviso e diagnóstico sem rede |
| Símbolos no tty1 | qualquer | `√ ▲ ✗` legíveis (nada de losangos) |

Anote o que não bater e envie o `iso/trabalho/build.log` se o build falhar.

## 6. Interface gráfica (`--ui`)

O mesmo diagnóstico, acompanhado por uma interface no navegador. O MSscan sobe um servidor
HTTP **somente em 127.0.0.1** (nunca acessível pela rede) e imprime o endereço com o token da sessão:

```
Interface gráfica disponível SOMENTE neste computador (127.0.0.1). Abra no navegador:
  http://127.0.0.1:8765/#t=<token>
```

O modo texto continua sendo o padrão (`python3 main.py`), no tty1 e por SSH.

### Modo demonstração (sem tocar no hardware)

```
python3 main.py --ui --replay tests/fixtures/dell/poweredge_r640
```

Usa as saídas gravadas do R640 e respeita o tempo real de cada comando (~10 s de diagnóstico),
então o progresso aparece como num servidor de verdade. `--ui-porta N` troca a porta (padrão 8765).
Ctrl+C encerra.

### Abrir no navegador do Windows (túnel SSH pelo MobaXterm)

Como o servidor só escuta em 127.0.0.1, o acesso de outro computador é por túnel SSH:

1. No MobaXterm, clique em **Tunneling** (barra superior) → **New SSH tunnel**.
2. Marque **Local port forwarding** e preencha:
   - *Forwarded port* (lado "My computer"): `8765`
   - *SSH server*: IP da máquina Linux, usuário (ex.: `root`), porta `22`
   - *Remote server*: `127.0.0.1`, porta `8765`
3. **Save**, depois clique em ▶ (**Start**) na linha do túnel (pede a senha/chave SSH).
4. Numa sessão SSH normal do MobaXterm, rode o modo demonstração (comando acima) e copie a
   linha `http://127.0.0.1:8765/#t=...` (no MobaXterm, selecionar o texto já copia).
5. Cole no Chrome/Edge/Firefox do Windows. O token vai junto no endereço; sem ele a API recusa.

Alternativa sem a janela de túneis: no terminal local do MobaXterm,
`ssh -L 8765:127.0.0.1:8765 root@<IP>` e rode o `main.py --ui` nessa mesma sessão.
Se a porta 8765 do Windows estiver ocupada, use outra no lado local (ex.: `18765`) e abra
`http://127.0.0.1:18765/#t=...` — o servidor aceita qualquer porta de 127.0.0.1/localhost.

Teclado: `?` mostra os atalhos; `g i`/`g d`/`g r`/`g a` trocam de tela; `/` filtra os componentes;
setas percorrem listas e tabelas; Enter abre; Esc volta.
