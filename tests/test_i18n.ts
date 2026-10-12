import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { JSDOM } from "jsdom";
const dom = new JSDOM("<!doctype html><html lang='en'><body></body></html>", {
  url: "http://localhost",
});
Object.assign(globalThis, {
  document: dom.window.document,
  localStorage: dom.window.localStorage,
});
const i18n = await import("../src/i18n.ts");
const {
  catalogs,
  translate,
  localizeMessage,
  setLocale,
  getLocale,
  initialLocale,
  detectLocale,
} = i18n;
const placeholders = (text: string) =>
  [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();

test("both catalogs cover identical keys and preserve all interpolation parameters", () => {
  assert.deepEqual(
    Object.keys(catalogs.en).sort(),
    Object.keys(catalogs.es).sort(),
  );
  for (const key of Object.keys(catalogs.es) as (keyof typeof catalogs.es)[]) {
    assert.ok(catalogs.en[key].trim(), key);
    assert.equal(catalogs.es[key], key);
    assert.deepEqual(
      placeholders(catalogs.en[key]),
      placeholders(catalogs.es[key]),
      key,
    );
  }
});
test("system languages support regional variants, priority and English fallback", () => {
  assert.equal(detectLocale(["es-DO", "en-US"]), "es");
  assert.equal(detectLocale(["en-GB", "es"]), "en");
  assert.equal(detectLocale(["de", "es_MX"]), "es");
  assert.equal(detectLocale(["ja-JP"]), "en");
  assert.equal(detectLocale([]), "en");
});
test("saved preference overrides detection; switching updates document language and subscribers", () => {
  let updates = 0;
  const stop = i18n.subscribeLocale(() => updates++);
  setLocale("es");
  assert.equal(document.documentElement.lang, "es");
  assert.equal(initialLocale(), "es");
  assert.equal(translate("Biblioteca"), "Biblioteca");
  setLocale("en");
  assert.equal(getLocale(), "en");
  assert.equal(document.documentElement.lang, "en");
  assert.equal(initialLocale(), "en");
  assert.equal(translate("Biblioteca"), "Library");
  assert.ok(updates >= 1);
  stop();
  localStorage.setItem(i18n.languageStorageKey, "unsupported");
  assert.notEqual(initialLocale(), "unsupported");
});
test("language changes remain usable when preference storage is blocked", () => {
  const storage = globalThis.localStorage;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error("Storage blocked");
    },
  });
  try {
    setLocale("es");
    assert.equal(getLocale(), "es");
    assert.equal(document.documentElement.lang, "es");
    assert.ok(["en", "es"].includes(initialLocale()));
    setLocale("en");
  } finally {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: storage,
      writable: true,
    });
  }
});
test("interpolation is literal and keeps file paths and diagnostic codes intact", () => {
  const path = "C:\\Games\\{count}\\$&\\game.exe";
  assert.equal(
    translate(
      "Paquete {index} de {count} · {path}",
      { index: 1, count: 2, path },
      "en",
    ),
    `Package 1 of 2 · ${path}`,
  );
  assert.equal(
    localizeMessage("STOVE rechazó la autenticación (código 49500).", "en"),
    "STOVE rejected authentication (code 49500).",
  );
  assert.equal(
    localizeMessage("STOVE respondió HTTP 401.", "en"),
    "STOVE returned HTTP 401.",
  );
  assert.equal(
    localizeMessage("Descargando WebView2 de Microsoft: 4 / 8 MiB.", "en"),
    "Downloading WebView2 from Microsoft: 4 / 8 MiB.",
  );
  assert.equal(
    localizeMessage("Descargando WebView2 de Microsoft: 4 MiB.", "en"),
    "Downloading WebView2 from Microsoft: 4 MiB.",
  );
  assert.equal(
    localizeMessage("Hay otra operación en curso.", "en"),
    "Another operation is in progress.",
  );
  assert.equal(
    localizeMessage("Hay otra operación en curso.", "es"),
    "Hay otra operación en curso.",
  );
  assert.equal(
    localizeMessage("Unknown diagnostic 42", "en"),
    "Unknown diagnostic 42",
  );
  assert.equal(localizeMessage(null), "");
});
test("all current engine display messages have translations without touching protocol identifiers", () => {
  const excluded = new Set([
    "Chaos Zero Nightmare",
    "DELETE FROM installed_info",
    "DPMS distribution files only",
  ]);
  const folder = new URL("../engine/rust/src/", import.meta.url);
  for (const file of readdirSync(folder).filter(
    (file) => file.endsWith(".rs") && file !== "tests.rs",
  )) {
    const text = readFileSync(new URL(file, folder), "utf8");
    for (const match of text.matchAll(/"((?:[^"\\]|\\.)*)"/g)) {
      const message = match[1];
      if (
        !/[ áéíóúñ¿]/.test(message) ||
        !/^[A-ZÁÉÍÓÚ¿]/.test(message) ||
        /[{}\\]/.test(message) ||
        /^(SELECT |CREATE |INSERT |PRAGMA )/.test(message) ||
        /^[A-Z]{2}( [A-Z]{2})+$/.test(message) ||
        excluded.has(message)
      )
        continue;
      assert.ok(Object.hasOwn(catalogs.en, message), `${file}: ${message}`);
    }
  }
});
