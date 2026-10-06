import { Button, Card, Chip } from "@heroui/react";
import {
  Disc3Icon,
  DownloadIcon,
  ListMusicIcon,
  RefreshCw,
  KeyRoundIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import createClient from "openapi-fetch";
import type { components, paths } from "@/api/intake-schema";
import { basePath } from "@/lib/base-path";
import { Footer } from "@/components/layout/footer";
import { ThemeToggler } from "@/components/layout/theme-toggler";
import { requestId, videoIds } from "./input";
import { SchedulePanel } from "./schedules";
import { readDeviceSession, writeDeviceSession } from "./device-session";

type Intake = components["schemas"]["IntakeDetailsResponse"];
type Track = components["schemas"]["TrackDetailsResponse"];
type Request = components["schemas"]["IntakeRequest"];
const field =
  "border-separator bg-surface text-foreground w-full rounded-xl border p-3";
const label = (value: string) => value.replaceAll("_", " ");

export function IntakeDashboard() {
  const [credentials, setCredentials] = useState(readDeviceSession);
  const [device, setDevice] = useState("");
  const [token, setToken] = useState("");
  const [tab, setTab] = useState("downloads");
  const [text, setText] = useState("");
  const [playlistUrl, setPlaylistUrl] = useState("");
  const [preview, setPreview] = useState<
    components["schemas"]["PlaylistPreviewResponse"] | null
  >(null);
  const [mode, setMode] = useState<
    "manual_song" | "manual_queue" | "manual_playlist"
  >("manual_song");
  const [intakes, setIntakes] = useState<Intake[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [before, setBefore] = useState<number | undefined>();
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [unconfirmed, setUnconfirmed] = useState(false);
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Track | null>(null);
  // Keep the exact intent after a transport failure so Retry cannot duplicate it.
  const pending = useRef<Request | null>(null);
  const generation = useRef(0);
  const actionRequests = useRef(new Map<string, string>());
  const api = useCallback(
    () =>
      createClient<paths>({
        baseUrl: basePath,
        headers: { Authorization: `Bearer ${credentials?.token ?? ""}` },
      }),
    [credentials],
  );

  useEffect(() => {
    if (!credentials) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const update = async () => {
      try {
        const client = api();
        const page = await client.GET("/v1/intakes", {
          params: { query: { limit: 20, before } },
          signal: controller.signal,
        });
        if (!page.data)
          throw new Error(page.error?.message ?? "Cannot load history");
        const rows = await Promise.all(
          page.data.items.map(async (row) => {
            const result = await client.GET("/v1/intakes/{intake_id}/details", {
              params: { path: { intake_id: row.intake_id } },
              signal: controller.signal,
            });
            if (!result.data)
              throw new Error(
                result.error?.message ?? "Cannot load submission details",
              );
            return result.data;
          }),
        );
        if (!controller.signal.aborted) {
          setIntakes(rows);
          setCursor(page.data.next_before);
          setConnectionError("");
        }
        if (selected) {
          const result = await client.GET("/v1/tracks/{video_id}/details", {
            params: { path: { video_id: selected } },
            signal: controller.signal,
          });
          if (!result.data)
            throw new Error(
              result.error?.message ?? "Cannot load track details",
            );
          if (!controller.signal.aborted) setDetail(result.data);
        }
      } catch (err) {
        if (!controller.signal.aborted)
          setConnectionError(
            err instanceof Error ? err.message : "Connection failed",
          );
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(update, 4000);
      }
    };
    void update();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [credentials, before, selected, api]);

  const submit = async () => {
    if (!credentials || busy) return;
    const current = generation.current;
    setBusy(true);
    setError("");
    try {
      if (!pending.current) {
        const ids = preview
          ? preview.tracks.map((track) => track.video_id)
          : videoIds(text);
        if (mode === "manual_song" && ids.length !== 1)
          throw new Error("Choose queue or playlist for multiple tracks.");
        pending.current = {
          request_id: requestId(),
          device_id: credentials.device,
          mode,
          source_context: {
            kind:
              mode === "manual_song"
                ? "song"
                : mode === "manual_queue"
                  ? "queue"
                  : "playlist",
            playlist_id: preview?.playlist_id,
          },
          tracks:
            preview?.tracks ??
            ids.map((video_id, position) => ({ video_id, position })),
        };
      }
      const result = await api().POST("/v1/intakes", { body: pending.current });
      if (generation.current !== current) return;
      if (!result.data) {
        if (result.response.status < 500 && result.response.status !== 429)
          pending.current = null;
        throw new Error(result.error?.message ?? "Submission failed");
      }
      pending.current = null;
      setText("");
      setPreview(null);
      setBefore(undefined);
      setNotice(
        `Accepted ${result.data.items.length} track occurrence(s). Download completion is shown below.`,
      );
    } catch (err) {
      if (generation.current === current)
        setError(
          err instanceof Error
            ? err.message
            : "Connection failed; retry sends the same request.",
        );
    } finally {
      if (generation.current === current) {
        setBusy(false);
        setUnconfirmed(pending.current !== null);
      }
    }
  };

  const loadPlaylist = async () => {
    const current = generation.current;
    setBusy(true);
    setError("");
    try {
      let id = playlistUrl.trim();
      if (id.startsWith("https://")) {
        const url = new URL(id);
        if (
          !["youtube.com", "www.youtube.com", "music.youtube.com"].includes(
            url.hostname,
          )
        )
          throw new Error("Use a YouTube playlist link.");
        id = url.searchParams.get("list") ?? "";
      }
      if (!/^[\w-]{2,256}$/.test(id))
        throw new Error("Enter a playlist ID or YouTube playlist link.");
      const result = await api().POST("/v1/intakes/preview-playlist", {
        body: { playlist_id: id, limit: 100 },
      });
      if (current !== generation.current) return;
      if (!result.data)
        throw new Error(result.error?.message ?? "Preview failed");
      setPreview(result.data);
      setMode("manual_playlist");
    } catch (err) {
      if (current === generation.current)
        setError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };

  const retry = async (id: string, tagging = false) => {
    const current = generation.current;
    setBusy(true);
    try {
      const result = tagging
        ? await api().POST("/v1/tracks/{video_id}/retry-tagging", {
            params: { path: { video_id: id } },
          })
        : await api().POST("/v1/tracks/{video_id}/retry", {
            params: { path: { video_id: id } },
          });
      if (current !== generation.current) return;
      if (result.error) throw new Error(result.error.message);
      setNotice(
        tagging
          ? "Tagging reopened; audio was not downloaded again."
          : "Retry queued.",
      );
    } catch (err) {
      if (current === generation.current)
        setError(err instanceof Error ? err.message : "Retry failed");
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };

  const rows = intakes.filter(
    (row) => tab !== "playlists" || row.request.mode.endsWith("playlist"),
  );
  const controlJob = async (
    id: string,
    state: "active" | "cancelled" | "deleted",
  ) => {
    const current = generation.current;
    setBusy(true);
    setError("");
    try {
      const result = await api().PATCH("/v1/intakes/{intake_id}/control", {
        params: { path: { intake_id: id } },
        body: { state },
      });
      if (current !== generation.current) return;
      if (result.error) throw new Error(result.error.message);
      setIntakes((rows) =>
        state === "deleted"
          ? rows.filter((row) => row.intake_id !== id)
          : rows.map((row) => (row.intake_id === id ? { ...row, state } : row)),
      );
      setNotice(
        state === "cancelled"
          ? "Job cancelled. An in-flight track may finish; shared downloads continue."
          : state === "deleted"
            ? "Removed from history. Download identity and audio are retained."
            : "Job resumed.",
      );
    } catch (err) {
      if (current === generation.current)
        setError(err instanceof Error ? err.message : "Job control failed");
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const trackAction = async (videoId: string, action: "forget" | "force") => {
    const current = generation.current;
    const key = `${videoId}:${action}`;
    const request = actionRequests.current.get(key) ?? requestId();
    actionRequests.current.set(key, request);
    setBusy(true);
    setError("");
    try {
      const options = {
        params: { path: { video_id: videoId } },
        body: { request_id: request },
      };
      const result =
        action === "forget"
          ? await api().DELETE("/v1/tracks/{video_id}", options)
          : await api().POST("/v1/tracks/{video_id}/force-redownload", options);
      if (current !== generation.current) return;
      if (result.error) throw new Error(result.error.message);
      actionRequests.current.delete(key);
      setSelected(null);
      setDetail(null);
      setNotice(
        action === "forget"
          ? "Song forgotten by the deduplication ledger. Existing audio files were kept; a future submission can download it again."
          : "Explicit redownload queued as a new intake. Previous audio files are preserved.",
      );
    } catch (err) {
      if (current === generation.current)
        setError(
          err instanceof Error
            ? err.message
            : "Track action failed; retry uses the same request ID.",
        );
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const items = rows.flatMap((row) => row.items);
  const completed = items.filter((item) => item.status === "downloaded").length;
  const failed = items.filter((item) =>
    ["failed", "missing_output"].includes(item.status),
  ).length;
  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-separator bg-background/90 sticky top-0 z-20 border-b backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-3 px-4 py-3">
          <Disc3Icon className="text-accent" />
          <span className="mr-4 text-xl font-bold">yubal</span>
          <Button
            variant={tab === "downloads" ? "secondary" : "ghost"}
            onPress={() => setTab("downloads")}
          >
            <DownloadIcon size={16} /> Downloads
          </Button>
          <Button
            variant={tab === "playlists" ? "secondary" : "ghost"}
            onPress={() => setTab("playlists")}
          >
            <ListMusicIcon size={16} /> My playlists
          </Button>
          <Button
            variant={tab === "schedules" ? "secondary" : "ghost"}
            onPress={() => setTab("schedules")}
          >
            Schedules
          </Button>
          <div className="ml-auto flex items-center gap-2">
            <a href={`${basePath}/docs`}>API docs</a>
            <ThemeToggler />
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-5xl flex-1 space-y-6 px-4 py-6">
        <div>
          <h1 className="text-2xl font-bold">
            {tab === "schedules"
              ? "Schedules"
              : tab === "playlists"
                ? "My playlists"
                : "Downloads"}
          </h1>
          <p className="text-muted">
            Durable history, verified audio and ordered submissions.
          </p>
        </div>
        {!credentials ? (
          <Card className="space-y-4 p-6">
            <h2 className="flex items-center gap-2 text-lg font-semibold">
              <KeyRoundIcon size={20} /> Connect your device
            </h2>
            <p>
              Enter the credentials issued by this backend. This tab remembers
              them across refreshes; Disconnect or closing the tab forgets them.
            </p>
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                generation.current++;
                const session = { device: device.trim(), token: token.trim() };
                writeDeviceSession(session);
                setCredentials(session);
                setToken("");
              }}
            >
              <label className="block">
                Device UUID
                <input
                  required
                  className={field}
                  value={device}
                  onChange={(e) => setDevice(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="block">
                Device token
                <input
                  required
                  type="password"
                  className={field}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <Button type="submit">Connect</Button>
            </form>
          </Card>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-muted text-sm break-all">
                Device {credentials.device} · history refreshes every 4 seconds
              </span>
              <Button
                variant="ghost"
                onPress={() => {
                  generation.current++;
                  writeDeviceSession(null);
                  setCredentials(null);
                  setIntakes([]);
                  setDetail(null);
                  setSelected(null);
                  setBusy(false);
                  setNotice("");
                  setError("");
                  setConnectionError("");
                  pending.current = null;
                  setUnconfirmed(false);
                  setPreview(null);
                  setText("");
                  setBefore(undefined);
                  actionRequests.current.clear();
                }}
              >
                Disconnect
              </Button>
            </div>
            {tab === "schedules" ? (
              <SchedulePanel token={credentials.token} />
            ) : (
              <>
                <Card className="space-y-3 p-5">
                  <h2 className="font-semibold">New download</h2>
                  <label>
                    Public playlist URL or ID
                    <input
                      className={field}
                      value={playlistUrl}
                      onChange={(e) => setPlaylistUrl(e.target.value)}
                      disabled={busy || unconfirmed}
                    />
                  </label>
                  <Button
                    variant="secondary"
                    isDisabled={busy || unconfirmed || !playlistUrl.trim()}
                    onPress={() => void loadPlaylist()}
                  >
                    Preview playlist
                  </Button>
                  {preview && (
                    <div>
                      <h3 className="font-semibold">
                        {preview.title} · {preview.tracks.length} tracks
                      </h3>
                      <p className="text-muted text-sm">
                        First 100 entries maximum; unavailable entries are
                        omitted. Review before downloading.
                      </p>
                      <ol className="max-h-48 list-inside list-decimal overflow-auto">
                        {preview.tracks.map((track) => (
                          <li key={track.position}>
                            {track.title_hint || track.video_id} —{" "}
                            {track.artist_hint}
                          </li>
                        ))}
                      </ol>
                      <Button
                        variant="ghost"
                        isDisabled={busy || unconfirmed}
                        onPress={() => setPreview(null)}
                      >
                        Clear preview
                      </Button>
                    </div>
                  )}
                  <label>
                    Submission type
                    <select
                      className={field}
                      value={mode}
                      disabled={busy || unconfirmed || !!preview}
                      onChange={(e) => setMode(e.target.value as typeof mode)}
                    >
                      <option value="manual_song">Single song</option>
                      <option value="manual_queue">Queue snapshot</option>
                      <option value="manual_playlist">Playlist snapshot</option>
                    </select>
                  </label>
                  <label>
                    Video IDs or YouTube track links
                    <textarea
                      rows={3}
                      className={field}
                      value={text}
                      disabled={busy || unconfirmed || !!preview}
                      onChange={(e) => setText(e.target.value)}
                      placeholder="Paste one track link, or up to 100 IDs/links in playlist order"
                    />
                  </label>
                  <p className="text-muted text-sm">
                    Repeated tracks retain their positions and share one
                    download. Playlist snapshots do not start automatic resync.
                  </p>
                  <Button onPress={() => void submit()} isPending={busy}>
                    <DownloadIcon size={16} />
                    {unconfirmed ? "Retry same submission" : "Download"}
                  </Button>
                </Card>
                <div className="grid grid-cols-3 gap-3">
                  {[
                    ["Occurrences", items.length],
                    ["Verified", completed],
                    ["Needs attention", failed],
                  ].map(([name, value]) => (
                    <Card key={name} className="p-4">
                      <span className="text-muted text-sm">
                        {name} · this page
                      </span>
                      <strong className="text-2xl">{value}</strong>
                    </Card>
                  ))}
                </div>
                <div className="flex flex-wrap gap-3">
                  <input
                    aria-label="Search submissions"
                    className={`${field} flex-1`}
                    placeholder="Search title, artist, ID or mode"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                  <select
                    aria-label="Filter by status"
                    className={`${field} w-auto`}
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                  >
                    {[
                      "all",
                      "pending",
                      "downloading",
                      "downloaded",
                      "failed",
                      "missing_output",
                    ].map((state) => (
                      <option key={state}>{state}</option>
                    ))}
                  </select>
                </div>
                {!rows.length && (
                  <Card className="p-6">
                    No submissions on this page. Submit tracks above or open
                    another history page.
                  </Card>
                )}
                {rows
                  .filter(
                    (row) =>
                      JSON.stringify(row.request)
                        .toLowerCase()
                        .includes(query.toLowerCase()) &&
                      (filter === "all" ||
                        row.items.some((item) => item.status === filter)),
                  )
                  .map((row) => {
                    const done = row.items.filter(
                      (item) => item.status === "downloaded",
                    ).length;
                    return (
                      <Card key={row.intake_id} className="space-y-4 p-5">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <h2 className="font-semibold">
                            {label(row.request.mode)}
                            {row.state !== "active" && ` · ${row.state}`}
                          </h2>
                          <Chip size="sm" variant="soft">
                            {done}/{row.items.length} verified
                          </Chip>
                        </div>
                        <progress
                          className="accent-accent w-full"
                          aria-label="Verified tracks"
                          value={done}
                          max={Math.max(1, row.items.length)}
                        />
                        <p className="text-muted text-xs break-all">
                          Intake {row.intake_id}
                          {row.request.source_context?.playlist_id
                            ? ` · Playlist ${row.request.source_context.playlist_id}`
                            : ""}
                        </p>
                        <div className="flex flex-wrap gap-2">
                          <Button
                            size="sm"
                            variant="secondary"
                            isDisabled={busy}
                            onPress={() =>
                              void controlJob(
                                row.intake_id,
                                row.state === "cancelled"
                                  ? "active"
                                  : "cancelled",
                              )
                            }
                          >
                            {row.state === "cancelled"
                              ? "Resume job"
                              : "Cancel job"}
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            isDisabled={busy}
                            onPress={() =>
                              void controlJob(row.intake_id, "deleted")
                            }
                          >
                            Delete from history
                          </Button>
                        </div>
                        <div className="overflow-x-auto">
                          <table className="w-full text-left text-sm">
                            <thead>
                              <tr className="text-muted">
                                <th className="p-2">#</th>
                                <th>Track</th>
                                <th>Download</th>
                                <th>Downstream</th>
                                <th>Actions</th>
                              </tr>
                            </thead>
                            <tbody>
                              {row.items.map((item) => {
                                const hint = row.request.tracks[item.position];
                                return (
                                  <tr
                                    key={item.position}
                                    className="border-separator border-t"
                                  >
                                    <td className="p-2">{item.position + 1}</td>
                                    <td className="py-3">
                                      <a
                                        className="text-accent"
                                        href={`https://music.youtube.com/watch?v=${item.video_id}`}
                                        target="_blank"
                                        rel="noreferrer"
                                      >
                                        {hint?.title_hint || item.video_id}
                                      </a>
                                      <div className="text-muted">
                                        {hint?.artist_hint}
                                      </div>
                                    </td>
                                    <td>{label(item.status)}</td>
                                    <td>{label(item.downstream_status)}</td>
                                    <td>
                                      <Button
                                        size="sm"
                                        variant="ghost"
                                        onPress={() => {
                                          setDetail(null);
                                          setSelected(item.video_id);
                                        }}
                                      >
                                        Details
                                      </Button>
                                      {item.status === "failed" && (
                                        <Button
                                          size="sm"
                                          isDisabled={busy}
                                          onPress={() =>
                                            void retry(item.video_id)
                                          }
                                        >
                                          <RefreshCw size={14} /> Retry
                                        </Button>
                                      )}
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      </Card>
                    );
                  })}
                <div className="flex gap-3">
                  <Button
                    variant="secondary"
                    onPress={() => setBefore(undefined)}
                    isDisabled={before === undefined}
                  >
                    Newest
                  </Button>
                  <Button
                    variant="secondary"
                    onPress={() => setBefore(cursor ?? undefined)}
                    isDisabled={cursor === null}
                  >
                    Older submissions
                  </Button>
                </div>
              </>
            )}
          </>
        )}
        {notice && (
          <p role="status" className="text-accent">
            {notice}
          </p>
        )}
        {error && (
          <p role="alert" className="text-danger">
            {error}
          </p>
        )}
        {connectionError && (
          <p role="alert" className="text-danger">
            {connectionError} · displayed history may be stale.
          </p>
        )}
        {selected && credentials && (
          <Card className="space-y-3 p-5">
            <div className="flex justify-between">
              <h2 className="font-semibold">Track details · {selected}</h2>
              <Button
                variant="ghost"
                onPress={() => {
                  setSelected(null);
                  setDetail(null);
                }}
              >
                Close
              </Button>
            </div>
            {!detail ? (
              <p>Loading…</p>
            ) : (
              <>
                <p>
                  {label(detail.status)} · {label(detail.downstream_status)}
                </p>
                <p className="break-all">
                  Final path: {detail.final_path ?? "Not filed"}
                </p>
                <p className="text-muted text-sm">
                  These explicit actions preserve audio files. Shared records
                  owned by another device, active transfers and active filing
                  plans cannot be reset.
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="secondary"
                    isDisabled={
                      busy ||
                      detail.status === "downloading" ||
                      detail.status === "pending"
                    }
                    onPress={() => void trackAction(selected, "force")}
                  >
                    Force redownload
                  </Button>
                  <Button
                    variant="ghost"
                    isDisabled={busy || detail.status === "downloading"}
                    onPress={() => void trackAction(selected, "forget")}
                  >
                    Forget song (keep files)
                  </Button>
                </div>
                {detail.downstream_status === "tagged" && (
                  <Button
                    isDisabled={busy}
                    onPress={() => void retry(selected, true)}
                  >
                    Reopen tagging (keep audio)
                  </Button>
                )}
                <h3 className="font-semibold">
                  Download attempts ({detail.attempts.length})
                </h3>
                {!detail.attempts.length && <p>No transfer attempt yet.</p>}
                {detail.attempts.map((attempt) => (
                  <div
                    key={attempt.id}
                    className="border-separator space-y-2 rounded-lg border p-3"
                  >
                    <p>
                      {attempt.status} · {attempt.codec ?? "codec unavailable"}{" "}
                      ·{" "}
                      {attempt.audio_bytes == null
                        ? "size unavailable"
                        : `${(attempt.audio_bytes / 1048576).toFixed(2)} MiB`}{" "}
                      ·{" "}
                      {attempt.duration_seconds == null
                        ? "duration unavailable"
                        : `${attempt.duration_seconds.toFixed(1)} seconds`}
                    </p>
                    {attempt.error && (
                      <p className="text-danger">{attempt.error}</p>
                    )}
                    <details>
                      <summary>Verification evidence</summary>
                      <pre className="overflow-auto text-xs">
                        {JSON.stringify(attempt, null, 2)}
                      </pre>
                    </details>
                  </div>
                ))}
              </>
            )}
          </Card>
        )}
      </main>
      <Footer />
    </div>
  );
}
