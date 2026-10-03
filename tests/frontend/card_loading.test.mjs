// Run with: node --test tests/frontend
//
// Home Assistant imports the card module in parallel with its app bundle, which
// may replace window.customElements with a scoped-registry polyfill. Anything
// defined before that is invisible to Home Assistant, so the module must not
// define a card before <home-assistant> is defined.
import assert from "node:assert/strict";
import { test } from "node:test";

const registry = {};
let appReady;
const appDefined = new Promise((resolve) => {
  appReady = resolve;
});
globalThis.HTMLElement = class {};
globalThis.customElements = {
  get: (name) => registry[name],
  define: (name, cls) => {
    registry[name] = cls;
  },
  whenDefined: (name) => (name === "home-assistant" ? appDefined : new Promise(() => {})),
};
globalThis.window = globalThis;
globalThis.document = { documentElement: { lang: "pl" } };

test("cards are defined only after Home Assistant defines its app element", async () => {
  const loading = import("../../custom_components/hubinet_ops/frontend/hubinet-ops-cards.js");
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(Object.keys(registry), [], "nothing defined before the app");
  assert.equal(window.customCards, undefined, "nothing in the card picker yet");
  appReady();
  await loading;
  assert.deepEqual(Object.keys(registry).sort(), [
    "hubinet-ops-easy-update-card",
    "hubinet-ops-lxc-card",
    "hubinet-ops-lxc-mini-card",
    "hubinet-ops-vm-card",
    "hubinet-ops-vm-mini-card",
  ]);
  assert.equal(window.customCards.length, 5);
});
