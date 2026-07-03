const cards = document.getElementById('cards');
const statusEl = document.getElementById('status');
const url = new URLSearchParams(location.search).get('ws') || `ws://${location.hostname}:8706/ws`;
let ws;
function receipt(intent, event) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ui_intent_id:intent.ui_intent_id, delivery_id:intent.delivery_id, event, observed_at:new Date().toISOString(), local_track_state:{}, source:'companion-web'}));
}
function render(intent) {
  const el = document.createElement('article'); el.className = 'card';
  el.innerHTML = `<b>${intent.component}</b><p>${intent.content?.message || JSON.stringify(intent.content)}</p><small>${intent.truth_level} · priority ${intent.priority}</small><br>`;
  const btn = document.createElement('button'); btn.textContent = 'dismiss'; btn.onclick = () => receipt(intent, 'dismissed');
  el.appendChild(btn); cards.prepend(el); receipt(intent, 'displayed');
}
function connect(){ ws = new WebSocket(url); ws.onopen=()=>statusEl.textContent='connected'; ws.onclose=()=>{statusEl.textContent='disconnected'; setTimeout(connect,1000)}; ws.onmessage=e=>render(JSON.parse(e.data)); }
connect();
