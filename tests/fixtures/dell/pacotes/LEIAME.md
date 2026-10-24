# Pacotes Dell (DUP Linux) — `package.xml` real

Recortes de dois DUP Linux (`.BIN`) reais baixados em 2026-09-29 de `dl.dell.com`. Só o `package.xml`
embutido (UTF-16, do BOM até `</SoftwareComponent>`), sem o script nem o arquivo compactado do pacote.

| Arquivo original | URL | SHA-256 (conferido com a página da Dell) |
|---|---|---|
| `BIOS_W1WJW_LN_2.9.0.BIN` (13 249 618 bytes) | `https://dl.dell.com/FOLDER05986717M/1/BIOS_W1WJW_LN_2.9.0.BIN` | `8fa8317ff0ce0e8f7095b1d4243dbe81cebf83dc63d39766d0ffef764d9174b3` |
| `Express-Flash-PCIe-SSD_Firmware_T79D5_LN64_1.2.0_A04_01.BIN` (7 915 153 bytes) | `https://dl.dell.com/FOLDER07119916M/2/Express-Flash-PCIe-SSD_Firmware_T79D5_LN64_1.2.0_A04_01.BIN` | `0bdd0e4bfdaa5ec9ff4efd9926963e2b67cd0f6b3f2cace7c70915264e1c9c56` |

Formato observado no `.BIN` real (script `#!/bin/sh` da Dell): as linhas de variáveis citam os marcadores
(`_MANIFEST_XML_MARKER="#####Startofpackage#####"`); depois do script vem a linha `#####Startofpackage#####`,
o `package.xml` em UTF-16 e a linha `#####Startofarchive#####` seguida do `archive.tar.gz`.

- BIOS do R620: `SupportedSystems` com systemID `04CE` (R620) e `048C` (R720); `componentID` 159;
  `packageType="LLXP"`; `rebootRequired="true"`.
- SSD Express Flash: sem `SupportedSystems` (casa por dispositivo PCI/`componentID`); `rebootRequired="false"`.

Página de detalhes (`https://www.dell.com/support/home/pt-br/drivers/driversdetails?driverid=W1WJW`): mostra
MD5, SHA1 e SHA-256 de cada formato (`.EXE` de 64 e 32 bits, `.BIN`, `.BIN.sign`, `.efi`), por exemplo
`SHA-256: 8fa8317f…9174b3` para o `.BIN`.
