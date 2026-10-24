"""Acesso SSH/SFTP ao MSscan (CLAUDE.md 22.5), DESLIGADO por padrão.

Ligado pelo botão "Ativar acesso SSH" (aba Terminal) ou pelo parâmetro de boot ``msscan.ssh=1``:

- usuário ``tecnico`` (nunca root: ``PermitRootLogin no``, ``AllowUsers tecnico`` em
  ``/etc/ssh/sshd_config.d/10-msscan.conf``), com uma senha aleatória forte gerada na hora, válida só nesta sessão: fica só
  na memória e na tela (nunca em disco, logs, relatórios nem no histórico de comandos: vai pelo stdin do ``chpasswd``);
- alternativa sem senha: as chaves públicas de ``MSSCAN/config/ssh_authorized_keys``, se existir;
- chaves do servidor geradas a cada ativação (a ISO sai sem elas: cada boot tem as suas);
- porta 22 aberta no firewall do quiosque só enquanto ativo e só para a(s) rede(s) local(is) do servidor (conjunto
  ``ssh_redes`` com timeout); limite de tentativas no sshd (``MaxAuthTries``, ``PerSourcePenalties`` do OpenSSH 10);
- desliga sozinho ao reiniciar ou desligar (o serviço não sobe no boot e o sistema live vive na RAM);
- relatório: quando foi ativado/desativado e o IP de quem conectou (journal do sshd), nunca a senha.
SFTP pelo mesmo acesso (``Subsystem sftp`` padrão do Debian).
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

USUARIO = "tecnico"
PORTA = 22
CHAVES_RUNTIME = "/run/msscan/ssh/authorized_keys"
"""``AuthorizedKeysFile`` do sshd (no drop-in da ISO): cópia, na RAM, das chaves do pendrive."""
ARQUIVO_CHAVES = "config/ssh_authorized_keys"
VALIDADE_FIREWALL_S = 12 * 3600
ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
"""Sem caracteres ambíguos (0/O, 1/l/I): a senha é lida na tela e digitada em outro computador."""
TAMANHO_SENHA = 20
_CHAVE_PUBLICA = re.compile(r"(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) "
                            r"[A-Za-z0-9+/=]{40,}( [^\r\n]{0,200})?")  # fmt: skip
_ACEITO = re.compile(r"Accepted (password|publickey) for (\S+) from (\S+) port (\d+)")


def gerar_senha() -> str:
    return "".join(secrets.choice(ALFABETO) for _ in range(TAMANHO_SENHA))


def chaves_validas(texto: str) -> list[str]:
    """Só linhas que são chaves públicas OpenSSH (sem opções como ``command=``). Função pura."""
    return [linha.strip() for linha in (texto or "").splitlines() if _CHAVE_PUBLICA.fullmatch(linha.strip())]


def redes_locais(saida_ip_json: str) -> list[dict[str, str]]:
    """``ip -j -4 addr show`` → ``[{"interface", "ip", "rede"}]`` das redes do servidor (sem loopback e sem o link-local do
    pass-through). Função pura."""
    try:
        interfaces = json.loads(saida_ip_json or "[]")
    except ValueError:
        return []
    redes = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        for endereco in interface.get("addr_info") or []:
            try:
                ip = ipaddress.ip_interface(f"{endereco.get('local')}/{endereco.get('prefixlen')}")
            except ValueError:
                continue
            if ip.version == 4 and not ip.ip.is_loopback and not ip.ip.is_link_local:
                redes.append({"interface": str(interface.get("ifname") or "?"), "ip": str(ip.ip), "rede": str(ip.network)})
    return redes


def conexoes(saida_journal: str) -> list[dict[str, str]]:
    """Linhas ``Accepted ...`` do journal do sshd (``-o short-iso``) → ``[{"horario", "ip", "metodo"}]``. Função pura."""
    vistas = []
    for linha in (saida_journal or "").splitlines():
        casado = _ACEITO.search(linha)
        if casado and casado.group(2) == USUARIO:
            vistas.append({"horario": linha.split(" ", 1)[0], "ip": casado.group(3),
                           "metodo": "senha" if casado.group(1) == "password" else "chave pública"})  # fmt: skip
    return vistas


def _definir_senha(usuario: str, senha: str) -> tuple[int, str]:
    """``chpasswd`` com a senha pelo stdin: nunca na linha de comando nem no histórico do executor."""
    try:
        processo = subprocess.run(["chpasswd"], input=f"{usuario}:{senha}\n", text=True, capture_output=True, timeout=30)
        return processo.returncode, processo.stderr.strip()
    except (OSError, subprocess.SubprocessError) as erro:
        return -1, str(erro)


class AcessoSSH:
    """Estado do acesso SSH desta sessão. ``rodar`` = ``Executor.rodar`` (comandos sem segredo, vão para o histórico)."""

    def __init__(self, rodar: Callable[..., tuple[int, str, str]], pasta_dados: str | None,
                 definir_senha: Callable[[str, str], tuple[int, str]] = _definir_senha, firewall: bool = True) -> None:  # fmt: skip
        self.rodar, self.pasta_dados, self.definir_senha, self.firewall = rodar, pasta_dados, definir_senha, firewall
        self.ativo = False
        self.senha: str | None = None
        self.redes: list[dict[str, str]] = []
        self.chaves = 0
        self.mensagem = ""
        self.historico: list[dict[str, Any]] = []
        """Para o relatório: ``{"ativado", "desativado", "redes", "chaves", "conexoes"}`` por ativação (sem senha)."""

    def _passo(self, comando: list[str]) -> str:
        rc, _, erro = self.rodar(comando, timeout=60)
        return "" if rc == 0 else f"{' '.join(comando[:3])}: {(erro or '').strip() or rc}"

    def ativar(self, origem: str = "botão") -> tuple[bool, str]:
        if self.ativo:
            return True, "acesso SSH já está ativo"
        rc, saida, erro = self.rodar(["ip", "-j", "-4", "addr", "show"], timeout=10)
        self.redes = redes_locais(saida) if rc == 0 else []
        if not self.redes:
            return False, "este servidor não tem IP na rede local: não há por onde conectar"
        if self.rodar(["id", USUARIO], timeout=10)[0] != 0:
            falha = self._passo(["useradd", "--create-home", "--shell", "/bin/bash", "--groups", "sudo", USUARIO])
            if falha:
                return False, f"não foi possível criar o usuário {USUARIO} ({falha})"
        senha = gerar_senha()
        rc, erro = self.definir_senha(USUARIO, senha)
        if rc != 0:
            return False, f"não foi possível definir a senha da sessão ({erro or rc})"
        self.chaves = self._copiar_chaves()
        for comando in (["ssh-keygen", "-A"], ["systemctl", "start", "ssh.service"]):
            falha = self._passo(comando)
            if falha:
                self._parar_servico()
                return False, f"o servidor SSH não subiu ({falha})"
        if self.firewall:
            for rede in self.redes:
                falha = self._passo(["nft", "add", "element", "inet", "msscan_quiosque", "ssh_redes",
                                     f"{{ {rede['rede']} timeout {VALIDADE_FIREWALL_S}s }}"])  # fmt: skip
                if falha:
                    self._parar_servico()
                    return False, f"não foi possível abrir a porta {PORTA} no firewall ({falha})"
        self.ativo, self.senha = True, senha
        self.historico.append({"ativado": datetime.now().isoformat(timespec="seconds"), "origem": origem, "desativado": None,
                               "redes": [r["rede"] for r in self.redes], "chaves": self.chaves, "conexoes": []})  # fmt: skip
        self.mensagem = "Acesso SSH ativo"
        return True, self.mensagem

    def _copiar_chaves(self) -> int:
        """Chaves públicas do pendrive → ``CHAVES_RUNTIME`` (só leitura, dono root). Arquivo ausente ou vazio: 0."""
        texto = ""
        if self.pasta_dados:
            try:
                texto = (Path(self.pasta_dados) / ARQUIVO_CHAVES).read_text(encoding="utf-8", errors="replace")
            except OSError:
                texto = ""
        chaves = chaves_validas(texto)
        destino = Path(CHAVES_RUNTIME)
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text("".join(c + "\n" for c in chaves), encoding="utf-8")
            destino.chmod(0o644)
        except OSError:
            return 0
        return len(chaves)

    def _parar_servico(self) -> None:
        self.rodar(["systemctl", "stop", "ssh.service"], timeout=60)
        self.rodar(["pkill", "-KILL", "-u", USUARIO], timeout=10)  # encerra sessões abertas
        self.rodar(["passwd", "--lock", USUARIO], timeout=10)
        if self.firewall:
            for rede in self.redes:
                self.rodar(["nft", "delete", "element", "inet", "msscan_quiosque", "ssh_redes", f"{{ {rede['rede']} }}"], timeout=10)
        try:
            Path(CHAVES_RUNTIME).unlink(missing_ok=True)
        except OSError:
            pass

    def desativar(self) -> tuple[bool, str]:
        if not self.ativo:
            return True, "acesso SSH já estava desligado"
        self.atualizar_conexoes()
        self._parar_servico()
        self.ativo, self.senha = False, None
        if self.historico:
            self.historico[-1]["desativado"] = datetime.now().isoformat(timespec="seconds")
        self.mensagem = "Acesso SSH desativado"
        return True, self.mensagem

    def atualizar_conexoes(self) -> None:
        """Quem conectou desde a ativação (journal do sshd) → relatório."""
        if not self.historico:
            return
        desde = self.historico[-1]["ativado"].replace("T", " ")
        rc, saida, _ = self.rodar(["journalctl", "--unit", "ssh.service", "--since", desde, "--output", "short-iso",
                                   "--no-pager", "--quiet"], timeout=20)  # fmt: skip
        if rc == 0:
            self.historico[-1]["conexoes"] = conexoes(saida)

    def estado(self) -> dict[str, Any]:
        """Para a tela (a senha só aparece aqui, enquanto ativo)."""
        return {"ativo": self.ativo, "usuario": USUARIO, "porta": PORTA, "senha": self.senha if self.ativo else None,
                "enderecos": [r["ip"] for r in self.redes] if self.ativo else [], "chaves": self.chaves,
                "mensagem": self.mensagem, "conexoes": (self.historico[-1]["conexoes"] if self.historico else [])}  # fmt: skip

    def para_relatorio(self) -> list[dict[str, Any]]:
        """Sem senha: ativações, redes liberadas e quem conectou (horário e IP)."""
        return [dict(h) for h in self.historico]


__all__ = ["AcessoSSH", "chaves_validas", "conexoes", "gerar_senha", "redes_locais"]
