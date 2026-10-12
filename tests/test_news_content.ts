import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
const dom = new JSDOM("<!doctype html><html><body></body></html>");
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
});
const { newsContent, allowedNewsImage } = await import("../src/newsContent.ts");
test("remote editorial HTML cannot execute scripts or change launcher navigation", () => {
  const result = newsContent(
    '<script>alert(1)</script><iframe src="https://evil.example"></iframe><form><input></form><p style="position:fixed" onclick="alert(1)">Text <a href="javascript:alert(1)">Link</a></p><img src="https://evil.example/x" onerror="alert(1)">',
  );
  const template = document.createElement("template");
  template.innerHTML = result;
  assert.equal(
    template.content.querySelectorAll(
      "script,iframe,form,input,[onclick],[onerror],[style],[href],img",
    ).length,
    0,
  );
  assert.ok(template.content.textContent?.includes("Text Link"));
});
test("official images and document structure remain readable without remote styling", () => {
  const result = newsContent(
    '<h2>Update</h2><p><strong>Changes</strong></p><table><tr><td colspan="2">A</td></tr></table><img src="//d2x8kymwjom7h7.cloudfront.net/a.png" style="width:9000px">',
  );
  const template = document.createElement("template");
  template.innerHTML = result;
  assert.equal(
    template.content.querySelector("img")?.src,
    "https://d2x8kymwjom7h7.cloudfront.net/a.png",
  );
  assert.equal(
    template.content.querySelector("img")?.getAttribute("referrerpolicy"),
    "no-referrer",
  );
  assert.ok(template.content.querySelector("h2,strong,table"));
  assert.equal(template.content.querySelectorAll("[style]").length, 0);
});
test("media allowlist rejects credentials and misleading domains", () => {
  assert.ok(allowedNewsImage("//d2x8kymwjom7h7.cloudfront.net/a.png"));
  for (const url of [
    "data:image/svg+xml,x",
    "javascript:alert(1)",
    "https://d2x8kymwjom7h7.cloudfront.net.evil.example/a",
    "https://user@d2x8kymwjom7h7.cloudfront.net/a",
  ])
    assert.equal(allowedNewsImage(url), null);
});
