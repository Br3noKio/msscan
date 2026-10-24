# QR Codes de referência

Matrizes geradas pela **libqrencode 4.1.1** (pacote Debian `qrencode` 4.1.1-2, trixie), a biblioteca de
referência de QR Code no Linux, em 28/09/2026:

    qrencode -t ASCII -m 0 -l M -8 "<texto>"

(`-l M`: correção nível M; `-8`: modo byte; `-m 0`: sem margem). Cada `#` duplo virou `1` e cada par de
espaços virou `0`. O pacote foi só extraído (`dpkg-deb -x`) numa pasta temporária para gerar estes
arquivos; ele NÃO faz parte da ISO nem é dependência do MSscan.

`msscan/qr.py` precisa reproduzir exatamente estas matrizes quando usa a mesma máscara (lida dos bits
de formato). A escolha automática de máscara pode diferir (as bibliotecas calculam a penalidade de formas
ligeiramente diferentes); qualquer máscara gera um QR válido.
