#!/bin/bash
# Constrói a ISO do MSscan com live-build.
#
# Uso (como root, num Debian 13 com live-build instalado):
#     sudo ./iso/build.sh
#
# Variáveis opcionais:
#     TRABALHO=/caminho   pasta de trabalho do live-build (padrão: iso/trabalho)
#     ESPELHO=URL         espelho Debian (padrão: http://deb.debian.org/debian/)
#     LIMPO=1             descarta o cache do live-build (pacotes e base do debootstrap)
#
# Resultado: iso/msscan-<versão>-amd64.iso (+ .sha256)
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
TRABALHO="${TRABALHO:-$RAIZ/iso/trabalho}"
ESPELHO="${ESPELHO:-http://deb.debian.org/debian/}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Erro: o live-build precisa de root. Rode: sudo $0" >&2
    exit 1
fi
if ! command -v lb >/dev/null 2>&1; then
    echo "Erro: live-build não instalado. Rode: apt install live-build" >&2
    exit 1
fi

# Proteções (incidente de 06/10/2026, CLAUDE.md 24): o build nunca pode tocar nas variáveis UEFI do servidor.
# 1) Tudo roda num namespace de montagem PRIVADO: as montagens do live-build (proc, sys, devpts...) não aparecem no
#    servidor e somem com o fim do build — nada fica montado para um `lb clean`/`rm -rf` seguinte.
if [ -z "${MSSCAN_BUILD_ISOLADO:-}" ]; then
    exec env MSSCAN_BUILD_ISOLADO=1 unshare --mount --propagation private "$0" "$@"
fi
. "$RAIZ/iso/protecao_build.sh"

VERSAO="$(cd "$RAIZ" && python3 -c 'import msscan; print(msscan.__version__)')"
echo "==> MSscan $VERSAO: pasta de trabalho $TRABALHO"

# Recomeça a configuração do zero (o live-build reaproveita estado de builds anteriores), mas
# PRESERVA o cache (cache/packages.* = .deb baixados; cache/bootstrap = base do debootstrap):
# a build com cache não baixa de novo nem refaz o debootstrap.
# - A base do debootstrap é descartada se tiver mais de 7 dias (atualizações de segurança entram).
# - LIMPO=1 descarta todo o cache (build do zero).
CACHE_GUARDADO=""
if [ -d "$TRABALHO" ]; then
    # 2) Nada montado dentro da pasta de trabalho antes de QUALQUER limpeza (senão aborta, sem apagar nada).
    exigir_sem_montagens "$TRABALHO" || exit 3
    if [ "${LIMPO:-0}" != "1" ] && [ -d "$TRABALHO/cache" ]; then
        CACHE_GUARDADO="$(mktemp -d "$(dirname "$TRABALHO")/.cache-live-build.XXXXXX")"
        mv "$TRABALHO/cache" "$CACHE_GUARDADO/cache"
        if [ -d "$CACHE_GUARDADO/cache/bootstrap" ] && \
           [ -n "$(find "$CACHE_GUARDADO/cache/bootstrap" -maxdepth 0 -mtime +7)" ]; then
            echo "==> Base do debootstrap em cache com mais de 7 dias: será refeita"
            rm -rf "$CACHE_GUARDADO/cache/bootstrap"
        fi
    fi
    exigir_sem_montagens "$TRABALHO" || exit 3
    (cd "$TRABALHO" && lb clean --purge) || true
    exigir_sem_montagens "$TRABALHO" || exit 3
    rm -rf --one-file-system "$TRABALHO"  # 3) nunca desce em outro sistema de arquivos
fi
mkdir -p "$TRABALHO"
if [ -n "$CACHE_GUARDADO" ]; then
    mv "$CACHE_GUARDADO/cache" "$TRABALHO/cache"
    rmdir "$CACHE_GUARDADO"
    echo "==> Usando o cache do live-build ($(du -sh "$TRABALHO/cache" | cut -f1)); LIMPO=1 para build do zero"
fi
cd "$TRABALHO"

# Rótulo do ISO9660 NÃO pode ser "MSSCAN": esse rótulo é da partição de dados.
lb config \
    --distribution trixie \
    --architecture amd64 \
    --binary-image iso-hybrid \
    --bootloaders "syslinux,grub-efi" \
    --uefi-secure-boot enable \
    --archive-areas "main contrib non-free non-free-firmware" \
    --mirror-bootstrap "$ESPELHO" \
    --mirror-binary "$ESPELHO" \
    --debian-installer none \
    --apt-recommends false \
    --firmware-chroot false \
    --firmware-binary false \
    --linux-flavours amd64 \
    --memtest none \
    --security true \
    --updates true \
    --iso-volume "MSSCAN_BOOT" \
    --iso-application "MSscan $VERSAO" \
    --image-name "msscan" \
    --bootappend-live "boot=live components quiet locales=pt_BR.UTF-8 keyboard-layouts=br timezone=America/Sao_Paulo"

# Configuração do projeto (pacotes, serviço, hook, módulos IPMI).
cp -a "$RAIZ/iso/config/." config/

# Aplicação em /opt/msscan com o banco de compatibilidade (sem testes, fixtures nem bytecode).
DESTINO="config/includes.chroot/opt/msscan"
mkdir -p "$DESTINO"
(cd "$RAIZ" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - main.py msscan compat_db) | tar -xf - -C "$DESTINO"

# 4) O efivarfs nunca é montado no chroot: o hook do mdadm no initramfs (Debian #962844) monta o efivarfs REAL do
#    servidor, com escrita, em chroot/sys/firmware/efi/efivars (causa do incidente). Os wrappers de iso/sem-efivarfs/
#    cobrem chroot/sys/firmware com um tmpfs vazio somente leitura assim que o live-build monta o /sys (o chroot
#    fica como numa máquina sem UEFI; a ISO não depende do firmware de quem constrói).
export PATH="$RAIZ/iso/sem-efivarfs:$PATH"
# 5) Reserva: um vigia desmonta na hora qualquer efivarfs que ainda apareça no chroot e o build FALHA (efivarfs.log).
EFIVARFS_LOG="$TRABALHO/efivarfs.log"
rm -f "$EFIVARFS_LOG"
vigiar_efivarfs "$TRABALHO" "$EFIVARFS_LOG" &
VIGIA=$!
trap 'kill "$VIGIA" 2>/dev/null || true' EXIT

echo "==> lb build (demora; log em $TRABALHO/build.log)"
lb build 2>&1 | tee build.log
kill "$VIGIA" 2>/dev/null || true
if [ -s "$EFIVARFS_LOG" ]; then
    echo "ERRO: o efivarfs foi montado dentro do chroot (desmontado na hora pelo vigia); ISO NÃO entregue. Veja" >&2
    echo "      $EFIVARFS_LOG" >&2
    exit 4
fi

ISO_GERADA="$(ls -1 msscan*.iso 2>/dev/null | head -1 || true)"
if [ -z "$ISO_GERADA" ]; then
    echo "Erro: o live-build não gerou a ISO. Veja $TRABALHO/build.log" >&2
    exit 1
fi
# FAT32 (Rufus modo ISO, Ventoy com FAT32) não guarda arquivo de 4 GiB ou mais.
GRANDES="$(find binary -type f -size +4194303k 2>/dev/null || true)"
if [ -n "$GRANDES" ]; then
    echo "Erro: arquivo(s) com 4 GiB ou mais na ISO (não cabem em FAT32):" >&2
    echo "$GRANDES" >&2
    exit 1
fi
MAIOR="$(find binary -type f -printf '%s %P\n' | sort -n | tail -1)"
echo "==> Maior arquivo da ISO: ${MAIOR} bytes (limite do FAT32: 4294967295)"

ISO_FINAL="$RAIZ/iso/msscan-$VERSAO-amd64.iso"
mv "$ISO_GERADA" "$ISO_FINAL"
(cd "$(dirname "$ISO_FINAL")" && sha256sum "$(basename "$ISO_FINAL")" > "$(basename "$ISO_FINAL").sha256")
echo "==> Pronto: $ISO_FINAL"
