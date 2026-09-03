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
/* **The two side buttons, from the page.** main._MouseNavFilter is
   installed on the application and catches these everywhere else, but a
   native child window is given the click by Windows and Qt never sees
   it - the same reason windows/player polls them over the bare picture
   rather than binding them. Buttons 3 and 4 are Back and Forward.
   `auxclick` rather than `mouseup`, so the browser's own back gesture
   does not also fire. */
addEventListener('auxclick', function (e) {
  if (e.button !== 3 && e.button !== 4) return;
  tellHost({ action: 'key', key: e.button === 3 ? 'Alt+Left' : 'Alt+Right' });
  e.preventDefault();
});
// Chromium navigates the *page* on these unless the default is stopped
// here too; there is nowhere to navigate to and it would strand the view
// on a blank document.
addEventListener('mouseup', function (e) {
  if (e.button === 3 || e.button === 4) e.preventDefault();
});

addEventListener('keydown', function (e) {
  // Belt to webview2_host._accelerator's braces: if the accelerator
  // hook is unavailable, the page still hands these to the app.
  if (e.key === 'F11' || e.key === 'Escape') {
    tellHost({ action: 'key', key: e.key });
    e.preventDefault();
  }
});

// **A picture that fails shows nothing at all** - the owner, 1 September
// 2026: "when an image do not load ... make it simply shows nothing at
// all, no frame no image icon". Chromium draws a broken-image glyph in a
// box the size of the element, and where that element has a border (the
// banner's cover has one) the result is an empty framed rectangle with a
// torn-page icon in the corner, which is his image 2.
//
// One listener in the capture phase, because `error` on an <img> does not
// bubble - so this catches every image on every page, including ones
// added after it was installed, without a handler at each call site.
addEventListener('error', function (e) {
  const node = e.target;
  if (node && node.tagName === 'IMG') {
    node.style.display = 'none';
    node.removeAttribute('src');
  }
}, true);

function tellHost(message) {
  try {
    if (window.chrome && window.chrome.webview) {
      window.chrome.webview.postMessage(JSON.stringify(message));
    }
  } catch (err) { /* standalone, no host - ignore */ }
}

/* ---- the sidebar fold, carried by the page ------------------------
   The owner, 2 September 2026: "the cards transition on the fold/unfold
   the sidebar in the watch/read pages is not smooth at all!!!"

   What the window can do for a native view during its 180ms fold is
   move it (main._toggle_sidebar); what it must not do is resize it per
   step, because every resize is a full re-layout of this document
   (measured 11 per fold, and "they move in clear steps"). So the window
   sends the width the page will finish at, and the page does the rest
   here: lay the grid out at that width ONCE, then slide every visible
   card from where it was to where it now is on the compositor - one
   transform animation per card, no layout per frame - on the same
   curve for the same time as the rail, so a card's place on the glass
   (window position + place in the page, both eased alike) is one
   eased line from old to new.

   Only #page's own width is stepped, so the scrollbar rides the visible
   edge rather than sitting off screen or in the middle; the grid is
   given its final width outright so that stepping lays out one block,
   not two hundred cards. Measured 3 September 2026 in this document,
   60 cards: one forced layout after a #page width change costs 0.02ms
   with the grid's width pinned, 0.34ms when the grid re-flows with it;
   preparing a fold (rects, layout, 45 animations) costs 1.1-1.5ms; and
   the main thread does under 2.4ms of work in any frame of the fold. */
const FOLD_CURVE = 'cubic-bezier(0.215, 0.61, 0.355, 1)'; // ease-out cubic
let foldState = null;

function endFold() {
  const st = foldState;
  foldState = null;
  if (!st) return;
  if (st.raf) cancelAnimationFrame(st.raf);
  if (st.timer) clearTimeout(st.timer);
  // A running slide is left to finish - a fold cut short re-aims the
  // cards from where they look to be (hostFold's `before`). A slide
  // that never started, though - fold.done arrived before fold.go, a
  // second click or a page change inside the ack window - would hold
  // its first keyframe's transform for ever (review, 3 September
  // 2026), so those are cancelled.
  if (!st.going) (st.anims || []).forEach(function (a) { try { a.cancel(); } catch (e) {} });
  page.style.width = '';
  if (st.grid && st.grid.isConnected) st.grid.style.width = '';
  // Pictures for the cards the fold brought near the viewport - the
  // lazy sweep listens for resize, and an unfold never resizes the
  // viewport. After the slide, not during it: fired at the start, the
  // fetches and decodes it set off held the unfold at 16.7ms frames
  // for 13 of its 43 (measured 3 September 2026); the fold, whose new
  // layout is shorter and already loaded, ran clean at 4.2ms.
  dispatchEvent(new Event('resize'));
}

/* Two messages from the window per fold, and two answers. `fold` (to,
   view, ms, curve): lay out at the final width, make every card's
   slide, hold it at its first frame, answer `ok`. The window then sizes
   the view to that width and waits for `sized` - sent once a frame at
   the new size has been drawn, because Edge shows its old frame in the
   new box for ~16ms after a resize and a rail that left at once had the
   cards frozen for its first four frames. `fold.go` (at): the rail has
   started, at that wall-clock instant - Date.now() reads the same clock
   as the window's time.time() - so the slides are set to the time
   already gone and run in step with the rail however long the answers
   took. `fold.done`: drop the widths. */
function hostFold(f) {
  if (!f) return;
  if (f.done) { endFold(); return; }
  if (f.go) { goFold(f); return; }
  const t0 = performance.now();
  const grid = page.querySelector('.grid');
  const cards = grid ? Array.prototype.slice.call(grid.querySelectorAll('.gc')) : [];
  // Nothing here to carry: no answer, and the window keeps its old way.
  if (!cards.length) return;
  // The window's pixel is not this page's (Qt 1.3333 against Chromium
  // 1.25 on the owner's panel, measured 3 September 2026): the width it
  // names is scaled by what its own view measures here.
  const scale = f.view ? innerWidth / f.view : 1;
  const to = Math.max(1, Math.round((f.to || 0) * scale));
  const ms = Math.max(1, f.ms || 180);
  const curve = f.curve || FOLD_CURVE;
  // Where every card *looks* to be right now - a card mid-slide from a
  // fold that was just cut short answers with its transformed box, so
  // a second click re-aims from there instead of jumping.
  const before = cards.map(function (c) { return c.getBoundingClientRect(); });
  const from = foldState ? foldState.width : page.getBoundingClientRect().width;
  endFold();
  const bar = page.offsetWidth - page.clientWidth;      // the scrollbar
  const top = page.scrollTop;
  page.style.width = Math.round(from) + 'px';
  grid.style.width = Math.max(1, to - bar) + 'px';
  page.scrollTop = top;
  const after = cards.map(function (c) { return c.getBoundingClientRect(); });
  const seen = innerHeight + 320;
  const anims = [];
  cards.forEach(function (c, i) {
    const a = before[i], b = after[i];
    if ((a.bottom < -320 || a.top > seen) && (b.bottom < -320 || b.top > seen)) return;
    // Centres, not left edges: the card's box is as wide as its column
    // and the column changes, while the poster inside stays centred.
    const dx = (a.left + a.width / 2) - (b.left + b.width / 2);
    const dy = a.top - b.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
    const anim = c.animate([{ transform: 'translate(' + dx + 'px,' + dy + 'px)' },
                            { transform: 'none' }], { duration: ms, easing: curve });
    anim.pause();
    anims.push(anim);
  });
  const st = { seq: f.seq, grid: grid, from: from, to: to, ms: ms,
               width: from, anims: anims, raf: 0, timer: 0,
               going: false, sized: false };
  foldState = st;
  // The window says when it has landed (fold.done); if it never does -
  // the page was rebuilt underneath, say - the widths are dropped anyway.
  st.timer = setTimeout(function () { if (foldState === st) endFold(); }, ms + 600);
  tellHost({ action: 'fold', seq: f.seq, ok: 1, from: from, to: to,
             w: innerWidth, n: cards.length, moved: anims.length,
             cost: +(performance.now() - t0).toFixed(2), now: Date.now() });
}

function goFold(f) {
  const st = foldState;
  if (!st || st.seq !== f.seq || st.going) return;
  st.going = true;
  const gone = Math.max(0, Math.min(st.ms, Date.now() - (f.at || Date.now())));
  st.anims.forEach(function (a) { a.currentTime = gone; a.play(); });
  const t0 = performance.now() - gone;
  // #page's width follows the visible edge, so the scrollbar rides it -
  // as far as the view goes: on an unfold the view is already at the
  // final width and the strip beyond it is the window's, not ours.
  function step(now) {
    if (foldState !== st) return;
    const p = Math.min(1, (now - t0) / st.ms);
    const e = 1 - Math.pow(1 - p, 3);
    st.width = Math.min(innerWidth, st.from + (st.to - st.from) * e);
    page.style.width = Math.round(st.width) + 'px';
    if (p < 1) st.raf = requestAnimationFrame(step);
  }
  st.raf = requestAnimationFrame(step);
  tellHost({ action: 'fold', seq: f.seq, went: 1, gone: gone });
}

function hostMessage(ev) {
  let m = ev.data;
  if (typeof m === 'string') {
    try { m = JSON.parse(m); } catch (err) { return; }
  }
  if (m && m.fold) hostFold(m.fold);
}
try {
  if (window.chrome && window.chrome.webview) {
    window.chrome.webview.addEventListener('message', hostMessage);
  }
} catch (err) { /* standalone */ }
// The view being sized for a fold: once a frame at the new size has
// gone out (the rAF after the resize is that frame; the timeout runs
// after it is committed), the window may start the rail.
addEventListener('resize', function () {
  const st = foldState;
  if (!st || st.going || st.sized) return;
  st.sized = true;
  requestAnimationFrame(function () {
    setTimeout(function () {
      if (foldState === st) tellHost({ action: 'fold', seq: st.seq, sized: 1 });
    }, 0);
  });
});

/* ---- rendering ---------------------------------------------------- */

/* **Ask for the art at the size it will be drawn.** A cover is 844x1200
   and a card draws it 160 CSS wide - 200 device pixels at DPR 1.25 - so
   the browser was downscaling by four on every paint, which is what the
   owner sees as blurry. windows/games.py never does that: Qt decodes at
   the target size. The server scales and caches when asked (`?w=`). */
function artURL(src, cssWidth) {
  if (!src || src.indexOf('?') >= 0) return src;
  const want = Math.round(cssWidth * (window.devicePixelRatio || 1));
  return src + '?w=' + want;
}

/* **`loading="lazy"` does not work on this page, and it fails silently
   into blank cards.** The owner: "fix the games and apps images."

   Measured 2 September 2026, in the page, against the real data. Open
   Games from another route and ten seconds later all ten covers are
   still `complete === false`, `naturalWidth === 0`, with **no network
   request ever issued** (performance.getEntriesByType('resource') has
   no entry for them). Controls, run in the same document:

     * the identical image with `loading = 'eager'` completes in <400ms;
     * scrolling the scroller and firing a resize kick nothing loose;
     * `content-visibility: visible` on the card changes nothing, so the
       card's containment is not the cause;
     * a bare lazy <img> appended straight to <body> at a fixed position
       inside the viewport never loads either - so it is not the grid,
       the card, or the order src is set in.

   The likeliest mechanism - not itself measured, so read it as the
   theory the controls left standing - is the page's own shape: `html`
   and `body` are `overflow: hidden` and the scrolling happens inside
   `<main>`, so the document has nothing to scroll and Chromium's
   deferred-image pass, which is driven off the document viewport, runs
   at first load and not again. What *is* measured either way is that
   every route reached by clicking in the app is a hash render on that
   same document, and that those renders load nothing: Games and Apps
   are never the first page opened, which is why they were the two he
   named, and Home on a cold load was fine.

   So the page does its own deferring, against the element that actually
   scrolls. Same benefit - nothing off screen is fetched - and it cannot
   be quietly skipped, because it is this file that decides.

   **By rectangle, not by IntersectionObserver**, which was tried first
   and measured failing the same way for a different reason: an observer
   delivers its entries inside the rendering lifecycle, so a view that
   is not producing frames never gets a callback - and this app suspends
   the view whenever an overlay covers it (windows/web_pages._check_covered).
   Reading a rectangle needs layout, which happens either way.

   After: Games' ten covers all decoded 21ms after the route changed,
   and History and Schedule load the rows on screen and hold the rest,
   which is what the deferring is for. */
/* How far outside the scroller a picture is fetched. Generous on
   purpose: it is the difference between a card arriving before it is
   looked at and arriving after. */
const LAZY_MARGIN_PX = 800;

const lazyPending = new Set();
let lazySweepQueued = false;

/* Everything the last render was waiting on died with it. */
function resetLazy() { lazyPending.clear(); lazySince = 0; lazyTold = false; }

/* Give `img` its picture when it comes near the scroller. A width of 0
   means the URL is already final - the reader's pages come through the
   app's own proxy and must not be scaled to a card. */
// When the first picture of a render was queued, so a render that never
// gets its pictures can say so instead of being reported as blank.
let lazySince = 0;
let lazyTold = false;

function lazyArt(img, src, cssWidth) {
  img.decoding = 'async';
  if (!lazySince) { lazySince = Date.now(); lazyTold = false; }
  if (!src) {
    // **A flat slab, not an empty outline.** images.blank_tile is what
    // Qt draws with nothing to show, at the owner's own instruction -
    // "completely empty until the image loads, whole app" - and a
    // src-less <img> is a hole with a placeholder border in it instead.
    // Apps had one (Stremio), Websites two.
    img.classList.add('blank');
    return img;
  }
  img.setAttribute('data-src', cssWidth ? artURL(src, cssWidth) : src);
  lazyPending.add(img);
  queueLazySweep();
  return img;
}

function queueLazySweep() {
  if (lazySweepQueued) return;
  lazySweepQueued = true;
  setTimeout(sweepLazy, 0);
}

function take(img) {
  lazyPending.delete(img);
  const want = img.getAttribute('data-src');
  if (want) { img.removeAttribute('data-src'); img.src = want; }
}

function sweepLazy() {
  lazySweepQueued = false;
  if (!lazyPending.size) return;
  const box = page.getBoundingClientRect();
  // **A render that still has no pictures after four seconds says so.**
  // Blank cards have been reported three times and reproduced none of
  // them here (games and apps both render in the frozen app, measured),
  // so the page reports its own state into atomic.log rather than
  // leaving the next report to be another guess.
  if (!lazyTold && lazySince && Date.now() - lazySince > 4000) {
    lazyTold = true;
    tellHost({ action: 'diag', what: 'pictures still waiting',
               pending: lazyPending.size, images: document.images.length,
               box: Math.round(box.width) + 'x' + Math.round(box.height),
               view: innerWidth + 'x' + innerHeight,
               dpr: window.devicePixelRatio || 1,
               route: location.hash });
  }
  if (!box.height) {
    /* **No viewport to measure against - so fetch a screenful anyway.**
       A WebView2 that has not been sized yet lays every box out at zero
       height, so "is this near the viewport" has no answer and the
       first version of this waited, retrying, until one arrived.
       Measured against the frozen build, 2 September 2026: the ten
       Games covers were requested **8,092ms** after the page rendered,
       and every one of them landed 31ms later - the pictures were never
       slow, they were never asked for. That is the owner's "apps and
       games has no images still".

       A shelf is 5-50 cards and a grid's first screenful is about two
       dozen, so this budget covers a shelf completely and the top of a
       grid. The rest keep their place in the queue and are picked up by
       the retry once there is a viewport to measure. */
    let budget = 24;
    lazyPending.forEach(function (img) {
      if (budget-- > 0) take(img);
    });
    setTimeout(queueLazySweep, 60);
    return;
  }
  const top = box.top - LAZY_MARGIN_PX;
  const bottom = box.bottom + LAZY_MARGIN_PX;
  lazyPending.forEach(function (img) {
    if (!img.isConnected) { lazyPending.delete(img); return; }
    const at = img.getBoundingClientRect();
    if (at.bottom < top || at.top > bottom) return;
    // Sideways too: a strip is a scroller of its own, and a card three
    // arrows along should not be fetched until it is scrolled to.
    if (at.right < box.left - LAZY_MARGIN_PX
        || at.left > box.right + LAZY_MARGIN_PX) return;
    take(img);
  });
}

// Capture, so the sideways rows' own scrolling arrives here too - a
// scroll event does not bubble, but it does propagate downwards.
addEventListener('scroll', queueLazySweep, true);
addEventListener('resize', queueLazySweep);

function cardFor(row) {
  const card = el('div', 'card');
  // The frost is part of the ring, so a card without one is a plain
  // cover - a Discover result has nothing to resume.
  const art = el('div', row.resume ? 'art' : 'art plain');
  const img = el('img');
  img.width = 160; img.height = 216;
  img.alt = '';
  lazyArt(img, row.cover, 160);
  art.appendChild(img);
  if (row.resume) {
    // tracker.ContinueCover: the ring resumes, the rest of the card
    // opens the list. Two targets on one card, as Home always had.
    const ring = el('div', 'ring');
    ring.title = 'Continue';
    ring.addEventListener('click', function (e) {
      e.stopPropagation();          // the body must not also fire
      tellHost({ action: 'open', mode: 'continue', kind: row.kind || 'title',
                 id: row.id || '', title: row.title || '',
                 type: row.type || '', url: row.url || '' });
    });
    art.appendChild(ring);
  }
  card.appendChild(art);
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
  const art_el = el('img', 'hart');
  art_el.src = hero.backdrop; art_el.alt = ''; art_el.decoding = 'async';
  box.appendChild(art_el);
  const inner = el('div', 'inner');
  if (hero.cover) {
    const art = el('img', 'herocover');
    art.src = hero.cover; art.alt = ''; art.decoding = 'async';
    inner.appendChild(art);
  }
  const text = el('div', 'col');
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
  // **Four lines, not one run** - the owner's format, 1 September 2026:
  // the runtime, then the rating and the year, then the genres, then the
  // schedule. Held in a box so the live answer from /api/hero can
  // replace all of them at once; an empty line is skipped rather than
  // drawn, which is the rule the bullet run followed before it.
  const bullets = el('div', 'bullets');
  function writeLines(lines) {
    bullets.innerHTML = '';
    (lines || []).forEach(function (line) {
      if (line) bullets.appendChild(el('p', null, line));
    });
  }
  // `meta` is a list of lines - the schedule is drawn as "Next
  // Chapter: N" and the timing under it (backend.schedule_lines). A
  // plain string is still honoured, which is what the Discover banner
  // and any older answer send.
  const metaLines = Array.isArray(hero.meta) ? hero.meta
                    : (hero.meta ? [hero.meta] : []);
  writeLines((hero.bullets || []).concat(metaLines));
  text.appendChild(bullets);
  if (hero.poster && hero.title) {
    // Standing in with the cover, so the wide still and the title
    // treatment come from TMDB by name - the same two calls
    // tracker._featured_backdrop_worker makes. Two banners land here: a
    // discover title, which has no saved entry at all, and a library
    // entry helpers/hero_art has not yet given hero_backdrop to.
    fetch('/api/featured?title=' + encodeURIComponent(hero.title) +
          '&imdb=' + encodeURIComponent(hero.imdb || '') +
          '&type=' + encodeURIComponent(hero.type || ''))
      .then(function (r) { return r.json(); })
      .then(function (art) {
        if (art.backdrop) {
          box.classList.remove('poster');
          art_el.src = art.backdrop;
        }
        if (art.logo) {
          const logo = el('img', 'logo');
          logo.src = art.logo; logo.alt = hero.title || '';
          const first = text.firstChild;
          if (first && first.tagName === 'H1') text.removeChild(first);
          text.insertBefore(logo, text.firstChild);
        }
      })
      .catch(function () { /* the poster stays */ });
  }
  if (hero.id) {
    fetch('/api/hero?id=' + encodeURIComponent(hero.id))
      .then(function (r) { return r.json(); })
      .then(function (live) {
        if ((live.bullets || []).some(function (b) { return b; })) {
          writeLines(live.bullets.concat(metaLines));
        }
      })
      .catch(function () { /* keep what the entry already told us */ });
  }
  inner.appendChild(text);
  // The backdrop itself opens the list, as a card body does everywhere
  // else here. Lost when the buttons went on; the buttons stop their own
  // clicks so the two never both fire.
  box.classList.add('clickable');
  box.addEventListener('click', function () {
    tellHost({ action: 'open', kind: 'title', id: hero.id || '',
               title: hero.title || '', type: hero.type || '',
               url: hero.url || '', poster: hero.cover || '',
               imdb: hero.imdb || '' });
  });

  // Home's two hero actions: Continue resumes where the entry stopped,
  // the outlined one opens the episode or chapter list. Same pair the Qt
  // banner carries, and the same wording it uses. Discover's banner gets
  // them too - its title is not in the library, so both open the list.
  if (hero.id || hero.title) {
    const acts = el('div', 'acts');
    const go = el('button', 'act go', '\u25B6  Continue');
    go.addEventListener('click', function (e) {
      e.stopPropagation();
      tellHost({ action: 'open', mode: hero.id ? 'continue' : '',
                 kind: 'title', id: hero.id || '',
                 title: hero.title || '', type: hero.type || '',
                 url: hero.url || '', poster: hero.cover || '',
                 imdb: hero.imdb || '' });
    });
    acts.appendChild(go);

    const reading = ['manga', 'manhwa', 'manhua'];
    const kind = (hero.type || '').toLowerCase();
    const label = reading.indexOf(kind) >= 0 ? 'View Chapters'
      : (kind.indexOf('movie') === 0 ? 'View Details' : 'View Episodes');
    const view = el('button', 'act quiet', label);
    view.addEventListener('click', function (e) {
      e.stopPropagation();
      tellHost({ action: 'open', kind: 'title', id: hero.id || '',
                 title: hero.title || '', type: hero.type || '',
                 url: hero.url || '', poster: hero.cover || '',
                 imdb: hero.imdb || '' });
    });
    acts.appendChild(view);
    text.appendChild(acts);
  }
  box.appendChild(inner);
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
    img.src = artURL(row.cover, 28); img.alt = ''; img.decoding = 'async';
    art.appendChild(img);
  } else {
    art.appendChild(el('span', null, (row.title || '?').slice(0, 1).toUpperCase()));
  }
  const body = el('div', 'tbody');
  body.appendChild(el('div', 'tname', row.title || ''));
  item.appendChild(art);
  item.appendChild(body);
  // The one thing the Qt row says besides the name: that it cannot
  // launch any more. Right-aligned, as link_grid draws it.
  if (row.missing) {
    item.appendChild(el('div', 'tgone', row.missing));
    if (row.missing_paths) {
      item.title = 'No longer on disk:\n' + row.missing_paths.join('\n');
    }
  }
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
      // **A panel, beside its neighbour.** home._build_quick_list makes
      // "Quick Apps" and "Websites" two columns of a single row, each a
      // titled list of icon-and-name - not two full-width grids with a
      // path under every entry, which is what this drew before.
      block.classList.add('quick');
      const list = el('div', 'tiles');
      section.rows.forEach(function (row) { list.appendChild(tileFor(row)); });
      block.appendChild(list);
    } else {
      const strip = el('div', 'strip');
      const make = parent.dataset.cardstyle === 'status' ? statusCard : cardFor;
      section.rows.forEach(function (row) { strip.appendChild(make(row)); });
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
let readerState = { id: '', index: 0, key: '', total: 0, zoom: 1 };
// The reader's key handler, so re-opening a chapter replaces it
// rather than stacking a second one that also changes chapter.
let readerKeys = null;
// The strip's ResizeObserver, for the same reason - a re-opened chapter
// replaces it rather than leaving the old one sizing a detached strip.
let readerResize = null;

async function openChapter(id, index) {
  // The zoom survives a chapter change - it is the reader's setting,
  // not the chapter's (it reset to 100% on every Next; review, 3
  // September 2026).
  readerState = { id: id, index: index, key: '', total: 0,
                  zoom: readerState.zoom || 1 };
  page.scrollTop = 0;
  page.innerHTML = '';

  // **The top bar, control for control as windows/reader builds it.**
  // Its _build_bar puts the way out and the chapter list at the far
  // left with the chapter's own number beside them, the title centred on
  // the *window* rather than between the two clusters (they are
  // different widths, so a centred flex item would sit off centre by
  // half their difference - reader's own note), and to the right: full
  // screen, then the size buttons around the percentage, then download,
  // refresh and open-in-browser. The glyphs are the same codepoints from
  // the same font, so these are the icons the Qt bar draws.
  const topbar = el('div', 'rbar');
  // Shown on hover only - the owner's ask. A reach for the top of the
  // window brings it back; reading is otherwise uninterrupted.
  const reach = el('div', 'rreach');
  page.appendChild(reach);

  const left = el('div', 'rgroup');
  const back = el('button', 'rglyph', '\ue76b');      // ChevronLeft
  back.title = 'Leave the reader (Esc)';
  back.addEventListener('click', function () { tellHost({ action: 'close' }); });
  const listBtn = el('button', 'rglyph', '\ue8fd');   // List
  listBtn.title = 'Back to the chapter list';
  const num = el('div', 'rnum', '');
  left.appendChild(back); left.appendChild(listBtn); left.appendChild(num);

  const title = el('div', 'rtitle', '');
  const label = el('div', 'rlabel', 'loading\u2026');

  const right = el('div', 'rgroup rright');
  const full = el('button', 'rglyph', '\ue740');      // FullScreen
  full.title = 'Full screen (F)';
  full.addEventListener('click', function () {
    tellHost({ action: 'key', key: 'F11' });
  });
  const zoomOut = el('button', 'rglyph', '\u2212');
  zoomOut.title = 'Smaller (\u2212)';
  const zoomLabel = el('div', 'rzoom', '100%');
  zoomLabel.title = "Page size - 100% is the image's own size (0 resets)";
  const zoomIn = el('button', 'rglyph', '+');
  zoomIn.title = 'Bigger (+)';
  const download = el('button', 'rglyph', '\ue896');  // Download
  download.title = 'Download this chapter';
  const refresh = el('button', 'rglyph', '\ue72c');   // Refresh
  refresh.title = 'Reload the chapter (R)';
  const browser = el('button', 'rglyph', '\ue774');   // Globe
  browser.title = 'Open in browser';
  [full, zoomOut, zoomLabel, zoomIn, download, refresh, browser]
    .forEach(function (node) { right.appendChild(node); });

  topbar.appendChild(left);
  topbar.appendChild(title);
  topbar.appendChild(right);
  topbar.appendChild(label);
  page.appendChild(topbar);

  // **The chapter controls, on the floor.** reader.BOTTOM_HEIGHT is 60
  // and _build_bottom puts them left to right as previous, the jump
  // list, next - the list being a dropdown that changes chapter without
  // leaving the page, which is a different thing from the top bar's
  // button back to the chapter list.
  const floorReach = el('div', 'rreach-b');
  page.appendChild(floorReach);
  const floor = el('div', 'rfloor');
  const prev = el('button', 'rbtn', '\u2039  Previous Chapter');
  const next = el('button', 'rbtn', 'Next Chapter  \u203A');
  const jump = el('select', 'rjump');
  jump.title = 'Jump to a chapter';
  floor.appendChild(prev);
  floor.appendChild(jump);
  floor.appendChild(next);
  page.appendChild(floor);

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

  /* reader.MEDIUM_TARGET_WIDTH, kept for one job only: the size of the
     box a page occupies *before* it has arrived, so nothing below jumps
     as the chapter fills in. What a page is actually drawn at is its
     own width - see sizePage. In CSS px, capped to the column by the
     same `min(..., 100%)` the loaded page is held to, and no longer
     divided by devicePixelRatio - sizePage stopped dividing on 2
     September 2026 (below), and a box a fifth narrower than the page
     that replaces it is exactly the jump this estimate exists to stop. */
  const WIDTHS = { manga: 1100, manhwa: 762, manhua: 762 };
  strip.style.setProperty('--pagew', (WIDTHS[data.medium] || 1100) + 'px');
  // The strip's own gutter, .reader's horizontal padding in app.css -
  // 30px a side, the site's own `.container{padding:0 30px}`.
  const STRIP_PAD = 30;
  function availableWidth() {
    // clientWidth counts the padding; the column a page may fill is what
    // is left inside it. Read live rather than once: the scrollbar
    // arrives after the first pages do and takes 12px off the strip.
    return Math.max(1, strip.clientWidth - 2 * STRIP_PAD);
  }
  // reader.STRIP_ASPECT_MIN - a page shaped like a strip is one whatever
  // the entry's type says, which is what decides the gap below.
  const STRIP_ASPECT = 3.0;
  // **Only manga.** The owner, 1 September 2026: "ONLY in manga add a
  // tiny space between pages". A manga page is a bordered scan and the
  // join between two of them is invisible without it; a manhwa or
  // manhua strip is one continuous image cut into pieces, and a gap
  // there would draw a line through the artwork.
  if (data.medium === 'manga') strip.classList.add('paged');
  readerState.key = data.key || '';
  readerState.total = data.total || 0;

  /* **Every page at its own width in CSS px, capped to the column, times
     the zoom - which is what the site draws.** The owner, three times
     over, the last on 2 September 2026: "the 3asq mangas still do not
     show the real size and quality of pages" and "make sure to show it
     as is in the browser website".

     The first pass here (same day) fixed a target width derived once
     per chapter from whichever page reported first - Kingdom (WAN)
     opens on a 2560 spread and drew every 828 page at ~1550, One Piece
     ch.1191 mixes 822, 1644 and 3288 in one chapter - and it fixed that
     by drawing each page at naturalWidth / devicePixelRatio: 1:1 device
     pixels, never upscaled. That was the wrong target. Measured against
     the site on Kingdom ch.886 (3asq.online/manga/kingdom-2/886/):

       what the app fetches is byte-identical to what the site shows
       (00.jpg 2760x1917 2.37MB, 01.jpg 1325x1920 1.94MB, 02.jpg
       1325x1920 2.38MB, served through /img/<token> unscaled) - so
       quality was never lost in transit; only the DRAWN size differed.

       the site's CSS: `.container{max-width:100%;padding:0 30px}`,
       `.main-col{width:100%}` (the sidebar is hidden on a reading page)
       and Madara's `.reading-content img{max-width:100%;height:auto}`,
       so it draws every page at min(naturalWidth, viewport - 60) CSS
       px, centred, and lets the browser upscale at DPR > 1. On his
       2560px panel at DPR 1.25 (CSS viewport ~2048) that is 01.jpg at
       1325 CSS px = 1656 device px, and the 2760 spread at ~1988.

       the app drew 01.jpg at 1325 / 1.25 = 1060 CSS px = 1325 device
       px: sharper per pixel and 20% smaller on screen, and the 20% is
       what he has been reporting as "zoomed out" and "not the real
       size".

     So the rule is now the site's rule, and the browser upscales at
     DPR > 1 exactly as the site does. It is also every other reading
     site's rule - Madara/WP-manga themes and the MangaDex-style readers
     all draw `max-width:100%; height:auto` in a padded column - so no
     site needs a case of its own here. Zoom multiplies the site's size
     (100% is the site's size), and the result is still held to the
     column: a page wider than the strip would be centred by the flex
     row and clipped on both sides by #page's overflow-x, which is
     unreadable rather than bigger. */
  function pageWidth(naturalWidth, available, zoom) {
    const base = Math.min(naturalWidth, available);
    return Math.min(Math.round(base * zoom), available);
  }
  function sizePage(img) {
    if (!img.naturalWidth) return;
    img.style.width =
      pageWidth(img.naturalWidth, availableWidth(), readerState.zoom) + 'px';
  }

  function applyZoom() {
    zoomLabel.textContent = Math.round(readerState.zoom * 100) + '%';
    strip.querySelectorAll('img.rpage').forEach(sizePage);
  }
  // Re-sized when the column changes width - the window, full screen,
  // the sidebar fold, and the scrollbar arriving once the chapter is
  // taller than the window. A ResizeObserver on the strip sees all four;
  // a window 'resize' listener sees only the first two.
  if (readerResize) readerResize.disconnect();
  readerResize = new ResizeObserver(function () {
    if (strip.isConnected) applyZoom();
  });
  readerResize.observe(strip);

  function changeZoom(step) {
    readerState.zoom = Math.min(4, Math.max(0.25, readerState.zoom + step));
    applyZoom();
  }
  readerState.zoom = readerState.zoom || 1;
  applyZoom();
  zoomIn.addEventListener('click', function () { changeZoom(0.15); });
  zoomOut.addEventListener('click', function () { changeZoom(-0.15); });
  zoomLabel.addEventListener('click', function () {
    readerState.zoom = 1; applyZoom();
  });

  // The chapter's *own* number beside the door, and the series title
  // centred - reader keeps those two apart on purpose, the index in a
  // 507-chapter listing saying nothing the number does not.
  // **The name at the door, the chapter in the middle.** The owner, 1
  // September 2026: "in the upper bar in the reading write the reading
  // name in the place of the ch num up left, and make the ch num in the
  // mid top bar instead of the reading name".
  num.textContent = data.title || '';
  title.textContent = data.label || '';
  label.textContent = (data.count || 0) + ' pages';

  // **The list runs newest first**, so the next chapter is a *lower*
  // index and the previous one a higher. Measured on the owner's
  // Kingdom: index 0 is chapter 886, index 380 is chapter 1. Wired the
  // other way round, Next Chapter walked backwards through the series.
  next.disabled = index <= 0;
  prev.disabled = index >= (data.total || 1) - 1;
  prev.addEventListener('click', function () { openChapter(id, index + 1); });
  next.addEventListener('click', function () { openChapter(id, index - 1); });
  listBtn.addEventListener('click', function () { showChapters(id); });
  refresh.addEventListener('click', function () { openChapter(id, index); });
  browser.addEventListener('click', function () {
    tellHost({ action: 'browser', id: id, i: index });
  });
  download.addEventListener('click', function () {
    tellHost({ action: 'download', id: id, i: index });
  });

  // The jump list on the floor: changes chapter without leaving the
  // page, which is what _ChapterCombo is and why the top bar's button
  // back to the list is a separate control.
  fetch('/api/chapters?id=' + encodeURIComponent(id))
    .then(function (r) { return r.json(); })
    .then(function (all) {
      const rows = all.items || all.rows || [];
      if (!rows.length) { jump.style.display = 'none'; return; }
      rows.forEach(function (row, i) {
        const opt = el('option', null, row.label || row.title || ('#' + (i + 1)));
        opt.value = String(i);
        if (i === index) opt.selected = true;
        jump.appendChild(opt);
      });
      jump.addEventListener('change', function () {
        openChapter(id, parseInt(jump.value, 10) || 0);
      });
    })
    .catch(function () { jump.style.display = 'none'; });

  // **The Reading block of global_search.SHORTCUTS.** Up/Down, Space,
  // PageUp/PageDown, Home and End are the browser's own and are left
  // alone; these are the rest. Bound to the document because the page is
  // what has focus - the window never sees them (see
  // webview2_host._accelerator).
  if (readerKeys) removeEventListener('keydown', readerKeys);
  readerKeys = function (e) {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const k = e.key;
    if (k === 'ArrowRight') { if (!next.disabled) openChapter(id, index - 1); }
    else if (k === 'ArrowLeft') { if (!prev.disabled) openChapter(id, index + 1); }
    else if (k === '+' || k === '=') changeZoom(0.15);
    else if (k === '-' || k === '_') changeZoom(-0.15);
    else if (k === '0') { readerState.zoom = 1; applyZoom(); }
    else if (k === 'r' || k === 'R') openChapter(id, index);
    else if (k === 'f' || k === 'F') tellHost({ action: 'key', key: 'F11' });
    else return;
    e.preventDefault();
  };
  addEventListener('keydown', readerKeys);

  let measured = false;
  function learn(img) {
    if (!img.naturalWidth || !img.naturalHeight) return;
    // This page's own shape, always. A double-page spread is twice as
    // wide as a single one, and forcing it into the shared ratio is
    // what squashed it instead of fitting it to the width.
    img.style.aspectRatio = img.naturalWidth + ' / ' + img.naturalHeight;
    // **Never wider than the scan itself, in CSS px.** The strip used
    // to lay every page out at the medium's target width and a narrower
    // scan was stretched up to meet it - the owner's "manga/manhua/
    // manhwa images are blurry". The site's own cap, and the reasoning
    // for it, is on sizePage.
    sizePage(img);
    // **One strip-shaped page anywhere closes the gap for the whole
    // chapter** - reader._on_page_shape, and its asymmetry: being wrong
    // towards 0 costs two printed pages sitting flush, being wrong
    // towards 6 draws a line through artwork cut mid-panel.
    if (img.naturalHeight / img.naturalWidth >= STRIP_ASPECT) {
      strip.classList.remove('paged');
    }
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
    img.alt = '';
    img.addEventListener('load', function () { learn(img); });
    // A chapter is 20-200 files, so only what is near the viewport is
    // fetched - through lazyArt rather than loading="lazy", which does
    // not work on this page at all (the measurement is above it). That
    // silently left every page past the third unrequested, which is one
    // half of the owner's "some manga images do not load"; the retry
    // below was the other, and could never have covered this one -
    // nothing was asked for, so nothing could fail.
    lazyArt(img, src, 0);
    // **One retry before a page is given up on.** These come through
    // the app's own proxy from hosts that rate-limit, and a single
    // refused fetch left a hole in the middle of a chapter - the owner's
    // "some manga images do not load". The global error handler hides an
    // image that fails; this gets in first, once, with a cache-buster so
    // the retry is not served the same refusal from the memory cache.
    // **The window's capture-phase handler has already run by now.**
    // `error` reaches the window's capture listener (top of this file)
    // before this target-phase one, and that listener hides the image
    // and strips its src - so reading the attribute here answered null
    // and the retry never fired (review, 3 September 2026: the hole in
    // the chapter this was written to close was still there). The URL
    // comes from the closure instead; the retry itself removes this
    // listener first, so a second refusal is final.
    img.addEventListener('error', function retry() {
      img.removeEventListener('error', retry);
      setTimeout(function () {
        img.style.display = '';
        img.src = src + (src.indexOf('?') >= 0 ? '&' : '?') + 'retry=1';
      }, 400);
    });
    // `complete` is true for an <img> that has no src at all, and
    // learn() would then set an aspect ratio of "0 / 0" on it.
    if (img.getAttribute('src') && img.complete) learn(img);
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
  // The chapter keys (arrows, +/-, R) belong to a chapter, not to this
  // list - left bound, ArrowRight opened the next chapter from here.
  if (readerKeys) { removeEventListener('keydown', readerKeys); readerKeys = null; }
  page.scrollTop = 0;
  page.innerHTML = '';
  const head = el('header');
  // **Back to where he came from.** The owner, 1 September 2026: a back
  // button on this page "takes me to the last page/ch I was on before
  // entering it". Only shown when there is one - reaching the list from
  // a card rather than from inside a chapter has nothing to go back to.
  if (readerState.id === id && readerState.total) {
    const back = el('button', 'plainbtn small', '‹  Back to reading');
    back.addEventListener('click', function () {
      openChapter(id, readerState.index);
    });
    head.appendChild(back);
  }
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

      // **Marking read, which the web list simply had no way to do.**
      // The Qt list carries it twice - a tick on the row and a
      // right-click menu (reader._ChapterCombo.markRequested) - so both
      // are here: the glyph is reader.ICON_READ, the same CheckMark.
      const tick = el('button', 'eptick', '');
      tick.title = 'Mark as read';
      function paint() {
        const done = read.has(item.key);
        line.classList.toggle('done', done);
        tick.classList.toggle('on', done);
        tick.title = done ? 'Mark as unread' : 'Mark as read';
      }
      function toggle(event) {
        if (event) { event.preventDefault(); event.stopPropagation(); }
        if (!item.key) return;
        const want = !read.has(item.key);
        // Painted first: the write is a file the app owns, it does not
        // fail in practice, and a tick that waits on a round trip reads
        // as a click that did nothing.
        if (want) read.add(item.key); else read.delete(item.key);
        paint();
        fetch('/api/mark?id=' + encodeURIComponent(id) +
              '&key=' + encodeURIComponent(item.key) +
              '&read=' + (want ? '1' : '0'))
          .then(function (r) { return r.json(); })
          .then(function (out) {
            if (out && out.ok) return;
            if (want) read.delete(item.key); else read.add(item.key);
            paint();                       // it did not take - say so
          })
          .catch(function () {
            if (want) read.delete(item.key); else read.add(item.key);
            paint();
          });
      }
      tick.addEventListener('click', toggle);
      line.addEventListener('contextmenu', toggle);
      paint();
      line.appendChild(tick);

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
  // currentRoute is a *function* (see the bottom of this file). Compared
  // as a value it never equals a string, so this reset ran on every
  // go() - the shelf sort and Select were undone before shelfInto drew
  // them - and the Downloads poll and the Movies wheel below never ran
  // at all. Found by review, 3 September 2026.
  if (route !== currentRoute() && ['games', 'apps', 'websites'].indexOf(route) >= 0) {
    // A shelf remembers its sort and its picks only while it is the page
    // being looked at - the Qt pages rebuild from scratch on every visit
    // and keep no state either (.claude/rules/ui.md).
    shelfState = { sort: 'Custom Order', selecting: false, picked: new Set() };
  }
  const mine = ++token;
  // Every picture the last render was waiting on died with it.
  resetLazy();
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
  if (bits[0] === 'search') {
    // Drawn before the answer arrives: four site searches take seconds
    // and an empty window for that long reads as nothing happening.
    const term = decodeURIComponent(bits.slice(1).join('/') || '');
    page.innerHTML = '';
    const head = el('header');
    head.appendChild(el('h1', null, 'Search'));
    head.appendChild(el('p', null, 'searching for “' + term + '”…'));
    page.appendChild(head);
    try {
      const found = await (await fetch('/api/search?q=' +
        encodeURIComponent(term))).json();
      if (mine !== token) return;
      page.innerHTML = '';
      const done = el('header');
      done.appendChild(el('h1', null, 'Search'));
      done.appendChild(el('p', null, found.note || ''));
      page.appendChild(done);
      sectionsInto(page, found.sections || []);
    } catch (err) {
      page.appendChild(el('div', 'empty', 'the search could not finish'));
    }
    return;
  }
  page.scrollTop = 0;
  page.innerHTML = '';

  let data;
  try {
    const sided = ['saved', 'history', 'schedule'].indexOf(route) >= 0;
    data = await (await fetch('/api/' + route +
                              (sided ? '?tab=' + sideTab : ''))).json();
  } catch (err) {
    page.appendChild(el('div', 'empty', 'could not load'));
    return;
  }
  if (mine !== token) return;              // a later click won

  const heroes = data.heroes || (data.hero ? [data.hero] : []);
  if (heroes.length) page.appendChild(heroCarousel(heroes));
  // The shelves and the queue draw their own title, in their own
  // header with the buttons opposite it - this generic one would print
  // the word a second time above it (it did, in the first screenshot).
  const ownsHeader = data.kind === 'shelf' || data.kind === 'downloads'
                     || !!data.tabs;
  if (!ownsHeader && (data.note || data.title || data.error)) {
    // The medium name and the note under it - the two lines the Qt page
    // opens with, where the name is the panel heading its own chrome
    // drew and the note is _category_note. Both small and dim: when the
    // name was set large the two "sat stacked" and the owner sent a
    // screenshot of it (tracker's own note, 22 August 2026).
    const head = el('header', data.kind === 'grid' ? 'gridhead' : null);
    if (data.title) head.appendChild(el('p', 'ptitle', data.title));
    if (data.note) head.appendChild(el('p', null, data.note));
    // A route that raised answers {error} now (server.do_GET) - said
    // here rather than drawn as an empty page.
    if (data.error) head.appendChild(el('p', 'empty', data.error));
    page.appendChild(head);
  }
  if (downloadsTimer) { clearInterval(downloadsTimer); downloadsTimer = null; }
  // The last grid's load-more listener goes with the grid.
  if (page._morePull) { page.removeEventListener('scroll', page._morePull); page._morePull = null; }

  if (data.tabs) tabsInto(page, data, route);
  if (data.kind === 'history') {
    historyInto(page, data);
    return;
  }
  if (data.kind === 'schedule') {
    scheduleInto(page, data);
    return;
  }
  if (data.kind === 'shelf') {
    shelfInto(page, data);
    return;
  }
  if (data.kind === 'downloads') {
    downloadsInto(page, data);
    // Every second, like DownloadsPage's own POLL_MS - and cancelled the
    // moment any other route is drawn, three lines above.
    downloadsTimer = setInterval(function () {
      if (currentRoute() === 'downloads' && mine === token) go('downloads');
    }, 1000);
    return;
  }
  if (data.kind === 'grid') {
    const grid = el('div', 'grid');
    (data.rows || []).forEach(function (row) { grid.appendChild(gridCard(row)); });
    page.appendChild(grid);
    if (!(data.rows || []).length) {
      page.appendChild(el('div', 'empty', 'Looking around...'));
    }
    liveBrowse(data, page, grid, token);
    moreOnScroll(data, page, grid, token);
    return;
  }

  page.dataset.cardstyle = data.cardstyle || '';
  sectionsInto(page, data.sections || []);
  if (!(data.sections || []).some(function (s) { return s.rows.length; })) {
    page.appendChild(el('div', 'empty', 'Nothing here yet.'));
  }
}



/* ---- an eased wheel notch, on Movies only -------------------------
   The owner, 1 September 2026: "make the mouse wheel tick travels the
   same distance as now but not on a jump, do this only on movies page so
   that I test it."

   So the distance is untouched - whatever Chromium was about to apply is
   what this applies - and only the delivery changes: instead of landing
   in one frame it is eased over a few. Retargeting rather than
   restarting, which is what widgets._SmoothWheel did on the Qt side: a
   second notch mid-glide moves the destination and the curve re-aims
   from wherever the view is now, so spinning the wheel reads as momentum
   rather than a queue of little animations.

   A touchpad is left alone. Its deltas are already one-to-one with the
   finger and are the smoothest thing in the app - that is the whole
   reason the wheel stood out. A notch arrives as one large delta; a
   finger as a stream of small ones, so the size is what tells them
   apart. */
const NOTCH_MIN_PX = 50;      // below this it is a finger, not a notch
const GLIDE_MS = 130;

let glideTo = null;
let glideFrom = 0;
let glideAt = 0;

function glideStep(now) {
  if (glideTo === null) return;
  const t = Math.min(1, (now - glideAt) / GLIDE_MS);
  // Chromium's own ease-out, the curve its scroll animation uses.
  const eased = 1 - Math.pow(1 - t, 3);
  page.scrollTop = glideFrom + (glideTo - glideFrom) * eased;
  if (t < 1) {
    requestAnimationFrame(glideStep);
  } else {
    glideTo = null;
  }
}

addEventListener('wheel', function (e) {
  if (currentRoute() !== 'movies') return;
  if (e.ctrlKey || e.shiftKey || e.deltaMode !== 0) return;
  if (Math.abs(e.deltaY) < NOTCH_MIN_PX) return;      // a finger
  e.preventDefault();
  const limit = page.scrollHeight - page.clientHeight;
  const from = glideTo === null ? page.scrollTop : glideTo;
  glideTo = Math.max(0, Math.min(limit, from + e.deltaY));
  glideFrom = page.scrollTop;
  glideAt = performance.now();
  requestAnimationFrame(glideStep);
}, { passive: false });


/* ---- Saved / History / Schedule -----------------------------------
   The Qt tabs' own shape: a Watch/Read pair of pills, then either cards
   that carry the status and the number under the title, or - on
   Schedule - a list of rows with the slot and the countdown at the
   right. Photographed from the Qt build before this was written. */
let sideTab = 'watch';

function tabsInto(parent, data, route) {
  const head = el('div', 'tabrow');
  head.appendChild(el('span', 'ptitle', data.title || ''));
  (data.tabs || []).forEach(function (tab) {
    const pill = el('button', 'pill2' + (tab.key === data.tab ? ' on' : ''),
                    tab.label);
    pill.addEventListener('click', function () {
      sideTab = tab.key;
      go(route);
    });
    head.appendChild(pill);
  });
  parent.appendChild(head);
  if (data.note) {
    const note = el('p', 'tabnote', data.note);
    parent.appendChild(note);
  }
}

function statusCard(row) {
  const card = cardFor(row);
  // The status under the title and the number under that, in accent -
  // the only colour on a tracker card and the thing being looked for.
  const meta = card.querySelector('.m');
  if (meta) meta.textContent = row.status || '';
  if (row.progress) card.appendChild(el('div', 'cnum', row.progress));
  return card;
}

/* ---- History -------------------------------------------------------
   tracker._build_history, row for row: the count and Clear History
   opposite each other, then a list - 46x62 cover, the title with its
   progress under it, and on the right when it was opened over an
   "In Saved" / "Not saved" tag.

   The owner, 2 September 2026: "the history is not showing like it was
   in the Qt, make it the same design with keeping the WebView2". What
   was here drew the sideways strip of poster cards every other section
   uses, which lost the three things this page is *for* - when, whether
   it is saved, and how far in he got. */
function historyInto(parent, data) {
  const rows = data.rows || [];
  if (!rows.length) return;      // tabsInto has already printed the note
  // The count is the note tabsInto drew a line above; only the button
  // belongs here, or the page says "28 titles" twice (it did).
  const head = el('div', 'histhead');
  const clear = el('button', 'histclear', 'Clear History');
  clear.addEventListener('click', function () {
    tellHost({ action: 'history', do: 'clear', tab: data.tab || 'watch' });
  });
  head.appendChild(clear);
  parent.appendChild(head);

  const list = el('div', 'histlist');
  rows.forEach(function (row) {
    const line = el('div', 'histrow');
    const art = el('img', 'histart');
    art.alt = '';
    lazyArt(art, row.cover, 60);
    line.appendChild(art);

    const body = el('div', 'histbody');
    body.appendChild(el('div', 'histname', row.title || ''));
    if (row.meta) body.appendChild(el('div', 'histmeta', row.meta));
    line.appendChild(body);

    const right = el('div', 'histwhen');
    right.appendChild(el('div', null, row.when || ''));
    right.appendChild(el('div', 'histtag' + (row.saved ? ' in' : ''),
                         row.saved ? 'In Saved' : 'Not saved'));
    line.appendChild(right);

    line.addEventListener('click', function () {
      tellHost({ action: 'open', kind: 'title', id: row.id || '',
                 title: row.title || '', type: row.type || '',
                 url: row.url || '', imdb: row.imdb || '' });
    });
    list.appendChild(line);
  });
  parent.appendChild(list);
}

function scheduleInto(parent, data) {
  (data.blocks || []).forEach(function (block) {
    const wrap = el('div', 'schedblock');
    wrap.appendChild(el('h2', null, block.title));
    if (block.note) wrap.appendChild(el('p', 'tabnote', block.note));
    let day = null;
    block.rows.forEach(function (row) {
      // A Read row has landed rather than being due, so it carries no
      // day at all - tracker's own note: nobody announces a scanlation.
      if (row.day && row.day !== day) {
        day = row.day;
        wrap.appendChild(el('div', 'schedday', day));
      }
      const line = el('div', 'schedrow');
      const art = el('img', 'schedart');
      art.alt = '';
      lazyArt(art, row.cover, 56);
      line.appendChild(art);
      const body = el('div', 'schedbody');
      const top = el('div', 'schedname');
      top.appendChild(el('span', null, row.title || ''));
      if (row.saved) top.appendChild(el('span', 'savedtag', 'Saved'));
      body.appendChild(top);
      if (row.progress) body.appendChild(el('div', 'schednum', row.progress));
      line.appendChild(body);
      if (row.slot || row.countdown) {
        const when = el('div', 'schedwhen');
        when.appendChild(el('div', null, row.slot || ''));
        when.appendChild(el('div', 'schedleft', row.countdown || ''));
        line.appendChild(when);
      }
      line.addEventListener('click', function () {
        tellHost({ action: 'open', kind: 'title', id: row.id || '',
                   title: row.title || '', type: row.type || '',
                   url: row.url || '' });
      });
      wrap.appendChild(line);
    });
    parent.appendChild(wrap);
  });
  if (!(data.blocks || []).length) {
    parent.appendChild(el('div', 'empty', 'Nothing scheduled.'));
  }
}

/* ---- the shelves: Games, Apps, Websites ---------------------------
   windows/games.py's layout, in order: the title with the two 40x40
   buttons opposite it, the Sort row with Select opposite it, the
   selection bar (hidden until the mode is on), then the centred grid. */
let shelfState = { sort: 'Custom Order', selecting: false, picked: new Set() };

function shelfCard(row, shelf) {
  const card = el('div', 'sc' + (row.shape === 'square' ? ' square' : ''));
  card.title = row.missing_paths
    ? 'No longer on disk:\n' + row.missing_paths.join('\n')
    : (row.name || '');
  const art = el('img', 'sart');
  art.alt = '';
  lazyArt(art, row.cover, 160);
  // Dimmed rather than swapped for a warning glyph: the icon is how a
  // card is picked out of a grid at a glance, and losing it would make
  // the card harder to read, not clearer (link_grid's own note). The
  // badge below is what states the verdict.
  if (row.missing) art.classList.add('gone');
  card.appendChild(art);
  card.appendChild(el('div', 'sname', row.name || ''));
  if (row.missing) card.appendChild(el('div', 'smissing', row.missing));

  if (shelfState.selecting) {
    const mark = el('div', 'spick' + (shelfState.picked.has(row.id) ? ' on' : ''));
    card.appendChild(mark);
    card.addEventListener('click', function () {
      if (shelfState.picked.has(row.id)) shelfState.picked.delete(row.id);
      else shelfState.picked.add(row.id);
      go(shelf);                       // redraw with the new marks
    });
  } else {
    card.addEventListener('click', function () {
      tellHost({ action: 'open', kind: row.kind, id: row.id,
                 title: row.name || '' });
    });
    card.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      // Qt owns the menu: Edit opens a dialog and Delete writes a file,
      // and both belong on the side that has storage's rules.
      tellHost({ action: 'menu', shelf: shelf, id: row.id,
                 title: row.name || '',
                 x: Math.round(e.clientX), y: Math.round(e.clientY) });
    });
    // Drag to reorder, which is what "Custom Order" means. Only in that
    // mode - games._begin_custom_order writes the order that is on
    // screen the moment a drag starts in any other, and doing that from
    // a filtered or re-sorted grid would write the wrong one.
    if (shelfState.sort === 'Custom Order') {
      card.draggable = true;
      card.addEventListener('dragstart', function (e) {
        e.dataTransfer.setData('text/plain', row.id);
        card.classList.add('dragging');
      });
      card.addEventListener('dragend', function () {
        card.classList.remove('dragging');
      });
      card.addEventListener('dragover', function (e) { e.preventDefault(); });
      card.addEventListener('drop', function (e) {
        e.preventDefault();
        const moved = e.dataTransfer.getData('text/plain');
        if (!moved || moved === row.id) return;
        tellHost({ action: 'reorder', shelf: shelf,
                   moved: moved, target: row.id });
      });
    }
  }
  return card;
}

function sortShelf(rows) {
  const by = shelfState.sort;
  const copy = rows.slice();
  if (by === 'Name (A-Z)') {
    copy.sort(function (a, b) {
      return (a.name || '').toLowerCase() < (b.name || '').toLowerCase() ? -1 : 1;
    });
  } else if (by === 'Date Added (Newest)') {
    copy.sort(function (a, b) { return (b.added_at || '') < (a.added_at || '') ? -1 : 1; });
  } else if (by === 'Last Played' || by === 'Last Used') {
    copy.sort(function (a, b) { return (b.used_at || '') < (a.used_at || '') ? -1 : 1; });
  }
  return copy;                          // Custom Order is the file's own
}

function shelfInto(parent, data) {
  const shelf = data.shelf;
  const head = el('div', 'shelfhead');
  head.appendChild(el('h1', 'paneltitle', data.title || ''));
  const acts = el('div', 'shelfacts');
  if (shelf === 'games') {
    const scan = el('button', 'roundbtn', '\ue72c');       // Refresh
    scan.title = 'Import from Launchers';
    scan.addEventListener('click', function () {
      tellHost({ action: 'import', shelf: shelf });
    });
    acts.appendChild(scan);
  }
  const add = el('button', 'roundbtn accent', '+');
  add.title = 'Add ' + (data.noun ? data.noun[0] : 'item');
  add.addEventListener('click', function () {
    tellHost({ action: 'add', shelf: shelf });
  });
  acts.appendChild(add);
  head.appendChild(acts);
  parent.appendChild(head);

  const sortRow = el('div', 'sortrow');
  sortRow.appendChild(el('span', 'sortlabel', 'Sort:'));
  const box = el('select', 'sortbox');
  (data.sorts || []).forEach(function (name) {
    const opt = el('option', null, name);
    if (name === shelfState.sort) opt.selected = true;
    box.appendChild(opt);
  });
  box.addEventListener('change', function () {
    shelfState.sort = box.value;
    go(shelf);
  });
  sortRow.appendChild(box);
  const pick = el('button', 'selbtn' + (shelfState.selecting ? ' on' : ''),
                  shelfState.selecting ? 'Done' : 'Select');
  pick.addEventListener('click', function () {
    shelfState.selecting = !shelfState.selecting;
    shelfState.picked.clear();
    go(shelf);
  });
  sortRow.appendChild(pick);
  parent.appendChild(sortRow);

  const rows = sortShelf(data.rows || []);
  if (shelfState.selecting) {
    const bar = el('div', 'selbar');
    const noun = data.noun || ['item', 'items'];
    const n = shelfState.picked.size;
    bar.appendChild(el('span', 'selcount',
                       n + ' ' + (n === 1 ? noun[0] : noun[1])));
    const all = el('label', 'selall');
    const tick = el('input');
    tick.type = 'checkbox';
    tick.checked = n > 0 && n === rows.length;
    tick.addEventListener('change', function () {
      shelfState.picked = tick.checked
        ? new Set(rows.map(function (r) { return r.id; })) : new Set();
      go(shelf);
    });
    all.appendChild(tick);
    all.appendChild(el('span', null, 'Select All'));
    bar.appendChild(all);
    const bin = el('button', 'binbtn', '\ue74d');          // Delete
    bin.title = 'Delete the picked ' + noun[1];
    bin.disabled = n === 0;
    bin.addEventListener('click', function () {
      tellHost({ action: 'delete', shelf: shelf,
                 ids: Array.from(shelfState.picked) });
    });
    bar.appendChild(bin);
    parent.appendChild(bar);
  }

  const panel = el('div', 'shelfpanel');
  const grid = el('div', 'shelfgrid');
  rows.forEach(function (row) { grid.appendChild(shelfCard(row, shelf)); });
  if (!rows.length) grid.appendChild(el('div', 'empty', 'Nothing here yet.'));
  panel.appendChild(grid);
  parent.appendChild(panel);
}

/* ---- Downloads ----------------------------------------------------
   windows/downloads_page.py: the queue newest first, "Clear Finished"
   opposite the title, the folder row under it, and a row per job with
   its state, a bar while it is running, and the buttons that state
   allows. Asked for again every second, which is what that page's own
   QTimer does - a download has no event to listen to. */
let downloadsTimer = null;

function downloadsInto(parent, data) {
  const head = el('div', 'shelfhead');
  head.appendChild(el('h1', 'paneltitle', data.title || 'Downloads'));
  const clear = el('button', 'plainbtn', 'Clear Finished');
  clear.title = 'Remove everything that has finished, failed or been cancelled';
  clear.addEventListener('click', function () {
    tellHost({ action: 'dl', do: 'clear' });
    setTimeout(function () { go('downloads'); }, 150);
  });
  head.appendChild(clear);
  parent.appendChild(head);

  const folder = el('div', 'dlfolder');
  folder.appendChild(el('span', 'sortlabel', 'Save to:'));
  folder.appendChild(el('span', 'dlpath', data.folder || ''));
  const pickFolder = el('button', 'plainbtn small', 'Change');
  pickFolder.addEventListener('click', function () {
    tellHost({ action: 'dl', do: 'folder' });
  });
  folder.appendChild(pickFolder);
  parent.appendChild(folder);

  const panel = el('div', 'shelfpanel');
  const list = el('div', 'dllist');
  (data.rows || []).forEach(function (job) {
    const row = el('div', 'dlrow');
    const text = el('div', 'dlmain');
    text.appendChild(el('div', 'dltitle', job.title || ''));
    text.appendChild(el('div', 'dldetail',
                        job.state_text + (job.detail ? '  \u00b7  ' + job.detail : '')));
    if (job.active) {
      const track = el('div', 'dlbar');
      const fill = el('div', 'dlfill');
      fill.style.width = Math.max(0, Math.min(100, job.percent)) + '%';
      track.appendChild(fill);
      text.appendChild(track);
    }
    row.appendChild(text);

    const acts = el('div', 'dlacts');
    if (job.can_pause) acts.appendChild(dlButton('Pause', job.id, 'pause'));
    if (job.can_resume) acts.appendChild(dlButton('Resume', job.id, 'resume'));
    if (job.active) acts.appendChild(dlButton('Cancel', job.id, 'cancel'));
    row.appendChild(acts);
    list.appendChild(row);
  });
  if (!(data.rows || []).length) {
    list.appendChild(el('div', 'empty', 'Nothing downloading.'));
  }
  panel.appendChild(list);
  parent.appendChild(panel);
}

function dlButton(label, id, what) {
  const button = el('button', 'plainbtn small', label);
  button.addEventListener('click', function () {
    tellHost({ action: 'dl', do: what, id: id });
    setTimeout(function () { go('downloads'); }, 150);
  });
  return button;
}

/* ---- the catalogue grid -------------------------------------------
   One card, shaped exactly as helpers/web_grid.py builds it: cover,
   title, meta, and the meta in ACCENT when the title is already his. */
function gridCard(row) {
  const card = el('div', 'gc');
  const img = el('img', 'p');
  img.width = 160; img.height = 216;
  img.alt = '';
  // Decoded off the thread that scrolls - web_grid's note: a cover
  // arriving mid-scroll can otherwise decode inline and cost the frame
  // it lands on. Deferred through lazyArt for the reason written there.
  lazyArt(img, row.cover, 160);
  card.appendChild(img);
  card.appendChild(el('div', 't', row.title || ''));
  card.appendChild(el('div', 'm' + (row.saved ? ' s' : ''), row.meta || ''));
  card.addEventListener('click', function () {
    tellHost({ action: 'open', kind: row.kind || 'title', id: row.id || '',
               title: row.title || '', type: row.type || '',
               url: row.url || '', poster: row.cover || '',
               imdb: row.imdb || '' });
  });
  return card;
}


/* **Load more, as the Qt page does it.** The owner, 22 August 2026:
   "make it always load more when the user scrolls down" - and these
   pages never had it, so a medium showed whatever the cache held and
   stopped. tracker._maybe_load_more_category is the same idea on the
   other side of the same server call.

   One batch at a time, and stopped for good once two batches in a row
   bring nothing new: the video catalogues page for hundreds of rows but
   the reading sweeps genuinely run out, and asking a dry source on every
   wheel notch would be a request per scroll for ever. Two rather than
   one because a single Cinemeta timeout answers [] and that is not the
   same as being finished (tracker's own note). */
const MORE_MARGIN_PX = 900;

// `stamp`, not `token`: the parameter used to be called `token` and so
// shadowed the module-level counter of that name, while the body tested
// `mine !== token` - and `mine` is a const local to go(). That is a
// ReferenceError on the first line of pull(), thrown before a single
// batch was ever asked for, which is why these pages loaded nothing more
// however far they were scrolled.
function moreOnScroll(data, host, grid, stamp) {
  if (!data.browse) return;
  const seen = new Set((data.rows || []).map(function (r) {
    return (r.title || '').trim().toLowerCase();
  }));
  // The source's own cursor when the server states one: a video genre
  // page is two Cinemeta catalogs (series and movies) paged from one
  // skip, so its first page of 100 rows sits at cursor 50, not 100 -
  // taking the row count there skipped the second fifty of each.
  let skip = (typeof data.skip === 'number') ? data.skip
                                             : (data.rows || []).length;
  let busy = false;
  let dry = 0;

  function pull() {
    if (busy || dry >= 2 || stamp !== token) return;
    if (host.scrollHeight - host.scrollTop - host.clientHeight > MORE_MARGIN_PX) return;
    busy = true;
    fetch('/api/more?medium=' + encodeURIComponent(data.browse) +
          '&have=' + seen.size + '&skip=' + skip)
      .then(function (r) { return r.json(); })
      .then(function (batch) {
        if (stamp !== token) return;
        skip = batch.skip || skip;
        const fresh = (batch.rows || []).filter(function (r) {
          const key = (r.title || '').trim().toLowerCase();
          if (!key || seen.has(key)) return false;
          seen.add(key);
          return true;
        });
        if (!fresh.length) { dry += 1; return; }
        dry = 0;
        const frag = document.createDocumentFragment();
        fresh.forEach(function (r) { frag.appendChild(gridCard(r)); });
        grid.appendChild(frag);
      })
      .catch(function () { dry += 1; })
      .then(function () {
        busy = false;
        // The batch may not have filled the viewport - ask again rather
        // than waiting for a scroll that will not come.
        setTimeout(pull, 60);
      });
  }

  // One listener per host, not one per visit: the previous page's pull
  // stays attached otherwise (its stamp check makes it a no-op, but a
  // session of catalogue visits stacked them up).
  if (host._morePull) host.removeEventListener('scroll', host._morePull);
  host._morePull = pull;
  host.addEventListener('scroll', pull, { passive: true });
  pull();
}

/* A medium's catalogue is a whole-sites sweep - measured 3.5s on a good
   day and 36.4s on a bad one - so the cache is drawn first and this
   fills in behind it, which is rule 7's "show what there is".

   **It may only append.** tracker._on_category_results pays for this
   lesson twice over: the sweep's row order depends on which sites
   answered this minute, so a refresh that redraws the grid swaps the
   titles under the cards the user is looking at, and one that replaces
   it wholesale cut a live grid from 60 cards to 30 and clamped the
   scroll position mid-scroll. Nothing already on screen moves; the grid
   only grows at the bottom. */
function liveBrowse(data, page, grid, stamp) {
  if (!data.browse) return;
  fetch('/api/browse?medium=' + encodeURIComponent(data.browse))
    .then(function (r) { return r.json(); })
    .then(function (live) {
      if (stamp !== token) return;              // a later click won
      const key = function (r) { return (r.title || '').trim().toLowerCase(); };
      const have = new Set((data.rows || []).map(key));
      const fresh = (live.rows || []).filter(function (r) {
        return r.title && !have.has(key(r));
      });
      if (!fresh.length) return;
      const frag = document.createDocumentFragment();
      fresh.forEach(function (r) { frag.appendChild(gridCard(r)); });
      grid.appendChild(frag);
      const note = page.querySelector('header p');
      if (note) note.textContent = data.note;
      const empty = page.querySelector('.empty');
      if (empty) empty.remove();
    })
    .catch(function () { /* the cached catalogue stays */ });
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

/* ---- the scrollbar -------------------------------------------------
   **The browser's own, deliberately.** This file used to draw one and
   ease the content toward the pointer, because his mouse reports at
   ~125Hz against 238Hz of frames and snapping straight to it stepped.
   Every version of that follower traded stepping for lag or lag for
   stepping, and the last one the owner described as "a bad delay in the
   dragging scrollbar method".

   Chromium has already solved this and drags with no interpolation at
   all. `::-webkit-scrollbar` in app.css gives it the app's colours, so
   nothing is lost but the code - and with it the pointer-pace estimate,
   the frame-length estimate and the thumb layout that had to be kept in
   step with the page. */

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
// **The hash is how the host changes pages.** WebTrackerPage points the
// view at #movies, #manhwa and so on, and a URL that differs only in its
// fragment is a *same-document* navigation - Chromium fires hashchange
// and never reloads, so without this the router runs once and the page
// stays on whatever it first drew. That is what left a converted
// watch/read page blank while the view itself reported loaded, visible
// and uncovered.
function currentRoute() {
  return (location.hash || '#home').slice(1) || 'home';
}

addEventListener('hashchange', function () { go(currentRoute()); });
go(currentRoute());
