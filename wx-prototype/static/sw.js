/* Greece Sky Weather - service worker.
 *
 * Scope: the whole site (served from `/sw.js`, not `/static/sw.js`, so the
 * default scope is `/` and not `/static/`).
 *
 * This worker does one job: receive a push and show it. It deliberately does no
 * offline caching - the forecast is live data and a stale cached page would be
 * worse than a network error, so no `fetch` handler is installed at all.
 */

self.addEventListener('install', (event) => {
  // Activate immediately rather than waiting for every tab to close.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'Ειδοποίηση', body: event.data ? event.data.text() : '' };
  }
  const title = data.title || 'Greece Sky and Weather';
  const options = {
    body: data.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    // `tag` is the server's event key. Two pushes for the same event replace
    // each other instead of stacking, which is what keeps an at-least-once
    // retry from producing two visible notifications.
    tag: data.tag || undefined,
    renotify: false,
    data: { url: data.url || '/', severity: data.severity || 'warn' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of all) {
      if ('focus' in client) {
        await client.focus();
        return;
      }
    }
    if (self.clients.openWindow) {
      await self.clients.openWindow(target);
    }
  })());
});

/* Some browsers rotate a subscription and fire this instead of silently
 * dropping it. The page is told so it can re-subscribe with the same identity;
 * the server endpoint is the only writer of subscription state. */
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of all) {
      client.postMessage({ type: 'pushsubscriptionchange' });
    }
  })());
});
