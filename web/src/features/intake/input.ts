/** Parse explicit video IDs/links without treating filenames or titles as identity. */
export function videoIds(value: string): string[] {
  const entries = value.trim().split(/\s+/).filter(Boolean);
  if (!entries.length || entries.length > 100)
    throw new Error("Enter 1–100 video IDs or YouTube links.");
  return entries.map((entry) => {
    if (/^[\w-]{11}$/.test(entry)) return entry;
    const url = new URL(entry);
    if (url.protocol !== "https:" || url.username || url.password)
      throw new Error("Use HTTPS YouTube links.");
    const host = url.hostname;
    const id =
      host === "youtu.be"
        ? url.pathname.slice(1)
        : ["youtube.com", "www.youtube.com", "music.youtube.com"].includes(
              host,
            ) && url.pathname === "/watch"
          ? url.searchParams.get("v")
          : null;
    if (!id || !/^[\w-]{11}$/.test(id))
      throw new Error(
        "Enter track links or IDs; playlist links need a preview first.",
      );
    return id;
  });
}

/** Works on LAN HTTP too; crypto.randomUUID is restricted to secure contexts. */
export function requestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 15) | 64;
  bytes[8] = (bytes[8]! & 63) | 128;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(
    "",
  );
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
