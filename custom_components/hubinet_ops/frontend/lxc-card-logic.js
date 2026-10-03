// Pure presentation logic for the Hubinet-Ops LXC card.
//
// It reads existing Home Assistant registry entries and entity states only and
// never decides anything the backend owns. Entities are found by device_id,
// platform and fixed translation key; Restart has no translation key, so it is
// found by its native `restart` device class on the same device.

export const DOMAIN = "hubinet_ops";
export const LXC_CARD_TYPE = "hubinet-ops-lxc-card";

export const LXC_ROLES = {
  status: ["sensor", "container_status"],
  cpu: ["sensor", "container_cpu"],
  memPct: ["sensor", "container_memory_percentage"],
  memMax: ["sensor", "container_max_memory"],
  mem: ["sensor", "container_memory"],
  uptime: ["sensor", "container_uptime"],
  disk: ["sensor", "container_disk"],
  diskMax: ["sensor", "container_max_disk"],
  netIn: ["sensor", "container_netin"],
  netOut: ["sensor", "container_netout"],
  start: ["button", "start"],
  stop: ["button", "stop"],
  create: ["button", "snapshot_create"],
  restore: ["button", "snapshot_restore"],
  delete: ["button", "snapshot_delete"],
  snapshot: ["select", "snapshot_to_restore"],
};

// Actions that need a second tap within CONFIRM_MS before they run.
export const CONFIRM = new Set(["stop", "restart", "restore", "delete"]);
export const CONFIRM_MS = 4000;

const STRINGS = {
  en: {
    choose: "Choose a Hubinet-Ops LXC",
    not_found: "Hubinet-Ops LXC not found",
    ambiguous: "Hubinet-Ops LXC entities are ambiguous",
    running: "Running",
    stopped: "Stopped",
    suspended: "Suspended",
    no_data: "No data",
    uptime: "uptime {value}",
    cpu: "CPU",
    ram: "RAM",
    disk: "Disk",
    net: "Network",
    of: "{used} of {total}",
    packages: "Packages",
    snapshots: "Snapshots",
    power: "Power",
    no_snapshots: "No snapshots",
    choose_snapshot: "Choose a snapshot",
    update: "Update",
    scan: "Scan",
    create: "Create",
    restore: "Restore",
    delete: "Delete",
    start: "Start",
    stop: "Stop",
    restart: "Restart",
    confirm: "Sure? Tap again",
    days: "{d} d {h} h",
    hours: "{h} h",
    minutes: "{m} min",
    label_device_id: "LXC",
    label_autoremove: "YOLO: remove unused packages after a successful update",
    label_name: "Name (optional)",
    label_compact: "Compact",
  },
  pl: {
    choose: "Wybierz LXC Hubinet-Ops",
    not_found: "Nie znaleziono LXC Hubinet-Ops",
    ambiguous: "Niejednoznaczne encje LXC Hubinet-Ops",
    running: "Uruchomiony",
    stopped: "Zatrzymany",
    suspended: "Wstrzymany",
    no_data: "Brak danych",
    uptime: "działa {value}",
    cpu: "CPU",
    ram: "RAM",
    disk: "Dysk",
    net: "Sieć",
    of: "{used} z {total}",
    packages: "Pakiety",
    snapshots: "Migawki",
    power: "Zasilanie",
    no_snapshots: "Brak migawek",
    choose_snapshot: "Wybierz migawkę",
    update: "Aktualizuj",
    scan: "Skanuj",
    create: "Utwórz",
    restore: "Przywróć",
    delete: "Usuń",
    start: "Start",
    stop: "Stop",
    restart: "Restart",
    confirm: "Na pewno? Dotknij ponownie",
    days: "{d} d {h} h",
    hours: "{h} h",
    minutes: "{m} min",
    label_device_id: "LXC",
    label_autoremove: "YOLO: usuń nieużywane pakiety po udanej aktualizacji",
    label_name: "Nazwa (opcjonalnie)",
    label_compact: "Kompaktowa",
  },
};

export const lxcStrings = (lang) =>
  STRINGS[String(lang || "en").toLowerCase().startsWith("pl") ? "pl" : "en"];

const fill = (template, values) =>
  template.replace(/\{(\w+)\}/g, (_m, key) =>
    values[key] === undefined ? "" : String(values[key])
  );

const domainOf = (entityId) => entityId.split(".", 1)[0];

// Resolve every role for one device; more than one match fails closed.
export const resolveLxc = (entities, states, deviceId) => {
  const found = { restart: [] };
  for (const role of Object.keys(LXC_ROLES)) {
    found[role] = [];
  }
  for (const entry of Object.values(entities || {})) {
    if (!entry || entry.device_id !== deviceId || entry.platform !== DOMAIN) {
      continue;
    }
    const domain = domainOf(entry.entity_id);
    for (const [role, [roleDomain, key]] of Object.entries(LXC_ROLES)) {
      if (entry.translation_key === key && domain === roleDomain) {
        found[role].push(entry.entity_id);
      }
    }
    const state = (states || {})[entry.entity_id];
    if (
      domain === "button" &&
      !entry.translation_key &&
      state &&
      state.attributes &&
      state.attributes.device_class === "restart"
    ) {
      found.restart.push(entry.entity_id);
    }
  }
  const resolved = {};
  for (const [role, ids] of Object.entries(found)) {
    if (ids.length > 1) {
      return { error: "ambiguous" };
    }
    resolved[role] = ids[0] || null;
  }
  if (!resolved.status) {
    return { error: "not_found" };
  }
  return { entities: resolved };
};

export const firstLxcDevice = (entities) => {
  const [domain, key] = LXC_ROLES.status;
  const match = Object.values(entities || {}).find(
    (entry) =>
      entry &&
      entry.platform === DOMAIN &&
      entry.translation_key === key &&
      entry.device_id &&
      domainOf(entry.entity_id) === domain
  );
  return match ? match.device_id : null;
};

const number = (state) => {
  if (!state) {
    return null;
  }
  const value = Number.parseFloat(state.state);
  return Number.isFinite(value) ? value : null;
};

const decimals = (value, lang) =>
  new Intl.NumberFormat(String(lang).startsWith("pl") ? "pl" : "en", {
    maximumFractionDigits: value < 10 ? 1 : 0,
  }).format(value);

const withUnit = (state, lang) => {
  const value = number(state);
  if (value === null) {
    return null;
  }
  const unit = state.attributes && state.attributes.unit_of_measurement;
  return `${decimals(value, lang)}${unit ? ` ${unit}` : ""}`;
};

export const formatDuration = (state, lang) => {
  const value = number(state);
  if (value === null) {
    return "";
  }
  const unit = state.attributes && state.attributes.unit_of_measurement;
  const factor = { s: 1, min: 60, h: 3600, d: 86400 }[unit] ?? 1;
  const seconds = value * factor;
  const s = lxcStrings(lang);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) {
    return fill(s.days, { d: days, h: hours });
  }
  if (hours > 0) {
    return fill(s.hours, { h: hours });
  }
  return fill(s.minutes, { m: Math.floor(seconds / 60) });
};

// Buttons read "unknown" until first pressed; only "unavailable" blocks them.
const available = (state) => Boolean(state) && state.state !== "unavailable";

// Build the LXC card view from existing facts. `packageView` is the Easy Update
// view for the same device (or null when the LXC has no package entities).
export const deriveLxcView = ({
  states,
  entities,
  devices,
  config,
  lang,
  packageView,
}) => {
  const s = lxcStrings(lang);
  const deviceId = config && config.device_id;
  if (!deviceId) {
    return { error: s.choose };
  }
  const lookup = resolveLxc(entities, states, deviceId);
  if (lookup.error) {
    return { error: s[lookup.error] };
  }
  const ids = lookup.entities;
  const st = (role) => (ids[role] && (states || {})[ids[role]]) || null;
  const device = (devices || {})[deviceId];
  const name =
    (config && config.name) ||
    (device && (device.name_by_user || device.name)) ||
    "";

  const statusValue = st("status") ? st("status").state : "unavailable";
  const status =
    statusValue === "running"
      ? { tone: "green", label: s.running }
      : statusValue === "stopped"
        ? { tone: "grey", label: s.stopped }
        : statusValue === "suspended"
          ? { tone: "grey", label: s.suspended }
          : { tone: "orange", label: s.no_data };
  const running = statusValue === "running";
  const noData = status.tone === "orange";

  const stats = [];
  const cpu = number(st("cpu"));
  stats.push({
    key: "cpu",
    label: s.cpu,
    value: running && cpu !== null ? `${decimals(cpu, lang)}%` : "—",
    history: ids.cpu,
    tone: "blue",
    max: 100,
  });
  const memPct = number(st("memPct"));
  const memMax = withUnit(st("memMax"), lang);
  const mem = withUnit(st("mem"), lang);
  stats.push({
    key: "ram",
    label: s.ram,
    value: running && memPct !== null ? `${decimals(memPct, lang)}%` : "—",
    detail:
      running && memMax ? (mem ? fill(s.of, { used: mem, total: memMax }) : memMax) : "",
    pct: running ? memPct : null,
    history: ids.memPct,
    tone: "purple",
    max: 100,
  });
  const disk = number(st("disk"));
  const diskMax = number(st("diskMax"));
  if (disk !== null && diskMax) {
    stats.push({
      key: "disk",
      label: s.disk,
      value: `${decimals((disk / diskMax) * 100, lang)}%`,
      detail: fill(s.of, {
        used: withUnit(st("disk"), lang),
        total: withUnit(st("diskMax"), lang),
      }),
      pct: (disk / diskMax) * 100,
      tone: "orange",
    });
  }
  const netIn = withUnit(st("netIn"), lang);
  const netOut = withUnit(st("netOut"), lang);
  if (netIn || netOut) {
    stats.push({
      key: "net",
      label: s.net,
      value: `↓${netIn || "—"} ↑${netOut || "—"}`,
      history: ids.netIn,
      tone: "green",
    });
  }

  const button = (role) => ({
    entity_id: ids[role],
    available: Boolean(ids[role]) && available(st(role)) && !noData,
  });
  const select = st("snapshot");
  const options =
    select && Array.isArray(select.attributes && select.attributes.options)
      ? select.attributes.options
      : [];
  // The backend's exact selection identity; the select's state is not used,
  // because a snapshot may itself be named like a state ("unknown").
  const chosen = select && select.attributes && select.attributes.selected_snapshot;
  const selected =
    typeof chosen === "string" && options.includes(chosen) ? chosen : null;

  return {
    name,
    status,
    running,
    noData,
    uptime: running ? fill(s.uptime, { value: formatDuration(st("uptime"), lang) }) : "",
    stats,
    package: packageView || null,
    snapshots: ids.snapshot
      ? {
          entity_id: ids.snapshot,
          options,
          selected,
          available: available(select) && !noData,
        }
      : null,
    actions: {
      create: button("create"),
      restore: { ...button("restore"), available: button("restore").available && Boolean(selected) },
      delete: { ...button("delete"), available: button("delete").available && Boolean(selected) },
      start: { ...button("start"), available: button("start").available && !running },
      stop: { ...button("stop"), available: button("stop").available && running },
      restart: { ...button("restart"), available: button("restart").available && running },
    },
  };
};

// Downsample history states to at most `points` finite numbers, in order,
// always keeping the first and the latest sample.
export const historyPoints = (rows, points = 48) => {
  const values = (rows || [])
    .map((row) => Number.parseFloat(row && (row.s ?? row.state)))
    .filter((value) => Number.isFinite(value));
  if (values.length <= points) {
    return values;
  }
  if (points < 2) {
    return values.slice(-points);
  }
  const step = (values.length - 1) / (points - 1);
  return Array.from({ length: points }, (_v, i) => values[Math.round(i * step)]);
};

// The exact operation a confirmation tap stands for: Stop and Restart target
// this LXC's button, Restore and Delete the snapshot selected right now. A
// second tap confirms only when this is unchanged; null means not armable.
export const confirmTarget = (action, view, deviceId) => {
  const info = view && view.actions && view.actions[action];
  if (!CONFIRM.has(action) || !info || !info.entity_id || !deviceId) {
    return null;
  }
  if (action === "restore" || action === "delete") {
    const selected = view.snapshots && view.snapshots.selected;
    return selected === null || selected === undefined
      ? null
      : JSON.stringify([deviceId, info.entity_id, view.snapshots.entity_id, selected]);
  }
  return JSON.stringify([deviceId, info.entity_id]);
};
