# Saídas avulsas reais

Saídas reais de comandos que não fazem parte do modo coleta.

| Arquivo | Comando | Máquina |
|---|---|---|
| `findmnt_raiz.json` | `findmnt -J -o SOURCE,TARGET,FSTYPE,OPTIONS /` | Dell PowerEdge R640 (desenvolvimento), 2026-09-24 |
| `pendrive/dd_bios__*` | `findmnt -J -o TARGET,SOURCE,FSTYPE,OPTIONS,LABEL /run/live/medium`, `lsblk -J -p -o NAME,PKNAME,TYPE,TRAN,RM,RO,FSTYPE,LABEL,PTTYPE,PARTTYPE,MOUNTPOINT` | ISO v0.5.0 no QEMU, gravada crua (como `dd`/Rufus modo DD), BIOS, 2026-09-25 |
| `pendrive/rufus_mbr_uefi_sb__*` | os mesmos + `od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-...` | ISO v0.5.0 no QEMU, arquivos copiados para FAT32 em MBR (como o Rufus modo ISO), UEFI com Secure Boot, 2026-09-25 |
| `pendrive/rufus_gpt_uefi__*` | os mesmos + `losetup -l -J` (sem a variável SecureBoot no OVMF sem chaves) | idem, FAT32 em GPT, UEFI sem Secure Boot, 2026-09-25 |

Pendente (`TODO(fixture)`): as mesmas saídas num pendrive do **Ventoy** e numa **mídia virtual do iDRAC**.
