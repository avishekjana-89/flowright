// background.js (service worker)
// recordingState can be 'recording', 'paused', or 'stopped'
let recordingState = 'paused';
const STORAGE_KEY = 'recorded_steps';

// Create context menu items for assertions
try {
  chrome.runtime.onInstalled.addListener(() => {
    try {
      chrome.contextMenus.create({ id: 'hover', title: 'Mouse Hover', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'getText', title: 'Get TextContent', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'verify-text', title: 'Verify Element Text', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'verify-value', title: 'Verify Element Value', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'verify-displayed', title: 'Verify Element Visible', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'verify-disabled', title: 'Verify Element Disabled', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'verify-attribute', title: 'Verify Element Attribute', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'verify-page-title', title: 'Verify Page Title', contexts: ['all'] });
      chrome.contextMenus.create({ id: 'verify-element-count', title: 'Verify Element Count', contexts: ['all'] });
    } catch (e) { }
  });
  chrome.contextMenus.onClicked.addListener((info, tab) => {
    try {
      // If frameId is available, target that specific frame so its content script
      // instance (and its stored lastRightClicked) receives the message and can attach frameInfo.
      if (info && typeof info.frameId === 'number') {
        chrome.tabs.sendMessage(tab.id, { type: 'context-assert', id: info.menuItemId, info }, { frameId: info.frameId }, () => { /* ignore errors */ });
      } else {
        // frameId not available: broadcast to all frames in the tab so the frame
        // that captured the contextmenu will receive and handle the assert.
        chrome.tabs.sendMessage(tab.id, { type: 'context-assert', id: info.menuItemId, info }, () => { /* ignore errors */ });
      }
    } catch (e) { }
  });
} catch (e) { }

function normalize(payload) {
  // Map the content_script payload to the compact format requested
  try {
    const base = {
      selectorRef: (payload && (payload.selectorRef || payload.selectorRef === null)) ? payload.selectorRef : null,
      hash: (payload && (payload.hash || payload.hash === null)) ? payload.hash : null
    };

    // helper to build common object with frameInfo preserved
    // Allows caller to explicitly override `inIframe` by passing it in `obj`.
    const build = (obj) => ({
      ...base,
      ...obj,
      inIframe: (typeof obj !== 'undefined' && typeof obj.inIframe !== 'undefined') ? !!obj.inIframe : !!(payload && payload.inIframe),
      frameInfo: (payload && payload.frameInfo) || undefined,
      timestamp: (payload && payload.timestamp) || Date.now()
    });

    // extract resource part (path + query + hash) from a full URL string
    const resourceFrom = (u) => {
      try {
        if (!u) return '';
        const parsed = new URL(u);
        return parsed.href;
      } catch (e) {
        // fallback: if it's a relative URL or malformed, return as-is
        return u || '';
      }
    };

    // payloads now come with payload.type === 'ui' and payload.action === <click|fill|...>
    const incomingType = payload && payload.type;
    const incomingAction = payload && payload.action;
    const act = (incomingAction || incomingType || '').toString();

    if (act === 'goto') {
      // Use build so frameInfo / inIframe are preserved for goto events
      const full = payload.value || (payload.post && payload.post.url) || '';
      // force inIframe=false for top-level navigation events
      return build({
        type: 'ui',
        action: 'goto',
        url: resourceFrom(full),
        timestamp: payload.timestamp || Date.now()
      });
    }

    if (act === 'click') {
      const selectors = payload.selectors || [];
      return build({
        type: 'ui',
        action: 'click',
        selectors: selectors,
        selectorRef: payload.selectorRef || null,
        hash: payload.hash || null
      });
    }

    if (act === 'fill') {
      return build({
        type: 'ui',
        action: 'fill',
        selectors: payload.selectors || [],
        value: payload.value || '',
        selectorRef: payload.selectorRef || null,
        hash: payload.hash || null
      });
    }

    if (act === 'selectDate') {
      return build({
        type: 'ui',
        action: 'selectDate',
        selectors: payload.selectors || [],
        value: payload.value || '',
        selectorRef: payload.selectorRef || null,
        hash: payload.hash || null
      });
    }

    if (act === 'doubleClick') {
      return build({ type: 'ui', action: 'doubleClick', selectors: payload.selectors || [], value: payload.value || null, selectorRef: payload.selectorRef || null, hash: payload.hash || null });
    }

    if (act === 'check') {
      return build({ type: 'ui', action: 'check', selectors: payload.selectors || [], value: payload.value || true, selectorRef: payload.selectorRef || null, hash: payload.hash || null });
    }
    if (act === 'uncheck') {
      return build({ type: 'ui', action: 'uncheck', selectors: payload.selectors || [], value: payload.value || null, selectorRef: payload.selectorRef || null, hash: payload.hash || null });
    }

    // assertion/verification actions produced by context menu
    if (act === 'verifyText' || act === 'verifyElementText') {
      return build({ type: 'ui', action: 'verifyElementText', selectors: payload.selectors || [], value: payload.value || '', selectorRef: payload.selectorRef || null, hash: payload.hash || null });
    }
    if (act === 'verifyValue' || act === 'verifyElementValue') {
      return build({ type: 'ui', action: 'verifyElementValue', selectors: payload.selectors || [], value: payload.value || '', selectorRef: payload.selectorRef || null, hash: payload.hash || null });
    }
    if (act === 'verifyDisplayed' || act === 'verifyElementVisible') {
      return build({ type: 'ui', action: 'verifyElementVisible', selectors: payload.selectors || [], value: !!payload.value, selectorRef: payload.selectorRef || null, hash: payload.hash || null });
    }
    if (act === 'verifyDisabled' || act === 'verifyElementDisabled') {
      return build({ type: 'ui', action: 'verifyElementDisabled', selectors: payload.selectors || [], value: !!payload.value, selectorRef: payload.selectorRef || null, hash: payload.hash || null });
    }

    if (act === 'change' || (payload && payload.type === 'change')) {
      // infer more specific action from element tag/type
      const post = payload.post || {};
      const tag = (post.elementTag || '').toLowerCase();
      const etype = (post.elementType || '').toLowerCase();

      if (tag === 'select') {
        return build({ type: 'ui', action: 'selectDropdownByValue', selectors: payload.selectors || [], value: payload.value || '' });
      }
      if (tag === 'input' && (etype === 'checkbox' || etype === 'radio')) {
        // previously we emitted 'toggle' for checkable inputs; leave the change branch to fallback to 'change'
        // content_script now emits explicit 'check'/'uncheck' so we no longer normalize toggle here
      }
      if (tag === 'input' && etype === 'file') {
        return build({ type: 'ui', action: 'upload', selectors: payload.selectors || [], value: payload.value || '' });
      }
      if (tag === 'input' || tag === 'textarea' || post.isContentEditable) {
        return build({ type: 'ui', action: 'fill', selectors: payload.selectors || [], value: payload.value || '' });
      }

      return build({ type: 'ui', action: 'change', selectors: payload.selectors || [], value: payload.value || '' });
    }

    // fallback: keep minimal - prefer canonical {type: 'ui', action: <verb>} when possible
    const selectors = payload.selectors || [];
    return { ...base, type: (payload && payload.type) || 'ui', action: (payload && payload.action) || ((payload && payload.type && payload.type !== 'ui') ? payload.type : 'event'), selectors: selectors, value: payload && payload.value || '', timestamp: payload && payload.timestamp || Date.now() };
  } catch (e) {
    return { type: (payload && payload.type) || 'ui', action: (payload && payload.action) || ((payload && payload.type && payload.type !== 'ui') ? payload.type : 'event'), timestamp: Date.now() };
  }
}
function broadcastRecordingState() {
  const isRecording = recordingState === 'recording';
  chrome.tabs.query({}, tabs => {
    for (const t of tabs) {
      try { chrome.tabs.sendMessage(t.id, { type: 'set-recording', recording: isRecording, state: recordingState }); } catch (e) { }
    }
  });
  // also broadcast to extension pages (popup/window)
  try { chrome.runtime.sendMessage({ type: 'set-recording', recording: isRecording, state: recordingState }); } catch (e) { }
}

function updateBadge() {
  try {
    chrome.storage.local.get([STORAGE_KEY], res => {
      const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
      const txt = arr.length ? String(arr.length) : '';
      chrome.action.setBadgeText({ text: txt });
      chrome.action.setBadgeBackgroundColor({ color: recordingState === 'recording' ? '#d93025' : '#5f6368' });
    });
  } catch (e) { }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return;
  // pinned origins handling
  if (msg.type === 'pin-site') {
    const origin = msg.origin;
    if (!origin) return sendResponse({ ok: false });
    chrome.storage.local.get(['pinned_origins'], res => {
      const arr = Array.isArray(res.pinned_origins) ? res.pinned_origins : [];
      if (!arr.includes(origin)) arr.push(origin);
      chrome.storage.local.set({ pinned_origins: arr }, () => sendResponse({ ok: true }));
    });
    return true;
  }
  if (msg.type === 'unpin-site') {
    const origin = msg.origin;
    if (!origin) return sendResponse({ ok: false });
    chrome.storage.local.get(['pinned_origins'], res => {
      let arr = Array.isArray(res.pinned_origins) ? res.pinned_origins : [];
      arr = arr.filter(x => x !== origin);
      chrome.storage.local.set({ pinned_origins: arr }, () => sendResponse({ ok: true }));
    });
    return true;
  }
  if (msg.type === 'should-open-sidebar') {
    const origin = msg.origin || '';
    chrome.storage.local.get(['pinned_origins'], res => {
      const arr = Array.isArray(res.pinned_origins) ? res.pinned_origins : [];
      sendResponse({ open: arr.includes(origin) });
    });
    return true;
  }
  if (msg.type === 'recorder-event') {
    const payload = msg.payload || {};
    // DEBUG: log incoming payload so we can verify frameInfo presence
    try { console.debug('[recorder-event] incoming payload keys:', Object.keys(payload), payload); } catch (e) { }
    // save to storage (append)
    // Ignore scroll events entirely — do not normalize or persist them
    if (payload && (payload.type === 'scroll' || payload.action === 'scroll')) {
      try { console.debug('[recorder-event] ignoring scroll event'); } catch (e) { }
      return true;
    }
    const norm = normalize(payload);
    // Use sender.frameId only to infer that the event came from a subframe, but do NOT persist frame/tab ids
    try {
      if (sender && typeof sender.frameId !== 'undefined') {
        // If the content script didn't detect iframe context, infer it from sender.frameId (non-zero means a subframe)
        if (!norm.inIframe && typeof sender.frameId === 'number' && sender.frameId !== 0) {
          norm.inIframe = true;
        }
        try { console.debug('[recorder-event] sender.frameId=', sender.frameId); } catch (e) { }

        // If this is a goto event coming from a subframe, ignore it — we only want top-level navigations
        if (norm && (norm.action === 'goto' || norm.type === 'goto') && typeof sender.frameId === 'number' && sender.frameId !== 0) {
          try { console.debug('[recorder-event] ignoring subframe goto', norm); } catch (e) { }
          return true;
        }
      }
      // If the payload indicates it's from an iframe but frameInfo is missing, try to request frameInfo
      // from the specific frame (if sender.frameId is present). This helps for right-click/assert flows
      // where the content script may not have attached frameInfo earlier.
      if (norm && norm.inIframe && (!norm.frameInfo || !norm.frameInfo.length) && sender && typeof sender.tab !== 'undefined' && typeof sender.frameId === 'number' && sender.frameId !== 0) {
        try {
          const req = { type: 'request-frame-info' };
          // send a message to the originating frame to ask for its frameInfo; expect a response
          chrome.tabs.sendMessage(sender.tab.id, req, { frameId: sender.frameId }, resp => {
            try {
              if (resp && Array.isArray(resp.frameInfo) && resp.frameInfo.length) {
                norm.frameInfo = resp.frameInfo;
              }
            } catch (e) { }
            // continue to store below (we don't block the main flow)
            chrome.storage.local.get([STORAGE_KEY], res2 => {
              const arr2 = Array.isArray(res2[STORAGE_KEY]) ? res2[STORAGE_KEY] : [];
              const migrated2 = arr2.map(it => {
                if (!it) return it;
                let copy = it;
                if (copy.action === 'ui' && copy.type && copy.type !== 'ui') {
                  copy = { ...copy, type: 'ui', action: copy.type };
                }
                if (!copy.type && copy.action) {
                  copy = { ...copy, type: 'ui' };
                }
                if (!copy.action && copy.type && copy.type !== 'ui') {
                  copy = { ...copy, action: copy.type, type: 'ui' };
                }
                return copy;
              });
              migrated2.push(norm);
              migrated2.forEach((it, i) => { try { it.id = i + 1 } catch (e) { } });
              chrome.storage.local.set({ [STORAGE_KEY]: migrated2 }, () => updateBadge());
            });
          });
          return true; // we handled storing asynchronously via the callback
        } catch (e) { /* fall through to normal store */ }
      }

      // Do not store sender.tab.id or sender.frameId on the normalized record to avoid leaking runtime ids
    } catch (e) { }
    chrome.storage.local.get([STORAGE_KEY], res => {
      const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
      // ensure legacy items have type/action
      const migrated = arr.map(it => {
        if (!it) return it;
        let copy = it;
        // legacy swapped records: type held the action and action === 'ui'
        if (copy.action === 'ui' && copy.type && copy.type !== 'ui') {
          copy = { ...copy, type: 'ui', action: copy.type };
        }
        // if only action present, ensure type is 'ui'
        if (!copy.type && copy.action) {
          copy = { ...copy, type: 'ui' };
        }
        // if only type present and it looks like an action, normalize to {type: 'ui', action: <type>}
        if (!copy.action && copy.type && copy.type !== 'ui') {
          copy = { ...copy, action: copy.type, type: 'ui' };
        }
        return copy;
      });
      // Append normalized record; do not merge actions by element hash.
      migrated.push(norm);
      // assign stable incremental ids starting at 1
      migrated.forEach((it, i) => { try { it.id = i + 1 } catch (e) { } });
      chrome.storage.local.set({ [STORAGE_KEY]: migrated }, () => updateBadge());
    });
  }
  else if (msg.type === 'start') {
    recordingState = 'recording';
    broadcastRecordingState();
    // notify tabs that Start was explicitly triggered so content scripts can act once
    try {
      chrome.tabs.query({}, tabs => {
        for (const t of tabs) {
          try { chrome.tabs.sendMessage(t.id, { type: 'recording-started' }); } catch (e) { }
        }
      });
    } catch (e) { }
    updateBadge();
    sendResponse({ ok: true, state: recordingState });
  }
  else if (msg.type === 'pause') {
    recordingState = 'paused';
    broadcastRecordingState();
    updateBadge();
    sendResponse({ ok: true, state: recordingState });
  }
  else if (msg.type === 'stop') {
    recordingState = 'stopped';
    broadcastRecordingState();
    updateBadge();
    sendResponse({ ok: true, state: recordingState });
  }
  else if (msg.type === 'clear') {
    chrome.storage.local.set({ [STORAGE_KEY]: [] }, () => updateBadge());
    sendResponse({ ok: true });
  }
  else if (msg.type === 'delete-index') {
    const idx = msg.index;
    chrome.storage.local.get([STORAGE_KEY], res => {
      let arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
      if (typeof idx === 'number' && idx >= 0 && idx < arr.length) {
        arr.splice(idx, 1);
        // reassign ids after deletion so ids remain contiguous starting at 1
        arr.forEach((it, i) => { try { it.id = i + 1 } catch (e) { } });
        chrome.storage.local.set({ [STORAGE_KEY]: arr }, () => updateBadge());
      }
      sendResponse({ ok: true });
    });
    return true;
  }
  else if (msg.type === 'dedupe') {
    // fuzzy dedupe using normalized (action, selector, value) and keep last occurrence
    chrome.storage.local.get([STORAGE_KEY], res => {
      const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];

      function normalizeSelector(sel) {
        if (!sel) return '';
        try {
          let s = sel.toString().trim().toLowerCase();
          // remove nth-of-type and nth-child
          s = s.replace(/:nth-of-type\(\d+\)/g, '');
          s = s.replace(/:nth-child\(\d+\)/g, '');
          // collapse consecutive whitespace and tidy '>' spacing
          s = s.replace(/\s*>\s*/g, ' > ');
          s = s.replace(/\s+/g, ' ');
          // shorten very long selector chains to last 4 segments
          const parts = s.split('>').map(p => p.trim()).filter(Boolean);
          if (parts.length > 4) {
            s = parts.slice(-4).join(' > ');
          } else {
            s = parts.join(' > ');
          }
          return s;
        } catch (e) { return sel.toString(); }
      }

      function normalizeValue(v) {
        if (v === undefined || v === null) return '';
        try {
          let x = v.toString().trim();
          // case-insensitive normalize for typical text values
          x = x.toLowerCase();
          return x;
        } catch (e) { return String(v); }
      }

      const seen = new Set();
      const outReversed = [];
      for (let i = arr.length - 1; i >= 0; --i) {
        const it = arr[i] || {};
        // prefer the `action` property (newer schema uses type='ui' and action=<verb>), fall back to type
        const typeProp = (it.action || '').toString();
        const selectorRaw = ((it.selectors && it.selectors[0]) || it.selector || '').toString();
        const valueRaw = (it.value || '').toString();
        // derive a lightweight frame identifier: prefer first frameSelector if available
        let frameIdPart = '';
        try {
          if (it && Array.isArray(it.frameInfo) && it.frameInfo.length) {
            const f = it.frameInfo[0];
            if (f && f.frameSelector) frameIdPart = String(f.frameSelector).toLowerCase();
          } else if (it && it.inIframe) {
            frameIdPart = '[in-iframe]';
          } else {
            frameIdPart = '[top]';
          }
        } catch (e) { frameIdPart = '' }
        // always preserve goto actions (don't dedupe them)
        if (typeProp === 'goto') {
          outReversed.push(it);
          continue;
        }
        const key = `${typeProp}||${frameIdPart}||${normalizeSelector(selectorRaw)}||${normalizeValue(valueRaw)}`;
        if (!seen.has(key)) {
          seen.add(key);
          outReversed.push(it);
        }
      }
      const out = outReversed.reverse();
      // reassign ids after dedupe so ids remain contiguous starting at 1
      out.forEach((it, i) => { try { it.id = i + 1 } catch (e) { } });
      chrome.storage.local.set({ [STORAGE_KEY]: out }, () => updateBadge());
      sendResponse({ ok: true });
    });
    return true;
  }
  else if (msg.type === 'get-state') {
    sendResponse({ recording: recordingState === 'recording', state: recordingState });
  }
  return true;
});

// initialize badge on service worker start
updateBadge();

// run a lightweight migration to fix legacy records that lack a type/action
try {
  chrome.storage.local.get([STORAGE_KEY], res => {
    const arr = Array.isArray(res[STORAGE_KEY]) ? res[STORAGE_KEY] : [];
    let changed = false;
    const fixed = arr.map(it => {
      if (!it) return it;
      let copy = it;
      // if legacy record used type to store action and action is 'ui', swap them
      if (copy.action === 'ui' && copy.type && copy.type !== 'ui') {
        copy = { ...copy, type: 'ui', action: copy.type }; changed = true;
      }
      // ensure we always have type set to 'ui' for recorded UI events
      if (!copy.type) { copy = { ...copy, type: 'ui' }; changed = true; }
      if (!copy.action) { copy = { ...copy, action: 'event' }; changed = true; }
      return copy;
    });
    // ensure all items have contiguous ids starting at 1
    fixed.forEach((it, i) => { try { if (!it || typeof it.id !== 'number' || it.id !== i + 1) { it.id = i + 1; changed = true } } catch (e) { } });
    if (changed) { chrome.storage.local.set({ [STORAGE_KEY]: fixed }, () => updateBadge()); }
  });
} catch (e) { }

// watch for persistent recorder window closed and clear stored id
try {
  chrome.windows.onRemoved.addListener((winId) => {
    chrome.storage.local.get(['recorder_window_id'], res => {
      if (res && res.recorder_window_id === winId) {
        chrome.storage.local.remove('recorder_window_id');
      }
    });
  });
} catch (e) { }
