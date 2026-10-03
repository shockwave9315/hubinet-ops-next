// Hubinet-Ops dashboard cards, shipped and loaded by the integration itself.
//
// Presentation and invocation only: the card shows existing Home Assistant
// facts and calls existing actions. A user click is the only authorization;
// rendering never mutates anything.

// Home Assistant imports this module in parallel with its own app bundle, which
// may install a scoped custom-element registry polyfill. That polyfill replaces
// window.customElements, and elements defined before it are invisible to Home
// Assistant ("Custom element doesn't exist", endless spinner in the card
// picker). So nothing is declared or defined until the app has defined its own
// <home-assistant> element; the polyfill is installed by then. The guest cards
// module is imported below, after this point. A fallback keeps the cards usable
// on a page that never defines <home-assistant>.
await Promise.race([
  customElements.whenDefined("home-assistant"),
  new Promise((resolve) => setTimeout(resolve, 10000)),
]);

// Load the logic module with this module's own release query so a cached
// copy from an older release is never combined with a newer card.
const logic = await import(
  new URL(`./easy-update-logic.js${new URL(import.meta.url).search}`, import.meta.url)
    .href
);
const {
  CARD_TYPE,
  DOMAIN,
  deriveView,
  firstEligibleDevice,
  language,
  resolveEntities,
  strings,
} = logic;

const HOLD_MS = 500;

const formLanguage = () =>
  language(
    (typeof document !== "undefined" && document.documentElement.lang) ||
      (typeof navigator !== "undefined" && navigator.language)
  );

const STYLE = `
  :host { display: block; }
  ha-card { height: 100%; box-sizing: border-box; padding: 12px;
    cursor: pointer; outline: none; }
  ha-card:focus-visible { box-shadow: 0 0 0 2px var(--primary-color); }
  .row { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .shape { flex: none; width: 40px; height: 40px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: var(--tone);
    background: color-mix(in srgb, var(--tone) 20%, transparent); }
  .text { min-width: 0; display: flex; flex-direction: column; }
  .name, .primary, .secondary { white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }
  .name { font-size: 12px; color: var(--secondary-text-color); }
  .primary { font-size: 14px; font-weight: 600;
    color: var(--primary-text-color); }
  .secondary { font-size: 12px; color: var(--secondary-text-color); }
  .busy ha-icon { animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
`;

const TONES = {
  amber: "var(--amber-color, #ffc107)",
  green: "var(--green-color, #4caf50)",
  blue: "var(--blue-color, #2196f3)",
  red: "var(--red-color, #f44336)",
  orange: "var(--orange-color, #ff9800)",
  grey: "var(--grey-color, #9e9e9e)",
  error: "var(--red-color, #f44336)",
};

class HubinetOpsEasyUpdateCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        {
          name: "device_id",
          required: true,
          selector: {
            device: {
              filter: { integration: DOMAIN, model: "Container" },
              // Only package LXCs have the package Update button, the
              // integration's only button with the native update class.
              entity: {
                integration: DOMAIN,
                domain: "button",
                device_class: "update",
              },
            },
          },
        },
        { name: "autoremove", selector: { boolean: {} } },
        { name: "name", selector: { text: {} } },
      ],
      computeLabel: (schema) => strings(formLanguage())[`label_${schema.name}`],
      computeHelper: (schema) =>
        schema.name === "autoremove"
          ? strings(formLanguage()).helper_autoremove
          : undefined,
    };
  }

  static getStubConfig(hass) {
    return {
      device_id: firstEligibleDevice(hass && hass.entities) || "",
      autoremove: false,
    };
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Invalid configuration");
    }
    this._config = { autoremove: false, ...config };
    this._signature = undefined;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 1;
  }

  getGridOptions() {
    return { columns: 6, rows: 1, min_columns: 4, min_rows: 1 };
  }

  _lang() {
    const hass = this._hass;
    return language((hass && hass.locale && hass.locale.language) || (hass && hass.language));
  }

  _build() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${STYLE}</style>
      <ha-card tabindex="0" role="button">
        <div class="row">
          <div class="shape"><ha-icon></ha-icon></div>
          <div class="text">
            <span class="name"></span>
            <span class="primary"></span>
            <span class="secondary"></span>
          </div>
        </div>
      </ha-card>`;
    this._card = root.querySelector("ha-card");
    this._icon = root.querySelector("ha-icon");
    this._shape = root.querySelector(".shape");
    this._name = root.querySelector(".name");
    this._primary = root.querySelector(".primary");
    this._secondary = root.querySelector(".secondary");
    this._card.addEventListener("pointerdown", () => this._startHold());
    for (const type of ["pointerup", "pointerleave", "pointercancel"]) {
      this._card.addEventListener(type, () => this._cancelHold());
    }
    this._card.addEventListener("click", () => this._tap());
    this._card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        this._tap();
      }
    });
  }

  _render() {
    if (!this._config || !this._hass) {
      return;
    }
    if (!this._card) {
      this._build();
    }
    const hass = this._hass;
    const lang = this._lang();
    const current = deriveView({
      states: hass.states,
      entities: hass.entities,
      devices: hass.devices,
      config: this._config,
      now: Date.now(),
      lang,
    });
    this._view = current;
    const shown = this._busy
      ? { ...current, icon: "mdi:loading", secondary: strings(lang).starting }
      : current;
    const signature = JSON.stringify([shown, this._busy]);
    if (signature === this._signature) {
      return;
    }
    this._signature = signature;
    this._shape.style.setProperty("--tone", TONES[shown.tone] || TONES.grey);
    this._shape.classList.toggle("busy", Boolean(this._busy));
    this._icon.setAttribute("icon", shown.icon);
    this._name.textContent = shown.name;
    this._primary.textContent = shown.primary;
    this._secondary.textContent = shown.secondary;
    this._secondary.hidden = !shown.secondary;
  }

  _startHold() {
    this._cancelHold();
    this._held = false;
    this._holdTimer = setTimeout(() => {
      this._held = true;
      this._details();
    }, HOLD_MS);
  }

  _cancelHold() {
    clearTimeout(this._holdTimer);
    this._holdTimer = undefined;
  }

  async _tap() {
    if (this._held) {
      this._held = false;
      return;
    }
    const action = this._view && this._view.action;
    if (!action || this._busy) {
      return;
    }
    if (action.kind === "details") {
      this._details();
      return;
    }
    if (action.kind !== "easy_update" && action.kind !== "scan") {
      return;
    }
    this._busy = true;
    this._render();
    try {
      if (action.kind === "easy_update") {
        await this._hass.callService(DOMAIN, "easy_update", action.data);
      } else {
        await this._hass.callService("button", "press", {
          entity_id: action.entity_id,
        });
      }
    } catch (_err) {
      // Home Assistant already shows the translated error as a toast.
    } finally {
      this._busy = false;
      this._signature = undefined;
      this._render();
    }
  }

  _details() {
    const hass = this._hass;
    const deviceId = this._config && this._config.device_id;
    if (!hass || !deviceId) {
      return;
    }
    if (hass.user && hass.user.is_admin) {
      window.history.pushState(null, "", `/config/devices/device/${deviceId}`);
      window.dispatchEvent(
        new CustomEvent("location-changed", { detail: { replace: false } })
      );
      return;
    }
    const lookup = resolveEntities(hass.entities, deviceId);
    if (lookup.entities) {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          detail: { entityId: lookup.entities.pending },
          bubbles: true,
          composed: true,
        })
      );
    }
  }
}

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, HubinetOpsEasyUpdateCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === CARD_TYPE)) {
  window.customCards.push({
    type: CARD_TYPE,
    name: "Hubinet-Ops Easy Update",
    description:
      "Hubinet-Ops: one-click LXC package update / aktualizacja pakietów LXC jednym kliknięciem (Hubinet)",
    preview: true,
    documentationURL:
      "https://github.com/shockwave9315/hubinet-ops-next#easy-update-card",
  });
}

// The LXC and VM cards live in their own module, loaded with this release's query.
await import(
  new URL(`./hubinet-ops-guest-cards.js${new URL(import.meta.url).search}`, import.meta.url)
    .href
);
