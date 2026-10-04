// Run with: node --test tests/frontend
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CONFIRM,
  confirmTarget,
  deriveGuestView,
  firstGuestDevice,
  formatDuration,
  historyPoints,
  resolveGuest,
} from "../../custom_components/hubinet_ops/frontend/guest-card-logic.js";

const DEVICE = "device-ct106";

// Deliberately renamed entity IDs: identity comes from registry metadata.
const entry = (entity_id, translation_key, device_id = DEVICE) => ({
  entity_id,
  translation_key,
  device_id,
  platform: "hubinet_ops",
});

const ENTITIES = {
  "sensor.a": entry("sensor.a", "container_status"),
  "sensor.b": entry("sensor.b", "container_cpu"),
  "sensor.c": entry("sensor.c", "container_memory_percentage"),
  "sensor.d": entry("sensor.d", "container_max_memory"),
  "sensor.e": entry("sensor.e", "container_uptime"),
  "sensor.f": entry("sensor.f", "container_disk"),
  "sensor.g": entry("sensor.g", "container_max_disk"),
  "button.h": entry("button.h", "start"),
  "button.i": entry("button.i", "stop"),
  "button.j": entry("button.j", undefined),
  "button.k": entry("button.k", "snapshot_create"),
  "button.l": entry("button.l", "snapshot_restore"),
  "button.m": entry("button.m", "snapshot_delete"),
  "select.n": entry("select.n", "snapshot_to_restore"),
  "sensor.other": entry("sensor.other", "container_status", "other"),
  "sensor.foreign": {
    ...entry("sensor.foreign", "container_cpu"),
    platform: "other_integration",
  },
};

const DEVICES = { [DEVICE]: { name: "nextcloud", name_by_user: "CT106" } };

const baseStates = () => ({
  "sensor.a": { state: "running", attributes: {} },
  "sensor.b": { state: "1.84", attributes: { unit_of_measurement: "%" } },
  "sensor.c": { state: "13.2", attributes: { unit_of_measurement: "%" } },
  "sensor.d": { state: "8", attributes: { unit_of_measurement: "GiB" } },
  "sensor.e": { state: "322000", attributes: { unit_of_measurement: "s" } },
  "sensor.f": { state: "5", attributes: { unit_of_measurement: "GiB" } },
  "sensor.g": { state: "20", attributes: { unit_of_measurement: "GiB" } },
  "button.h": { state: "unknown", attributes: {} },
  "button.i": { state: "unknown", attributes: {} },
  "button.j": { state: "unknown", attributes: { device_class: "restart" } },
  "button.k": { state: "unknown", attributes: {} },
  "button.l": { state: "unknown", attributes: {} },
  "button.m": { state: "unknown", attributes: {} },
  "select.n": {
    state: "unknown",
    attributes: { options: ["hubinet-preupd-20260926", "manual"] },
  },
});

const view = (states = baseStates(), config = { device_id: DEVICE }) =>
  deriveGuestView({
    states,
    entities: ENTITIES,
    devices: DEVICES,
    config,
    lang: "pl",
    packageView: null,
  });

test("roles resolve by device, platform and translation key", () => {
  const { entities } = resolveGuest(ENTITIES, baseStates(), DEVICE);
  assert.equal(entities.status, "sensor.a");
  assert.equal(entities.cpu, "sensor.b");
  assert.equal(entities.restart, "button.j");
  assert.equal(entities.snapshot, "select.n");
  assert.equal(entities.netIn, null);
});

test("restart needs the restart device class", () => {
  const states = baseStates();
  states["button.j"].attributes = {};
  assert.equal(resolveGuest(ENTITIES, states, DEVICE).entities.restart, null);
});

test("a duplicate role fails closed", () => {
  const entities = {
    ...ENTITIES,
    "sensor.dup": entry("sensor.dup", "container_status"),
  };
  assert.deepEqual(resolveGuest(entities, baseStates(), DEVICE), {
    error: "ambiguous",
  });
});

test("a device without a status sensor is not an LXC", () => {
  assert.deepEqual(resolveGuest(ENTITIES, {}, "missing"), { error: "not_found" });
  assert.equal(view(baseStates(), {}).error, "Wybierz LXC Hubinet-Ops");
});

test("the first LXC device is offered as the stub", () => {
  assert.equal(firstGuestDevice(ENTITIES), DEVICE);
  assert.equal(firstGuestDevice({}), null);
});

test("a running LXC shows stats and power actions", () => {
  const v = view();
  assert.equal(v.name, "CT106");
  assert.equal(v.status.tone, "green");
  assert.equal(v.uptime, "działa 3 d 17 h");
  assert.deepEqual(
    v.stats.map((stat) => [stat.key, stat.value]),
    [
      ["cpu", "1,8%"],
      ["ram", "13%"],
      ["disk", "25%"],
    ]
  );
  assert.equal(v.actions.start.available, false);
  assert.equal(v.actions.stop.available, true);
  assert.equal(v.actions.restart.available, true);
  assert.equal(v.actions.create.available, true);
});

test("a stopped LXC can only be started", () => {
  const states = baseStates();
  states["sensor.a"].state = "stopped";
  const v = view(states);
  assert.equal(v.status.tone, "grey");
  assert.equal(v.uptime, "");
  assert.equal(v.stats[0].value, "—");
  assert.equal(v.actions.start.available, true);
  assert.equal(v.actions.stop.available, false);
  assert.equal(v.actions.restart.available, false);
});

test("missing Proxmox data disables every action", () => {
  const states = baseStates();
  states["sensor.a"].state = "unavailable";
  const v = view(states);
  assert.equal(v.status.tone, "orange");
  assert.equal(v.noData, true);
  for (const action of Object.values(v.actions)) {
    assert.equal(action.available, false);
  }
  assert.equal(v.snapshots.available, false);
});

test("restore and delete need a selected snapshot", () => {
  let v = view();
  assert.equal(v.snapshots.selected, null);
  assert.equal(v.actions.restore.available, false);
  assert.equal(v.actions.delete.available, false);
  const states = baseStates();
  states["select.n"].state = "manual";
  states["select.n"].attributes.selected_snapshot = "manual";
  v = view(states);
  assert.equal(v.snapshots.selected, "manual");
  assert.equal(v.actions.restore.available, true);
  assert.equal(v.actions.delete.available, true);
});

test("Test C: the selection is selected_snapshot, never the select state", () => {
  const states = baseStates();
  states["select.n"] = {
    state: "unknown",
    attributes: { options: ["unknown", "manual"], selected_snapshot: null },
  };
  let v = view(states);
  assert.equal(v.snapshots.selected, null);
  assert.equal(v.actions.restore.available, false);
  assert.equal(v.actions.delete.available, false);
  states["select.n"].attributes.selected_snapshot = "unknown";
  v = view(states);
  assert.equal(v.snapshots.selected, "unknown");
  assert.equal(v.actions.restore.available, true);
  assert.equal(v.actions.delete.available, true);
  // A selection that is no longer offered is no selection.
  states["select.n"].attributes.selected_snapshot = "gone";
  assert.equal(view(states).snapshots.selected, null);
});

test("the confirmation target is the exact operation", () => {
  const states = baseStates();
  states["select.n"].attributes.selected_snapshot = "manual";
  const a = view(states);
  assert.equal(confirmTarget("stop", a, DEVICE), JSON.stringify([DEVICE, "button.i"]));
  assert.equal(confirmTarget("start", a, DEVICE), null);
  const restoreA = confirmTarget("restore", a, DEVICE);
  states["select.n"].attributes.selected_snapshot = "hubinet-preupd-20260926";
  const b = view(states);
  assert.notEqual(confirmTarget("restore", b, DEVICE), restoreA);
  assert.notEqual(confirmTarget("delete", b, DEVICE), confirmTarget("restore", b, DEVICE));
  states["select.n"].attributes.selected_snapshot = null;
  assert.equal(confirmTarget("delete", view(states), DEVICE), null);
});

test("an unavailable button is disabled", () => {
  const states = baseStates();
  states["button.k"].state = "unavailable";
  assert.equal(view(states).actions.create.available, false);
});

test("destructive actions need a second tap, Start and Create do not", () => {
  assert.deepEqual([...CONFIRM].sort(), [
    "delete",
    "hibernate",
    "reset",
    "restart",
    "restore",
    "shutdown",
    "stop",
  ]);
  for (const action of ["start", "create"]) {
    assert.equal(CONFIRM.has(action), false);
  }
});

test("durations format in days, hours or minutes", () => {
  const at = (state, unit = "s") => ({
    state: String(state),
    attributes: { unit_of_measurement: unit },
  });
  assert.equal(formatDuration(at(90061), "en"), "1 d 1 h");
  assert.equal(formatDuration(at(2, "h"), "en"), "2 h");
  assert.equal(formatDuration(at(300), "en"), "5 min");
  assert.equal(formatDuration(at("unknown"), "en"), "");
});

test("history keeps finite numbers and downsamples", () => {
  assert.deepEqual(historyPoints([{ s: "1" }, { s: "x" }, { state: "2.5" }]), [
    1, 2.5,
  ]);
  const rows = Array.from({ length: 100 }, (_v, i) => ({ s: String(i) }));
  const points = historyPoints(rows, 10);
  assert.equal(points.length, 10);
  assert.equal(points[0], 0);
  assert.equal(points[9], 99);
  assert.deepEqual(points, [...points].sort((x, y) => x - y));
  assert.equal(new Set(points).size, 10);
});

// VM: the same rules over the QEMU entities of one VM device.
const VM = "device-vm100";
const vmEntry = (entity_id, translation_key) => ({
  entity_id,
  translation_key,
  device_id: VM,
  platform: "hubinet_ops",
});
const VM_ENTITIES = {
  "sensor.v1": vmEntry("sensor.v1", "vm_status"),
  "sensor.v2": vmEntry("sensor.v2", "vm_cpu"),
  "sensor.v3": vmEntry("sensor.v3", "vm_memory_percentage"),
  "sensor.v4": vmEntry("sensor.v4", "vm_uptime"),
  "button.v5": vmEntry("button.v5", "start"),
  "button.v6": vmEntry("button.v6", "stop"),
  "button.v7": vmEntry("button.v7", "shutdown"),
  "button.v8": vmEntry("button.v8", "reset"),
  "button.v9": vmEntry("button.v9", "hibernate"),
  "button.v10": vmEntry("button.v10", undefined),
  "button.v11": vmEntry("button.v11", "snapshot_create"),
  "select.v12": vmEntry("select.v12", "snapshot_to_restore"),
  // An LXC status sensor on another device is never a VM.
  "sensor.ct": entry("sensor.ct", "container_status"),
};
const vmStates = (status = "running") => ({
  "sensor.v1": { state: status, attributes: {} },
  "sensor.v2": { state: "12", attributes: { unit_of_measurement: "%" } },
  "sensor.v3": { state: "47", attributes: { unit_of_measurement: "%" } },
  "sensor.v4": { state: "530000", attributes: { unit_of_measurement: "s" } },
  "button.v5": { state: "unknown", attributes: {} },
  "button.v6": { state: "unknown", attributes: {} },
  "button.v7": { state: "unknown", attributes: {} },
  "button.v8": { state: "unknown", attributes: {} },
  "button.v9": { state: "unknown", attributes: {} },
  "button.v10": { state: "unknown", attributes: { device_class: "restart" } },
  "button.v11": { state: "unknown", attributes: {} },
  "select.v12": { state: "unknown", attributes: { options: ["a", "b"], selected_snapshot: null } },
});
const vmView = (status = "running") =>
  deriveGuestView({
    states: vmStates(status),
    entities: VM_ENTITIES,
    devices: { [VM]: { name: "windows-11", name_by_user: "VM100" } },
    config: { device_id: VM },
    lang: "pl",
    packageView: null,
    kind: "vm",
  });

test("VM roles resolve by VM translation keys only", () => {
  const { entities } = resolveGuest(VM_ENTITIES, vmStates(), VM, "vm");
  assert.equal(entities.status, "sensor.v1");
  assert.equal(entities.shutdown, "button.v7");
  assert.equal(entities.restart, "button.v10");
  // A container is not a VM, and a VM is not a container.
  assert.deepEqual(resolveGuest(ENTITIES, baseStates(), DEVICE, "vm"), { error: "not_found" });
  assert.deepEqual(resolveGuest(VM_ENTITIES, vmStates(), VM, "lxc"), { error: "not_found" });
  assert.equal(firstGuestDevice(VM_ENTITIES, "vm"), VM);
  assert.equal(firstGuestDevice(VM_ENTITIES, "lxc"), DEVICE);
});

test("a running VM offers Shut down, Stop, Restart, Reset, Hibernate", () => {
  const v = vmView();
  assert.equal(v.kind, "vm");
  assert.equal(v.status.label, "Uruchomiona");
  assert.equal(v.package, null);
  const on = Object.entries(v.actions)
    .filter(([, info]) => info.available)
    .map(([key]) => key)
    .sort();
  assert.deepEqual(on, ["create", "hibernate", "reset", "restart", "shutdown", "stop"]);
  assert.deepEqual(
    v.stats.map((stat) => [stat.key, stat.entity]),
    [
      ["cpu", "sensor.v2"],
      ["ram", "sensor.v3"],
    ]
  );
});

test("a stopped VM can only be started; missing data disables all", () => {
  const stopped = vmView("stopped");
  assert.equal(stopped.status.label, "Zatrzymana");
  assert.equal(stopped.actions.start.available, true);
  for (const key of ["shutdown", "stop", "restart", "reset", "hibernate"]) {
    assert.equal(stopped.actions[key].available, false, key);
  }
  const none = vmView("unavailable");
  for (const info of Object.values(none.actions)) {
    assert.equal(info.available, false);
  }
});

test("VM power actions confirm on their own button", () => {
  const v = vmView();
  for (const action of ["shutdown", "reset", "hibernate"]) {
    assert.equal(
      confirmTarget(action, v, VM),
      JSON.stringify([VM, v.actions[action].entity_id])
    );
  }
  assert.equal(confirmTarget("start", v, VM), null);
});

test("LXC views carry no VM-only actions", () => {
  const v = view();
  assert.equal(v.kind, "lxc");
  for (const key of ["shutdown", "reset", "hibernate"]) {
    assert.equal(v.actions[key], undefined);
  }
});

test("a network tile with only the upload sensor is bound to that sensor", () => {
  const entities = { ...ENTITIES, "sensor.out": entry("sensor.out", "container_netout") };
  const states = baseStates();
  states["sensor.out"] = { state: "12", attributes: { unit_of_measurement: "kB/s" } };
  const v = deriveGuestView({
    states,
    entities,
    devices: DEVICES,
    config: { device_id: DEVICE },
    lang: "pl",
    packageView: null,
  });
  const net = v.stats.find((stat) => stat.key === "net");
  assert.equal(net.value, "↓—");
  assert.equal(net.entity, "sensor.out");
  assert.equal(net.history, "sensor.out");
});

for (const kind of ["lxc", "vm"]) {
  test(`${kind}: backend Create running blocks only snapshot actions`, () => {
    const states = baseStates();
    states["select.n"].attributes.selected_snapshot = "manual";
    const entities = Object.fromEntries(Object.entries(ENTITIES).map(([id, item]) => [
      id, { ...item, translation_key: kind === "vm" ? item.translation_key?.replace("container_", "vm_") : item.translation_key },
    ]));
    const derive = () => deriveGuestView({ states, entities, devices: DEVICES, config: { device_id: DEVICE }, lang: "pl", kind });
    const normal = derive();
    for (const value of [false, undefined, "true", true]) {
      states["button.k"].attributes.snapshot_create_running = value;
      const current = derive();
      assert.equal(current.snapshotCreateRunning, value === true);
      for (const action of ["create", "restore", "delete"]) {
        assert.equal(current.actions[action].available, value !== true);
      }
      for (const action of ["start", "stop", "restart", ...(kind === "vm" ? ["shutdown", "reset", "hibernate"] : [])]) {
        assert.deepEqual(current.actions[action], normal.actions[action]);
      }
    }
    // Unavailability must not erase the independently published running fact.
    states["button.k"].state = "unavailable";
    assert.equal(derive().snapshotCreateRunning, true);
    states["sensor.a"].state = "stopped";
    assert.equal(derive().actions.start.available, true);
  });
}
