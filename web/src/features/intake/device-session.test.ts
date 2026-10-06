import { describe, expect, test } from "bun:test";
import { readDeviceSession, writeDeviceSession } from "./device-session";

function memoryStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() {
      return entries.size;
    },
    clear() {
      entries.clear();
    },
    getItem(key) {
      return entries.get(key) ?? null;
    },
    key(index) {
      return Array.from(entries.keys())[index] ?? null;
    },
    removeItem(key) {
      entries.delete(key);
    },
    setItem(key, value) {
      entries.set(key, value);
    },
  };
}

describe("one-tab device credentials", () => {
  test("survives a component reload and disconnect removes both values", () => {
    const storage = memoryStorage();
    const credentials = {
      device: "7a35b8f3-bdf6-4c15-b86f-a36b93712d50",
      token: "synthetic-test-token",
    };
    writeDeviceSession(credentials, storage);
    expect(readDeviceSession(storage)).toEqual(credentials);
    writeDeviceSession(null, storage);
    expect(readDeviceSession(storage)).toBeNull();
    expect(storage.length).toBe(0);
  });

  test("ignores invalid or partial sessions", () => {
    const storage = memoryStorage();
    storage.setItem(
      "yubal:intake-device-session",
      '{"device":"bad","token":"test"}',
    );
    expect(readDeviceSession(storage)).toBeNull();
    storage.setItem("yubal:intake-device-session", "not json");
    expect(readDeviceSession(storage)).toBeNull();
  });
});
