// MSscan — preferências do Firefox ESR no modo quiosque (perfil temporário em /run).
// A página inicial (com o token da sessão) é acrescentada pelo msscan-iniciar.
// Objetivo: menos conexões externas (o bloqueio de fato é o nftables), nenhuma tela de
// boas-vindas, renderização por software.

// Primeira execução e avisos
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.page", 1);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.sessionstore.max_resumed_crashes", 0);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.warnOnQuit", false);
user_pref("browser.rights.3.shown", true);
user_pref("datareporting.policy.firstRunURL", "");
user_pref("browser.translations.enabled", false);
user_pref("browser.newtabpage.enabled", false);
user_pref("browser.newtabpage.activity-stream.feeds.section.topstories", false);
user_pref("extensions.pocket.enabled", false);
user_pref("identity.fxaccounts.enabled", false);
user_pref("dom.webnotifications.enabled", false);
user_pref("geo.enabled", false);

// Nenhuma conexão para fora (a interface é 100% local)
user_pref("app.update.enabled", false);
user_pref("app.update.auto", false);
user_pref("app.normandy.enabled", false);
user_pref("app.shield.optoutstudies.enabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("toolkit.telemetry.unified", false);
user_pref("toolkit.telemetry.archive.enabled", false);
user_pref("browser.ping-centre.telemetry", false);
user_pref("browser.safebrowsing.malware.enabled", false);
user_pref("browser.safebrowsing.phishing.enabled", false);
user_pref("browser.safebrowsing.downloads.enabled", false);
user_pref("browser.safebrowsing.downloads.remote.enabled", false);
user_pref("browser.safebrowsing.provider.google4.updateURL", "");
user_pref("browser.safebrowsing.provider.google.updateURL", "");
user_pref("browser.search.update", false);
user_pref("browser.region.update.enabled", false);
user_pref("browser.region.network.url", "");
user_pref("extensions.update.enabled", false);
user_pref("extensions.getAddons.cache.enabled", false);
user_pref("extensions.systemAddon.update.enabled", false);
user_pref("media.gmp-manager.updateEnabled", false);
user_pref("media.gmp-gmpopenh264.enabled", false);
user_pref("network.captive-portal-service.enabled", false);
user_pref("network.connectivity-service.enabled", false);
user_pref("network.prefetch-next", false);
user_pref("network.dns.disablePrefetch", true);
user_pref("network.http.speculative-parallel-limit", 0);
user_pref("browser.urlbar.suggest.searches", false);
user_pref("browser.search.suggest.enabled", false);
user_pref("security.remote_settings.crlite_filters.enabled", false);
user_pref("dom.push.enabled", false);
user_pref("dom.push.connection.enabled", false);
user_pref("network.trr.mode", 5);
user_pref("security.OCSP.enabled", 0);
user_pref("security.certerrors.mitm.priming.enabled", false);
user_pref("security.remote_settings.intermediates.enabled", false);
user_pref("extensions.blocklist.enabled", false);
user_pref("browser.discovery.enabled", false);
user_pref("browser.topsites.contile.enabled", false);
user_pref("browser.newtabpage.activity-stream.feeds.topsites", false);
user_pref("browser.newtabpage.activity-stream.showSponsored", false);
user_pref("browser.newtabpage.activity-stream.showSponsoredTopSites", false);
user_pref("browser.urlbar.quicksuggest.enabled", false);
user_pref("messaging-system.rsexperimentloader.enabled", false);
user_pref("media.gmp-widevinecdm.enabled", false);
user_pref("captivedetect.canonicalURL", "");

// Tudo que não for 127.0.0.1/localhost vai para um "proxy" que não existe (porta 9 fechada).
// Testado: NÃO basta (alguns serviços internos da Mozilla ainda saem). A garantia real é a regra
// de firewall /etc/msscan/quiosque.nft (usuário msscan-ui só alcança 127.0.0.1/::1).
// O navegador só precisa falar com o MSscan local; quem acessa a internet é o Python.
user_pref("network.proxy.type", 1);
user_pref("network.proxy.http", "127.0.0.1");
user_pref("network.proxy.http_port", 9);
user_pref("network.proxy.share_proxy_settings", true);
user_pref("network.proxy.ssl", "127.0.0.1");
user_pref("network.proxy.ssl_port", 9);
user_pref("network.proxy.socks", "127.0.0.1");
user_pref("network.proxy.socks_port", 9);
user_pref("network.proxy.socks_remote_dns", true);
user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");
user_pref("network.proxy.allow_hijacking_localhost", false);
// Sem isto, serviços internos do Firefox "caem" para conexão direta quando o proxy não responde.
user_pref("network.proxy.failover_direct", false);
user_pref("services.settings.server", "http://127.0.0.1:9/v1");

// Teclado: "/" e "'" são atalhos da própria interface, não a busca rápida do navegador;
// Alt não abre a barra de menus (não há barra no quiosque).
user_pref("accessibility.typeaheadfind", false);
user_pref("accessibility.typeaheadfind.manual", false);
user_pref("ui.key.menuAccessKeyFocuses", false);

// Vídeo de servidor (Matrox G200, ASPEED, ES1000): sem aceleração 3D; renderização por software.
user_pref("gfx.webrender.software", true);
user_pref("layers.acceleration.disabled", true);
user_pref("media.hardware-video-decoding.enabled", false);

// Sistema live em RAM: sem cache em disco
user_pref("browser.cache.disk.enable", false);
