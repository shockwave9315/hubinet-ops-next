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
// Window-level pointer listeners (the card tracks a press until release).
const windowListeners = {};
globalThis.addEventListener = (type, fn) => (windowListeners[type] ||= new Set()).add(fn);
globalThis.removeEventListener = (type, fn) => windowListeners[type]?.delete(fn);
const release = () => [...(windowListeners.pointerup || [])].forEach((fn) => fn());
globalThis.document = { documentElement: { lang: "pl" } };

await import("../../custom_components/hubinet_ops/frontend/hubinet-ops-guest-cards.js");
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

test("a tap acts on the shown scan, not on a newer one not yet shown", async () => {
  const at = (hh) => new Date(2026, 9, 3, hh, 0).toISOString();
  const pkg = (id, key) => entry(id, key);
  const entities = {
    ...ENTITIES,
    "sensor.pending": pkg("sensor.pending", "pending_packages"),
    "sensor.update": pkg("sensor.update", "package_update_status"),
    "button.update": pkg("button.update", "package_update"),
  };
  const hassAt = (hh) => {
    const hass = makeHass("A");
    hass.entities = entities;
    hass.states["sensor.pending"] = {
      state: "7",
      attributes: { scan_status: "success", last_attempt: at(hh) },
    };
    hass.states["sensor.update"] = { state: "never", attributes: {} };
    hass.states["button.update"] = { state: "unknown", attributes: {} };
    return hass;
  };
  const { card, calls, connect } = setup();
  connect(hassAt(4));
  const shown = card.shadowRoot.innerHTML;
  // A pointer goes down on Update; a newer scan arrives before the click.
  const target = { dataset: { action: "update" }, disabled: false };
  target.closest = (selector) => (selector === "[data-action]" ? target : null);
  for (const fn of listeners.get(card.shadowRoot).pointerdown) {
    fn({ target });
  }
  connect(hassAt(6));
  assert.equal(card.shadowRoot.innerHTML, shown, "no rebuild under a press");
  for (const fn of listeners.get(card.shadowRoot).click) {
    await fn({ target });
  }
  release();
  assert.deepEqual(calls, [
    [
      "hubinet_ops",
      "easy_update",
      { device_id: DEVICE, autoremove: false, expected_scan_attempt: at(4) },
    ],
  ]);
  // After the release the card shows the newer scan.
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.notEqual(card.shadowRoot.innerHTML, shown);
});

// Every package state whose shared view action is "details" (see
// easy_update_logic.test.mjs) renders one Details button that only navigates.
const DETAILS_STATES = {
  "failed Update": (st) => {
    st["sensor.update"] = { state: "failed", attributes: { update_status: "failed" } };
  },
  "failed Autoremove": (st) => {
    st["sensor.unused"] = { state: "0", attributes: { autoremove_status: "failed" } };
  },
  "failed Health": (st) => {
    st["sensor.health"] = { state: "failed", attributes: { check_status: "completed" } };
  },
  "updates without the package Update button": (_st, entities) => {
    delete entities["button.update"];
  },
};

for (const [name, mutate] of Object.entries(DETAILS_STATES)) {
  test(`package row offers Details for ${name}, and it only navigates`, async () => {
    const entities = {
      ...ENTITIES,
      "sensor.pending": entry("sensor.pending", "pending_packages"),
      "sensor.update": entry("sensor.update", "package_update_status"),
      "sensor.unused": entry("sensor.unused", "unused_packages"),
      "sensor.health": entry("sensor.health", "package_health"),
      "button.update": entry("button.update", "package_update"),
      "button.scan": entry("button.scan", "package_scan"),
    };
    const hass = makeHass("A");
    Object.assign(hass.states, {
      "sensor.pending": {
        state: "7",
        attributes: { scan_status: "success", last_attempt: new Date(2026, 9, 3, 4).toISOString() },
      },
      "sensor.update": { state: "never", attributes: {} },
      "sensor.unused": { state: "0", attributes: { autoremove_status: "never" } },
      "sensor.health": { state: "unknown", attributes: { check_status: "never" } },
      "button.update": { state: "unknown", attributes: { device_class: "update" } },
      "button.scan": { state: "unknown", attributes: {} },
    });
    mutate(hass.states, entities);
    hass.entities = entities;
    const { card, calls, connect, armed } = setup();
    let opened = 0;
    card._details = () => {
      opened += 1;
    };
    connect(hass);
    const pkg = card.shadowRoot.innerHTML.split('<div class="pkg">')[1].split('<div class="section">')[0];
    const tag = pkg.match(/<button data-action="details"[^>]*>/s);
    assert.ok(tag, "Details button in the package row");
    assert.doesNotMatch(tag[0], /\sdisabled[\s>]/);
    assert.match(pkg, /Szczegóły/);
    assert.doesNotMatch(pkg, /data-action="(update|scan)"/);
    const target = { dataset: { action: "details" }, disabled: false, tagName: "BUTTON" };
    target.closest = () => target;
    for (const fn of listeners.get(card.shadowRoot).click) {
      await fn({ target });
    }
    assert.equal(opened, 1, "the existing details path runs");
    assert.deepEqual(calls, [], "no easy_update, button.press or select_option");
    for (const action of ["stop", "restart", "restore", "delete"]) {
      assert.equal(armed(action), false);
    }
  });
}

// --- Four card types, stat history, VM power and mini cards ---------------

// Mount a card of `type` with `hass`; taps go through the card's listeners.
const mount = (type, hass, config) => {
  const calls = [];
  const opened = [];
  const card = new registry[type]();
  card.setConfig(config);
  card.dispatchEvent = (ev) => opened.push(ev.detail.entityId);
  hass.callService = async (domain, service, data) => {
    calls.push([domain, service, data]);
  };
  hass.callWS = async () => ({});
  card.hass = hass;
  const html = () => card.shadowRoot.innerHTML;
  const press = async (selector) => {
    const tag = html().match(new RegExp(`<button[^>]*${selector}[^>]*>`, "s"));
    assert.ok(tag, `rendered: ${selector}`);
    const dataset = Object.fromEntries(
      [...tag[0].matchAll(/data-(\w+)="([^"]*)"/g)].map((m) => [m[1], m[2]])
    );
    const target = {
      dataset,
      tagName: "BUTTON",
      disabled: /\sdisabled[\s>]/.test(tag[0].replace(/\s+/g, " ")),
      closest: (sel) => (sel === "[data-action]" ? target : null),
    };
    for (const fn of listeners.get(card.shadowRoot).click) {
      await fn({ target });
    }
  };
  const armedAction = () => (html().match(/data-action="(\w+)"[^>]*class="armed/) || [])[1];
  return { card, calls, opened, html, press, armedAction };
};

const VM = "device-vm100";
const vmEntry = (entity_id, translation_key) => ({
  entity_id,
  translation_key,
  device_id: VM,
  platform: "hubinet_ops",
});
const vmHass = (status = "running") => ({
  entities: {
    "sensor.vs": vmEntry("sensor.vs", "vm_status"),
    "sensor.vc": vmEntry("sensor.vc", "vm_cpu"),
    "sensor.vm": vmEntry("sensor.vm", "vm_memory_percentage"),
    "button.vstart": vmEntry("button.vstart", "start"),
    "button.vstop": vmEntry("button.vstop", "stop"),
    "button.vshutdown": vmEntry("button.vshutdown", "shutdown"),
    "button.vreset": vmEntry("button.vreset", "reset"),
    "button.vhibernate": vmEntry("button.vhibernate", "hibernate"),
    "button.vrestart": vmEntry("button.vrestart", undefined),
    "select.vsnap": vmEntry("select.vsnap", "snapshot_to_restore"),
  },
  devices: { [VM]: { name: "windows-11" } },
  locale: { language: "pl" },
  user: { is_admin: true },
  states: {
    "sensor.vs": { state: status, attributes: {} },
    "sensor.vc": { state: "12", attributes: { unit_of_measurement: "%" } },
    "sensor.vm": { state: "47", attributes: { unit_of_measurement: "%" } },
    "button.vstart": { state: "unknown", attributes: {} },
    "button.vstop": { state: "unknown", attributes: {} },
    "button.vshutdown": { state: "unknown", attributes: {} },
    "button.vreset": { state: "unknown", attributes: {} },
    "button.vhibernate": { state: "unknown", attributes: {} },
    "button.vrestart": { state: "unknown", attributes: { device_class: "restart" } },
    "select.vsnap": { state: "unknown", attributes: { options: ["a", "b"], selected_snapshot: null } },
  },
});

test("four guest cards register, each in the card picker", () => {
  const types = [
    "hubinet-ops-lxc-card",
    "hubinet-ops-lxc-mini-card",
    "hubinet-ops-vm-card",
    "hubinet-ops-vm-mini-card",
  ];
  for (const type of types) {
    assert.ok(registry[type], type);
  }
  assert.deepEqual(
    window.customCards.filter((c) => types.includes(c.type)).map((c) => c.name),
    ["Hubinet-Ops LXC", "Hubinet-Ops LXC mini", "Hubinet-Ops VM", "Hubinet-Ops VM mini"]
  );
  const vmForm = registry["hubinet-ops-vm-card"].getConfigForm();
  assert.equal(vmForm.schema[0].selector.device.filter.model, "VM");
  assert.deepEqual(vmForm.schema.map((f) => f.name), ["device_id", "name"]);
  const lxcForm = registry["hubinet-ops-lxc-mini-card"].getConfigForm();
  assert.equal(lxcForm.schema[0].selector.device.filter.model, "Container");
  assert.deepEqual(lxcForm.schema.map((f) => f.name), ["device_id", "autoremove", "name"]);
  assert.deepEqual(registry["hubinet-ops-vm-mini-card"].getStubConfig(vmHass()), {
    device_id: VM,
  });
});

for (const type of ["hubinet-ops-vm-card", "hubinet-ops-vm-mini-card"]) {
  test(`${type}: tapping CPU or RAM opens the native history only`, async () => {
    const { calls, opened, press, armedAction } = mount(type, vmHass(), { device_id: VM });
    await press('data-stat="cpu"');
    await press('data-stat="ram"');
    assert.deepEqual(opened, ["sensor.vc", "sensor.vm"]);
    assert.deepEqual(calls, []);
    assert.equal(armedAction(), undefined);
  });
}

test("LXC card stat tiles open the native history too", async () => {
  const hass = makeHass("A");
  hass.entities = { ...ENTITIES, "sensor.cpu": entry("sensor.cpu", "container_cpu") };
  hass.states["sensor.cpu"] = { state: "1.8", attributes: { unit_of_measurement: "%" } };
  const { calls, opened, press } = mount("hubinet-ops-lxc-card", hass, { device_id: DEVICE });
  await press('data-stat="cpu"');
  assert.deepEqual(opened, ["sensor.cpu"]);
  assert.deepEqual(calls, []);
});

test("VM card: Start runs at once, Shut down and Stop need a second tap", async () => {
  let m = mount("hubinet-ops-vm-card", vmHass("stopped"), { device_id: VM });
  await m.press('data-action="start"');
  assert.deepEqual(m.calls, [["button", "press", { entity_id: "button.vstart" }]]);
  m = mount("hubinet-ops-vm-card", vmHass(), { device_id: VM });
  for (const [action, entity] of [
    ["shutdown", "button.vshutdown"],
    ["stop", "button.vstop"],
    ["restart", "button.vrestart"],
  ]) {
    await m.press(`data-action="${action}"`);
    assert.equal(m.armedAction(), action);
    assert.equal(m.calls.length, 0);
    await m.press(`data-action="${action}"`);
    assert.deepEqual(m.calls.pop(), ["button", "press", { entity_id: entity }]);
  }
});

test("VM card: Reset and Hibernate live under More and confirm", async () => {
  const m = mount("hubinet-ops-vm-card", vmHass(), { device_id: VM });
  assert.doesNotMatch(m.html(), /data-action="reset"/);
  await m.press('data-action="more"');
  assert.match(m.html(), /aria-expanded="true"/);
  for (const [action, entity] of [
    ["reset", "button.vreset"],
    ["hibernate", "button.vhibernate"],
  ]) {
    await m.press(`data-action="${action}"`);
    assert.equal(m.calls.length, 0);
    await m.press(`data-action="${action}"`);
    assert.deepEqual(m.calls.pop(), ["button", "press", { entity_id: entity }]);
  }
  assert.doesNotMatch(m.html(), /Pakiety|data-action="update"/);
});

test("VM mini: Shut down (confirmed) when running, Start when stopped", async () => {
  let m = mount("hubinet-ops-vm-mini-card", vmHass(), { device_id: VM });
  assert.doesNotMatch(m.html(), /data-role="snapshot"|data-action="stop"/);
  await m.press('data-action="shutdown"');
  assert.equal(m.calls.length, 0);
  await m.press('data-action="shutdown"');
  assert.deepEqual(m.calls, [["button", "press", { entity_id: "button.vshutdown" }]]);
  m = mount("hubinet-ops-vm-mini-card", vmHass("stopped"), { device_id: VM });
  assert.doesNotMatch(m.html(), /data-action="shutdown"/);
  await m.press('data-action="start"');
  assert.deepEqual(m.calls, [["button", "press", { entity_id: "button.vstart" }]]);
});

const lxcPackageHass = () => {
  const hass = makeHass("A");
  hass.entities = {
    ...ENTITIES,
    "sensor.pending": entry("sensor.pending", "pending_packages"),
    "sensor.update": entry("sensor.update", "package_update_status"),
    "button.update": entry("button.update", "package_update"),
  };
  Object.assign(hass.states, {
    "sensor.pending": {
      state: "7",
      attributes: { scan_status: "success", last_attempt: new Date(2026, 9, 3, 4).toISOString() },
    },
    "sensor.update": { state: "never", attributes: {} },
    "button.update": { state: "unknown", attributes: { device_class: "update" } },
  });
  return hass;
};

test("LXC mini: status, CPU/RAM and the package action only", async () => {
  const m = mount("hubinet-ops-lxc-mini-card", lxcPackageHass(), { device_id: DEVICE });
  assert.match(m.html(), /7 aktualizacji/);
  assert.doesNotMatch(m.html(), /data-role="snapshot"|data-action="stop"/);
  await m.press('data-action="update"');
  assert.equal(m.calls[0][1], "easy_update");
});

test("a saved LXC card with compact: true renders as LXC mini", () => {
  const legacy = mount("hubinet-ops-lxc-card", lxcPackageHass(), {
    device_id: DEVICE,
    compact: true,
  });
  const mini = mount("hubinet-ops-lxc-mini-card", lxcPackageHass(), { device_id: DEVICE });
  assert.equal(legacy.html(), mini.html());
  assert.deepEqual(legacy.card.getGridOptions(), { columns: 6, min_columns: 6 });
});

test("a network tile with only the upload sensor opens its history", async () => {
  const hass = vmHass();
  hass.entities["sensor.vout"] = vmEntry("sensor.vout", "vm_netout");
  hass.states["sensor.vout"] = { state: "12", attributes: { unit_of_measurement: "kB/s" } };
  const m = mount("hubinet-ops-vm-card", hass, { device_id: VM });
  assert.match(m.html(), /<button class="stat[^"]*" data-action="history" data-stat="net"/);
  await m.press('data-stat="net"');
  assert.deepEqual(m.opened, ["sensor.vout"]);
  assert.deepEqual(m.calls, []);
});

mock.reset();
