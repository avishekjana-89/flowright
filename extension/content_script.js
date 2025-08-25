// content_script.js - injects event listeners and forwards events to the extension background
(function () {
  let recording = false;
  // remember the last input/textarea that received focus (used to map datepicker popups back to their input)
  let lastInteractedInput = null;
  // de-dupe map to avoid sending the same selectDate repeatedly for the same input
  const lastActionTs = new WeakMap();
  // debounce map for input elements -> { timeout, lastValue }
  const inputDebounces = new WeakMap();
  // store a snapshot captured before the user starts typing (to preserve pre-value locators)
  const preSnapshots = new WeakMap();
  // remember last element that was right-clicked (for context-menu assertions)
  let lastRightClicked = null;

  function flushPendingInput(el, sendChange = false) {
    if (!el) return;
    const rec = inputDebounces.get(el);
    if (rec && rec.timeout) {
      clearTimeout(rec.timeout);
      inputDebounces.delete(el);
      try {
        const val = rec.lastValue !== undefined ? rec.lastValue : (el.value || el.textContent || '');
        sendPayload(sendChange ? 'change' : 'fill', el, val);
      } catch (e) {/* swallow */ }
    }
  }

  function cssPath(el) {
    if (!el) return null;
    if (el.id && !/^\d/.test(el.id)) return `#${el.id}`;

    const parts = [];
    while (el && el.nodeType === 1 && el.tagName !== 'HTML') {
      let part = el.tagName.toLowerCase();

      if (el.className && typeof el.className === 'string') {
        const classes = el.className.trim().split(/\s+/).filter(Boolean);
        if (classes.length > 0) {
          part += `.${classes[0]}`;
        }
      }

      const parent = el.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        if (siblings.length > 1) {
          const idx = siblings.indexOf(el) + 1;
          part += `:nth-of-type(${idx})`;
        }
      }

      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(' > ');
  }

  // Walk up the frame chain and collect info for each hosting iframe (stops on cross-origin)
  function getFrameChainInfo(el) {
    const frames = [];
    try {
      let win = el && el.ownerDocument && el.ownerDocument.defaultView;

      // helper to build a robust selector for an iframe element in its parent
      function frameSelectorFor(frameEl) {
        try {
          if (!frameEl) return null;
          // prefer id
          if (frameEl.id && !/^\d/.test(frameEl.id)) return `#${frameEl.id}`;
          // prefer name attribute
          if (frameEl.getAttribute && frameEl.getAttribute('name')) return `iframe[name="${frameEl.getAttribute('name')}"]`;
          // data attributes
          const dataTest = frameEl.getAttribute && (frameEl.getAttribute('data-testid') || frameEl.getAttribute('data-test') || frameEl.getAttribute('data-test-id'));
          if (dataTest) return `iframe[data-testid="${dataTest}"]`;
          // src attr
          const src = frameEl.getAttribute && frameEl.getAttribute('src');
          if (src) return `iframe[src="${src}"]`;
          // fallback: compute nth-of-type among parent's iframe children, optionally prefix with parent's cssPath
          const parent = frameEl.parentElement;
          if (parent) {
            const iframes = Array.from(parent.children).filter(n => n.tagName === 'IFRAME');
            const idx = iframes.indexOf(frameEl);
            if (idx >= 0) {
              const nth = `iframe:nth-of-type(${idx + 1})`;
              try {
                const parentPath = cssPath(parent);
                if (parentPath) return `${parentPath} > ${nth}`;
              } catch (e) { /* ignore */ }
              return nth;
            }
          }
        } catch (e) { /* ignore */ }
        return null;
      }

      while (win && win !== window.top) {
        const frameEl = win.frameElement;
        if (!frameEl) break;
        let sameOrigin = true;
        let parentWin = null;
        try { parentWin = win.parent; } catch (err) { sameOrigin = false; }
        const src = (frameEl.getAttribute && frameEl.getAttribute('src')) || null;
        const rect = frameEl.getBoundingClientRect ? frameEl.getBoundingClientRect() : null;

        const selector = frameSelectorFor(frameEl) || ((typeof cssPath === 'function') ? cssPath(frameEl) : null);

        frames.push({
          selector,
          src,
          rect: rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null,
          sameOrigin
        });
        if (!sameOrigin) break;
        win = parentWin;
      }
    } catch (e) { /* swallow, return what we have */ }
    return frames;
  }


  function isUniqueLocator(selector, targetElement) {
    try {
      // Helper: normalize visible text for comparison
      function normalizeText(t) {
        try { return (t || '').toString().replace(/\s+/g, ' ').trim(); } catch (e) { return '' }
      }
      // XPATH selectors are prefixed with `xpath=`. Validate using document.evaluate
      if (selector.startsWith('xpath=')) {
        const xpath = selector.slice('xpath='.length);
        try {
          const snap = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
          if (!snap) return false;
          return snap.snapshotLength === 1 && snap.snapshotItem(0) === targetElement;
        } catch (e) {
          return false; // invalid xpath
        }
      }

      // For other selectors, use querySelectorAll to check uniqueness
      const matches = document.querySelectorAll(selector);
      // Must match exactly 1 element AND that element must be our target
      return matches.length === 1 && matches[0] === targetElement;
    } catch (e) {
      return false; // Invalid selector or other error
    }
  }

  // Helper to produce a valid XPath string-literal for arbitrary text.
  // Ensures quotes inside the string are handled using concat() when needed.
  function xpathLiteral(s) {
    if (s === null || typeof s === 'undefined') return '""';
    s = String(s);
    if (s.indexOf('"') === -1) return '"' + s + '"';
    if (s.indexOf("'") === -1) return "'" + s + "'";
    const parts = s.split('"');
    const pieces = [];
    for (let i = 0; i < parts.length; i++) {
      pieces.push('"' + parts[i] + '"');
      if (i !== parts.length - 1) pieces.push("'" + '"' + "'");
    }
    return 'concat(' + pieces.join(',') + ')';
  }

  // Safely stringify values (handles circular refs and functions) for transport/logging
  function safeStringify(obj) {
    try {
      const seen = new WeakSet();
      return JSON.stringify(obj, function (k, v) {
        if (typeof v === 'function') return String(v);
        if (v && typeof v === 'object') {
          if (seen.has(v)) return '[Circular]';
          seen.add(v);
        }
        return v;
      });
    } catch (e) {
      try { return String(obj); } catch (_) { return '[[unserializable]]'; }
    }
  }

  // Helper: compute a reasonably robust XPath for an element.
  // Returns a string like 'xpath=//*[@id="foo"]' or an absolute path as fallback.
  function elementXPath(el) {
    if (!el || el.nodeType !== 1) return null;

    // If element has an id, prefer attribute-based xpath which is short and robust
    try {
      if (el.id && !/^\d/.test(el.id)) {
        return `xpath=//*[@id="${el.id}"]`;
      }
    } catch (e) { /* ignore */ }

    // If element has a unique combination of tag + name or other attribute, try it
    try {
      const tag = el.tagName.toLowerCase();
      if (el.getAttribute) {
        const name = el.getAttribute('name');
        if (name) return `xpath=//${tag}[@name="${name}"]`;
        const dataTest = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-test-id');
        if (dataTest) return `xpath=//${tag}[@data-testid="${dataTest}"]`;
      }
    } catch (e) { /* ignore */ }

    // Try to use visible text for elements with short text content
    try {
      const text = (el.innerText || '').trim();
      if (text && text.length > 0 && text.length < 60) {
        // use normalize-space to reduce whitespace sensitivity; use xpathLiteral to handle quotes
        const lit = xpathLiteral(text);
        return `xpath=//${el.tagName.toLowerCase()}[normalize-space()=${lit}]`;
      }
    } catch (e) { /* ignore */ }

    // Heuristics: try to build axis-based or ancestor-relative XPaths before absolute fallbacks
    try {
      // 1) If associated label exists, use label text -> following axis
      try {
        let labelEl = null;
        if (el.id) labelEl = document.querySelector(`label[for="${el.id}"]`);
        if (!labelEl) {
          // maybe input is wrapped by a label
          let p = el.parentElement;
          while (p) {
            if (p.tagName === 'LABEL') { labelEl = p; break; }
            p = p.parentElement;
          }
        }
        if (labelEl) {
          const labText = (labelEl.innerText || '').trim();
          if (labText && labText.length < 80) {
            const lit = xpathLiteral(labText);
            // prefer following:: which searches descendants too
            return `xpath=//label[normalize-space()=${lit}]/following::${el.tagName.toLowerCase()}[1]`;
          }
        }
      } catch (e) { /* ignore label strategy errors */ }

      // helper: find nearest ancestor with a stable attribute
      function nearestAncestorWithAttrs(node, attrs) {
        let cur = node.parentElement;
        while (cur) {
          for (const a of attrs) {
            try {
              const v = cur.getAttribute && cur.getAttribute(a);
              if (v) return { node: cur, attr: a, value: v };
            } catch (e) { /* ignore */ }
          }
          cur = cur.parentElement;
        }
        return null;
      }

      const anchorAttrs = ['id', 'data-testid', 'data-test', 'data-test-id', 'data-qa'];
      const anc = nearestAncestorWithAttrs(el, anchorAttrs);
      if (anc && anc.node) {
        // build anchor selector
        let anchorSel = null;
        try {
          if (anc.attr === 'id') anchorSel = `//*[@id=\"${anc.value}\"]`;
          else anchorSel = `//*[@${anc.attr}=\"${anc.value}\"]`;
        } catch (e) { anchorSel = null; }

        if (anchorSel) {
          // If element has a distinguishing attribute, prefer it
          try {
            const tag = el.tagName.toLowerCase();
            if (el.getAttribute && el.getAttribute('name')) {
              const name = el.getAttribute('name');
              return `xpath=${anchorSel}//${tag}[@name=\"${name}\"]`;
            }
            const dataTest = el.getAttribute && (el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-test-id'));
            if (dataTest) return `xpath=${anchorSel}//${tag}[@data-testid=\"${dataTest}\"]`;
            const text = (el.innerText || '').trim();
            if (text && text.length > 0 && text.length < 60) {
              const lit = xpathLiteral(text);
              return `xpath=${anchorSel}//${tag}[normalize-space()=${lit}]`;
            }

            // fallback: pick the Nth occurrence of tag under anchor
            const descendants = Array.from(anc.node.querySelectorAll(el.tagName));
            const idx = descendants.indexOf(el) + 1;
            if (idx > 0) {
              return `xpath=${anchorSel}//${el.tagName.toLowerCase()}[${idx}]`;
            }
          } catch (e) { /* ignore */ }
        }
      }

      // 3) Nearby text/sibling heuristic: find preceding element with stable text and use following::
      try {
        // look up to 6 preceding elements in DOM order
        let walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, null);
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        const idx = nodes.indexOf(el);
        if (idx > 0) {
          for (let i = idx - 1; i >= Math.max(0, idx - 12); i--) {
            const n = nodes[i];
            try {
              const t = (n.innerText || '').trim();
              if (t && t.length > 0 && t.length < 80) {
                const lit = xpathLiteral(t);
                return `xpath=//*[normalize-space()=${lit}]/following::${el.tagName.toLowerCase()}[1]`;
              }
            } catch (e) { /* ignore */ }
          }
        }
      } catch (e) { /* ignore */ }

      // 4) As a last-ditch attempt before absolute path, build a shorter absolute-like path limited depth
      try {
        const parts = [];
        let node = el;
        let depth = 0;
        while (node && node.nodeType === 1 && depth < 6) {
          let idx = 1;
          let sib = node.previousElementSibling;
          while (sib) {
            if (sib.tagName === node.tagName) idx++;
            sib = sib.previousElementSibling;
          }
          const tag = node.tagName.toLowerCase();
          parts.unshift(`${tag}[${idx}]`);
          node = node.parentElement;
          depth++;
        }
        if (parts.length) {
          return `xpath=//${parts.join('/')}`; // note: starts with // to make it more flexible than absolute /
        }
      } catch (e) { /* ignore */ }
    } catch (e) { /* ignore everything and fallback */ }

    // Final fallback: absolute xpath (original behavior)
    try {
      const parts = [];
      let node = el;
      while (node && node.nodeType === 1) {
        let idx = 1;
        let sib = node.previousElementSibling;
        while (sib) {
          if (sib.tagName === node.tagName) idx++;
          sib = sib.previousElementSibling;
        }
        const tag = node.tagName.toLowerCase();
        parts.unshift(`${tag}[${idx}]`);
        node = node.parentElement;
      }
      if (parts.length) {
        return `xpath=/${parts.join('/')}`;
      }
    } catch (e) { /* ignore */ }

    return null;
  }

  function snapshot(el) {
    return {
      url: location.href,
      title: document.title,
      ts: Date.now(),
      elementVisible: !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)),
      elementTag: el && el.tagName ? el.tagName.toLowerCase() : null,
      elementType: (el && (el.tagName === 'INPUT' || el.tagName === 'BUTTON')) ? (el.type || null) : null,
      locators: el ? getLocators(el) : [],
      text: el ? (el.innerText || '').slice(0, 500) : null
    };
  }

  // Enhanced getLocators function with uniqueness validation
  function getLocators(el) {
    if (!el) return [];
    const candidateLocators = [];

    try {
      // Enhanced data attributes detection
      const dataAttrs = [
        'data-testid', 'data-test', 'data-test-id', 'data-qa', 'data_qa',
        'data-cy', 'data-selenium', 'data-automation', 'data-tid', 'data-e2e'
      ];

      for (const attr of dataAttrs) {
        const value = el.getAttribute && el.getAttribute(attr);
        if (value) {
          candidateLocators.push(`[${attr}="${value}"]`);
        }
      }
    } catch (e) { }

    try {
      // ID selector
      if (el.id && !/^\d/.test(el.id)) {
        candidateLocators.push(`#${el.id}`);
      }
    } catch (e) { }

    try {
      // Name attribute
      if (el.getAttribute && el.getAttribute('name')) {
        const name = el.getAttribute('name');
        candidateLocators.push(`[name="${name}"]`);
        candidateLocators.push(`${el.tagName.toLowerCase()}[name="${name}"]`);
      }
    } catch (e) { }

    try {
      // ARIA attributes
      const ariaAttrs = ['aria-label', 'aria-labelledby', 'aria-describedby'];
      for (const attr of ariaAttrs) {
        const value = el.getAttribute && el.getAttribute(attr);
        if (value) {
          candidateLocators.push(`[${attr}="${value}"]`);
        }
      }
    } catch (e) { }

    try {
      // Other attributes
      const attrs = ['placeholder', 'title', 'alt', 'href'];
      for (const attr of attrs) {
        const value = el.getAttribute && el.getAttribute(attr);
        if (value && value.indexOf('javascript:') !== 0) {
          candidateLocators.push(`[${attr}="${value}"]`);
          candidateLocators.push(`${el.tagName.toLowerCase()}[${attr}="${value}"]`);
        }
      }
    } catch (e) { }

    try {
      // Type-specific selectors
      const tag = el.tagName ? el.tagName.toLowerCase() : null;
      const etype = el.type || (el.getAttribute && el.getAttribute('type')) || null;

      if (tag && etype) {
        // This will often NOT be unique (like input[type="text"])
        candidateLocators.push(`${tag}[type="${etype}"]`);
      }
    } catch (e) { }

    try {
      // Class-based selectors
      if (el.className && typeof el.className === 'string') {
        const classes = el.className.trim().split(/\s+/).filter(Boolean);
        const tag = el.tagName.toLowerCase();

        if (classes.length > 0) {
          candidateLocators.push(`${tag}.${classes[0]}`);
          candidateLocators.push(`.${classes}`);

          if (classes.length > 1) {
            candidateLocators.push(`${tag}.${classes.slice(0, 2).join('.')}`);
          }
        }
      }
    } catch (e) { }

    try {
      // Label association
      if (el.id) {
        const lab = document.querySelector(`label[for="${el.id}"]`);
        if (lab) {
          const labText = (lab.innerText || '').trim();
          if (labText) {
            candidateLocators.push(`label[for="${el.id}"]`);
          }
        }
      }
    } catch (e) { }

    try {
      // Role-based selectors - ONLY with names to ensure uniqueness
      const role = el.getAttribute && el.getAttribute('role');
      if (role) {
        candidateLocators.push(`[role="${role}"]`);
        const name = (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || '').trim();
        if (name && name.length < 50) {
          candidateLocators.push(`[role="${role}"][aria-label="${name}"]`);
          // avoid adding :has-text variants (Playwright-style) — prefer aria-label/role combos
        }
      }
    } catch (e) { }

    try {
      // Text nodes: intentionally not emitted as Playwright-style text selectors here
      // We prefer attribute-based selectors to avoid fragile global text matches.
    } catch (e) { }

    try {
      // XPath candidate - prefer attribute-based or context-sensitive xpath
      const xp = elementXPath(el);
      if (xp) candidateLocators.push(xp);
    } catch (e) { }

    try {
      // Link-specific selectors
      if (el.tagName === 'A') {
        const href = el.getAttribute('href');
        if (href && href.indexOf('javascript:') !== 0) {
          candidateLocators.push(`a[href="${href}"]`);
        }
      }
    } catch (e) { }

    try {
      // CSS path as fallback
      const path = cssPath(el);
      if (path) candidateLocators.push(path);
    } catch (e) { }

    // **CRITICAL STEP: Filter to only unique locators**
    const uniqueLocators = [];
    for (const selector of candidateLocators) {
      if (selector && typeof selector === 'string' && selector.trim()) {
        if (isUniqueLocator(selector.trim(), el)) {
          uniqueLocators.push(selector.trim());
        }
      }
    }

    // Remove duplicates while preserving order
    const finalLocators = [];
    for (const loc of uniqueLocators) {
      if (!finalLocators.includes(loc)) {
        finalLocators.push(loc);
      }
    }

    return finalLocators;
  }

  // Produce a short deterministic hash from a string (FNV-1a -> base36)
  function shortHash(input) {
    try {
      let h = 2166136261 >>> 0;
      for (let i = 0; i < input.length; i++) {
        h ^= input.charCodeAt(i);
        // multiply by FNV prime
        h = Math.imul(h, 16777619) >>> 0;
      }
      // turn into base36 and pad
      const s = (h >>> 0).toString(36);
      // append a secondary small checksum to reduce collisions slightly
      let c = 0;
      for (let i = 0; i < input.length; i++) c = (c + input.charCodeAt(i) * (i + 1)) % 1679616; // 36^4
      const tail = c.toString(36);
      return (s + tail).slice(0, 20);
    } catch (e) { return String(Math.random()).slice(2, 10); }
  }

  // Robust hint extractor: take a meaningful short name from a selector or element
  function extractElementHint(selectorOrEl) {
    try {
      let sel = '';
      if (!selectorOrEl) return 'element';
      if (typeof selectorOrEl === 'string') sel = selectorOrEl;
      else if (selectorOrEl && selectorOrEl.getAttribute) {
        // element provided: prefer stable attributes in order of usefulness
        const el = selectorOrEl;
        const prefer = [
          el.getAttribute && (el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-test-id')),
          el.id,
          el.getAttribute && el.getAttribute('name'),
          el.getAttribute && el.getAttribute('aria-label'),
          el.getAttribute && el.getAttribute('title'),
          el.placeholder,
          (el.getAttribute && el.getAttribute('value')) || (el.value || null)
        ];
        for (const p of prefer) {
          if (p) return sanitizeHint(p);
        }

        // fallback to short visible text
        const text = (el.innerText || '').trim();
        if (text && text.length < 80) return sanitizeHint(text);

        // fallback to class or tag
        if (el.className && typeof el.className === 'string') {
          const cls = el.className.trim().split(/\s+/).filter(Boolean)[0];
          if (cls) return sanitizeHint(cls);
        }
        sel = (el.tagName || '').toString();
      }

      sel = sel.toString().trim();

      // Special-case XPath selectors: extract the literal used in predicates (normalize-space/text/contains) or attribute values
      try {
        if (sel.startsWith('xpath=')) {
          const xp = sel.slice('xpath='.length);
          // patterns capturing quoted literal inside common predicates
          const patterns = [
            /\/\/\/?([a-z0-9_-]+)\[\s*normalize-space\(\.?\)\s*=\s*(['"])(.*?)\2\s*\]/i,
            /\/\/\/?([a-z0-9_-]+)\[\s*text\(\.?\)\s*=\s*(['"])(.*?)\2\s*\]/i,
            /\/\/\/?([a-z0-9_-]+)\[\s*\.\s*=\s*(['"])(.*?)\2\s*\]/i,
            /\/\/\/?([a-z0-9_-]+)\[\s*contains\(\s*normalize-space\(\.?\)\s*,\s*(['"])(.*?)\2\s*\)\s*\]/i,
            /\/\/\/?([a-z0-9_-]+)\[\s*contains\(\s*text\(\.?\)\s*,\s*(['"])(.*?)\2\s*\)\s*\]/i,
            /\/\/\/?([a-z0-9_-]+)\[\s*@([a-z0-9_-]+)\s*=\s*(['"])(.*?)\3\s*\]/i
          ];
          for (const re of patterns) {
            const m = xp.match(re);
            if (m) {
              const v = m[3] || m[4];
              if (v) {
                // avoid returning helper words like 'normalize' captured by loose patterns
                const cleaned = v.replace(/normalize\s*-?space/i, '').trim();
                if (cleaned) return sanitizeHint(cleaned);
              }
            }
          }
          // last-resort: any quoted literal inside the xpath
          let m = xp.match(/(['\"])([^'\"]{1,80})\1/);
          if (m && m[2]) return sanitizeHint(m[2]);
          // if nothing useful, try to return the tag name (first path segment)
          m = xp.match(/\/\/?([a-z0-9_-]+)/i);
          if (m && m[1]) return sanitizeHint(m[1]);
        }
      } catch (e) { /* ignore xpath parse errors */ }

      // CSS/text selectors and attribute patterns
      // Do not use Playwright ':has-text' or 'text=' style hints — they are intentionally excluded
      m = sel.match(/\[aria-label=\"([^\"]+)\"\]/i);
      if (m && m[1]) return sanitizeHint(m[1]);
      m = sel.match(/\[title=\"([^\"]+)\"\]/i);
      if (m && m[1]) return sanitizeHint(m[1]);
      m = sel.match(/\[placeholder=\"([^\"]+)\"\]/i);
      if (m && m[1]) return sanitizeHint(m[1]);
      m = sel.match(/#([a-z0-9_\-]+)/i);
      if (m && m[1]) return sanitizeHint(m[1]);
      m = sel.match(/\[name=\"([^\"]+)\"\]/i);
      if (m && m[1]) return sanitizeHint(m[1]);
      m = sel.match(/@?id=\"([^\"]+)\"/i);
      if (m && m[1]) return sanitizeHint(m[1]);

      // fallback: collapse visible words from the selector
      const words = sel.replace(/[^a-zA-Z0-9]+/g, ' ').trim().split(/\s+/).filter(Boolean);
      if (words.length) return sanitizeHint(words.slice(0, 3).join('_'));
      return 'element';
    } catch (e) { return 'element'; }
  }

  function sanitizeHint(s) {
    const out = s.toString().trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    return out ? out.slice(0, 32) : 'element';
  }

  // Detect element type using the actual element when possible, otherwise from selector
  function detectElementTypeFromElement(el) {
    try {
      if (!el) return 'element';
      const tag = (el.tagName || '').toLowerCase();
      const t = ((el.type || (el.getAttribute && el.getAttribute('type')) || '')).toLowerCase();
      const role = (el.getAttribute && (el.getAttribute('role') || el.getAttribute('aria-role')) || '').toLowerCase();

      // input types
      if (tag === 'input') {
        if (!t) return 'textbox';
        if (t === 'password') return 'password';
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'file') return 'file';
        if (t === 'date' || t === 'datetime-local' || t === 'time' || t === 'month' || t === 'week') return 'date';
        if (t === 'color') return 'color';
        if (t === 'range') return 'range';
        if (t === 'search') return 'search';
        if (t === 'email') return 'email';
        if (t === 'tel') return 'tel';
        if (t === 'url') return 'url';
        if (t === 'number') return 'number';
        if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
        return 'textbox';
      }

      if (tag === 'textarea') return 'textbox';
      if (tag === 'select') return 'dropdown';
      if (tag === 'button') return 'button';
      if (tag === 'a') return 'link';
      if (tag === 'img' || tag === 'picture' || tag === 'svg') return 'image';

      // contenteditable elements behave like textboxes
      try { if (el.isContentEditable) return 'textbox'; } catch (e) { /* ignore */ }

      // ARIA/role hints
      if (role) {
        if (role.includes('button')) return 'button';
        if (role.includes('link')) return 'link';
        if (role.includes('textbox') || role.includes('searchbox') || role.includes('search')) return 'textbox';
        if (role.includes('combobox') || role.includes('listbox') || role.includes('menu')) return 'dropdown';
        if (role.includes('checkbox')) return 'checkbox';
        if (role.includes('radio')) return 'radio';
      }

      // heuristic: onclick handlers often mean clickable/button-like
      try {
        if ((el.getAttribute && el.getAttribute('onclick')) || typeof el.onclick === 'function') return 'button';
      } catch (e) { /* ignore */ }

      return tag || 'element';
    } catch (e) { return 'element'; }
  }

  function detectElementTypeFromSelector(sel) {
    try {
      if (!sel) return 'element';
      const s = sel.toString().toLowerCase();
      // xpath: try to detect tag
      if (s.startsWith('xpath=')) {
        const m = s.match(/^xpath=\/\/?([a-z0-9]+)/i);
        if (m && m[1]) return m[1];
        return 'element';
      }

      // CSS attribute-based patterns
      if (s.includes('input[') || s.startsWith('input')) {
        if (s.includes('type="password"') || s.includes("type='password'") || /input\[type=.?password/.test(s)) return 'password';
        if (s.includes('type="checkbox"') || s.includes("type='checkbox'") || /input\[type=.?checkbox/.test(s)) return 'checkbox';
        if (s.includes('type="radio"') || s.includes("type='radio'") || /input\[type=.?radio/.test(s)) return 'radio';
        if (s.includes('type="file"') || /input\[type=.?file/.test(s)) return 'file';
        if (s.includes('type="search"') || /input\[type=.?search/.test(s)) return 'search';
        if (s.includes('type="date"') || /input\[type=.?date/.test(s)) return 'date';
        if (s.includes('type="email"') || /input\[type=.?email/.test(s)) return 'email';
        if (s.includes('type="tel"') || /input\[type=.?tel/.test(s)) return 'tel';
        if (s.includes('type="url"') || /input\[type=.?url/.test(s)) return 'url';
        if (s.includes('type="number"') || /input\[type=.?number/.test(s)) return 'number';
        if (s.includes('type="range"') || /input\[type=.?range/.test(s)) return 'range';
        return 'textbox';
      }

      if (s.startsWith('textarea') || s.includes('textarea')) return 'textbox';
      if (s.includes('select') || s.startsWith('select')) return 'dropdown';
      if (s.startsWith('button') || s.includes('button') || s.includes('[role="button"]') || s.includes("[role='button']")) return 'button';
      if (s.startsWith('a[') || s.startsWith('a:') || s.startsWith('a') || s.includes('[role="link"]') || s.includes("[role='link']")) return 'link';
      if (s.startsWith('img') || s.includes('img') || s.includes('svg') || s.includes('picture')) return 'image';

      if (s.includes('[aria-label=') || s.includes('[placeholder=')) return 'text';

      // role-combining selectors
      if (s.includes('[role=')) {
        if (s.includes('role="button"') || s.includes("role='button'")) return 'button';
        if (s.includes('role="link"') || s.includes("role='link'")) return 'link';
        if (s.includes('role="textbox"') || s.includes('role="searchbox"')) return 'textbox';
        if (s.includes('role="combobox"') || s.includes('role="listbox"') || s.includes('role="menu"')) return 'dropdown';
      }

      // fallback: look for tag tokens
      if (/^\s*[a-z0-9]+\b/.test(s)) {
        const m = s.match(/^\s*([a-z0-9]+)/);
        if (m && m[1]) {
          const tag = m[1];
          if (tag === 'input') return 'textbox';
          if (tag === 'button') return 'button';
          if (tag === 'select') return 'dropdown';
          if (tag === 'textarea') return 'textbox';
          if (tag === 'a') return 'link';
          if (tag === 'img') return 'image';
          return tag;
        }
      }

      return 'element';
    } catch (e) { return 'element'; }
  }

  function rankLocators(cands) {
    if (!cands || !cands.length) return [];

    function score(sel) {
      let s = 0;

      if (sel.startsWith('[data-testid=') || sel.startsWith('[data-test=')) s += 100;
      else if (sel.startsWith('#')) s += 95;
      else if (sel.includes('[name=')) s += 85;
      else if (sel.includes('[aria-label=')) s += 80;
      // Prefer XPath selectors slightly, but demote ones that use normalize-space() because
      // they are more brittle; this ensures when multiple xpath variants exist, the
      // normalize-space version is ranked last among xpath options.
      if (sel.startsWith('xpath=')) {
        s += 77;
        try {
          if (/normalize-?space\s*\(/i.test(sel)) {
            s -= 10; // smaller penalty: keep normalize-space xpaths last among xpaths but typically above nth-of-type selectors
          }
        } catch (e) { /* ignore regex issues */ }
      }
      // do not promote Playwright-style text selectors (text=/:has-text) — removed by policy
      else if (sel.includes('role=')) s += 65;

      if (sel.includes('nth-of-type')) s -= 20;
      if ((sel.match(/>/g) || []).length >= 3) s -= 15;
      if (sel.length > 120) s -= 10;

      return s;
    }

    const unique = Array.from(new Set(cands.filter(Boolean)));
    unique.sort((a, b) => score(b) - score(a));

    return unique;
  }

  function sendPayload(type, target, value) {
    // prefer a pre-captured snapshot (captured on focusin) so locators reflect pre-typing state
    const preSnap = (target && preSnapshots.get && preSnapshots.get(target)) || snapshot(target);
    const postSnap = snapshot(target);
    try { if (target && preSnapshots.get && preSnapshots.has(target)) preSnapshots.delete(target); } catch (e) { }
    // combine locators from post and pre
    const allLocators = (postSnap.locators || []).concat(preSnap.locators || []);
    const ranked = rankLocators(allLocators);
    const primary = ranked.length ? ranked[0] : null;
    // compute selectorRef and stable hash using top two locators and element hint/type
    const primaryLocator = primary || null;
    const second = (ranked && ranked.length > 1) ? ranked[1] : null;
    const third = (ranked && ranked.length > 2) ? ranked[2] : null;
    let selectorRef = null;
    let hash = null;
    // Only compute selectorRef/hash when we have a primary selector
    if (primaryLocator) {
      try {
        const hint = extractElementHint(primaryLocator || second || third || (target && target));
        const elType = (target && detectElementTypeFromElement(target)) || detectElementTypeFromSelector(primaryLocator || second || third || '');

        let pageTag = 'site';
        try {
          const t = (document && document.title) ? document.title : (location && location.hostname ? location.hostname : 'site');
          pageTag = sanitizeHint(t) || 'site';
        } catch (e) { pageTag = 'site'; }

        // build selectorRef like `${type}_${hint}_${shortHash(primary+second+pageTag)}`
        // NOTE: do NOT include url/location/origin other than pageTag in hash calculation to keep hash stable across origins
        // Use top 3 ranked locators plus pageTag to compute a stable hash
        const baseForHash = ((primaryLocator || '') + '||' + (second || '') + '||' + (third || '')+ '||' + pageTag).toString();
        const h = shortHash(baseForHash);
        selectorRef = `$${pageTag}.${elType}_${hint}_${h.slice(0, 5)}`;
        hash = h;
      } catch (e) { selectorRef = null; hash = null; }
    }

    // assemble the canonical payload and send to background
    // `selectors` is an ordered list (most-stable first -> least-stable last)
    // Only include the top 5 selectors to keep the payload small.
    const selectorsToSend = Array.isArray(ranked)
      ? ranked.slice(0, 5)
      : (Array.isArray(allLocators) ? allLocators.slice(0, 5) : []);

    const payload = {
      type: 'ui',
      action: type,
      timestamp: Date.now(),
      value: value === undefined ? null : (value && typeof value === 'object' ? safeStringify(value) : value),
      pre: preSnap,
      post: postSnap,
      selectors: selectorsToSend
    };

    // Attach selectorRef/hash only when computed (primary selector existed)
    if (selectorRef) payload.selectorRef = selectorRef;
    if (hash) payload.hash = hash;

    try {
      // attach frameInfo: array of {index, frameSelector} from outer -> inner and an inIframe flag
      const probeEl = target || (typeof document !== 'undefined' ? document.body : null);
      const frameChain = (typeof getFrameChainInfo === 'function' && probeEl) ? getFrameChainInfo(probeEl) : [];
      const inIframeFlag = !!(frameChain && frameChain.length);
      payload.inIframe = inIframeFlag;
      if (inIframeFlag) {
        const chainOuterFirst = Array.isArray(frameChain) ? frameChain.slice().reverse() : [];
        const frameInfo = chainOuterFirst.map((f, idx) => ({ index: idx + 1, frameSelector: f && f.selector ? f.selector : null }));
        payload.frameInfo = frameInfo;
      }
    } catch (e) { /* swallow */ }

    try {
      chrome.runtime.sendMessage({ type: 'recorder-event', payload });
    } catch (e) { /* swallow */ }

    // end of sendPayload
  }

  // event listeners
  // capture clicks and special-case certain widgets (datepickers, selects) so we emit the right high-level action
  document.addEventListener('click', e => {
    if (!recording) return;
    const target = e.target;

    // detect clicks inside common datepicker popup containers and map them back to the last focused input
    try {
      const datepickerSelectors = [
        '.react-datepicker', '.datepicker', '.ui-datepicker', '[class*="datepicker"]', '[data-datepicker]', '[data-date]'
      ];
      for (const sel of datepickerSelectors) {
        try {
          if (document.querySelector && document.querySelector(sel) && (target.closest && target.closest(sel))) {
            if (lastInteractedInput) {
              // read value after a short delay; many datepickers update the input asynchronously
              setTimeout(() => {
                try {
                  // avoid duplicates: skip if we recently sent a selectDate for this element
                  const prev = lastActionTs.get(lastInteractedInput) || 0;
                  if (Date.now() - prev < 250) return;
                  let v = lastInteractedInput.value || (lastInteractedInput.getAttribute && lastInteractedInput.getAttribute('data-value')) || null;
                  // fallback: try to extract from the clicked popup cell if input is still empty
                  if (!v) {
                    try {
                      v = (target.getAttribute && (target.getAttribute('data-date') || target.getAttribute('data-value'))) || (target.textContent || '').trim() || null;
                    } catch (_) { /* ignore */ }
                  }
                  lastActionTs.set(lastInteractedInput, Date.now());
                  sendPayload('selectDate', lastInteractedInput, v);
                } catch (_) {/* swallow */ }
              }, 40);
              return;
            }
          }
        } catch (_) { /* ignore selector errors */ }
      }
    } catch (_) { /* swallow */ }

    try {
      // Native <option> inside <select>
      if (target && target.tagName === 'OPTION') {
        const sel = target.closest && target.closest('select');
        if (sel) {
          // prefer the select as the target so locators point to the control
          sendPayload('selectDropdownByValue', sel, sel.value || target.value || null);
          return;
        }
      }
    } catch (err) { /* swallow and fallthrough to generic click */ }

    // Some browsers dispatch clicks on the <select> itself (or on inner wrapper) rather than OPTION elements.
    // If the click occurred inside a select, schedule a short delayed read and emit a 'select' event instead
    // of letting it fall through to a generic 'click'. Use lastActionTs to avoid duplicates.
    try {
      const selAncestor = target && target.closest && target.closest('select');
      if (selAncestor) {
        setTimeout(() => {
          try {
            const prev = lastActionTs.get(selAncestor) || 0;
            if (Date.now() - prev < 250) return;
            const val = selAncestor.value || null;
            lastActionTs.set(selAncestor, Date.now());
            sendPayload('selectDropdownByValue', selAncestor, val);
          } catch (_) { }
        }, 40);
        return;
      }
    } catch (_) { }

    // fallback: generic click
    sendPayload('click', target, null);
  }, true);

  // capture double-clicks
  document.addEventListener('dblclick', e => {
    if (!recording) return;
    const target = e.target;
    try {
      // send as a doubleClick action so the runner can replay with dblclick
      sendPayload('doubleClick', target, null);
    } catch (err) { /* swallow */ }
  }, true);

  document.addEventListener('input', e => {
    if (!recording) return;
    const t = e.target;
    if (!(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable))) return;
    // debounce per-element so rapid keystrokes become one fill
    let rec = inputDebounces.get(t) || {};
    if (rec.timeout) clearTimeout(rec.timeout);
    rec.lastValue = (t.value || t.textContent || '');
    rec.timeout = setTimeout(() => {
      inputDebounces.delete(t);
      try { if (recording) sendPayload('fill', t, rec.lastValue); } catch (e) { }
    }, 1000);
    inputDebounces.set(t, rec);
  }, true);

  document.addEventListener('change', e => {
    if (!recording) return;
    const t = e.target;
    // flush any pending input for this element
    try { flushPendingInput(t, true); } catch (e) { }
    // Special-case checkboxes and radios: emit explicit check/uncheck
    try {
      if (t && t.matches && (t.matches('input[type="checkbox"]') || t.matches('input[type="radio"]'))) {
        const isCheckbox = t.matches('input[type="checkbox"]');
        const checked = !!t.checked;
        if (checked) {
          sendPayload('check', t, t.value || true);
          return;
        } else if (isCheckbox && !checked) {
          // only emit uncheck for checkboxes (radios becoming unchecked are implicit)
          sendPayload('uncheck', t, null);
          return;
        }
      }
    } catch (err) { /* swallow */ }

    // Special-case native date inputs, native selects, and known custom datepickers
    try {
      // Native <select> controls: capture as 'select' so runner can set value directly
      if (t && t.matches && t.matches('select')) {
        sendPayload('selectDropdownByValue', t, t.value || null);
        return;
      }
      if (t && t.matches && t.matches('input[type="date"]')) {
        // native date input: capture as selectDate so runner can set value directly
        sendPayload('selectDate', t, t.value || null);
        return;
      }
      // custom datepickers can be detected by common attributes/classes used by various libraries
      if (t && t.matches && (
        t.matches('[data-nocr-datepicker]') ||
        t.matches('.nocr-datepicker') ||
        t.matches('.hasDatepicker') ||
        t.matches('[class*="datepicker"]') ||
        t.matches('[data-datepicker]') ||
        t.matches('[data-date]') ||
        (t.id && /date/i.test(t.id))
      )) {
        // for custom widgets, prefer explicit data-* attributes if present, otherwise element value
        const v = (t.getAttribute && (t.getAttribute('data-value') || t.getAttribute('data-date') || t.getAttribute('data-datepicker'))) || t.value || null;
        sendPayload('selectDate', t, v);
        return;
      }
    } catch (err) { /* swallow and fallthrough to generic change */ }

    // fallback: generic change
    sendPayload('change', t, t.value || null);
  }, true);

  // flush on focusout/blur so leaving field also emits the final value
  document.addEventListener('focusout', e => {
    const t = e.target;
    try { flushPendingInput(t, false); } catch (e) { }
  }, true);

  document.addEventListener('keydown', e => {
    if (!recording) return;
    const el = document.activeElement;
    const tag = el && el.tagName ? el.tagName.toUpperCase() : null;
    const isEditable = el && (tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable);
    // Important keys to still capture inside editable fields
    const importantKeys = ['Enter', 'Tab', 'Escape'];
    // If IME composition is in progress, don't emit press or flush
    if (e.isComposing) return;
    if (isEditable) {
      // On Enter/Tab/Escape or when Ctrl/Cmd held (shortcuts), flush pending input and record the key.
      if (importantKeys.includes(e.key) || e.ctrlKey || e.metaKey) {
        try { flushPendingInput(el, true); } catch (err) { }
        sendPayload('press', el, e.key);
      }
      return;
    }
    // non-editable elements: emit key presses as before
    sendPayload('press', el, e.key);
  }, true);

  // scrolling events: intentionally disabled — do not record or emit scroll steps
  // (kept here as a commented reference in case we need to re-enable with a setting)
  // let lastScroll = 0;
  // window.addEventListener('scroll', () => {
  //   if (!recording) return;
  //   const now = Date.now();
  //   if (now - lastScroll < 400) return;
  //   lastScroll = now;
  // (scroll events intentionally ignored)
  // }, true);

  // messaging from background (set recording state)
  let hasEmittedInitialGoto = false;
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === 'set-recording') {
      recording = !!msg.recording;
    }
    if (msg && msg.type === 'recording-started') {
      if (hasEmittedInitialGoto) return;
      hasEmittedInitialGoto = true;
      try {
        chrome.storage.local.get(['recorded_steps'], res => {
          const arr = Array.isArray(res.recorded_steps) ? res.recorded_steps : [];
          if (arr.length === 0) {
            try { sendPayload('goto', null, location.href); } catch (e) { }
          }
        });
      } catch (e) { }
    }
    if (msg && msg.type === 'get-state') {
      sendResponse({ recording });
    }
    if (msg && msg.type === 'open-sidebar') {
      try {
        // create a simple iframe dock on the right
        if (document.getElementById('nocr-recorder-sidebar-host')) return sendResponse({ ok: true });
        const host = document.createElement('div');
        host.id = 'nocr-recorder-sidebar-host';
        // dock to bottom and make larger for pinned-site use
        host.style.position = 'fixed'; host.style.top = '12px'; host.style.bottom = '12px'; host.style.right = '12px'; host.style.width = '720px'; host.style.maxWidth = 'calc(100% - 24px)'; host.style.height = 'auto'; host.style.zIndex = '2147483647';
        host.style.boxShadow = '0 8px 20px rgba(0,0,0,0.25)'; host.style.background = 'white'; host.style.borderRadius = '8px';
        const iframe = document.createElement('iframe');
        iframe.src = chrome.runtime.getURL('recorder_window.html');
        iframe.style.width = '100%'; iframe.style.height = '100%'; iframe.style.border = '0';
        host.appendChild(iframe);
        document.documentElement.appendChild(host);
        sendResponse({ ok: true });
      } catch (e) { sendResponse({ ok: false }); }
    }
    if (msg && msg.type === 'close-sidebar') {
      try {
        const host = document.getElementById('nocr-recorder-sidebar-host');
        if (host && host.parentNode) host.parentNode.removeChild(host);
        sendResponse({ ok: true });
      } catch (e) { sendResponse({ ok: false }); }
    }
    if (msg && msg.type === 'context-assert-ack') {
      // no-op ack handler for context assert responses
      sendResponse({ ok: true });
    }
    // Respond to frame info requests (used by background when recorder-event
    // arrived without frameInfo). Return the frame chain info for the last
    // right-clicked element so background can attach frameInfo before storing.
    if (msg && msg.type === 'request-frame-info') {
      try {
        const el = lastRightClicked || null;
        if (!el) return sendResponse({ frameInfo: [] });
        const frameChain = (typeof getFrameChainInfo === 'function') ? getFrameChainInfo(el) : [];
        const chainOuterFirst = Array.isArray(frameChain) ? frameChain.slice().reverse() : [];
        const frameInfo = chainOuterFirst.map((f, idx) => ({ index: idx + 1, frameSelector: f && f.selector ? f.selector : null }));
        return sendResponse({ frameInfo });
      } catch (e) { return sendResponse({ frameInfo: [] }); }
    }
  });

  // capture right-click target for use by context menu assertions
  document.addEventListener('contextmenu', e => {
    try {
      lastRightClicked = e.target;
      // also record a right-click event when recording is active
      if (recording) {
        try {
          // use sendPayload so locators, snapshots and frameInfo are attached
          // right-clicks handled via context menu messages; no inline recording here
        } catch (err) { /* swallow */ }
      }
    } catch (err) { lastRightClicked = null; }
  }, true);

  // handle assertion requests from background (context menu clicks)
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || !msg.type) return;
    if (msg.type === 'context-assert') {
      try {
        const which = msg.id || msg.menuItemId || (msg.info && msg.info.menuItemId) || msg.id;
        const el = lastRightClicked;
        if (!el) return sendResponse({ ok: false, reason: 'no-target' });

        const snap = snapshot(el);

        if (which === 'hover' || which === 'Mouse Over') {
          sendPayload('hover', el);
          return sendResponse({ ok: true });
        }
        if (which === 'getText' || which === 'Get TextContent') {
          sendPayload('getText', el);
          return sendResponse({ ok: true });
        }
        if (which === 'verify-text' || which === 'Verify Element Text') {
          const text = (el.innerText || '').trim();
          // use sendPayload so assertions include locators, backups, snapshots, and frameInfo
          sendPayload('verifyElementText', el, text);
          return sendResponse({ ok: true });
        }
        if (which === 'verify-value' || which === 'Verify Element Value') {
          const value = (el.getAttribute && el.getAttribute('value') || '').trim();
          sendPayload('verifyElementValue', el, value);
          return sendResponse({ ok: true });
        }
        if (which === 'verify-displayed' || which === 'Verify Element Visible') {
          const visible = !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          sendPayload('verifyElementVisible', el, visible);
          return sendResponse({ ok: true });
        }
        if (which === 'verify-disabled' || which === 'Verify Element Disabled') {
          const disabled = !!(el && (el.disabled || (el.getAttribute && el.getAttribute('aria-disabled') === 'true')));
          sendPayload('verifyElementDisabled', el, disabled);
          return sendResponse({ ok: true });
        }
        if (which === 'verify-attribute' || which === 'Verify Element Attribute') {
          // collect all attribute name/value pairs from the element
          const attrs = {};
          try {
            if (el && el.attributes && el.attributes.length) {
              for (let i = 0; i < el.attributes.length; i++) {
                const a = el.attributes[i];
                attrs[a.name] = a.value;
              }
            }
          } catch (e) { /* swallow */ }

          // send the complete attributes object as the value
          sendPayload('verifyElementAttribute', el, attrs);
          return sendResponse({ ok: true });
        }

        if (which === 'verify-page-title' || which === 'Verify Page Title') {
          const title = (document.title || '').trim();

          sendPayload('verifyPageTitle', '', title);
          return sendResponse({ ok: true });
        }

        if (which === 'verify-element-count' || which === 'Verify Element Count') {
          // previous implementation used an incorrect selector variable; keep simple fallback
          const selector = cssPath(el) || (el.tagName ? el.tagName.toLowerCase() : null);
          let count = 0;
          try {
            if (selector && selector.startsWith('xpath=')) {
              const xp = selector.slice('xpath='.length);
              const snap = document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
              count = snap ? snap.snapshotLength : 0;
            } else if (selector) {
              const nodes = document.querySelectorAll(selector);
              count = nodes ? nodes.length : 0;
            } else if (el && el.tagName) {
              count = document.getElementsByTagName(el.tagName).length;
            }
          } catch (e) { count = 0 }

          sendPayload('verifyElementCount', el, String(count));
          return sendResponse({ ok: true });
        }
      } catch (e) { /* swallow */ }
    }
  });

  // request current state on load
  chrome.runtime.sendMessage({ type: 'get-state' }, resp => {
    try { if (resp && typeof resp.recording !== 'undefined') recording = !!resp.recording; } catch (e) { }
  });

  function capturePreSnapshot(el) {
    try {
      if (!el) return;
      if (!(el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
      preSnapshots.set(el, snapshot(el));
      try { lastInteractedInput = el; } catch (e) { }
    } catch (e) { }
  }

  // capture locators before the user begins typing or interacting
  document.addEventListener('focusin', e => { try { capturePreSnapshot(e.target); } catch (e) { } }, true);
  document.addEventListener('mousedown', e => { try { capturePreSnapshot(e.target); } catch (e) { } }, true);
  document.addEventListener('pointerdown', e => { try { capturePreSnapshot(e.target); } catch (e) { } }, true);
  document.addEventListener('touchstart', e => { try { capturePreSnapshot(e.target); } catch (e) { } }, true);

  // cleanup preSnapshots on focusout if nothing was sent
  document.addEventListener('focusout', e => {
    try { if (e.target && preSnapshots.has && preSnapshots.has(e.target)) preSnapshots.delete(e.target); } catch (e) { }
  }, true);
})();
