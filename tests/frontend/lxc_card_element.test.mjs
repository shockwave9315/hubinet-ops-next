// Run with: node --test tests/frontend
//
// Drives the real LXC card element through a minimal DOM shim: only the
// pieces the card touches (shadow root listeners, innerHTML, custom element
// registry) exist, and taps are dispatched through the card's own listeners.
import assert from "node:assert/strict";
import { mock, test } from "node:test";

const listeners = new WeakMap();

class ShadowRoot {
  constructor() {
    this.innerHTML = "";
    listeners.set(this, {});
  }

  addEventListener(type, fn) {
    (listeners.get(this)[type] ||= []).push(fn);
  }
}

globalThis.HTMLElement = class {
  attachShadow() {
    this.shadowRoot = new ShadowRoot();
    return this.shadowRoot;
  }

  dispatchEvent() {
    return true;
  }
};
const registry = {};
globalThis.customElements = {
  get: (name) => registry[name],
  define: (name, cls) => {
    registry[name] = cls;
  },
};
globalThis.window = globalThis;
globalThis.document = { documentElement: { lang: "pl" } };

await import("../../custom_components/hubinet_ops/frontend/hubinet-ops-lxc-card.js");
const Card = registry["hubinet-ops-lxc-card"];

const DEVICE = "device-ct106";
const entry = (entity_id, translation_key) => ({
  entity_id,
  translation_key,
  device_id: DEVICE,
  platform: "hubinet_ops",
});
const ENTITIES = {
  "sensor.status": entry("sensor.status", "container_status"),
  "button.stop": entry("button.stop", "stop"),
  "button.restart": entry("button.restart", undefined),
  "button.restore": entry("button.restore", "snapshot_restore"),
  "button.delete": entry("button.delete", "snapshot_delete"),
  "select.snap": entry("select.snap", "snapshot_to_restore"),
};

const makeHass = (selected, options = ["A", "B"]) => ({
  entities: ENTITIES,
  devices: { [DEVICE]: { name: "nextcloud" } },
  locale: { language: "pl" },
  user: { is_admin: true },
  states: {
    "sensor.status": { state: "running", attributes: {} },
    "button.stop": { state: "unknown", attributes: {} },
    "button.restart": { state: "unknown", attributes: { device_class: "restart" } },
    "button.restore": { state: "unknown", attributes: {} },
    "button.delete": { state: "unknown", attributes: {} },
    "select.snap": {
      state: selected ?? "unknown",
      attributes: { options, selected_snapshot: selected },
    },
  },
});

const setup = (selected = "A", options) => {
  const calls = [];
  const card = new Card();
  card.setConfig({ device_id: DEVICE });
  const connect = (hass) => {
    hass.callService = async (domain, service, data) => {
      calls.push([domain, service, data]);
    };
    hass.callWS = async () => ({});
    card.hass = hass;
  };
  connect(makeHass(selected, options));
  // A tap goes through the card's own click listener, on the rendered button.
  const tap = async (action) => {
    const html = card.shadowRoot.innerHTML;
    const tag = html.match(new RegExp(`<button data-action="${action}"[^>]*>`, "s"));
    assert.ok(tag, `button ${action} is rendered`);
    const target = {
      dataset: { action },
      disabled: /\sdisabled>$/.test(tag[0].replace(/\s+/g, " ")),
      closest: () => target,
    };
    for (const fn of listeners.get(card.shadowRoot).click) {
      await fn({ target });
    }
  };
  const armed = (action) =>
    new RegExp(`<button data-action="${action}"[^>]*class="armed`).test(
      card.shadowRoot.innerHTML
    );
  return { card, calls, connect, tap, armed };
};

test("a second tap within 4 s confirms Stop", async () => {
  const { calls, tap, armed } = setup();
  await tap("stop");
  assert.equal(calls.length, 0);
  assert.equal(armed("stop"), true);
  await tap("stop");
  assert.deepEqual(calls, [["button", "press", { entity_id: "button.stop" }]]);
  assert.equal(armed("stop"), false);
});

test("Test A: disconnecting cancels an armed Stop", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const { card, calls, connect, tap, armed } = setup();
  await tap("stop");
  assert.equal(armed("stop"), true);
  card.disconnectedCallback();
  t.mock.timers.tick(5000);
  connect(makeHass("A"));
  await tap("stop");
  assert.equal(calls.length, 0, "one tap after reconnect must not press");
  assert.equal(armed("stop"), true, "that tap only arms again");
  await tap("stop");
  assert.deepEqual(calls, [["button", "press", { entity_id: "button.stop" }]]);
});

test("disconnecting cancels even without waiting", async () => {
  const { card, calls, connect, tap, armed } = setup();
  await tap("restart");
  card.disconnectedCallback();
  connect(makeHass("A"));
  assert.equal(armed("restart"), false);
  await tap("restart");
  assert.equal(calls.length, 0);
});

test("confirmation expires after 4 s", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const { calls, tap, armed } = setup();
  await tap("stop");
  t.mock.timers.tick(4001);
  assert.equal(armed("stop"), false);
  await tap("stop");
  assert.equal(calls.length, 0);
});

for (const action of ["restore", "delete"]) {
  test(`Test B: a changed snapshot cancels an armed ${action}`, async () => {
    const { calls, connect, tap, armed } = setup("A");
    await tap(action);
    assert.equal(armed(action), true);
    connect(makeHass("B"));
    assert.equal(armed(action), false, "confirmation for A does not cover B");
    await tap(action);
    assert.equal(calls.length, 0, `${action} of B must not run on one tap`);
    assert.equal(armed(action), true, `${action} of B is only armed`);
    await tap(action);
    assert.deepEqual(calls, [
      ["button", "press", { entity_id: `button.${action}` }],
    ]);
  });
}

test("choosing a snapshot in the card cancels an armed Restore", async () => {
  const { card, calls, tap, armed } = setup("A");
  await tap("restore");
  const select = { value: "B", closest: () => select };
  for (const fn of listeners.get(card.shadowRoot).change) {
    await fn({ target: select });
  }
  assert.deepEqual(calls, [
    ["select", "select_option", { entity_id: "select.snap", option: "B" }],
  ]);
  assert.equal(armed("restore"), false);
  await tap("restore");
  assert.equal(calls.length, 1, "no press after the selection changed");
});

test("Test C: a snapshot named unknown is not selected by state alone", async () => {
  const { tap, calls, connect } = setup(null, ["unknown", "manual"]);
  await tap("restore");
  await tap("restore");
  await tap("delete");
  await tap("delete");
  assert.equal(calls.length, 0, "Restore and Delete stay disabled");
  connect(makeHass("unknown", ["unknown", "manual"]));
  await tap("restore");
  await tap("restore");
  assert.deepEqual(calls, [["button", "press", { entity_id: "button.restore" }]]);
});

test("one confirmation never covers another action", async () => {
  const { calls, tap, armed } = setup();
  await tap("stop");
  await tap("restart");
  assert.equal(calls.length, 0);
  assert.equal(armed("stop"), false);
  assert.equal(armed("restart"), true);
});

test("Enter and Space on the header open details; other keys do nothing", async () => {
  const { card } = setup();
  const opened = [];
  card.dispatchEvent = (ev) => opened.push(ev.detail.entityId);
  card._hass.user = { is_admin: false };
  const head = { dataset: { action: "details" }, disabled: false };
  head.closest = () => head;
  const press = async (key) => {
    let prevented = false;
    for (const fn of listeners.get(card.shadowRoot).keydown) {
      await fn({ key, target: head, preventDefault: () => (prevented = true) });
    }
    return prevented;
  };
  assert.equal(await press("Enter"), true);
  assert.equal(await press(" "), true, "Space must not scroll the page");
  assert.equal(await press("a"), false);
  assert.deepEqual(opened, ["sensor.status", "sensor.status"]);
});

test("RAM shows its meter and its 24 h sparkline together", async () => {
  const card = new Card();
  card.setConfig({ device_id: DEVICE });
  const hass = makeHass("A");
  hass.entities = {
    ...ENTITIES,
    "sensor.mem": entry("sensor.mem", "container_memory_percentage"),
  };
  hass.states["sensor.mem"] = { state: "13", attributes: { unit_of_measurement: "%" } };
  hass.callService = async () => {};
  hass.callWS = async () => ({ "sensor.mem": [{ s: "10" }, { s: "20" }, { s: "13" }] });
  card.hass = hass;
  await new Promise((resolve) => setImmediate(resolve));
  const ram = card.shadowRoot.innerHTML.split("<small>RAM</small>")[1].split('<div class="stat')[0];
  assert.match(ram, /class="meter"/);
  assert.match(ram, /<svg /);
});

mock.reset();
