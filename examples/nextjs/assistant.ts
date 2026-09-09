"use client";

/**
 * The assistant's data model, its site-context builder, and the store that
 * polls the bridge.
 *
 * One "message" in the chat is one *job* on the bridge: the prompt the visitor
 * typed and the answer Claude Desktop eventually gives back. The bridge is a
 * queue, not a stream, so a job carries its own progress - which is what lets
 * the panel show something honest while a turn runs for minutes.
 */
import { create } from "zustand";
import type { Payload } from "@/lib/types";
import type { Filters } from "@/lib/filters";
import { S } from "@/lib/types";


// ── the model ───────────────────────────────────────────────────────────────

/** Where a job is in its life. `needs_input` means Claude asked something back. */
export type JobStatus = "queued" | "running" | "needs_input" | "done" | "error";

/** The finer sub-state while running. The UI owns the wording for these. */
export type JobStage =
  | "queued"
  | "opening"
  | "typing"
  | "generating"
  | "needs_input"
  | "reading"
  | "done";

export interface Job {
  id: string;
  thread_title: string;
  prompt: string;
  context: string;
  status: JobStatus;
  stage: JobStage;
  answer: string | null;
  /** What Claude has written so far, while it is still writing. */
  partial: string;
  error: string | null;
  question: string | null;
  options: string[] | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface Health {
  ok: boolean;
  worker_running: boolean;
  trusted: boolean;
  app_running: boolean;
  thread: string | null;
  generating: boolean;
  chats: string[];
  error: string | null;
  /** Set by the proxy when the bridge itself cannot be reached. */
  offline?: boolean;
  /** The bridge address that failed, so a wrong BRIDGE_URL is visible. */
  where?: string;
}

export interface ChatSummary {
  title: string;
  in_sidebar: boolean;
  duplicate: boolean;
  messages: number;
  pending: number;
  last_at: number | null;
}

export const LIVE_STATUSES: JobStatus[] = ["queued", "running", "needs_input"];

export function isLive(job: Job): boolean {
  return LIVE_STATUSES.includes(job.status);
}

/**
 * What the visitor is looking at, in a form Claude can use.
 *
 * Sent alongside the prompt rather than glued into it, so the transcript in the
 * desktop app still shows the question as it was actually asked. Deliberately
 * short: a full dump of 27k sites would bury the question, and the numbers that
 * matter for an out-of-home brief are counts, ranges and the current selection.
 */
export function siteContext(
  payload: Payload | null,
  filters: Filters,
  visible: number[],
  cart: number[]
): string {
  if (!payload) return "";
  const lines: string[] = [];
  const total = payload.meta.siteCount;

  lines.push(
    `Berlin out-of-home inventory: ${total.toLocaleString("en")} advertising sites ` +
      `across ${Object.keys(payload.meta.districtCounts).length} Bezirke, from ` +
      `${Object.entries(payload.meta.providerCounts)
        .map(([name, n]) => `${name} (${n.toLocaleString("en")})`)
        .join(", ")}.`
  );

  const active: string[] = [];
  if (filters.providers.length) active.push(`provider ${filters.providers.join(", ")}`);
  if (filters.districts.length) active.push(`Bezirk ${filters.districts.join(", ")}`);
  if (filters.formats.length) active.push(`media class ${filters.formats.join(", ")}`);
  if (filters.illumination.length) active.push(`illumination ${filters.illumination.join(", ")}`);
  if (filters.query.trim()) active.push(`search "${filters.query.trim()}"`);
  if (filters.near) active.push(`within ${filters.near.radiusM} m of ${filters.near.label}`);
  if (filters.bookableFrom || filters.bookableTo) {
    active.push(`bookable ${filters.bookableFrom ?? "any"} to ${filters.bookableTo ?? "any"}`);
  }
  const [lo, hi] = filters.priceRange;
  const [pLo, pHi] = payload.meta.priceRange;
  if (lo > pLo || hi < pHi) active.push(`price EUR ${lo}-${hi} per booking period`);

  lines.push(
    active.length
      ? `The visitor is filtering by: ${active.join("; ")}. ` +
          `${visible.length.toLocaleString("en")} sites match.`
      : `No filters are applied; all ${visible.length.toLocaleString("en")} sites are shown.`
  );

  if (cart.length) {
    const rows = cart.slice(0, 12).map((i) => {
      const t = payload.sites[i];
      const price = t[S.price];
      return `- ${t[S.address]} (${payload.enums.format[t[S.format]] ?? "?"}, ` +
        `${payload.enums.district[t[S.district]] ?? "?"}, ` +
        `${price == null ? "price on request" : `EUR ${price}`})`;
    });
    lines.push(
      `Campaign cart: ${cart.length} site(s).\n${rows.join("\n")}` +
        (cart.length > rows.length ? `\n- …and ${cart.length - rows.length} more` : "")
    );
  }

  return `[Context from the Mathrix AI website — the visitor cannot see this block]\n${lines.join(
    "\n"
  )}`;
}


// ── polling the bridge ──────────────────────────────────────────────────────

const THREAD_KEY = "mathrix:assistant:thread";

/** The bridge is a queue behind a local desktop app, so the panel polls it. */
const FAST_MS = 900; // something is in flight
const SLOW_MS = 4000; // idle
const HEALTH_MS = 6000;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/assistant/${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : `The assistant bridge returned ${res.status}.`;
    throw new Error(detail);
  }
  return body as T;
}

interface AssistantState {
  open: boolean;
  settingsOpen: boolean;
  health: Health | null;
  chats: ChatSummary[];
  thread: string;
  jobs: Job[];
  draft: string;
  sending: boolean;
  sendError: string | null;

  setOpen: (open: boolean) => void;
  setSettingsOpen: (open: boolean) => void;
  setDraft: (draft: string) => void;
  start: () => void;
  stop: () => void;
  selectThread: (title: string) => Promise<void>;
  send: (context: string) => Promise<void>;
  answer: (
    jobId: string,
    answer: { choice?: string; text?: string; skip?: boolean }
  ) => Promise<void>;
  dismissError: () => void;
}

let jobTimer: ReturnType<typeof setTimeout> | null = null;
let healthTimer: ReturnType<typeof setTimeout> | null = null;
/**
 * Bumped by every start() and stop(). Each polling loop captures the value it
 * was born with and gives up the moment it no longer matches, so a loop whose
 * request was still in flight when the panel closed cannot reschedule itself.
 * Clearing the timer handles alone is not enough: a loop that is *awaiting*
 * owns no timer to clear.
 */
let generation = 0;

function storedThread(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(THREAD_KEY) ?? "";
  } catch {
    return "";
  }
}

export const useAssistantStore = create<AssistantState>((set, get) => ({
  open: false,
  settingsOpen: false,
  health: null,
  chats: [],
  thread: "",
  jobs: [],
  draft: "",
  sending: false,
  sendError: null,

  setOpen(open) {
    set({ open });
    if (open) get().start();
    else get().stop();
  },

  setSettingsOpen(settingsOpen) {
    set({ settingsOpen });
  },

  setDraft(draft) {
    set({ draft });
  },

  /**
   * Two independent loops, each rescheduled from its own completion rather than
   * on an interval: a slow accessibility walk must not stack up requests behind
   * itself while Claude Desktop is busy.
   */
  start() {
    get().stop();

    // Adopt the remembered conversation synchronously, so a returning visitor's
    // history loads on the first tick instead of waiting on the health call -
    // the first accessibility walk of a cold Claude Desktop can take ten
    // seconds, and an empty panel for ten seconds reads as a broken one.
    if (!get().thread) {
      const stored = storedThread();
      if (stored) set({ thread: stored });
    }

    const mine = ++generation;

    const pollJobs = async () => {
      if (mine !== generation) return;
      const thread = get().thread;
      try {
        if (thread) {
          const jobs = await call<Job[]>(
            `messages?thread=${encodeURIComponent(thread)}&limit=100`
          );
          set({ jobs });
        }
      } catch {
        // Transient: the health loop owns telling the user the bridge is down.
      }
      if (mine !== generation) return;
      const busy = get().jobs.some(isLive);
      jobTimer = setTimeout(pollJobs, busy ? FAST_MS : SLOW_MS);
    };

    const pollHealth = async () => {
      if (mine !== generation) return;
      try {
        const health = await call<Health>("health");
        set({ health });
        const { chats } = await call<{ selected: string; chats: ChatSummary[] }>("chats");
        set({ chats });
        // Adopt the operator's stored choice the first time, so the panel opens
        // on the conversation the bridge is already pointed at.
        if (!get().thread) {
          const stored = storedThread();
          const fallback =
            stored ||
            chats.find((c) => c.in_sidebar && !c.duplicate && c.messages > 0)?.title ||
            "";
          if (fallback) {
            set({ thread: fallback });
            // Bring the next job poll forward instead of starting a second
            // loop: two self-rescheduling chains would both survive a stop().
            if (jobTimer) clearTimeout(jobTimer);
            jobTimer = setTimeout(pollJobs, 0);
          }
        }
      } catch {
        set({
          health: {
            ok: false,
            offline: true,
            worker_running: false,
            trusted: false,
            app_running: false,
            thread: null,
            generating: false,
            chats: [],
            error: "The assistant bridge is not reachable.",
          },
        });
      }
      if (mine !== generation) return;
      healthTimer = setTimeout(pollHealth, HEALTH_MS);
    };

    void pollHealth();
    void pollJobs();
  },

  stop() {
    generation += 1; // orphans any loop currently awaiting a response
    if (jobTimer) clearTimeout(jobTimer);
    if (healthTimer) clearTimeout(healthTimer);
    jobTimer = null;
    healthTimer = null;
  },

  async selectThread(title) {
    set({ thread: title, jobs: [], sendError: null });
    try {
      window.localStorage.setItem(THREAD_KEY, title);
    } catch {
      // Private mode: the choice still holds for this session.
    }
    // Mirror it onto the bridge so the worker's default matches the panel's.
    try {
      await call("config", { method: "POST", body: JSON.stringify({ thread_title: title }) });
    } catch {
      // Not fatal - every job carries its own thread title anyway.
    }
    get().start();
  },

  async send(context) {
    const prompt = get().draft.trim();
    const thread = get().thread;
    if (!prompt || get().sending) return;
    set({ sending: true, sendError: null });
    try {
      const job = await call<Job>("messages", {
        method: "POST",
        body: JSON.stringify({ prompt, thread, context }),
      });
      // Shown immediately, so the message appears the instant it is accepted
      // rather than on the next poll.
      set((s) => ({ jobs: [...s.jobs, job], draft: "" }));
      get().start();
    } catch (e) {
      set({ sendError: e instanceof Error ? e.message : String(e) });
    } finally {
      set({ sending: false });
    }
  },

  async answer(jobId, answer) {
    try {
      const job = await call<Job>(`messages/${jobId}/answer`, {
        method: "POST",
        body: JSON.stringify({
          choice: answer.choice ?? null,
          text: answer.text ?? null,
          skip: answer.skip ?? false,
        }),
      });
      set((s) => ({ jobs: s.jobs.map((j) => (j.id === job.id ? job : j)) }));
    } catch (e) {
      set({ sendError: e instanceof Error ? e.message : String(e) });
    }
  },

  dismissError() {
    set({ sendError: null });
  },
}));
