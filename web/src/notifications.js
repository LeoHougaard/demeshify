export function createConversionNotifier(environment = globalThis) {
  const NotificationApi = environment.Notification;
  let workerRegistration;

  const getWorkerRegistration = () => {
    if (!environment.navigator?.serviceWorker) return Promise.resolve(null);
    workerRegistration ||= environment.navigator.serviceWorker
      .register("/conversion-worker.js")
      .catch(() => null);
    return workerRegistration;
  };

  return {
    async requestPermission() {
      if (!NotificationApi) return "unsupported";
      try {
        const permission = NotificationApi.permission === "default"
          ? await NotificationApi.requestPermission()
          : NotificationApi.permission;
        if (permission === "granted") void getWorkerRegistration();
        return permission;
      } catch {
        return NotificationApi.permission || "denied";
      }
    },

    async notifyComplete({ runId, fileName }) {
      if (!environment.document?.hidden || NotificationApi?.permission !== "granted") {
        return false;
      }
      const resultUrl = new URL(environment.location.href);
      resultUrl.searchParams.set("run", runId);
      const title = "STEP conversion complete";
      const options = {
        body: `${fileName || "Your STL"} is ready. Open the converter to download the result.`,
        silent: true,
        tag: `stl-to-step-${runId}`,
        data: { url: resultUrl.href },
      };

      try {
        const registration = await getWorkerRegistration();
        if (registration) {
          await registration.showNotification(title, options);
          return true;
        }
        const notification = new NotificationApi(title, options);
        notification.onclick = () => {
          notification.close();
          const currentUrl = new URL(environment.location.href);
          if (currentUrl.searchParams.get("run") !== runId) {
            environment.location.assign(resultUrl.href);
          }
          environment.focus?.();
        };
        return true;
      } catch {
        return false;
      }
    },
  };
}
