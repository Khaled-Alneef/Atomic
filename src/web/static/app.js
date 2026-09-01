/* Atomic's Home and Discover.

   **The wheel is the browser's, untouched, and that is deliberate.**
   Chromium scrolls 120 CSS px per notch on Windows, and every Qt page in
   this app except Home does exactly the same - widgets._SmoothWheel's
   NOTCH_FLOOR_PX is 120 and only Home passes a notch_scale (0.7, because
   the owner asked for that page alone to be slower). So "the same
   sensitivity as Movies" is 120px a notch, which is what the browser
   already does. An earlier version multiplied it by 1.3 and had to take
   the wheel over to do it; that is what made these pages feel unlike the
   rest, and it is gone.

   Two things the browser does not do, and this file does:

     * the rows scroll sideways, and a browser gives a horizontal
       scroller no animation at all from a sideways gesture;
     * the scrollbar thumb, which a browser snaps to the pointer every
       frame - on a long page that is ten content pixels per pointer
       pixel, and it reads as stepping.
*/

const page = document.getElementById('page');
const rail = document.getElementById('rail');
const EMBED = location.search.indexOf('embed=1') >= 0;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

/* ---- talking back to Qt -------------------------------------------
   Embedded, a click has to reach the app: opening a details page is
   Qt's job and always was. window.open would put an actual browser
   window on screen, which is never what was wanted. */
addEventListener('keydown', function (e) {
  // Belt to webview2_host._accelerator's braces: if the accelerator
  // hook is unavailable, the page still hands these to the app.
  if (e.key === 'F11' || e.key === 'Escape') {
    tellHost({ action: 'key', key: e.key });
    e.preventDefault();
  }
});

function tellHost(message) {
  try {
    if (window.chrome && window.chrome.webview) {
      window.chrome.webview.postMessage(JSON.stringify(message));
    }
  } catch (err) { /* standalone, no host - ignore */ }
}

/* ---- rendering ---------------------------------------------------- */
function cardFor(row) {
  const card = el('div', 'card');
  const img = el('img');
  img.loading = 'lazy';
  img.decoding = 'async';
  img.width = 160; img.height = 216;
  img.alt = '';
  if (row.cover) img.src = row.cover;
  card.appendChild(img);
  card.appendChild(el('div', 't', row.title || ''));
  card.appendChild(el('div', 'm', row.meta || ''));
  card.addEventListener('click', function () {
    tellHost({ action: 'open', kind: row.kind || 'title',
               id: row.id || '', title: row.title || '',
               type: row.type || '', url: row.url || '' });
  });
  return card;
}

function heroFor(hero) {
  const box = el('div', 'hero' + (hero.poster ? ' poster' : ''));
  // A real <img>, not a CSS background. The banner is an element on the
  // page with the page's own ground around it - the same shape
  // widgets.HeroBanner has (300px tall, 28px corners, inset) - rather
  // than an image behind everything.
  const art = el('img', 'hart');
  art.src = hero.backdrop; art.alt = ''; art.decoding = 'async';
  box.appendChild(art);
  const inner = el('div', 'inner');
  if (hero.cover) {
    const art = el('img', 'herocover');
    art.src = hero.cover; art.alt = ''; art.decoding = 'async';
    inner.appendChild(art);
  }
  const text = el('div');
  if (hero.logo) {
    const logo = el('img', 'logo');
    logo.src = hero.logo; logo.alt = hero.title || '';
    text.appendChild(logo);
  } else {
    text.appendChild(el('h1', null, hero.title || ''));
  }
  // Joined with a middle dot, the way the details page writes its own
  // fact line. Drawn from what the entry knows, then replaced by
  // Cinemeta's runtime/years/rating when that arrives - the banner is
  // never left waiting on a network call.
  const bullets = el('p', 'bullets', (hero.bullets || []).join('  ·  '));
  text.appendChild(bullets);
  if (hero.id) {
    fetch('/api/hero?id=' + encodeURIComponent(hero.id))
      .then(function (r) { return r.json(); })
      .then(function (live) {
        if ((live.bullets || []).length) {
          bullets.textContent = live.bullets.join('  ·  ');
        }
      })
      .catch(function () { /* keep what the entry already told us */ });
  }
  if (hero.meta) text.appendChild(el('p', null, hero.meta));
  inner.appendChild(text);
  box.appendChild(inner);
  if (hero.id) {
    // The class carries the cursor, not an inline style: setting it
    // per-element made the page re-resolve the cursor on every hover
    // change, which showed up as the banner's pointer flickering.
    box.classList.add('clickable');
    box.addEventListener('click', function () {
      tellHost({ action: 'open', id: hero.id, title: hero.title || '' });
    });
  }
  return box;
}

function tileFor(row) {
  // Apps and Websites: the icon, the name, and what it opens - the
  // shape they had in the Qt Home. A website has no poster, and a
  // poster-shaped card for one is mostly empty space.
  const item = el('div', 'tile');
  const art = el('div', 'tart');
  if (row.cover) {
    const img = el('img');
    img.src = row.cover; img.alt = ''; img.decoding = 'async';
    art.appendChild(img);
  } else {
    art.appendChild(el('span', null, (row.title || '?').slice(0, 1).toUpperCase()));
  }
  const body = el('div', 'tbody');
  body.appendChild(el('div', 'tname', row.title || ''));
  if (row.meta) body.appendChild(el('div', 'tlink', row.meta));
  item.appendChild(art);
  item.appendChild(body);
  item.addEventListener('click', function () {
    tellHost({ action: 'open', kind: row.kind || 'title',
               id: row.id || '', title: row.title || '',
               type: row.type || '', url: row.url || '' });
  });
  return item;
}


/* ---- the banner's pager -------------------------------------------
   windows.home._HeroDash, in CSS. Its constants: 18px at rest, 30px
   lit, 6px tall, a 200ms tween that grows the incoming pill while the
   outgoing one shrinks, and HERO_SLIDE_INTERVAL_MS of 6000 between
   slides. The rotation re-arms whenever a pill is used, because a slide
   that changes as somebody reaches for it is worse than no rotation. */
const HERO_SLIDE_MS = 6000;

function heroCarousel(heroes) {
  const box = el('div', 'herobox');
  const slides = heroes.map(function (hero, i) {
    const slide = heroFor(hero);
    slide.classList.add('slide');
    if (i === 0) slide.classList.add('on');
    box.appendChild(slide);
    return slide;
  });

  let at = 0, timer = 0;
  const pager = el('div', 'pager');
  const pills = heroes.map(function (_hero, i) {
    const pill = el('div', 'pill' + (i === 0 ? ' on' : ''));
    pill.addEventListener('click', function () { show(i); });
    pager.appendChild(pill);
    return pill;
  });
  if (heroes.length > 1) box.appendChild(pager);

  function show(next) {
    if (next === at) return;
    slides[at].classList.remove('on');
    pills[at].classList.remove('on');
    at = (next + slides.length) % slides.length;
    slides[at].classList.add('on');
    pills[at].classList.add('on');
    arm();
  }

  function arm() {
    clearInterval(timer);
    if (slides.length > 1) {
      timer = setInterval(function () { show(at + 1); }, HERO_SLIDE_MS);
    }
  }

  arm();
  // Reaching for the banner stops it moving under the pointer.
  box.addEventListener('mouseenter', function () { clearInterval(timer); });
  box.addEventListener('mouseleave', arm);
  return box;
}

function sectionsInto(parent, sections) {
  sections.forEach(function (section) {
    if (!section.rows || !section.rows.length) return;
    const block = el('div', 'row');
    block.appendChild(el('h2', null, section.title));
    if (section.style === 'list') {
      const list = el('div', 'tiles');
      section.rows.forEach(function (row) { list.appendChild(tileFor(row)); });
      block.appendChild(list);
    } else {
      const strip = el('div', 'strip');
      section.rows.forEach(function (row) { strip.appendChild(cardFor(row)); });
      block.appendChild(strip);
    }
    parent.appendChild(block);
  });
}

/* ---- the reader ---------------------------------------------------
   One long strip of pictures, scrolled by the browser. The Qt reader
   this replaces built the same strip out of widgets and had to pace its
   own scrolling; here there is nothing to pace.

   **Every page reserves its true height before it loads**, taken from
   the first picture that arrives, because a scanlated chapter is
   uniform. Without it each image replaces a guessed box with its real
   one and everything below moves - once per page, twenty to two hundred
   times a chapter, which is the reader "jumping" as it loads. */
let readerState = { id: '', index: 0, key: '', total: 0 };

async function openChapter(id, index) {
  readerState = { id: id, index: index, key: '', total: 0 };
  page.scrollTop = 0;
  page.innerHTML = '';

  const topbar = el('div', 'rbar');
  // Shown on hover only - the owner's ask. A reach for the top
  // of the window brings it back; reading is otherwise
  // uninterrupted.
  const reach = el('div', 'rreach');
  page.appendChild(reach);
  const back = el('button', 'rbtn', '‹  Library');
  back.addEventListener('click', function () { tellHost({ action: 'close' }); });
  const prev = el('button', 'rbtn', 'Previous');
  const next = el('button', 'rbtn', 'Next');
  const label = el('div', 'rlabel', 'loading…');
  const list = el('button', 'rbtn', 'Chapters');
  topbar.appendChild(back);
  topbar.appendChild(prev);
  topbar.appendChild(label);
  topbar.appendChild(next);
  topbar.appendChild(list);
  page.appendChild(topbar);

  const strip = el('div', 'reader');
  page.appendChild(strip);

  let data;
  try {
    data = await (await fetch('/api/pages?id=' + encodeURIComponent(id) +
                              '&i=' + index)).json();
  } catch (err) {
    label.textContent = 'could not load';
    strip.appendChild(el('div', 'empty', 'could not load this chapter'));
    return;
  }
  if (data.error) {
    label.textContent = data.error;
    strip.appendChild(el('div', 'empty', data.error));
    return;
  }

  // reader.MEDIUM_TARGET_WIDTH, exactly.
  const WIDTHS = { manga: 1100, manhwa: 762, manhua: 762 };
  strip.style.setProperty('--pagew',
    (WIDTHS[data.medium] || 1100) + 'px');
  readerState.key = data.key || '';
  readerState.total = data.total || 0;
  label.textContent = (data.title ? data.title + '  ·  ' : '') +
    (data.label || '') + '  ·  ' + (data.count || 0) + ' pages';
  prev.disabled = index <= 0;
  next.disabled = index >= (data.total || 1) - 1;
  prev.addEventListener('click', function () { openChapter(id, index - 1); });
  next.addEventListener('click', function () { openChapter(id, index + 1); });
  list.addEventListener('click', function () { showChapters(id); });

  let measured = false;
  function learn(img) {
    if (!img.naturalWidth || !img.naturalHeight) return;
    // This page's own shape, always. A double-page spread is twice as
    // wide as a single one, and forcing it into the shared ratio is
    // what squashed it instead of fitting it to the width.
    img.style.aspectRatio = img.naturalWidth + ' / ' + img.naturalHeight;
    // **And a spread gets the whole width.** A single manga page is
    // drawn at reader.MEDIUM_TARGET_WIDTH (1100); a two-page spread is
    // about twice as wide, and holding it to the single-page column
    // left it small in the middle of the window instead of filling it.
    // 1.2 rather than exactly 2: scans are trimmed unevenly, and
    // nothing that is merely taller than wide should ever qualify.
    if (img.naturalWidth / img.naturalHeight > 1.2) {
      img.classList.add('spread');
    }
    if (measured) return;
    // The first one to arrive also becomes the estimate for every box
    // still unloaded, so nothing below moves as they come in.
    measured = true;
    strip.style.setProperty('--ar',
      img.naturalWidth + ' / ' + img.naturalHeight);
  }
  (data.pages || []).forEach(function (src, i) {
    const img = el('img', 'rpage');
    img.loading = i < 3 ? 'eager' : 'lazy';   // a chapter is 20-200 files
    img.decoding = 'async';
    img.alt = '';
    img.addEventListener('load', function () { learn(img); });
    img.src = src;
    if (img.complete) learn(img);
    strip.appendChild(img);
  });

  // Marked read on open, which is what the Qt reader records too.
  if (readerState.key) {
    fetch('/api/mark?id=' + encodeURIComponent(id) +
          '&key=' + encodeURIComponent(readerState.key))
      .catch(function () {});
  }
}

async function showChapters(id) {
  page.scrollTop = 0;
  page.innerHTML = '';
  const head = el('header');
  head.appendChild(el('h1', null, 'Chapters'));
  const note = el('p', null, 'reading the list…');
  head.appendChild(note);
  page.appendChild(head);
  const box = el('div', 'items');
  page.appendChild(box);

  let read = new Set();
  try {
    const state = await (await fetch('/api/read_state?id=' +
      encodeURIComponent(id))).json();
    read = new Set(state.watched || []);
  } catch (err) { /* the list is still worth showing */ }

  function fill(items, cached) {
    box.innerHTML = '';
    note.textContent = items.length
      ? items.length + ' chapters' + (cached ? '' : '  ·  fresh from the site')
      : 'nothing found';
    items.forEach(function (item) {
      const line = el('div', 'ep' + (read.has(item.key) ? ' done' : ''));
      line.appendChild(el('span', 'epn', item.label));
      if (item.sub) line.appendChild(el('span', 'eps', item.sub));
      line.addEventListener('click', function () { openChapter(id, item.i); });
      box.appendChild(line);
    });
  }

  try {
    const cached = await (await fetch('/api/chapters?id=' +
      encodeURIComponent(id))).json();
    if ((cached.items || []).length) fill(cached.items, true);
  } catch (err) { /* fall through to the live fetch */ }

  // The full list is a site fetch - measured 21.7s cold on a
  // 249-chapter entry - so it is asked for only after whatever was
  // cached is already on screen (rule 7).
  try {
    const live = await (await fetch('/api/chapters?live=1&id=' +
      encodeURIComponent(id))).json();
    if ((live.items || []).length) fill(live.items, false);
    else if (live.error) note.textContent = live.error;
  } catch (err) {
    note.textContent = 'could not read the chapter list';
  }
}

/* ---- routing ------------------------------------------------------ */
let token = 0;

async function go(route) {
  const mine = ++token;
  location.hash = route;
  // read/<id>/<index> and chapters/<id> are the reader. They draw
  // themselves rather than going through the sections path below.
  const bits = route.split('/');
  if (bits[0] === 'read') {
    openChapter(decodeURIComponent(bits[1] || ''),
                parseInt(bits[2] || '0', 10));
    return;
  }
  if (bits[0] === 'chapters') {
    showChapters(decodeURIComponent(bits[1] || ''));
    return;
  }
  page.scrollTop = 0;
  page.innerHTML = '';

  let data;
  try {
    data = await (await fetch('/api/' + route)).json();
  } catch (err) {
    page.appendChild(el('div', 'empty', 'could not load'));
    return;
  }
  if (mine !== token) return;              // a later click won

  const heroes = data.heroes || (data.hero ? [data.hero] : []);
  if (heroes.length) page.appendChild(heroCarousel(heroes));
  if (data.note) {
    const head = el('header');
    head.appendChild(el('p', null, data.note));
    page.appendChild(head);
  }
  sectionsInto(page, data.sections || []);
  if (!(data.sections || []).some(function (s) { return s.rows.length; })) {
    page.appendChild(el('div', 'empty', 'Nothing here yet.'));
  }
  bar.layout();
}

/* ---- sideways gestures on a row -----------------------------------
   Chromium's own curve, so a row moves the way the page does:
   cc::ScrollOffsetAnimationCurve times a scroll as
   clamp(14 - |delta| / 60, 6, 12) sixtieths of a second and eases it
   with cubic-bezier(0.42, 0, 0.58, 1). */
function bezier(x1, y1, x2, y2) {
  const ax = 3 * x1 - 3 * x2 + 1, bx = 3 * x2 - 6 * x1, cx = 3 * x1;
  const ay = 3 * y1 - 3 * y2 + 1, by = 3 * y2 - 6 * y1, cy = 3 * y1;
  const atX = t => ((ax * t + bx) * t + cx) * t;
  const slope = t => (3 * ax * t + 2 * bx) * t + cx;
  return function (x) {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 5; i++) {
      const d = slope(t);
      if (Math.abs(d) < 1e-6) break;
      const err = atX(t) - x;
      if (Math.abs(err) < 1e-6) break;
      t -= err / d;
    }
    t = Math.min(1, Math.max(0, t));
    return ((ay * t + by) * t + cy) * t;
  };
}

const EASE = bezier(0.42, 0, 0.58, 1);

function sideScroller(node) {
  let raf = 0, from = 0, to = 0, began = 0, span = 0;
  function step(now) {
    const done = Math.min(1, (now - began) / span);
    node.scrollLeft = from + (to - from) * EASE(done);
    raf = done < 1 ? requestAnimationFrame(step) : 0;
  }
  return function (delta) {
    const base = raf ? to : node.scrollLeft;
    const limit = node.scrollWidth - node.clientWidth;
    const wanted = Math.max(0, Math.min(limit, base + delta));
    if (wanted === node.scrollLeft && !raf) return false;
    span = Math.min(12, Math.max(6, 14 - Math.abs(wanted - node.scrollLeft) / 60))
           / 60 * 1000;
    from = node.scrollLeft; to = wanted; began = performance.now();
    if (!raf) raf = requestAnimationFrame(step);
    return true;
  };
}

page.addEventListener('wheel', function (e) {
  if (e.ctrlKey) return;
  const strip = e.target.closest ? e.target.closest('.strip') : null;
  const sideways = e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY);
  if (!strip || !sideways) return;         // the page's wheel is the browser's
  if (strip.scrollWidth <= strip.clientWidth + 1) return;
  if (!strip._side) strip._side = sideScroller(strip);
  if (strip._side(e.shiftKey ? e.deltaY : e.deltaX)) e.preventDefault();
}, { passive: false });

/* ---- the scrollbar ------------------------------------------------
   **Velocity, not a target.** The first version eased toward wherever
   the pointer last was, and the owner still called it stepped. Measured
   on his own drag: frames were arriving perfectly (4.20ms median, one
   miss in 1194), but his mouse reports at ~125Hz against 238Hz of
   frames, so between two pointer samples the remaining gap shrank until
   a fraction of it was under a pixel and the page could not move at
   all. Jump, decay, freeze, jump - every two frames.

   So the thumb now carries a speed. Each pointer sample updates it, and
   every frame in between advances by that speed, with a gentle pull
   back toward the true position so it can never drift. Motion is
   continuous at the panel's rate rather than restarting 125 times a
   second. */
const bar = (function () {
  const track = document.getElementById('sb');
  const thumb = document.getElementById('th');
  let dragging = false, raf = 0;
  let wanted = 0, shown = 0, speed = 0, lastFrame = 0, lastSample = 0;

  function layout() {
    const view = page.clientHeight, all = page.scrollHeight;
    if (all <= view + 1) { thumb.style.display = 'none'; return; }
    thumb.style.display = 'block';
    const h = Math.max(36, view * view / all);
    thumb.style.height = h + 'px';
    thumb.style.top = (view - h) * (page.scrollTop / (all - view)) + 'px';
  }

  function follow(now) {
    raf = 0;
    if (!dragging) return;
    const dt = lastFrame ? Math.min(48, now - lastFrame) : 8;
    lastFrame = now;

    // Carry on at the last known speed, and lean toward the truth. The
    // 0.012 gain closes a gap in about five frames at 240Hz - fast
    // enough that a direction change is not felt as lag, slow enough
    // that it never becomes the jump this replaced.
    shown += speed * dt + (wanted - shown) * Math.min(1, 0.012 * dt);
    const limit = page.scrollHeight - page.clientHeight;
    shown = Math.max(0, Math.min(limit, shown));
    page.scrollTop = shown;
    raf = requestAnimationFrame(follow);
  }

  function aim(clientY) {
    const view = page.clientHeight, all = page.scrollHeight;
    const h = Math.max(36, view * view / all);
    let target = (clientY - h / 2) / Math.max(1, view - h) * (all - view);
    target = Math.max(0, Math.min(all - view, target));

    const now = performance.now();
    if (lastSample) {
      const gap = now - lastSample;
      if (gap > 0.5 && gap < 200) {
        // Smoothed: one late sample should bend the speed, not define it.
        const fresh = (target - wanted) / gap;
        speed = speed * 0.35 + fresh * 0.65;
      }
    }
    lastSample = now;
    wanted = target;
    if (!raf) raf = requestAnimationFrame(follow);
  }

  track.addEventListener('pointerdown', function (e) {
    dragging = true;
    wanted = shown = page.scrollTop;
    speed = 0; lastFrame = 0; lastSample = 0;
    thumb.classList.add('on');
    track.setPointerCapture(e.pointerId);
    aim(e.clientY);
    e.preventDefault();
  });
  track.addEventListener('pointermove', function (e) {
    if (dragging) aim(e.clientY);
  });
  function release(e) {
    if (!dragging) return;
    dragging = false;
    speed = 0;
    thumb.classList.remove('on');
    try { track.releasePointerCapture(e.pointerId); } catch (err) {}
    page.scrollTop = wanted;
  }
  track.addEventListener('pointerup', release);
  track.addEventListener('pointercancel', release);
  page.addEventListener('scroll', layout, { passive: true });
  addEventListener('resize', layout);
  return { layout: layout };
})();

/* ---- start -------------------------------------------------------- */
if (EMBED) document.body.classList.add('embed');
else {
  // Standalone, the page draws its own two rows so it can be opened in a
  // browser and compared side by side. Embedded, Qt owns the sidebar.
  const brand = el('div'); brand.id = 'brand';
  brand.appendChild(el('span', null, '▲'));
  brand.appendChild(el('b', null, 'Atomic'));
  rail.appendChild(brand);
  [['home', 'Home'], ['discover', 'Discover']].forEach(function (row) {
    const item = el('div', 'nav');
    item.dataset.route = row[0];
    item.appendChild(el('span', null, row[1]));
    item.addEventListener('click', function () {
      rail.querySelectorAll('.nav').forEach(function (n) {
        n.classList.toggle('on', n === item);
      });
      go(row[0]);
    });
    rail.appendChild(item);
  });
}
go((location.hash || '#home').slice(1) || 'home');
