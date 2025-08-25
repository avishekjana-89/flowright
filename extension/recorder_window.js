const STORAGE_KEY = 'recorded_steps';
function $id(n) { return document.getElementById(n) }
async function render() {
  const tbody = document.querySelector('#eventsTable tbody');
  tbody.innerHTML = '';
  const res = await new Promise(r => chrome.storage.local.get([STORAGE_KEY], r));
  const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
  arr.forEach((it, idx) => {
    const tr = document.createElement('tr');
    const i = document.createElement('td'); i.textContent = it.id || (idx + 1);
    const t = document.createElement('td'); t.textContent = new Date(it.timestamp || Date.now()).toLocaleTimeString();
    const a = document.createElement('td'); a.textContent = it.action || '';
  const s = document.createElement('td'); s.textContent = (it.selectors && it.selectors[0]) || it.selector || '';
    const v = document.createElement('td'); v.textContent = (it.value || '');
    const d = document.createElement('td');
    const btn = document.createElement('button'); btn.textContent = 'Details';
    btn.addEventListener('click', () => {
      try {
        document.getElementById('detailPre').textContent = JSON.stringify(it, null, 2);
        document.getElementById('detailModal').style.display = 'flex';
      } catch (e) { alert(JSON.stringify(it, null, 2)); }
    });
    d.appendChild(btn);
    const del = document.createElement('button'); del.textContent = 'Delete';
    del.addEventListener('click', () => {
      chrome.runtime.sendMessage({ type: 'delete-index', index: idx }, () => { render(); });
    });
    d.appendChild(del);
    tr.appendChild(i); tr.appendChild(t); tr.appendChild(a); tr.appendChild(s); tr.appendChild(v);
    tr.appendChild(d);
    tbody.appendChild(tr);
  });
  $id('evtCount').textContent = arr.length;
}

document.addEventListener('DOMContentLoaded', () => {
  $id('startBtn').addEventListener('click', () => { chrome.runtime.sendMessage({ type: 'start' }, () => { render(); }); });
  $id('pauseBtn').addEventListener('click', () => { chrome.runtime.sendMessage({ type: 'pause' }, () => { render(); }); });
  $id('stopBtn').addEventListener('click', () => { chrome.runtime.sendMessage({ type: 'stop' }, () => { render(); }); });
  $id('refreshBtn').addEventListener('click', () => { render(); });
  $id('dedupeBtn').addEventListener('click', () => { chrome.runtime.sendMessage({ type: 'dedupe' }, () => { render(); }); });
  $id('clearBtn').addEventListener('click', () => { chrome.runtime.sendMessage({ type: 'clear' }, () => { render(); }); });
  $id('downloadBtn').addEventListener('click', async () => {
    const res = await new Promise(r => chrome.storage.local.get([STORAGE_KEY], r));
    const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
    // ensure exported payload is sorted by id ascending
    const sorted = arr.slice().sort((a, b) => {
      const ai = (a && typeof a.id === 'number') ? a.id : Infinity;
      const bi = (b && typeof b.id === 'number') ? b.id : Infinity;
      return ai - bi;
    });
    const data = JSON.stringify(sorted, null, 2);
    const blob = new Blob([data], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    try {
      if (chrome && chrome.downloads && chrome.downloads.download) {
        chrome.downloads.download({ url, filename: 'recorded_steps.json' }, () => { setTimeout(() => URL.revokeObjectURL(url), 1500); });
      } else {
        const a = document.createElement('a'); a.href = url; a.download = 'recorded_steps.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1500);
      }
    } catch (e) {
      const a = document.createElement('a'); a.href = url; a.download = 'recorded_steps.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1500);
    }
  });

  render();
  document.getElementById('closeModal').addEventListener('click', () => {
    document.getElementById('detailModal').style.display = 'none';
  });
  // no automatic interval refresh; listen for recording state events
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === 'set-recording') {
      const state = msg.state || (typeof msg.recording !== 'undefined' ? (msg.recording ? 'recording' : 'paused') : 'paused');
      const statusText = state === 'recording' ? 'Recording' : (state === 'stopped' ? 'Stopped' : 'Paused');
      try { document.getElementById('statusText').textContent = statusText; } catch (e) { }
      render();
    }
  });
  chrome.runtime.sendMessage({ type: 'get-state' }, resp => { try { const state = resp && resp.state ? resp.state : (resp && resp.recording ? 'recording' : 'paused'); document.getElementById('statusText').textContent = state === 'recording' ? 'Recording' : (state === 'stopped' ? 'Stopped' : 'Paused'); } catch (e) { } });
});
