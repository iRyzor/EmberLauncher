import { invoke } from "@tauri-apps/api/core";
export type Snapshot = {
  remember_session?: boolean;
  account_warning?: string | null;
  phase: string;
  detail: string;
  image?: string | null;
  connected: boolean;
  reauth_required?: boolean;
  version: number | null;
  updates?: { published_version: number; update_available: boolean };
  job?: {
    kind: string;
    running: boolean;
    detail?: string;
    cancelling?: boolean;
    progress?: {
      phase: string;
      downloaded_bytes: number;
      total_bytes: number;
      bytes_per_second: number;
      eta_seconds?: number | null;
      package_index?: number;
      package_count?: number;
      path?: string;
    };
    error?: string;
    result?: Record<string, unknown>;
  };
  prepared: boolean;
  resumable_download?: { published_version: number } | null;
  game_running?: boolean;
  game_starting?: boolean;
  recovery_needed?: boolean;
  prepared_summary?: {
    installed_version: number;
    published_version: number;
    change_count: number;
    download_bytes: number;
    changes: { path: string; action: string }[];
  } | null;
  root: string;
  game_directory?: string | null;
  suggested_prefix?: string;
  platform?: string;
  native_game?: boolean;
  runtime_prefix?: string | null;
  runner: string;
  runners?: { name: string; path: string; kind: string }[];
  env?: string;
  game_installed: boolean;
  fresh_destination?: boolean;
  error?: string;
};
async function rpc<T>(
  action: string,
  payload?: Record<string, unknown>,
): Promise<T> {
  if ("__TAURI_INTERNALS__" in window)
    return invoke("engine_request", { action, payload });
  const result = await fetch("/api", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, payload }),
  });
  if (!result.ok)
    throw new Error("No se pudo contactar al motor del launcher.");
  return result.json();
}

export function request(
  action: string,
  payload?: Record<string, unknown>,
): Promise<Snapshot> {
  return rpc<Snapshot>(action, payload);
}
export type NewsItem = {
  id: number;
  title: string;
  summary: string;
  category: string;
  published_at: number;
  updated_at: number;
  image: string | null;
  url: string;
  content?: string;
};
export type NewsResponse = {
  news: {
    items?: NewsItem[];
    article?: NewsItem;
    fetched_at: number;
    partial?: boolean;
  } | null;
  loading: boolean;
  cached: boolean;
  error?: string | null;
};
export function newsRequest(
  gameId: string,
  articleId?: number,
  refresh = false,
): Promise<NewsResponse> {
  return rpc<NewsResponse>("news", {
    game_id: gameId,
    refresh,
    ...(articleId ? { article_id: articleId } : {}),
  });
}
export async function openNewsOriginal(url: string) {
  if (
    !/^https:\/\/page\.onstove\.com\/chaoszeronightmare\/en\/view\/\d{1,20}$/.test(
      url,
    )
  )
    throw new Error("Enlace no permitido");
  if ("__TAURI_INTERNALS__" in window) return invoke("open_news_link", { url });
  window.open(url, "_blank", "noopener,noreferrer");
}

export async function openCreatorProfile() {
  if ("__TAURI_INTERNALS__" in window) return invoke("open_creator_profile");
  window.open("https://github.com/iRyzor", "_blank", "noopener,noreferrer");
}
