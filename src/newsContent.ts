import DOMPurify from "dompurify";
export function allowedNewsImage(value: string): string | null {
  try {
    const url = new URL(value.startsWith("//") ? `https:${value}` : value);
    return url.protocol === "https:" &&
      url.hostname === "d2x8kymwjom7h7.cloudfront.net" &&
      !url.username &&
      !url.password &&
      !url.port
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function newsContent(html: string): string {
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      "p",
      "div",
      "span",
      "h1",
      "h2",
      "h3",
      "h4",
      "h5",
      "h6",
      "strong",
      "b",
      "em",
      "i",
      "u",
      "s",
      "br",
      "hr",
      "ul",
      "ol",
      "li",
      "blockquote",
      "table",
      "thead",
      "tbody",
      "tr",
      "th",
      "td",
      "img",
      "a",
    ],
    ALLOWED_ATTR: ["src", "alt", "title", "colspan", "rowspan"],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
  const template = document.createElement("template");
  template.innerHTML = clean;
  for (const img of template.content.querySelectorAll("img")) {
    const url = allowedNewsImage(img.getAttribute("src") || "");
    if (!url) img.remove();
    else {
      img.src = url;
      img.setAttribute("loading", "lazy");
      img.setAttribute("referrerpolicy", "no-referrer");
      img.alt = img.alt || "Imagen de la publicación oficial";
    }
  }
  // Links remain readable text. The explicit original-publication button is the
  // only external navigation; remote scripts, forms and embedded sites never run.
  return template.innerHTML;
}
