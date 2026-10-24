"""Credenciais do BMC digitadas na hora pelo técnico (CLAUDE.md, seção 6b).

- Pergunta só quando há Redfish confirmado; "Pular" é sempre possível e normal.
- Validação na hora (sessão Redfish); no máximo 3 tentativas.
- Nunca há credencial fixa, padrão ou salva; nada é tentado automaticamente.
- A senha só existe durante ``tentar`` (não é guardada); o token fica no cliente, em memória.
"""

from __future__ import annotations

import getpass
from collections.abc import Callable
from typing import Any

from msscan.redfish.cliente import ClienteRedfish

MAX_TENTATIVAS = 3
EXPLICACAO = "O Redfish traz informações mais completas de fontes, sensores, armazenamento (RAID e discos) e placas de rede."
AVISO_BLOQUEIO = (
    "Limite de 3 tentativas atingido: seguindo sem Redfish. Mais tentativas podem bloquear o acesso ao BMC "
    "(iDRAC/iLO bloqueiam o endereço após várias senhas erradas). O diagnóstico continua por IPMI, "
    "ferramentas do fabricante e sistema operacional."
)
MENSAGEM_PULADO = "Credenciais do BMC não informadas pelo técnico"

# Situações
NAO_PERGUNTADO = "nao_perguntado"
SEM_REDFISH = "sem_redfish"
PULADO = "pulado"
VALIDADO = "validado"
ESGOTADO = "esgotado"

PERFIS = {
    "Administrator": "administrador",
    "Operator": "operador (sem permissão de administrador)",
    "ReadOnly": "somente leitura",
}


def descrever_perfil(papel: str | None, privilegios: list[str]) -> tuple[str, bool | None]:
    """``(texto do perfil, é administrador?)``; ``None`` quando o BMC não informou."""
    if papel in PERFIS:
        return PERFIS[papel], papel == "Administrator"
    if privilegios:
        admin = {"ConfigureManager", "ConfigureComponents", "ConfigureUsers"} <= set(privilegios)
        if admin:
            return f"administrador (papel '{papel}')", True
        if set(privilegios) <= {"Login"}:
            return f"somente leitura (papel '{papel}')", False
        return f"papel '{papel}' (privilégios: {', '.join(privilegios)})", False
    if papel:
        return f"papel '{papel}'", None
    return "perfil não informado pelo BMC", None


class AcessoBMC:
    """Estado do acesso Redfish de uma execução do MSscan."""

    def __init__(self, cliente: ClienteRedfish | None, contas: str | None = None) -> None:
        self.cliente = cliente
        self.contas = contas
        self.situacao = NAO_PERGUNTADO if cliente else SEM_REDFISH
        self.tentativas = 0
        self.perfil = ""
        self.administrador: bool | None = None
        self.mensagem = "" if cliente else "BMC com Redfish não detectado"
        self.historico: list[str] = []

    @property
    def pode_tentar(self) -> bool:
        return self.cliente is not None and self.situacao != ESGOTADO and self.tentativas < MAX_TENTATIVAS

    @property
    def validado(self) -> bool:
        return self.situacao == VALIDADO and self.cliente is not None and self.cliente.autenticado

    def pular(self) -> str:
        """O técnico preferiu não informar credenciais (normal: o diagnóstico usa o fallback)."""
        if self.situacao != VALIDADO:
            self.situacao = PULADO if self.situacao != ESGOTADO else ESGOTADO
            self.mensagem = MENSAGEM_PULADO
        return "Sem credenciais do BMC: o diagnóstico continua por IPMI, ferramentas do fabricante e sistema operacional."

    def tentar(self, usuario: str, senha: str) -> str:
        """Valida as credenciais abrindo uma sessão Redfish. Devolve a mensagem para o técnico."""
        if not self.pode_tentar or self.cliente is None:
            return AVISO_BLOQUEIO if self.situacao == ESGOTADO else "Redfish indisponível."
        self.tentativas += 1
        resultado = self.cliente.login(usuario, senha)
        del senha
        if resultado.ok:
            papel, privilegios = self.cliente.descobrir_papel(usuario, self.contas)
            self.perfil, self.administrador = descrever_perfil(papel, privilegios)
            self.situacao = VALIDADO
            self.mensagem = f"Credenciais validadas: sessão Redfish aberta (perfil: {self.perfil})."
        elif resultado.status not in (400, 401, 403):
            # A senha nem foi conferida (405, 404, timeout...): sem risco de bloqueio, não gasta tentativa.
            self.tentativas -= 1
            self.mensagem = f"{resultado.mensagem}: as credenciais não chegaram a ser conferidas (não conta como tentativa)."
        elif self.tentativas >= MAX_TENTATIVAS:
            self.situacao = ESGOTADO
            self.mensagem = f"Tentativa {self.tentativas} de {MAX_TENTATIVAS}: {resultado.mensagem}. {AVISO_BLOQUEIO}"
        else:
            restantes = MAX_TENTATIVAS - self.tentativas
            self.mensagem = (
                f"Tentativa {self.tentativas} de {MAX_TENTATIVAS}: {resultado.mensagem}. "
                f"Restam {restantes} tentativa(s); também é possível pular."
            )
        self.historico.append(self.mensagem)
        return self.mensagem

    def encerrar(self) -> bool:
        """Logout da sessão Redfish (chamado ao terminar). Nunca levanta exceção."""
        try:
            return bool(self.cliente and self.cliente.logout())
        except Exception:  # noqa: BLE001
            return False

    def para_dict(self) -> dict[str, Any]:
        """Estado serializável (sem usuário, senha nem token)."""
        return {
            "situacao": self.situacao,
            "endereco": self.cliente.base if self.cliente else None,
            "tentativas": self.tentativas,
            "max_tentativas": MAX_TENTATIVAS,
            "perfil": self.perfil,
            "administrador": self.administrador,
            "mensagem": self.mensagem,
            "explicacao": EXPLICACAO,
        }


def perguntar_credenciais(
    acesso: AcessoBMC,
    entrada: Callable[[str], str] = input,
    ler_senha: Callable[[str], str] = getpass.getpass,
    saida: Callable[[str], Any] = print,
) -> None:
    """Modo texto: pergunta usuário e senha (mascarada); usuário em branco + Enter = Pular."""
    if acesso.cliente is None or acesso.validado:
        return
    saida(f"\nBMC com Redfish detectado em {acesso.cliente.base}. {EXPLICACAO}")
    saida("Informe usuário e senha do BMC (iDRAC, iLO, XCC...). Nada é salvo: a senha fica só na memória.")
    saida("Para PULAR, deixe o usuário em branco e pressione Enter (o diagnóstico continua sem Redfish).")
    while acesso.pode_tentar:
        try:
            usuario = entrada(f"Usuário do BMC (tentativa {acesso.tentativas + 1} de {MAX_TENTATIVAS}; Enter = Pular): ").strip()
            if not usuario:
                saida("  " + acesso.pular())
                return
            senha = ler_senha("Senha do BMC (não aparece na tela): ")
        except (EOFError, KeyboardInterrupt):
            saida("\n  " + acesso.pular())
            return
        saida("  Validando no BMC...")
        mensagem = acesso.tentar(usuario, senha)
        del senha
        saida(("  √ " if acesso.validado else "  ") + mensagem)
        if acesso.validado:
            return
