"""QR Code gerado localmente (só biblioteca padrão), para abrir o link de criação da chave no celular.

Implementa o necessário da norma ISO/IEC 18004: modo byte, correção de erros nível M, versões 1 a 10
(de 14 bytes na versão 1 a 213 bytes na versão 10), escolha da máscara pela
penalidade da norma. Validado contra a libqrencode (fixtures em ``tests/fixtures/qr/``). Nada é enviado
para fora: o link vira uma matriz de módulos que a interface desenha.
"""

from __future__ import annotations

VERSAO_MAXIMA = 10
NIVEL_M = 0  # bits de formato do nível M (L=1, M=0, Q=3, H=2)
# Por versão (índice 1..10), nível M: código de correção por bloco e número de blocos (norma, tabela 9).
ECC_POR_BLOCO_M = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
BLOCOS_M = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
PENALIDADES = (3, 3, 40, 10)


def _bit(valor: int, i: int) -> bool:
    return (valor >> i) & 1 != 0


def _modulos_de_dados(versao: int) -> int:
    """Módulos disponíveis para dados + correção (tudo menos padrões fixos e informações de formato/versão)."""
    resultado = (16 * versao + 128) * versao + 64
    if versao >= 2:
        alinhamentos = versao // 7 + 2
        resultado -= (25 * alinhamentos - 10) * alinhamentos - 55
        if versao >= 7:
            resultado -= 36
    return resultado


def _palavras_de_dados(versao: int) -> int:
    return _modulos_de_dados(versao) // 8 - ECC_POR_BLOCO_M[versao] * BLOCOS_M[versao]


def capacidade_bytes(versao: int) -> int:
    """Bytes que cabem em modo byte (cabeçalho: 4 bits de modo + 8 bits de tamanho, versões 1 a 9; 16 na 10)."""
    bits_tamanho = 8 if versao <= 9 else 16
    return (_palavras_de_dados(versao) * 8 - 4 - bits_tamanho) // 8


# --- Reed-Solomon sobre GF(256), polinômio 0x11D ---------------------------------------------------


def _multiplicar(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(grau: int) -> list[int]:
    resultado = [0] * (grau - 1) + [1]
    raiz = 1
    for _ in range(grau):
        for j in range(grau):
            resultado[j] = _multiplicar(resultado[j], raiz)
            if j + 1 < grau:
                resultado[j] ^= resultado[j + 1]
        raiz = _multiplicar(raiz, 0x02)
    return resultado


def _resto(dados: list[int], divisor: list[int]) -> list[int]:
    resultado = [0] * len(divisor)
    for byte in dados:
        fator = byte ^ resultado.pop(0)
        resultado.append(0)
        for i, coeficiente in enumerate(divisor):
            resultado[i] ^= _multiplicar(coeficiente, fator)
    return resultado


# --- Codificação ----------------------------------------------------------------------------------


def _codificar(dados: bytes, versao: int) -> list[int]:
    """Palavras de dados (com cabeçalho, terminador e enchimento) + correção, já intercaladas."""
    bits: list[int] = []

    def anexar(valor: int, tamanho: int) -> None:
        bits.extend((valor >> i) & 1 for i in reversed(range(tamanho)))

    anexar(0b0100, 4)
    anexar(len(dados), 8 if versao <= 9 else 16)
    for byte in dados:
        anexar(byte, 8)
    capacidade = _palavras_de_dados(versao) * 8
    anexar(0, min(4, capacidade - len(bits)))
    anexar(0, (-len(bits)) % 8)
    enchimento = 0xEC
    while len(bits) < capacidade:
        anexar(enchimento, 8)
        enchimento ^= 0xEC ^ 0x11
    palavras = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]

    blocos_total = BLOCOS_M[versao]
    ecc = ECC_POR_BLOCO_M[versao]
    brutas = _modulos_de_dados(versao) // 8
    curtos = blocos_total - brutas % blocos_total
    tamanho_curto = brutas // blocos_total
    divisor = _divisor(ecc)
    blocos, posicao = [], 0
    for i in range(blocos_total):
        tamanho = tamanho_curto - ecc + (0 if i < curtos else 1)
        bloco = palavras[posicao : posicao + tamanho]
        posicao += tamanho
        correcao = _resto(bloco, divisor)
        if i < curtos:
            bloco = bloco + [None]  # posição vazia: blocos curtos têm uma palavra de dados a menos
        blocos.append(bloco + correcao)
    resultado = []
    for i in range(len(blocos[0])):
        for j, bloco in enumerate(blocos):
            if i != tamanho_curto - ecc or j >= curtos:
                resultado.append(bloco[i])
    return resultado


class _Matriz:
    def __init__(self, versao: int) -> None:
        self.versao = versao
        self.tamanho = versao * 4 + 17
        self.modulos = [[False] * self.tamanho for _ in range(self.tamanho)]
        self.funcao = [[False] * self.tamanho for _ in range(self.tamanho)]
        self._padroes_fixos()

    def _fixo(self, x: int, y: int, escuro: bool) -> None:
        self.modulos[y][x] = escuro
        self.funcao[y][x] = True

    def _padroes_fixos(self) -> None:
        n = self.tamanho
        for i in range(n):
            self._fixo(6, i, i % 2 == 0)
            self._fixo(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        distancia = max(abs(dx), abs(dy))
                        self._fixo(x, y, distancia not in (2, 4))
        posicoes = self._alinhamentos()
        for i, cx in enumerate(posicoes):
            for j, cy in enumerate(posicoes):
                if (i, j) in ((0, 0), (0, len(posicoes) - 1), (len(posicoes) - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._fixo(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self._formato(0, NIVEL_M)  # reserva as posições (o valor certo vem depois da máscara)
        if self.versao >= 7:
            resto = self.versao
            for _ in range(12):
                resto = (resto << 1) ^ ((resto >> 11) * 0x1F25)
            bits = self.versao << 12 | resto
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self._fixo(a, b, _bit(bits, i))
                self._fixo(b, a, _bit(bits, i))

    def _alinhamentos(self) -> list[int]:
        if self.versao == 1:
            return []
        quantidade = self.versao // 7 + 2
        passo = (self.versao * 8 + quantidade * 3 + 5) // (quantidade * 4 - 4) * 2
        return list(reversed([self.tamanho - 7 - i * passo for i in range(quantidade - 1)] + [6]))

    def _formato(self, mascara: int, nivel: int) -> None:
        dados = nivel << 3 | mascara
        resto = dados
        for _ in range(10):
            resto = (resto << 1) ^ ((resto >> 9) * 0x537)
        bits = (dados << 10 | resto) ^ 0x5412
        n = self.tamanho
        for i in range(6):
            self._fixo(8, i, _bit(bits, i))
        self._fixo(8, 7, _bit(bits, 6))
        self._fixo(8, 8, _bit(bits, 7))
        self._fixo(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._fixo(14 - i, 8, _bit(bits, i))
        for i in range(8):
            self._fixo(n - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._fixo(8, n - 15 + i, _bit(bits, i))
        self._fixo(8, n - 8, True)

    def dados(self, palavras: list[int]) -> None:
        n, i = self.tamanho, 0
        direita = n - 1
        while direita >= 1:
            if direita == 6:
                direita = 5
            for vertical in range(n):
                for j in range(2):
                    x = direita - j
                    subindo = ((direita + 1) & 2) == 0
                    y = n - 1 - vertical if subindo else vertical
                    if not self.funcao[y][x] and i < len(palavras) * 8:
                        self.modulos[y][x] = _bit(palavras[i >> 3], 7 - (i & 7))
                        i += 1
            direita -= 2

    def mascarar(self, mascara: int) -> None:
        condicoes = (
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        )
        condicao = condicoes[mascara]
        for y in range(self.tamanho):
            for x in range(self.tamanho):
                if not self.funcao[y][x] and condicao(x, y):
                    self.modulos[y][x] = not self.modulos[y][x]

    def penalidade(self) -> int:
        n, m = self.tamanho, self.modulos
        total = 0
        for linhas in (m, [list(coluna) for coluna in zip(*m)]):
            for linha in linhas:
                corrida = 1
                for i in range(1, n + 1):
                    if i < n and linha[i] == linha[i - 1]:
                        corrida += 1
                        continue
                    if corrida >= 5:
                        total += PENALIDADES[0] + corrida - 5
                    corrida = 1
                texto = "".join("1" if v else "0" for v in linha)
                for padrao in ("00001011101", "10111010000"):
                    inicio = texto.find(padrao)
                    while inicio != -1:
                        total += PENALIDADES[2]
                        inicio = texto.find(padrao, inicio + 1)
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    total += PENALIDADES[1]
        escuros = sum(map(sum, m))
        k = (abs(escuros * 20 - n * n * 10) + n * n - 1) // (n * n) - 1
        return total + max(k, 0) * PENALIDADES[3]


def gerar(texto: str, mascara: int | None = None) -> list[list[bool]] | None:
    """Matriz do QR Code (``True`` = módulo escuro), sem a margem. ``None`` se não couber na versão 10."""
    dados = texto.encode("utf-8")
    versao = next((v for v in range(1, VERSAO_MAXIMA + 1) if len(dados) <= capacidade_bytes(v)), None)
    if versao is None:
        return None
    palavras = _codificar(dados, versao)
    melhor: tuple[int, list[list[bool]]] | None = None
    for candidata in range(8) if mascara is None else (mascara,):
        matriz = _Matriz(versao)
        matriz.dados(palavras)
        matriz.mascarar(candidata)
        matriz._formato(candidata, NIVEL_M)
        pontos = matriz.penalidade()
        if melhor is None or pontos < melhor[0]:
            melhor = (pontos, matriz.modulos)
    return melhor[1] if melhor else None


def para_linhas(matriz: list[list[bool]]) -> list[str]:
    """Matriz como texto ("1" escuro, "0" claro), uma linha por fileira — o formato da API."""
    return ["".join("1" if v else "0" for v in linha) for linha in matriz]
