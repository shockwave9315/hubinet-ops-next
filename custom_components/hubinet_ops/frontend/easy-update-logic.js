// Pure presentation logic for the Hubinet-Ops Easy Update card.
//
// It only reads existing Home Assistant registry entries and entity states and
// never calculates package plans, security classification, or mutation truth.
// The card is identified by a stable device_id; entities are found through the
// frontend entity registry by device_id, platform and fixed translation key.

export const DOMAIN = "hubinet_ops";
export const CARD_TYPE = "hubinet-ops-easy-update-card";

// role -> [entity domain, translation_key set by the integration]
export const ROLES = {
  pending: ["sensor", "pending_packages"],
  update: ["sensor", "package_update_status"],
  unused: ["sensor", "unused_packages"],
  health: ["sensor", "package_health"],
  status: ["sensor", "container_status"],
  scan: ["button", "package_scan"],
};

const STRINGS = {
  en: {
    choose: "Choose a Hubinet-Ops LXC",
    not_found: "Hubinet-Ops LXC not found",
    ambiguous: "Hubinet-Ops LXC entities are ambiguous",
    stopped: "LXC is not running",
    no_data: "No current data from Proxmox",
    no_data_detail: "The LXC may still be running",
    updating: "Update in progress…",
    autoremoving: "Removing unused packages…",
    scanning: "Scanning…",
    checking: "Checking system health…",
    starting: "Starting…",
    update_failed: "Update failed",
    autoremove_failed: "Removing unused packages failed",
    health_failed: "System problem (Health)",
    health_detail: "Open details",
    current: "System up to date",
    reboot: "Restart required",
    scan_failed: "Scan failed",
    no_scan: "No current scan",
    last_scan: "Last scan: {time}",
    scan_short: "scan {time}",
    security: "{count} security",
    unused: "{count} unused",
    autoremove: "+ autoremove",
    updated: "Updated {count} packages · {time}",
    today: "today {time}",
    yesterday: "yesterday {time}",
    snapshot: "snapshot kept: {name}",
    updates: { one: "{count} update", other: "{count} updates" },
    outcome: {
      plan_changed: "the plan changed — scan again",
      plan_failed: "the plan could not be prepared",
      snapshot_failed: "the safety snapshot failed",
      package_manager_busy: "the package manager was busy",
      mutation_failed: "the package upgrade failed",
      mutation_timed_out: "the package upgrade timed out",
      mutation_uncertain: "the result is uncertain",
      guest_unavailable: "the LXC became unavailable",
      liveness_failed: "the LXC did not answer afterwards",
      helper_outdated: "the host helper is outdated",
    },
    label_device_id: "LXC",
    label_autoremove: "YOLO: remove unused packages after a successful update",
    label_name: "Name (optional)",
    helper_autoremove:
      "After the same update succeeds and Health has finished, remove freshly found unused packages with the existing safe Autoremove.",
  },
  pl: {
    choose: "Wybierz LXC Hubinet-Ops",
    not_found: "Nie znaleziono LXC Hubinet-Ops",
    ambiguous: "Niejednoznaczne encje LXC Hubinet-Ops",
    stopped: "LXC nie działa",
    no_data: "Brak aktualnych danych z Proxmox",
    no_data_detail: "LXC może nadal działać",
    updating: "Aktualizacja w toku…",
    autoremoving: "Usuwanie nieużywanych pakietów…",
    scanning: "Skanowanie…",
    checking: "Sprawdzanie stanu systemu…",
    starting: "Uruchamianie…",
    update_failed: "Aktualizacja nie powiodła się",
    autoremove_failed: "Usuwanie pakietów nie powiodło się",
    health_failed: "Problem z systemem (Health)",
    health_detail: "Otwórz szczegóły",
    current: "System aktualny",
    reboot: "Wymagany restart",
    scan_failed: "Skan nieudany",
    no_scan: "Brak aktualnego skanu",
    last_scan: "Ostatni skan: {time}",
    scan_short: "skan: {time}",
    security: "w tym {count} bezpieczeństwa",
    unused: "{count} nieużywane",
    autoremove: "+ autoremove",
    updated: "Zaktualizowano pakiety: {count} · {time}",
    today: "dziś {time}",
    yesterday: "wczoraj {time}",
    snapshot: "pozostawiona migawka: {name}",
    updates: {
      one: "{count} aktualizacja",
      few: "{count} aktualizacje",
      many: "{count} aktualizacji",
      other: "{count} aktualizacji",
    },
    outcome: {
      plan_changed: "plan się zmienił — wykonaj skan",
      plan_failed: "nie udało się przygotować planu",
      snapshot_failed: "nie udało się utworzyć migawki",
      package_manager_busy: "menedżer pakietów był zajęty",
      mutation_failed: "aktualizacja pakietów nie powiodła się",
      mutation_timed_out: "przekroczono czas aktualizacji",
      mutation_uncertain: "wynik jest niepewny",
      guest_unavailable: "LXC stał się niedostępny",
      liveness_failed: "LXC nie odpowiada po aktualizacji",
      helper_outdated: "helper na hoście jest przestarzały",
    },
    label_device_id: "LXC",
    label_autoremove: "YOLO: usuń nieużywane pakiety po udanej aktualizacji",
    label_name: "Nazwa (opcjonalnie)",
    helper_autoremove:
      "Po udanej tej samej aktualizacji i zakończeniu Health usuwa świeżo wykryte nieużywane pakiety istniejącym bezpiecznym Autoremove.",
  },
};

export const language = (value) =>
  String(value || "en").toLowerCase().startsWith("pl") ? "pl" : "en";

export const strings = (lang) => STRINGS[language(lang)];

const fill = (template, values) =>
  template.replace(/\{(\w+)\}/g, (_match, key) =>
    values[key] === undefined ? "" : String(values[key])
  );

export const formatUpdates = (count, lang) => {
  const table = strings(lang).updates;
  const rule = new Intl.PluralRules(language(lang)).select(count);
  return fill(table[rule] || table.other, { count });
};

const parseTime = (value) => {
  const time = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? time : null;
};

export const formatTime = (iso, now, lang) => {
  const time = parseTime(iso);
  if (time === null) {
    return "";
  }
  const date = new Date(time);
  const locale = language(lang);
  const clock = new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
  const dayStart = (moment) =>
    new Date(moment.getFullYear(), moment.getMonth(), moment.getDate()).getTime();
  const days = Math.round((dayStart(new Date(now)) - dayStart(date)) / 86400000);
  const s = strings(lang);
  if (days === 0) {
    return fill(s.today, { time: clock });
  }
  if (days === 1) {
    return fill(s.yesterday, { time: clock });
  }
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
};

// Find this device's entities by registry metadata only (never by names or
// entity_id text). A role with more than one match fails closed.
export const resolveEntities = (entities, deviceId) => {
  const found = {};
  for (const role of Object.keys(ROLES)) {
    found[role] = [];
  }
  for (const entry of Object.values(entities || {})) {
    if (!entry || entry.device_id !== deviceId || entry.platform !== DOMAIN) {
      continue;
    }
    for (const [role, [domain, key]] of Object.entries(ROLES)) {
      if (
        entry.translation_key === key &&
        entry.entity_id.split(".", 1)[0] === domain
      ) {
        found[role].push(entry.entity_id);
      }
    }
  }
  const resolved = {};
  for (const [role, ids] of Object.entries(found)) {
    if (ids.length > 1) {
      return { error: "ambiguous" };
    }
    resolved[role] = ids[0] || null;
  }
  if (!resolved.pending) {
    return { error: "not_found" };
  }
  return { entities: resolved };
};

// The first package-capable LXC device, for the card picker preview.
export const firstEligibleDevice = (entities) => {
  const [domain, key] = ROLES.pending;
  const match = Object.values(entities || {}).find(
    (entry) =>
      entry &&
      entry.platform === DOMAIN &&
      entry.translation_key === key &&
      entry.device_id &&
      entry.entity_id.split(".", 1)[0] === domain
  );
  return match ? match.device_id : null;
};

const view = (tone, icon, primary, secondary, action = { kind: "none" }) => ({
  tone,
  icon,
  primary,
  secondary,
  action,
});

const DETAILS = { kind: "details" };

// Derive the compact card presentation from existing backend facts only.
// input: { states, entities, devices, config, now, lang }
export const deriveView = ({ states, entities, devices, config, now, lang }) => {
  const s = strings(lang);
  const deviceId = config && config.device_id;
  if (!deviceId) {
    return { ...view("error", "mdi:package-variant", s.choose, ""), name: "" };
  }
  const device = (devices || {})[deviceId];
  const name =
    (config && config.name) ||
    (device && (device.name_by_user || device.name)) ||
    "";
  const lookup = resolveEntities(entities, deviceId);
  if (lookup.error) {
    return { ...view("error", "mdi:alert", s[lookup.error], ""), name };
  }
  const ids = lookup.entities;
  const state = (role) => (ids[role] && states[ids[role]]) || null;
  const attr = (role, key) => {
    const obj = state(role);
    return obj && obj.attributes ? obj.attributes[key] : undefined;
  };
  const value = (role) => (state(role) ? state(role).state : undefined);
  const scanAction = ids.scan
    ? { kind: "scan", entity_id: ids.scan }
    : { kind: "none" };
  const result = (v) => ({ ...v, name });

  // Native container status tells "stopped" apart from missing current data:
  // a failed Proxmox refresh makes every entity of the host unavailable.
  const pending = state("pending");
  const status = ids.status ? value("status") : undefined;
  if (status === "stopped" || status === "suspended") {
    return result(view("grey", "mdi:stop-circle-outline", s.stopped, ""));
  }
  if (ids.status && status !== "running") {
    return result(view("orange", "mdi:cloud-alert", s.no_data, s.no_data_detail));
  }
  if (!pending || pending.state === "unavailable") {
    return result(view("grey", "mdi:stop-circle-outline", s.stopped, ""));
  }

  if (value("update") === "running") {
    return result(view("blue", "mdi:progress-download", s.updating, ""));
  }
  if (attr("unused", "autoremove_status") === "running") {
    return result(view("blue", "mdi:broom", s.autoremoving, ""));
  }
  if (attr("pending", "scan_status") === "running") {
    return result(view("blue", "mdi:magnify", s.scanning, ""));
  }
  if (attr("health", "check_status") === "running") {
    return result(view("blue", "mdi:heart-pulse", s.checking, ""));
  }

  const scanStatus = attr("pending", "scan_status");
  const scanAt = parseTime(attr("pending", "last_attempt"));
  const newerScan = (iso) => {
    const failedAt = parseTime(iso);
    return (
      scanStatus === "success" &&
      scanAt !== null &&
      failedAt !== null &&
      scanAt > failedAt
    );
  };

  if (value("update") === "failed" && !newerScan(attr("update", "last_attempt"))) {
    const parts = [];
    const outcome = attr("update", "outcome");
    if (outcome && s.outcome[outcome]) {
      parts.push(s.outcome[outcome]);
    }
    const kept =
      attr("update", "retained_snapshot_name") ||
      attr("update", "uncertain_snapshot_name");
    if (kept) {
      parts.push(fill(s.snapshot, { name: kept }));
    }
    return result(
      view("red", "mdi:alert-circle", s.update_failed, parts.join(" · "), DETAILS)
    );
  }
  if (
    attr("unused", "autoremove_status") === "failed" &&
    !newerScan(attr("unused", "last_attempt"))
  ) {
    const outcome = attr("unused", "outcome");
    return result(
      view(
        "red",
        "mdi:alert-circle",
        s.autoremove_failed,
        (outcome && s.outcome[outcome]) || "",
        DETAILS
      )
    );
  }
  if (value("health") === "failed") {
    return result(
      view("red", "mdi:alert-circle", s.health_failed, s.health_detail, DETAILS)
    );
  }

  const count = Number.parseInt(value("pending"), 10);
  const hasCount = scanStatus === "success" && Number.isFinite(count) && count >= 0;
  const scanTime = formatTime(attr("pending", "last_attempt"), now, lang);
  const unused = Number.parseInt(value("unused"), 10);

  if (hasCount && count > 0) {
    const parts = [];
    const security = Number(attr("pending", "security_updates"));
    if (Number.isFinite(security) && security > 0) {
      parts.push(fill(s.security, { count: security }));
    }
    if (scanTime) {
      parts.push(fill(s.scan_short, { time: scanTime }));
    }
    if (Number.isFinite(unused) && unused > 0) {
      parts.push(fill(s.unused, { count: unused }));
    }
    if (config.autoremove) {
      parts.push(s.autoremove);
    }
    return result(
      view("amber", "mdi:package-up", formatUpdates(count, lang), parts.join(" · "), {
        kind: "easy_update",
        data: {
          device_id: deviceId,
          autoremove: Boolean(config.autoremove),
          expected_scan_attempt: attr("pending", "last_attempt"),
        },
      })
    );
  }
  if (hasCount) {
    const reboot = value("health") === "degraded";
    return result(
      view(
        reboot ? "orange" : "green",
        reboot ? "mdi:restart-alert" : "mdi:package-check",
        reboot ? s.reboot : s.current,
        scanTime ? fill(s.last_scan, { time: scanTime }) : "",
        scanAction
      )
    );
  }
  if (scanStatus === "failed") {
    return result(
      view(
        "grey",
        "mdi:package-variant-remove",
        s.scan_failed,
        attr("pending", "last_error") || "",
        scanAction
      )
    );
  }
  let secondary = "";
  const changed = Number(attr("update", "changed_package_count"));
  if (value("update") === "success" && Number.isFinite(changed)) {
    secondary = fill(s.updated, {
      count: changed,
      time: formatTime(attr("update", "last_attempt"), now, lang),
    });
  }
  return result(
    view("grey", "mdi:package-variant-closed", s.no_scan, secondary, scanAction)
  );
};
