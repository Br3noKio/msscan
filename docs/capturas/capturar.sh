#!/bin/sh
# Capturas da interface pela demonstração estática (docs/demo), com o Chromium sem tela.
# Uso: [TAMANHOS="800x600 1366x768"] docs/capturas/capturar.sh <pasta-de-saída> [prefixo]
# Movimento reduzido: no tempo virtual do Chromium sem tela as transições ficam congeladas no início.
set -eu
SAIDA="$1"; PREFIXO="${2:-}"
DEMO="file://$(cd "$(dirname "$0")/../demo" && pwd)/index.html"
mkdir -p "$SAIDA"
for tamanho in ${TAMANHOS:-1920x1080 1024x768}; do
  for cena in "inicio::" "diagnostico:diagnostico:diagnostico" "ia:ia:ia" "chat:chat:chat" "atualizacoes:atualizacoes:atualizacoes" \
              "plano-atualizacao:atualizacoes:aviso" "relatorio:relatorios:diagnostico" "terminal:terminal:diagnostico" "sobre:sobre:diagnostico"; do
    nome=${cena%%:*}; resto=${cena#*:}; rota=${resto%%:*}; estado=${resto#*:}
    url="$DEMO?cena=$estado"; [ -n "$rota" ] && url="$url&rota=$rota"
    chromium --headless --no-sandbox --disable-gpu --hide-scrollbars --force-prefers-reduced-motion --allow-file-access-from-files \
      --window-size="$(echo $tamanho | tr x ,)" --virtual-time-budget=8000 \
      --screenshot="$SAIDA/${PREFIXO}${nome}-${tamanho}.png" "$url" >/dev/null 2>&1
  done
done
ls "$SAIDA" | wc -l
