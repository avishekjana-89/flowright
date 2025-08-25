// popup.js - UI for the extension popup
const STORAGE_KEY = 'recorded_steps';

function $id(n) { return document.getElementById(n) }

// Global error capture to aid debugging inside the popup
try {
  window.addEventListener('error', (ev) => {
    try {
      console.error('Popup error captured', ev.error || ev.message, ev);
      const pre = document.getElementById('detailPre');
      if (pre) pre.textContent = 'Popup error: ' + (ev.error && ev.error.stack ? ev.error.stack : (ev.message || 'unknown'));
      const dlg = document.getElementById('detailModal'); if (dlg) dlg.style.display = 'flex';
    } catch (_) {/* swallow */ }
  });
  window.addEventListener('unhandledrejection', (ev) => {
    try {
      console.error('Popup unhandled rejection', ev.reason);
      const pre = document.getElementById('detailPre'); if (pre) pre.textContent = 'Unhandled rejection: ' + (ev.reason && ev.reason.stack ? ev.reason.stack : String(ev.reason));
      const dlg = document.getElementById('detailModal'); if (dlg) dlg.style.display = 'flex';
    } catch (_) {/* swallow */ }
  });
} catch (_) { }

// (responsive CSS injection removed for popup so it shows the standard table layout)

async function render() {
  const tbody = document.querySelector('#eventsTable tbody');
  if (!tbody) return; // nothing to render in unexpected DOM
  tbody.innerHTML = '';
  const res = await new Promise(r => chrome.storage.local.get([STORAGE_KEY], r));
  const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
  // ensure ascending: oldest first
  arr.forEach((it, idx) => {
    const tr = document.createElement('tr');

    const i = document.createElement('td'); i.textContent = idx + 1;

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
  document.getElementById('evtCount').textContent = arr.length;
}

document.addEventListener('DOMContentLoaded', () => {
  try {
    // helper to safely attach event listeners when elements exist
    function safeOn(id, ev, fn) {
      const el = document.getElementById(id);
      if (el && typeof el.addEventListener === 'function') el.addEventListener(ev, fn);
    }

    safeOn('startBtn', 'click', () => { chrome.runtime.sendMessage({ type: 'start' }, () => { render(); }); });
    safeOn('pauseBtn', 'click', () => { chrome.runtime.sendMessage({ type: 'pause' }, () => { render(); }); });
    safeOn('stopBtn', 'click', () => { chrome.runtime.sendMessage({ type: 'stop' }, () => { render(); }); });
    safeOn('clearBtn', 'click', () => { chrome.runtime.sendMessage({ type: 'clear' }, () => { render(); }); });
    safeOn('refreshBtn', 'click', () => { render(); });
    safeOn('dedupeBtn', 'click', () => { chrome.runtime.sendMessage({ type: 'dedupe' }, () => { render(); }); });
    // openWindowBtn removed from UI
    safeOn('downloadBtn', 'click', async () => {
      const res = await new Promise(r => chrome.storage.local.get([STORAGE_KEY], r));
      const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
      const data = JSON.stringify(arr, null, 2);
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

    safeOn('pinSiteBtn', 'click', () => {
      try {
        chrome.tabs.query({ active: true, currentWindow: true }, async (tabs) => {
          const tab = tabs && tabs[0];
          if (!tab || !tab.url) return;
          let origin;
          try { const url = new URL(tab.url); origin = url.origin; } catch (e) { return; }
          // toggle pinned
          chrome.storage.local.get(['pinned_origins'], res => {
            const arr = Array.isArray(res.pinned_origins) ? res.pinned_origins : [];
            if (arr.includes(origin)) {
              chrome.runtime.sendMessage({ type: 'unpin-site', origin }, () => {
                const btn = document.getElementById('pinSiteBtn'); if (btn) btn.textContent = 'Pin to site';
                try { if (tab && tab.id) chrome.tabs.sendMessage(tab.id, { type: 'close-sidebar' }); } catch (e) { }
              });
            } else {
              chrome.runtime.sendMessage({ type: 'pin-site', origin }, () => {
                const btn = document.getElementById('pinSiteBtn'); if (btn) btn.textContent = 'Unpin site';
                try { if (tab && tab.id) chrome.tabs.sendMessage(tab.id, { type: 'open-sidebar' }); } catch (e) { }
              });
            }
          });
        });
      } catch (e) { }
    });


    render();
    // no automatic refresh; rely on manual Refresh and runtime messages

    // set pin button state for current active tab
    try {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs && tabs[0];
        if (!tab || !tab.url) return;
        try {
          const url = new URL(tab.url); const origin = url.origin;
          chrome.storage.local.get(['pinned_origins'], res => {
            const arr = Array.isArray(res.pinned_origins) ? res.pinned_origins : [];
            const btn = document.getElementById('pinSiteBtn');
            if (arr.includes(origin)) {
              if (btn) btn.textContent = 'Unpin site';
              try { if (tab && tab.id) chrome.tabs.sendMessage(tab.id, { type: 'open-sidebar' }); } catch (e) { }
            } else {
              if (btn) btn.textContent = 'Pin to site';
            }
          });
        } catch (e) { }
      });
    } catch (e) { }

    safeOn('closeModal', 'click', () => { const dlg = document.getElementById('detailModal'); if (dlg) dlg.style.display = 'none'; });

  } catch (err) {
    try { console.error('Error during popup init:', err); } catch (_) { }
  }

  // update UI state from background and listen for immediate state changes
  chrome.runtime.sendMessage({ type: 'get-state' }, resp => {
    try { const state = resp && resp.state ? resp.state : (resp && resp.recording ? 'recording' : 'paused'); document.getElementById('statusText').textContent = state === 'recording' ? 'Recording' : (state === 'stopped' ? 'Stopped' : 'Paused'); } catch (e) { }
  });
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === 'set-recording') {
      const state = msg.state || (typeof msg.recording !== 'undefined' ? (msg.recording ? 'recording' : 'paused') : 'paused');
      document.getElementById('statusText').textContent = state === 'recording' ? 'Recording' : (state === 'stopped' ? 'Stopped' : 'Paused');
      render();
    }
  });
});
