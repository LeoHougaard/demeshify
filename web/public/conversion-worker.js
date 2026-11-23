self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const requestedUrl = new URL(
      event.notification.data?.url || self.registration.scope,
      self.location.origin,
    );
    const targetUrl = requestedUrl.href.startsWith(self.registration.scope)
      ? requestedUrl.href
      : self.registration.scope;
    const windowClients = await self.clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });
    for (const client of windowClients) {
      if (client.url !== targetUrl) continue;
      return client.focus();
    }
    return self.clients.openWindow(targetUrl);
  })());
});
