import { test } from "node:test";
import assert from "node:assert/strict";
import {
  mkdtempSync,
  readFileSync,
  writeFileSync,
  mkdirSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";
import { JSDOM } from "jsdom";
import React, { act } from "react";
import { createRoot } from "react-dom/client";

// Compile the real components with the project's TypeScript compiler; replace only IPC.
// No launcher process, account, game, network, or installation is touched.
test("live language switching preserves game controls, errors and news filter identities", async () => {
  const folder = mkdtempSync(join(tmpdir(), "ember-i18n-ui-"));
  const dom = new JSDOM(
    "<!doctype html><html><body><div id='root'></div></body></html>",
    { url: "http://localhost" },
  );
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  localStorage.setItem("ember.language", "en");
  const root = createRoot(document.getElementById("root")!);
  try {
    mkdirSync(join(folder, "locales"));
    for (const language of ["en", "es"])
      writeFileSync(
        join(folder, "locales", language + ".json"),
        readFileSync(
          new URL(`../src/locales/${language}.json`, import.meta.url),
        ),
      );
    for (const file of [
      "main.tsx",
      "NewsPanel.tsx",
      "newsContent.ts",
      "games.ts",
      "i18n.ts",
      "useLocale.ts",
    ]) {
      let source = readFileSync(
        new URL("../src/" + file, import.meta.url),
        "utf8",
      );
      if (file === "main.tsx")
        source = source
          .replace('import "./style.css";', "")
          .replace("function App()", "export function App()")
          .replace(
            /createRoot\(document.getElementById\("root"\)!\)\.render\(<App \/>\);/,
            "",
          );
      const output = ts
        .transpileModule(source, {
          compilerOptions: {
            target: ts.ScriptTarget.ES2022,
            module: ts.ModuleKind.ESNext,
            jsx: ts.JsxEmit.React,
          },
        })
        .outputText.replace(/from "([^"\n]+)"/g, (_, specifier: string) => {
          if (specifier.startsWith("./"))
            return `from "${specifier.endsWith(".json") ? specifier : specifier + ".js"}"`;
          return `from "${import.meta.resolve(specifier)}"`;
        });
      writeFileSync(join(folder, file.replace(/\.tsx?$/, ".js")), output);
    }
    writeFileSync(
      join(folder, "api.js"),
      `
      export const actions=[];
      export const payloads=[];
      let snapshot={phase:'ready',detail:'Cuenta conectada. Todo listo para jugar.',connected:true,version:55,prepared:false,root:'/library/Smilegate',runner:'wine',game_installed:true,game_running:true,error:'Hay otra operación en curso.'};
      export function setSnapshot(value) {snapshot=value;}
      export async function request(action,payload) {
        actions.push(action);payloads.push({action,payload});
        if(action==='account_settings')snapshot={...snapshot,...payload};
        if(action==='login')snapshot={...snapshot,phase:'qr_pending',image:'data:image/png;base64,iVBORw0KGgo='};
        return snapshot;
      }
      export async function newsRequest() {actions.push('news');return {loading:false,cached:false,news:{items:[{id:1,title:'Original English announcement',summary:'Original English content',category:'Anuncios',published_at:1700000000000,image:null,url:'https://page.onstove.com/chaoszeronightmare/en/view/1'},{id:2,title:'Original English update',summary:'Content',category:'Actualizaciones',published_at:1700000001000,image:null,url:'https://page.onstove.com/chaoszeronightmare/en/view/2'}],fetched_at:1700000002000}};}
      export async function openCreatorProfile() {throw Error('External navigation is disabled in tests');}
      export async function openNewsOriginal() {throw Error('External navigation is disabled in tests');}
    `,
    );
    const { App } = await import(pathToFileURL(join(folder, "main.js")).href);
    const { actions } = await import(
      pathToFileURL(join(folder, "api.js")).href
    );
    await act(async () => {
      root.render(React.createElement(App));
    });
    const click = async (selector: string) => {
      const button = document.querySelector<HTMLButtonElement>(selector);
      assert.ok(button, selector);
      await act(async () => button.click());
    };
    const switchLanguage = async (language: string) => {
      await click(".sidebar-bottom .nav-item");
      const select =
        document.querySelector<HTMLSelectElement>("#launcher-language");
      assert.ok(select);
      await act(async () => {
        select.value = language;
        select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
      });
      assert.equal(document.documentElement.lang, language);
      assert.equal(localStorage.getItem("ember.language"), language);
      await click(".game-nav");
    };
    await click(".game-nav");
    assert.match(document.querySelector(".play")!.textContent!, /Close game/);
    assert.match(
      document.querySelector(".toast")!.textContent!,
      /Another operation is in progress/,
    );
    assert.ok(
      document
        .querySelector(".news-filters")!
        .textContent!.includes("Announcements"),
    );
    await switchLanguage("es");
    assert.match(document.querySelector(".play")!.textContent!, /Cerrar juego/);
    assert.match(
      document.querySelector(".toast")!.textContent!,
      /Hay otra operación en curso/,
    );
    const announcements = [
      ...document.querySelectorAll<HTMLButtonElement>(".news-filters button"),
    ].find((button) => button.textContent?.trim() === "Anuncios");
    assert.ok(announcements);
    await act(async () => announcements.click());
    assert.equal(document.querySelectorAll(".news-card").length, 1);
    assert.equal(
      document.querySelector(".news-card h4")!.textContent,
      "Original English announcement",
    );
    // Change directly through the same store while the filter remains mounted.
    const { setLocale } = await import(
      pathToFileURL(join(folder, "i18n.js")).href
    );
    await act(async () => setLocale("en"));
    assert.match(document.querySelector(".play")!.textContent!, /Close game/);
    assert.equal(document.querySelectorAll(".news-card").length, 1);
    assert.equal(
      document
        .querySelector(".news-filters [aria-pressed=true]")!
        .textContent!.trim(),
      "Announcements",
    );
    assert.ok(
      actions.every((action: string) =>
        ["state", "poll", "news"].includes(action),
      ),
      actions.join(","),
    );
    // Remount the real app with a remembered account and verify one startup attempt.
    const { setSnapshot, payloads } = await import(
      pathToFileURL(join(folder, "api.js")).href
    );
    setSnapshot({
      phase: "signed_out",
      detail: "Conecta tu cuenta para jugar.",
      connected: false,
      version: 55,
      prepared: false,
      root: "/library/Smilegate",
      runner: "wine",
      game_installed: true,
      game_running: false,
      remember_session: true,
      country: "DO",
    });
    await act(async () =>
      root.render(React.createElement(App, { key: "remembered" })),
    );
    assert.equal(
      actions.filter((a: string) => a === "restore_session").length,
      1,
    );
    await click(".sidebar-bottom .nav-item");
    const checkbox = document.querySelector<HTMLInputElement>(
      ".remember-session input",
    )!;
    assert.equal(checkbox.checked, true);
    await act(async () => checkbox.click());
    assert.equal(
      document.querySelector<HTMLInputElement>(".remember-session input")!
        .checked,
      false,
    );
    assert.ok(
      payloads.some(
        (p: { action: string; payload?: { remember_session?: boolean } }) =>
          p.action === "account_settings" &&
          p.payload?.remember_session === false,
      ),
    );
    assert.match(
      document.querySelector(".session-settings")!.textContent!,
      /discarded when you close/,
    );
    await click(".profile");
    assert.ok(document.querySelector("#login-title"));
    assert.equal(
      document.querySelector<HTMLInputElement>(".remember-session input")!
        .checked,
      false,
    );
    assert.equal(
      actions.filter((a: string) => a === "restore_session").length,
      1,
    );
    assert.equal(actions.filter((a: string) => a === "login").length, 1);
    // Region detection belongs to the engine and never introduces a country selector.
    setSnapshot({
      phase: "signed_out",
      detail: "Conecta tu cuenta para jugar.",
      connected: false,
      version: 55,
      prepared: false,
      root: "/library/Smilegate",
      runner: "wine",
      game_installed: true,
      game_running: false,
      remember_session: false,
      country: "",
    });
    await act(async () =>
      root.render(React.createElement(App, { key: "no-country" })),
    );
    await click(".profile");
    assert.equal(document.querySelector("#account-country"), null);
    assert.ok(document.querySelector("#login-title"));
    assert.equal(actions.filter((a: string) => a === "login").length, 2);
    assert.equal(
      actions.filter((a: string) => a === "restore_session").length,
      1,
    );
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    rmSync(folder, { recursive: true, force: true });
  }
});
