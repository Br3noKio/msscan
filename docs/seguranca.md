# Segurança

## Dados do cliente

- O MSscan **não monta, não lê e não altera** partições, sistemas de arquivos e volumes RAID dos discos do cliente. O
  executor recusa `mount`, `fsck`, `debugfs`, leitura de `/dev/sdX`/`/dev/nvmeX` por ferramentas de arquivo e qualquer
  alteração de volume. A única montagem permitida é a do pendrive do MSscan.
- Comandos de ferramentas de RAID passam por uma lista de leitura permitida.
- A única ação sobre os discos é o autoteste SMART (`smartctl -t short|long` e `-X`), por um método próprio do executor,
  com o disco escolhido entre os já diagnosticados; o teste roda dentro do disco, sem leitura dos dados pelo MSscan.
  Qualquer outro `smartctl` que altere o disco é recusado.

## Credenciais e chaves

| Dado | Onde fica | Onde nunca vai |
|---|---|---|
| Senha do iDRAC | Só na memória durante a execução | Disco, log, relatório, coleta, IA |
| Senha do usuário temporário do iDRAC | Memória; fora da linha de comando | Idem |
| Senha do SSH | Memória | Idem |
| Chave da IA | `MSSCAN/config/ia.conf` no pendrive | Logs, relatórios, coletas; na tela, só os 4 últimos caracteres |

Nunca há credencial fixa, padrão ou tentativa automática de senha.

## Dados enviados para fora

- **Dell:** catálogo e pacotes; o service tag só vai na URL da página de drivers.
- **IA (opcional):** dados sanitizados (sem serial, service tag, UUID, asset tag, MAC, IP, hostname, WWN), com prévia.
- Nada mais sai do servidor.

## Escritas no iDRAC

No Redfish, o executor permite `GET` livre, `POST` só para abrir sessão e `DELETE` só da própria sessão. Escritas de
firmware e ações corretivas usam métodos próprios, com URL e corpo exatos. `ipmitool` de escrita fora do catálogo é
recusado. Confirmações digitadas (`ATUALIZAR`, `CORRIGIR`) são conferidas no servidor, não no navegador.

## Rede

- Interface gráfica só em 127.0.0.1, com token aleatório por sessão. Terminal nunca exposto à rede.
- Firewall: nada entra por padrão. Portas abrem só enquanto necessárias:

| Porta | Quando | Para quem |
|---|---|---|
| Pareamento do celular (HTTPS) | Tela "Configurar IA" aberta, até 5 min | Rede local |
| Repositório de firmware (HTTP) | Durante a atualização pelo repositório | Só o IP do iDRAC |
| SSH (22) | Só com o acesso SSH ativado | Rede local |

- Navegador integrado: perfil temporário, usuário próprio que só alcança um proxy local, que só conecta aos domínios
  oficiais da Dell, HPE e Lenovo. Arquivo baixado nunca é executado sozinho.

## Boot e build

- Secure Boot com shim e GRUB assinados pela Debian.
- O build da ISO roda num namespace de montagem privado e nunca monta o `efivarfs` no chroot, para não tocar nas
  variáveis UEFI da máquina de build.
- Cada versão publica o `.sha256` da ISO.

## Reportar uma vulnerabilidade

Abra uma issue sem detalhes de exploração e peça um contato privado.
