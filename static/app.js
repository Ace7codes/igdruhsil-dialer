// Visark dialer — browser softphone logic.
// Auth: the page itself is behind nginx basic auth, so fetch() to same-origin
// endpoints carries credentials automatically.

let device = null;
let activeCall = null;
let timerId = null;
let callStart = null;

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const statusText = $("status-text");

function setStatus(state) {
  statusEl.className = "status " + state;
  statusText.textContent = state;
}

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function fmt(secs) {
  const m = String(Math.floor(secs / 60)).padStart(2, "0");
  const s = String(secs % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function startTimer() {
  callStart = Date.now();
  $("call-timer").textContent = "00:00";
  timerId = setInterval(() => {
    $("call-timer").textContent = fmt(Math.floor((Date.now() - callStart) / 1000));
  }, 1000);
}

function stopTimer() {
  clearInterval(timerId);
  timerId = null;
}

function showCallPanel({ who, incoming }) {
  $("call-who").textContent = who || "—";
  show($("call-panel"));
  hide($("dial-panel"));
  if (incoming) {
    show($("accept-btn"));
    show($("reject-btn"));
    hide($("hangup-btn"));
    hide($("mute-btn"));
  } else {
    hide($("accept-btn"));
    hide($("reject-btn"));
    show($("hangup-btn"));
    show($("mute-btn"));
  }
}

function clearCallPanel() {
  stopTimer();
  hide($("call-panel"));
  show($("dial-panel"));
  activeCall = null;
  $("mute-btn").textContent = "Mute";
  loadRecent();
}

function wireCall(call) {
  activeCall = call;
  call.on("accept", () => {
    setStatus("on-call");
    hide($("accept-btn"));
    hide($("reject-btn"));
    show($("hangup-btn"));
    show($("mute-btn"));
    startTimer();
  });
  call.on("disconnect", () => { setStatus("ready"); clearCallPanel(); });
  call.on("cancel", () => { setStatus("ready"); clearCallPanel(); });
  call.on("reject", () => { setStatus("ready"); clearCallPanel(); });
  call.on("error", (e) => { console.error("call error", e); setStatus("ready"); clearCallPanel(); });
}

async function initDevice() {
  setStatus("connecting");
  let token;
  try {
    ({ token } = await fetch("/api/token").then((r) => r.json()));
  } catch (e) {
    console.error("token fetch failed", e);
    setStatus("offline");
    return;
  }

  device = new Twilio.Device(token, { codecPreferences: ["opus", "pcmu"], logLevel: "warn" });

  device.on("registered", () => { setStatus("ready"); $("dial-btn").disabled = false; });
  device.on("unregistered", () => setStatus("offline"));
  device.on("error", (e) => { console.error("device error", e); setStatus("offline"); });

  // Refresh the token before it expires.
  device.on("tokenWillExpire", async () => {
    try {
      const { token: fresh } = await fetch("/api/token").then((r) => r.json());
      device.updateToken(fresh);
    } catch (e) { console.error("token refresh failed", e); }
  });

  device.on("incoming", (call) => {
    const from = call.parameters.From || "unknown";
    showCallPanel({ who: `Incoming: ${from}`, incoming: true });
    setStatus("ringing");
    wireCall(call);
    $("accept-btn").onclick = () => call.accept();
    $("reject-btn").onclick = () => call.reject();
  });

  await device.register();
}

async function dial() {
  const to = $("number").value.trim();
  if (!to || !device) return;
  showCallPanel({ who: `Calling: ${to}`, incoming: false });
  setStatus("connecting");
  const call = await device.connect({ params: { To: to } });
  wireCall(call);
}

async function loadRecent() {
  let rows;
  try {
    rows = await fetch("/api/calls").then((r) => r.json());
  } catch { return; }
  const body = $("recent-body");
  body.innerHTML = "";
  for (const c of rows) {
    const tr = document.createElement("tr");
    const dir = (c.direction || "").includes("inbound") ? "in" : "out";
    const num = dir === "in" ? (c.from || "") : (c.to || "");
    tr.innerHTML =
      `<td>${dir}</td><td>${num}</td><td>${c.status || ""}</td>` +
      `<td>${c.duration || ""}</td><td>${c.timestamp || ""}</td>`;
    body.appendChild(tr);
  }
}

async function loadMessages() {
  let rows;
  try {
    rows = await fetch("/api/messages").then((r) => r.json());
  } catch { return; }
  const body = $("messages-body");
  body.innerHTML = "";
  for (const m of rows) {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const td2 = document.createElement("td");
    td1.textContent = m.from || "";
    td2.textContent = m.body || "";
    tr.append(td1, td2);
    body.appendChild(tr);
  }
}

$("dial-btn").onclick = dial;
$("hangup-btn").onclick = () => activeCall && activeCall.disconnect();
$("mute-btn").onclick = () => {
  if (!activeCall) return;
  const muted = !activeCall.isMuted();
  activeCall.mute(muted);
  $("mute-btn").textContent = muted ? "Unmute" : "Mute";
};
$("number").addEventListener("keydown", (e) => { if (e.key === "Enter") dial(); });

initDevice();
loadRecent();
loadMessages();
setInterval(loadRecent, 5000);
setInterval(loadMessages, 5000);
