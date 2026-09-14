// Service worker minimo -- solo lo necesario para que el navegador
// ofrezca "Instalar app" (PWA instalable). NO cachea nada del panel a
// proposito: los datos de trading cambian todo el tiempo, un service
// worker que cachee agresivo mostraria datos viejos. Cada request sigue
// yendo a la red como si no hubiera service worker; existir y estar
// activo es lo unico que hace falta para la instalabilidad.
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  self.clients.claim();
});

self.addEventListener("fetch", () => {
  // sin cache -- deja pasar todo a la red tal cual.
});
