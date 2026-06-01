// Visark dialer — browser softphone logic.
// Auth: the page is behind a single-user form login; the session cookie rides
// on same-origin fetch() automatically. A 401 means the session expired.

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

// --- Ringtone: synthesized dual-tone ring (no audio file). Web Audio needs a
// prior user gesture, so we create/resume the context on any click. ---
let audioCtx = null;
let ringTimer = null;

function unlockAudio() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) audioCtx = new Ctx();
  }
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
}
window.addEventListener("click", unlockAudio);

function ringBurst() {
  if (!audioCtx || audioCtx.state !== "running") return;
  const now = audioCtx.currentTime;
  const gain = audioCtx.createGain();
  gain.gain.value = 0.0001;
  gain.connect(audioCtx.destination);
  [440, 480].forEach((f) => {            // classic North-American ring pair
    const osc = audioCtx.createOscillator();
    osc.frequency.value = f;
    osc.connect(gain);
    osc.start(now);
    osc.stop(now + 2);                    // 2s of tone
  });
  // soft envelope so it doesn't click on/off
  gain.gain.exponentialRampToValueAtTime(0.18, now + 0.05);
  gain.gain.setValueAtTime(0.18, now + 1.9);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 2);
}

function startRinging() {
  unlockAudio();
  if (ringTimer) return;
  ringBurst();
  ringTimer = setInterval(ringBurst, 6000);  // 2s ring + 4s silence cadence
}

function stopRinging() {
  if (ringTimer) { clearInterval(ringTimer); ringTimer = null; }
}

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
  stopRinging();
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
    stopRinging();
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

async function ensureMicAccess() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    console.error("getUserMedia unavailable — needs a secure (HTTPS) context");
    return false;
  }
  try {
    // Triggers the browser's mic permission prompt and unlocks device labels.
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    return true;
  } catch (e) {
    console.error("microphone access denied/unavailable", e);
    return false;
  }
}

async function fetchToken() {
  const res = await fetch("/api/token", { cache: "no-store" });
  if (res.status === 401) {
    window.location.href = "/login";   // session expired → sign in again
    return null;
  }
  return (await res.json()).token;
}

async function initDevice() {
  unlockAudio();                         // this "Go online" click is our audio gesture
  const goBtn = $("go-online-btn");
  if (goBtn) goBtn.disabled = true;
  setStatus("connecting");

  // Calls need a mic — demand it up front so you can't sit at "ready" with
  // audio silently blocked.
  if (!(await ensureMicAccess())) {
    setStatus("offline");
    statusText.textContent = "allow mic + retry";
    if (goBtn) goBtn.disabled = false;
    return;
  }

  let token;
  try {
    token = await fetchToken();
  } catch (e) {
    console.error("token fetch failed", e);
    setStatus("offline");
    if (goBtn) goBtn.disabled = false;
    return;
  }
  if (!token) { if (goBtn) goBtn.disabled = false; return; }

  device = new Twilio.Device(token, { codecPreferences: ["opus", "pcmu"], logLevel: "warn" });
  // We play our own ringtone (more reliable than the SDK's, which depends on
  // output-device enumeration). Disable the SDK's to avoid a double ring.
  try { device.audio.incoming(false); } catch (e) { /* older SDK */ }

  device.on("registered", () => {
    setStatus("ready");
    $("dial-btn").disabled = false;
    hide($("connect-panel"));
  });
  device.on("unregistered", () => {
    setStatus("offline");
    show($("connect-panel"));
    if (goBtn) goBtn.disabled = false;
  });
  device.on("error", (e) => {
    console.error("device error", e);
    setStatus("offline");
    show($("connect-panel"));
    if (goBtn) goBtn.disabled = false;
  });

  // Refresh the token before it expires.
  device.on("tokenWillExpire", async () => {
    const fresh = await fetchToken();
    if (fresh) device.updateToken(fresh);
  });

  device.on("incoming", (call) => {
    const from = call.parameters.From || "unknown";
    showCallPanel({ who: `Incoming: ${from}`, incoming: true });
    setStatus("ringing");
    startRinging();
    wireCall(call);
    $("accept-btn").onclick = () => call.accept();   // stops ring via accept handler
    $("reject-btn").onclick = () => call.reject();   // stops ring via clearCallPanel
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
  // Collapse the raw event stream to one row per call: rows are most-recent
  // first, so the first time we see a call_sid is its latest status.
  const seen = new Set();
  for (const c of rows) {
    if (!c.call_sid || seen.has(c.call_sid)) continue;
    seen.add(c.call_sid);
    const inbound = (c.direction || "").includes("inbound");
    const number = inbound ? c.from : c.to;
    if (!number || number.startsWith("client:")) continue;  // hide internal browser leg
    const td = (t) => { const e = document.createElement("td"); e.textContent = t; return e; };
    const dirCell = td(inbound ? "Received" : "Called");
    dirCell.className = inbound ? "dir-in" : "dir-out";
    const statusCell = td(c.status || "");
    if (["missed", "no-answer", "busy", "failed"].includes(c.status)) {
      statusCell.className = "missed";
    }
    const tr = document.createElement("tr");
    tr.append(dirCell, td(number), statusCell, td(c.timestamp || ""));
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

$("go-online-btn").onclick = initDevice;
loadRecent();
loadMessages();
setInterval(loadRecent, 5000);
setInterval(loadMessages, 5000);
