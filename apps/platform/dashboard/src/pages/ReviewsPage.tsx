// Miner sign-in and private console. The public ATH queue moved to #/ath.
import { For, Show, createEffect, createMemo, createSignal, onCleanup } from "solid-js";
import type { JSX } from "solid-js";

import { HandleBadge } from "../components/ui/HandleBadge";
import { MinerAvatar } from "../components/ui/MinerAvatar";
import { API_BASE } from "../lib/config";
import { authJSON, postJSON } from "../lib/api";
import { copyText } from "../lib/copy";
import { fullEntityHref } from "../lib/router";
import {
  clearMinerSession,
  minerSession,
  sessionAuthHeader,
  setMinerSession,
} from "../stores/sessionStore";
import type { NameHandle } from "../types/leaderboard";

const SCOPES: Array<{ id: string; label: string; hint: string }> = [
  { id: "read", label: "Read", hint: "Required. Profile, my submissions, my reviews" },
  { id: "profile", label: "Profile", hint: "Set picture, X, GitHub, Discord" },
  { id: "download", label: "Download", hint: "Download my own submissions" },
  { id: "upload", label: "Upload", hint: "Returns a ditto upload command" },
  { id: "handle", label: "Username", hint: "Returns a ditto name claim command" },
  { id: "challenges", label: "Challenges", hint: "Returns dispute / appeal commands" },
];

const POLL_STORAGE_KEY = "ditto.miner.poll.v1";

const HOURS = [
  { hours: 1, label: "1 hour" },
  { hours: 8, label: "8 hours" },
  { hours: 24, label: "24 hours" },
  { hours: 168, label: "7 days" },
  { hours: 720, label: "30 days" },
];

interface DeviceStart {
  user_code: string;
  poll_token: string;
  login_command: string;
  login_clone?: string | null;
  verification_uri_complete: string;
  scopes: string[];
  ttl_seconds: number;
}

interface DevicePublic {
  user_code: string;
  status: string;
  login_command: string;
  login_clone?: string | null;
  scopes?: string[];
  ttl_seconds?: number;
  miner_hotkey?: string | null;
  oauth?: boolean;
}

interface DeviceStatus {
  status: string;
  access_token?: string | null;
  session?: {
    miner_hotkey: string;
    scopes: string[];
    expires_at: string;
  } | null;
  continue_url?: string | null;
  oauth?: boolean;
}

interface MinerMe {
  session: {
    miner_hotkey: string;
    scopes: string[];
    expires_at: string;
    expires_in: number;
  };
  profile: { x_url?: string | null; github_url?: string | null; discord_handle?: string | null };
  name_handle?: NameHandle | null;
  avatar_url?: string | null;
  profile_url: string;
  commands: Array<{ action: string; command: string; reason: string }>;
}

interface MinerSubmission {
  agent_id: string;
  name: string;
  status: string;
  created_at: string;
}

interface MinerReview {
  kind: string;
  agent_id: string;
  name: string;
  status: string;
  opened_at: string;
  detail?: string | null;
}

interface StoredPollGrant {
  user_code: string;
  poll_token: string;
  expires_at: number;
}

function hashParams(): URLSearchParams {
  return new URLSearchParams(location.hash.split("?")[1] || "");
}

function persistPollGrant(userCode: string, pollToken: string): void {
  try {
    const record: StoredPollGrant = {
      user_code: userCode,
      poll_token: pollToken,
      expires_at: Date.now() + 15 * 60 * 1000,
    };
    localStorage.setItem(POLL_STORAGE_KEY, JSON.stringify(record));
  } catch {
    // storage can be unavailable; in-memory pending still works.
  }
}

function readPollGrant(): StoredPollGrant | null {
  try {
    const raw = localStorage.getItem(POLL_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredPollGrant;
    if (!parsed.user_code || !parsed.poll_token || !parsed.expires_at) return null;
    if (parsed.expires_at <= Date.now()) {
      localStorage.removeItem(POLL_STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function writeLoginHash(userCode: string, completeToken?: string): void {
  const params = new URLSearchParams({ code: userCode });
  if (completeToken) params.set("complete", completeToken);
  history.replaceState(history.state ?? {}, "", "#/reviews?" + params.toString());
}

export function ReviewsPage(): JSX.Element {
  const params = () => hashParams();
  const presetCode = () => params().get("code") || params().get("login") || "";
  const completeToken = () => params().get("complete") || "";

  return (
    <section class="page active account-page" data-page="reviews">
      <Show
        when={minerSession()}
        fallback={<SignInPanel presetCode={presetCode()} completeToken={completeToken()} />}
      >
        <AccountPanel />
      </Show>
    </section>
  );
}

function SignInPanel(props: { presetCode: string; completeToken: string }): JSX.Element {
  const [hours, setHours] = createSignal(24);
  const [scopes, setScopes] = createSignal<string[]>(["read", "profile"]);
  const [pending, setPending] = createSignal<DeviceStart | null>(null);
  const [status, setStatus] = createSignal("idle");
  const [error, setError] = createSignal("");
  const [copied, setCopied] = createSignal(false);
  const [oauth, setOauth] = createSignal(false);
  let stopped = false;

  const command = createMemo(() => pending()?.login_command || "");
  const terminal = (value: string) =>
    value === "expired" || value === "denied" || value === "consumed";

  createEffect(() => {
    if (pending()) return;
    const hashCode = props.presetCode;
    const hashComplete = props.completeToken;
    const stored = readPollGrant();
    void (async () => {
      const code = hashCode || stored?.user_code || "";
      const pollToken = hashComplete || stored?.poll_token || "";
      if (!code) return;
      if (!pollToken) {
        setError("This browser cannot finish that sign-in. Start a new one here.");
        return;
      }
      try {
        const publicDevice = await authJSON<DevicePublic>(
          "/miner-auth/device/" + encodeURIComponent(code),
        );
        if (stored && stored.user_code !== publicDevice.user_code && !hashCode) {
          return;
        }
        const grant: DeviceStart = {
          user_code: publicDevice.user_code,
          poll_token: pollToken,
          login_command: publicDevice.login_command,
          login_clone: publicDevice.login_clone,
          verification_uri_complete: location.href,
          scopes: publicDevice.scopes || [],
          ttl_seconds: publicDevice.ttl_seconds || 0,
        };
        persistPollGrant(grant.user_code, pollToken);
        setOauth(Boolean(publicDevice.oauth) || Boolean(hashComplete));
        setPending(grant);
        setStatus(publicDevice.status);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load that login code.");
      }
    })();
  });

  createEffect(() => {
    const grant = pending();
    if (!grant) return;
    stopped = false;
    if (!grant.poll_token) {
      setError("This browser cannot finish that sign-in. Start a new one here.");
      return;
    }
    void poll(grant);
    const timer = window.setInterval(() => {
      if (!stopped) void poll(grant);
    }, 2000);
    onCleanup(() => window.clearInterval(timer));
  });

  async function start(): Promise<void> {
    setError("");
    const requested = scopes().includes("read") ? scopes() : ["read", ...scopes()];
    try {
      const started = await postJSON<DeviceStart>("/miner-auth/device", {
        scopes: requested,
        ttl_seconds: hours() * 3600,
      });
      persistPollGrant(started.user_code, started.poll_token);
      writeLoginHash(started.user_code);
      setOauth(false);
      setPending(started);
      setStatus("pending");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start sign-in.");
    }
  }

  async function finishOauth(grant: DeviceStart): Promise<void> {
    try {
      const result = await authJSON<{ redirect_to?: string }>("/mcp/oauth/complete", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ code: grant.user_code, complete: grant.poll_token }),
      });
      if (result.redirect_to) location.assign(result.redirect_to);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not finish MCP login.");
    }
  }

  async function poll(grant: DeviceStart): Promise<void> {
    try {
      const result = await authJSON<DeviceStatus>(
        "/miner-auth/device/" + encodeURIComponent(grant.user_code) + "/status",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ poll_token: grant.poll_token }),
        },
      );
      setStatus(result.status);
      if (result.access_token && result.session) {
        setMinerSession({
          token: result.access_token,
          hotkey: result.session.miner_hotkey,
          scopes: result.session.scopes,
          expiresAt: result.session.expires_at,
        });
        try {
          localStorage.removeItem(POLL_STORAGE_KEY);
        } catch {
          // ignore
        }
      }
      if ((result.oauth || oauth()) && result.status === "approved" && grant.poll_token) {
        stopped = true;
        await finishOauth(grant);
        return;
      }
      if (terminal(result.status) && !result.access_token) {
        stopped = true;
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Polling failed.");
    }
  }

  function toggleScope(id: string): void {
    if (id === "read") return;
    setScopes((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  return (
    <div class="account-sign">
      <div class="account-hero">
        <p class="ath-eyebrow">Miner console</p>
        <h2>Sign in with your hotkey</h2>
        <p>
          Copy the <code>uvx</code> command, run it, and this page becomes your private backroom. No
          clone required. The CLI will offer to pick your coldkey and hotkey from{" "}
          <code>~/.bittensor/wallets</code>. No TAO moves.
        </p>
      </div>
      <div class="account-grid">
        <div class="account-card">
          <h3>Permissions</h3>
          <For each={SCOPES}>
            {(item) => (
              <label class="account-scope">
                <input
                  type="checkbox"
                  checked={scopes().includes(item.id)}
                  disabled={item.id === "read" || Boolean(props.presetCode)}
                  onChange={() => toggleScope(item.id)}
                />
                <span>
                  <strong>{item.label}</strong>
                  <em>{item.hint}</em>
                </span>
              </label>
            )}
          </For>
        </div>
        <div class="account-card">
          <h3>Session length</h3>
          <div class="account-hours">
            <For each={HOURS}>
              {(item) => (
                <button
                  class="btn ghost"
                  classList={{ active: hours() === item.hours }}
                  disabled={Boolean(props.presetCode)}
                  onClick={() => setHours(item.hours)}
                >
                  {item.label}
                </button>
              )}
            </For>
          </div>
          <Show when={!pending()}>
            <button class="btn" onClick={() => void start()} disabled={!scopes().length}>
              Start sign-in
            </button>
          </Show>
        </div>
      </div>
      <Show when={command()}>
        <div class="account-command">
          <div>
            <span class="ath-eyebrow">Run this locally</span>
            <pre>{command()}</pre>
            <p class="muted">
              Omit <code>--coldkey</code> / <code>--hotkey</code> — the CLI asks to search your
              local wallets and uses <code>fzf</code> when it is installed.
            </p>
            <Show when={pending()?.login_clone}>
              {(clone) => (
                <>
                  <span class="ath-eyebrow">Or clone the repo</span>
                  <pre>{clone()}</pre>
                </>
              )}
            </Show>
            <p class="muted">Status: {status()}</p>
          </div>
          <button
            class="btn ghost"
            onClick={() => {
              void copyText(command()).then(() => setCopied(true));
            }}
          >
            {copied() ? "Copied" : "Copy command"}
          </button>
        </div>
      </Show>
      <Show when={error()}>
        <p class="account-error">{error()}</p>
      </Show>
      <p class="muted">
        Public ATH holds now live at <a href="#/ath">#/ath</a>. Your submissions stay on{" "}
        <a href="#/submissions">#/submissions</a>.
      </p>
    </div>
  );
}

function AccountPanel(): JSX.Element {
  const [me, setMe] = createSignal<MinerMe | null>(null);
  const [tab, setTab] = createSignal<"profile" | "submissions" | "reviews" | "mcp">("profile");
  const [subs, setSubs] = createSignal<MinerSubmission[]>([]);
  const [reviews, setReviews] = createSignal<MinerReview[]>([]);
  const [xUrl, setXUrl] = createSignal("");
  const [github, setGithub] = createSignal("");
  const [discord, setDiscord] = createSignal("");
  const [error, setError] = createSignal("");
  const [saved, setSaved] = createSignal("");

  createEffect(() => {
    void loadMe();
  });

  async function loadMe(): Promise<boolean> {
    try {
      const body = await authJSON<MinerMe>("/me", { headers: sessionAuthHeader() });
      setMe(body);
      setXUrl(body.profile.x_url || "");
      setGithub(body.profile.github_url || "");
      setDiscord(body.profile.discord_handle || "");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Session expired.";
      setError(message);
      if (message.includes("HTTP 401") || message.includes("invalid or expired")) {
        clearMinerSession();
      }
      return false;
    }
    try {
      const mine = await authJSON<MinerSubmission[]>("/me/submissions", {
        headers: sessionAuthHeader(),
      });
      setSubs(mine);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load submissions.");
    }
    try {
      const held = await authJSON<{ reviews: MinerReview[] }>("/me/reviews", {
        headers: sessionAuthHeader(),
      });
      setReviews(held.reviews);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load reviews.");
    }
    return true;
  }

  async function saveProfile(): Promise<void> {
    setError("");
    setSaved("");
    try {
      const body = await authJSON<MinerMe>("/me", {
        method: "PATCH",
        headers: { ...sessionAuthHeader(), "content-type": "application/json" },
        body: JSON.stringify({
          x_url: xUrl() || null,
          github_url: github() || null,
          discord_handle: discord() || null,
        }),
      });
      setMe(body);
      setSaved("Profile saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save profile.");
    }
  }

  async function uploadAvatar(file: File): Promise<void> {
    const data = new FormData();
    data.append("file", file);
    setError("");
    setSaved("");
    try {
      const response = await fetch(API_BASE + "/me/avatar", {
        method: "POST",
        headers: sessionAuthHeader(),
        body: data,
      });
      const payload: unknown = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail =
          typeof payload === "object" &&
          payload !== null &&
          typeof (payload as { detail?: unknown }).detail === "string"
            ? (payload as { detail: string }).detail
            : "Could not upload picture.";
        if (response.status === 401 || detail.includes("invalid or expired")) {
          clearMinerSession();
        }
        throw new Error(detail);
      }
      if (await loadMe()) setSaved("Picture updated.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not upload picture.");
    }
  }

  const handle = () => me()?.name_handle ?? null;
  const title = () => handle()?.stem || minerSession()?.hotkey || "Miner";

  return (
    <div class="account-console">
      <header class="account-head">
        <MinerAvatar url={me()?.avatar_url} size="lg" />
        <div>
          <p class="ath-eyebrow">Signed in</p>
          <h2>{title()}</h2>
          <HandleBadge handle={handle()} />
          <p class="mono muted">{me()?.session.miner_hotkey}</p>
          <p class="muted">Expires {me()?.session.expires_at}</p>
        </div>
        <div class="account-head-actions">
          <a class="btn ghost" href={fullEntityHref("miner", me()?.session.miner_hotkey || "")}>
            Public profile
          </a>
          <button class="btn ghost" onClick={() => clearMinerSession()}>
            Log out
          </button>
        </div>
      </header>
      <nav class="account-tabs">
        <For each={["profile", "submissions", "reviews", "mcp"] as const}>
          {(item) => (
            <button
              class="btn ghost"
              classList={{ active: tab() === item }}
              onClick={() => setTab(item)}
            >
              {item}
            </button>
          )}
        </For>
      </nav>
      <Show when={tab() === "profile"}>
        <div class="account-card">
          <h3>Profile</h3>
          <label class="account-field">
            Picture
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              onChange={(ev) => {
                const file = ev.currentTarget.files?.[0];
                if (file) void uploadAvatar(file);
              }}
            />
          </label>
          <label class="account-field">
            X
            <input
              value={xUrl()}
              onInput={(ev) => setXUrl(ev.currentTarget.value)}
              placeholder="https://x.com/you"
            />
          </label>
          <label class="account-field">
            GitHub
            <input
              value={github()}
              onInput={(ev) => setGithub(ev.currentTarget.value)}
              placeholder="https://github.com/you"
            />
          </label>
          <label class="account-field">
            Discord
            <input
              value={discord()}
              onInput={(ev) => setDiscord(ev.currentTarget.value)}
              placeholder="handle"
            />
          </label>
          <p class="muted">
            Username is your reserved handle. Claim or change it with{" "}
            <code>ditto name claim --name &lt;handle&gt;</code>.
          </p>
          <button class="btn" onClick={() => void saveProfile()}>
            Save profile
          </button>
        </div>
      </Show>
      <Show when={tab() === "submissions"}>
        <div class="account-card">
          <h3>My submissions</h3>
          <Show when={subs().length} fallback={<p class="muted">No submissions yet.</p>}>
            <ul class="account-list">
              <For each={subs()}>
                {(item) => (
                  <li>
                    <a href={"/agent/" + item.agent_id}>{item.name}</a>
                    <span class="muted">
                      {item.status} · {item.created_at}
                    </span>
                  </li>
                )}
              </For>
            </ul>
          </Show>
        </div>
      </Show>
      <Show when={tab() === "reviews"}>
        <div class="account-card">
          <h3>My reviews</h3>
          <Show when={reviews().length} fallback={<p class="muted">No ATH holds or disputes.</p>}>
            <ul class="account-list">
              <For each={reviews()}>
                {(item) => (
                  <li>
                    <a href={"/agent/" + item.agent_id}>
                      {item.kind}: {item.name}
                    </a>
                    <span class="muted">
                      {item.status} · {item.detail || ""}
                    </span>
                  </li>
                )}
              </For>
            </ul>
          </Show>
        </div>
      </Show>
      <Show when={tab() === "mcp"}>
        <div class="account-card">
          <h3>Miner MCP</h3>
          <p>
            Add <code>{location.origin}/mcp</code> to your coding agent. The agent opens this page,
            you run the copied <code>ditto login</code> command, and the session is authorized for
            the hours you picked.
          </p>
          <p class="muted">
            Tools that still need a hotkey signature return the matching <code>ditto</code> command
            instead of signing remotely.
          </p>
          <ul class="account-list">
            <For each={me()?.commands || []}>
              {(item) => (
                <li>
                  <strong>{item.action}</strong>
                  <pre>{item.command}</pre>
                  <span class="muted">{item.reason}</span>
                </li>
              )}
            </For>
          </ul>
        </div>
      </Show>
      <Show when={saved()}>
        <p class="muted">{saved()}</p>
      </Show>
      <Show when={error()}>
        <p class="account-error">{error()}</p>
      </Show>
    </div>
  );
}
