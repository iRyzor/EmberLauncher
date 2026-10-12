import en from "./locales/en.json" with { type: "json" };
import es from "./locales/es.json" with { type: "json" };

export type Locale = "en" | "es";
export type MessageKey = keyof typeof en;
export type Parameters = Record<string, string | number>;
export const catalogs = { en, es };
export const languageStorageKey = "ember.language";

export function detectLocale(languages: readonly string[]): Locale {
  for (const language of languages) {
    const base = language.toLowerCase().split(/[-_]/)[0];
    if (base === "en" || base === "es") return base;
  }
  return "en";
}
export function initialLocale(): Locale {
  try {
    const saved = localStorage.getItem(languageStorageKey);
    if (saved === "en" || saved === "es") return saved;
  } catch {
    /* Language still works when storage is unavailable. */
  }
  return detectLocale(
    typeof navigator === "undefined"
      ? []
      : navigator.languages?.length
        ? navigator.languages
        : [navigator.language],
  );
}
let locale = initialLocale();
const listeners = new Set<() => void>();
export const getLocale = () => locale;
export function subscribeLocale(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
export function setLocale(next: Locale) {
  if (next !== "en" && next !== "es") return;
  try {
    localStorage.setItem(languageStorageKey, next);
  } catch {
    /* Memory preference remains usable. */
  }
  if (locale === next) return;
  locale = next;
  if (typeof document !== "undefined") document.documentElement.lang = next;
  listeners.forEach((listener) => listener());
}
if (typeof document !== "undefined") document.documentElement.lang = locale;

export function translate(
  key: MessageKey,
  params: Parameters = {},
  language: Locale = locale,
): string {
  const template = catalogs[language][key];
  // Values are never interpreted as HTML or recursively interpolated.
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) =>
    Object.hasOwn(params, name) ? String(params[name]) : placeholder,
  );
}
export const t = translate;

// Presentation adapter for the engine's existing Spanish message contract.
// Only display messages pass through here, never paths, RPC actions, tokens or article content.
const dynamicMessages: [RegExp, MessageKey, string[]][] = [
  [
    /^STOVE respondió HTTP (\d+)\.$/,
    "STOVE respondió HTTP {status}.",
    ["status"],
  ],
  [
    /^STOVE rechazó la autenticación \(código (\d+)\)\.$/,
    "STOVE rechazó la autenticación (código {code}).",
    ["code"],
  ],
  [
    /^Descargando WebView2 de Microsoft: ([\d.]+) \/ ([\d.]+) MiB\.$/,
    "Descargando WebView2 de Microsoft: {downloaded} / {total} MiB.",
    ["downloaded", "total"],
  ],
  [
    /^Descargando WebView2 de Microsoft: ([\d.]+) MiB\.$/,
    "Descargando WebView2 de Microsoft: {downloaded} MiB.",
    ["downloaded"],
  ],
];
export function localizeMessage(
  message: string | null | undefined,
  language: Locale = locale,
): string {
  if (!message) return "";
  if (Object.hasOwn(en, message))
    return translate(message as MessageKey, {}, language);
  for (const [pattern, key, names] of dynamicMessages) {
    const match = message.match(pattern);
    if (match)
      return translate(
        key,
        Object.fromEntries(
          names.map((name, index) => [name, match[index + 1]]),
        ),
        language,
      );
  }
  // Preserve unknown diagnostics verbatim rather than hiding their meaning.
  return message;
}
export function formatNumber(value: number, digits = 0): string {
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}
