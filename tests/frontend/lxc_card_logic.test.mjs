// Run with: node --test tests/frontend
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CONFIRM,
  deriveLxcView,
  firstLxcDevice,
  formatDuration,
  historyPoints,
  resolveLxc,
} from "../../custom_components/hubinet_ops/frontend/lxc-card-logic.js";

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
  deriveLxcView({
    states,
    entities: ENTITIES,
    devices: DEVICES,
    config,
    lang: "pl",
    packageView: null,
  });

test("roles resolve by device, platform and translation key", () => {
  const { entities } = resolveLxc(ENTITIES, baseStates(), DEVICE);
  assert.equal(entities.status, "sensor.a");
  assert.equal(entities.cpu, "sensor.b");
  assert.equal(entities.restart, "button.j");
  assert.equal(entities.snapshot, "select.n");
  assert.equal(entities.netIn, null);
});

test("restart needs the restart device class", () => {
  const states = baseStates();
  states["button.j"].attributes = {};
  assert.equal(resolveLxc(ENTITIES, states, DEVICE).entities.restart, null);
});

test("a duplicate role fails closed", () => {
  const entities = {
    ...ENTITIES,
    "sensor.dup": entry("sensor.dup", "container_status"),
  };
  assert.deepEqual(resolveLxc(entities, baseStates(), DEVICE), {
    error: "ambiguous",
  });
});

test("a device without a status sensor is not an LXC", () => {
  assert.deepEqual(resolveLxc(ENTITIES, {}, "missing"), { error: "not_found" });
  assert.equal(view(baseStates(), {}).error, "Wybierz LXC Hubinet-Ops");
});

test("the first LXC device is offered as the stub", () => {
  assert.equal(firstLxcDevice(ENTITIES), DEVICE);
  assert.equal(firstLxcDevice({}), null);
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
  v = view(states);
  assert.equal(v.snapshots.selected, "manual");
  assert.equal(v.actions.restore.available, true);
  assert.equal(v.actions.delete.available, true);
});

test("an unavailable button is disabled", () => {
  const states = baseStates();
  states["button.k"].state = "unavailable";
  assert.equal(view(states).actions.create.available, false);
});

test("destructive actions need a second tap, Start and Create do not", () => {
  assert.deepEqual([...CONFIRM].sort(), ["delete", "restart", "restore", "stop"]);
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
  assert.equal(points[9], 90);
});
