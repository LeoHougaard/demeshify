self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const requestedUrl = new URL(
      event.notification.data?.url || self.registration.scope,
      self.location.origin,
    );
    const targetUrl = requestedUrl.origin === self.location.origin
      ? requestedUrl.href
      : self.registration.scope;
    const windowClients = await self.clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });
    for (const client of windowClients) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      if (client.url !== targetUrl) await client.navigate(targetUrl);
      return client.focus();
    }
    return self.clients.openWindow(targetUrl);
  })());
});
