import { Button, Card } from "@heroui/react";
import { useEffect, useMemo, useState } from "react";
import createClient from "openapi-fetch";
import type { components, paths } from "@/api/intake-schema";
import { basePath } from "@/lib/base-path";

type Schedule = components["schemas"]["ScheduleResponse"];
const field =
  "border-separator bg-surface text-foreground w-full rounded-xl border p-3";

export function SchedulePanel({ token }: { token: string }) {
  const api = useMemo(
    () =>
      createClient<paths>({
        baseUrl: basePath,
        headers: { Authorization: `Bearer ${token}` },
      }),
    [token],
  );
  const [rows, setRows] = useState<Schedule[]>([]);
  const [title, setTitle] = useState("");
  const [playlist, setPlaylist] = useState("");
  const [cron, setCron] = useState("0 6 * * *");
  const [timezone, setTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  const [limit, setLimit] = useState(100);
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const result = await api.GET("/v1/schedules", {
          signal: controller.signal,
        });
        if (result.error) throw new Error(result.error.message);
        if (!controller.signal.aborted) setRows(result.data ?? []);
      } catch (err) {
        if (!controller.signal.aborted)
          setError(
            err instanceof Error ? err.message : "Cannot load schedules",
          );
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(refresh, 5000);
      }
    };
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, revision]);
  const perform = async (
    work: () => Promise<{ error?: { message: string } }>,
  ) => {
    setBusy(true);
    setError("");
    try {
      const result = await work();
      if (result.error) throw new Error(result.error.message);
      setRevision((value) => value + 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Schedule operation failed",
      );
    } finally {
      setBusy(false);
    }
  };
  const save = () =>
    perform(async () => {
      let playlistId = playlist.trim();
      if (playlistId.startsWith("https://")) {
        const url = new URL(playlistId);
        if (
          !["music.youtube.com", "youtube.com", "www.youtube.com"].includes(
            url.hostname,
          )
        )
          throw new Error("Use a YouTube playlist URL");
        playlistId = url.searchParams.get("list") ?? "";
      }
      const body = {
        title,
        playlist_id: playlistId,
        cron,
        timezone,
        limit,
        enabled: true,
      };
      const result = editing
        ? await api.PUT("/v1/schedules/{schedule_id}", {
            params: { path: { schedule_id: editing } },
            body,
          })
        : await api.POST("/v1/schedules", { body });
      if (!result.error) {
        setEditing(null);
        setTitle("");
        setPlaylist("");
      }
      return result;
    });
  return (
    <section className="space-y-4">
      <Card className="space-y-3 p-5">
        <h2 className="font-semibold">
          {editing ? "Edit playlist schedule" : "Schedule a playlist"}
        </h2>
        <p className="text-muted text-sm">
          Scheduled snapshots use the same download-once ledger. The intake
          worker must be enabled on the server. Each run captures up to 100
          public playlist entries.
        </p>
        <label>
          Schedule name
          <input
            className={field}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          Playlist URL or ID
          <input
            className={field}
            value={playlist}
            onChange={(e) => setPlaylist(e.target.value)}
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-3">
          <label>
            Cron (minute hour day month weekday)
            <input
              className={field}
              value={cron}
              onChange={(e) => setCron(e.target.value)}
            />
          </label>
          <label>
            IANA timezone
            <input
              className={field}
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
            />
          </label>
          <label>
            Track limit
            <input
              className={field}
              type="number"
              min={1}
              max={100}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            />
          </label>
        </div>
        <Button
          isDisabled={busy || !title || !playlist}
          onPress={() => void save()}
        >
          {editing ? "Save schedule" : "Create schedule"}
        </Button>
        {editing && (
          <Button variant="ghost" onPress={() => setEditing(null)}>
            Cancel editing
          </Button>
        )}
      </Card>
      {error && (
        <p role="alert" className="text-danger">
          {error}
        </p>
      )}
      {rows.map((row) => (
        <Card className="space-y-3 p-5" key={row.id}>
          <h3 className="font-semibold">
            {row.title} ·{" "}
            {row.run_id ? "running" : row.enabled ? "enabled" : "paused"}
          </h3>
          <p className="text-muted text-sm">
            {row.playlist_id} · {row.cron} · {row.timezone} · max {row.limit}
          </p>
          <p>
            Next:{" "}
            {row.enabled
              ? new Date(row.next_run * 1000).toLocaleString()
              : "paused"}{" "}
            · Last:{" "}
            {row.last_run
              ? new Date(row.last_run * 1000).toLocaleString()
              : "not run yet"}
          </p>
          {row.last_intake_id && (
            <p className="text-sm break-all">
              Last intake: {row.last_intake_id}
            </p>
          )}
          {row.last_error && <p className="text-danger">{row.last_error}</p>}
          <div className="flex flex-wrap gap-2">
            <Button
              isDisabled={busy || !!row.run_id}
              variant="secondary"
              onPress={() =>
                void perform(() =>
                  api.POST("/v1/schedules/{schedule_id}/run", {
                    params: { path: { schedule_id: row.id } },
                  }),
                )
              }
            >
              Run now (enables)
            </Button>
            <Button
              isDisabled={busy || !!row.run_id}
              variant="secondary"
              onPress={() =>
                void perform(() =>
                  api.PUT("/v1/schedules/{schedule_id}", {
                    params: { path: { schedule_id: row.id } },
                    body: {
                      title: row.title,
                      playlist_id: row.playlist_id,
                      cron: row.cron,
                      timezone: row.timezone,
                      limit: row.limit,
                      enabled: !row.enabled,
                    },
                  }),
                )
              }
            >
              {row.enabled ? "Pause" : "Enable"}
            </Button>
            <Button
              isDisabled={busy || !!row.run_id}
              variant="ghost"
              onPress={() => {
                setEditing(row.id);
                setTitle(row.title);
                setPlaylist(row.playlist_id);
                setCron(row.cron);
                setTimezone(row.timezone);
                setLimit(row.limit);
              }}
            >
              Edit
            </Button>
            <Button
              isDisabled={busy || !!row.run_id}
              variant="ghost"
              onPress={() =>
                void perform(() =>
                  api.DELETE("/v1/schedules/{schedule_id}", {
                    params: { path: { schedule_id: row.id } },
                  }),
                )
              }
            >
              Delete schedule
            </Button>
          </div>
        </Card>
      ))}
      {!rows.length && <p>No schedules for this device.</p>}
    </section>
  );
}
