import { t, localizeMessage } from "./i18n";
import { useLocale } from "./useLocale";
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowUpRight,
  LoaderCircle,
  Newspaper,
  RefreshCw,
} from "lucide-react";
import {
  newsRequest,
  openNewsOriginal,
  type NewsItem,
  type NewsResponse,
} from "./api";
import { newsContent, allowedNewsImage } from "./newsContent";
import type { Game } from "./games";

export function NewsPanel({ game }: { game: Game }) {
  const locale = useLocale();
  const date = (ms: number) =>
    ms
      ? new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(ms)
      : "";
  const [feed, setFeed] = useState<NewsResponse | null>(null);
  const [selected, setSelected] = useState<NewsItem | null>(null);
  const [detail, setDetail] = useState<NewsResponse | null>(null);
  const [error, setError] = useState("");
  const [category, setCategory] = useState("Todas");
  const [retry, setRetry] = useState(0);
  const reader = useRef<HTMLElement>(null);
  const panel = useRef<HTMLElement>(null);
  const [visibleCount, setVisibleCount] = useState(6);
  useEffect(() => {
    setSelected(null);
    setFeed(null);
    setDetail(null);
    setCategory("Todas");
  }, [game.id]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let first = true;
    const load = async () => {
      try {
        const response = await newsRequest(
          game.id,
          selected?.id,
          first && retry > 0,
        );
        first = false;
        if (!active) return;
        if (selected) setDetail(response);
        else setFeed(response);
        setError("");
        if (response.loading) timer = setTimeout(load, 1000);
        else timer = setTimeout(load, 15 * 60 * 1000);
      } catch {
        if (active) {
          setError("No se pudo cargar el contenido. Puedes seguir jugando.");
          timer = setTimeout(load, 30000);
        }
      }
    };
    void load();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [game.id, selected?.id, retry]);
  useEffect(() => {
    if (selected) {
      reader.current?.focus();
      reader.current?.scrollIntoView({ block: "start", behavior: "instant" });
    }
  }, [selected?.id]);
  const article = detail?.news?.article;
  const html = useMemo(
    () => newsContent(article?.content || ""),
    [article?.content],
  );
  const items = feed?.news?.items || [];
  const filtered =
    category === "Todas"
      ? items
      : items.filter((item) => item.category === category);
  const newest = filtered.reduce<NewsItem | null>(
    (latest, item) =>
      item.published_at > (latest?.published_at || 0) ? item : latest,
    null,
  );
  const current = selected ? detail : feed;
  const message = localizeMessage(error || current?.error);
  const back = () => {
    setSelected(null);
    setDetail(null);
    setError("");
    const id = selected?.id;
    setTimeout(
      () =>
        panel.current
          ?.querySelector<HTMLButtonElement>(`[data-article-id="${id}"]`)
          ?.focus(),
      0,
    );
  };
  return (
    <section ref={panel} className="news-panel" aria-labelledby="news-title">
      <div className="section-heading news-heading">
        <div>
          <h3 id="news-title">{t("Noticias y anuncios")}</h3>
          <p>{t("Publicaciones oficiales de STOVE · Inglés")}</p>
        </div>
        <button
          className="outline"
          disabled={!!current?.loading}
          onClick={() => setRetry((v) => v + 1)}
          aria-label={t("Actualizar noticias")}
        >
          <RefreshCw size={16} className={current?.loading ? "spin" : ""} />
        </button>
      </div>
      {selected ? (
        <article
          className="news-reader"
          ref={reader}
          tabIndex={-1}
          aria-labelledby="article-title"
        >
          <button className="news-back" onClick={back}>
            <ArrowLeft size={16} /> {t("Volver a noticias")}{" "}
          </button>
          <span className="news-category">
            {localizeMessage(selected.category)}
          </span>
          <h2 id="article-title">{selected.title}</h2>
          <p className="news-meta">
            <time dateTime={new Date(selected.published_at).toISOString()}>
              {date(selected.published_at)}
            </time>{" "}
            · {t("Equipo de {game}", { game: game.title })}
          </p>
          {!article && current?.loading && (
            <p role="status">
              <LoaderCircle className="spin" size={18} />{" "}
              {t("Cargando publicación…")}{" "}
            </p>
          )}
          {message && (
            <p className="news-notice" role="status">
              {message}
            </p>
          )}
          {article && (
            <div
              className="news-body"
              dangerouslySetInnerHTML={{ __html: html }}
            />
          )}
          <div className="news-source">
            <p>
              {" "}
              {t(
                "Contenido de STOVE. Vídeos y enlaces interactivos están disponibles en la publicación original.",
              )}{" "}
            </p>
            <button
              className="outline"
              onClick={() =>
                void openNewsOriginal(selected.url).catch(() =>
                  setError("No se pudo abrir el original."),
                )
              }
            >
              {" "}
              {t("Ver original en STOVE")} <ArrowUpRight size={16} />
            </button>
          </div>
        </article>
      ) : (
        <>
          <div
            className="news-filters"
            role="group"
            aria-label={t("Categorías de noticias")}
          >
            {[
              "Todas",
              "Anuncios",
              "Actualizaciones",
              "Incidencias",
              "Notas del equipo",
            ].map((label) => (
              <button
                key={label}
                aria-pressed={category === label}
                onClick={() => {
                  setCategory(label);
                  setVisibleCount(6);
                }}
              >
                {localizeMessage(label)}
              </button>
            ))}
          </div>
          {(!feed || (!items.length && feed.loading)) && !error && (
            <p className="news-notice" role="status">
              <LoaderCircle className="spin" size={18} />{" "}
              {t("Cargando noticias…")}{" "}
            </p>
          )}
          {message && (
            <p className="news-notice" role="status">
              {message}
              {items.length > 0 && t(" Mostramos la última copia disponible.")}
            </p>
          )}
          {feed?.news?.partial && (
            <p className="news-notice">
              {" "}
              {t("Algunas categorías no pudieron actualizarse.")}{" "}
            </p>
          )}
          {feed?.cached && (
            <p className="news-notice">
              {t("Actualizando las noticias guardadas…")}
            </p>
          )}
          <div className="news-grid">
            {filtered.slice(0, visibleCount).map((item) => (
              <button
                className={
                  "news-card" + (item.id === newest?.id ? " latest" : "")
                }
                key={item.id}
                data-article-id={item.id}
                onClick={(event) => {
                  setDetail(null);
                  setError("");
                  setSelected(item);
                }}
              >
                {item.image && allowedNewsImage(item.image) ? (
                  <img
                    src={allowedNewsImage(item.image)!}
                    alt=""
                    loading="lazy"
                    referrerPolicy="no-referrer"
                  />
                ) : (
                  <div className="news-art-empty">
                    <Newspaper size={30} />
                  </div>
                )}
                <div className="news-card-copy">
                  <span className="news-category">
                    {localizeMessage(item.category)}
                  </span>
                  <h4>{item.title}</h4>
                  <p>{item.summary}</p>
                  <div className="news-date-row">
                    <time dateTime={new Date(item.published_at).toISOString()}>
                      {date(item.published_at)}
                    </time>
                    {item.id === newest?.id && (
                      <span className="news-latest-label">
                        {t("Más reciente")}
                      </span>
                    )}
                  </div>
                </div>
              </button>
            ))}
          </div>
          {filtered.length > visibleCount && (
            <button
              className="outline news-more"
              onClick={() => setVisibleCount((v) => v + 6)}
            >
              {" "}
              {t("Ver más noticias")}{" "}
            </button>
          )}
          {feed && !feed.loading && !filtered.length && !message && (
            <p className="news-notice">
              {" "}
              {t("No hay publicaciones en esta categoría.")}{" "}
            </p>
          )}
          {feed?.news?.fetched_at && (
            <p className="news-meta">
              {" "}
              {t("Última consulta:")}{" "}
              {new Intl.DateTimeFormat(locale, {
                dateStyle: "short",
                timeStyle: "short",
              }).format(feed.news.fetched_at)}
            </p>
          )}
        </>
      )}
    </section>
  );
}
