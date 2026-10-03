// Hubinet-Ops LXC card: one container's status, resources, packages,
// snapshots and power controls. Presentation and invocation of existing
// entities and actions only; Stop, Restart, Restore and Delete need a second
// tap on the same target within a few seconds, and nothing acts on render.

const version = new URL(import.meta.url).search;
const load = (file) => import(new URL(`./${file}${version}`, import.meta.url).href);
const easy = await load("easy-update-logic.js");
const lxc = await load("lxc-card-logic.js");

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
};
const ACTION_TONES = {
  create: "blue",
  restore: "orange",
  delete: "red",
  start: "green",
  stop: "red",
  restart: "orange",
  update: "amber",
  scan: "blue",
};

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

const STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; display: grid; gap: 14px; box-sizing: border-box; height: 100%; }
  .head { display: flex; gap: 12px; align-items: center; min-width: 0; cursor: pointer; }
  .shape { flex: none; width: 42px; height: 42px; border-radius: 50%; display: grid;
    place-items: center; color: var(--tone);
    background: color-mix(in srgb, var(--tone) 18%, transparent); }
  .titles { min-width: 0; flex: 1; }
  .name { font-weight: 700; font-size: 16px; color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { color: var(--secondary-text-color); font-size: 12px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
    border-radius: 999px; font-size: 12px; font-weight: 600; color: var(--tone);
    background: color-mix(in srgb, var(--tone) 15%, transparent); white-space: nowrap; }
  .chip i { width: 7px; height: 7px; border-radius: 50%; background: var(--tone); }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
  .stat { background: var(--secondary-background-color, rgba(127,127,127,.1));
    border-radius: 10px; padding: 8px 10px; display: grid; gap: 2px; min-width: 0; }
  .stat small { color: var(--secondary-text-color); font-size: 11px;
    text-transform: uppercase; letter-spacing: .05em; }
  .stat b { font-size: 15px; font-weight: 600; color: var(--primary-text-color);
    font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat em { font-style: normal; font-size: 11px; color: var(--secondary-text-color); }
  .stat svg { width: 100%; height: 26px; display: block; }
  .meter { height: 8px; border-radius: 999px; overflow: hidden; margin-top: 6px;
    background: color-mix(in srgb, var(--tone) 16%, transparent); }
  .meter span { display: block; height: 100%; border-radius: inherit; background: var(--tone); }
  .dim { opacity: .45; }
  .section { border-top: 1px solid var(--divider-color, rgba(127,127,127,.25));
    padding-top: 12px; display: grid; gap: 10px; }
  .section h3 { margin: 0; font-size: 12px; font-weight: 600; color: var(--secondary-text-color);
    text-transform: uppercase; letter-spacing: .06em; }
  .pkg { display: flex; gap: 12px; align-items: center; min-width: 0; }
  .pkg .t { min-width: 0; flex: 1; }
  .pkg .p { font-weight: 700; color: var(--primary-text-color); }
  .pkg .s { color: var(--secondary-text-color); font-size: 12px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  select { font: inherit; flex: 1 1 200px; min-width: 0; padding: 8px 10px; border-radius: 10px;
    border: 1px solid var(--divider-color, rgba(127,127,127,.35));
    background: var(--secondary-background-color, transparent); color: var(--primary-text-color); }
  .power { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  button { font: inherit; font-size: 13px; font-weight: 600; display: inline-flex;
    align-items: center; justify-content: center; gap: 6px; padding: 8px 12px;
    border-radius: 10px; border: 0; cursor: pointer; color: var(--tone);
    background: color-mix(in srgb, var(--tone) 14%, transparent); }
  button:hover { background: color-mix(in srgb, var(--tone) 22%, transparent); }
  button:focus-visible, select:focus-visible, .head:focus-visible {
    outline: 2px solid var(--primary-color); outline-offset: 2px; }
  button[disabled] { opacity: .4; cursor: not-allowed; }
  button.armed { background: var(--tone); color: #fff; }
  button.primary { background: var(--tone); color: #111; }
  ha-icon { --mdc-icon-size: 20px; }
  .error { color: var(--secondary-text-color); }
  .compact { display: flex; gap: 12px; align-items: center; cursor: pointer; min-width: 0; }
  .compact .t { min-width: 0; }
  .compact .n { font-size: 12px; color: var(--secondary-text-color); }
  .compact .p, .compact .s { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .compact .p { font-weight: 700; color: var(--primary-text-color); }
  .compact .s { font-size: 12px; color: var(--secondary-text-color); }
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

class HubinetOpsLxcCard extends HTMLElement {
  static getConfigForm() {
    const s = lxc.lxcStrings(document.documentElement.lang || navigator.language);
    return {
      schema: [
        {
          name: "device_id",
          required: true,
          selector: {
            device: { filter: { integration: lxc.DOMAIN, model: "Container" } },
          },
        },
        { name: "autoremove", selector: { boolean: {} } },
        { name: "name", selector: { text: {} } },
        { name: "compact", selector: { boolean: {} } },
      ],
      computeLabel: (schema) => s[`label_${schema.name}`],
    };
  }

  static getStubConfig(hass) {
    return {
      device_id: lxc.firstLxcDevice(hass && hass.entities) || "",
      autoremove: false,
      compact: false,
    };
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Invalid configuration");
    }
    this._config = { autoremove: false, compact: false, ...config };
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
    return this._config && this._config.compact ? 1 : 7;
  }

  getGridOptions() {
    return this._config && this._config.compact
      ? { columns: 6, rows: 1, min_columns: 4 }
      : { columns: 12, min_columns: 6 };
  }

  disconnectedCallback() {
    // A confirmation never survives the card leaving the page.
    this._disarm();
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
        armed.target === lxc.confirmTarget(action, view, this._config.device_id)
    );
  }

  _arm(action, target) {
    clearTimeout(this._armTimer);
    this._armed = { action, target, until: Date.now() + lxc.CONFIRM_MS };
    this._armTimer = setTimeout(() => {
      this._disarm();
      this._render();
    }, lxc.CONFIRM_MS);
  }

  _lang() {
    const hass = this._hass;
    return (hass && hass.locale && hass.locale.language) || (hass && hass.language) || "en";
  }

  _derive() {
    const hass = this._hass;
    const lang = this._lang();
    const packageView = easy.resolveEntities(hass.entities, this._config.device_id).entities
      ? easy.deriveView({
          states: hass.states,
          entities: hass.entities,
          devices: hass.devices,
          config: { device_id: this._config.device_id, autoremove: Boolean(this._config.autoremove) },
          now: Date.now(),
          lang,
        })
      : null;
    return lxc.deriveLxcView({
      states: hass.states,
      entities: hass.entities,
      devices: hass.devices,
      config: this._config,
      lang,
      packageView,
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
        this._history[id] = lxc.historyPoints(result && result[id]);
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
          // The snapshot list may rebuild again once it is no longer in use.
          this._snapshotChosen = false;
          this._signature = undefined;
          this._render();
        }
      });
      this.shadowRoot.addEventListener("pointerdown", (ev) => this._holdStart(ev));
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
    const signature = JSON.stringify([view, this._armed, this._busy, this._history]);
    if (signature === this._signature) {
      return;
    }
    // Rebuilding replaces every element: never under a snapshot list that is
    // being used (it would close), and give focus back to the same control.
    const active = this.shadowRoot.activeElement;
    const focused = active && active.dataset ? active.dataset : null;
    if (focused && focused.role === "snapshot" && !this._snapshotChosen) {
      return;
    }
    this._signature = signature;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card>${
      view.error ? `<div class="error">${esc(view.error)}</div>` : this._config.compact ? this._compact(view) : this._full(view)
    }</ha-card>`;
    // A control disabled while an action runs gets focus back when it ends,
    // unless focus has meanwhile moved elsewhere on the page.
    const page = typeof document !== "undefined" ? document.activeElement : null;
    const unclaimed = !page || page === document.body || page === this;
    const selector = focused
      ? focused.action
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

  _button(action, info, label, extra = "") {
    const s = lxc.lxcStrings(this._lang());
    const armed = this._isArmed(action);
    const tone = TONES[ACTION_TONES[action]];
    return `<button data-action="${action}" style="--tone:${tone}" class="${armed ? "armed" : ""} ${extra}"
      ${info && info.available && !this._busy ? "" : "disabled"}>
      <ha-icon icon="${ICONS[action]}"></ha-icon>${esc(armed ? s.confirm : label)}</button>`;
  }

  _full(view) {
    const s = lxc.lxcStrings(this._lang());
    const stats = view.stats
      .map((stat) => {
        const tone = TONES[stat.tone];
        const history = stat.history ? this._history[stat.history] : null;
        const meter =
          stat.pct !== undefined && stat.pct !== null
            ? `<div class="meter" style="--tone:${tone}"><span style="width:${Math.max(0, Math.min(100, stat.pct))}%"></span></div>`
            : "";
        return `<div class="stat${view.running ? "" : " dim"}"><small>${esc(stat.label)}</small>
          <b>${esc(stat.value)}</b>${stat.detail ? `<em>${esc(stat.detail)}</em>` : ""}
          ${meter}${spark(history, tone, stat.max)}</div>`;
      })
      .join("");
    const pkg = view.package;
    const pkgAction = pkg && pkg.action.kind;
    const pkgButton =
      pkgAction === "easy_update"
        ? this._button("update", { available: true }, s.update, "primary")
        : pkgAction === "scan"
          ? this._button("scan", { available: true }, s.scan)
          : "";
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
      <div class="head" tabindex="0" role="button" data-action="details">
        <div class="shape" style="--tone:${TONES[view.status.tone]}"><ha-icon icon="mdi:cube-outline"></ha-icon></div>
        <div class="titles"><div class="name">${esc(view.name)}</div><div class="sub">${esc(view.uptime)}</div></div>
        <span class="chip" style="--tone:${TONES[view.status.tone]}"><i></i>${esc(view.status.label)}</span>
      </div>
      <div class="stats">${stats}</div>
      ${
        pkg
          ? `<div class="section"><h3>${esc(s.packages)}</h3><div class="pkg">
          <div class="shape" style="--tone:${TONES[pkg.tone] || TONES.grey}"><ha-icon icon="${esc(pkg.icon)}"></ha-icon></div>
          <div class="t"><div class="p">${esc(pkg.primary)}</div><div class="s">${esc(pkg.secondary)}</div></div>
          ${pkgButton}</div></div>`
          : ""
      }
      ${
        snap
          ? `<div class="section"><h3>${esc(s.snapshots)}</h3>
          <div class="row"><select data-role="snapshot" ${snap.available && snap.options.length ? "" : "disabled"}>${options}</select></div>
          <div class="row">${this._button("create", view.actions.create, s.create)}${this._button("restore", view.actions.restore, s.restore)}${this._button("delete", view.actions.delete, s.delete)}</div></div>`
          : ""
      }
      <div class="section"><h3>${esc(s.power)}</h3><div class="power">
        ${this._button("start", view.actions.start, s.start)}${this._button("stop", view.actions.stop, s.stop)}${this._button("restart", view.actions.restart, s.restart)}
      </div></div>`;
  }

  _compact(view) {
    const cpu = view.stats.find((stat) => stat.key === "cpu");
    const ram = view.stats.find((stat) => stat.key === "ram");
    const pkg = view.package;
    return `<div class="compact" data-action="details" tabindex="0" role="button">
      <div class="shape" style="--tone:${TONES[pkg ? pkg.tone : view.status.tone] || TONES.grey}"><ha-icon icon="mdi:cube-outline"></ha-icon></div>
      <div class="t"><div class="n">${esc(view.name)}</div>
        <div class="p">${esc(`${view.status.label} · CPU ${cpu.value} · RAM ${ram.value}`)}</div>
        <div class="s">${esc(pkg ? [pkg.primary, pkg.secondary].filter(Boolean).join(" · ") : view.uptime)}</div></div></div>`;
  }

  _holdStart(ev) {
    if (!ev.target.closest(".head, .compact")) {
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
    const lookup = lxc.resolveLxc(hass.entities, hass.states, deviceId);
    if (lookup.entities) {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          detail: { entityId: lookup.entities.status },
          bubbles: true,
          composed: true,
        })
      );
    }
  }

  async _change(ev) {
    const select = ev.target.closest("select[data-role=snapshot]");
    if (!select || !select.value || !this._view.snapshots) {
      return;
    }
    this._disarm();
    this._snapshotChosen = true;
    await this._call("select", "select_option", {
      entity_id: this._view.snapshots.entity_id,
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
      if (this._held) {
        this._held = false;
      } else {
        this._details();
      }
      return;
    }
    const view = this._view;
    if (lxc.CONFIRM.has(action)) {
      const target = lxc.confirmTarget(action, view, this._config.device_id);
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
      await this._call(lxc.DOMAIN, "easy_update", view.package.action.data);
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

if (!customElements.get(lxc.LXC_CARD_TYPE)) {
  customElements.define(lxc.LXC_CARD_TYPE, HubinetOpsLxcCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === lxc.LXC_CARD_TYPE)) {
  window.customCards.push({
    type: lxc.LXC_CARD_TYPE,
    name: "Hubinet-Ops LXC",
    description:
      "Hubinet-Ops: LXC status, resources, packages, snapshots and power / karta kontenera LXC (Hubinet)",
    preview: true,
    documentationURL: "https://github.com/shockwave9315/hubinet-ops-next#lxc-card",
  });
}
