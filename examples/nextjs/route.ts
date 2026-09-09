/**
 * Server-side proxy to the local Claude Desktop bridge.
 *
 * The bridge binds 127.0.0.1 and has no authentication - anything that can
 * reach it can drive the operator's Claude session. So the browser never talks
 * to it: this route is the only thing that does, it runs on the Next.js server,
 * and it forwards a fixed set of paths and nothing else.
 */
import { NextResponse } from "next/server";
import { clientKey, rateLimit } from "@/lib/rateLimit";

export const runtime = "nodejs";
// The bridge's state changes constantly; a cached poll would show a finished
// job as still running.
export const dynamic = "force-dynamic";

const BRIDGE = process.env.BRIDGE_URL ?? "http://127.0.0.1:8765";

/**
 * Only these reach the bridge. An open proxy would expose every future endpoint
 * it grows, including any that read the operator's machine.
 */
const ALLOWED: { method: string; pattern: RegExp }[] = [
  { method: "GET", pattern: /^health$/ },
  { method: "GET", pattern: /^chats$/ },
  { method: "GET", pattern: /^config$/ },
  { method: "POST", pattern: /^config$/ },
  { method: "GET", pattern: /^messages$/ },
  { method: "POST", pattern: /^messages$/ },
  { method: "GET", pattern: /^messages\/[a-z0-9]{6,32}$/ },
  { method: "POST", pattern: /^messages\/[a-z0-9]{6,32}\/answer$/ },
];

/** Long enough for a slow accessibility walk, short enough not to pile up. */
const TIMEOUT_MS = 20000;

/**
 * Sending drives the operator's real Claude session, so it is capped. The read
 * endpoints are deliberately not limited: the panel polls them about once a
 * second while an answer is in flight, and a limit low enough to matter would
 * break the UI before it stopped anyone.
 */
const SEND_LIMIT = 12;
const SEND_WINDOW_MS = 5 * 60 * 1000;

/**
 * Whether this request came from the machine running the site.
 *
 * The bridge has no authentication and drives the operator's own Claude
 * Desktop, so by default only the local machine may reach it. `next dev`
 * listens on every interface, which means without this check anyone on the
 * same Wi-Fi can open the site and type into the operator's Claude.
 *
 * This reads the Host header, which a determined caller on the network can
 * forge. It is a guard against accidental exposure, not an access control:
 * for real isolation bind the server itself with `next dev -H 127.0.0.1`.
 * Set ASSISTANT_ALLOW_REMOTE=1 to serve the assistant to other machines
 * knowingly.
 */
function fromThisMachine(req: Request): boolean {
  if (process.env.ASSISTANT_ALLOW_REMOTE === "1") return true;
  const host = (req.headers.get("host") ?? "").split(":")[0].toLowerCase();
  return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
}

function allowed(method: string, path: string): boolean {
  return ALLOWED.some((rule) => rule.method === method && rule.pattern.test(path));
}

async function forward(req: Request, path: string, method: "GET" | "POST") {
  if (!allowed(method, path)) {
    return NextResponse.json({ detail: "Not available." }, { status: 404 });
  }

  if (!fromThisMachine(req)) {
    return NextResponse.json(
      {
        detail:
          "The assistant only answers on the machine it runs on. Set " +
          "ASSISTANT_ALLOW_REMOTE=1 to serve it to other devices.",
      },
      { status: 403 }
    );
  }

  if (method === "POST" && path === "messages") {
    const limit = rateLimit(`assistant:${clientKey(req)}`, SEND_LIMIT, SEND_WINDOW_MS);
    if (!limit.ok) {
      return NextResponse.json(
        { detail: `Too many messages. Try again in ${limit.retryAfter}s.` },
        { status: 429, headers: { "Retry-After": String(limit.retryAfter) } }
      );
    }
  }

  const url = new URL(req.url);
  const target = `${BRIDGE}/api/${path}${url.search}`;

  let body: string | undefined;
  if (method === "POST") {
    body = await req.text();
  }

  try {
    const res = await fetch(target, {
      method,
      headers: { "Content-Type": "application/json" },
      body,
      signal: AbortSignal.timeout(TIMEOUT_MS),
      cache: "no-store",
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": res.headers.get("Content-Type") ?? "application/json" },
    });
  } catch (e) {
    // A refused connection is the ordinary case - the operator has not started
    // the bridge - so it gets its own shape rather than a generic 500. The
    // health endpoint answers with a body the panel can render as a banner.
    const offline =
      e instanceof Error && (e.name === "TimeoutError" || e.name === "AbortError")
        ? `The bridge at ${BRIDGE} did not answer in time. Claude Desktop may be busy.`
        : `No assistant bridge is answering at ${BRIDGE}. Start it with ` +
          `./start-assistant.sh, or correct BRIDGE_URL in web/.env.local.`;
    if (path === "health") {
      // 200 with an offline body, so the panel can render a banner rather than
      // an exception - but `where` names the address that failed. Without it a
      // misconfigured BRIDGE_URL is indistinguishable from a bridge that is
      // simply not started, and the panel tells you to start one that is
      // already running.
      return NextResponse.json(
        {
          ok: false,
          offline: true,
          where: BRIDGE,
          worker_running: false,
          trusted: false,
          app_running: false,
          thread: null,
          generating: false,
          chats: [],
          error: offline,
        },
        { status: 200 }
      );
    }
    return NextResponse.json({ detail: offline }, { status: 503 });
  }
}

export async function GET(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  return forward(req, path.join("/"), "GET");
}

export async function POST(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  return forward(req, path.join("/"), "POST");
}
