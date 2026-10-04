// Run with: node --test tests/frontend
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  deriveView,
  firstEligibleDevice,
  formatTime,
  formatUpdates,
  resolveEntities,
} from "../../custom_components/hubinet_ops/frontend/easy-update-logic.js";

const DEVICE = "device-ct106";
const NOW = new Date(2026, 9, 3, 12, 0).getTime();
const SCAN_AT = new Date(2026, 9, 3, 4, 0).toISOString();

// Deliberately renamed entity IDs: identity comes from registry metadata.
const entry = (entity_id, translation_key, device_id = DEVICE) => ({
  entity_id,
  translation_key,
  device_id,
  platform: "hubinet_ops",
});

const ENTITIES = {
  "sensor.renamed_a": entry("sensor.renamed_a", "pending_packages"),
  "sensor.renamed_b": entry("sensor.renamed_b", "package_update_status"),
  "sensor.renamed_c": entry("sensor.renamed_c", "unused_packages"),
  "sensor.renamed_d": entry("sensor.renamed_d", "package_health"),
  "sensor.renamed_e": entry("sensor.renamed_e", "container_status"),
  "button.renamed_f": entry("button.renamed_f", "package_scan"),
  "button.renamed_g": entry("button.renamed_g", "package_update"),
  "sensor.other_lxc": entry("sensor.other_lxc", "pending_packages", "other"),
  "sensor.foreign": {
    entity_id: "sensor.foreign",
    translation_key: "pending_packages",
    device_id: DEVICE,
    platform: "other_integration",
  },
};

const DEVICES = { [DEVICE]: { name: "nextcloud", name_by_user: "CT106" } };

const baseStates = () => ({
  "sensor.renamed_a": {
    state: "7",
    attributes: {
      scan_status: "success",
      last_attempt: SCAN_AT,
      security_updates: 2,
    },
  },
  "sensor.renamed_b": { state: "never", attributes: { update_status: "never" } },
  "sensor.renamed_c": {
    state: "0",
    attributes: { autoremove_status: "never", running: false },
  },
  "sensor.renamed_d": { state: "unknown", attributes: { check_status: "never" } },
  "sensor.renamed_e": { state: "running", attributes: {} },
  "button.renamed_f": { state: "unknown", attributes: {} },
  "button.renamed_g": { state: "unknown", attributes: { device_class: "update", snapshot_permission: true } },
});

const render = (mutate = () => {}, config = {}, lang = "pl") => {
  const states = baseStates();
  mutate(states);
  return deriveView({
    states,
    entities: ENTITIES,
    devices: DEVICES,
    config: { device_id: DEVICE, autoremove: false, ...config },
    now: NOW,
    lang,
  });
};

test("resolves entities by device, platform and translation key only", () => {
  assert.deepEqual(resolveEntities(ENTITIES, DEVICE).entities, {
    pending: "sensor.renamed_a",
    update: "sensor.renamed_b",
    unused: "sensor.renamed_c",
    health: "sensor.renamed_d",
    status: "sensor.renamed_e",
    scan: "button.renamed_f",
    updater: "button.renamed_g",
  });
  assert.equal(resolveEntities(ENTITIES, "missing").error, "not_found");
  const duplicate = {
    ...ENTITIES,
    "sensor.dup": entry("sensor.dup", "pending_packages"),
  };
  assert.equal(resolveEntities(duplicate, DEVICE).error, "ambiguous");
});

test("stub config uses the picker's criterion: the package Update button", () => {
  assert.equal(firstEligibleDevice({}), null);
  // LXC A lacks the package capability button; B has it. Permission is a state fact.
  const entities = {
    "sensor.a": entry("sensor.a", "pending_packages", "lxc-a"),
    "sensor.b": entry("sensor.b", "pending_packages", "lxc-b"),
    "button.b": entry("button.b", "package_update", "lxc-b"),
  };
  assert.equal(firstEligibleDevice(entities), "lxc-b");
  const onlyA = { "sensor.a": entities["sensor.a"] };
  assert.equal(firstEligibleDevice(onlyA), null);
});

test("without the Update button the card never offers Easy Update", () => {
  const { "button.renamed_g": _removed, ...entities } = ENTITIES;
  const result = deriveView({
    states: baseStates(),
    entities,
    devices: DEVICES,
    config: { device_id: DEVICE, autoremove: true },
    now: NOW,
    lang: "pl",
  });
  assert.equal(result.tone, "amber");
  assert.equal(result.primary, "7 aktualizacji");
  assert.equal(result.secondary, "Aktualizacja niedostępna dla tego LXC");
  assert.deepEqual(result.action, { kind: "details" });
});

for (const permission of [false, undefined, null, "true", 1]) {
  test(`normal Easy Update requires explicit snapshot permission (${permission})`, () => {
    const result = render((states) => {
      states["button.renamed_g"].attributes.snapshot_permission = permission;
    });
    assert.equal(result.secondary, "Aktualizacja niedostępna dla tego LXC");
    assert.deepEqual(result.action, { kind: "details" });
    assert.equal(firstEligibleDevice(ENTITIES), DEVICE, "capability is separate");
  });
}

test("an unapproved native button still permits Easy Update with snapshot permission", () => {
  const result = render((states) => {
    states["button.renamed_g"].state = "unavailable";
  });
  assert.equal(result.action.kind, "easy_update");
  assert.equal(result.action.data.skip_snapshot, undefined, "the normal action never skips");
  assert.equal(result.skipSnapshot, undefined, "the standalone view offers no skip");
});

test("amber: updates available start Easy Update with the displayed scan", () => {
  const result = render(undefined, { autoremove: true });
  assert.equal(result.tone, "amber");
  assert.equal(result.name, "CT106");
  assert.equal(result.primary, "7 aktualizacji");
  assert.match(result.secondary, /w tym 2 bezpieczeństwa/);
  assert.match(result.secondary, /skan: dziś 04:00/);
  assert.match(result.secondary, /\+ autoremove/);
  assert.deepEqual(result.action, {
    kind: "easy_update",
    data: { device_id: DEVICE, autoremove: true, expected_scan_attempt: SCAN_AT },
  });
});

test("YOLO off is passed explicitly", () => {
  assert.equal(render().action.data.autoremove, false);
});

test("green: system current shows last scan and offers Scan", () => {
  const result = render((s) => {
    s["sensor.renamed_a"].state = "0";
  });
  assert.equal(result.tone, "green");
  assert.equal(result.primary, "System aktualny");
  assert.equal(result.secondary, "Ostatni skan: dziś 04:00");
  assert.deepEqual(result.action, { kind: "scan", entity_id: "button.renamed_f" });
});

test("orange: Health degraded means restart required, not failure", () => {
  const result = render((s) => {
    s["sensor.renamed_a"].state = "0";
    s["sensor.renamed_d"].state = "degraded";
  });
  assert.equal(result.tone, "orange");
  assert.equal(result.primary, "Wymagany restart");
});

test("blue: running operations never offer another start", () => {
  const cases = [
    [(s) => (s["sensor.renamed_b"].state = "running"), "Aktualizacja w toku…"],
    [
      (s) => (s["sensor.renamed_c"].attributes.autoremove_status = "running"),
      "Usuwanie nieużywanych pakietów…",
    ],
    [(s) => (s["sensor.renamed_a"].attributes.scan_status = "running"), "Skanowanie…"],
    [
      (s) => (s["sensor.renamed_d"].attributes.check_status = "running"),
      "Sprawdzanie stanu systemu…",
    ],
  ];
  for (const [mutate, text] of cases) {
    const result = render(mutate);
    assert.equal(result.tone, "blue");
    assert.equal(result.primary, text);
    assert.deepEqual(result.action, { kind: "none" });
  }
});

test("red: failed update until a newer successful scan exists", () => {
  const failedAt = new Date(2026, 9, 3, 5, 0).toISOString();
  const failed = (s) => {
    s["sensor.renamed_b"] = {
      state: "failed",
      attributes: {
        last_attempt: failedAt,
        outcome: "plan_changed",
        retained_snapshot_name: "hubinet-preupd-x",
      },
    };
    s["sensor.renamed_a"] = { state: "unknown", attributes: { scan_status: "never" } };
  };
  const result = render(failed);
  assert.equal(result.tone, "red");
  assert.equal(result.primary, "Aktualizacja nie powiodła się");
  assert.match(result.secondary, /plan się zmienił/);
  assert.match(result.secondary, /hubinet-preupd-x/);
  assert.deepEqual(result.action, { kind: "details" });

  const rescanned = render((s) => {
    failed(s);
    s["sensor.renamed_a"] = {
      state: "3",
      attributes: {
        scan_status: "success",
        last_attempt: new Date(2026, 9, 3, 6, 0).toISOString(),
      },
    };
  });
  assert.equal(rescanned.tone, "amber");
});

test("red: failed Autoremove and failed Health use backend facts", () => {
  const cleanup = render((s) => {
    s["sensor.renamed_c"].attributes = {
      autoremove_status: "failed",
      last_attempt: new Date(2026, 9, 3, 5, 0).toISOString(),
    };
  });
  assert.equal(cleanup.primary, "Usuwanie pakietów nie powiodło się");
  assert.deepEqual(cleanup.action, { kind: "details" });
  const health = render((s) => {
    s["sensor.renamed_d"].state = "failed";
  });
  assert.equal(health.tone, "red");
  assert.equal(health.primary, "Problem z systemem (Health)");
  assert.deepEqual(health.action, { kind: "details" });
});

test("grey: no current scan after a successful update offers Scan", () => {
  const result = render((s) => {
    s["sensor.renamed_a"] = { state: "unknown", attributes: { scan_status: "never" } };
    s["sensor.renamed_b"] = {
      state: "success",
      attributes: {
        last_attempt: new Date(2026, 9, 3, 11, 0).toISOString(),
        changed_package_count: 7,
      },
    };
  });
  assert.equal(result.tone, "grey");
  assert.equal(result.primary, "Brak aktualnego skanu");
  assert.equal(result.secondary, "Zaktualizowano pakiety: 7 · dziś 11:00");
  assert.equal(result.action.kind, "scan");
});

test("grey: failed scan and stopped LXC", () => {
  const failed = render((s) => {
    s["sensor.renamed_a"] = {
      state: "unknown",
      attributes: { scan_status: "failed", last_error: "dpkg_unfinished" },
    };
  });
  assert.equal(failed.primary, "Skan nieudany");
  assert.equal(failed.secondary, "dpkg_unfinished");
  const stopped = render((s) => {
    s["sensor.renamed_e"].state = "stopped";
    s["sensor.renamed_a"].state = "unavailable";
  });
  assert.equal(stopped.primary, "LXC nie działa");
  assert.deepEqual(stopped.action, { kind: "none" });
});

test("configuration errors fail closed", () => {
  assert.equal(
    deriveView({ states: {}, entities: {}, devices: {}, config: {}, now: NOW, lang: "en" })
      .primary,
    "Choose a Hubinet-Ops LXC"
  );
  const missing = deriveView({
    states: {},
    entities: ENTITIES,
    devices: {},
    config: { device_id: "gone" },
    now: NOW,
    lang: "en",
  });
  assert.equal(missing.tone, "error");
  assert.equal(missing.action.kind, "none");
});

test("Polish plurals and English texts", () => {
  assert.equal(formatUpdates(1, "pl"), "1 aktualizacja");
  assert.equal(formatUpdates(3, "pl"), "3 aktualizacje");
  assert.equal(formatUpdates(7, "pl"), "7 aktualizacji");
  assert.equal(formatUpdates(1, "en"), "1 update");
  assert.equal(render(undefined, {}, "en").primary, "7 updates");
  assert.equal(
    formatTime(new Date(2026, 9, 2, 4, 0).toISOString(), NOW, "pl"),
    "wczoraj 04:00"
  );
});

test("orange: missing Proxmox data is not reported as a stopped LXC", () => {
  for (const status of ["unavailable", "unknown"]) {
    const result = render((s) => {
      s["sensor.renamed_e"].state = status;
      s["sensor.renamed_a"].state = "unavailable";
    });
    assert.equal(result.tone, "orange");
    assert.equal(result.primary, "Brak aktualnych danych z Proxmox");
    assert.equal(result.secondary, "LXC może nadal działać");
    assert.deepEqual(result.action, { kind: "none" });
  }
  const suspended = render((s) => {
    s["sensor.renamed_e"].state = "suspended";
  });
  assert.equal(suspended.primary, "LXC nie działa");
});

// --- Checkpoint C: one Update without a snapshot, on request only -----------

const offer = (mutate = () => {}, config = {}) => {
  const states = baseStates();
  mutate(states);
  return deriveView({
    states,
    entities: ENTITIES,
    devices: DEVICES,
    config: { device_id: DEVICE, autoremove: false, ...config },
    now: NOW,
    lang: "pl",
    offerSkipSnapshot: true,
  });
};

test("standalone Easy Update never gets the skip action in any state", () => {
  const mutations = [
    () => {},
    (st) => (st["button.renamed_g"].attributes.snapshot_permission = false),
    (st) => (st["sensor.renamed_a"].state = "0"),
    (st) => (st["sensor.renamed_a"].attributes.scan_status = "failed"),
    (st) => (st["sensor.renamed_b"].state = "running"),
  ];
  for (const mutate of mutations) {
    for (const config of [{}, { autoremove: true }]) {
      const result = render(mutate, config);
      assert.equal("skipSnapshot" in result, false);
      assert.doesNotMatch(JSON.stringify(result), /skip/);
    }
  }
  // The standalone card module neither asks for nor renders it.
  const card = readFileSync(
    new URL("../../custom_components/hubinet_ops/frontend/hubinet-ops-cards.js", import.meta.url),
    "utf8"
  );
  assert.doesNotMatch(card, /skip/i);
});

test("a requested skip targets exactly the displayed scan without YOLO", () => {
  for (const autoremove of [false, true]) {
    const result = offer(undefined, { autoremove });
    assert.deepEqual(result.skipSnapshot, {
      pending: 7,
      data: {
        device_id: DEVICE,
        autoremove: false,
        skip_snapshot: true,
        expected_scan_attempt: SCAN_AT,
      },
    });
    // The normal action is unchanged and keeps the saved YOLO choice.
    assert.deepEqual(result.action, {
      kind: "easy_update",
      data: { device_id: DEVICE, autoremove, expected_scan_attempt: SCAN_AT },
    });
  }
});

for (const permission of [false, undefined, null, "true", 1]) {
  test(`skip needs no snapshot permission; normal Update still does (${permission})`, () => {
    const result = offer((states) => {
      states["button.renamed_g"].attributes.snapshot_permission = permission;
    });
    assert.deepEqual(result.action, { kind: "details" });
    assert.equal(result.secondary, "Aktualizacja niedostępna dla tego LXC");
    assert.equal(result.skipSnapshot.data.skip_snapshot, true);
    assert.equal(result.skipSnapshot.data.expected_scan_attempt, SCAN_AT);
  });
}

test("skip is offered only for a current successful scan with pending packages", () => {
  const absent = {
    "no pending packages": (st) => (st["sensor.renamed_a"].state = "0"),
    "unknown count": (st) => (st["sensor.renamed_a"].state = "unknown"),
    "failed scan": (st) => (st["sensor.renamed_a"].attributes.scan_status = "failed"),
    "no scan yet": (st) => (st["sensor.renamed_a"].attributes.scan_status = "never"),
    "scan running": (st) => (st["sensor.renamed_a"].attributes.scan_status = "running"),
    "no scan attempt": (st) => delete st["sensor.renamed_a"].attributes.last_attempt,
    "unparsable scan attempt": (st) => (st["sensor.renamed_a"].attributes.last_attempt = "x"),
    "update running": (st) => (st["sensor.renamed_b"].state = "running"),
    "autoremove running": (st) => (st["sensor.renamed_c"].attributes.autoremove_status = "running"),
    "health running": (st) => (st["sensor.renamed_d"].attributes.check_status = "running"),
    "failed update newer than the scan": (st) => {
      st["sensor.renamed_b"] = {
        state: "failed",
        attributes: { last_attempt: new Date(2026, 9, 3, 5, 0).toISOString() },
      };
    },
    "failed health": (st) => (st["sensor.renamed_d"].state = "failed"),
    "stopped LXC": (st) => (st["sensor.renamed_e"].state = "stopped"),
    "no Proxmox data": (st) => (st["sensor.renamed_e"].state = "unavailable"),
    "pending unavailable": (st) => (st["sensor.renamed_a"].state = "unavailable"),
  };
  for (const [name, mutate] of Object.entries(absent)) {
    assert.equal("skipSnapshot" in offer(mutate), false, name);
  }
  // Package capability is the Update button; without it there is no skip.
  const { "button.renamed_g": _removed, ...entities } = ENTITIES;
  const result = deriveView({
    states: baseStates(),
    entities,
    devices: DEVICES,
    config: { device_id: DEVICE },
    now: NOW,
    lang: "pl",
    offerSkipSnapshot: true,
  });
  assert.equal("skipSnapshot" in result, false);
});
