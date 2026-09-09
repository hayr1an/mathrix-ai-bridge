# The web side

Three files from the Mathrix AI site, as a reference integration.

| | |
|---|---|
| `route.ts` | The server-side proxy. The browser never reaches the bridge: this runs on the Next.js server, forwards an explicit allowlist of paths, refuses non-loopback `Host`, and rate-limits sending. |
| `assistant.ts` | The job model, the site-context builder, and the store that polls. Two loops with a generation counter, so a request still in flight when the panel closes cannot reschedule itself. |
| `Assistant.tsx` | The drawer, its launcher, and a small Markdown renderer that builds React elements rather than HTML — a code block containing `<script>` is text, like any other text. |

They are not drop-in: they import from the site they came from (`@/lib/types`,
`@/store/useAppStore`). They are here to show the shape — particularly how the
bridge is kept off the network, and how a queue is presented as a chat.
