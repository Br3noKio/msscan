#!/bin/sh
# Papel de parede da interface: SVG original → JPEG nítido e JPEG JÁ DESFOCADO (em msscan/interface/estaticos/).
# Ferramentas da Debian: chromium (sem tela) e python3-pil.
set -eu
RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
python3 "$RAIZ/docs/design/gerar_fundo.py" > "$TMP/fundo.svg"
cp "$TMP/fundo.svg" "$RAIZ/docs/design/fundo.svg"
printf '<html><body style="margin:0"><img src="fundo.svg" width="1920" height="1080"></body></html>' > "$TMP/f.html"
chromium --headless --no-sandbox --disable-gpu --hide-scrollbars --window-size=1920,1080 --screenshot="$TMP/fundo.png" "file://$TMP/f.html" >/dev/null 2>&1
python3 -I - "$TMP/fundo.png" "$RAIZ/msscan/interface/estaticos" <<'EOF'
import sys
from PIL import Image, ImageEnhance, ImageFilter
origem, destino = sys.argv[1], sys.argv[2]
imagem = Image.open(origem).convert("RGB")
imagem.save(f"{destino}/fundo.jpg", quality=84, optimize=True, progressive=True)
# Desfoque pronto: reduz, desfoca e escurece (o painel translúcido fica por cima; contraste do texto garantido no tema).
pequena = imagem.resize((480, 270), Image.LANCZOS).filter(ImageFilter.GaussianBlur(9))
desfocada = ImageEnhance.Brightness(pequena.resize((1920, 1080), Image.BICUBIC).filter(ImageFilter.GaussianBlur(6))).enhance(0.8)
desfocada.save(f"{destino}/fundo-desfocado.jpg", quality=80, optimize=True, progressive=True)
EOF
ls -la "$RAIZ/msscan/interface/estaticos/"fundo*.jpg
