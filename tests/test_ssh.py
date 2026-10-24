"""CLAUDE.md 22.5: acesso SSH/SFTP ao MSscan — desligado por padrão, senha só na tela, firewall só para a rede local."""

from __future__ import annotations

import json
import re
from pathlib import Path

from msscan.interface import ssh
from msscan.interface.sessao import _formatar_ssh

RAIZ = Path(__file__).resolve().parent.parent
IP_JSON = json.dumps([
    {"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "eno3", "addr_info": [{"local": "192.168.1.85", "prefixlen": 24}]},
    {"ifname": "idrac", "addr_info": [{"local": "169.254.1.2", "prefixlen": 24}]},
])
CHAVE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0aM1Yk9Qb6rjH2v3sS7Gm1y0d4bWkqN8lZ6v2y1c3 tecnico@notebook"
JOURNAL = ("2026-10-06T14:02:11-03:00 msscan sshd-session[1234]: Accepted password for tecnico from 192.168.0.50 port 51122 ssh2\n"
           "2026-10-06T14:03:00-03:00 msscan sshd-session[1240]: Failed password for root from 192.168.0.66 port 4000 ssh2\n")


class Rodar:
    def __init__(self, falhar: str = "") -> None:
        self.comandos: list[list[str]] = []
        self.falhar = falhar

    def __call__(self, comando, timeout=None):
        self.comandos.append(list(comando))
        if comando[:2] == ["ip", "-j"]:
            return 0, IP_JSON, ""
        if comando[0] == "id":
            return 1, "", "no such user"
        if comando[0] == "journalctl":
            return 0, JOURNAL, ""
        if self.falhar and comando[0] == self.falhar:
            return 1, "", "falha simulada"
        return 0, "", ""


def _acesso(tmp_path, monkeypatch, rodar=None, chaves: str | None = None):
    monkeypatch.setattr(ssh, "CHAVES_RUNTIME", str(tmp_path / "run/authorized_keys"))
    if chaves is not None:
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config/ssh_authorized_keys").write_text(chaves)
    senhas: list[tuple[str, str]] = []
    acesso = ssh.AcessoSSH(rodar or Rodar(), str(tmp_path), definir_senha=lambda u, s: (senhas.append((u, s)), (0, ""))[1])
    return acesso, senhas


def test_funcoes_puras():
    assert [r["rede"] for r in ssh.redes_locais(IP_JSON)] == ["192.168.1.0/24"]  # sem loopback nem pass-through
    assert ssh.chaves_validas(f"{CHAVE}\ncommand=\"rm -rf /\" {CHAVE}\nlixo\n") == [CHAVE]
    assert ssh.conexoes(JOURNAL) == [{"horario": "2026-10-06T14:02:11-03:00", "ip": "192.168.0.50", "metodo": "senha"}]
    senha = ssh.gerar_senha()
    assert len(senha) == 20 and not re.search(r"[0O1lI]", senha) and senha != ssh.gerar_senha()


def test_ativar_e_desativar_sem_a_senha_em_comando_algum(tmp_path, monkeypatch):
    rodar = Rodar()
    acesso, senhas = _acesso(tmp_path, monkeypatch, rodar, chaves=CHAVE + "\n")
    assert acesso.ativar() == (True, "Acesso SSH ativo")
    senha = acesso.estado()["senha"]
    assert senhas == [("tecnico", senha)] and acesso.estado()["enderecos"] == ["192.168.1.85"] and acesso.chaves == 1
    assert ["useradd", "--create-home", "--shell", "/bin/bash", "--groups", "sudo", "tecnico"] in rodar.comandos
    assert ["ssh-keygen", "-A"] in rodar.comandos and ["systemctl", "start", "ssh.service"] in rodar.comandos
    assert ["nft", "add", "element", "inet", "msscan_quiosque", "ssh_redes", "{ 192.168.1.0/24 timeout 43200s }"] in rodar.comandos
    assert (tmp_path / "run/authorized_keys").read_text() == CHAVE + "\n"
    assert acesso.desativar()[0] and acesso.estado()["senha"] is None and not acesso.ativo
    assert ["systemctl", "stop", "ssh.service"] in rodar.comandos and ["passwd", "--lock", "tecnico"] in rodar.comandos
    assert ["nft", "delete", "element", "inet", "msscan_quiosque", "ssh_redes", "{ 192.168.1.0/24 }"] in rodar.comandos
    assert not (tmp_path / "run/authorized_keys").exists()
    assert not any(senha in " ".join(c) for c in rodar.comandos)
    relatorio = acesso.para_relatorio()
    assert relatorio[0]["conexoes"] == [{"horario": "2026-10-06T14:02:11-03:00", "ip": "192.168.0.50", "metodo": "senha"}]
    texto = _formatar_ssh(relatorio)
    assert senha not in json.dumps(relatorio) and senha not in texto and "192.168.0.50" in texto


def test_falha_ao_subir_desfaz_tudo(tmp_path, monkeypatch):
    rodar = Rodar(falhar="systemctl")
    acesso, _ = _acesso(tmp_path, monkeypatch, rodar)
    ok, mensagem = acesso.ativar()
    assert not ok and "não subiu" in mensagem and not acesso.ativo and acesso.estado()["senha"] is None
    assert ["passwd", "--lock", "tecnico"] in rodar.comandos


def test_sem_rede_local_nao_ativa(tmp_path, monkeypatch):
    acesso, senhas = _acesso(tmp_path, monkeypatch, lambda c, timeout=None: (0, "[]", ""))
    assert acesso.ativar()[0] is False and not senhas


def test_configuracao_da_iso():
    conf = (RAIZ / "iso/config/includes.chroot/etc/ssh/sshd_config.d/10-msscan.conf").read_text()
    for linha in ("PermitRootLogin no", "AllowUsers tecnico", "MaxAuthTries 3", "AuthorizedKeysFile /run/msscan/ssh/authorized_keys"):
        assert re.search(rf"^{linha}$", conf, re.M), linha
    hook = (RAIZ / "iso/config/hooks/normal/9000-msscan.hook.chroot").read_text()
    assert "systemctl disable ssh.service ssh.socket" in hook and "rm -f /etc/ssh/ssh_host_*" in hook
    assert "openssh-server" in (RAIZ / "iso/config/package-lists/msscan.list.chroot").read_text()
    nft = (RAIZ / "iso/config/includes.chroot/etc/msscan/quiosque.nft").read_text()
    assert "ip saddr @ssh_redes tcp dport 22 accept" in nft and "tcp dport 22 counter reject with tcp reset" in nft
    assert nft.index("@ssh_redes tcp dport 22 accept") < nft.index("tcp dport 22 counter reject")
    assert "ip saddr . tcp dport @repositorio_lan accept" in nft
