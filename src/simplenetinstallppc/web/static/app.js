"use strict";
// SimpleNetInstallPPC UI: plain DOM code over the JSON API (see web/app.py).
// Everything shown comes from the network (model names, vendor strings), so
// text only ever goes in through textContent, never innerHTML.

const $ = (sel) => document.querySelector(sel);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    else if (key === "class") node.className = value;
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function showError(message) {
  const box = $("#error");
  box.textContent = message || "";
  box.hidden = !message;
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (method !== "GET") {
    options.headers["X-Netboot-UI"] = "1";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(path, options);
  if (response.status === 401) {
    location.href = "/login.html";
    throw new Error("login required");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.error || `${response.status} ${response.statusText}`;
    showError(message);
    throw new Error(message);
  }
  showError("");
  return data;
}

const time = (t) => t ? new Date(t * 1000).toLocaleString() : "-";
const clock = (t) => new Date(t * 1000).toLocaleTimeString();
function size(bytes) {
  if (bytes === null || bytes === undefined) return "-";
  const units = ["B", "KiB", "MiB", "GiB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(i ? 1 : 0)} ${units[i]}`;
}

// -- help: ? buttons show their explanation in a popover -------------------------
const popover = el("div", { class: "popover", role: "tooltip", hidden: true });
document.body.append(popover);
let popoverFor = null;

function helpButton(text) {
  return el("button", { type: "button", class: "help", "aria-label": "Help", "data-help": text }, "?");
}

function closeHelp() {
  popover.hidden = true;
  if (popoverFor) popoverFor.setAttribute("aria-expanded", "false");
  popoverFor = null;
}

function openHelp(button) {
  closeHelp();
  popover.textContent = button.dataset.help;
  popover.hidden = false;
  const r = button.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.left, document.documentElement.clientWidth - popover.offsetWidth - 8));
  popover.style.left = `${left + window.scrollX}px`;
  popover.style.top = `${r.bottom + window.scrollY + 6}px`;
  popoverFor = button;
  button.setAttribute("aria-expanded", "true");
}

let pinned = false;                   // opened by a click: stays until the next click

document.addEventListener("click", (e) => {
  const button = e.target.closest("button.help");
  if (button) {
    e.preventDefault();                 // don't tick the radio or checkbox the label holds
    if (popoverFor === button && pinned) { closeHelp(); pinned = false; return; }
    openHelp(button);
    pinned = true;
  } else if (!popover.contains(e.target)) {
    closeHelp();
    pinned = false;
  }
});
document.addEventListener("mouseover", (e) => {
  const button = e.target.closest("button.help");
  if (button && !pinned && popoverFor !== button) openHelp(button);
});
document.addEventListener("mouseout", (e) => {
  const button = e.target.closest("button.help");
  if (button && !pinned && popoverFor === button && !button.contains(e.relatedTarget)) closeHelp();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeHelp(); pinned = false; } });
window.addEventListener("hashchange", closeHelp);

// -- get started --------------------------------------------------------------
const STEP_HELP = {
  network: "Pick the network interface the Macs are plugged into. If your router hands out " +
    "addresses, BSDP only is enough for Mac OS X 10.2 and later; Full DHCP (NetBoot Macs only) " +
    "also covers 10.0/10.1 and the tray-loading iMac.",
  image: "Drop the install CD or DVD images (every disc of a CD set) on the Build page " +
    "from this computer, and press Build. It takes a few minutes per disc.",
  default: "The image a Mac boots when you hold N. A Mac can pick another one with the Option " +
    "key or in Startup Disk.",
  boot: "Turn the Mac on and hold N until a globe or spinning icon appears. Option shows a list " +
    "of images instead (on newer firmware). From Open Firmware: boot enet:<server address>,booter.",
};
let setupShown = "";

async function loadStart() {
  const data = await api("GET", "/api/setup");
  const key = JSON.stringify(data);
  if (key === setupShown) return data;
  setupShown = key;
  $("#start-count").textContent = `${data.done} of ${data.total} done`;
  const next = data.steps.find((step) => !step.done);
  $("#steps").replaceChildren(...data.steps.map((step, i) => el("li",
    { class: step.done ? "done" : (step === next ? "next" : "") },
    el("span", { class: "num" }, step.done ? "\u2713" : i + 1),
    el("strong", {}, step.title), " ", helpButton(STEP_HELP[step.id] || ""),
    el("span", { class: "state" }, step.state),
    el("a", { class: "go", href: `#${step.page}` }, `${step.action} \u2192`))));
  return data;
}

async function updateBanner() {
  const data = await api("GET", "/api/setup");
  const banner = $("#setup-banner");
  banner.hidden = data.done === data.total;
  banner.firstElementChild.textContent = `Get started: ${data.done} of ${data.total} steps done \u2192`;
}

// -- dashboard ------------------------------------------------------------------
let shownStatus = "";

async function loadDashboard() {
  const status = await api("GET", "/api/status");
  const key = JSON.stringify(status);
  if (key === shownStatus) return;
  shownStatus = key;
  updateBanner().catch(() => {});
  $("#lights").replaceChildren(...status.lights.map((l) => el("li", { title: l.detail },
    el("span", { class: `dot ${l.state}` }), el("strong", {}, l.name),
    el("span", { class: "note" }, l.detail))));
  $("#status").replaceChildren(...status.summary.map(([key, value]) =>
    el("tr", {}, el("th", {}, key), el("td", {}, value))));
  $("#warnings").replaceChildren(...(status.warnings.length ? status.warnings.reverse().map(logLine)
    : [el("li", { class: "note" }, "none")]));
}

// -- network ------------------------------------------------------------------
let network = null;

function fillAddresses(form, selected) {
  const iface = network.interfaces.find((i) => i.name === form.elements.namedItem("interface").value);
  const addresses = iface ? iface.addresses : [];
  form.elements.namedItem("server_ip").replaceChildren(
    el("option", { value: "" }, addresses.length ? `first (${addresses[0]})` : "none"),
    ...addresses.slice(1).map((a) => {
      const ip = a.split("/")[0];
      return el("option", { value: ip, selected: ip === selected }, a);
    }));
}

function showFullOptions(form) {
  const full = form.querySelector("input[name=dhcp_mode]:checked")?.value === "full";
  $("#full-only").hidden = !full;
}

async function loadNetwork() {
  network = await api("GET", "/api/network");
  const form = $("#net-form");
  const f = form.elements;
  const st = network.settings;
  f.namedItem("interface").replaceChildren(
    el("option", { value: "" }, "choose..."),
    ...network.interfaces.map((i) => el("option", { value: i.name, selected: i.name === st.interface },
      `${i.name}  ${i.addresses.join(", ") || "no IPv4 address"}${i.up ? "" : " (down)"}`)));
  fillAddresses(form, st.server_ip);
  for (const radio of form.querySelectorAll("input[name=dhcp_mode]")) radio.checked = radio.value === st.dhcp_mode;
  f.namedItem("dhcp_clients").value = st.dhcp_clients;
  for (const key of ["pool_start", "pool_end", "router"]) f.namedItem(key).value = st[key] || "";
  f.namedItem("lease_time").value = st.lease_time;
  f.namedItem("dns").value = st.dns.join(", ");
  showFullOptions(form);
  showNetworkState(network);
}

function showNetworkState(data) {
  const state = $("#net-state");
  state.className = data.running ? "" : "l40";
  state.textContent = data.running ? "Running."
    : `Stopped: ${data.error || "no settings yet"}.`;
  if (data.command_line.length) {
    state.append(" ", el("span", { class: "note" }, "Some set on the command line"), " ",
      helpButton(`The serve command line sets ${data.command_line.join(", ").replaceAll("_", " ")}. ` +
        "Changes here apply now, but those options win again at the next start."));
  }
}

$("#net-form").addEventListener("change", (e) => {
  if (e.target.name === "interface") fillAddresses(e.currentTarget, "");
  if (e.target.name === "dhcp_mode") showFullOptions(e.currentTarget);
});

$("#probe").addEventListener("click", async (e) => {
  const iface = $("#net-form").elements.namedItem("interface").value;
  e.target.disabled = true;
  $("#probe-result").textContent = "probing (3 s)...";
  try {
    const { answers } = await api("POST", "/api/probe", { interface: iface || null });
    $("#probe-result").textContent = answers.length
      ? answers.map((a) => a.text).join("; ") : "no other DHCP server answered";
  } catch (err) {
    $("#probe-result").textContent = "";
  } finally {
    e.target.disabled = false;
  }
});

$("#net-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const f = form.elements;
  const settings = {
    interface: f.namedItem("interface").value, server_ip: f.namedItem("server_ip").value,
    dhcp_mode: form.querySelector("input[name=dhcp_mode]:checked")?.value || "bsdp-only",
    dhcp_clients: f.namedItem("dhcp_clients").value,
    pool_start: f.namedItem("pool_start").value.trim(), pool_end: f.namedItem("pool_end").value.trim(),
    router: f.namedItem("router").value.trim(), dns: f.namedItem("dns").value,
    lease_time: Number(f.namedItem("lease_time").value) || 3600,
  };
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  $("#net-result").textContent = "applying (checks for other DHCP servers first)...";
  $("#net-result").className = "";
  try {
    const result = await api("POST", "/api/network", { settings, force: f.namedItem("force").checked });
    showNetworkState(result);
    if (result.applied) {
      $("#net-result").textContent = "applied and saved";
      $("#force-row").hidden = true;
      f.namedItem("force").checked = false;
      shownStatus = "";
    } else {
      $("#net-result").textContent = result.apply_error;
      $("#net-result").className = "l40";
      $("#force-row").hidden = !result.probe_refusal;
    }
  } catch (err) {
    $("#net-result").textContent = "";
  } finally {
    button.disabled = false;
  }
});

// -- images ---------------------------------------------------------------------
let images = [];

function renderImages(data) {
  images = data.images;
  const cli = Object.entries(data.cli_defaults).map(([a, i]) => `${a}=${i}`).join(", ");
  $("#images-help").dataset.help = `Images live in ${data.images_root}; Reload picks up folders ` +
    "added or changed there. Enabled images are offered to the Macs; the default is what a Mac " +
    "boots when it hasn't chosen one." +
    (cli ? ` The serve command sets --default ${cli}, which wins again at the next start.` : "");
  $("#images-table tbody").replaceChildren(...data.images.map((img) => {
    const defaults = img.architectures.map((arch) => el("label", {},
      el("input", { type: "radio", name: `default-${arch}`, checked: data.defaults[arch] === img.index,
        disabled: !img.enabled,
        on: { change: () => api("POST", "/api/defaults", { arch, index: img.index }).then(renderImages) } }),
      ` ${arch} `));
    return el("tr", {},
      el("td", {}, img.index),
      el("td", {}, el("div", {}, img.name), el("div", { class: "note" }, `${img.dir}/`),
        img.problems.map((p) => el("div", { class: "problem" }, p))),
      el("td", {}, img.os_version || "-", img.install ? " installer" : ""),
      el("td", {}, img.architectures.join(", ")),
      el("td", {}, img.folder_root ? "NFS folder" : img.protocol),
      el("td", {}, size(img.size)),
      el("td", {}, el("input", { type: "checkbox", checked: img.enabled,
        on: { change: (e) => api("POST", `/api/images/${img.index}`, { enabled: e.target.checked })
          .then(renderImages, loadImages) } })),
      el("td", {}, defaults),
      el("td", {}, el("button", { on: { click: () => rename(img) } }, "Rename")));
  }));
}

async function rename(img) {
  const name = prompt("Name shown in Startup Disk and the Option-key picker:", img.name);
  if (name === null || name.trim() === "" || name === img.name) return;
  renderImages(await api("POST", `/api/images/${img.index}`, { name }));
}

const loadImages = async () => renderImages(await api("GET", "/api/images"));
$("#reload").addEventListener("click", async () => renderImages(await api("POST", "/api/reload")));

// -- build ----------------------------------------------------------------------
let shownJob = null;
const picked = new Map();          // name in the sources folder -> {name, size}
const DISC = /\.(iso|cdr|dmg|img|toast|dc42|image)$/i;
const enc = encodeURIComponent;

function renderPicked() {
  const box = $("#picked");
  for (const tick of document.querySelectorAll("#sources input[type=checkbox]")) {
    tick.checked = picked.has(tick.dataset.name);
  }
  if (!picked.size) { box.replaceChildren("none"); box.className = "note"; return; }
  box.className = "";
  box.replaceChildren(...[...picked].map(([name, f]) => el("span", { class: "chip" },
    `${f.name} (${size(f.size)})`,
    el("button", { type: "button", title: "remove", on: { click: () => {
      picked.delete(name);
      renderPicked();
    } } }, "×"))));
}

async function loadSources() {
  const data = await api("GET", "/api/sources");
  $("#uploaded").hidden = !data.sources.length;
  $("#sources").replaceChildren(...data.sources.map((f) => el("li", {}, el("label", {},
    el("input", { type: "checkbox", checked: picked.has(f.name), "data-name": f.name,
      on: { change: (e) => {
        if (e.target.checked) picked.set(f.name, f); else picked.delete(f.name);
        renderPicked();
      } } }),
    ` ${f.name} `, el("span", { class: "note" }, size(f.size))))));
}

// Uploads: each file goes up in 16 MiB chunks and resumes where the server left off.
const CHUNK = 16 * 1024 * 1024;
const uploadQueue = [];
let uploading = false;

function addFiles(files) {
  for (const file of files) {
    if (!DISC.test(file.name) || file.name.startsWith(".")) continue;
    const bar = el("progress", { max: 1, value: 0 });
    const state = el("span", { class: "note" }, "waiting");
    const item = { file, bar, state, cancelled: false };
    const cancel = el("button", { type: "button", on: { click: () => {
      item.cancelled = true; state.textContent = "cancelling"; } } }, "Cancel");
    item.row = el("li", {}, file.name, bar, state, " ", cancel);
    item.cancel = cancel;
    $("#uploads").append(item.row);
    uploadQueue.push(item);
  }
  runUploads();
}

async function runUploads() {
  if (uploading) return;
  uploading = true;
  try {
    while (uploadQueue.length) {
      const item = uploadQueue.shift();
      try { await upload(item); } catch (err) { item.state.textContent = `failed: ${err.message}`; item.state.className = "l40"; }
      item.cancel.remove();
    }
  } finally {
    uploading = false;
  }
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function upload(item) {
  const { file } = item;
  const name = file.name, total = file.size;
  let status = await api("GET", `/api/uploads?name=${enc(name)}`);
  if (status.done) {
    if (status.size !== total) throw new Error("a different file with this name is already on the server");
    item.bar.value = 1;
    item.state.textContent = "already on the server";
  } else {
    let offset = status.received > total ? 0 : status.received;
    if (status.received > total) await api("POST", `/api/uploads/cancel?name=${enc(name)}`);
    const started = Date.now(), startOffset = offset;
    let failures = 0;
    do {
      if (item.cancelled) {
        await api("POST", `/api/uploads/cancel?name=${enc(name)}`);
        item.state.textContent = "cancelled";
        return;
      }
      const end = Math.min(offset + CHUNK, total);
      let response;
      try {
        response = await fetch(`/api/uploads?name=${enc(name)}&offset=${offset}&total=${total}`, {
          method: "POST", body: file.slice(offset, end),
          headers: { "X-Netboot-UI": "1", "Content-Type": "application/octet-stream" } });
      } catch (err) {
        if (++failures > 5) throw err;
        item.state.textContent = "connection lost, retrying...";
        await pause(2000);
        offset = (await api("GET", `/api/uploads?name=${enc(name)}`)).received;
        continue;
      }
      if (response.status === 401) { location.href = "/login.html"; return; }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const expected = /expected offset (\d+)/.exec(data.error || "");
        if (response.status === 409 && expected) { offset = Number(expected[1]); continue; }
        throw new Error(data.error || `${response.status}`);
      }
      failures = 0;
      offset = data.received;
      item.bar.value = total ? offset / total : 1;
      const rate = (offset - startOffset) / Math.max((Date.now() - started) / 1000, 0.001);
      item.state.textContent = `${size(offset)} of ${size(total)}, ${size(rate)}/s`;
    } while (offset < total);
    item.state.textContent = `uploaded (${size(total)})`;
  }
  status = await api("GET", `/api/uploads?name=${enc(name)}`);
  picked.set(status.name, { name: status.name, size: total });
  renderPicked();
  if (current === "build") loadSources().catch(() => {});
}

// Folders dropped from Finder or a file manager arrive as directory entries.
async function filesFromEntry(entry, out) {
  if (entry.isFile) {
    out.push(await new Promise((resolve, reject) => entry.file(resolve, reject)));
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) await filesFromEntry(child, out);
    }
  }
}

const drop = $("#drop");
for (const type of ["dragenter", "dragover"]) {
  drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
}
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) { addFiles([...e.dataTransfer.files]); return; }
  const files = [];
  for (const entry of entries) await filesFromEntry(entry, files);
  addFiles(files);
});
// Dropping outside the box must not make the browser open the file instead.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());
$("#pick-files").addEventListener("click", () => $("#file-input").click());
$("#pick-folder").addEventListener("click", () => $("#folder-input").click());
for (const input of ["#file-input", "#folder-input"]) {
  $(input).addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
}
window.addEventListener("beforeunload", (e) => {
  if (uploading) { e.preventDefault(); e.returnValue = ""; }
});

async function loadBuilds() {
  const { builds } = await api("GET", "/api/builds");
  $("#builds-table tbody").replaceChildren(...builds.map((b) => el("tr", {},
    el("td", {}, b.id),
    el("td", { title: b.sources.join("\n") }, b.sources.map((s) => s.split("/").pop()).join(", ")),
    el("td", { class: b.state === "failed" ? "l40" : "" }, b.state),
    el("td", {}, time(b.started)),
    el("td", {}, b.last_line),
    el("td", {}, el("button", { on: { click: () => { shownJob = b.id; loadLog(); } } }, "Log")))));
  if (shownJob !== null) loadLog();
}

async function loadLog() {
  const job = await api("GET", `/api/builds/${shownJob}`);
  const pre = $("#build-log");
  const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 5;
  pre.hidden = false;
  pre.textContent = `Build ${job.id} (${job.state})\n` + job.log.join("\n");
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

$("#build-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!picked.size) { showError("Pick at least one disc image first."); return; }
  const f = e.target.elements;
  const job = await api("POST", "/api/builds", {
    sources: [...picked.keys()], name: f.namedItem("name").value.trim(),
    dir: f.namedItem("dir").value.trim(),
    index: f.namedItem("index").value ? Number(f.namedItem("index").value) : null,
    protocol: f.namedItem("protocol").value, force: f.namedItem("force").checked,
  });
  shownJob = job.id;
  loadBuilds();
});

// -- clients --------------------------------------------------------------------
let shownClients = "";

async function loadClients(force) {
  if (!images.length || force === true) await loadImages();
  const { clients } = await api("GET", "/api/clients");
  const body = $("#clients-table tbody");
  if (body.contains(document.activeElement)) return;   // don't redraw under an open menu
  const key = JSON.stringify([clients, images]);
  if (key === shownClients && force !== true) return;  // nothing new: keep the page still
  shownClients = key;
  body.replaceChildren(...(clients.length ? clients.map((c) => {
    const select = el("select", { on: { change: (e) => api("POST", `/api/clients/${c.mac}`,
      { image: e.target.value === "" ? null : Number(e.target.value) })
      .then(() => { e.target.blur(); loadClients(); }) } },
      el("option", { value: "" }, "- (boots the default)"),
      images.map((img) => el("option", { value: img.index, selected: c.selected_image === img.index },
        `${img.index} ${img.name}${img.enabled ? "" : " (disabled)"}`)));
    if (c.selected_image !== null && c.selected_image !== undefined &&
        !images.some((img) => img.index === c.selected_image)) {
      select.append(el("option", { value: c.selected_image, selected: true },
        `${c.selected_image} (missing: boots the default)`));
    }
    return el("tr", {},
      el("td", {}, c.mac),
      el("td", {}, c.model || "-", c.arch ? el("span", { class: "note" }, ` ${c.arch}`) : ""),
      el("td", {}, c.ip || "-"),
      el("td", {}, time(c.last_seen), c.last_message ? el("div", { class: "note" }, c.last_message) : ""),
      el("td", {}, c.activity ? [el("div", {}, c.activity.message),
        el("div", { class: "note" }, `${c.activity.subsystem}, ${clock(c.activity.time)}`)] : "-"),
      el("td", {}, select));
  }) : [el("tr", {}, el("td", { colSpan: 6, class: "note" }, "no Macs seen yet"))]));
}

// -- logs -----------------------------------------------------------------------
const MAX_LOG = 2000;
let lastSeq = 0;

function logLine(e) {
  return el("li", { class: `l${e.level}` }, el("time", {}, clock(e.time)),
    `${e.subsystem.padEnd(9)} ${e.client ? `[${e.client}] ` : ""}${e.message}`);
}

function visible(e) {
  const level = Number($("#log-level").value);
  const filter = $("#log-filter").value.trim().toLowerCase();
  if (e.level < level) return false;
  if (!filter) return true;
  return [e.message, e.subsystem, e.client || "", e.kind].some((s) => s.toLowerCase().includes(filter));
}

const allEvents = [];

function addEvent(e) {
  allEvents.push(e);
  if (allEvents.length > MAX_LOG * 2) allEvents.splice(0, allEvents.length - MAX_LOG * 2);
  if ($("#log-pause").checked || !visible(e)) return;
  const list = $("#log");
  list.prepend(logLine(e));
  while (list.children.length > MAX_LOG) list.lastChild.remove();
}

function redrawLog() {
  $("#log").replaceChildren(...allEvents.filter(visible).slice(-MAX_LOG).reverse().map(logLine));
}
$("#log-filter").addEventListener("input", redrawLog);
$("#log-level").addEventListener("change", redrawLog);
$("#log-pause").addEventListener("change", redrawLog);
$("#log-clear").addEventListener("click", () => { allEvents.length = 0; redrawLog(); });

let serverInstance = null;

function connect() {
  const source = new EventSource(`/api/events/stream?since=${lastSeq}`);
  source.onopen = () => { $("#conn").textContent = "live"; };
  // The server was restarted while this page was open: its event numbers
  // started again and everything shown is from the old one, so start over.
  source.addEventListener("hello", (msg) => {
    if (serverInstance !== null && msg.data !== serverInstance) location.reload();
    serverInstance = msg.data;
  });
  source.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    if (e.seq <= lastSeq) return;
    lastSeq = e.seq;
    addEvent(e);
    if (e.subsystem === "build" && current === "build" && e.kind !== "output") loadBuilds();
    if (e.subsystem === "catalog" && current === "images") loadImages();
  };
  source.onerror = () => {
    $("#conn").textContent = "disconnected, retrying...";
    source.close();
    setTimeout(connect, 3000);
  };
}

// -- navigation -----------------------------------------------------------------
const pages = {
  start: loadStart,
  dashboard: loadDashboard,
  network: loadNetwork,
  images: loadImages,
  build: () => Promise.all([loadSources(), loadBuilds()]),
  clients: () => loadClients(true),
  logs: async () => {},
};
const refresh = { start: loadStart, dashboard: loadDashboard, clients: loadClients, build: loadBuilds };
let current = null;
let firstShown = false;

function show() {
  current = (location.hash.slice(1) in pages) ? location.hash.slice(1) : "dashboard";
  if (!location.hash && !firstShown) {
    // First visit without a page in the address: open the guide until setup is done.
    firstShown = true;
    api("GET", "/api/setup").then((data) => {
      if (data.done < data.total && !location.hash) location.hash = "#start";
    }).catch(() => {});
  }
  firstShown = true;
  for (const section of document.querySelectorAll("main > section")) {
    section.classList.toggle("active", section.id === current);
  }
  for (const link of document.querySelectorAll("nav a")) {
    link.classList.toggle("active", link.getAttribute("href") === `#${current}`);
  }
  pages[current]().catch(() => {});
}

window.addEventListener("hashchange", show);
setInterval(() => { if (refresh[current] && !document.hidden) refresh[current]().catch(() => {}); }, 5000);
show();
connect();
api("GET", "/api/session").then((s) => { $("#logout").hidden = !s.login; }).catch(() => {});
