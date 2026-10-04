// Hubinet-Ops guest cards: one LXC or VM with status, resources, snapshots and
// power controls (LXC also packages), as a full or a mini card. Presentation
// and invocation of existing entities and actions only; power actions other
// than Start, and Restore and Delete, need a second tap on the same target
// within a few seconds, and nothing acts on render. Tapping a stat tile opens
// Home Assistant's own more-info (history) for that sensor.

const version = new URL(import.meta.url).search;
const load = (file) => import(new URL(`./${file}${version}`, import.meta.url).href);
const easy = await load("easy-update-logic.js");
const guest = await load("guest-card-logic.js");

const HOLD_MS = 500;
const HISTORY_MS = 24 * 3600 * 1000;
const HISTORY_REFRESH_MS = 5 * 60 * 1000;

const TONES = {
  amber: "var(--amber-color, #ffc107)",
  green: "var(--green-color, #4caf50)",
  blue: "var(--blue-color, #2196f3)",
  red: "var(--red-color, #f44336)",
  orange: "var(--orange-color, #ff9800)",
  grey: "var(--grey-color, #9e9e9e)",
  purple: "var(--purple-color, #9c27b0)",
  error: "var(--red-color, #f44336)",
};

const ICONS = {
  create: "mdi:camera-plus-outline",
  restore: "mdi:backup-restore",
  delete: "mdi:delete-outline",
  start: "mdi:play",
  stop: "mdi:stop",
  restart: "mdi:restart",
  update: "mdi:package-up",
  scan: "mdi:magnify",
  details: "mdi:information-outline",
  shutdown: "mdi:power",
  reset: "mdi:flash",
  hibernate: "mdi:power-sleep",
  [guest.SKIP_UPDATE]: "mdi:alert",
};
const GUEST_ICONS = { lxc: "mdi:cube-outline", vm: "mdi:monitor" };
const ACTION_TONES = {
  create: "blue",
  restore: "orange",
  delete: "red",
  start: "green",
  stop: "red",
  restart: "orange",
  update: "amber",
  scan: "blue",
  details: "grey",
  shutdown: "orange",
  reset: "red",
  hibernate: "purple",
  [guest.SKIP_UPDATE]: "red",
};

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

const STYLE = `
  :host { display: block; }
  /* Every grid has minmax(0, 1fr) tracks: content never widens the card. */
  ha-card { padding: 16px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px;
    box-sizing: border-box; height: 100%; container-type: inline-size; }
  .head { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; min-width: 0;
    cursor: pointer; }
  .shape { flex: none; width: 42px; height: 42px; border-radius: 50%; display: grid;
    place-items: center; color: var(--tone);
    background: color-mix(in srgb, var(--tone) 18%, transparent); }
  .titles { min-width: 0; flex: 1 1 72px; }
  .name { font-weight: 700; font-size: 16px; color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { color: var(--secondary-text-color); font-size: 12px; overflow-wrap: anywhere; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
    border-radius: 999px; font-size: 12px; font-weight: 600; color: var(--tone);
    background: color-mix(in srgb, var(--tone) 15%, transparent);
    box-sizing: border-box; min-width: 0; max-width: 100%; }
  .chip i { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--tone); }
  .chip span { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stats { display: grid; gap: 8px;
    grid-template-columns: repeat(auto-fit, minmax(min(110px, 100%), 1fr)); }
  .stat { background: var(--secondary-background-color, rgba(127,127,127,.1));
    border-radius: 10px; padding: 8px 10px; display: grid; gap: 2px; min-width: 0;
    grid-template-columns: minmax(0, 1fr);
    align-content: start; justify-content: stretch; align-items: stretch;
    text-align: start; font-weight: 400; color: var(--primary-text-color); }
  button.stat:hover { background: color-mix(in srgb, var(--primary-text-color) 8%,
    var(--secondary-background-color, rgba(127,127,127,.1))); }
  .stat small { color: var(--secondary-text-color); font-size: 11px;
    text-transform: uppercase; letter-spacing: .05em; overflow-wrap: anywhere; }
  .stat b { font-size: 15px; font-weight: 600; color: var(--primary-text-color);
    font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat em { font-style: normal; font-size: 11px; color: var(--secondary-text-color);
    overflow-wrap: anywhere; }
  .stat svg { width: 100%; height: 26px; display: block; }
  .meter { height: 8px; border-radius: 999px; overflow: hidden; margin-top: 6px;
    background: color-mix(in srgb, var(--tone) 16%, transparent); }
  .meter span { display: block; height: 100%; border-radius: inherit; background: var(--tone); }
  /* RAM with guest data: Guest and Host, each with its own thin bar. */
  .lines { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; }
  .line { display: grid; grid-template-columns: minmax(0, 1fr); gap: 1px; }
  .line span { font-size: 11px; color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .line .meter { height: 4px; margin-top: 3px; }
  .mini .line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 6px; }
  .mini .line b { font-size: 13px; }
  .mini .line .meter { flex: 1 1 100%; }
  .dim { opacity: .45; }
  .section { border-top: 1px solid var(--divider-color, rgba(127,127,127,.25));
    padding-top: 12px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .section h3 { margin: 0; font-size: 12px; font-weight: 600; color: var(--secondary-text-color);
    text-transform: uppercase; letter-spacing: .06em; display: flex; flex-wrap: wrap;
    justify-content: space-between; align-items: center; gap: 4px 8px; overflow-wrap: anywhere; }
  button.more { padding: 2px 6px; font-size: 12px; text-transform: none; letter-spacing: 0;
    background: none; color: var(--primary-color); }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11.5px;
    color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .legend b { color: var(--primary-text-color); font-weight: 600; }
  /* The text takes the free space on a shared line; when the actions do not
     fit beside it they wrap below and fill the row. */
  .pkg { display: flex; flex-wrap: wrap; gap: 10px 12px; align-items: center; min-width: 0; }
  .pkg .t { min-width: 0; flex: 999 1 180px; }
  @container (max-width: 300px) { .pkg .t { flex-basis: 88px; } }
  .pkg .p { font-weight: 700; color: var(--primary-text-color); overflow-wrap: anywhere; }
  .pkg .s { color: var(--secondary-text-color); font-size: 12px; overflow-wrap: anywhere; }
  .pkg .acts { --slot: 126px; flex: 1 1 var(--slot); }
  .pkg .acts.two { flex-basis: calc(2 * var(--slot) + 8px); }
  /* Action groups: every button keeps one fixed slot, so a longer armed
     label wraps inside its button and never moves a neighbour. Buttons that
     do not fit go to the next row and share it. */
  .acts { --slot: 116px; display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; }
  .acts > button { flex: 1 1 var(--slot); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; min-width: 0; }
  select { font: inherit; flex: 1 1 200px; min-width: 0; max-width: 100%; box-sizing: border-box;
    padding: 8px 10px; border-radius: 10px;
    border: 1px solid var(--divider-color, rgba(127,127,127,.35));
    background: var(--secondary-background-color, transparent); color: var(--primary-text-color); }
  .power4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .power4 > button { padding-inline: 8px; }
  @container (max-width: 440px) { .power4 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @container (max-width: 215px) { .power4 { grid-template-columns: minmax(0, 1fr); } }
  button { font: inherit; font-size: 13px; font-weight: 600; display: inline-flex;
    align-items: center; justify-content: center; gap: 6px; padding: 8px 12px;
    border-radius: 10px; border: 0; cursor: pointer; color: var(--tone);
    background: color-mix(in srgb, var(--tone) 14%, transparent);
    box-sizing: border-box; min-width: 0; max-width: 100%; text-align: center; }
  button > span { min-width: 0; overflow-wrap: break-word; }
  button > ha-icon, button > ha-circular-progress { flex: none; }
  /* A frontend that no longer ships <ha-circular-progress> still shows the
     running spinner. */
  ha-circular-progress:not(:defined) { display: inline-block; box-sizing: border-box;
    width: 16px; height: 16px; border: 2px solid currentColor;
    border-inline-end-color: transparent; border-radius: 50%;
    animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  button:hover { background: color-mix(in srgb, var(--tone) 22%, transparent); }
  button:focus-visible, select:focus-visible, .head:focus-visible, .stat:focus-visible {
    outline: 2px solid var(--primary-color); outline-offset: 2px; }
  button[disabled] { opacity: .4; cursor: not-allowed; }
  button.armed { background: var(--tone); color: #fff; }
  button.primary { background: var(--tone); color: #111; }
  ha-icon { --mdc-icon-size: 20px; }
  .error { color: var(--secondary-text-color); overflow-wrap: anywhere; }
  ha-card.mini { padding: 12px; gap: 10px; }
  .mini .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  @container (max-width: 199px) { .mini .stats { grid-template-columns: minmax(0, 1fr); } }
  .mini .stat { padding: 6px 9px; }
  .mini .stat svg { height: 18px; }
  .mline { --slot: 116px; display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center;
    min-width: 0; }
  .mline .t { flex: 999 1 96px; min-width: 0; }
  .mline > button { flex: 1 1 var(--slot); }
  .mline .p, .mline .s { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .mline .p { font-weight: 700; color: var(--primary-text-color); }
  .mline .s { font-size: 12px; color: var(--secondary-text-color); }
`;

const spark = (values, tone, max) => {
  if (!values || values.length < 2) {
    return "";
  }
  const w = 100;
  const h = 26;
  const top = max ?? (Math.max(...values) * 1.15 || 1);
  const pts = values.map((v, i) => [
    (i * w) / (values.length - 1),
    h - 2 - (Math.min(v, top) / top) * (h - 4),
  ]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${line}L${w},${h}L0,${h}Z" fill="${tone}" fill-opacity=".14"/>
    <path d="${line}" fill="none" stroke="${tone}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="2.2" fill="${tone}"/></svg>`;
};

class HubinetOpsGuestCard extends HTMLElement {
  static kind = "lxc";
  static mini = false;

  static getConfigForm() {
    const s = guest.guestStrings(document.documentElement.lang || navigator.language);
    const lxc = this.kind === "lxc";
    return {
      schema: [
        {
          name: "device_id",
          required: true,
          selector: {
            device: {
              filter: { integration: guest.DOMAIN, model: guest.KINDS[this.kind].model },
            },
          },
        },
        ...(lxc ? [{ name: "autoremove", selector: { boolean: {} } }] : []),
        { name: "name", selector: { text: {} } },
      ],
      computeLabel: (schema) =>
        (!lxc && s[`label_${schema.name}_vm`]) || s[`label_${schema.name}`],
    };
  }

  static getStubConfig(hass) {
    return {
      device_id: guest.firstGuestDevice(hass && hass.entities, this.kind) || "",
      ...(this.kind === "lxc" ? { autoremove: false } : {}),
    };
  }

  get _kind() {
    return this.constructor.kind;
  }

  // Mini card type, or a saved LXC card from before with `compact: true`.
  get _isMini() {
    return this.constructor.mini || Boolean(this._config && this._config.compact);
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Invalid configuration");
    }
    this._config = { autoremove: false, ...config };
    this._more = false;
    this._disarm();
    this._history = {};
    this._historyKey = undefined;
    this._signature = undefined;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return this._isMini ? 3 : 7;
  }

  // Defaults only: Home Assistant applies a saved grid_options over them.
  getGridOptions() {
    return this._isMini ? { columns: 9, min_columns: 6 } : { columns: 12, min_columns: 6 };
  }

  disconnectedCallback() {
    // A confirmation never survives the card leaving the page.
    this._disarm();
    this._pressing = false;
    this._signature = undefined;
  }

  _disarm() {
    clearTimeout(this._armTimer);
    this._armed = undefined;
  }

  // True only while the armed tap still stands for exactly this operation.
  _isArmed(action, view = this._view) {
    const armed = this._armed;
    return Boolean(
      armed &&
        armed.action === action &&
        Date.now() < armed.until &&
        armed.target === guest.confirmTarget(action, view, this._config.device_id)
    );
  }

  _arm(action, target) {
    clearTimeout(this._armTimer);
    this._armed = { action, target, until: Date.now() + guest.CONFIRM_MS };
    this._armTimer = setTimeout(() => {
      this._disarm();
      this._render();
    }, guest.CONFIRM_MS);
  }

  _lang() {
    const hass = this._hass;
    return (hass && hass.locale && hass.locale.language) || (hass && hass.language) || "en";
  }

  _derive() {
    const hass = this._hass;
    const lang = this._lang();
    const packageView =
      this._kind === "lxc" && easy.resolveEntities(hass.entities, this._config.device_id).entities
      ? easy.deriveView({
          states: hass.states,
          entities: hass.entities,
          devices: hass.devices,
          config: { device_id: this._config.device_id, autoremove: Boolean(this._config.autoremove) },
          now: Date.now(),
          lang,
          // Update without a snapshot exists in the full LXC card only.
          offerSkipSnapshot: !this._isMini,
        })
      : null;
    return guest.deriveGuestView({
      states: hass.states,
      entities: hass.entities,
      devices: hass.devices,
      config: this._config,
      lang,
      packageView,
      kind: this._kind,
    });
  }

  async _loadHistory(view) {
    const ids = view.stats.map((stat) => stat.history).filter(Boolean);
    const key = ids.join(",");
    const now = Date.now();
    if (!ids.length || (key === this._historyKey && now - (this._historyAt || 0) < HISTORY_REFRESH_MS)) {
      return;
    }
    this._historyKey = key;
    this._historyAt = now;
    try {
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: new Date(now - HISTORY_MS).toISOString(),
        entity_ids: ids,
        minimal_response: true,
        no_attributes: true,
      });
      for (const id of ids) {
        this._history[id] = guest.historyPoints(result && result[id]);
      }
      this._signature = undefined;
      this._render();
    } catch (_err) {
      // Sparklines are optional; current values still show.
    }
  }

  _render() {
    if (!this._config || !this._hass) {
      return;
    }
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (ev) => this._click(ev));
      this.shadowRoot.addEventListener("change", (ev) => this._change(ev));
      this.shadowRoot.addEventListener("keydown", (ev) => this._key(ev));
      this.shadowRoot.addEventListener("focusout", (ev) => {
        if (ev.target && ev.target.dataset && ev.target.dataset.role === "snapshot") {
          // The snapshot list may rebuild again once it is no longer in use;
          // only after focus has settled, and only if the view changed.
          this._snapshotChosen = false;
          setTimeout(() => this._render(), 0);
        }
      });
      this.shadowRoot.addEventListener("pointerdown", (ev) => {
        this._press();
        this._holdStart(ev);
      });
      for (const type of ["pointerup", "pointerleave", "pointercancel"]) {
        this.shadowRoot.addEventListener(type, () => clearTimeout(this._holdTimer));
      }
    }
    const view = this._derive();
    this._view = view;
    // A changed target (another snapshot, another LXC) cancels confirmation.
    if (this._armed && !this._isArmed(this._armed.action, view)) {
      this._disarm();
    }
    if (!view.error) {
      this._loadHistory(view);
    }
    const signature = JSON.stringify([view, this._armed, this._busy, this._history, this._more]);
    if (signature === this._signature) {
      return;
    }
    // Rebuilding replaces every element: never while a pointer is pressed on
    // the card (the click would be lost) or under a snapshot list in use (it
    // would close), and give focus back to the same control.
    const active = this.shadowRoot.activeElement;
    const focused = active && active.dataset ? active.dataset : null;
    if (this._pressing || (focused && focused.role === "snapshot" && !this._snapshotChosen)) {
      return;
    }
    this._signature = signature;
    const mini = this._isMini;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card class="${mini ? "mini" : ""}">${
      view.error ? `<div class="error">${esc(view.error)}</div>` : mini ? this._mini(view) : this._full(view)
    }</ha-card>`;
    // Taps act on what the user sees, never on a newer view not yet shown.
    this._shown = view;
    // A control disabled while an action runs gets focus back when it ends,
    // unless focus has meanwhile moved elsewhere on the page.
    const page = typeof document !== "undefined" ? document.activeElement : null;
    const unclaimed = !page || page === document.body || page === this;
    const selector = focused
      ? focused.key
        ? `[data-key="${focused.key}"]`
        : focused.action
          ? `[data-action="${focused.action}"]`
          : focused.role
            ? `[data-role="${focused.role}"]`
            : null
      : unclaimed
        ? this._focusLater
        : null;
    const again = selector && this.shadowRoot.querySelector(selector);
    if (again && !again.disabled) {
      again.focus();
      this._focusLater = undefined;
    } else {
      // Kept only across one running action, never for later updates.
      this._focusLater = this._busy ? selector || undefined : undefined;
    }
  }

  // While a pointer is pressed on the card the DOM is not rebuilt, so the
  // press still produces its click. The release may happen anywhere.
  _press() {
    this._pressing = true;
    const release = () => {
      window.removeEventListener("pointerup", release, true);
      window.removeEventListener("pointercancel", release, true);
      this._pressing = false;
      // Runs after the click this press produces, never under it.
      setTimeout(() => this._render(), 0);
    };
    window.addEventListener("pointerup", release, true);
    window.addEventListener("pointercancel", release, true);
  }

  // Enter and Space on the header (role=button) act like a tap; native
  // buttons already turn them into clicks.
  _key(ev) {
    if (ev.key !== "Enter" && ev.key !== " ") {
      return;
    }
    const target = ev.target.closest && ev.target.closest('[role="button"][data-action]');
    if (!target) {
      return;
    }
    ev.preventDefault();
    this._click({ target });
  }

  _button(action, info, label, extra = "", key = action, hint = "") {
    const s = guest.guestStrings(this._lang());
    const armed = this._isArmed(action);
    const tone = TONES[ACTION_TONES[action]];
    const creating = action === "create" && this._view?.snapshotCreateRunning;
    return `<button data-action="${action}" data-key="${key}" style="--tone:${tone}" class="${armed ? "armed" : ""} ${extra}"${hint ? ` title="${esc(hint)}"` : ""}
      ${info && info.available && !this._busy ? "" : "disabled"}>
      ${creating ? '<ha-circular-progress size="small" indeterminate></ha-circular-progress>' : `<ha-icon icon="${ICONS[action]}"></ha-icon>`}<span>${esc(creating ? s.creating : armed ? s.confirm : label)}</span></button>`;
  }

  // Stat tiles; a tile with a sensor opens its native history when tapped.
  _stats(view, keys) {
    const s = guest.guestStrings(this._lang());
    return view.stats
      .filter((stat) => !keys || keys.includes(stat.key))
      .map((stat) => {
        const tone = TONES[stat.tone];
        const history = stat.history ? this._history[stat.history] : null;
        // Only the bar width is clamped; the shown value never is.
        const meter = (pct) =>
          pct !== undefined && pct !== null
            ? `<div class="meter" style="--tone:${tone}"><span style="width:${Math.max(0, Math.min(100, pct))}%"></span></div>`
            : "";
        const values = stat.lines
          ? `<div class="lines">${stat.lines
              .map(
                (line) => `<div class="line" data-line="${line.key}"><span>${esc(line.label)}</span>
                  <b>${esc(line.value)}</b>${!this._isMini && line.detail ? `<em>${esc(line.detail)}</em>` : ""}
                  ${meter(line.pct)}</div>`
              )
              .join("")}</div>`
          : `<b>${esc(stat.value)}</b>${stat.detail ? `<em>${esc(stat.detail)}</em>` : ""}
          ${meter(stat.pct)}`;
        const body = `<small>${esc(stat.label)}</small>
          ${values}${spark(history, tone, stat.max)}`;
        const cls = `stat${view.running ? "" : " dim"}`;
        return stat.entity
          ? `<button class="${cls}" data-action="history" data-stat="${stat.key}" data-key="stat-${stat.key}"
              aria-label="${esc(guest.fill(s.history, { label: stat.label, value: stat.value }))}">${body}</button>`
          : `<div class="${cls}">${body}</div>`;
      })
      .join("");
  }

  _head(view) {
    return `<div class="head" tabindex="0" role="button" data-action="details">
        <div class="shape" style="--tone:${TONES[view.status.tone]}"><ha-icon icon="${GUEST_ICONS[view.kind]}"></ha-icon></div>
        <div class="titles"><div class="name">${esc(view.name)}</div><div class="sub">${esc(view.uptime)}</div></div>
        <span class="chip" style="--tone:${TONES[view.status.tone]}"><i></i><span>${esc(view.status.label)}</span></span>
      </div>`;
  }

  _pkgButton(view) {
    const s = guest.guestStrings(this._lang());
    const pkgAction = view.package && view.package.action.kind;
    return pkgAction === "easy_update"
      ? this._button("update", { available: true }, s.update, "primary")
      : pkgAction === "scan"
        ? this._button("scan", { available: true }, s.scan)
        : pkgAction === "details"
          ? // Navigation only, through the same path as the header.
            this._button("details", { available: true }, s.details, "", "package-details")
          : "";
  }

  // The Update without a snapshot, beside the package action of a full LXC card.
  _skipButton(view) {
    const s = guest.guestStrings(this._lang());
    return view.skipUpdate
      ? this._button(guest.SKIP_UPDATE, view.skipUpdate, s.skip_update, "", guest.SKIP_UPDATE, s.skip_update_hint)
      : "";
  }

  _power(view) {
    const s = guest.guestStrings(this._lang());
    const a = view.actions;
    if (view.kind !== "vm") {
      return `<div class="section"><h3>${esc(s.power)}</h3><div class="acts">
        ${this._button("start", a.start, s.start)}${this._button("stop", a.stop, s.stop)}${this._button("restart", a.restart, s.restart)}
      </div></div>`;
    }
    const more = this._more;
    return `<div class="section"><h3>${esc(s.power)}<button class="more" data-action="more"
        aria-expanded="${more}" style="--tone:var(--primary-color)">${esc(more ? `${s.less} ▴` : `${s.more} ▾`)}</button></h3>
      <div class="power4">${this._button("start", a.start, s.start)}${this._button("shutdown", a.shutdown, s.shutdown)}${this._button("stop", a.stop, s.stop)}${this._button("restart", a.restart, s.restart)}</div>
      ${more ? `<div class="acts">${this._button("reset", a.reset, s.reset)}${this._button("hibernate", a.hibernate, s.hibernate)}</div>` : ""}
      <div class="legend"><span><b>${esc(s.shutdown)}</b> ${esc(s.legend_shutdown)}</span><span><b>${esc(s.stop)}</b> ${esc(s.legend_stop)}</span>${more ? `<span><b>${esc(s.reset)}</b> ${esc(s.legend_reset)}</span>` : ""}</div>
    </div>`;
  }

  _mini(view) {
    const s = guest.guestStrings(this._lang());
    const pkg = view.package;
    const snap = view.snapshots;
    let line;
    let action = "";
    if (view.kind === "vm") {
      line = { p: view.status.label, s: snap ? guest.fill(s.snapshot_count, { count: snap.options.length }) : "" };
      action = view.running
        ? this._button("shutdown", view.actions.shutdown, s.shutdown)
        : this._button("start", view.actions.start, s.start);
    } else if (pkg) {
      line = { p: pkg.primary, s: pkg.secondary };
      action = this._pkgButton(view);
    } else {
      line = { p: view.status.label, s: view.uptime };
    }
    return `${this._head(view)}
      <div class="stats">${this._stats(view, ["cpu", "ram"])}</div>
      <div class="mline"><div class="t"><div class="p">${esc(line.p)}</div>${line.s ? `<div class="s">${esc(line.s)}</div>` : ""}</div>${action}</div>`;
  }

  _full(view) {
    const s = guest.guestStrings(this._lang());
    const stats = this._stats(view);
    const pkg = view.package;
    const pkgButton = this._pkgButton(view);
    const skipButton = this._skipButton(view);
    const snap = view.snapshots;
    const options = snap
      ? snap.options.length
        ? [`<option value="" ${snap.selected ? "" : "selected"} disabled>${esc(s.choose_snapshot)}</option>`]
            .concat(
              snap.options.map(
                (o) => `<option value="${esc(o)}" ${o === snap.selected ? "selected" : ""}>${esc(o)}</option>`
              )
            )
            .join("")
        : `<option>${esc(s.no_snapshots)}</option>`
      : "";
    return `
      ${this._head(view)}
      <div class="stats">${stats}</div>
      ${
        pkg
          ? `<div class="section"><h3>${esc(s.packages)}</h3><div class="pkg">
          <div class="shape" style="--tone:${TONES[pkg.tone] || TONES.grey}"><ha-icon icon="${esc(pkg.icon)}"></ha-icon></div>
          <div class="t"><div class="p">${esc(pkg.primary)}</div><div class="s">${esc(pkg.secondary)}</div></div>
          ${pkgButton || skipButton ? `<div class="acts${pkgButton && skipButton ? " two" : ""}">${pkgButton}${skipButton}</div>` : ""}</div></div>`
          : ""
      }
      ${
        snap
          ? `<div class="section"><h3>${esc(s.snapshots)}</h3>
          <div class="row"><select data-role="snapshot" ${snap.available && snap.options.length ? "" : "disabled"}>${options}</select></div>
          <div class="acts">${this._button("create", view.actions.create, s.create)}${this._button("restore", view.actions.restore, s.restore)}${this._button("delete", view.actions.delete, s.delete)}</div></div>`
          : ""
      }
      ${this._power(view)}`;
  }

  _holdStart(ev) {
    if (!ev.target.closest(".head")) {
      return;
    }
    this._held = false;
    clearTimeout(this._holdTimer);
    this._holdTimer = setTimeout(() => {
      this._held = true;
      this._details();
    }, HOLD_MS);
  }

  _details() {
    const hass = this._hass;
    const deviceId = this._config.device_id;
    if (hass.user && hass.user.is_admin) {
      window.history.pushState(null, "", `/config/devices/device/${deviceId}`);
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
      return;
    }
    const lookup = guest.resolveGuest(hass.entities, hass.states, deviceId, this._kind);
    if (lookup.entities) {
      this._moreInfo(lookup.entities.status);
    }
  }

  _moreInfo(entityId) {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      })
    );
  }

  async _change(ev) {
    const select = ev.target.closest("select[data-role=snapshot]");
    const shown = this._shown;
    if (!select || !select.value || !shown || !shown.snapshots) {
      return;
    }
    this._disarm();
    this._snapshotChosen = true;
    await this._call("select", "select_option", {
      entity_id: shown.snapshots.entity_id,
      option: select.value,
    });
  }

  async _click(ev) {
    const target = ev.target.closest("[data-action]");
    if (!target || target.disabled) {
      return;
    }
    const action = target.dataset.action;
    if (action === "details") {
      // A hold already opened details from the header; a button never holds.
      if (this._held && target.tagName !== "BUTTON") {
        this._held = false;
      } else {
        this._details();
      }
      return;
    }
    const view = this._shown;
    if (!view) {
      return;
    }
    if (action === "history") {
      // Navigation only: Home Assistant's own more-info shows the history.
      const stat = view.stats.find((item) => item.key === target.dataset.stat);
      if (stat && stat.entity) {
        this._moreInfo(stat.entity);
      }
      return;
    }
    if (action === "more") {
      this._more = !this._more;
      this._render();
      return;
    }
    // Never during a request in flight, even from a not yet rebuilt button.
    if (action === guest.SKIP_UPDATE && this._busy) {
      return;
    }
    if (guest.CONFIRM.has(action)) {
      const target = guest.confirmTarget(action, view, this._config.device_id);
      if (!target) {
        this._disarm();
        this._render();
        return;
      }
      if (!this._isArmed(action, view)) {
        this._arm(action, target);
        this._render();
        return;
      }
    }
    this._disarm();
    if (action === "update") {
      await this._call(guest.DOMAIN, "easy_update", view.package.action.data);
    } else if (action === guest.SKIP_UPDATE) {
      // One request for exactly the shown scan; nothing is saved in the card.
      await this._call(guest.DOMAIN, "easy_update", view.skipUpdate.data);
    } else if (action === "scan") {
      await this._call("button", "press", { entity_id: view.package.action.entity_id });
    } else {
      await this._call("button", "press", { entity_id: view.actions[action].entity_id });
    }
  }

  async _call(domain, service, data) {
    this._busy = true;
    this._render();
    try {
      await this._hass.callService(domain, service, data);
    } catch (_err) {
      // Home Assistant already shows the translated error as a toast.
    } finally {
      this._busy = false;
      this._signature = undefined;
      this._render();
    }
  }
}

const CARDS = [
  {
    kind: "lxc",
    mini: false,
    name: "Hubinet-Ops LXC",
    description:
      "Hubinet-Ops: LXC status, resources, packages, snapshots and power / karta kontenera LXC (Hubinet)",
    doc: "lxc-card",
  },
  {
    kind: "lxc",
    mini: true,
    name: "Hubinet-Ops LXC mini",
    description: "Hubinet-Ops: LXC at a glance, CPU/RAM and packages / mała karta LXC (Hubinet)",
    doc: "mini-cards",
  },
  {
    kind: "vm",
    mini: false,
    name: "Hubinet-Ops VM",
    description:
      "Hubinet-Ops: QEMU VM status, resources, snapshots and full power / karta maszyny VM (Hubinet)",
    doc: "vm-card",
  },
  {
    kind: "vm",
    mini: true,
    name: "Hubinet-Ops VM mini",
    description: "Hubinet-Ops: VM at a glance, CPU/RAM, Start/Shut down / mała karta VM (Hubinet)",
    doc: "mini-cards",
  },
];

window.customCards = window.customCards || [];
for (const card of CARDS) {
  const type = guest.KINDS[card.kind][card.mini ? "mini" : "full"];
  if (!customElements.get(type)) {
    customElements.define(
      type,
      class extends HubinetOpsGuestCard {
        static kind = card.kind;
        static mini = card.mini;
      }
    );
  }
  if (!window.customCards.some((entry) => entry.type === type)) {
    window.customCards.push({
      type,
      name: card.name,
      description: card.description,
      preview: true,
      documentationURL: `https://github.com/shockwave9315/hubinet-ops-next#${card.doc}`,
    });
  }
}
