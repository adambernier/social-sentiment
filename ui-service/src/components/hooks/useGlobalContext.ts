import { useEffect, useState } from "react";
import type { GlobalContextData } from "../types";
import { startCompletionScheduledPolling } from "./polling.mjs";

const API_BASE = "/api";
const POLL_INTERVAL_MS = 5 * 60 * 1000;
const BUILD_ENABLED =
  process.env.NEXT_PUBLIC_GLOBAL_CONTEXT_ENABLED === "true";

type GlobalContextStatus =
  | "disabled"
  | "loading"
  | "ready"
  | "empty"
  | "error";

interface GlobalContextSnapshot {
  data: GlobalContextData | null;
  status: GlobalContextStatus;
  error: string | null;
}

export function useGlobalContext(symbol: string) {
  const [horizon, setHorizon] = useState<30 | 90>(30);
  const [retryKey, setRetryKey] = useState(0);
  const [snapshot, setSnapshot] = useState<GlobalContextSnapshot>(() => ({
    data: null,
    status: BUILD_ENABLED ? "loading" : "disabled",
    error: null,
  }));

  useEffect(() => {
    if (!BUILD_ENABLED) return;

    return startCompletionScheduledPolling(
      async (signal) => {
        setSnapshot((current) => {
          const hasCurrentData = current.data?.symbol === symbol;
          return hasCurrentData
            ? { ...current, error: null }
            : { data: null, status: "loading", error: null };
        });

        const response = await fetch(
          `${API_BASE}/stats/global-context?symbol=${encodeURIComponent(symbol)}&horizon_sessions=${horizon}`,
          { signal },
        );
        if (signal.aborted) return;
        if (response.status === 404) {
          setSnapshot({ data: null, status: "disabled", error: null });
          return;
        }
        if (!response.ok) {
          throw new Error(`Global context request failed (${response.status})`);
        }

        const data = (await response.json()) as GlobalContextData;
        if (signal.aborted) return;
        setSnapshot({
          data,
          status: data.configured ? "ready" : "empty",
          error: null,
        });
      },
      {
        intervalMs: POLL_INTERVAL_MS,
        onError: (error) => {
          setSnapshot((current) => ({
            data: current.data?.symbol === symbol ? current.data : null,
            status: "error",
            error:
              error instanceof Error
                ? error.message
                : "Global context is unavailable",
          }));
        },
      },
    );
  }, [symbol, horizon, retryKey]);

  return {
    data: snapshot.data?.symbol === symbol ? snapshot.data : null,
    status: snapshot.status,
    error: snapshot.error,
    horizon,
    setHorizon,
    retry: () => setRetryKey((value) => value + 1),
  };
}
