import { describe, expect, test } from "bun:test";
import { requestId, videoIds } from "./input";

describe("intake input", () => {
  test("keeps order and repeats across IDs and links", () => {
    expect(
      videoIds(
        "dQw4w9WgXcQ https://youtu.be/jNQXAC9IVRw https://music.youtube.com/watch?v=dQw4w9WgXcQ",
      ),
    ).toEqual(["dQw4w9WgXcQ", "jNQXAC9IVRw", "dQw4w9WgXcQ"]);
  });
  test("rejects unrelated destinations and over-limit input", () => {
    for (const input of [
      "",
      "https://evil.test/watch?v=dQw4w9WgXcQ",
      "https://youtube.com/playlist?list=PL123",
      Array(101).fill("dQw4w9WgXcQ").join(" "),
    ]) {
      expect(() => videoIds(input)).toThrow();
    }
  });
  test("creates UUIDs without relying on secure-context randomUUID", () => {
    expect(requestId()).toMatch(
      /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/,
    );
    expect(requestId()).not.toBe(requestId());
  });
});
