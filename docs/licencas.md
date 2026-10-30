# Licenças e créditos de terceiros

Licença do próprio MSscan: **proprietária, código fechado**; só a ISO é distribuída ([LICENSE](../LICENSE)).
Os componentes abaixo continuam sob as suas próprias licenças.

## Usados pelo MSscan

| Componente | Uso | Licença | Origem |
|---|---|---|---|
| Debian GNU/Linux 13 (trixie) | sistema base da ISO | várias (ver [lista da ISO](licencas-iso.md)) | pacotes Debian |
| Python 3, `requests`, `PyYAML`, `packaging` | programa | PSF, Apache-2.0, MIT, Apache-2.0/BSD-2-Clause | pacotes Debian |
| three.js r111 | modelo 3D do servidor | MIT | pacote Debian `libjs-three` |
| xterm.js | terminal embutido | MIT | pacote Debian `node-xterm` |
| Firefox ESR | navegador do quiosque e navegador integrado | MPL-2.0 | pacote Debian `firefox-esr` |
| Lucide | ícones | ISC (partes do Feather, MIT) | incorporados em `msscan/interface/estaticos/icones.js` |
| Barlow e Barlow Condensed | fontes da interface | SIL Open Font License 1.1 | `msscan/interface/estaticos/fontes/` (com o `OFL-Barlow.txt`) |
| JetBrains Mono | fonte de dados e números | SIL Open Font License 1.1 | pacote Debian `fonts-jetbrains-mono` |
| Logotipo MSServer | cabeçalho da interface | marca da MSServer, uso exclusivo | `msscan/interface/estaticos/logo-msserver.png` |

Os textos das licenças MIT, ISC e OFL também aparecem na aba **Sobre** da interface.

## Fora do repositório e da ISO

- **Ferramentas dos fabricantes** (Dell iDRAC Tools/racadm, `perccli64`, `storcli64`): têm licença própria e não são
  redistribuídas. O técnico copia para `MSSCAN/vendor/` ou o MSscan baixa do site oficial da Dell quando necessário.
- **Pacotes de firmware**: baixados na hora do site oficial do fabricante, com a verificação de integridade (SHA-256) do
  catálogo; nunca ficam no repositório nem na ISO.
- **Catálogo de atualizações da Dell**: lido do site oficial a cada verificação.

## Componentes da ISO

A lista completa dos pacotes da ISO, com as licenças declaradas, está em [licencas-iso.md](licencas-iso.md), gerada por
`iso/licencas.py` a partir do build. O texto integral de cada licença fica na própria ISO, em
`/usr/share/doc/<pacote>/copyright`.
