# Catálogo Dell (recorte real)

`Catalog_recorte_r640.xml.gz`: recorte do catálogo oficial `https://downloads.dell.com/catalog/Catalog.xml.gz`
(versão 26.09.17, baixado em 2026-09-25). Mesmo formato do original (XML UTF-16, gzip): cabeçalho
`<Manifest>` intacto e 14 `<SoftwareComponent>` inteiros, sem edição — 13 que atendem o PowerEdge R640
(systemID `0716`: BIOS, iDRAC, NICs Intel, PERC H730P/H330/H740P, BOSS-S1, algumas em várias versões) e
1 BIOS de outros modelos (R340/R240/T340/T140), para testar o filtro por modelo.

Observações do catálogo real: todos os pacotes são `packageType="LW64"` (os que o iDRAC aplica),
hash `SHA256`; o PowerEdge R410 não aparece no catálogo atual.

`Catalog_recorte_dependencia.xml.gz` (30/09/2026): 1 `<SoftwareComponent>` inteiro do catálogo oficial baixado nesse
dia (`Firmware_FF4WY_WN64_1.7_A02.EXE`), um dos 3 do catálogo com `<Dependency>` (pré-requisito: iDRAC com Lifecycle
Controller `componentID="25227"` na versão 2.40.40.40, com `DepModel systemID`). Nenhum pacote do R640 tem
`<Dependency>` nesse catálogo.

`Catalog_recorte_prerequisito.xml.gz` (15/10/2026): catálogo oficial 26.10.08 — cabeçalho `<Manifest>` e o bloco
`<Prerequisites>` com o único `<SoftwareComponent>` inteiro dele que atende servidores (iDRAC 2.40.40.40, `2091K`), sem
edição. É o único pacote do catálogo para a 13ª geração (R530 `063A`, marca "Enterprise Servers"); o R620 (`04CE`) não aparece.

Catálogos ANTIGOS (15/10/2026, `msscan/atualizacao/catalogos_dell.yaml`), recortes sem edição dos catálogos oficiais:
- `Catalog_recorte_13g_260601.xml.gz`: catálogo 26.06.01 (último com a 13ª geração) — BIOS 2.19.0, iDRAC 2.86.86.86 e dois
  pacotes que dependem dele, do R530 (`063A`).
- `Catalog_recorte_frota_13g_260601.xml.gz`: o mesmo catálogo, todos os 269 pacotes do R530 + o `<Prerequisites>` (iDRAC
  2.40.40.40, `2091K`).
- `Catalog_recorte_frota_12g_221230.xml.gz`: catálogo 22.12.30 (último com a 12ª geração), os 186 pacotes do R320 (`04F7`,
  nome no catálogo "R320/NX400").
