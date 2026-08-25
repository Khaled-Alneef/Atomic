/**
 * Atomic's rating write proxy.
 *
 * WHY THIS EXISTS
 * ---------------
 * Reading the community ratings needs nothing: raw.githubusercontent
 * serves the `ratings` branch publicly. Writing one needs a GitHub token
 * with Contents:write - and a token cannot be shipped inside Atomic.exe,
 * for two reasons that were both checked rather than assumed:
 *
 *   * a bundled file is not hidden. The exe's own archive lists it by
 *     name and PyInstaller's reader hands it over in 0ms - that is how
 *     the TMDB token comes out today. Encrypting it only moves the
 *     problem, since the app must decrypt it at runtime.
 *   * Atomic.exe is committed to `main` at every release, so anything
 *     bundled in it is published in a public repository. GitHub scans
 *     repository contents for its own token patterns and revokes what it
 *     finds; an embedded PAT would be dead within minutes of a release
 *     and ratings would break for everybody.
 *
 * So the token lives here instead, on the owner's Cloudflare account,
 * and the app carries only a URL. A URL is not a secret.
 *
 * DEPLOYING IT (about five minutes, no command line)
 * -------------------------------------------------
 *  1. Make a fine-grained GitHub token: github.com/settings/tokens
 *     Repository access: only `Khaled-Alneef/Atomic`.
 *     Permissions: Contents -> Read and write. Nothing else.
 *  2. dash.cloudflare.com -> Workers & Pages -> Create -> Worker.
 *     Name it (say) `atomic-ratings`, Deploy, then Edit code and paste
 *     this file over what is there. Deploy again.
 *  3. Settings -> Variables and Secrets -> add:
 *       GITHUB_TOKEN   (type: Secret)  the token from step 1
 *       REPO           (Text)          Khaled-Alneef/Atomic
 *       BRANCH         (Text)          ratings
 *  4. Copy the worker's URL and put it in
 *     helpers/community_ratings.DEFAULT_PROXY, or paste it into
 *     Settings > API Keys while testing.
 *
 * WHAT IT DOES AND DOES NOT PREVENT
 * ---------------------------------
 * It validates every field, so nothing here can be talked into writing
 * outside `ratings/<key>.json` - the key is matched against a strict
 * pattern before it is ever put in a path. It rejects scores outside the
 * scale and caps how many votes one file may hold.
 *
 * It does *not* prove who is voting. Anybody who finds the URL can post
 * a rating, and a determined person can post many by inventing voter
 * ids. The blast radius is bounded on purpose: the token reaches one
 * repository's contents, the store is a branch nothing merges from, and
 * the whole thing is restorable with a force push from a local copy.
 * If it is ever abused, change the worker's URL and ship a build - the
 * token itself never leaves Cloudflare.
 */

const KEY_RE = /^[a-z0-9_-]{1,80}$/;
const ITEM_RE = /^(\d{1,3}x\d{1,4}|c\d{1,6}(\.\d{1,2})?)$/;
const VOTER_RE = /^[a-f0-9]{8,32}$/;
const MIN_SCORE = 1;
const MAX_SCORE = 10;
// A title nobody could have watched this many times is a script, not an
// audience. The write is refused rather than the file being allowed to
// grow without bound.
const MAX_VOTES_PER_ITEM = 5000;
const MAX_ITEMS_PER_TITLE = 2000;

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));
    if (request.method !== "POST") return cors(json({ ok: false, error: "POST only" }, 405));

    let body;
    try {
      body = await request.json();
    } catch {
      return cors(json({ ok: false, error: "bad json" }, 400));
    }

    const key = String(body.key || "");
    const item = String(body.item || "");
    const voter = String(body.voter || "");
    const score = Number(body.score);
    const title = String(body.title || "").slice(0, 200);
    const kind = body.kind === "reading" ? "reading" : "video";

    if (!KEY_RE.test(key)) return cors(json({ ok: false, error: "bad key" }, 400));
    if (!ITEM_RE.test(item)) return cors(json({ ok: false, error: "bad item" }, 400));
    if (!VOTER_RE.test(voter)) return cors(json({ ok: false, error: "bad voter" }, 400));
    if (!Number.isInteger(score) || score < MIN_SCORE || score > MAX_SCORE) {
      return cors(json({ ok: false, error: "bad score" }, 400));
    }

    const repo = env.REPO || "Khaled-Alneef/Atomic";
    const branch = env.BRANCH || "ratings";
    const path = `ratings/${key}.json`;

    // Two attempts: the contents API refuses a write whose sha is not
    // the file's current one, which is exactly what happens when
    // somebody else rated the same title in between. Re-read and merge.
    for (let attempt = 0; attempt < 2; attempt++) {
      const current = await read(env, repo, branch, path);
      const doc = current.doc || { key, title, kind, items: {} };
      doc.key = doc.key || key;
      if (title && !doc.title) doc.title = title;
      doc.items = doc.items || {};
      if (!doc.items[item] && Object.keys(doc.items).length >= MAX_ITEMS_PER_TITLE) {
        return cors(json({ ok: false, error: "too many items" }, 429));
      }
      const bucket = (doc.items[item] = doc.items[item] || { votes: {} });
      bucket.votes = bucket.votes || {};
      if (!bucket.votes[voter] && Object.keys(bucket.votes).length >= MAX_VOTES_PER_ITEM) {
        return cors(json({ ok: false, error: "too many votes" }, 429));
      }
      bucket.votes[voter] = { score, at: new Date().toISOString() };

      const wrote = await write(env, repo, branch, path, doc, current.sha,
                                `Rate ${doc.title || key} ${item}`);
      if (wrote.ok) return cors(json({ ok: true }));
      if (wrote.status !== 409 && wrote.status !== 422) {
        return cors(json({ ok: false, error: `github ${wrote.status}` }, 502));
      }
    }
    return cors(json({ ok: false, error: "conflict" }, 409));
  },
};

function headers(env) {
  return {
    "User-Agent": "atomic-ratings-worker",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Content-Type": "application/json",
  };
}

async function read(env, repo, branch, path) {
  const url = `https://api.github.com/repos/${repo}/contents/${path}?ref=${branch}`;
  const response = await fetch(url, { headers: headers(env) });
  if (response.status === 404) return { doc: null, sha: null };
  if (!response.ok) return { doc: null, sha: null };
  const meta = await response.json();
  try {
    const raw = atob((meta.content || "").replace(/\n/g, ""));
    const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    return { doc: JSON.parse(new TextDecoder().decode(bytes)), sha: meta.sha };
  } catch {
    // Unreadable: start the file again rather than lose this write.
    return { doc: null, sha: meta.sha };
  }
}

async function write(env, repo, branch, path, doc, sha, message) {
  const text = JSON.stringify(doc, Object.keys(doc).sort(), 1);
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  const payload = { message, branch, content: btoa(binary) };
  if (sha) payload.sha = sha;
  const response = await fetch(`https://api.github.com/repos/${repo}/contents/${path}`, {
    method: "PUT",
    headers: headers(env),
    body: JSON.stringify(payload),
  });
  return { ok: response.ok, status: response.status };
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function cors(response) {
  response.headers.set("Access-Control-Allow-Origin", "*");
  response.headers.set("Access-Control-Allow-Headers", "Content-Type");
  response.headers.set("Access-Control-Allow-Methods", "POST, OPTIONS");
  return response;
}
