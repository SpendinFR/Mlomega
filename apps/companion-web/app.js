const cards = document.getElementById('cards');
const statusEl = document.getElementById('status');
const url = new URLSearchParams(location.search).get('ws') || `ws://${location.hostname}:8706/ws`;
let ws;
function receipt(intent, event) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ui_intent_id:intent.ui_intent_id, delivery_id:intent.delivery_id, event, observed_at:new Date().toISOString(), local_track_state:{}, source:'companion-web'}));
}
// E35 §1: play a bounded WAV blob pushed as a `tts_audio` message. The base64 is
// decoded to a Blob and played once; nothing is cached (companion/phone mode).
function playTtsAudio(msg) {
  try {
    const bin = atob(msg.audio_b64 || '');
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const blob = new Blob([bytes], {type: 'audio/wav'});
    const audioUrl = URL.createObjectURL(blob);
    const audio = new Audio(audioUrl);
    audio.onended = () => URL.revokeObjectURL(audioUrl);
    audio.play().catch(() => {});
  } catch (e) { /* honest no-op if the browser blocks autoplay */ }
}
// E35 §2: a `virtual_screen` replay renders the ordered frame refs as an <img>
// slideshow the viewer can step through.
function renderReplay(intent) {
  const c = intent.content || {};
  const frames = (c.frames || []).map(f => f.ref || f.path).filter(Boolean);
  const el = document.createElement('article'); el.className = 'card replay';
  const counts = c.counts || {};
  el.innerHTML = `<b>replay</b> <small>${counts.keyframes||0} images · ${counts.clips||0} clips · ${counts.events||0} events</small>`;
  if (frames.length) {
    const img = document.createElement('img'); img.style.maxWidth = '100%'; img.src = frames[0];
    let i = 0; el.appendChild(document.createElement('br')); el.appendChild(img);
    if (frames.length > 1) {
      const next = document.createElement('button'); next.textContent = 'next';
      next.onclick = () => { i = (i + 1) % frames.length; img.src = frames[i]; };
      el.appendChild(next);
    }
  }
  cards.prepend(el); receipt(intent, 'displayed');
}
function render(intent) {
  if (intent.type === 'tts_audio') { playTtsAudio(intent); return; }
  if (intent.component === 'virtual_screen' && intent.content?.kind === 'replay') { renderReplay(intent); return; }
  const el = document.createElement('article'); el.className = 'card';
  el.innerHTML = `<b>${intent.component}</b><p>${intent.content?.summary || intent.content?.message || JSON.stringify(intent.content)}</p><small>${intent.truth_level} · priority ${intent.priority}</small><br>`;
  const btn = document.createElement('button'); btn.textContent = 'dismiss'; btn.onclick = () => receipt(intent, 'dismissed');
  el.appendChild(btn); cards.prepend(el); receipt(intent, 'displayed');
}
function connect(){ ws = new WebSocket(url); ws.onopen=()=>statusEl.textContent='connected'; ws.onclose=()=>{statusEl.textContent='disconnected'; setTimeout(connect,1000)}; ws.onmessage=e=>render(JSON.parse(e.data)); }
connect();
