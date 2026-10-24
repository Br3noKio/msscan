# Saídas reais do `xrandr --props`

Capturadas dentro da ISO v0.8.0 (Xorg + driver modesetting, kernel 6.12 do trixie) no QEMU 10.0,
com a interface em quiosque no ar (`DISPLAY=:0 XAUTHORITY=/run/msscan/Xauthority xrandr --props`),
em 2026-09-25. O EDID é o monitor virtual do QEMU ("QEMU Monitor"), sem dado identificável.

| Arquivo | Vídeo do QEMU | Driver DRM |
|---|---|---|
| `props_edid1080.txt` | `-device VGA,edid=on,xres=1920,yres=1080` | bochs-drm |
| `props_edid1280.txt` | `-device VGA,edid=on,xres=1280,yres=1024` | bochs-drm |
| `props_semedid.txt` | `-device VGA,edid=off` (sem EDID: o driver marca 1024x768 como preferido) | bochs-drm |
| `props_cirrus.txt` | `-vga cirrus` (sem EDID, máximo 2044x1024: vídeo antigo e limitado) | cirrus |
