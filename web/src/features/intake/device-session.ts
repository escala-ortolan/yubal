/** One-tab credentials. Never persist a device bearer token in localStorage. */
const key = "yubal:intake-device-session";

export interface DeviceSession {
  device: string;
  token: string;
}

export function readDeviceSession(
  storage: Storage = sessionStorage,
): DeviceSession | null {
  try {
    const value = storage.getItem(key);
    if (!value) return null;
    const parsed: unknown = JSON.parse(value);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("device" in parsed) ||
      typeof parsed.device !== "string" ||
      !("token" in parsed) ||
      typeof parsed.token !== "string" ||
      !/^[0-9a-f-]{36}$/i.test(parsed.device) ||
      !parsed.token
    ) {
      storage.removeItem(key);
      return null;
    }
    return { device: parsed.device, token: parsed.token };
  } catch {
    return null;
  }
}

export function writeDeviceSession(
  session: DeviceSession | null,
  storage: Storage = sessionStorage,
): void {
  try {
    if (session) storage.setItem(key, JSON.stringify(session));
    else storage.removeItem(key);
  } catch {
    // Browsers with blocked session storage can still connect for this page.
  }
}
