import { useEffect, useRef, useState } from "react";

import {
  caseEventsUrl,
  submissionEventsUrl,
} from "@/lib/api";
import type { CaseDetail, Submission } from "@/types/api";

/**
 * Subscribe to the SSE stream of submission lifecycle events.
 *
 * The backend emits a `snapshot` first (current state), then `update`
 * events for each state change. The stream closes after `sent` or `failed`.
 *
 * Returns the latest Submission snapshot, or `undefined` until the first
 * event arrives.
 */
export function useSubmissionStream(
  submissionId: string | null,
  initial?: Submission,
): Submission | undefined {
  const [snap, setSnap] = useState<Submission | undefined>(initial);
  const lastIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!submissionId) return;
    if (initial && lastIdRef.current !== submissionId) {
      setSnap(initial);
    }
    lastIdRef.current = submissionId;

    // If the seed snapshot is already terminal, don't open a stream.
    if (initial && (initial.state === "sent" || initial.state === "failed")) {
      return;
    }

    const es = new EventSource(submissionEventsUrl(submissionId));
    const onSnapshot = (e: MessageEvent) => {
      try {
        setSnap(JSON.parse(e.data) as Submission);
      } catch (err) {
        console.warn("submission snapshot parse failed", err);
      }
    };
    const onUpdate = (e: MessageEvent) => {
      try {
        const payload = JSON.parse(e.data) as {
          snapshot: Submission | null;
          fields: string[];
        };
        if (payload.snapshot) setSnap(payload.snapshot);
      } catch (err) {
        console.warn("submission update parse failed", err);
      }
    };
    const onError = () => {
      // EventSource auto-reconnects on transient errors; nothing to do.
      // The stream will return readyState=CLOSED if the server hangs up.
      if (es.readyState === EventSource.CLOSED) {
        // Server-side terminal — fine, leave the last snapshot in place.
      }
    };
    es.addEventListener("snapshot", onSnapshot as EventListener);
    es.addEventListener("update", onUpdate as EventListener);
    es.addEventListener("error", onError as EventListener);

    return () => {
      es.removeEventListener("snapshot", onSnapshot as EventListener);
      es.removeEventListener("update", onUpdate as EventListener);
      es.removeEventListener("error", onError as EventListener);
      es.close();
    };
    // initial is intentionally not in the dep array — it's a seed only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submissionId]);

  return snap;
}

/**
 * Subscribe to the SSE stream of case lifecycle events.
 *
 * First event is a `snapshot`; then `partial` events fire as fields are
 * populated; final event is `complete` carrying the resolved case.
 */
export function useCaseStream(caseId: string | null): CaseDetail | undefined {
  const [snap, setSnap] = useState<CaseDetail | undefined>(undefined);
  const lastIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!caseId) {
      setSnap(undefined);
      lastIdRef.current = null;
      return;
    }
    if (lastIdRef.current !== caseId) {
      setSnap(undefined);
      lastIdRef.current = caseId;
    }

    const es = new EventSource(caseEventsUrl(caseId));
    const apply = (e: MessageEvent) => {
      try {
        const payload = JSON.parse(e.data);
        // `snapshot` and `complete` carry the case dict at the top level;
        // `partial` wraps it under .snapshot.
        const snapshot: CaseDetail | undefined =
          payload?.snapshot ?? payload;
        if (snapshot && typeof snapshot === "object" && "case_id" in snapshot) {
          setSnap(snapshot as CaseDetail);
        }
      } catch (err) {
        console.warn("case event parse failed", err);
      }
    };
    es.addEventListener("snapshot", apply as EventListener);
    es.addEventListener("partial", apply as EventListener);
    es.addEventListener("complete", apply as EventListener);

    return () => {
      es.removeEventListener("snapshot", apply as EventListener);
      es.removeEventListener("partial", apply as EventListener);
      es.removeEventListener("complete", apply as EventListener);
      es.close();
    };
  }, [caseId]);

  return snap;
}
