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
    hass.states["button.update"] = { state: "unknown", attributes: { snapshot_permission: true } };
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
      "button.update": { state: "unknown", attributes: { device_class: "update", snapshot_permission: true } },
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
    "button.update": { state: "unknown", attributes: { device_class: "update", snapshot_permission: true } },
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
  assert.deepEqual(legacy.card.getGridOptions(), { columns: 9, min_columns: 6 });
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

const snapshotHass = (kind, running, language = "pl", status = "running") => {
  const hass = kind === "vm" ? vmHass(status) : makeHass("A");
  const device = kind === "vm" ? VM : DEVICE;
  const item = (entity_id, translation_key) => ({ entity_id, translation_key, device_id: device, platform: "hubinet_ops" });
  hass.locale.language = language;
  hass.states[kind === "vm" ? "sensor.vs" : "sensor.status"].state = status;
  hass.entities = { ...hass.entities };
  delete hass.entities["select.vsnap"];
  for (const [action, key] of [["create", "snapshot_create"], ["restore", "snapshot_restore"], ["delete", "snapshot_delete"]]) {
    const id = `button.${action}`;
    hass.entities[id] = item(id, key);
    hass.states[id] = { state: "unknown", attributes: action === "create" ? { snapshot_create_running: running } : {} };
  }
  hass.entities["select.snap"] = item("select.snap", "snapshot_to_restore");
  hass.states["select.snap"] = { state: "A", attributes: { options: ["A"], selected_snapshot: "A" } };
  if (kind === "lxc") {
    hass.entities["button.start"] = item("button.start", "start");
    hass.states["button.start"] = { state: "unknown", attributes: {} };
  }
  return hass;
};
const snapshotButton = (html, action) => html.match(new RegExp(`<button data-action="${action}"[^>]*>.*?</button>`, "s"))?.[0];

for (const kind of ["lxc", "vm"]) {
  for (const lang of ["pl", "en"]) {
    test(`${kind}/${lang}: remount restores backend spinner and snapshot blocking, with power intact`, async () => {
      const device = kind === "vm" ? VM : DEVICE;
      const hass = snapshotHass(kind, true, lang);
      let m = mount(`hubinet-ops-${kind}-card`, hass, { device_id: device });
      assert.equal(Boolean(m.card._busy), false, "no local request owns running");
      const creating = snapshotButton(m.html(), "create");
      assert.match(creating, /ha-circular-progress[^>]*indeterminate/);
      assert.match(creating, lang === "pl" ? /Tworzenie\.\.\./ : /Creating\.\.\./);
      for (const action of ["create", "restore", "delete"]) {
        assert.match(snapshotButton(m.html(), action), /\sdisabled[\s>]/);
        await m.press(`data-action="${action}"`);
        await m.press(`data-action="${action}"`);
      }
      assert.deepEqual(m.calls, []);
      for (const action of ["stop", "restart"]) {
        assert.doesNotMatch(snapshotButton(m.html(), action), /\sdisabled[\s>]/);
      }
      await m.press('data-action="stop"');
      await m.press('data-action="stop"');
      assert.equal(m.calls.length, 1, "power retains normal confirmation and submission");
      assert.match(snapshotButton(m.html(), "create"), /ha-circular-progress/);
      m.card.disconnectedCallback();
      m = mount(`hubinet-ops-${kind}-card`, snapshotHass(kind, true, lang), { device_id: device });
      assert.match(snapshotButton(m.html(), "create"), /ha-circular-progress/);
      const finished = snapshotHass(kind, false, lang);
      finished.callWS = async () => ({});
      m.card.hass = finished;
      assert.doesNotMatch(snapshotButton(m.html(), "create"), /ha-circular-progress|\sdisabled[\s>]/);
      assert.match(snapshotButton(m.html(), "create"), lang === "pl" ? /Utwórz/ : /Create/);
      const stopped = mount(`hubinet-ops-${kind}-card`, snapshotHass(kind, true, lang, "stopped"), { device_id: device });
      assert.doesNotMatch(snapshotButton(stopped.html(), "start"), /\sdisabled[\s>]/);
    });
  }
  test(`${kind}: accepted Create request clears local busy while backend running remains`, async () => {
    const device = kind === "vm" ? VM : DEVICE;
    const hass = snapshotHass(kind, false);
    const m = mount(`hubinet-ops-${kind}-card`, hass, { device_id: device });
    const nativeService = hass.callService;
    hass.callService = async (...args) => {
      await nativeService(...args);
      hass.states["button.create"].attributes.snapshot_create_running = true;
      m.card.hass = hass;
    };
    await m.press('data-action="create"');
    assert.equal(m.card._busy, false);
    assert.equal(m.calls.length, 1);
    assert.match(snapshotButton(m.html(), "create"), /Tworzenie\.\.\./);
    assert.match(snapshotButton(m.html(), "create"), /ha-circular-progress/);
    assert.doesNotMatch(snapshotButton(m.html(), "restart"), /\sdisabled[\s>]/);
  });
}

// --- Checkpoint C: Update without a snapshot in the full LXC card -----------

const SCAN_1 = new Date(2026, 9, 3, 4).toISOString();
const SCAN_2 = new Date(2026, 9, 3, 6).toISOString();
const SKIP = 'data-action="skip_update"';
const pkgHass = ({
  attempt = SCAN_1,
  pending = "7",
  scanStatus = "success",
  permission = true,
  creating = false,
  lang = "pl",
} = {}) => {
  const hass = lxcPackageHass();
  hass.locale = { language: lang };
  hass.entities = {
    ...hass.entities,
    "button.start": entry("button.start", "start"),
    "button.create": entry("button.create", "snapshot_create"),
  };
  hass.states["sensor.pending"] = {
    state: pending,
    attributes: { scan_status: scanStatus, last_attempt: attempt },
  };
  hass.states["button.update"].attributes.snapshot_permission = permission;
  hass.states["button.start"] = { state: "unknown", attributes: {} };
  hass.states["button.create"] = {
    state: "unknown",
    attributes: { snapshot_create_running: creating },
  };
  return hass;
};
const skipPayload = (attempt = SCAN_1, device = DEVICE) => [
  "hubinet_ops",
  "easy_update",
  { device_id: device, autoremove: false, skip_snapshot: true, expected_scan_attempt: attempt },
];
// Give the mounted card new backend state, as Home Assistant does.
const update = (m, hass) => {
  hass.callService = m.card._hass.callService;
  hass.callWS = async () => ({});
  m.card.hass = hass;
};
const buttonTag = (html, action) =>
  html.match(new RegExp(`<button data-action="${action}"[^>]*>`, "s"))?.[0];

for (const lang of ["pl", "en"]) {
  test(`full LXC/${lang}: skip arms on the first tap and submits on the second`, async () => {
    const m = mount("hubinet-ops-lxc-card", pkgHass({ lang }), { device_id: DEVICE });
    const row = m.html().split('<div class="pkg">')[1].split('<div class="section">')[0];
    assert.match(row, /<div class="acts two">/);
    assert.ok(row.indexOf('data-action="update"') < row.indexOf(SKIP), "Update, then skip");
    assert.match(snapshotButton(m.html(), "skip_update"), lang === "pl" ? /Bez migawki/ : /No snapshot/);
    await m.press(SKIP);
    assert.deepEqual(m.calls, [], "the first tap only arms");
    assert.equal(m.armedAction(), "skip_update");
    assert.match(
      snapshotButton(m.html(), "skip_update"),
      lang === "pl" ? /Na pewno\? Dotknij ponownie/ : /Sure\? Tap again/
    );
    await m.press(SKIP);
    assert.deepEqual(m.calls, [skipPayload()]);
    assert.equal(m.armedAction(), undefined, "submitting clears the confirmation");
    // A third tap starts over: it only arms again.
    await m.press(SKIP);
    assert.equal(m.calls.length, 1);
  });
}

test("normal Update stays one tap, snapshot-required, with the saved YOLO choice", async () => {
  const config = { device_id: DEVICE, autoremove: true };
  const m = mount("hubinet-ops-lxc-card", pkgHass(), config);
  const events = [];
  m.card.dispatchEvent = (ev) => events.push(ev.type);
  const saved = JSON.stringify(m.card._config);
  await m.press(SKIP);
  await m.press(SKIP);
  await m.press('data-action="update"');
  assert.deepEqual(m.calls, [
    skipPayload(),
    [
      "hubinet_ops",
      "easy_update",
      { device_id: DEVICE, autoremove: true, expected_scan_attempt: SCAN_1 },
    ],
  ]);
  // Skip is one request: no saved setting, no YOLO change, no config event.
  assert.equal(JSON.stringify(m.card._config), saved);
  assert.deepEqual(config, { device_id: DEVICE, autoremove: true });
  assert.deepEqual(events, []);
  assert.equal(m.calls.every(([domain, service]) => `${domain}.${service}` === "hubinet_ops.easy_update"), true);
});

test("without snapshot permission normal Update is unavailable; skip still works", async () => {
  const m = mount("hubinet-ops-lxc-card", pkgHass({ permission: false }), { device_id: DEVICE });
  const row = m.html().split('<div class="pkg">')[1].split('<div class="section">')[0];
  assert.doesNotMatch(row, /data-action="update"/);
  assert.match(row, /Aktualizacja niedostępna dla tego LXC/);
  assert.match(row, /data-action="details"/);
  let opened = 0;
  m.card._details = () => {
    opened += 1;
  };
  await m.press('data-key="package-details"');
  assert.equal(opened, 1);
  assert.deepEqual(m.calls, [], "Details only navigates");
  await m.press(SKIP);
  await m.press(SKIP);
  assert.deepEqual(m.calls, [skipPayload()]);
});

const NO_SKIP = {
  "no pending packages": ["hubinet-ops-lxc-card", () => pkgHass({ pending: "0" }), {}],
  "a failed scan": ["hubinet-ops-lxc-card", () => pkgHass({ scanStatus: "failed" }), {}],
  "no scan yet": ["hubinet-ops-lxc-card", () => pkgHass({ scanStatus: "never", pending: "unknown" }), {}],
  "a running scan": ["hubinet-ops-lxc-card", () => pkgHass({ scanStatus: "running" }), {}],
  "LXC mini": ["hubinet-ops-lxc-mini-card", () => pkgHass(), {}],
  "LXC mini without snapshot permission": ["hubinet-ops-lxc-mini-card", () => pkgHass({ permission: false }), {}],
  "a legacy compact card": ["hubinet-ops-lxc-card", () => pkgHass(), { compact: true }],
};
for (const [name, [type, hass, extra]] of Object.entries(NO_SKIP)) {
  test(`skip is not rendered for ${name}`, () => {
    const m = mount(type, hass(), { device_id: DEVICE, ...extra });
    assert.doesNotMatch(m.html(), /skip_update|Bez migawki/);
  });
}

test("skip is not rendered in the VM card or VM mini", () => {
  for (const type of ["hubinet-ops-vm-card", "hubinet-ops-vm-mini-card"]) {
    const m = mount(type, vmHass(), { device_id: VM });
    assert.doesNotMatch(m.html(), /skip_update|Bez migawki|data-action="update"/);
  }
});

test("LXC mini keeps its one normal Update action", async () => {
  const m = mount("hubinet-ops-lxc-mini-card", pkgHass(), { device_id: DEVICE });
  await m.press('data-action="update"');
  assert.deepEqual(m.calls, [
    ["hubinet_ops", "easy_update", { device_id: DEVICE, autoremove: false, expected_scan_attempt: SCAN_1 }],
  ]);
});

test("a new scan attempt cancels an armed skip; the old arm never runs for it", async () => {
  const m = mount("hubinet-ops-lxc-card", pkgHass(), { device_id: DEVICE });
  await m.press(SKIP);
  assert.equal(m.armedAction(), "skip_update");
  update(m, pkgHass({ attempt: SCAN_2 }));
  assert.equal(m.armedAction(), undefined, "the arm belonged to the older scan");
  await m.press(SKIP);
  assert.deepEqual(m.calls, [], "one tap on the new scan only arms");
  assert.equal(m.armedAction(), "skip_update");
  await m.press(SKIP);
  assert.deepEqual(m.calls, [skipPayload(SCAN_2)]);
});

test("a tap during a press acts on the shown scan and a newer scan disarms it", async () => {
  const m = mount("hubinet-ops-lxc-card", pkgHass(), { device_id: DEVICE });
  await m.press(SKIP);
  // A pointer goes down; the newer scan arrives before the click.
  for (const fn of listeners.get(m.card.shadowRoot).pointerdown) {
    fn({ target: { closest: () => null } });
  }
  const shown = m.html();
  update(m, pkgHass({ attempt: SCAN_2 }));
  assert.equal(m.html(), shown, "no rebuild under a press");
  await m.press(SKIP);
  assert.deepEqual(m.calls, [], "the old arm is gone; this tap cannot submit");
  release();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(m.armedAction(), undefined, "the re-arm for the old scan is cancelled too");
  await m.press(SKIP);
  await m.press(SKIP);
  assert.deepEqual(m.calls, [skipPayload(SCAN_2)]);
});

test("a changed pending count or lost readiness cancels an armed skip", async () => {
  const m = mount("hubinet-ops-lxc-card", pkgHass(), { device_id: DEVICE });
  await m.press(SKIP);
  update(m, pkgHass({ pending: "8" }));
  assert.equal(m.armedAction(), undefined, "pending changed under the same attempt");
  await m.press(SKIP);
  assert.equal(m.armedAction(), "skip_update");
  update(m, pkgHass({ pending: "8", scanStatus: "running" }));
  assert.doesNotMatch(m.html(), /skip_update/);
  update(m, pkgHass({ pending: "8" }));
  assert.equal(m.armedAction(), undefined, "readiness was lost in between");
  await m.press(SKIP);
  assert.deepEqual(m.calls, [], "only armed again");
});

test("another device, expiry and disconnect each cancel an armed skip", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const m = mount("hubinet-ops-lxc-card", pkgHass(), { device_id: DEVICE });
  // Expiry of the existing confirmation timeout.
  await m.press(SKIP);
  t.mock.timers.tick(4001);
  assert.equal(m.armedAction(), undefined);
  await m.press(SKIP);
  assert.deepEqual(m.calls, []);
  // Disconnect and remount.
  assert.equal(m.armedAction(), "skip_update");
  m.card.disconnectedCallback();
  update(m, pkgHass());
  assert.equal(m.armedAction(), undefined);
  await m.press(SKIP);
  assert.deepEqual(m.calls, []);
  // Another device.
  assert.equal(m.armedAction(), "skip_update");
  const OTHER = "device-ct107";
  const other = pkgHass();
  other.entities = Object.fromEntries(
    Object.entries(other.entities).map(([id, item]) => [id, { ...item, device_id: OTHER }])
  );
  m.card.setConfig({ device_id: OTHER });
  update(m, other);
  assert.equal(m.armedAction(), undefined);
  await m.press(SKIP);
  assert.deepEqual(m.calls, [], "the first tap on the other device only arms");
  await m.press(SKIP);
  assert.deepEqual(m.calls, [skipPayload(SCAN_1, OTHER)]);
});

test("a request in flight disables skip", async () => {
  const hass = pkgHass();
  const m = mount("hubinet-ops-lxc-card", hass, { device_id: DEVICE });
  let finish;
  hass.callService = (domain, service, data) => {
    m.calls.push([domain, service, data]);
    return new Promise((resolve) => {
      finish = resolve;
    });
  };
  await m.press(SKIP);
  const running = m.press('data-action="create"');
  await Promise.resolve();
  assert.equal(m.card._busy, true);
  assert.match(buttonTag(m.html(), "skip_update"), /\sdisabled>$/);
  await m.press(SKIP);
  await m.press(SKIP);
  assert.equal(m.calls.length, 1, "only the Create request");
  // Even a tap that reaches the handler from a stale enabled button is refused.
  const stale = { dataset: { action: "skip_update" }, disabled: false, closest: () => stale };
  for (let tap = 0; tap < 2; tap += 1) {
    for (const fn of listeners.get(m.card.shadowRoot).click) {
      await fn({ target: stale });
    }
  }
  assert.equal(m.calls.length, 1);
  assert.equal(m.armedAction(), undefined);
  finish();
  await running;
  assert.equal(m.card._busy, false);
  assert.doesNotMatch(buttonTag(m.html(), "skip_update"), /\sdisabled>$/);
});

test("a running snapshot Create disables skip and cancels its arm; Update and power stay", async () => {
  const m = mount("hubinet-ops-lxc-card", pkgHass(), { device_id: DEVICE });
  await m.press(SKIP);
  assert.equal(m.armedAction(), "skip_update");
  update(m, pkgHass({ creating: true }));
  assert.equal(Boolean(m.card._busy), false, "running comes from the backend, not from _busy");
  assert.equal(m.armedAction(), undefined);
  assert.match(buttonTag(m.html(), "skip_update"), /\sdisabled>$/);
  await m.press(SKIP);
  await m.press(SKIP);
  assert.deepEqual(m.calls, []);
  // B: spinner and label on Create, Restore/Delete blocked, power independent.
  assert.match(snapshotButton(m.html(), "create"), /ha-circular-progress[^>]*indeterminate/);
  assert.match(snapshotButton(m.html(), "create"), /Tworzenie\.\.\./);
  for (const action of ["create", "restore", "delete"]) {
    assert.match(buttonTag(m.html(), action), /\sdisabled>$/);
  }
  for (const action of ["stop", "restart", "update"]) {
    assert.doesNotMatch(buttonTag(m.html(), action), /\sdisabled>$/);
  }
  // A remount still reads running from the entity state.
  m.card.disconnectedCallback();
  const again = mount("hubinet-ops-lxc-card", pkgHass({ creating: true }), { device_id: DEVICE });
  assert.match(snapshotButton(again.html(), "create"), /ha-circular-progress/);
  assert.match(buttonTag(again.html(), "skip_update"), /\sdisabled>$/);
  update(again, pkgHass());
  assert.doesNotMatch(buttonTag(again.html(), "skip_update"), /\sdisabled>$/);
  assert.doesNotMatch(snapshotButton(again.html(), "create"), /ha-circular-progress/);
});

// --- Checkpoint D: mini width and dual RAM ----------------------------------

const vmRamHass = (guest, host = "99", lang = "pl") => {
  const hass = vmHass();
  hass.locale = { language: lang };
  hass.entities = {
    ...hass.entities,
    "sensor.vmax": vmEntry("sensor.vmax", "vm_max_memory"),
    "sensor.vg": vmEntry("sensor.vg", "vm_guest_memory_percentage"),
  };
  hass.states["sensor.vm"] = { state: host, attributes: { unit_of_measurement: "%" } };
  hass.states["sensor.vmax"] = { state: "6", attributes: { unit_of_measurement: "GiB" } };
  hass.states["sensor.vg"] = { state: guest, attributes: { unit_of_measurement: "%" } };
  return hass;
};
const ramTile = (html) => html.split('data-stat="ram"')[1].split("</button>")[0];
const count = (text, pattern) => (text.match(pattern) || []).length;

test("mini cards default to 9 columns; full cards keep 12", () => {
  const options = (type, config) => new registry[type]().getGridOptions.call(
    Object.assign(new registry[type](), { _config: config })
  );
  for (const type of ["hubinet-ops-lxc-mini-card", "hubinet-ops-vm-mini-card"]) {
    assert.deepEqual(options(type, {}), { columns: 9, min_columns: 6 });
  }
  assert.deepEqual(options("hubinet-ops-lxc-card", { compact: true }), { columns: 9, min_columns: 6 });
  for (const type of ["hubinet-ops-lxc-card", "hubinet-ops-vm-card"]) {
    assert.deepEqual(options(type, {}), { columns: 12, min_columns: 6 });
  }
});

test("a saved grid_options wins over the card default and is never rewritten", () => {
  const config = { device_id: VM, grid_options: { columns: 6, rows: 3 } };
  const m = mount("hubinet-ops-vm-mini-card", vmHass(), config);
  // Home Assistant merges the saved options over the element's defaults.
  assert.deepEqual(
    { ...m.card.getGridOptions(), ...m.card._config.grid_options },
    { columns: 6, min_columns: 6, rows: 3 }
  );
  assert.deepEqual(m.card.getGridOptions(), { columns: 9, min_columns: 6 });
  assert.deepEqual(m.card._config.grid_options, { columns: 6, rows: 3 });
  assert.deepEqual(config, { device_id: VM, grid_options: { columns: 6, rows: 3 } });
});

test("full VM: dual RAM shows Guest and Host with amounts and two thin bars", async () => {
  const m = mount("hubinet-ops-vm-card", vmRamHass("94.33"), { device_id: VM });
  const tile = ramTile(m.html());
  const [guest, host] = tile.split('data-line="host"');
  assert.match(guest, /data-line="guest"><span>Gość<\/span>\s*<b>94%<\/b><em>5,66 GiB<\/em>/);
  assert.match(host, /<span>Host<\/span>\s*<b>99%<\/b><em>5,94 GiB<\/em>/);
  assert.equal(count(tile, /class="meter"/g), 2, "one thin bar per line, no third bar");
  assert.equal(count(tile, /class="line"/g), 2);
  // The tile is still the host sensor's history; nothing is called.
  await m.press('data-stat="ram"');
  assert.deepEqual(m.opened, ["sensor.vm"]);
  assert.deepEqual(m.calls, []);
  assert.match(m.html(), /aria-label="RAM: 99%, pokaż historię"/);
});

test("VM mini: dual RAM shows both percentages with two thin bars and no amounts", async () => {
  const m = mount("hubinet-ops-vm-mini-card", vmRamHass("94.33", "99", "en"), { device_id: VM });
  const tile = ramTile(m.html());
  assert.match(tile, /<span>Guest<\/span>\s*<b>94%<\/b>/);
  assert.match(tile, /<span>Host<\/span>\s*<b>99%<\/b>/);
  assert.doesNotMatch(tile, /GiB|<em>/);
  assert.equal(count(tile, /class="meter"/g), 2);
  await m.press('data-stat="ram"');
  assert.deepEqual(m.opened, ["sensor.vm"]);
});

for (const guest of ["unknown", "unavailable", "-3"]) {
  test(`guest ${guest}: the RAM tile stays host-only`, () => {
    for (const type of ["hubinet-ops-vm-card", "hubinet-ops-vm-mini-card"]) {
      const tile = ramTile(mount(type, vmRamHass(guest), { device_id: VM }).html());
      assert.doesNotMatch(tile, /data-line|Gość|class="lines"/);
      assert.match(tile, /<b>99%<\/b><em>5,9 GiB z 6 GiB<\/em>/);
      assert.equal(count(tile, /class="meter"/g), 1);
    }
  });
}

test("host above 100% is shown as it is; only the bar is clamped", () => {
  const tile = ramTile(mount("hubinet-ops-vm-card", vmRamHass("94.33", "104.2"), { device_id: VM }).html());
  assert.match(tile, /<b>104%<\/b><em>6,25 GiB<\/em>/);
  assert.deepEqual(
    [...tile.matchAll(/<span style="width:([\d.]+)%">/g)].map((match) => match[1]),
    ["94.33", "100"]
  );
});

test("a VM without guest sensors and an LXC render the RAM tile as before", () => {
  const vm = ramTile(mount("hubinet-ops-vm-card", vmHass(), { device_id: VM }).html());
  assert.doesNotMatch(vm, /data-line/);
  assert.match(vm, /<b>47%<\/b>/);
  const hass = makeHass("A");
  hass.entities = { ...ENTITIES, "sensor.mem": entry("sensor.mem", "container_memory_percentage") };
  hass.states["sensor.mem"] = { state: "13", attributes: { unit_of_measurement: "%" } };
  const lxc = ramTile(mount("hubinet-ops-lxc-card", hass, { device_id: DEVICE }).html());
  assert.doesNotMatch(lxc, /data-line/);
  assert.match(lxc, /<b>13%<\/b>/);
});
