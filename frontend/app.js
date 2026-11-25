/* MedCare Voice AI Doctor Appointment Dashboard + Phone Call Interface */

const TARGET_SAMPLE_RATE = 16000;

const state = {
  dashboard: { doctors: [], appointments: [] },
  selectedDate: "all",
  highlightApptId: null,
  highlightType: null,
  highlightSlotId: null,
  callState: "idle", // idle | ringing | connected | ended
  muted: false,
  callStartMs: 0,
  timerInterval: null,
  wsVoice: null,
  wsEvents: null,
  micStream: null,
  inputAudioCtx: null,
  inputSource: null,
  inputProcessor: null,
  inputAnalyser: null,
  outputAudioCtx: null,
  outputAnalyser: null,
  nextPlayTime: 0,
  activeSources: new Set(),
  ringInterval: null,
  waveRaf: null,
  transcript: [],
  interimUserText: "",
};

const els = {
  healthDot: document.getElementById("health-dot"),
  healthLabel: document.getElementById("health-label"),
  tryCallBtn: document.getElementById("try-call-btn"),
  reopenPhoneBtn: document.getElementById("reopen-phone-btn"),
  metricDoctors: document.getElementById("metric-doctors"),
  metricOpenSlots: document.getElementById("metric-open-slots"),
  metricBooked: document.getElementById("metric-booked"),
  dateFilters: document.getElementById("date-filters"),
  doctorsList: document.getElementById("doctors-list"),
  appointmentsCount: document.getElementById("appointments-count"),
  appointmentsTbody: document.getElementById("appointments-tbody"),
  dashboardTranscript: document.getElementById("dashboard-transcript"),
  dashboardCallState: document.getElementById("dashboard-call-state"),
  phoneBackdrop: document.getElementById("phone-backdrop"),
  phoneDevice: document.querySelector(".phone-device"),
  phoneTimer: document.getElementById("phone-timer"),
  phoneCallStatus: document.getElementById("phone-call-status"),
  vadPill: document.getElementById("vad-pill"),
  waveformCanvas: document.getElementById("waveform-canvas"),
  confirmationSlot: document.getElementById("confirmation-slot"),
  phoneTranscript: document.getElementById("phone-transcript"),
  muteBtn: document.getElementById("mute-btn"),
  muteLabel: document.getElementById("mute-label"),
  micOnIcon: document.getElementById("mic-on-icon"),
  micOffIcon: document.getElementById("mic-off-icon"),
  endCallBtn: document.getElementById("end-call-btn"),
  phoneEndedActions: document.getElementById("phone-ended-actions"),
  callAgainBtn: document.getElementById("call-again-btn"),
  closePhoneBtn: document.getElementById("close-phone-btn"),
  toastRegion: document.getElementById("toast-region"),
};

// ---------------- Utility Formatting ----------------

function formatTime12h(time24) {
  if (!time24 || !time24.includes(":")) return time24 || "";
  const [hStr, mStr] = time24.split(":");
  let h = parseInt(hStr, 10);
  const suffix = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return `${h}:${mStr} ${suffix}`;
}

function formatDateShort(isoDate) {
  if (!isoDate) return "";
  const [y, m, d] = isoDate.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  return dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function showToast(text) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = text;
  els.toastRegion.appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

// ---------------- Sound Effects (Web Audio API) ----------------

function playRingPulse() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.001, now);
    gain.gain.exponentialRampToValueAtTime(0.06, now + 0.04);
    gain.gain.setValueAtTime(0.06, now + 0.65);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.72);
    gain.connect(ctx.destination);

    [440, 480].forEach((freq) => {
      const osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, now);
      osc.connect(gain);
      osc.start(now);
      osc.stop(now + 0.74);
    });
    setTimeout(() => ctx.close().catch(() => {}), 900);
  } catch {
    // Ignore audio context autoplay restrictions
  }
}

function startRingTone() {
  stopRingTone();
  playRingPulse();
  state.ringInterval = setInterval(playRingPulse, 1800);
}

function stopRingTone() {
  if (state.ringInterval) {
    clearInterval(state.ringInterval);
    state.ringInterval = null;
  }
}

function playEndCallSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    const notes = [523.25, 392.0];
    notes.forEach((freq, idx) => {
      const t = now + idx * 0.11;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, t);
      gain.gain.setValueAtTime(0.07, t);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.18);
    });
    setTimeout(() => ctx.close().catch(() => {}), 600);
  } catch {
    // Ignore
  }
}

// ---------------- Dashboard Data & Rendering ----------------

async function fetchHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    const ok = data?.keys_configured?.elevenlabs && data?.keys_configured?.gemini;
    els.healthDot.className = `dot ${ok ? "dot-online" : "dot-warn"}`;
    els.healthLabel.textContent = ok ? "Pipecat · Silero VAD" : "API Keys Needed";
  } catch {
    els.healthDot.className = "dot dot-warn";
    els.healthLabel.textContent = "Server Offline";
  }
}

async function fetchDashboard() {
  const res = await fetch("/api/dashboard");
  if (!res.ok) return;
  const data = await res.json();
  applyDashboardData(data);
}

function applyDashboardData(data) {
  state.dashboard = data || { doctors: [], appointments: [] };
  renderMetrics();
  renderDateFilters();
  renderDoctors();
  renderAppointments();
}

function renderMetrics() {
  const doctors = state.dashboard.doctors || [];
  const appointments = state.dashboard.appointments || [];
  let openSlots = 0;
  doctors.forEach((doc) => {
    (doc.slots || []).forEach((s) => {
      if (!s.is_booked) openSlots += 1;
    });
  });
  const activeBooked = appointments.filter((a) => a.status === "booked").length;

  els.metricDoctors.textContent = String(doctors.length);
  els.metricOpenSlots.textContent = String(openSlots);
  els.metricBooked.textContent = String(activeBooked);
  els.appointmentsCount.textContent = String(appointments.length);
}

function renderDateFilters() {
  const dateSet = new Set();
  (state.dashboard.doctors || []).forEach((doc) => {
    (doc.slots || []).forEach((slot) => dateSet.add(slot.date));
  });
  const dates = Array.from(dateSet).sort();

  els.dateFilters.innerHTML = "";
  const makeBtn = (value, label) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `filter-pill ${state.selectedDate === value ? "active" : ""}`;
    btn.textContent = label;
    btn.addEventListener("click", () => {
      state.selectedDate = value;
      renderDateFilters();
      renderDoctors();
    });
    return btn;
  };

  els.dateFilters.appendChild(makeBtn("all", "All Dates"));
  dates.forEach((d, idx) => {
    const label = idx === 0 ? `Today · ${formatDateShort(d)}` : formatDateShort(d);
    els.dateFilters.appendChild(makeBtn(d, label));
  });
}

function renderDoctors() {
  const doctors = state.dashboard.doctors || [];
  if (!doctors.length) {
    els.doctorsList.innerHTML = `<div class="empty-state">No doctors available.</div>`;
    return;
  }

  els.doctorsList.innerHTML = "";
  doctors.forEach((doc) => {
    const card = document.createElement("article");
    card.className = "doctor-card";

    const filteredSlots = (doc.slots || []).filter(
      (s) => state.selectedDate === "all" || s.date === state.selectedDate
    );
    const byDay = new Map();
    filteredSlots.forEach((slot) => {
      if (!byDay.has(slot.date)) byDay.set(slot.date, []);
      byDay.get(slot.date).push(slot);
    });

    const openCount = filteredSlots.filter((s) => !s.is_booked).length;

    const head = document.createElement("div");
    head.className = "doctor-card-head";
    head.innerHTML = `
      <div>
        <div class="doctor-name">${doc.name}</div>
      </div>
      <div class="doctor-meta">
        <span class="specialty-tag">${doc.specialty}</span>
        <span class="room-tag">${doc.room} · ${openCount} open</span>
      </div>
    `;
    card.appendChild(head);

    const daysWrap = document.createElement("div");
    daysWrap.className = "slots-by-day";

    for (const [day, slots] of byDay.entries()) {
      const row = document.createElement("div");
      row.className = "day-row";

      const label = document.createElement("span");
      label.className = "day-label";
      label.textContent = formatDateShort(day);
      row.appendChild(label);

      const pills = document.createElement("div");
      pills.className = "slot-pills";
      slots.forEach((slot) => {
        const pill = document.createElement("span");
        const isUpdated = state.highlightSlotId === slot.id;
        pill.className = `slot-pill ${slot.is_booked ? "booked" : ""} ${isUpdated ? "slot-updated" : ""}`;
        pill.textContent = formatTime12h(slot.time);
        pill.title = slot.is_booked ? "Booked" : "Available";
        pills.appendChild(pill);
      });
      row.appendChild(pills);
      daysWrap.appendChild(row);
    }

    card.appendChild(daysWrap);
    els.doctorsList.appendChild(card);
  });
}

function renderAppointments() {
  const appts = state.dashboard.appointments || [];
  if (!appts.length) {
    els.appointmentsTbody.innerHTML = `<tr><td colspan="4" class="empty-state">No appointments yet.</td></tr>`;
    return;
  }

  els.appointmentsTbody.innerHTML = "";
  appts.forEach((appt) => {
    const tr = document.createElement("tr");
    if (state.highlightApptId === appt.id) {
      tr.className =
        state.highlightType === "cancel_appointment"
          ? "highlight-cancelled"
          : "highlight-booked";
    }

    tr.innerHTML = `
      <td class="phone-cell">${appt.caller_phone}</td>
      <td>
        <div class="doc-cell-title">${appt.doctor_name}</div>
        <div class="doc-cell-sub">${appt.specialty}</div>
      </td>
      <td class="schedule-cell">${formatDateShort(appt.date)} · ${formatTime12h(appt.time)}</td>
      <td><span class="status-badge ${appt.status}">${appt.status}</span></td>
    `;
    els.appointmentsTbody.appendChild(tr);
  });
}

function handleDbUpdate(msg) {
  const appt = msg.appointment || {};
  state.highlightApptId = appt.id || null;
  state.highlightSlotId = appt.slot_id || null;
  state.highlightType = msg.action || null;

  if (msg.dashboard) {
    applyDashboardData(msg.dashboard);
  } else {
    fetchDashboard();
  }

  renderConfirmationCard(msg.action, appt);

  const label =
    msg.action === "cancel_appointment"
      ? `Cancelled: ${appt.doctor_name || "Appointment"}`
      : `Booked: ${appt.doctor_name || "Appointment"} at ${formatTime12h(appt.time)}`;
  showToast(label);

  setTimeout(() => {
    state.highlightApptId = null;
    state.highlightSlotId = null;
    state.highlightType = null;
    renderDoctors();
    renderAppointments();
  }, 3800);
}

function renderConfirmationCard(action, appt) {
  if (!appt || !appt.doctor_name) return;
  const isCancel = action === "cancel_appointment";
  els.confirmationSlot.hidden = false;
  els.confirmationSlot.innerHTML = `
    <div class="confirm-card ${isCancel ? "cancelled" : ""}">
      <div class="confirm-card-title">${isCancel ? "Appointment Cancelled" : "Booking Confirmed"}</div>
      <div class="confirm-card-main">${appt.doctor_name} · ${appt.specialty || ""}</div>
      <div class="confirm-card-meta">${formatDateShort(appt.date)} · ${formatTime12h(appt.time)} · ${appt.caller_phone || ""}</div>
    </div>
  `;
}

// ---------------- Live Events WebSocket ----------------

function connectDashboardEvents() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  state.wsEvents = ws;

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "dashboard_snapshot" && msg.dashboard) {
        applyDashboardData(msg.dashboard);
      } else if (msg.type === "db_update") {
        handleDbUpdate(msg);
      }
    } catch {
      // Ignore malformed messages
    }
  };

  ws.onclose = () => {
    setTimeout(connectDashboardEvents, 3000);
  };
}

// ---------------- Transcript Rendering ----------------

function renderTranscript() {
  const items = [...state.transcript];
  if (state.interimUserText) {
    items.push({ role: "user", text: state.interimUserText, interim: true });
  }

  // Phone transcript
  els.phoneTranscript.innerHTML = "";
  if (!items.length) {
    els.phoneTranscript.innerHTML = `<div class="bubble bubble-system">Connecting audio pipeline...</div>`;
  } else {
    items.forEach((item) => {
      const div = document.createElement("div");
      if (item.role === "system") {
        div.className = "bubble bubble-system";
      } else {
        div.className = `bubble ${item.role === "user" ? "bubble-user" : "bubble-assistant"} ${item.interim ? "interim" : ""}`;
      }
      div.textContent = item.text;
      els.phoneTranscript.appendChild(div);
    });
    els.phoneTranscript.scrollTop = els.phoneTranscript.scrollHeight;
  }

  // Dashboard transcript mirror
  els.dashboardTranscript.innerHTML = "";
  if (!items.length) {
    els.dashboardTranscript.innerHTML = `<div class="empty-state">Click <strong>Try Call</strong> to speak with Aria.</div>`;
  } else {
    items
      .filter((item) => item.role !== "system")
      .forEach((item) => {
        const row = document.createElement("div");
        row.className = `dash-line ${item.role}`;
        row.innerHTML = `
          <div class="dash-line-role">${item.role === "user" ? "Caller" : "Aria (AI)"}</div>
          <div></div>
        `;
        row.lastElementChild.textContent = item.text;
        els.dashboardTranscript.appendChild(row);
      });
    els.dashboardTranscript.scrollTop = els.dashboardTranscript.scrollHeight;
  }
}

// ---------------- Audio Capture, Playback & Waveform ----------------

function downsampleTo16k(float32Array, inputSampleRate) {
  if (inputSampleRate === TARGET_SAMPLE_RATE) {
    const out = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
      const s = Math.max(-1, Math.min(1, float32Array[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out.buffer;
  }

  const ratio = inputSampleRate / TARGET_SAMPLE_RATE;
  const newLength = Math.floor(float32Array.length / ratio);
  const out = new Int16Array(newLength);
  for (let i = 0; i < newLength; i++) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const s0 = float32Array[idx] || 0;
    const s1 = float32Array[idx + 1] ?? s0;
    const sample = s0 + (s1 - s0) * frac;
    const clamped = Math.max(-1, Math.min(1, sample));
    out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
  }
  return out.buffer;
}

function playIncomingPcm(arrayBuffer) {
  if (!state.outputAudioCtx) return;
  if (state.outputAudioCtx.state === "suspended") {
    state.outputAudioCtx.resume().catch(() => {});
  }

  const int16 = new Int16Array(arrayBuffer);
  if (!int16.length) return;
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 0x8000;
  }

  const audioBuffer = state.outputAudioCtx.createBuffer(1, float32.length, TARGET_SAMPLE_RATE);
  audioBuffer.copyToChannel(float32, 0);

  const source = state.outputAudioCtx.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(state.outputAnalyser);

  const now = state.outputAudioCtx.currentTime;
  if (state.nextPlayTime < now) {
    state.nextPlayTime = now + 0.02;
  }
  source.start(state.nextPlayTime);
  state.nextPlayTime += audioBuffer.duration;

  state.activeSources.add(source);
  source.onended = () => {
    state.activeSources.delete(source);
  };
}

function clearPlaybackQueue() {
  for (const src of state.activeSources) {
    try {
      src.stop();
    } catch {
      // Ignore already stopped
    }
  }
  state.activeSources.clear();
  if (state.outputAudioCtx) {
    state.nextPlayTime = state.outputAudioCtx.currentTime;
  }
}

function drawWaveform() {
  const canvas = els.waveformCanvas;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const barCount = 34;
  const gap = 4;
  const barWidth = (w - gap * (barCount - 1)) / barCount;

  const getBins = (analyser) => {
    if (!analyser) return null;
    const arr = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(arr);
    return arr;
  };

  const outBins = getBins(state.outputAnalyser);
  const inBins = state.muted ? null : getBins(state.inputAnalyser);

  let outEnergy = 0;
  if (outBins) {
    for (let i = 0; i < 24; i++) outEnergy += outBins[i];
  }
  const activeBins = outEnergy > 140 ? outBins : inBins;
  const isBotSpeaking = outEnergy > 140;

  for (let i = 0; i < barCount; i++) {
    let norm = 0.08;
    if (state.callState === "connected" && activeBins) {
      const idx = Math.floor((i / barCount) * Math.min(48, activeBins.length));
      norm = Math.max(0.1, activeBins[idx] / 255);
    } else if (state.callState === "ringing") {
      norm = 0.12 + 0.14 * Math.abs(Math.sin(Date.now() / 240 + i * 0.35));
    }

    const barH = Math.max(4, norm * (h - 8));
    const x = i * (barWidth + gap);
    const y = (h - barH) / 2;

    ctx.fillStyle =
      state.callState === "ended"
        ? "rgba(251, 113, 133, 0.45)"
        : isBotSpeaking
        ? "#38bdf8"
        : "#2dd4bf";

    ctx.beginPath();
    if (typeof ctx.roundRect === "function") {
      ctx.roundRect(x, y, barWidth, barH, 3);
      ctx.fill();
    } else {
      ctx.fillRect(x, y, barWidth, barH);
    }
  }

  state.waveRaf = requestAnimationFrame(drawWaveform);
}

// ---------------- Call Lifecycle ----------------

function setCallState(nextState, statusLabel) {
  state.callState = nextState;
  els.phoneDevice.dataset.callState = nextState;
  els.phoneCallStatus.textContent = statusLabel;
  els.dashboardCallState.textContent =
    nextState === "idle"
      ? "No active call"
      : `${statusLabel} (${els.phoneTimer.textContent})`;

  const isConnected = nextState === "connected";
  const isEnded = nextState === "ended";

  els.muteBtn.disabled = !isConnected;
  els.endCallBtn.disabled = isEnded;
  els.phoneEndedActions.hidden = !isEnded;
}

function startCallTimer() {
  stopCallTimer();
  state.callStartMs = Date.now();
  els.phoneTimer.textContent = "00:00";
  state.timerInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - state.callStartMs) / 1000);
    const mins = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const secs = String(elapsed % 60).padStart(2, "0");
    els.phoneTimer.textContent = `${mins}:${secs}`;
  }, 500);
}

function stopCallTimer() {
  if (state.timerInterval) {
    clearInterval(state.timerInterval);
    state.timerInterval = null;
  }
}

async function setupAudioPipeline() {
  state.micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });

  state.inputAudioCtx = new (window.AudioContext || window.webkitAudioContext)({
    sampleRate: TARGET_SAMPLE_RATE,
  });
  state.inputSource = state.inputAudioCtx.createMediaStreamSource(state.micStream);
  state.inputAnalyser = state.inputAudioCtx.createAnalyser();
  state.inputAnalyser.fftSize = 128;

  state.inputProcessor = state.inputAudioCtx.createScriptProcessor(512, 1, 1);
  state.inputProcessor.onaudioprocess = (event) => {
    if (
      state.muted ||
      state.callState !== "connected" ||
      !state.wsVoice ||
      state.wsVoice.readyState !== WebSocket.OPEN
    ) {
      return;
    }
    const inputData = event.inputBuffer.getChannelData(0);
    const pcmBuffer = downsampleTo16k(inputData, state.inputAudioCtx.sampleRate);
    state.wsVoice.send(pcmBuffer);
  };

  // Connect source -> analyser -> processor -> muted gain -> destination so script processor runs cleanly
  const silentGain = state.inputAudioCtx.createGain();
  silentGain.gain.value = 0;
  state.inputSource.connect(state.inputAnalyser);
  state.inputAnalyser.connect(state.inputProcessor);
  state.inputProcessor.connect(silentGain);
  silentGain.connect(state.inputAudioCtx.destination);

  state.outputAudioCtx = new (window.AudioContext || window.webkitAudioContext)({
    sampleRate: TARGET_SAMPLE_RATE,
  });
  state.outputAnalyser = state.outputAudioCtx.createAnalyser();
  state.outputAnalyser.fftSize = 128;
  state.outputAnalyser.connect(state.outputAudioCtx.destination);
  state.nextPlayTime = state.outputAudioCtx.currentTime;
}

function teardownAudioPipeline() {
  clearPlaybackQueue();
  if (state.inputProcessor) {
    state.inputProcessor.disconnect();
    state.inputProcessor = null;
  }
  if (state.inputSource) {
    state.inputSource.disconnect();
    state.inputSource = null;
  }
  if (state.micStream) {
    state.micStream.getTracks().forEach((t) => t.stop());
    state.micStream = null;
  }
  if (state.inputAudioCtx) {
    state.inputAudioCtx.close().catch(() => {});
    state.inputAudioCtx = null;
  }
  if (state.outputAudioCtx) {
    state.outputAudioCtx.close().catch(() => {});
    state.outputAudioCtx = null;
  }
  state.inputAnalyser = null;
  state.outputAnalyser = null;
}

async function startCall() {
  if (state.callState === "ringing" || state.callState === "connected") return;

  state.muted = false;
  updateMuteUI();
  state.transcript = [];
  state.interimUserText = "";
  els.confirmationSlot.hidden = true;
  els.confirmationSlot.innerHTML = "";
  els.reopenPhoneBtn.hidden = true;

  els.phoneBackdrop.hidden = false;
  els.phoneBackdrop.setAttribute("aria-hidden", "false");
  setCallState("ringing", "Ringing MedCare Reception...");
  renderTranscript();
  startRingTone();

  if (!state.waveRaf) {
    state.waveRaf = requestAnimationFrame(drawWaveform);
  }

  try {
    await setupAudioPipeline();
  } catch (err) {
    stopRingTone();
    setCallState("ended", "Microphone permission denied");
    state.transcript.push({
      role: "system",
      text: `Could not access microphone: ${err.message || err}`,
    });
    renderTranscript();
    return;
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/voice`);
  ws.binaryType = "arraybuffer";
  state.wsVoice = ws;

  ws.onopen = () => {
    // Keep ringback briefly for a natural pickup feel, then transition to connected
    setTimeout(() => {
      if (state.wsVoice === ws && ws.readyState === WebSocket.OPEN) {
        stopRingTone();
        setCallState("connected", "Connected · Listening");
        startCallTimer();
        state.transcript.push({
          role: "system",
          text: "Connected to Aria · Speak naturally",
        });
        renderTranscript();
      }
    }, 850);
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      playIncomingPcm(event.data);
      return;
    }

    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "interruption") {
        clearPlaybackQueue();
        els.vadPill.textContent = "Interrupted";
        return;
      }

      if (msg.type === "status") {
        const labels = {
          connected: "Connected · Listening",
          listening: "Connected · Listening",
          thinking: "Aria is checking...",
          speaking: "Aria is speaking",
        };
        if (state.callState === "connected" && labels[msg.state]) {
          els.phoneCallStatus.textContent = labels[msg.state];
          els.vadPill.textContent =
            msg.state === "speaking"
              ? "TTS Audio"
              : msg.state === "thinking"
              ? "Gemini LLM"
              : "Silero VAD";
        }
        return;
      }

      if (msg.type === "transcript") {
        if (!msg.final) {
          state.interimUserText = msg.text || "";
        } else {
          if (msg.role === "user") state.interimUserText = "";
          if (msg.text) {
            state.transcript.push({ role: msg.role, text: msg.text });
          }
        }
        renderTranscript();
        return;
      }

      if (msg.type === "tool_call") {
        const friendly = {
          check_doctor_availability: "Checking available doctor slots...",
          book_appointment: "Booking appointment...",
          cancel_appointment: "Cancelling appointment...",
        };
        state.transcript.push({
          role: "system",
          text: friendly[msg.name] || `Running ${msg.name}...`,
        });
        renderTranscript();
        return;
      }

      if (msg.type === "db_update") {
        handleDbUpdate(msg);
        return;
      }

      if (msg.type === "error") {
        state.transcript.push({
          role: "system",
          text: `Pipeline error: ${msg.message}`,
        });
        renderTranscript();
      }
    } catch {
      // Ignore non-JSON text frames
    }
  };

  ws.onerror = () => {
    stopRingTone();
    if (state.callState !== "ended") {
      endCall(false);
      state.transcript.push({
        role: "system",
        text: "Connection error. Please check server logs and .env API keys.",
      });
      renderTranscript();
    }
  };

  ws.onclose = () => {
    if (state.callState === "ringing" || state.callState === "connected") {
      endCall(false);
    }
  };
}

function endCall(playTone = true) {
  stopRingTone();
  stopCallTimer();

  if (state.wsVoice) {
    const ws = state.wsVoice;
    state.wsVoice = null;
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      ws.close();
    }
  }

  teardownAudioPipeline();
  if (playTone) playEndCallSound();

  if (state.callState !== "ended") {
    setCallState("ended", `Call Ended · ${els.phoneTimer.textContent}`);
    state.transcript.push({ role: "system", text: "Call ended" });
    renderTranscript();
  }
}

function updateMuteUI() {
  els.muteBtn.setAttribute("aria-pressed", String(state.muted));
  els.muteLabel.textContent = state.muted ? "Unmute" : "Mute";
  els.micOnIcon.hidden = state.muted;
  els.micOffIcon.hidden = !state.muted;
}

function closePhoneModal() {
  if (state.callState === "ringing" || state.callState === "connected") {
    endCall(true);
  }
  els.phoneBackdrop.hidden = true;
  els.phoneBackdrop.setAttribute("aria-hidden", "true");
  els.reopenPhoneBtn.hidden = state.transcript.length === 0;
}

// ---------------- Event Bindings ----------------

els.tryCallBtn.addEventListener("click", startCall);
els.callAgainBtn.addEventListener("click", startCall);
els.endCallBtn.addEventListener("click", () => endCall(true));
els.closePhoneBtn.addEventListener("click", closePhoneModal);
els.reopenPhoneBtn.addEventListener("click", () => {
  els.phoneBackdrop.hidden = false;
  els.phoneBackdrop.setAttribute("aria-hidden", "false");
});

els.muteBtn.addEventListener("click", () => {
  state.muted = !state.muted;
  updateMuteUI();
});

els.phoneBackdrop.addEventListener("click", (e) => {
  if (e.target === els.phoneBackdrop && state.callState === "ended") {
    closePhoneModal();
  }
});

// ---------------- Init ----------------

fetchHealth();
fetchDashboard();
connectDashboardEvents();
drawWaveform();
