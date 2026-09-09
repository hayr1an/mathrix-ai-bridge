"use client";

/**
 * The assistant drawer, its launcher, and the Markdown renderer for answers.
 *
 * Every answer here is produced by Claude Desktop on the operator's own Mac,
 * driven through the local bridge - so unlike a hosted chat it can be offline,
 * busy with someone typing in the app, or waiting on a question of its own.
 * The panel is built around showing that honestly: a message is never left as a
 * silent spinner, and every failure arrives with the sentence that explains it.
 */
import {
  Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode,
} from "react";
import { useAppStore } from "@/store/useAppStore";
import { useT } from "@/store/useLocale";
import { siteContext, isLive, useAssistantStore } from "@/lib/assistant";
import type { Health, Job, JobStage } from "@/lib/assistant";
import type { MessageKey } from "@/lib/i18n";


// ── rendering Claude's markdown ─────────────────────────────────────────────

type Token =
  | { kind: "code"; lang: string; text: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "quote"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "rule" }
  | { kind: "para"; text: string };

const FENCE = /^```([\w+-]*)\s*$/;
const HEADING = /^(#{1,4})\s+(.*)$/;
const BULLET = /^[-*+]\s+(.*)$/;
const NUMBERED = /^(\d{1,3})[.)]\s+(.*)$/;
const QUOTE = /^>\s?(.*)$/;
const RULE = /^(-{3,}|\*{3,}|_{3,})$/;

function tokenize(source: string): Token[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const tokens: Token[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    const fence = FENCE.exec(line.trim());
    if (fence) {
      const body: string[] = [];
      i += 1;
      // An unterminated fence still renders as a code block: Claude's answer may
      // simply be cut off mid-block while it is still streaming.
      while (i < lines.length && !FENCE.test(lines[i].trim())) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      tokens.push({ kind: "code", lang: fence[1] ?? "", text: body.join("\n") });
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    if (RULE.test(line.trim())) {
      tokens.push({ kind: "rule" });
      i += 1;
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      tokens.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      i += 1;
      continue;
    }

    const quote = QUOTE.exec(line);
    if (quote) {
      const body = [quote[1]];
      i += 1;
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(QUOTE.exec(lines[i])![1]);
        i += 1;
      }
      tokens.push({ kind: "quote", text: body.join("\n") });
      continue;
    }

    if (BULLET.test(line) || NUMBERED.test(line)) {
      const ordered = !BULLET.test(line);
      const items: string[] = [];
      while (i < lines.length) {
        const bullet = BULLET.exec(lines[i]);
        const numbered = NUMBERED.exec(lines[i]);
        if (!bullet && !numbered) break;
        if (!!numbered !== ordered) break;
        items.push((bullet ? bullet[1] : numbered![2]).trim());
        i += 1;
        // A wrapped continuation line belongs to the item above it.
        while (i < lines.length && lines[i].startsWith("  ") && lines[i].trim()) {
          items[items.length - 1] += " " + lines[i].trim();
          i += 1;
        }
      }
      tokens.push({ kind: "list", ordered, items });
      continue;
    }

    const body: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !FENCE.test(lines[i].trim()) &&
      !HEADING.test(lines[i]) &&
      !QUOTE.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !NUMBERED.test(lines[i]) &&
      !RULE.test(lines[i].trim())
    ) {
      body.push(lines[i]);
      i += 1;
    }
    tokens.push({ kind: "para", text: body.join("\n") });
  }

  return tokens;
}

/**
 * Inline spans, tried in order: code first, so `**not bold**` inside backticks
 * stays literal, then links before bare URLs.
 *
 * `lead` names a capture group holding the character(s) that had to be matched
 * *before* the span in order to anchor it — the text that a lookbehind would
 * have asserted without consuming. Those characters are re-emitted as plain
 * text rather than swallowed.
 *
 * No lookbehind on purpose. Safari only gained support in 16.4, and an
 * unsupported lookbehind is a *parse*-time SyntaxError, not a runtime one — so
 * one in this module would take down every page that imports it, which here is
 * the whole site, on any older browser.
 */
const INLINE: {
  re: RegExp;
  lead?: number;
  render: (m: RegExpExecArray, k: number) => ReactNode;
}[] = [
  { re: /`([^`]+)`/, render: (m, k) => (
      <code key={k} className="rounded bg-white/10 px-1 py-0.5 font-mono text-[0.92em] text-sky-200">
        {m[1]}
      </code>
    ) },
  { re: /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/, render: (m, k) => (
      <a key={k} href={m[2]} target="_blank" rel="noopener noreferrer"
         className="text-sky-400 underline decoration-sky-400/40 underline-offset-2 hover:text-sky-300">
        {m[1]}
      </a>
    ) },
  { re: /\*\*([^*]+)\*\*/, render: (m, k) => (
      <strong key={k} className="font-semibold text-white">{m[1]}</strong>
    ) },
  // Italic: the opening * must not follow a word character or another *, or
  // "a*b*c" and the inside of "**bold**" would both match.
  { re: /(^|[^*\w])\*([^*\n]+)\*(?!\w)/, lead: 1, render: (m, k) => (
      <em key={k} className="italic">{m[2]}</em>
    ) },
  // A bare URL has to start at a boundary, or the tail of a longer token would
  // match.
  { re: /(^|\s)(https?:\/\/[^\s<>()]+)/, lead: 1, render: (m, k) => (
      <a key={k} href={m[2]} target="_blank" rel="noopener noreferrer"
         className="break-all text-sky-400 underline decoration-sky-400/40 underline-offset-2 hover:text-sky-300">
        {m[2]}
      </a>
    ) },
];

function inline(text: string, keyBase = 0): ReactNode[] {
  for (let r = 0; r < INLINE.length; r += 1) {
    const { re, lead, render } = INLINE[r];
    const match = re.exec(text);
    if (!match || match.index === undefined) continue;
    const anchor = lead ? match[lead] ?? "" : "";
    const before = text.slice(0, match.index) + anchor;
    const after = text.slice(match.index + match[0].length);
    return [
      ...inline(before, keyBase + 1),
      render(match, keyBase),
      ...inline(after, keyBase + 2),
    ];
  }
  return text ? [text] : [];
}

function InlineMarkdown({ text }: { text: string }) {
  return <>{inline(text)}</>;
}

function Markdown({ text }: { text: string }) {
  const tokens = tokenize(text);

  return (
    <div className="space-y-2.5 text-[13px] leading-relaxed text-white/85">
      {tokens.map((token, i) => {
        switch (token.kind) {
          case "code":
            return (
              <pre key={i} className="overflow-x-auto rounded-lg border border-white/10 bg-black/50 p-3">
                <code className="font-mono text-[11.5px] leading-relaxed text-sky-100">
                  {token.text}
                </code>
              </pre>
            );
          case "heading": {
            const size = token.level <= 1 ? "text-[15px]" : token.level === 2 ? "text-[14px]" : "text-[13px]";
            return (
              <p key={i} className={`${size} pt-1 font-semibold text-white`}>
                <InlineMarkdown text={token.text} />
              </p>
            );
          }
          case "quote":
            return (
              <blockquote key={i} className="border-l-2 border-sky-500/50 pl-3 text-white/60">
                <InlineMarkdown text={token.text} />
              </blockquote>
            );
          case "rule":
            return <hr key={i} className="border-white/10" />;
          case "list":
            return (
              <ul key={i} className="space-y-1.5 pl-1">
                {token.items.map((item, j) => (
                  <li key={j} className="flex gap-2">
                    <span className="select-none pt-px font-mono text-[11px] text-sky-400/70">
                      {token.ordered ? `${j + 1}.` : "—"}
                    </span>
                    <span className="min-w-0 flex-1">
                      <InlineMarkdown text={item} />
                    </span>
                  </li>
                ))}
              </ul>
            );
          default:
            return (
              <p key={i} className="whitespace-pre-wrap">
                {token.text.split("\n").map((line, j, all) => (
                  <Fragment key={j}>
                    <InlineMarkdown text={line} />
                    {j < all.length - 1 && <br />}
                  </Fragment>
                ))}
              </p>
            );
        }
      })}
    </div>
  );
}


// ── the drawer ──────────────────────────────────────────────────────────────

const STAGE_KEY: Record<JobStage, MessageKey> = {
  queued: "ai.stage.queued",
  opening: "ai.stage.opening",
  typing: "ai.stage.typing",
  generating: "ai.stage.generating",
  needs_input: "ai.stage.needs_input",
  reading: "ai.stage.reading",
  done: "ai.stage.done",
};

function Dots() {
  return (
    <span className="inline-flex gap-0.5" aria-hidden>
      {[0, 150, 300].map((delay) => (
        <span
          key={delay}
          className="h-1 w-1 animate-pulse rounded-full bg-sky-400"
          style={{ animationDelay: `${delay}ms` }}
        />
      ))}
    </span>
  );
}

/** The one thing most worth fixing, or null when everything is up. */
function healthProblem(
  health: Health | null,
  thread: string
): { title: MessageKey; hint: MessageKey } | null {
  if (!health) return null;
  if (health.offline) return { title: "ai.offline", hint: "ai.offlineHint" };
  if (!health.trusted) return { title: "ai.noTrust", hint: "ai.noTrustHint" };
  if (!health.app_running) return { title: "ai.noApp", hint: "ai.noAppHint" };
  if (!health.worker_running) return { title: "ai.noWorker", hint: "ai.noWorkerHint" };
  if (!thread) return { title: "ai.noChat", hint: "ai.noChatHint" };
  return null;
}

function CopyButton({ text }: { text: string }) {
  const { t } = useT();
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          // Clipboard access can be refused; the answer is still selectable.
        }
      }}
      className="rounded px-1.5 py-0.5 text-[10px] text-white/35 transition hover:bg-white/10 hover:text-white/70"
    >
      {done ? t("ai.copied") : t("ai.copy")}
    </button>
  );
}

function QuestionPanel({ job }: { job: Job }) {
  const { t } = useT();
  const answer = useAssistantStore((s) => s.answer);
  const [other, setOther] = useState("");
  const [busy, setBusy] = useState(false);

  const respond = async (payload: { choice?: string; text?: string; skip?: boolean }) => {
    setBusy(true);
    await answer(job.id, payload);
    setBusy(false);
  };

  return (
    <div className="mt-2 rounded-lg border border-amber-400/30 bg-amber-400/5 p-3">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-amber-300/80">
        {t("ai.question")}
      </p>
      <p className="mt-1.5 text-[13px] text-white/90">{job.question}</p>

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        {(job.options ?? []).map((option) => (
          <button
            key={option}
            disabled={busy}
            onClick={() => void respond({ choice: option })}
            className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1.5 text-[12px] text-white/90 transition hover:border-sky-400/50 hover:bg-sky-500/15 disabled:opacity-40"
          >
            {option}
          </button>
        ))}
      </div>

      <div className="mt-2 flex gap-1.5">
        <input
          value={other}
          disabled={busy}
          onChange={(e) => setOther(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && other.trim()) void respond({ text: other.trim() });
          }}
          placeholder={t("ai.questionOther")}
          className="min-w-0 flex-1 rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-[12px] text-white placeholder:text-white/25 focus:border-sky-500/60 focus:outline-none"
        />
        <button
          disabled={busy || !other.trim()}
          onClick={() => void respond({ text: other.trim() })}
          className="rounded-md bg-sky-500 px-2.5 py-1.5 text-[12px] font-medium text-white transition hover:bg-sky-400 disabled:opacity-30"
        >
          {t("ai.questionSend")}
        </button>
        <button
          disabled={busy}
          onClick={() => void respond({ skip: true })}
          className="rounded-md px-2 py-1.5 text-[12px] text-white/45 transition hover:bg-white/10 hover:text-white/80 disabled:opacity-40"
        >
          {t("ai.questionSkip")}
        </button>
      </div>
    </div>
  );
}

function Turn({ job }: { job: Job }) {
  const { t } = useT();
  const live = isLive(job);
  const body = job.answer ?? job.partial;

  return (
    <div className="space-y-2.5">
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-lg rounded-br-sm bg-sky-500/15 px-3 py-2 text-[13px] leading-relaxed text-white ring-1 ring-inset ring-sky-400/20">
          {job.prompt}
        </div>
      </div>

      <div className="group">
        <div className="mb-1 flex items-center gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-white/35">
            {t("ai.assistant")}
          </span>
          {live && (
            <span className="flex items-center gap-1.5 text-[10px] text-sky-400/80">
              <Dots />
              {t(STAGE_KEY[job.stage] ?? "ai.stage.queued")}
            </span>
          )}
          {job.status === "done" && job.answer && (
            <span className="opacity-0 transition group-hover:opacity-100">
              <CopyButton text={job.answer} />
            </span>
          )}
        </div>

        {body ? (
          <Markdown text={body} />
        ) : job.status === "error" ? null : (
          <p className="text-[13px] text-white/25">…</p>
        )}

        {job.status === "needs_input" && <QuestionPanel job={job} />}

        {job.status === "error" && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-3">
            <p className="text-[12px] leading-relaxed text-red-200/90">{job.error}</p>
          </div>
        )}
      </div>
    </div>
  );
}

function ChatPicker() {
  const { t } = useT();
  const chats = useAssistantStore((s) => s.chats);
  const thread = useAssistantStore((s) => s.thread);
  const selectThread = useAssistantStore((s) => s.selectThread);
  const selected = chats.find((c) => c.title === thread);

  return (
    <div className="border-b border-white/10 bg-black/30 px-4 py-3">
      <label className="text-[10px] font-semibold uppercase tracking-wide text-white/40">
        {t("ai.chatLabel")}
      </label>
      <select
        value={thread}
        onChange={(e) => void selectThread(e.target.value)}
        className="mt-1.5 w-full rounded-md border border-white/10 bg-neutral-900 px-2 py-1.5 text-[12px] text-white focus:border-sky-500/60 focus:outline-none"
      >
        <option value="">—</option>
        {chats.map((chat) => (
          <option key={chat.title} value={chat.title} disabled={chat.duplicate}>
            {chat.title}
            {chat.duplicate ? " ⚠" : ""}
            {!chat.in_sidebar ? " ·" : ""}
          </option>
        ))}
      </select>
      {selected?.duplicate && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-amber-300/80">
          {t("ai.chatDuplicate")}
        </p>
      )}
      {selected && !selected.in_sidebar && (
        <p className="mt-1.5 text-[11px] text-white/35">{t("ai.chatMissing")}</p>
      )}
    </div>
  );
}

export default function AssistantPanel() {
  const { t } = useT();
  const open = useAssistantStore((s) => s.open);
  const setOpen = useAssistantStore((s) => s.setOpen);
  const settingsOpen = useAssistantStore((s) => s.settingsOpen);
  const setSettingsOpen = useAssistantStore((s) => s.setSettingsOpen);
  const health = useAssistantStore((s) => s.health);
  const jobs = useAssistantStore((s) => s.jobs);
  const thread = useAssistantStore((s) => s.thread);
  const draft = useAssistantStore((s) => s.draft);
  const setDraft = useAssistantStore((s) => s.setDraft);
  const send = useAssistantStore((s) => s.send);
  const sending = useAssistantStore((s) => s.sending);
  const sendError = useAssistantStore((s) => s.sendError);
  const dismissError = useAssistantStore((s) => s.dismissError);

  const payload = useAppStore((s) => s.payload);
  const filters = useAppStore((s) => s.filters);
  const visible = useAppStore((s) => s.visible);
  const cart = useAppStore((s) => s.cart);

  const scrollRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const pinned = useRef(true);

  const problem = healthProblem(health, thread);
  const busy = jobs.some(isLive);

  const context = useMemo(
    () => siteContext(payload, filters, visible, cart),
    [payload, filters, visible, cart]
  );

  // Stick to the bottom, but only while the reader is already there - yanking
  // the view down mid-read of a long answer is worse than not following.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [jobs, open]);

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    box.style.height = "0px";
    box.style.height = `${Math.min(box.scrollHeight, 160)}px`;
  }, [draft, open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  if (!open) return null;

  const canSend = Boolean(draft.trim()) && !sending && !!thread && !problem;

  return (
    <aside
      className="absolute inset-y-0 right-0 z-30 flex w-full max-w-[440px] flex-col border-l border-white/10 bg-neutral-950/95 backdrop-blur-md sm:w-[440px]"
      aria-label={t("ai.title")}
    >
      <header className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
        {/* Neutral while the first health call is still out. Showing green
            before anything has been checked claims a working bridge that may
            not be there. */}
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
            !health
              ? "animate-pulse bg-white/30"
              : problem
                ? "bg-red-400"
                : busy || health.generating
                  ? "bg-amber-400"
                  : "bg-emerald-400"
          }`}
        />
        <h2 className="min-w-0 flex-1 truncate text-xs font-semibold text-white">
          {t("ai.title")}
          {thread && <span className="ml-2 font-normal text-white/35">{thread}</span>}
        </h2>
        <button
          onClick={() => setSettingsOpen(!settingsOpen)}
          aria-label={t("ai.settings")}
          className={`rounded p-1.5 transition hover:bg-white/10 ${
            settingsOpen ? "text-sky-400" : "text-white/40 hover:text-white/80"
          }`}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
        <button
          onClick={() => setOpen(false)}
          aria-label={t("ai.close")}
          className="rounded p-1.5 text-white/40 transition hover:bg-white/10 hover:text-white/80"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M18 6 6 18M6 6l12 12" />
          </svg>
        </button>
      </header>

      {settingsOpen && <ChatPicker />}

      {problem && (
        <div className="border-b border-amber-400/20 bg-amber-400/5 px-4 py-3">
          <p className="text-[12px] font-medium text-amber-200">{t(problem.title)}</p>
          <p className="mt-1 text-[11px] leading-relaxed text-amber-200/60">{t(problem.hint)}</p>
          {health?.error && (
            <p className="mt-1.5 font-mono text-[10px] leading-relaxed text-amber-200/40">
              {health.error}
            </p>
          )}
          {problem.title === "ai.noChat" && (
            <button
              onClick={() => setSettingsOpen(true)}
              className="mt-2 rounded-md border border-amber-400/30 px-2 py-1 text-[11px] text-amber-200 transition hover:bg-amber-400/10"
            >
              {t("ai.chatLabel")}
            </button>
          )}
        </div>
      )}

      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4"
      >
        {jobs.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center px-6 text-center">
            {health ? (
              <>
                <p className="text-[13px] text-white/50">{t("ai.empty")}</p>
                <p className="mt-2 text-[11px] leading-relaxed text-white/25">
                  {t("ai.emptyHint")}
                </p>
              </>
            ) : (
              <p className="flex items-center gap-2 text-[12px] text-white/30">
                <Dots />
                {t("ai.connecting")}
              </p>
            )}
          </div>
        ) : (
          jobs.map((job) => <Turn key={job.id} job={job} />)
        )}
      </div>

      {sendError && (
        <div className="flex items-start gap-2 border-t border-red-500/25 bg-red-500/5 px-4 py-2.5">
          <p className="min-w-0 flex-1 text-[11px] leading-relaxed text-red-200/90">{sendError}</p>
          <button
            onClick={dismissError}
            className="shrink-0 text-[11px] text-red-200/50 hover:text-red-200"
          >
            ✕
          </button>
        </div>
      )}

      <div className="border-t border-white/10 px-3 py-3">
        <div className="flex items-end gap-2 rounded-lg border border-white/10 bg-black/40 px-2.5 py-2 focus-within:border-sky-500/50">
          <textarea
            ref={boxRef}
            rows={1}
            value={draft}
            disabled={sending}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (canSend) void send(context);
              }
            }}
            placeholder={t("ai.placeholder")}
            className="min-w-0 flex-1 resize-none bg-transparent text-[13px] leading-relaxed text-white placeholder:text-white/25 focus:outline-none"
          />
          <button
            onClick={() => void send(context)}
            disabled={!canSend}
            aria-label={t("ai.send")}
            className="mb-0.5 shrink-0 rounded-md bg-sky-500 p-1.5 text-white transition hover:bg-sky-400 disabled:bg-white/10 disabled:text-white/30"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z" />
            </svg>
          </button>
        </div>
        <p className="mt-1.5 px-1 text-[10px] text-white/25">
          {busy ? t("ai.busy") : payload ? t("ai.contextOn") : t("ai.sendHint")}
        </p>
      </div>
    </aside>
  );
}


// ── the launcher ────────────────────────────────────────────────────────────

/** The button that opens the assistant, with a dot when something is waiting. */
export function AssistantLauncher() {
  const { t } = useT();
  const open = useAssistantStore((s) => s.open);
  const setOpen = useAssistantStore((s) => s.setOpen);
  const jobs = useAssistantStore((s) => s.jobs);

  const waiting = jobs.some((j) => j.status === "needs_input");
  const busy = jobs.some(isLive);

  if (open) return null;

  return (
    <button
      onClick={() => setOpen(true)}
      aria-label={t("ai.open")}
      className="pointer-events-auto relative flex items-center gap-2 rounded-lg border border-white/10 bg-neutral-950/85 px-3 py-2 text-xs font-medium text-white/80 backdrop-blur transition hover:border-sky-500/40 hover:text-white"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
      </svg>
      {t("ai.title")}
      {(waiting || busy) && (
        <span
          className={`absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full ${
            waiting ? "bg-amber-400" : "animate-pulse bg-sky-400"
          }`}
        />
      )}
    </button>
  );
}
