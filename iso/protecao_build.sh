# Proteções do build da ISO (incidente de 06/10/2026, CLAUDE.md 24): o build NUNCA pode tocar nas variáveis UEFI do
# servidor onde roda. Num build, o /sys do chroot ficou montado com o efivarfs (variáveis UEFI do R640, com escrita)
# embaixo dele; o `lb clean --purge` seguinte fez `rm -rf` ali e apagou BootOrder, Boot####, ConIn/ConOut/ErrOut,
# Lang/PlatformLang e Timeout.
#
# Usado pelo build.sh (source). Funções testadas em tests/test_build_protecao.py.
#   montagens_em DIR          lista os pontos de montagem em DIR ou abaixo dele (um por linha)
#   exigir_sem_montagens DIR  aborta (código 3) se houver qualquer montagem em DIR — chamar ANTES de lb clean e rm -rf
#   vigiar_efivarfs DIR FLAG  em segundo plano: desmonta na hora qualquer efivarfs em DIR, registra quem estava rodando
#                             e cria FLAG (o build.sh falha no fim se FLAG existir)
# MONTAGENS (variável de ambiente) troca /proc/self/mounts nos testes.

montagens_em() {
    local dir="${1%/}"
    # Campo 2 do /proc/self/mounts, com espaço/tab/quebra/barra codificados em octal (\040 \011 \012 \134).
    awk -v dir="$dir" '{
        p = $2
        gsub(/\\040/, " ", p); gsub(/\\011/, "\t", p); gsub(/\\012/, "\n", p); gsub(/\\134/, "\\", p)
        if (p == dir || index(p, dir "/") == 1) print p
    }' "${MONTAGENS:-/proc/self/mounts}"
}

exigir_sem_montagens() {
    local montadas
    montadas="$(montagens_em "$1")"
    if [ -n "$montadas" ]; then
        echo "ERRO: há sistemas de arquivos montados dentro de $1 — limpeza ABORTADA (nada foi apagado):" >&2
        printf '%s\n' "$montadas" | sed 's/^/    /' >&2
        echo "Desmonte-os (ex.: umount -R \"$1/chroot/sys\") e confira com: findmnt -R \"$1\"" >&2
        return 3
    fi
}

vigiar_efivarfs() {
    local dir="${1%/}" flag="$2" ponto
    while :; do
        awk -v dir="$dir" '$3 == "efivarfs" && index($2, dir "/") == 1 { print $2 }' "${MONTAGENS:-/proc/self/mounts}" |
            while read -r ponto; do
                {
                    echo "$(date '+%F %T') efivarfs montado em $ponto — desmontado na hora. Processos:"
                    ps -eo pid,ppid,args --sort=start_time | tail -n 25
                } >> "$flag"
                umount "$ponto" 2>>"$flag" || umount -l "$ponto" 2>>"$flag"
            done
        sleep 0.2
    done
}
