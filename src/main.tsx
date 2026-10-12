import packageMetadata from "../package.json" with { type: "json" };
const launcherVersion = packageMetadata.version;
import { t, localizeMessage, setLocale, formatNumber } from "./i18n";
import { useLocale } from "./useLocale";
import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import {
  Flame,
  LayoutGrid,
  Download,
  Settings,
  ChevronRight,
  ArrowUpRight,
  Play,
  Square,
  ShieldCheck,
  Folder,
  RefreshCw,
  X,
  Check,
  LogOut,
  Monitor,
  Terminal,
  ScanLine,
  LoaderCircle,
  ArrowLeft,
  HardDrive,
  CheckCircle2,
  AlertCircle,
  Trash2,
} from "lucide-react";
import { request, openCreatorProfile, type Snapshot } from "./api";
import "./style.css";
import { games, type Game } from "./games";
import { NewsPanel } from "./NewsPanel";
const initial: Snapshot = {
  phase: "loading",
  detail: "Conectando con el launcher…",
  connected: false,
  version: null,
  prepared: false,
  root: "",
  runner: "wine",
  game_installed: false,
};
function bytes(value: number) {
  if (value >= 1024 ** 3) return `${formatNumber(value / 1024 ** 3, 2)} GB`;
  if (value >= 1024 ** 2) return `${formatNumber(value / 1024 ** 2, 1)} MB`;
  return `${formatNumber(value / 1024, 1)} KB`;
}
function App() {
  const locale = useLocale();
  const [state, setState] = useState(initial),
    [page, setPage] = useState("library"),
    [selectedGame, setSelectedGame] = useState<Game | null>(null),
    [modal, setModal] = useState(false),
    [setup, setSetup] = useState(false),
    [advanced, setAdvanced] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [toast, setToast] = useState(""),
    [root, setRoot] = useState(""),
    [runner, setRunner] = useState("wine"),
    [newRoot, setNewRoot] = useState(""),
    [newPrefix, setNewPrefix] = useState(""),
    [uninstallTarget, setUninstallTarget] = useState<string | null>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const setupRequested = useRef(false);
  const restoreChecked = useRef(false);
  const inflight = useRef(false);
  const polling = useRef(false);
  const revision = useRef(0);
  const observe = async (action: "state" | "poll") => {
    if (polling.current || inflight.current) return;
    polling.current = true;
    const started = revision.current;
    try {
      const snapshot = await request(action);
      if (!inflight.current && revision.current === started) {
        setState(snapshot);
        if (snapshot.error) setError(snapshot.error);
      }
    } catch (e) {
      if (!inflight.current && revision.current === started) {
        setError(
          (current) =>
            current ||
            (e instanceof Error
              ? e.message
              : "No se pudo contactar al launcher."),
        );
      }
    } finally {
      polling.current = false;
    }
  };
  const call = async (action: string, payload?: Record<string, unknown>) => {
    if (inflight.current) return;
    inflight.current = true;
    revision.current += 1;
    setBusy(true);
    setError("");
    try {
      const s = await request(action, payload);
      setState(s);
      if (s.error) {
        setError(s.error);
        if (action === "bootstrap") setupRequested.current = false;
      }
      if (["settings", "new_installation"].includes(action) && !s.error)
        setToast("Ajustes guardados");
      if (action === "new_installation" && !s.error) setPage("downloads");
    } catch (e) {
      if (action === "bootstrap") setupRequested.current = false;
      setError(
        e instanceof Error ? e.message : "No se pudo completar la operación.",
      );
    } finally {
      inflight.current = false;
      setBusy(false);
    }
  };
  useEffect(() => {
    void observe("state");
    const id = setInterval(() => {
      void observe("poll");
    }, 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    if (state.phase === "loading" || restoreChecked.current) return;
    restoreChecked.current = true;
    if (state.remember_session && !state.connected)
      void call("restore_session");
  }, [state.phase]);
  useEffect(() => {
    if (state.root) {
      setRoot(state.root);
      setRunner(state.runner);
    }
  }, [state.root, state.runner]);
  useEffect(() => {
    if (state.suggested_prefix)
      setNewPrefix((current) => current || state.suggested_prefix!);
  }, [state.suggested_prefix]);
  useEffect(() => {
    if (state.connected) setModal(false);
  }, [state.connected]);
  useEffect(() => {
    if (toast) {
      const id = setTimeout(() => setToast(""), 3500);
      return () => clearTimeout(id);
    }
  }, [toast]);
  useEffect(() => {
    if (!modal && !setup) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, [tabindex="0"]',
        ) || [],
      ).filter((element) => element.getClientRects().length > 0);
    focusable()[0]?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setModal(false);
        setSetup(false);
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      const first = elements[0],
        last = elements[elements.length - 1];
      if (!first) {
        event.preventDefault();
        return;
      }
      if (
        event.shiftKey &&
        (document.activeElement === first ||
          !dialog?.contains(document.activeElement))
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last ||
          !dialog?.contains(document.activeElement))
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("keydown", handleKey);
      previous?.focus();
    };
  }, [modal, setup]);
  useEffect(() => {
    if (
      !setupRequested.current ||
      state.job?.kind !== "bootstrap" ||
      state.job.running
    )
      return;
    setupRequested.current = false;
    if (!state.job.error && state.fresh_destination) {
      setSetup(false);
      setPage("downloads");
      setToast("Ubicación preparada. Inicia sesión para descargar el juego.");
    }
  }, [state.job, state.fresh_destination]);
  const openCreator = (event: React.MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    void openCreatorProfile().catch(() =>
      setError("No se pudo abrir el enlace en el navegador."),
    );
  };
  const login = () => {
    setModal(true);
    void call("login");
  };
  const running = !!state.game_running || !!state.game_starting;
  const operation = busy || !!state.job?.running;
  const restoring =
    !!state.job?.running && state.job.kind === "restore_account";
  const stopping =
    !!state.job?.running &&
    ["stop_game", "force_stop_game"].includes(state.job.kind);
  const version = state.version === null ? "—" : String(state.version);
  const status = restoring
    ? t("Recuperando sesión…")
    : stopping
      ? t("Cerrando juego")
      : running
        ? t("Juego en ejecución")
        : state.connected
          ? t("Cuenta conectada")
          : t("Sin conexión de cuenta");
  return (
    <div className="app">
      <aside className="sidebar" inert={modal || setup}>
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setPage("library");
            setSelectedGame(null);
          }}
        >
          <span className="brand-icon">
            <Flame size={25} />
          </span>
          <span>
            ember<span className="brand-dot">.</span>
          </span>
        </a>
        <div className="nav-label">{t("TU ESPACIO")}</div>
        <nav aria-label={t("Navegación principal")}>
          {[
            { id: "library", label: t("Biblioteca"), icon: LayoutGrid },
            { id: "downloads", label: t("Actualizaciones"), icon: Download },
          ].map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              aria-current={page === id ? "page" : undefined}
              className={"nav-item " + (page === id ? "active" : "")}
              onClick={() => {
                setPage(id);
                if (id === "library") setSelectedGame(null);
              }}
            >
              <Icon size={18} />
              {label}
              {id === "library" && <span className="count">1</span>}
              {id === "downloads" && state.updates?.update_available && (
                <i className="dot" />
              )}
            </button>
          ))}
        </nav>
        <div className="side-divider" />
        <div className="nav-label">
          {" "}
          {t("JUEGOS")} <span>01</span>
        </div>
        <button
          className={
            "game-nav" +
            (page === "library" && selectedGame?.id === games[0].id
              ? " active"
              : "")
          }
          aria-current={
            page === "library" && selectedGame?.id === games[0].id
              ? "true"
              : undefined
          }
          onClick={() => {
            setSelectedGame(games[0]);
            setPage("library");
          }}
        >
          <div className="mini-art" />
          <span>{games[0].title}</span>
        </button>
        <div className="sidebar-bottom">
          <div className="launcher-note">
            <Flame size={12} aria-hidden="true" /> {t("Launcher independiente")}
            <span>β</span>
          </div>
          <button
            aria-current={page === "settings" ? "page" : undefined}
            className={"nav-item " + (page === "settings" ? "active" : "")}
            onClick={() => setPage("settings")}
          >
            <Settings size={18} /> {t("Ajustes")}{" "}
          </button>
          <button
            className="profile"
            disabled={!state.connected && (operation || running)}
            onClick={state.connected ? () => setPage("settings") : login}
          >
            <div className={"avatar " + (state.connected ? "connected" : "")}>
              <ScanLine size={19} />
            </div>
            <div>
              <strong>
                {state.connected ? t("Cuenta STOVE") : t("Conecta tu cuenta")}
              </strong>
              <small>
                {state.connected
                  ? t("Gestionar cuenta")
                  : t("Accede para empezar a jugar")}
              </small>
            </div>
            {state.connected ? (
              <ChevronRight size={16} />
            ) : (
              <ChevronRight size={16} />
            )}
          </button>
        </div>
      </aside>
      <main inert={modal || setup}>
        <header>
          <div className="breadcrumb">
            <span>{t("Tu espacio")}</span>
            <ChevronRight size={13} />
            <strong>
              {page === "library"
                ? t("Biblioteca")
                : page === "downloads"
                  ? t("Actualizaciones")
                  : t("Ajustes")}
            </strong>
          </div>
          <div className="header-right">
            <span className="platform">
              <Monitor size={14} />
              {state.native_game
                ? "WINDOWS"
                : state.platform === "darwin"
                  ? "MACOS"
                  : "LINUX"}
            </span>
            <span
              className={"account-status " + (state.connected ? "online" : "")}
            >
              <i />
              {restoring
                ? t("Recuperando sesión…")
                : state.connected
                  ? t("Conectado")
                  : t("Cuenta sin conectar")}
            </span>
          </div>
        </header>
        {state.account_warning && (
          <div className="session-notice" role="status">
            <p>{localizeMessage(state.account_warning)}</p>
          </div>
        )}
        {state.job?.kind === "restore_account" && state.job.error && (
          <div className="session-notice" role="status">
            <p>{localizeMessage(state.job.error)}</p>
          </div>
        )}
        {state.reauth_required && (
          <div className="session-notice" role="status">
            <ScanLine size={20} />
            <div>
              <strong>{t("Conecta de nuevo tu cuenta")}</strong>
              <p>
                {running
                  ? t(
                      "Puedes cerrar la partida desde Ember. Después, vuelve a acceder mediante QR.",
                    )
                  : t("La sesión necesita un nuevo acceso mediante QR.")}
              </p>
            </div>
            <button
              className="outline"
              disabled={operation || running}
              onClick={login}
            >
              {" "}
              {t("Conectar con QR")}{" "}
            </button>
          </div>
        )}
        {page === "library" ? (
          <div className="content library">
            <div className="page-heading">
              {selectedGame ? (
                <button
                  className="library-back"
                  onClick={() => setSelectedGame(null)}
                >
                  <ArrowLeft size={16} /> {t("Volver a Biblioteca")}
                </button>
              ) : (
                <h1>{t("Biblioteca")}</h1>
              )}
              <span className="library-filter">{t("1 juego disponible")}</span>
            </div>
            {selectedGame ? (
              <>
                <section className="hero">
                  <div className="hero-art" />
                  <div className="hero-shade" />
                  <div className="hero-top">
                    <span className="genre">
                      {" "}
                      {t("RPG ESTRATÉGICO")} <span>·</span> DECKBUILDING
                    </span>
                    <span className="installed">
                      <Check size={12} />{" "}
                      {state.game_installed
                        ? t("INSTALADO")
                        : t("POR INSTALAR")}
                    </span>
                  </div>
                  <div className="hero-copy">
                    <div className="game-kicker">
                      SMILEGATE · SUPER CREATIVE
                    </div>
                    <h2>
                      CHAOS ZERO
                      <br />
                      NIGHTMARE
                    </h2>
                    <p>{t("¡Acaba con toda desesperación!")}</p>
                    <div className="hero-actions">
                      <button
                        className="play primary"
                        disabled={operation || state.phase === "loading"}
                        onClick={
                          running
                            ? () => void call("stop_game")
                            : state.recovery_needed ||
                                state.prepared ||
                                state.updates?.update_available
                              ? () => setPage("downloads")
                              : !state.game_installed
                                ? state.fresh_destination
                                  ? () => setPage("downloads")
                                  : () => setSetup(true)
                                : state.connected
                                  ? () => void call("launch")
                                  : login
                        }
                      >
                        {operation ? (
                          <LoaderCircle className="spin" size={19} />
                        ) : running ? (
                          <Square size={18} fill="currentColor" />
                        ) : (
                          <Play size={18} fill="currentColor" />
                        )}
                        {stopping
                          ? t("Cerrando…")
                          : state.game_starting
                            ? t("Iniciando…")
                            : running
                              ? t("Cerrar juego")
                              : operation
                                ? t("Preparando…")
                                : state.phase === "loading"
                                  ? t("Cargando…")
                                  : state.recovery_needed
                                    ? t("Recuperar instalación")
                                    : state.prepared
                                      ? t("Revisar instalación")
                                      : state.updates?.update_available
                                        ? t("Actualizar juego")
                                        : !state.game_installed
                                          ? t("Instalar juego")
                                          : state.connected
                                            ? t("Jugar ahora")
                                            : t("Iniciar sesión")}
                        <ArrowUpRight size={18} />
                      </button>
                      {state.job?.result?.force_required === true &&
                        running && (
                          <button
                            className="outline"
                            disabled={operation}
                            onClick={() => void call("force_stop_game")}
                          >
                            {" "}
                            {t("Forzar cierre")}{" "}
                          </button>
                        )}
                      <button
                        className="hero-secondary"
                        onClick={() => setPage("downloads")}
                        title={t("Ver actualizaciones")}
                        aria-label={t("Ver actualizaciones")}
                      >
                        <Download size={20} />
                      </button>
                    </div>
                    <div className="hero-foot">
                      <span className="live-dot" />
                      {running
                        ? state.native_game
                          ? t("Juego en ejecución")
                          : t("Juego en ejecución con Wine")
                        : state.game_installed
                          ? t("Disponible en tu biblioteca")
                          : t("Instala el juego para empezar")}
                      <span className="hero-foot-divider" /> {t("Versión")}{" "}
                      {version}
                    </div>
                  </div>
                  <div className="hero-number">
                    01 <span>/ 01</span>
                  </div>
                </section>
                <div className="under-hero">
                  <div>
                    <ShieldCheck size={18} />
                    <span>{t("Tu sesión, en tu equipo.")}</span>
                    <small>
                      {" "}
                      {state.remember_session
                        ? t(
                            "Recordar sesión utiliza el almacén seguro del sistema.",
                          )
                        : t(
                            "Las credenciales se conservan solo en memoria.",
                          )}{" "}
                    </small>
                  </div>
                  <button onClick={() => setPage("settings")}>
                    {" "}
                    {t("Ver instalación")} <ArrowUpRight size={15} />
                  </button>
                </div>
                <NewsPanel game={selectedGame} />
                <div className="section-heading">
                  <h3>
                    {running
                      ? t("Tu partida")
                      : state.game_installed
                        ? t("Tu juego y tu cuenta")
                        : t("Empieza tu primera partida")}
                  </h3>
                  <span>{t("ESTADO DEL LAUNCHER")}</span>
                </div>
                <div className="status-grid">
                  <article>
                    <div className="tile-icon">
                      <Monitor size={19} />
                    </div>
                    <span>{t("Entorno de juego")}</span>
                    <strong>
                      {state.native_game
                        ? "Windows"
                        : state.platform === "darwin"
                          ? "macOS + Wine"
                          : "Linux + Wine"}
                    </strong>
                    <small>
                      {state.native_game
                        ? t("El juego se ejecuta en Windows")
                        : t("Wine es necesario para jugar")}
                    </small>
                  </article>
                  <article>
                    <div className="tile-icon">
                      <ShieldCheck size={19} />
                    </div>
                    <span>{t("Cuenta STOVE")}</span>
                    <strong>
                      {state.connected
                        ? t("Sesión conectada")
                        : t("Conexión pendiente")}
                    </strong>
                    <button
                      onClick={
                        state.connected ? () => setPage("settings") : login
                      }
                    >
                      {state.connected
                        ? t("Gestionar cuenta")
                        : t("Iniciar sesión con QR")}
                      <ArrowUpRight size={13} />
                    </button>
                  </article>
                  <article>
                    <div className="tile-icon">
                      <RefreshCw size={19} />
                    </div>
                    <span>{t("Versión instalada")}</span>
                    <strong>
                      {state.game_installed
                        ? t("Versión {version}", { version })
                        : t("Sin instalar")}
                    </strong>
                    <button
                      disabled={operation}
                      onClick={() => {
                        setPage("downloads");
                        if (state.connected) void call("check_updates");
                      }}
                    >
                      {" "}
                      {t("Comprobar actualizaciones")}{" "}
                      <ArrowUpRight size={13} />
                    </button>
                  </article>
                </div>
              </>
            ) : (
              <section
                className="library-games"
                aria-label={t("Juegos de tu biblioteca")}
              >
                {games.map((game) => (
                  <button
                    className="library-game-card"
                    key={game.id}
                    onClick={() => setSelectedGame(game)}
                  >
                    <img src={game.artwork} alt="" />
                    <div>
                      <span>
                        {state.game_installed
                          ? t("Instalado")
                          : t("Disponible para instalar")}
                      </span>
                      <h2>{game.title}</h2>
                      <p>{t("Jugar, gestionar instalación y ver noticias")}</p>
                      <ChevronRight size={20} />
                    </div>
                  </button>
                ))}
              </section>
            )}
          </div>
        ) : page === "downloads" ? (
          <div className="content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">
                  {t("MANTÉN TU BIBLIOTECA AL DÍA")}
                </div>
                <h1>{t("Actualizaciones")}</h1>
              </div>
              <button
                className="outline"
                disabled={operation}
                onClick={
                  state.connected ? () => void call("check_updates") : login
                }
              >
                <RefreshCw size={16} className={operation ? "spin" : ""} />{" "}
                {t("Comprobar versión")}{" "}
              </button>
            </div>
            <section className="update-card">
              <div className="update-art" />
              <div className="update-info">
                <span className="eyebrow">CHAOS ZERO NIGHTMARE</span>
                <h2>
                  {state.updates
                    ? state.fresh_destination
                      ? t("Todo listo para preparar la instalación")
                      : state.updates.update_available
                        ? t("Hay una actualización disponible")
                        : t("Estás al día")
                    : state.fresh_destination
                      ? t("Instala Chaos Zero Nightmare")
                      : t("Comprueba la versión del juego")}
                </h2>
                <p>
                  {state.updates
                    ? t("Instalada {installed} · Publicada {published}", {
                        installed: version,
                        published: state.updates.published_version,
                      })
                    : t(
                        "Conecta tu cuenta para consultar la versión publicada en STOVE.",
                      )}
                </p>
                <div className="update-actions">
                  <button
                    className="primary"
                    disabled={operation}
                    onClick={
                      state.connected ? () => void call("prepare") : login
                    }
                  >
                    <Download size={16} />
                    {!state.connected
                      ? t("Iniciar sesión para descargar")
                      : state.resumable_download
                        ? t("Reanudar descarga")
                        : state.fresh_destination
                          ? t("Descargar instalación")
                          : t("Preparar archivos")}
                  </button>
                  <button
                    className="outline"
                    disabled={operation || !state.game_installed}
                    onClick={() => void call("verify")}
                  >
                    <ShieldCheck size={16} /> {t("Verificar instalación")}{" "}
                  </button>
                </div>
              </div>
            </section>
            {state.job?.kind === "prepare" && state.job.running && (
              <section className="download-panel" aria-live="polite">
                <div className="download-heading">
                  <h3>
                    {state.job.cancelling
                      ? t("Cancelando descarga…")
                      : state.job.progress?.phase === "download"
                        ? t("Descargando y verificando")
                        : t("Preparando descarga…")}
                  </h3>
                  <button
                    className="outline"
                    disabled={busy || state.job.cancelling}
                    onClick={() => void call("cancel_download")}
                  >
                    {" "}
                    {t("Cancelar")}{" "}
                  </button>
                </div>
                {state.job.progress && (
                  <>
                    <progress
                      aria-label={t("Progreso de descarga")}
                      value={state.job.progress.downloaded_bytes}
                      max={Math.max(1, state.job.progress.total_bytes)}
                    />
                    <div className="download-stats">
                      <span>
                        {bytes(state.job.progress.downloaded_bytes)} /{" "}
                        {bytes(state.job.progress.total_bytes)}
                      </span>
                      <span>
                        {bytes(state.job.progress.bytes_per_second)}/s
                      </span>
                      <span>
                        {state.job.progress.eta_seconds != null
                          ? t("≈ {minutes} min restantes", {
                              minutes: formatNumber(
                                Math.ceil(state.job.progress.eta_seconds / 60),
                              ),
                            })
                          : t("Calculando tiempo…")}
                      </span>
                    </div>
                    {state.job.progress.path && (
                      <p className="download-path">
                        {t("Paquete {index} de {count} · {path}", {
                          index: state.job.progress.package_index || 0,
                          count: state.job.progress.package_count || 0,
                          path: state.job.progress.path,
                        })}
                      </p>
                    )}
                  </>
                )}
                <small>
                  {" "}
                  {t(
                    "Los archivos descargados se conservan para reanudar.",
                  )}{" "}
                </small>
              </section>
            )}
            {state.resumable_download &&
              !state.job?.running &&
              !state.prepared && (
                <div className="info-box">
                  <Download size={20} />
                  <div>
                    <strong>
                      {t("Descarga pendiente · versión {version}", {
                        version: state.resumable_download.published_version,
                      })}
                    </strong>
                    <p>
                      {" "}
                      {t(
                        "Conecta tu cuenta y pulsa Reanudar descarga. Se comprobará la versión publicada y se reutilizarán los paquetes válidos.",
                      )}{" "}
                    </p>
                  </div>
                </div>
              )}
            {state.prepared_summary && (
              <section className="review-panel">
                <h3>
                  {state.fresh_destination
                    ? t("Revisar nueva instalación")
                    : t("Revisar cambios antes de aplicar")}
                </h3>
                <p>
                  {state.fresh_destination
                    ? t(
                        "Nueva instalación · versión {version} · {count} cambios",
                        {
                          version: state.prepared_summary.published_version,
                          count: formatNumber(
                            state.prepared_summary.change_count,
                          ),
                        },
                      )
                    : t("Versión {installed} → {published} · {count} cambios", {
                        installed: state.prepared_summary.installed_version,
                        published: state.prepared_summary.published_version,
                        count: formatNumber(
                          state.prepared_summary.change_count,
                        ),
                      })}
                </p>
                <ul>
                  {state.prepared_summary.changes.map((x) => (
                    <li key={x.path}>
                      <span>{localizeMessage(x.action)}</span>
                      <code>{x.path}</code>
                    </li>
                  ))}
                </ul>
                <button
                  className="primary"
                  disabled={operation || running || !!state.recovery_needed}
                  onClick={() => void call("apply_update")}
                >
                  <ShieldCheck size={16} />
                  {state.fresh_destination
                    ? t("Instalar juego")
                    : t("Aplicar cambios")}
                </button>
                {running && (
                  <small>
                    {t("Cierra el juego para aplicar la actualización.")}
                  </small>
                )}
              </section>
            )}
            {state.recovery_needed && (
              <section className="review-panel recovery">
                <h3>{t("Una operación necesita recuperación")}</h3>
                <p>
                  {" "}
                  {t(
                    "Recupera el estado anterior o finaliza el registro antes de iniciar el juego.",
                  )}{" "}
                </p>
                <button
                  className="outline"
                  disabled={operation || running}
                  onClick={() => void call("recover_update")}
                >
                  {" "}
                  {t("Recuperar instalación")}{" "}
                </button>
              </section>
            )}
            <div className="job-panel">
              <div className="job-icon">
                {state.job?.running ? (
                  <LoaderCircle className="spin" />
                ) : state.job?.error ? (
                  <AlertCircle />
                ) : (
                  <CheckCircle2 />
                )}
              </div>
              <div>
                <h3>
                  {state.job?.running
                    ? t("Operación en curso…")
                    : state.job?.error
                      ? t("La operación no se completó")
                      : state.job?.result?.cancelled === true
                        ? t("Descarga cancelada")
                        : state.prepared
                          ? t("Archivos preparados y verificados")
                          : state.job?.result
                            ? t("Comprobación completada")
                            : t("Preparación segura")}
                </h3>
                <p>
                  {localizeMessage(state.job?.error) ||
                    (state.job?.result?.installed === true
                      ? t(
                          "Instalación publicada y verificada. Ya puedes iniciar el juego.",
                        )
                      : state.job?.result?.cancelled === true
                        ? t(
                            "Descarga cancelada. Puedes reanudarla; tus archivos del juego siguen intactos.",
                          )
                        : state.job?.result?.applied === true
                          ? t(
                              "Actualización aplicada y verificada. Se conservaron los respaldos.",
                            )
                          : state.job?.result?.recovered === true
                            ? t(
                                "Instalación recuperada. Puedes volver a iniciar el juego.",
                              )
                            : state.job?.result?.matching !== undefined
                              ? t(
                                  "{matching} de {files} archivos coinciden con el manifiesto.",
                                  {
                                    matching: formatNumber(
                                      Number(state.job.result.matching),
                                    ),
                                    files: formatNumber(
                                      Number(state.job.result.files),
                                    ),
                                  },
                                )
                              : state.job?.result?.staged_packages !== undefined
                                ? t(
                                    "{count} paquetes preparados. La instalación no se ha modificado.",
                                    {
                                      count: formatNumber(
                                        Number(
                                          state.job.result.staged_packages,
                                        ),
                                      ),
                                    },
                                  )
                                : t(
                                    "Los archivos se descargan y verifican antes de modificar el juego.",
                                  ))}
                </p>
              </div>
            </div>
            <div className="info-box">
              <ShieldCheck size={20} />
              <div>
                <strong>
                  {t("Actualización con respaldo y recuperación")}
                </strong>
                <p>
                  {" "}
                  {t(
                    "Prepara los archivos, revisa los cambios y aplícalos con el juego cerrado. Ember conserva respaldos y puede recuperar una operación interrumpida.",
                  )}{" "}
                </p>
              </div>
            </div>
          </div>
        ) : (
          <div className="content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">{t("A TU MANERA")}</div>
                <h1>{t("Ajustes")}</h1>
              </div>
            </div>
            <section className="settings-card language-settings">
              <div className="settings-title">
                <Monitor size={20} />
                <div>
                  <h3>{t("Idioma del launcher")}</h3>
                  <p>
                    {t(
                      "Elige el idioma de la interfaz. Las noticias conservan su idioma original.",
                    )}
                  </p>
                </div>
              </div>
              <label htmlFor="launcher-language">{t("Idioma")}</label>
              <select
                id="launcher-language"
                value={locale}
                onChange={(event) =>
                  setLocale(event.target.value as "en" | "es")
                }
              >
                <option value="en" lang="en">
                  English
                </option>
                <option value="es" lang="es">
                  Español
                </option>
              </select>
            </section>
            <section className="settings-card session-settings">
              <div className="settings-title">
                <ShieldCheck size={20} />
                <div>
                  <h3>{t("Cuenta y privacidad")}</h3>
                  <p>
                    {state.connected
                      ? t("Tu sesión está conectada a STOVE.")
                      : t("Todavía no has conectado tu cuenta.")}
                  </p>
                </div>
              </div>
              <p>
                {" "}
                {state.remember_session
                  ? t(
                      "Solo el token de renovación se guarda en el almacén seguro del sistema. Nunca guardamos tu contraseña.",
                    )
                  : t(
                      "La sesión se mantiene en memoria y se descarta al cerrar el launcher. No guardamos tu contraseña ni los tokens en disco.",
                    )}{" "}
              </p>
              <label className="remember-session">
                <input
                  type="checkbox"
                  checked={!!state.remember_session}
                  disabled={operation || running}
                  onChange={(event) =>
                    void call("account_settings", {
                      remember_session: event.target.checked,
                    })
                  }
                />
                <span className="remember-control" aria-hidden="true">
                  <Check size={12} strokeWidth={2.5} />
                </span>
                <span>{t("Recordar sesión en este equipo")}</span>
              </label>
              <p>
                {t(
                  "Al volver a abrir Ember intentaremos recuperar tu sesión sin otro QR. Desconectar cuenta elimina la sesión guardada.",
                )}
              </p>
              <div className="session-actions">
                {state.remember_session && !state.connected && (
                  <button
                    className="outline"
                    disabled={operation || running}
                    onClick={() =>
                      void call("restore_session", { retry: true })
                    }
                  >
                    {t("Recuperar sesión guardada")}
                  </button>
                )}
                <button
                  className="outline"
                  disabled={operation || running}
                  onClick={state.connected ? () => void call("logout") : login}
                >
                  {state.connected ? (
                    <LogOut size={16} />
                  ) : (
                    <ScanLine size={16} />
                  )}{" "}
                  {state.connected
                    ? t("Desconectar cuenta")
                    : t("Iniciar sesión con QR")}
                </button>
              </div>
            </section>
            {state.connected && (
              <section className="settings-card">
                <h3>{t("Renovación de sesión")}</h3>
                <p>
                  {" "}
                  {t(
                    "Renueva la sesión en memoria sin cambiar de cuenta. Si STOVE exige otro acceso, Ember te pedirá un QR.",
                  )}{" "}
                </p>
                <button
                  className="outline"
                  disabled={operation}
                  onClick={() => void call("renew_session")}
                >
                  <RefreshCw size={16} /> {t("Renovar sesión")}{" "}
                </button>
                {state.job?.result?.session_renewed === true && (
                  <p role="status">{t("Sesión renovada correctamente.")}</p>
                )}
                {state.job?.kind === "renew_session" && state.job.error && (
                  <p role="status">{localizeMessage(state.job.error)}</p>
                )}
              </section>
            )}
            <section className="settings-card">
              <div className="settings-title">
                <Folder size={20} />
                <div>
                  <h3>{t("Ubicación del juego")}</h3>
                  <p>
                    {state.game_installed
                      ? t("Tu instalación seleccionada.")
                      : t(
                          "Prepara una instalación o selecciona un juego existente.",
                        )}
                  </p>
                </div>
              </div>
              <code className="installation-path">
                {state.game_directory || t("Todavía no has instalado el juego")}
              </code>
              <button
                className="outline"
                disabled={operation || running}
                onClick={() => setSetup(true)}
              >
                {state.game_installed
                  ? t("Instalar en otra ubicación")
                  : t("Instalar juego")}
              </button>
            </section>
            <details
              className="advanced-settings"
              open={advanced}
              onToggle={(event) => setAdvanced(event.currentTarget.open)}
            >
              <summary>
                {" "}
                {t("Opciones avanzadas")}{" "}
                <span>{t("Instalación existente, Wine y reparación")}</span>
              </summary>
              <section className="settings-card">
                <div className="settings-title">
                  <HardDrive size={20} />
                  <div>
                    <h3>{t("Preparar una instalación desde cero")}</h3>
                    <p>
                      {state.native_game
                        ? t(
                            "Prepara una biblioteca nueva sin necesitar el cliente STOVE.",
                          )
                        : t(
                            "Crea un prefijo Wine nuevo sin necesitar el cliente STOVE.",
                          )}
                    </p>
                  </div>
                </div>
                <label>
                  {state.native_game
                    ? t("Carpeta de la nueva biblioteca")
                    : t("Carpeta del nuevo prefijo Wine")}
                  <input
                    value={newPrefix}
                    onChange={(e) => setNewPrefix(e.target.value)}
                    spellCheck={false}
                    placeholder={
                      state.native_game
                        ? "C:\\Games\\EmberLibrary"
                        : "/path/to/EmberPrefix"
                    }
                  />
                </label>
                <p>
                  {state.native_game
                    ? t(
                        "Ember prepara una carpeta nueva para el juego nativo de Windows.",
                      )
                    : t(
                        "Wine debe estar instalado. Ember inicializa una carpeta nueva e instala WebView2 desde Microsoft sin modificar tus otros prefijos.",
                      )}{" "}
                  {t(
                    "Después conecta tu cuenta por QR y descarga el juego.",
                  )}{" "}
                </p>
                <button
                  className="primary"
                  disabled={
                    operation || state.connected || running || !newPrefix
                  }
                  onClick={() =>
                    void call("bootstrap", { prefix: newPrefix, runner })
                  }
                >
                  {state.job?.kind === "bootstrap" && state.job.running ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : (
                    <HardDrive size={16} />
                  )}
                  {state.native_game
                    ? t("Preparar biblioteca y conexión")
                    : t("Crear prefijo y preparar conexión")}
                </button>
                {state.job?.kind === "bootstrap" && (
                  <p role="status">
                    {state.job.running
                      ? localizeMessage(state.job.detail) ||
                        t(
                          "Preparando la instalación. Puede tardar unos minutos…",
                        )
                      : localizeMessage(state.job.error) ||
                        (state.job.result?.prefix_created
                          ? t(
                              "Instalación preparada. Conecta por QR y abre Actualizaciones para instalar el juego.",
                            )
                          : "")}
                  </p>
                )}
              </section>
              <section className="settings-card">
                <div className="settings-title">
                  <ShieldCheck size={20} />
                  <div>
                    <h3>{t("Dependencias del juego")}</h3>
                    <p>
                      {t(
                        "Prepara WebView2 para las ventanas del SDK del juego.",
                      )}
                    </p>
                  </div>
                </div>
                <p>
                  {" "}
                  {t(
                    "Se usa el instalador oficial de Microsoft en el entorno seleccionado. Cierra el juego antes de continuar. El launcher de STOVE no es necesario.",
                  )}{" "}
                </p>
                {!state.native_game && state.runtime_prefix && (
                  <p>
                    {t("Prefijo seleccionado:")} {state.runtime_prefix}
                  </p>
                )}
                <button
                  className="primary"
                  disabled={operation || running || !state.runtime_prefix}
                  onClick={() => void call("prepare_dependencies")}
                >
                  {state.job?.kind === "dependencies" && state.job.running ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : (
                    <ShieldCheck size={16} />
                  )}{" "}
                  {t("Preparar dependencias")}{" "}
                </button>
                {state.job?.kind === "dependencies" && (
                  <p role="status">
                    {state.job.running
                      ? localizeMessage(state.job.detail) ||
                        t("Comprobando las dependencias…")
                      : localizeMessage(state.job.error) ||
                        (state.job.result?.runtime_ready
                          ? t(
                              "Dependencias preparadas{runtime}. Puedes iniciar el juego.",
                              {
                                runtime: state.job.result.runtime_version
                                  ? " · WebView2 " +
                                    state.job.result.runtime_version
                                  : "",
                              },
                            )
                          : "")}
                  </p>
                )}
              </section>
              <section className="settings-card">
                <div className="settings-title">
                  <Folder size={20} />
                  <div>
                    <h3>{t("Instalación del juego")}</h3>
                    <p>
                      {state.native_game
                        ? t("Selecciona la carpeta Smilegate de tu biblioteca.")
                        : t(
                            "Selecciona la carpeta Smilegate de tu prefijo Wine.",
                          )}
                    </p>
                  </div>
                </div>
                <label>
                  {" "}
                  {t("Carpeta Smilegate")}{" "}
                  <input
                    value={root}
                    onChange={(e) => setRoot(e.target.value)}
                    spellCheck={false}
                  />
                </label>
                {!state.native_game && (
                  <label>
                    {" "}
                    {t("Ejecutable Wine")}{" "}
                    <input
                      value={runner}
                      onChange={(e) => setRunner(e.target.value)}
                      spellCheck={false}
                    />
                  </label>
                )}
                <div className="settings-footer">
                  <span>
                    {" "}
                    {t(
                      "Desconecta la cuenta antes de cambiar estas rutas.",
                    )}{" "}
                  </span>
                  <button
                    className="primary"
                    disabled={operation || state.connected || running}
                    onClick={() => void call("settings", { root, runner })}
                  >
                    {" "}
                    {t("Guardar ajustes")}{" "}
                  </button>
                </div>
              </section>
              <section className="settings-card">
                <div className="settings-title">
                  <Download size={20} />
                  <div>
                    <h3>{t("Instalar en una carpeta nueva")}</h3>
                    <p>
                      {state.native_game
                        ? t("Selecciona una carpeta nueva para la biblioteca.")
                        : t(
                            "Selecciona una carpeta dentro de drive_c de un prefijo Wine ya inicializado.",
                          )}
                    </p>
                  </div>
                </div>
                <label>
                  {" "}
                  {t("Nueva carpeta Smilegate")}{" "}
                  <input
                    value={newRoot}
                    onChange={(e) => setNewRoot(e.target.value)}
                    placeholder={
                      state.native_game
                        ? "C:\\Games\\EmberLibrary\\Smilegate"
                        : "/path/to/prefix/drive_c/EmberLibrary/Smilegate"
                    }
                    spellCheck={false}
                  />
                </label>
                <p>
                  {" "}
                  {t(
                    "Ember descarga los archivos de arranque oficiales. El juego gestiona la descarga adicional de contenido. Se reutiliza tu perfil de conexión validado; la carpeta del juego no debe existir.",
                  )}{" "}
                </p>
                <button
                  className="primary"
                  disabled={operation || state.connected || running || !newRoot}
                  onClick={() =>
                    void call("new_installation", { root: newRoot, runner })
                  }
                >
                  {" "}
                  {t("Seleccionar nuevo destino")}{" "}
                </button>
              </section>
            </details>
            <section className="settings-card uninstall-card">
              <div className="settings-title">
                <Trash2 size={20} />
                <div>
                  <h3>{t("Desinstalar el juego")}</h3>
                  <p>
                    {" "}
                    {t(
                      "Envía la instalación seleccionada a la papelera del sistema.",
                    )}{" "}
                  </p>
                </div>
              </div>
              <p>
                {" "}
                {t(
                  "Se conservan la biblioteca y las demás instalaciones. Vacía la papelera para liberar el espacio ocupado.",
                )}{" "}
              </p>
              <code className="installation-path">
                {state.game_directory || t("Sin instalación seleccionada")}
              </code>
              {uninstallTarget && uninstallTarget === state.game_directory ? (
                <div className="uninstall-review">
                  <p>
                    {" "}
                    {t(
                      "¿Enviar esta carpeta completa, incluidos los recursos descargados, a la papelera?",
                    )}{" "}
                  </p>
                  <div className="uninstall-actions">
                    <button
                      className="outline"
                      disabled={operation}
                      onClick={() => setUninstallTarget(null)}
                    >
                      {" "}
                      {t("Cancelar")}{" "}
                    </button>
                    <button
                      className="outline danger"
                      disabled={
                        operation ||
                        running ||
                        state.connected ||
                        !!state.prepared ||
                        !!state.recovery_needed ||
                        !state.game_installed
                      }
                      onClick={() => {
                        void call("uninstall", {
                          path: uninstallTarget,
                          confirmed: true,
                        });
                        setUninstallTarget(null);
                      }}
                    >
                      <Trash2 size={16} /> {t("Confirmar desinstalación")}{" "}
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  className="outline danger"
                  disabled={
                    operation ||
                    running ||
                    state.connected ||
                    !!state.prepared ||
                    !!state.recovery_needed ||
                    !state.game_installed ||
                    !state.game_directory
                  }
                  onClick={() =>
                    setUninstallTarget(state.game_directory || null)
                  }
                >
                  <Trash2 size={16} /> {t("Desinstalar juego")}{" "}
                </button>
              )}
              {(running || state.connected) && (
                <p>
                  {" "}
                  {t(
                    "Cierra el juego y desconecta la cuenta antes de desinstalar.",
                  )}{" "}
                </p>
              )}
              {(state.prepared || state.recovery_needed) && (
                <p>
                  {" "}
                  {t(
                    "Completa o recupera la actualización pendiente antes de desinstalar.",
                  )}{" "}
                </p>
              )}
              {state.job?.kind === "uninstall" && (
                <p role="status">
                  {state.job.running
                    ? t("Enviando el juego a la papelera…")
                    : localizeMessage(state.job.error) ||
                      (state.job.result?.uninstalled
                        ? t(
                            "Juego desinstalado. Puedes recuperarlo desde la papelera o volver a instalarlo.",
                          )
                        : "")}
                </p>
              )}
            </section>
            <div className="about">
              <Flame size={22} />
              <div>
                <strong>
                  Ember Launcher <span>{launcherVersion} · Beta</span>
                </strong>
                <p className="creator-credit">
                  {t("Desarrollado por")}{" "}
                  <a
                    href="https://github.com/iRyzor"
                    onClick={openCreator}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={t("Perfil de GitHub de Ryzor")}
                  >
                    Ryzor <ArrowUpRight size={12} aria-hidden="true" />
                  </a>
                </p>
                <p>
                  {" "}
                  {t(
                    "Launcher independiente. No afiliado a STOVE ni a Smilegate.",
                  )}{" "}
                </p>
              </div>
            </div>
          </div>
        )}
        <footer>
          <span>
            <i className={"live-dot " + (!state.connected ? "muted" : "")} />
            {status}
          </span>
          <span>
            EMBER LAUNCHER <b>{launcherVersion}</b>
            <a
              className="footer-credit"
              href="https://github.com/iRyzor"
              onClick={openCreator}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={t("Perfil de GitHub de Ryzor")}
            >
              Ryzor <ArrowUpRight size={11} aria-hidden="true" />
            </a>
          </span>
        </footer>
      </main>
      {(error || toast) && (
        <div
          className={"toast " + (error ? "error" : "")}
          role={error ? "alert" : "status"}
        >
          {error ? <AlertCircle size={18} /> : <Check size={18} />}
          <span>{localizeMessage(error || toast)}</span>
          <button
            onClick={() => {
              setError("");
              setToast("");
            }}
            aria-label={t("Cerrar aviso")}
          >
            <X size={16} />
          </button>
        </div>
      )}
      {setup && (
        <div
          className="modal-backdrop"
          onClick={(event) => {
            if (event.target === event.currentTarget) setSetup(false);
          }}
        >
          <section
            ref={dialogRef}
            className="modal setup-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="setup-title"
            aria-describedby="setup-description"
          >
            <button
              className="modal-close"
              onClick={() => setSetup(false)}
              aria-label={t("Cerrar instalación")}
            >
              <X size={20} />
            </button>
            <span className="modal-icon">
              <Download size={26} />
            </span>
            <h2 id="setup-title">{t("Instala Chaos Zero Nightmare")}</h2>
            <p id="setup-description">
              {" "}
              {t(
                "Ember prepara una ubicación para Chaos Zero Nightmare. Después podrás iniciar sesión con STOVE y descargarlo.",
              )}{" "}
            </p>
            <ol className="setup-steps">
              <li>{t("Preparar ubicación")}</li>
              <li>{t("Iniciar sesión con QR")}</li>
              <li>{t("Descargar y jugar")}</li>
            </ol>
            <div className="setup-location">
              <strong>{t("Ubicación recomendada")}</strong>
              <code>{newPrefix || t("Cargando ubicación…")}</code>
            </div>
            <details className="setup-options">
              <summary>{t("Cambiar ubicación")}</summary>
              <label>
                {" "}
                {t("Carpeta nueva")}{" "}
                <input
                  value={newPrefix}
                  onChange={(event) => setNewPrefix(event.target.value)}
                  spellCheck={false}
                  disabled={operation}
                />
              </label>
            </details>
            {!state.native_game && (
              <p className="setup-note">
                {" "}
                {t(
                  "Necesitas Wine instalado. Ember prepara los componentes del juego en esta carpeta; puede tardar unos minutos.",
                )}{" "}
              </p>
            )}
            {state.connected && (
              <p className="setup-note">
                {" "}
                {t(
                  "Cierra tu sesión antes de preparar una nueva instalación. Puedes hacerlo en Ajustes.",
                )}{" "}
              </p>
            )}
            {(error ||
              (state.job?.kind === "bootstrap" && state.job.error)) && (
              <p className="setup-error" role="alert">
                {localizeMessage(error || state.job?.error)}
              </p>
            )}
            <button
              className="primary"
              disabled={
                operation || running || state.connected || !newPrefix.trim()
              }
              onClick={() => {
                setupRequested.current = true;
                void call("bootstrap", { prefix: newPrefix.trim(), runner });
              }}
            >
              {operation ? (
                <LoaderCircle className="spin" size={18} />
              ) : (
                <Download size={18} />
              )}
              {operation
                ? t("Preparando ubicación…")
                : t("Preparar instalación")}
            </button>
            {state.job?.kind === "bootstrap" && state.job.running && (
              <p role="status">
                {localizeMessage(state.job.detail) ||
                  t("Preparando los componentes del juego…")}
              </p>
            )}
            <button
              className="setup-existing"
              disabled={operation}
              onClick={() => {
                setSetup(false);
                setAdvanced(true);
                setPage("settings");
              }}
            >
              {" "}
              {t("Ya tengo el juego instalado")}{" "}
            </button>
          </section>
        </div>
      )}
      {modal && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setModal(false);
          }}
        >
          <section
            ref={dialogRef}
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="login-title"
          >
            <button
              className="modal-close"
              onClick={() => setModal(false)}
              aria-label={t("Cerrar")}
            >
              <X size={20} />
            </button>
            <span className="modal-icon">
              <ScanLine size={26} />
            </span>
            <div className="eyebrow">{t("TU CUENTA STOVE")}</div>
            <h2 id="login-title">{t("Inicia sesión con STOVE")}</h2>
            <p>
              {" "}
              {t("Abre la app de STOVE, escanea el código")} <br />
              {t("y confirma el inicio de sesión.")}{" "}
            </p>
            <div className="qr-box">
              {state.image ? (
                <img
                  src={"data:image/png;base64," + state.image}
                  alt={t("Código QR para iniciar sesión en STOVE")}
                />
              ) : busy ? (
                <LoaderCircle size={35} className="spin" />
              ) : (
                <ScanLine size={45} />
              )}
            </div>
            <div className="qr-status" role="status">
              {state.phase === "qr_expired"
                ? t("El código ha vencido")
                : localizeMessage(error) ||
                  (state.image
                    ? t("Esperando confirmación en tu app…")
                    : t("Generando tu código…"))}
            </div>
            <label className="remember-session">
              <input
                type="checkbox"
                checked={!!state.remember_session}
                disabled={operation}
                onChange={(event) =>
                  void call("account_settings", {
                    remember_session: event.target.checked,
                  })
                }
              />
              <span className="remember-control" aria-hidden="true">
                <Check size={12} strokeWidth={2.5} />
              </span>
              <span>{t("Recordar sesión en este equipo")}</span>
            </label>
            <button
              className="outline"
              disabled={busy}
              onClick={() => void call("login")}
            >
              <RefreshCw size={15} /> {t("Generar otro código")}{" "}
            </button>
            <div className="modal-private">
              <ShieldCheck size={14} />{" "}
              {t("Tu sesión se conserva solo en este equipo.")}{" "}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
