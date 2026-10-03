// Run with: node --test tests/frontend
//
// HA's app replaces the native custom-element registry with a polyfill. Its
// definitions also create native stand-ins, resolving native whenDefined waits.
// Exercise both download orders, including an app that takes over ten seconds.
import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";

const CARD_TYPES = [
  "hubinet-ops-easy-update-card",
  "hubinet-ops-lxc-card",
  "hubinet-ops-lxc-mini-card",
  "hubinet-ops-vm-card",
  "hubinet-ops-vm-mini-card",
];

function makeRegistry(nativeRegistry) {
  const definitions = new Map();
  const pending = new Map();
  let requested;
  const waiting = new Promise((resolve) => { requested = resolve; });
  return {
    waiting,
    get: (name) => definitions.get(name),
    define(name, element) {
      assert.equal(definitions.has(name), false, `duplicate definition: ${name}`);
      definitions.set(name, element);
      // The polyfill defines a native stand-in after storing its definition.
      if (nativeRegistry && !nativeRegistry.get(name)) {
        nativeRegistry.define(name, class {});
      }
      pending.get(name)?.resolve(element);
    },
    whenDefined(name) {
      requested();
      if (definitions.has(name)) return Promise.resolve(definitions.get(name));
      if (!pending.has(name)) {
        let resolve;
        const promise = new Promise((done) => { resolve = done; });
        pending.set(name, { promise, resolve });
      }
      return pending.get(name).promise;
    },
  };
}

function setup(t) {
  const registry = makeRegistry();
  globalThis.HTMLElement = class {};
  globalThis.customElements = registry;
  globalThis.window = globalThis;
  globalThis.document = { documentElement: { lang: "pl" } };
  delete window.customCards;
  t.mock.timers.enable({ apis: ["setTimeout"] });
  return registry;
}

function loadCards(version) {
  const url = new URL(
    "../../custom_components/hubinet_ops/frontend/hubinet-ops-cards.js",
    import.meta.url,
  );
  // The release query also isolates the real guest module for each scenario.
  url.searchParams.set("v", version);
  return import(url.href);
}

function assertCards(registry) {
  for (const type of CARD_TYPES) {
    assert.ok(registry.get(type), `${type} visible to Home Assistant`);
    assert.ok(
      HTMLElement.prototype.isPrototypeOf(registry.get(type).prototype),
      `${type} extends the current HTMLElement`,
    );
  }
  assert.deepEqual(window.customCards.map(({ type }) => type).sort(), CARD_TYPES);
}

test("cards wait for a slow app even after ten seconds", async (t) => {
  const registry = setup(t);
  const loading = loadCards("slow-app");
  await registry.waiting;
  t.after(async () => {
    registry.define("home-assistant", class {});
    await loading;
  });
  t.mock.timers.tick(20000);
  // Allow any imports incorrectly started by a timeout to finish.
  await delay(50);
  for (const type of CARD_TYPES) assert.equal(registry.get(type), undefined);
  assert.equal(window.customCards, undefined);
});

test("a registry installed after ten seconds sees all five cards", async (t) => {
  const nativeRegistry = setup(t);
  const loading = loadCards("slow-polyfill");
  await nativeRegistry.waiting;
  t.mock.timers.tick(20000);
  await delay(50);
  const appRegistry = makeRegistry(nativeRegistry);
  globalThis.customElements = appRegistry;
  globalThis.HTMLElement = class {};
  appRegistry.define("home-assistant", class {});
  await loading;
  assertCards(appRegistry);
});

test("a native wait survives replacement before the app is defined", async (t) => {
  const nativeRegistry = setup(t);
  const loading = loadCards("native-wait");
  await nativeRegistry.waiting;
  const appRegistry = makeRegistry(nativeRegistry);
  globalThis.customElements = appRegistry;
  globalThis.HTMLElement = class {};
  appRegistry.define("home-assistant", class {});
  await loading;
  assertCards(appRegistry);
});

test("an already loaded app registers the cards once across resource imports", async (t) => {
  const registry = setup(t);
  registry.define("home-assistant", class {});
  await loadCards("app-first");
  assertCards(registry);
  const constructors = CARD_TYPES.map((type) => registry.get(type));
  await loadCards("another-resource-url");
  assertCards(registry);
  assert.deepEqual(CARD_TYPES.map((type) => registry.get(type)), constructors);
});
