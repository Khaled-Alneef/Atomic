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

/* **A press on a page leaves the search box.** The owner, 4 September
   2026: "the global search box, when I click anywhere outside it make
   it leave the search box".

   Qt's own answer to this is the search field losing focus, and it
   cannot see this press: a page is a WebView2 native child and Windows
   gives it the click directly, so the field keeps focus and the
   suggestion panel keeps standing. Photographed that day - "solo" still
   in the box and the panel over a details page the click had just
   opened.

   Capture phase and left button only, so it lands before whatever the
   press is actually for; the host does nothing at all unless a panel is
   open (web_pages._leave_search). */
addEventListener('pointerdown', function (e) {
  if (e.button === 0) tellHost({ action: 'pagepress' });
}, true);

addEventListener('keydown', function (e) {
  // Belt to webview2_host._accelerator's braces: if the accelerator
  // hook is unavailable, the page still hands these to the app.
  if (e.key === 'Escape' && window._histMenu) {
    // The open menu is what Escape means while one is up - the app's own
    // Escape (leave the page) would otherwise fire underneath it.
    closeHistMenu();
    e.preventDefault();
    return;
  }
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

/* The fold's own frames, once per fold. Reported from endFold as well
   as from the last step, because the window's `fold.done` usually
   arrives on the same tick the animation finishes and cancels the
   rAF - so the version that only reported from `step` reported one
   fold in thirty. `said` makes it once either way. */
function sayFold(st) {
  if (!st || st.said || !(st.frames || []).length) return;
  st.said = true;
  const f = st.frames.slice().sort(function (a, b) { return a - b; });
  tellHost({ action: 'diag', what: 'fold ran', route: currentRoute(),
             cards: st.cards, frames: f.length, median: f[f.length >> 1],
             worst: f[f.length - 1],
             over: f.filter(function (x) { return x > 20; }).length });
}

function endFold() {
  const st = foldState;
  foldState = null;
  if (!st) return;
  sayFold(st);
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
  /* **The shelves fold the same way the catalogues do.** The owner, 4
     September 2026: "in the apps and games and websites pages make the
     page and the cards transition while sidebar folding/unfolding same
     as movies page exactly!"

     This looked for `.grid` and `.gc` and nothing else, and a shelf
     draws `.shelfgrid` and `.sc` - so Games, Apps and Websites fell
     straight through the guard below, answered the window nothing, and
     were left to the plain per-frame resize this whole mechanism exists
     to replace (see the note at the top of the file: eleven layouts per
     fold, "they move in clear steps"). One selector pair short.

     Everything after this is shape-agnostic already: it reads
     rectangles, sets the grid's width once, and animates the difference
     on the compositor. */
  const grid = page.querySelector('.grid') || page.querySelector('.shelfgrid');
  const all = grid ? Array.prototype.slice.call(
    grid.querySelectorAll('.gc, .sc')) : [];
  // Nothing here to carry: no answer, and the window keeps its old way.
  if (!all.length) return;
  /* **Only the cards that can be seen, found without looking at the
     rest.** The owner, 3 September 2026: "the cards transition while
     fold/unfold the sidebar on Manhwa and Movies still not the same as
     series and anime".

     His own log said why, once the fold started reporting itself:

       fold series:  cards=31   moved=31  prep=0.8-1.1ms
       fold manga:   cards=91   moved=45  prep=1.1-1.7ms
       fold manhua:  cards=218  moved=45  prep=1.5-1.8ms
       fold manhwa:  cards=725  moved=45  prep=2.6-3.4ms
       fold movies:  cards=917  moved=45  prep=3.0-3.6ms

     The same 45 cards move on every one of them; what grows is the
     preparation, because this read a rectangle for **every** card in
     the grid, twice, and a `.gc` is `content-visibility: auto` so each
     read forces the layout of a subtree the browser had skipped. And
     the preparation is not free time: the window waits for this ack
     before it starts the rail (web_pages.offer_fold), so a deep page
     began its whole fold later than a shallow one. The frames
     themselves were never the problem - measured the same day, 179
     cards, 43 frames, median 4.2ms, none over budget.

     A grid is row-major and uniform, so `top` never decreases along the
     list: the first and last card of the visible band can be found by
     bisection. Ten rect reads for 917 cards instead of 917, and the
     count no longer appears in the cost at all. */
  const seenTop = -320;
  const seenBottom = innerHeight + 320;
  const topOf = function (i) { return all[i].getBoundingClientRect().top; };
  const firstFrom = function (limit) {
    // lowest index whose bottom is past `seenTop`
    let lo = 0, hi = all.length - 1, best = all.length;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const box = all[mid].getBoundingClientRect();
      if (box.bottom >= limit) { best = mid; hi = mid - 1; } else { lo = mid + 1; }
    }
    return best;
  };
  const lastTo = function (limit) {
    // highest index whose top is before `limit`
    let lo = 0, hi = all.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (topOf(mid) <= limit) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return best;
  };
  // A row is one card tall, so widen by a row each way rather than
  // trusting the bisection's exact boundary on a partially-realised
  // grid: cheap insurance, and it is still O(log n).
  const columns = 12;
  const from0 = Math.max(0, firstFrom(seenTop) - columns);
  const to0 = Math.min(all.length - 1, lastTo(seenBottom) + columns);
  const cards = to0 >= from0 ? all.slice(from0, to0 + 1) : all.slice(0, 1);
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
               going: false, sized: false, cards: all.length, frames: [] };
  foldState = st;
  // The window says when it has landed (fold.done); if it never does -
  // the page was rebuilt underneath, say - the widths are dropped anyway.
  st.timer = setTimeout(function () { if (foldState === st) endFold(); }, ms + 600);
  tellHost({ action: 'fold', seq: f.seq, ok: 1, from: from, to: to,
             w: innerWidth, n: all.length, near: cards.length,
             moved: anims.length,
             cost: +(performance.now() - t0).toFixed(2), now: Date.now() });
}

function goFold(f) {
  const st = foldState;
  if (!st || st.seq !== f.seq || st.going) return;
  st.going = true;
  const gone = Math.max(0, Math.min(st.ms, Date.now() - (f.at || Date.now())));
  st.anims.forEach(function (a) { a.currentTime = gone; a.play(); });
  const t0 = performance.now() - gone;
  // **How the fold actually ran, not how long preparing it took.** The
  // owner, 3 September 2026: "the cards transition while fold/unfold
  // the sidebar on Manhwa and Movies still not the same as series and
  // anime". His own log already showed why prep cannot answer that -
  // series 1.0ms at 31 cards against movies 3.4ms at 917, a difference
  // nobody can see - so the frames themselves are timed here and
  // reported with the card count beside them.
  st.frames = [];
  // #page's width follows the visible edge, so the scrollbar rides it -
  // as far as the view goes: on an unfold the view is already at the
  // final width and the strip beyond it is the window's, not ours.
  let last = 0;
  function step(now) {
    if (foldState !== st) return;
    if (last) st.frames.push(+(now - last).toFixed(1));
    last = now;
    const p = Math.min(1, (now - t0) / st.ms);
    const e = 1 - Math.pow(1 - p, 3);
    st.width = Math.min(innerWidth, st.from + (st.to - st.from) * e);
    page.style.width = Math.round(st.width) + 'px';
    if (p < 1) { st.raf = requestAnimationFrame(step); return; }
    sayFold(st);
  }
  st.raf = requestAnimationFrame(step);
  tellHost({ action: 'fold', seq: f.seq, went: 1, gone: gone });
}

function hostMessage(ev) {
  let m = ev.data;
  if (typeof m === 'string') {
    try { m = JSON.parse(m); } catch (err) { return; }
  }
  if (!m) return;
  if (m.fold) hostFold(m.fold);
  /* **Draw this route again, now.** The owner, 3 September 2026: "in
     the history make it when I clear the history it immediately clears
     not when I change pages or tabs then come back!"

     _WebPage.reload used to be `show_url(<the same url>)`, and a URL
     that differs in nothing - fragment included - is a *same-document*
     navigation: Chromium fires no hashchange, the router never runs,
     and the document keeps whatever it already drew. So Clear History
     emptied the file and the page went on showing the list until
     something changed the hash. Nothing about the clear was slow; the
     redraw was never asked for. */
  /* **A redraw keeps the place he was reading.** The owner, 4
     September 2026: "when I open an app or a website in the main page
     do not make it takes me up when its reload!". Opening one stamps
     `last_used` so Home can put it first (link_grid._stamp_used), the
     window notices the file move within 150ms and asks for this - and
     the page was drawn again from the top, which on Home means the row
     he had just scrolled down to jumps away under his hand.

     The scroll is this element's own, not the document's (html/body are
     `overflow: hidden` here - see the sweepLazy note), so it is read and
     put back around the render. Restored after a frame, because the new
     content has to be laid out before an offset into it means
     anything. */
  if (m.redraw) {
    const at = page.scrollTop;
    go(currentRoute()).then(function () {
      requestAnimationFrame(function () { page.scrollTop = at; });
    }, function () { /* the route failed; nothing to restore into */ });
    return;
  }
  /* The marks under the file changed - patch the numbers where they
     are rather than redrawing (progressInto). */
  if (m.marks) progressInto();
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
  // A card with nothing to draw asks for something, now that it has
  // been reached - see askForCover for why this waits until here.
  const ask = img._askCover;
  if (ask) { img._askCover = null; ask(); }
}

let sweepTold = 0;
function sweepLazy() {
  lazySweepQueued = false;
  if (!lazyPending.size) return;
  const sweepAt = performance.now();
  const pendingAt = lazyPending.size;
  try { sweepLazyInner(); } finally {
    // **A sweep that costs a frame says so.** It runs on every scroll
    // event, and on a laptop mid-flick with pictures decoding it is the
    // one piece of this file's work on the scroll path; a line here is
    // how his machine reports what this one cannot reproduce.
    const took = performance.now() - sweepAt;
    if (took > 8 && sweepAt - sweepTold > 5000) {
      sweepTold = sweepAt;
      tellHost({ action: 'diag', what: 'lazy sweep slow', ms: Math.round(took),
                 pending: pendingAt, route: location.hash });
    }
  }
}

function sweepLazyInner() {
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
    /* **The card's rectangle, not the picture's.** A `.gc` carries
       `content-visibility: auto`, so asking for a rect *inside* one
       forces the browser to lay out a subtree it had deliberately
       skipped - and this runs over every pending picture, which on a
       scrolled Movies page is nine hundred of them. The card itself is
       the contained element, so its own rect is free; the picture is
       centred in it and a row tall, which is all this test needs. */
    if (img._box === undefined) img._box = img.closest('.gc');
    const at = (img._box || img).getBoundingClientRect();
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

/* **Every card that shows a number says which title's number it is.**
   The owner, 3 September 2026: "make the ep and season / ch numbers
   change immediately when marked as watched/unwatched in home page, and
   saved and all pages that shows progress".

   A web page is a document: it is fetched once and then it is what it
   is, and the marks are written by the reader, the player and the
   details page - three surfaces over this one with no way to reach into
   it. Redrawing the page was the mechanism for Home alone
   (web_pages._WATCH_DATA_ROUTES) and cannot be the mechanism for the
   rest: a catalogue page holds hundreds of rows scrolled in a batch at
   a time and a shelf holds a sort and a selection, all of which a
   redraw throws away.

   So the card carries the two keys /api/progress is indexed by - the
   entry id and the lowercased title, exactly what server._marked_progress
   looks a mark up under - and the parts of the meta line either side of
   the number. progressInto() then rewrites the number where it stands.
   Nothing else on the page moves. */
function markProgress(card, row) {
  card.dataset.pid = row.id || '';
  card.dataset.ptitle = (row.title || '').trim().toLowerCase();
  card.dataset.ptype = row.type || '';
  if (row.status) card.dataset.pstatus = row.status;
  // A unit separator, so a genre containing a comma or a space cannot
  // be split in half by the filter's own delimiter.
  if ((row.genres || []).length) {
    card.dataset.pgenres = row.genres.join('');
  }
  card.dataset.pbase = row.metabase || '';
  card.dataset.psep = row.sep || '  ';
  return card;
}

/* **A reading card with no art asks for its own.** The owner, 3
   September 2026: "in the searching page the readings images are not
   loading". Madara's search endpoint - which is what 3asq answers with -
   returns titles and URLs and no picture at all, so those rows arrive
   coverless; /api/cover walks the same chain the Qt tracker walked for
   a saved entry (the site's own card, then MangaDex, then AniList).

   Asked here rather than inside the search, and that was measured: the
   chain is site round trips, and waiting for it took a 1.33s search to
   a 2.23-3.34s median with a 5.62s worst case (server._card_cover
   carries the numbers). This way the row is on screen at once with the
   blank slab lazyArt already draws, and the picture arrives behind it -
   rule 7's "show what there is". The server caches per (title, url), so
   a card re-drawn on the next visit costs one local request.

   The same route answers the other half of that report - "the reading
   cover images from the 3asq site are not clear at all". `row.thin` is
   the server saying this row's cover is a file the site *named* small
   (`cover_250x350.jpg`, 250px against a card that draws 201 device
   pixels - server._thin_cover has the measurement): the small picture
   is drawn at once and quietly replaced when the better one lands, so
   nothing is ever blank waiting for it.

   Only a reading row, and only one with a page to ask about: a video
   row with no poster has nothing this route could look up. */
const READING_KINDS = ['manga', 'manhwa', 'manhua', 'other'];

/* `width` is what the picture is drawn at, so the replacement is asked
   for at the size it will occupy rather than at a card's 160. */
function askForCover(img, row, width) {
  /* **Every kind, not only reading.** The owner, 4 September 2026:
     "in the schedule page some of the cover images do not load" and
     "the history still does not load images". A video row was refused
     here on its `type` and had nothing else to fall back to, so one
     `image fetch failed for images.metahub.space: HTTPError` in his log
     is a row that stays blank for good - the window's own error handler
     hides the <img> and strips its src (top of this file). The server
     answers a watched title through cover_fetch now, so the gate is
     gone; what is still true is that a row with neither a title nor a
     url has nothing to ask about. */
  if (!row.url && !row.title) return;
  const drawn = width || 160;
  function ask() {
    fetch('/api/cover?title=' + encodeURIComponent(row.title || '') +
          '&url=' + encodeURIComponent(row.url || '') +
          '&type=' + encodeURIComponent(row.type || '') +
          '&imdb=' + encodeURIComponent(row.imdb || '') +
          (row.thin ? '&thin=1' : ''))
      .then(function (r) { return r.json(); })
      .then(function (found) {
        if (!found.cover || !img.isConnected) return;
        /* **Decoded before it is shown, so a replacement can never be
           worse than what it replaces.** Measured 3 September 2026: the
           first version assigned the new URL straight onto the card,
           and Hunter X Hunter's replacement 404d - so a small but
           perfectly visible 250x350 cover became a blank tile. The
           server proves its candidates now (server._card_cover) and
           this proves it again on the way in, because the two failures
           are different: the server asks the host, this asks the
           browser's own cache and decoder. */
        const probe = new Image();
        probe.onload = function () {
          if (!img.isConnected) return;
          img.classList.remove('blank');
          img.style.display = '';
          img.removeAttribute('data-src');
          img.src = probe.src;
        };
        probe.src = artURL(found.cover, drawn);
      })
      .catch(function () { /* whatever is on the card stays */ });
  }
  /* **A cover that fails is the same as a cover that was never
     there.** Measured 3 September 2026 on his Manhwa page, cold: two of
     the twenty-four cards on the first screenful ended with no `src` at
     all, `complete === true` and `naturalWidth === 0` - the window's own
     error handler had hidden them and stripped the URL, which is what
     it is for. They are the blank tiles in the middle of an otherwise
     full page, and nothing was asking for anything better because the
     row *had* a cover; it just did not work.

     Once only, and the `error` listener removes itself first, so a
     replacement that also fails is final rather than a loop. */
  if (row.cover && !row.thin) {
    img.addEventListener('error', function failed() {
      img.removeEventListener('error', failed);
      ask();
    });
    return;
  }
  /* **Asked when the card is reached, not when it is built.** A
     catalogue page scrolled deep holds hundreds of rows, and firing
     this per row at build time would put a site round trip in flight
     for every coverless card on the page at once - the server is
     threaded, so that is dozens of threads doing dozens of scrapes for
     cards nobody has looked at. The lazy sweep already decides which
     pictures are near the viewport (sweepLazy); this rides on the same
     decision, so the number in flight is bounded by what is on screen.
     A card with a picture is unaffected: its `error` listener above is
     free until it fires. */
  img._askCover = ask;
  if (!row.cover) {
    // A card with no picture at all is not in the sweep's queue -
    // lazyArt draws the blank slab and returns without registering it -
    // so it is put there now, with nothing to fetch. take() then finds
    // no data-src, does nothing to the image, and fires the ask. That
    // keeps one rule for "which cards have been reached" rather than a
    // second rectangle test here.
    lazyPending.add(img);
    queueLazySweep();
  }
}

function cardFor(row) {
  const card = el('div', 'card' + (row.kind === 'person' ? ' person' : ''));
  // The frost is part of the ring, so a card without one is a plain
  // cover - a Discover result has nothing to resume.
  const art = el('div', row.resume ? 'art' : 'art plain');
  const img = el('img');
  img.width = 160; img.height = 216;
  img.alt = '';
  lazyArt(img, row.cover, 160);
  askForCover(img, row);
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
    /* **The picture and the IMDb id go with it.** The owner, 4
       September 2026: "when I search and hit Enter then go to some
       watch/read it does not load the bg image and the logo in the
       ch/ep list page!", with a screenshot of a details page whose
       episode list read "This entry has no matched title".

       A row here is not in the library, so the app builds a transient
       entry from exactly what this message carries
       (web_pages._transient) - and this one carried neither. Without
       `poster` the details page's ground is a blur of nothing
       (details._seed_backdrop_from_cover walks cover_path then
       cover_url) and there is no logo to fetch; without `imdb` there is
       no id to match an episode list against, which is the sentence in
       his picture.

       gridCard has sent both since 3 September and this did not, so the
       same title opened correctly from a catalogue page and blank from
       a search, from Home, or from any Discover row - every surface
       drawn as a strip rather than a grid. `art` is the picture's real
       address rather than this server's own token, which is what
       helpers/images is keyed by (web/server._row). */
    tellHost({ action: 'open', kind: row.kind || 'title',
               id: row.id || '', title: row.title || '',
               type: row.type || '', url: row.url || '',
               poster: row.art || row.cover || '', imdb: row.imdb || '' });
  });
  return markProgress(card, row);
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
    /* **And ask for a better one, as a card does.** The owner, 4
       September 2026: "the 3asq readings cover image in the banners home
       and discover pages are not clear (blurry)". A grid card has asked
       /api/cover for a bigger picture since 3 September, which is why
       the same 3asq entry is sharp on Manga and soft here - the banner
       draws it at 196x264 CSS, three times a card's area, off the same
       `cover_250x350.jpg`. `askForCover` is that ask; it replaces the
       picture only when it has fetched something better (see the probe
       inside it), so a banner never blinks to a broken image. */
    askForCover(art, hero, 196);
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
    /* **Discover's banner saves; Home's resumes.** The owner, 3
       September 2026: "in the discovery page, instead of the continue
       btn in the banner make it 'Save to My List' or 'Remove from My
       List'".

       Continue was never right here and could not be: a Discover title
       is not in the library, so `hero.id` is empty, so the message went
       out with `mode: ''` and opened the details page - the identical
       thing the button beside it does. Two buttons for one action, and
       the one thing the banner could usefully offer (put this in the
       library) had no control at all.

       `hero.list` is the boolean the server reads back out of
       series.json/tracker.json on every build (server._discover), so
       the word is right whichever way the title was saved or dropped.
       The button re-labels itself the moment the app answers rather
       than waiting for the page to be rebuilt, because the banner
       rotates every six seconds and a stale word here is worse than a
       slow one. */
    let go;
    if (typeof hero.list === 'boolean') {
      let saved = hero.list;
      go = el('button', 'act go', '');
      /* **The tick says what is; the bin says what the press would
         do.** The owner, 4 September 2026: "make in both ch/ep list
         page and the discover page banner when hover and it is saved
         make the correct-icon turns into bin icon". So a saved title
         rests on a tick and only reads as destructive under the
         pointer - the same swap details._sync_save_button makes, from
         the same three Fluent codepoints (Accept, Add, Delete). */
      let over = false;
      /* The codepoint in its own span, because one element cannot carry
         two font families - see `.hero .act .gi` for the tofu box that
         taught us so. */
      const icon = el('span', 'gi');
      const words = el('span');
      go.appendChild(icon); go.appendChild(words);
      const label = function () {
        if (!saved) {
          icon.textContent = ''; words.textContent = 'Save to My List';
        } else {
          icon.textContent = over ? '' : '';
          words.textContent = over ? 'Remove from My List'
                                   : 'Saved to My List';
        }
        go.classList.toggle('danger', saved && over);
        go.classList.toggle('quiet', saved && !over);
        go.classList.toggle('go', !saved);
      };
      go.addEventListener('mouseenter', function () { over = true; label(); });
      go.addEventListener('mouseleave', function () { over = false; label(); });
      label();
      go.addEventListener('click', function (e) {
        e.stopPropagation();
        const want = !saved;
        saved = want;
        label();
        tellHost({ action: 'list', save: want ? 1 : 0,
                   title: hero.title || '', type: hero.type || '',
                   url: hero.url || '', poster: hero.cover || '',
                   imdb: hero.imdb || '' });
      });
    } else {
      go = el('button', 'act go', '\u25B6  Continue');
      go.addEventListener('click', function (e) {
        e.stopPropagation();
        tellHost({ action: 'open', mode: hero.id ? 'continue' : '',
                   kind: 'title', id: hero.id || '',
                   title: hero.title || '', type: hero.type || '',
                   url: hero.url || '', poster: hero.cover || '',
                   imdb: hero.imdb || '' });
      });
    }
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
    // Same payload a card sends - see cardFor for why the picture and
    // the id have to travel with the click.
    tellHost({ action: 'open', kind: row.kind || 'title',
               id: row.id || '', title: row.title || '',
               type: row.type || '', url: row.url || '',
               poster: row.art || row.cover || '', imdb: row.imdb || '' });
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
      const strip = el('div', 'strip' + (section.style === 'person' ? ' faces' : ''));
      // A face is never a status card: the Cast row lands on Saved's
      // page style nowhere today, and pinning it here means it cannot
      // start to.
      const make = (parent.dataset.cardstyle === 'status'
                    && section.style !== 'person') ? statusCard : cardFor;
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
  /* **The chapter list is this dropdown, and there is no second one.**
     The owner, 4 September 2026: "the ch list button, REMOVE IT
     ENTIRELY AND REMOVE ITS PAGE" and "make the ch selection list in
     the bottom bar of reader at top bar instead of the ch num in the
     mid of top bar".

     There were three ways to change chapter - a button to a whole
     chapter-list *page*, a dropdown on the floor, and Previous/Next -
     and the page was the odd one out: the details page already lists
     every chapter with its read ticks, so the reader's copy of it was
     a second list to keep working and a second place to be wrong.

     The dropdown takes the middle, where the chapter's own number was
     (`title` below): it says which chapter is open, which is all that
     number did, and changes it without leaving the page. The series
     name keeps the left, where he put it on 1 September. */
  const num = el('div', 'rnum', '');
  left.appendChild(back); left.appendChild(num);

  const jump = el('select', 'rjump rtitle');
  jump.title = 'Chapter';
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
  topbar.appendChild(jump);
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
  // The jump list moved to the top bar (see above); the floor keeps
  // the two buttons it always had.
  floor.appendChild(prev);
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
  // **And only manga fills a spread to the column** - see sizePage.
  const fillSpreads = data.medium === 'manga';
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
  /* **One width for the whole chapter, and it is the chapter's own.**
     The owner, 3 September 2026: "the 3asq readings are still not
     loading on fixed pages size and not in good quality as the source
     website".

     Measured that day, every page of two real 3asq chapters fetched
     directly: Kingdom (WAN) ch.886 is **twenty pages at 1325x1920 and
     one spread at 2760x1917**; One Piece ch.1191 is **twelve at
     1644x2400 (one 1644x2335) and three spreads at 3288x2400**. A
     chapter is uniform, and a spread is exactly twice a single page -
     the "822, 1644 and 3288 in one chapter" this file used to record
     was not what these chapters hold.

     So drawing each page at its *own* naturalWidth, which is what this
     did, gave a fixed width by accident for the loaded pages and a
     different one for every page still loading: `--pagew` is
     reader.MEDIUM_TARGET_WIDTH (manga 1100) and the real pages are
     1325 or 1644, so the strip stepped in and out by 20-30% as it
     filled - which is the "not ... on fixed pages size" half of the
     report, and it is worst on 3asq because 3asq's scans are the widest.

     `single` is learned from the first page that is not a spread and
     every single page is then drawn at it, capped to the column;
     `--pagew` is set to the same number so an unloaded box already
     occupies it. A spread keeps its own width, capped the same way,
     because that is what the site does with it and halving it would
     draw two facing pages at half size.

     Quality is unchanged and is still the site's rule: nothing is
     drawn wider than the column, so nothing is upscaled beyond what
     Madara's own `max-width:100%` upscales - and the pages inside one
     chapter differ by pixels, not by a factor, so holding them to one
     width costs nothing measurable. */
  /* **The Qt reader's own rule, number for number.** The owner, 3
     September 2026: *"the reading view page sizes and the quality are
     not good at all, compare them with the old Qt and make it like the
     Qt it was good! (keep the Webviewer2)"*.

     So this is windows/reader.py's `_on_page_width` transcribed rather
     than re-derived, and its constants are that file's:

       MEDIUM_TARGET_WIDTH  manga 1100, manhwa 762, manhua 762
       STRIP_TARGET_WIDTH   762      (a page taller than 3x its width)
       STRIP_ASPECT_MIN     3.0

     and the rule it applies, in logical pixels:

       a strip-shaped page  -> STRIP_TARGET_WIDTH, whatever the entry
                               calls itself;
       a paged scan wider
       than its medium's
       target               -> its own width, capped to the window
                               ("native up to the window" - his ask of
                               24 August, "why does the ch appear in
                               less resolution than the original");
       a paged scan narrower -> the medium's target, i.e. scaled up to
                               a readable column;
       a double-page spread -> fitted to the window, untouched by the
                               above (reader._show does exactly this).

     What that gives on the four chapters measured 3 September 2026,
     with 1899 CSS px of column:

       Kingdom 883    1326 wide, aspect 1.45  -> 1326  (its own width)
       Kingdom 885     829 wide, aspect 2.32  -> 1100  (manga's target)
       One Piece      1644/822,  aspect 1.46  -> 1644 / 1100
       Eternal Supreme 800 wide, aspect 14.3  ->  762  (a strip)

     The previous version here drew every one of them at 1326 - one
     size, which is what "fixed page size" had asked for, and the wrong
     size for a webtoon strip and for a narrow manga scan. Qt never had
     one size; it had one size *per medium and per shape*, and that is
     what he is comparing against.

     Unknown media keep their own width, capped to the column, because
     that is what Qt does when MEDIUM_TARGET_WIDTH has no row: it never
     sets a base scale at all. */
  const DPR = window.devicePixelRatio || 1;
  let single = 0;          // this chapter's page width, in image pixels

  /* **Qt's targets are image pixels, and that is the half that was
     missing.** The owner, 4 September 2026: "the manga still has no
     size and quality as before in Qt".

     reader._decode_page_job scales a page to `target` **pixels** and
     `_tagged` then hands the pixmap to Qt with the screen's device
     ratio on it - so a 1100-pixel page occupies 1100/1.25 = 880
     *logical* pixels on his panel and is blitted one image pixel to one
     screen pixel. Nothing is ever resampled at paint time; the one
     resample happens in the decode, with SmoothTransformation, and the
     result is exact.

     The first attempt at this read the same numbers as CSS pixels, so
     every page came out 1.25x too wide AND upscaled by the compositor
     on top - bigger and softer, which is precisely the two words he
     used. Dividing by the device ratio is what makes it Qt.

     Measured against his own chapters (1899 CSS px of column, DPR 1.25):

       Kingdom 883   1326px scan -> 1326 image px -> 1061 CSS (1:1)
       Kingdom 885    829px scan -> 1100 image px ->  880 CSS
       One Piece      822px scan -> 1100 image px ->  880 CSS
       Eternal Sup.   800px strip ->  762 image px ->  610 CSS (a downscale)
       any spread                 -> the column, as reader._show fits it

     This is about 20% smaller on screen than the version before it, and
     that is the trade he has now asked for twice: Qt's size was the one
     he called good. The zoom control multiplies it. */
  /* **The medium's width is the size; the file's width is the quality,
     and they are two different questions.** The owner, 4 September
     2026: "the reader quality is bad in all readings, but the 3asq also
     has the pages size different from the source site!!!!"

     Measured against the site he reads on, same chapter, same maximised
     window: 3asq draws its page at about **55%** of the screen, which
     on his panel is ~1136 CSS px - and MEDIUM_TARGET_WIDTH's 1100 is
     that number. The scan itself is only **829 px** (the reader's own
     `reader sized` line), so the site is *enlarging* it too. Drawing it
     at 829 to avoid that made it 40% of the screen and no longer the
     size he reads at, which is the pass before this one.

     So the size stays the medium's - Qt's own targets - and the
     enlargement is made to cost nothing, which is askForExact's job
     below: the browser upscales the original file once, rather than the
     proxy upscaling it *and* re-encoding it and the browser then drawing
     that. Two resamples and a JPEG round trip is what "the quality is
     bad in all readings" was. */
  /* **The site's rule, and nothing else - 5 September 2026.** The owner:
     "3asq manga page size and sharpness: make it like the source site
     exactly on size and quality, also the other sites like TeamX and
     others". Measured that day on his own three reading hosts, each
     site's reader page fetched with its stylesheets and one chapter's
     images:

       3asq (Madara)       page 1325x1920, `.reading-content img
                           {max-width:100%}` in a `.container{max-width:
                           100%;padding:0 30px}` - drawn at its natural
                           width, capped to the column (1327 of a 1638px
                           viewport, read off the live page)
       TeamX/olympustaff   page 760x12945, `.manga-chapter-img` in a
                           `.reading-content{padding:0}` column - natural
       Lava Scans          page 800x14995, `.reader-area{max-width:800px}
                           .reader-area img{width:100%}` - 800, which is
                           the natural width of every page it serves

     So every site draws a page at its own width in CSS px, capped to
     the column, and the medium floors this function used to apply
     (manga 1100, strips 762) are what made the app differ: an 829px
     3asq chapter drew at 1100 - a 1.33x enlargement the site does not
     make, which is the "sharpness" in his report - and a 800px strip
     drew at 762, 5% under the site. His earlier ask to make narrow
     scans bigger (4 September) is superseded by this one, and said so
     here rather than silently.

     Quality follows from size: a page drawn at its natural CSS width on
     a 125% display is enlarged 1.25x by *some* resampler either way;
     the site leaves that to the browser's bilinear stretch, the app asks
     the proxy for the device pixels once (askForExact, server._scaled's
     LANCZOS + unsharp, measured 13.53 against 10.18 for the stretch) and
     draws them 1:1. Same size, at least the site's sharpness. */
  function targetFor(naturalWidth, naturalHeight, available) {
    return Math.min(naturalWidth, available);
  }
  /* **The target is a width on the page, not a count of image pixels.**
     The owner, 4 September 2026, with the app beside the site the same
     chapter is read on: the app's page took about **37%** of the window
     and the site's took **55%** of the same screen - "look at the diff
     BRO!!!!".

     Dividing by the device ratio here is what made it small: the page
     was laid out at `target / 1.25` CSS px, so MEDIUM_TARGET_WIDTH's
     1100 became 880 CSS - 1100 device pixels on his panel, measured -
     against roughly 1420 for the same page on the site. Without the
     divide, 1100 is 1100 CSS px (1375 device, 53% of his window), and a
     1325px scan draws at 1325 CSS - which is that scan at its own
     resolution, one image pixel per CSS pixel, exactly what a browser
     showing the file does.

     Nothing is lost on sharpness by this: askForExact still asks the
     proxy for `drawn * DPR` device pixels and Qt still does the one
     resample (server._scaled), so the page is cut for the pixels it
     actually occupies. */
  function pageWidth(width, available, zoom) {
    const css = Math.round(width * zoom);
    return Math.max(1, Math.min(css, available));
  }
  /* **A page's own size, remembered.** Once askForExact swaps in a
     copy resampled to the drawn width, `naturalWidth` is that width -
     so every rule that reads it would be reading its own last answer
     and the page would walk. `_natW/_natH` are the scan's, taken the
     first time it loads and never touched again. */
  function natural(img) {
    if (!img._natW && img.naturalWidth) {
      img._natW = img.naturalWidth;
      img._natH = img.naturalHeight;
    }
    return [img._natW || img.naturalWidth, img._natH || img.naturalHeight];
  }
  function isSpread(img) {
    const [w, h] = natural(img);
    return h > 0 && w / h > 1.2;
  }
  function sizePage(img) {
    const [natW] = natural(img);
    if (!natW) return;
    /* **The chapter's width, but never past this page's own pixels.**
       The owner, 4 September 2026: "in the reading viewer some pages
       load in perfect quality and some in bad quality in 3asq
       readings!!!!"

       `single` is one width for the whole chapter, taken from its first
       ordinary page, so the strip lines up. A 3asq chapter is not cut at
       one width though - measured across his own chapters, 829, 1325 and
       1327 - and every page narrower than `single` was being stretched
       to it: a 800px scan drawn at 1327 CSS is a 1.66x upscale, beside
       neighbours drawn 1:1. That is exactly "some perfect, some bad",
       within one chapter.

       Drawing each page at its own width answered that and cost the
       thing it was protecting. The owner, 4 September 2026: "make the
       small pages bigger, keep them sharp". Measured on his own titles:
       a chapter is not a few odd narrow pages, it is scanned small
       *whole* - One Piece ch906 is 890 to 921 px across all sixteen
       pages against a manga target of 1100, and those sixteen widths
       are all slightly different, so per-page sizing gave the strip a
       ragged edge as well as a small one.

       So the chapter's one width is back, and the sharpness is bought
       where it was actually being lost: server._scaled now enlarges
       with LANCZOS and an unsharp mask instead of handing the job to
       the compositor, measured at 13.53 against 10.18 for the browser's
       own stretch on that page. See askForExact. */
    /* **Each page at its own width now, as the site draws it** (5
       September 2026 - see targetFor). The chapter-wide `single` above
       stretched every narrower page to the first page's width, which on
       a 3asq chapter mixing 829 and 1325 is a 1.6x enlargement of some
       pages beside neighbours drawn 1:1 - the site makes no such
       stretch, and "exactly like the site" is the ask. `single` keeps
       its other job: the size of the box a page occupies before it has
       arrived (--pagew), so the strip does not jump as it fills. */
    /* **A manga spread is drawn at the column's width, whatever the
       scan's.** The owner, 5 September 2026: "make the double pages in
       the Manga fit in width and make sure it works in all resolutions
       monitors! (ONLY IN MANGA, and keep the single pages as is)".

       Measured that night on his Kingdom (WAN), through the app's own
       pages route: ch.886 opens on a 2760x1917 spread and ch.885 holds
       a 1205x880 one beside 1327px singles. Under the site's rule above
       the first took the whole 1991px column of his panel and the second
       took 60% of it - the same kind of page at two sizes, and the
       smaller one drawn at the width of a single page, which is what a
       spread is not. So a spread's target is the column itself, read
       live (availableWidth) so every window and monitor gets its own,
       and zoom still multiplies it; a single page keeps the site's rule
       untouched. Only manga, by the same medium test `paged` uses: a
       manhwa or manhua strip is never a spread. The enlargement is the
       proxy's LANCZOS pass (askForExact), as for any small scan. */
    const spread = fillSpreads && isSpread(img);
    const want = spread ? availableWidth() : natW;
    const drawn = pageWidth(want, availableWidth(), readerState.zoom);
    img.style.width = drawn + 'px';
    if (spread && Math.abs((img._spreadAt || 0) - drawn) / drawn > 0.02) {
      // Once per real change of size, so his log carries the number.
      img._spreadAt = drawn;
      const [, natH] = natural(img);
      tellHost({ action: 'diag', what: 'reader spread', scan: natW + 'x' + natH,
                 column: Math.round(availableWidth()), drawn: drawn,
                 zoom: Math.round(readerState.zoom * 100) });
    }
    askForExact(img, drawn);        // in image pixels: drawn * DPR
  }

  /* **Ask the server for the page at the size it is drawn, once.**
     The owner, 4 September 2026: "the manga still has no size and
     quality as before in Qt".

     The size is Qt's now (targetFor above), and what was left is who
     does the resampling. Qt decoded each page once at its drawn size
     with `scaledToWidth(..., SmoothTransformation)` and blitted the
     result 1:1; the browser was given the scan at its own resolution
     and a CSS width, so the compositor resampled it on every paint with
     the filter it uses for that - which on line art is what softens the
     inking. server._scaled(exact=1) is Qt's resample, done once and
     cached beside the page.

     In device pixels, because that is what is actually painted: a page
     drawn 1100 CSS px wide on his 1.25 display is 1375 real ones.
     Re-asked only when the drawn size genuinely changes (a zoom, a
     window resize, the sidebar folding), never per paint - `_exactAt`
     is the guard, and a change under 2% is not worth a re-decode. */
  function askForExact(img, drawn) {
    const want = Math.round(drawn * (window.devicePixelRatio || 1));
    if (!want || !img._rawSrc) return;
    /* **A request that will not be made puts the original back.** The
       owner, 4 September 2026: "when I zoom on or out while I am inside
       the reader the quality goes bad!".

       Zooming out asks for fewer pixels than the scan has, so the proxy
       answers with a genuinely smaller file and `img.src` becomes that
       file. Zooming back in past the ceiling below then returns -
       correctly, there is nothing better to ask for - and it used to
       return leaving the *downscaled* copy in place, so the browser was
       enlarging a 600px picture instead of the 921px original. Every
       out-and-back lost resolution for good, which is both of his
       directions in one bug. Restoring the scan costs nothing but the
       assignment: the browser already holds that file. */
    /* **A page whose size is not known yet is not asked for at all.**
       The owner, 4 September 2026: "now the ch pages sometimes has the
       quality and the size issue!!!!!!!!!!!!!!!"

       `natural()` pins the scan's width the first time the picture
       decodes, and sizePage runs before that too - on the placeholder
       box, on a resize, on a zoom. With `natW` still 0 the guard below
       could not fire, so those calls asked the proxy for whatever the
       estimate said, which for a scan narrower than its medium's width
       is an upscale and a JPEG round trip: exactly the softness this
       guard exists to prevent, on whichever pages happened to be sized
       before they arrived. That is the "sometimes".

       Not knowing the width is a reason to wait, not to guess: the load
       calls sizePage again (see the `load` handler on each page), and by
       then `natural()` has an answer. */
    const [natW] = natural(img);
    if (!natW) return;
    /* **An enlargement is asked for again, because the proxy is now
       better at it than the browser.** The owner, 4 September 2026:
       "make the small pages bigger, keep them sharp".

       The pass before this declined every upscale and left the browser
       to stretch the original, on the reasoning that the proxy's
       enlargement plus a JPEG round trip was two resamples where the
       browser does one. The first half was right and the second was
       measured wrong: it is one resample either way (the answer is
       exactly the device pixels the page occupies, so nothing resamples
       it again), and the browser's is a bilinear stretch - the worst of
       the four measured. server._scaled's enlargement is LANCZOS plus
       an unsharp mask and scores 13.53 edge contrast against its 10.18.

       The ceiling is 2.5x the scan. Past that there is nothing left to
       sharpen and the file is only getting heavier - a 200px thumbnail
       blown to a full page is a different problem from a small scan. */
    if (want > natW * 2.5) {
      if (img._exactAt) {
        img._exactAt = 0;
        if (img.getAttribute('src') !== img._rawSrc) img.src = img._rawSrc;
      }
      return;
    }
    /* **A long strip is never asked to be enlarged.** The owner, 5
       September 2026: "the eternal supreme ch 550 is the one that has
       the quality issue!".

       800px is what these sites publish for a strip, not a scan that
       came out small - measured that day, his manhwa and manhua sources
       are 800px on every page, and lavascans' own unsuffixed variant is
       690px, smaller than what it already serves. So there is nothing to
       recover by enlarging, and enlarging costs something real here: a
       strip is 13,000-17,000px tall, so the 1.25x his display asks for
       made every page of ch.550 16,831 to 21,379 pixels tall - past the
       16,384-pixel edge a browser can texture, whereupon it downsamples
       the whole page to fit. That is the softness, and it lands only on
       the strips because a manga page is 1920px tall.

       server._scaled refuses the same enlargement and clamps to the
       edge limit, which is the invariant; this is the round trip that no
       longer has to happen at all. A *reduction* is still asked for
       normally - that is the zoomed-out case, and it is real work. */
    const [, natHeight] = natural(img);
    if (natHeight && natHeight >= natW * 3 && want > natW) {
      if (img._exactAt) {
        img._exactAt = 0;
        if (img.getAttribute('src') !== img._rawSrc) img.src = img._rawSrc;
      }
      return;
    }
    if (img._exactAt && Math.abs(img._exactAt - want) / want < 0.02) return;
    img._exactAt = want;
    const url = img._rawSrc + (img._rawSrc.indexOf('?') >= 0 ? '&' : '?')
              + 'w=' + want + '&exact=1';
    const probe = new Image();
    probe.onload = function () {
      // Only if this is still the size it wants - a zoom mid-fetch
      // makes the answer stale, and the newer request will land.
      if (img._exactAt === want && img.isConnected) img.src = probe.src;
    };
    probe.src = url;
  }

  function applyZoom() {
    zoomLabel.textContent = Math.round(readerState.zoom * 100) + '%';
    // The estimate box moves with the pages, or a chapter re-sized
    // mid-load steps around whatever has not arrived yet.
    if (single) {
      strip.style.setProperty(
        '--pagew',
        pageWidth(single, availableWidth(), readerState.zoom) + 'px');
    }
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
    try { localStorage.setItem('atomic.reader.zoom', String(readerState.zoom)); }
    catch (err) { /* a private window; the zoom still holds this session */ }
    applyZoom();
  }
  /* **The zoom is remembered between sessions.** It survived a chapter
     change already; it did not survive closing the app, so a size he
     had set by hand was gone the next morning and the argument started
     again. Kept in settings beside everything else he sets once. */
  if (!readerState.zoom) {
    try {
      const kept = parseFloat(localStorage.getItem('atomic.reader.zoom'));
      readerState.zoom = (kept > 0.2 && kept < 5) ? kept : 1;
    } catch (err) { readerState.zoom = 1; }
  }
  applyZoom();
  zoomIn.addEventListener('click', function () { changeZoom(0.15); });
  zoomOut.addEventListener('click', function () { changeZoom(-0.15); });
  zoomLabel.addEventListener('click', function () {
    readerState.zoom = 1;
    try { localStorage.setItem('atomic.reader.zoom', '1'); } catch (err) {}
    applyZoom();
  });

  // The chapter's *own* number beside the door, and the series title
  // centred - reader keeps those two apart on purpose, the index in a
  // 507-chapter listing saying nothing the number does not.
  // **The name at the door, the chapter in the middle.** The owner, 1
  // September 2026: "in the upper bar in the reading write the reading
  // name in the place of the ch num up left, and make the ch num in the
  // mid top bar instead of the reading name".
  num.textContent = data.title || '';
  label.textContent = (data.count || 0) + ' pages';

  // **The list runs newest first**, so the next chapter is a *lower*
  // index and the previous one a higher. Measured on the owner's
  // Kingdom: index 0 is chapter 886, index 380 is chapter 1. Wired the
  // other way round, Next Chapter walked backwards through the series.
  next.disabled = index <= 0;
  prev.disabled = index >= (data.total || 1) - 1;
  prev.addEventListener('click', function () { openChapter(id, index + 1); });
  next.addEventListener('click', function () { openChapter(id, index - 1); });
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
    const [natW, natH] = natural(img);
    if (!natW || !natH) return;
    // This page's own shape, always. A double-page spread is twice as
    // wide as a single one, and forcing it into the shared ratio is
    // what squashed it instead of fitting it to the width.
    img.style.aspectRatio = natW + ' / ' + natH;
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
    if (natH / natW >= STRIP_ASPECT) {
      strip.classList.remove('paged');
    }
    // **And a spread gets the whole width.** A single manga page is
    // drawn at reader.MEDIUM_TARGET_WIDTH (1100); a two-page spread is
    // about twice as wide, and holding it to the single-page column
    // left it small in the middle of the window instead of filling it.
    // 1.2 rather than exactly 2: scans are trimmed unevenly, and
    // nothing that is merely taller than wide should ever qualify.
    if (natW / natH > 1.2) {
      img.classList.add('spread');
    }
    if (measured) return;
    /* **The first *single* page becomes the estimate for every box
       still unloaded**, so nothing below moves as they come in - and
       the width as well as the shape, which is the half that was
       missing. `--pagew` was reader.MEDIUM_TARGET_WIDTH (manga 1100)
       while the loaded pages drew at the scan's own width (1325 on his
       Kingdom, 1644 on One Piece), so the strip changed width around
       every page that had not arrived yet.

       Never from a spread: Kingdom ch.886 *opens* on one (2760x1917),
       and taking it would give every unloaded box twice the width and
       two-thirds the height of the page that replaces it - the worst
       possible estimate, and the one this chapter would always have
       picked. A chapter that is nothing but spreads keeps the first of
       them rather than no estimate at all. */
    const spread = isSpread(img);
    if (spread && !strip.dataset.est) {
      strip.dataset.est = 'spread';       // a stand-in until a single arrives
      strip.style.setProperty('--ar', natW + ' / ' + natH);
      return;                             // keep looking
    }
    if (spread) return;
    measured = true;
    strip.dataset.est = 'single';
    // The chapter's one width, decided by the first ordinary page the
    // way reader._on_page_width decides it - see targetFor.
    single = targetFor(natW, natH, availableWidth());
    /* **The reader says what it decided.** Three rounds of "the sizes
       and the quality are wrong" were argued from words alone, because
       nothing on either side of the conversation carried a number. This
       one line puts the scan's size, the medium's target, the width on
       screen and the pixels asked of the proxy into atomic.log, so the
       next report arrives with its own evidence. */
    tellHost({ action: 'diag', what: 'reader sized',
               medium: data.medium || '', scan: natW + 'x' + natH,
               target: single, dpr: DPR,
               column: Math.round(availableWidth()),
               drawn: pageWidth(single, availableWidth(), readerState.zoom),
               zoom: Math.round(readerState.zoom * 100) });
    strip.style.setProperty('--ar', natW + ' / ' + natH);
    // The same number sizePage will compute, so an unloaded box is
    // already the width of the page that replaces it.
    strip.style.setProperty(
      '--pagew', pageWidth(single, availableWidth(), readerState.zoom) + 'px');
    // Every page already on screen re-measured against it, so the one
    // width applies from the first page rather than the second.
    strip.querySelectorAll('img.rpage').forEach(sizePage);
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
    // The page's own address, kept so it can be re-asked for at the
    // size it is drawn - see askForExact.
    img._rawSrc = src;
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
    savedState = { selecting: false, picked: new Set() };
  }
  const mine = ++token;
  routeAt = performance.now();
  // A filter belongs to the page it was typed on - see filterInto.
  if (route.split('&')[0] !== currentRoute().split('&')[0]) resetFilter();
  // Every picture the last render was waiting on died with it.
  resetLazy();
  location.hash = route;
  // read/<id>/<index> is the reader. It draws itself rather than going
  // through the sections path below. `chapters/<id>` used to be a route
  // too - a whole chapter-list page - and was removed on 4 September
  // 2026 at the owner's word ("REMOVE IT ENTIRELY AND REMOVE ITS
  // PAGE"): the details page already lists every chapter with its read
  // ticks, and the reader's own dropdown changes chapter in place.
  const bits = route.split('/');
  if (bits[0] === 'read') {
    openChapter(decodeURIComponent(bits[1] || ''),
                parseInt(bits[2] || '0', 10));
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
  // The tick list is a child that was just removed; the reference to it
  // is not, and a page without a filter would otherwise keep writing
  // into the last page's box.
  page._genreBox = null;

  /* **The way out is drawn before the rows are asked for.** The owner,
     4 September 2026: "in the genre and cast pages make the buttons
     appear even if the cards did not load yet."

     Those two routes are the slowest the app has - a genre is three
     Cinemeta catalogs walked for one genre's rows and measured at 2-10s
     - and the header, the back button included, was built *after* the
     await. So the whole wait was a blank window inside web_reader's
     shell, which covers the app's own title bar: nothing on screen and
     no way back except Escape. Rule 7's answer is to draw what there is
     at once, and the door is something there is.

     Only these two, because only they are drawn in that shell and only
     they are slow; the header below reuses this element rather than
     making a second one, so nothing moves when the rows land. */
  const early = route.split('&')[0].split('?')[0];
  if (early === 'genre' || early === 'cast') {
    const head = el('header', 'gridhead');
    const door = el('button', 'pback', '');
    door.title = 'Back (Esc)';
    door.addEventListener('click', function () {
      tellHost({ action: 'close' });
    });
    head.appendChild(door);
    head.appendChild(el('p', 'pwait', 'looking...'));
    page.appendChild(head);
  }

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
    // Reuse the door drawn before the fetch, if there is one, so the
    // button does not jump as the rows arrive - see the note above.
    const early_head = page.querySelector('header.gridhead');
    const head = early_head || el('header',
                                  data.kind === 'grid' ? 'gridhead' : null);
    if (early_head) {
      const wait = early_head.querySelector('.pwait');
      if (wait) wait.remove();
    }
    /* **A door out of the genre and cast pages.** The owner, 3
       September 2026: "in the same genere/ cast pages there is no back
       button, add it".

       These two routes are the only ones drawn inside web_reader's
       shell, which takes `immersive_host` - the whole window, the app's
       own title bar included - so there was no Back anywhere on screen
       and Escape was the only way back. The same shell already carries
       `{action:'close'}` for the reader's own chevron, so the button is
       that message and the Qt side needs nothing new.

       The glyph is the reader's own: U+E76B, ChevronLeft out of Segoe
       Fluent Icons, the same codepoint openChapter's back button uses,
       so the two doors look like one control in two places. */
    if (data.back && !head.querySelector('.pback')) {
      const door = el('button', 'pback', '');
      door.title = 'Back (Esc)';
      door.addEventListener('click', function () {
        tellHost({ action: 'close' });
      });
      head.appendChild(door);
    }
    if (data.title) head.appendChild(el('p', 'ptitle', data.title));
    /* **Anime / Series / Movies on a genre or a cast page.** The owner,
       3 September 2026. The kind is on every row already, so a tab is a
       re-fetch of the same route with `tab=` rather than a different
       source - which keeps the scroll-for-more cursor honest, because
       `browse` carries the tab too (server._more_browse). */
    if ((data.browsetabs || []).length) {
      const tabs = el('div', 'btabs');
      data.browsetabs.forEach(function (t) {
        const pill = el('button',
                        'pill2' + (t.key === data.browsetab ? ' on' : ''),
                        t.label);
        pill.addEventListener('click', function () {
          const base = route.split('&tab=')[0];
          go(base + '&tab=' + encodeURIComponent(t.key));
        });
        tabs.appendChild(pill);
      });
      head.appendChild(tabs);
    }
    if (data.note) head.appendChild(el('p', null, data.note));
    // A route that raised answers {error} now (server.do_GET) - said
    // here rather than drawn as an empty page.
    if (data.error) head.appendChild(el('p', 'empty', data.error));
    if (!head.isConnected) page.appendChild(head);
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
  if (data.cardstyle === 'status') {
    // Saved: the library, grouped by status - so the ticks are the
    // statuses on the page, which is what the Qt filter menu offered
    // (tracker._build_filter_menu reads _page_statuses()).
    /* **Every status the side has, not only the ones in use.** The
       owner, 4 September 2026: "re-add like the Qt the other than
       Watching / Reading like on hold and so on in the saved!". This
       read the statuses off the section headings, so a library that is
       all Watching offered one pill - and On Hold, Dropped, Completed
       and Plan to Watch could not be reached from the page at all. The
       server sends the whole list now (server.SAVED_STATUSES, which is
       tracker's own); the headings are still the fallback for a status
       an older answer does not name. */
    const seen = (data.statuses || []).slice();
    (data.sections || []).forEach(function (sec) {
      const status = String(sec.title || '').split('·')[0].trim()
        .replace(/\s*\(\d+\)\s*$/, '');
      if (status && seen.indexOf(status) < 0) seen.push(status);
    });
    filterInto(page, data, [{key: 'all', label: 'All'}].concat(
      seen.map(function (t) { return {key: t.toLowerCase(), label: t}; })));
    savedSelectInto(page, data);
  }
  if (data.kind === 'downloads') {
    downloadsInto(page, data);
    /* **Asked again every second, redrawn only when something changed.**
       The owner, 4 September 2026: "the Downloads page stutters even
       when I am not moving."

       This used to call `go('downloads')` on the timer, and go() empties
       the page and builds it again from nothing - so the whole document
       was torn down and re-created once a second whether or not a single
       byte had moved. That is the stutter, and it is why it happened
       with the page sitting still.

       A download has no event to listen to, so the polling stays (the Qt
       page's own QTimer does the same); what changes is that the answer
       is *patched* into the rows that are already there, the way
       progressInto patches a card's number. A job list that has not
       moved touches nothing at all, and a running one writes one string
       and one width per row. Only a change in *which* jobs exist rebuilds
       the page. */
    downloadsTimer = setInterval(function () {
      if (currentRoute() !== 'downloads' || mine !== token) return;
      fetch('/api/downloads')
        .then(function (r) { return r.json(); })
        .then(function (fresh) {
          if (currentRoute() !== 'downloads' || mine !== token) return;
          downloadsPatch(fresh);
        })
        .catch(function () { /* one dropped poll changes nothing */ });
    }, 1000);
    return;
  }
  if (data.kind === 'grid') {
    // The six Watch/Read catalogues, and the genre and cast pages -
    // every page whose rows are titles the library may or may not hold.
    filterInto(page, data, [{key: 'all', label: 'All'},
                            {key: 'saved', label: 'Saved'},
                            {key: 'unsaved', label: 'Not saved'}]);
    const grid = el('div', 'grid');
    (data.rows || []).forEach(function (row) { grid.appendChild(gridCard(row)); });
    page.appendChild(grid);
    if (!(data.rows || []).length) {
      page.appendChild(el('div', 'empty', 'Looking around...'));
    }
    sayRender(route, performance.now() - routeAt, (data.rows || []).length);
    // The ticks are built from the cards, so they can only be built once
    // the cards exist - filterInto runs before the grid is appended.
    refreshGenres(page);
    applySort(page);
    applyFilter(page);
    liveBrowse(data, page, grid, token);
    moreOnScroll(data, page, grid, token);
    return;
  }

  page.dataset.cardstyle = data.cardstyle || '';
  sectionsInto(page, data.sections || []);
  refreshGenres(page);
  applyFilter(page);
  if (!(data.sections || []).some(function (s) { return s.rows.length; })) {
    page.appendChild(el('div', 'empty', 'Nothing here yet.'));
  }
  sayRender(route, performance.now() - routeAt,
            (data.sections || []).reduce(function (n, s) {
              return n + (s.rows || []).length; }, 0));
}



/* ---- an eased wheel notch, on every page ---------------------------
   The owner, 1 September 2026: "make the mouse wheel tick travels the
   same distance as now but not on a jump, do this only on movies page so
   that I test it." - and, having tested it, 3 September 2026: **"make
   the scrolling on all pages in the app EXACTLY like the movies page it
   is smooth on scrolling on the mouse wheel!"**. So the gate is gone
   and the same curve runs everywhere `#page` scrolls.

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
/* **The glide reports itself.** Measured 5 September 2026 on the frozen
   build: five notches on Movies were 130 eased frames on a fresh launch
   and five single jumps once the reader had been opened and closed.
   Nothing in the log could say which half of the machinery had stopped
   - the frames, or the handler - so a finished glide writes how many
   frames it got, how far apart they were, and whether the document
   thought it was visible. Rate-limited to one line a second. */
let glideFrames = 0, glideLastNow = 0, glideGapMax = 0, glideTold = 0;

function glideStep(now) {
  if (glideTo === null) return;
  glideFrames++;
  if (glideLastNow) glideGapMax = Math.max(glideGapMax, now - glideLastNow);
  glideLastNow = now;
  const t = Math.min(1, (now - glideAt) / GLIDE_MS);
  // Chromium's own ease-out, the curve its scroll animation uses.
  const eased = 1 - Math.pow(1 - t, 3);
  page.scrollTop = glideFrom + (glideTo - glideFrom) * eased;
  if (t < 1) {
    requestAnimationFrame(glideStep);
  } else {
    glideTo = null;
    if (now - glideTold > 1000) {
      glideTold = now;
      tellHost({ action: 'diag', what: 'glide', frames: glideFrames,
                 ms: Math.round(now - glideAt), gapMax: Math.round(glideGapMax),
                 visible: document.visibilityState, hasFocus: document.hasFocus(),
                 route: location.hash });
    }
    glideFrames = 0; glideLastNow = 0; glideGapMax = 0;
  }
}

/* **A finger is told by its cadence as well as its size.** The owner,
   5 September 2026, on his laptop: "when I scroll using the touch pad
   with 2 fingers the scroll stutters especially while cards are
   loading". A precision touchpad streams wheel events at 60-120Hz and a
   flick's deltas run well past NOTCH_MIN_PX, so the size test alone
   let every larger delta of a fast flick through to the glide - a new
   130ms ease re-aimed on each of them, a few milliseconds apart, on the
   main thread while the cards' pictures were decoding. Chromium's own
   compositor path, which the small deltas were already taking, is what
   the touchpad should have all along. A mouse notch cannot follow a
   finger's stream: it arrives alone, or at 30ms+ from the last notch. */
const FINGER_GAP_MS = 40;
let lastWheelAt = 0, lastWasFinger = false;
addEventListener('wheel', function (e) {
  if (e.ctrlKey || e.shiftKey || e.deltaMode !== 0) return;
  const now = performance.now();
  const finger = Math.abs(e.deltaY) < NOTCH_MIN_PX
                 || (lastWasFinger && now - lastWheelAt < FINGER_GAP_MS);
  lastWheelAt = now; lastWasFinger = finger;
  if (finger) return;                                   // the browser's own
  /* **The reader too - 5 September 2026.** It used to be the one
     surface this did not take, on the note that its scrolling was
     "already the browser's own and already smooth"; with Chromium's
     wheel animation off (webview2_host._BROWSER_ARGS) the browser's own
     is one 100px jump per notch, and that is what the owner measured
     against Movies: "make the scrolling in the reader smooth exactly
     like the scrolling in movies page". Sampled at 240Hz on the frozen
     build: Movies 33 eased frames a notch, the reader one frame of
     125px. Same page element scrolls in both, same curve now. */
  e.preventDefault();
  const limit = page.scrollHeight - page.clientHeight;
  const from = glideTo === null ? page.scrollTop : glideTo;
  glideTo = Math.max(0, Math.min(limit, from + e.deltaY));
  glideFrom = page.scrollTop;
  glideAt = performance.now();
  if (glideFrames === 0) { glideLastNow = 0; glideGapMax = 0; }
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
  if (savedState.selecting) {
    /* **A sheet over the card, not a copy of it.** cardFor has already
       wired the body to open and the ring to continue, and while picking
       neither should fire. Cloning the node drops those listeners but
       drops the picture with them: `lazyArt` fills each `<img>` when the
       observer reaches it, so a clone taken before that is an empty box -
       measured, every cover on the page blank the moment Select was
       pressed.
       So the card is left exactly as it is and a transparent child is
       laid over it. The click lands on the sheet, which is the target,
       and stopPropagation keeps it from reaching the card underneath. */
    card.classList.add('picking');
    const picked = savedState.picked.has(row.id);
    if (picked) card.classList.add('picked');
    const sheet = el('div', 'spickover');
    const mark = el('div', 'spick' + (picked ? ' on' : ''));
    sheet.appendChild(mark);
    /* **Picked in place, never redrawn.** The owner, 4 September 2026:
       "when I select a card or unselect it in the selection mode in
       saved it takes me to the top of the page". `go()` rebuilds the
       page element, so the scroll position went with it - on a library
       that is two screens deep, every tick threw him back to the top.
       A pick changes two classes and the bar's three numbers; nothing
       else on the page moves, so nothing else is touched. */
    sheet.addEventListener('click', function (e) {
      e.stopPropagation();
      const on = savedState.picked.has(row.id);
      if (on) savedState.picked.delete(row.id);
      else savedState.picked.add(row.id);
      card.classList.toggle('picked', !on);
      mark.classList.toggle('on', !on);
      if (page && page._savedSync) page._savedSync();
    });
    card.appendChild(sheet);
  } else {
    /* **Right-click sets the status.** The owner, 4 September 2026:
       "add some way to change from watching to other status like right
       click on the card in the saved page, then change status or
       something."

       Qt owns the menu, as it does for a shelf card (shelfCard): the
       statuses differ by side, the write is storage's, and a native
       menu is what the Qt page offered. Not while picking - a click
       there means "pick this one" and a menu over it would be a second
       meaning for the same card. */
    card.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      tellHost({ action: 'saved', do: 'menu', id: row.id || '',
                 title: row.title || '', status: row.status || '',
                 x: Math.round(e.clientX), y: Math.round(e.clientY) });
    });
  }
  // The status under the title and the number under that, in accent -
  // the only colour on a tracker card and the thing being looked for.
  const meta = card.querySelector('.m');
  if (meta) meta.textContent = row.status || '';
  // **Always the node, even with nothing in it yet.** progressInto
  // writes the number into `.cnum`, and a card that was drawn before
  // anything was marked had no `.cnum` to write into - so the first
  // mark on a title showed nothing until the page was rebuilt. `:empty`
  // in app.css takes it out of the layout, so an unmarked card looks
  // exactly as it did.
  card.appendChild(el('div', 'cnum', row.progress || ''));
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
    // A row whose cover fails asks for a better one, exactly as a grid
    // card does - see askForCover for the blank rows this is for.
    askForCover(art, row, 60);
    line.appendChild(art);

    const body = el('div', 'histbody');
    body.appendChild(el('div', 'histname', row.title || ''));
    // Always drawn, `:empty` when there is nothing to say - see
    // statusCard for why the node has to exist before the first mark.
    body.appendChild(el('div', 'histmeta', row.meta || ''));
    line.appendChild(body);

    const right = el('div', 'histwhen');
    right.appendChild(el('div', null, row.when || ''));
    right.appendChild(el('div', 'histtag' + (row.saved ? ' in' : ''),
                         row.saved ? 'In Saved' : 'Not saved'));
    line.appendChild(right);

    line.addEventListener('click', function () {
      tellHost({ action: 'open', kind: 'title', id: row.id || '',
                 title: row.title || '', type: row.type || '',
                 url: row.url || '', imdb: row.imdb || '',
                 poster: row.art || row.cover || '' });
    });
    /* **Right-click forgets this one title.** The owner, 4 September
       2026: "make when I right click on any item in the history show a
       Remove from History button and make it in red, and make it remove
       it from history immediately!"

       Drawn here rather than handed to Qt the way a shelf card's menu
       is (app.js shelfCard -> web_pages._open_menu): a shelf menu opens
       an Edit dialog and needs storage's rules, and this is one item
       with one action. The row goes the moment it is pressed - the
       write is the host's and the page does not wait for it, which is
       the same answer Clear History got on 3 September ("it immediately
       clears not when I change pages"). */
    line.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      histMenu(e, row, line, list, parent);
    });
    list.appendChild(markProgress(line, row));
  });
  parent.appendChild(list);
}

/* The one-item menu a history row opens, and the only floating menu in
   this page - so it is built and torn down here rather than becoming a
   framework. Closed by anything: a press elsewhere, a scroll, Escape,
   or the route changing under it. */
function histMenu(e, row, line, list, parent) {
  closeHistMenu();
  const menu = el('div', 'histmenu');
  const go = el('button', 'histforget', 'Remove from History');
  menu.appendChild(go);
  document.body.appendChild(menu);
  // Placed off the pointer and pulled back inside the window - a row
  // near the bottom or the right edge would otherwise open a menu that
  // is half off screen.
  const box = menu.getBoundingClientRect();
  const x = Math.min(e.clientX, window.innerWidth - box.width - 8);
  const y = Math.min(e.clientY, window.innerHeight - box.height - 8);
  menu.style.left = Math.max(8, x) + 'px';
  menu.style.top = Math.max(8, y) + 'px';
  /* **On pointerdown, not click** - and the dismisser below has to let
     this one through. Measured on the frozen build, 4 September 2026:
     the first version closed the menu from a document `pointerdown` and
     put the action on the button's `click`, so pressing the button
     removed the button between down and up and no click ever fired -
     42 history rows before the press and 42 after. */
  go.addEventListener('pointerdown', function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    closeHistMenu();
    // Gone now, written after: history.forget is a file read, a filter
    // and a write, and he asked for the row to go immediately.
    line.remove();
    if (!list.querySelector('.histrow')) {
      parent.appendChild(el('div', 'empty', 'Nothing here yet.'));
    }
    tellHost({ action: 'history', do: 'forget', key: row.id || '',
               title: row.title || '' });
  });
  window._histMenu = menu;
  // Not `{once: true}`: a press *inside* the menu would consume the
  // listener without closing anything, and the next press outside would
  // then leave the menu up.
  const away = function (ev) {
    if (ev && ev.target && menu.contains(ev.target)) return;
    closeHistMenu();
  };
  window._histAway = away;
  setTimeout(function () {
    document.addEventListener('pointerdown', away);
    document.addEventListener('scroll', away, true);
  }, 0);
}

function closeHistMenu() {
  if (window._histAway) {
    document.removeEventListener('pointerdown', window._histAway);
    document.removeEventListener('scroll', window._histAway, true);
    window._histAway = null;
  }
  if (window._histMenu) {
    window._histMenu.remove();
    window._histMenu = null;
  }
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
      askForCover(art, row, 56);
      line.appendChild(art);
      const body = el('div', 'schedbody');
      const top = el('div', 'schedname');
      top.appendChild(el('span', null, row.title || ''));
      if (row.saved) top.appendChild(el('span', 'savedtag', 'Saved'));
      body.appendChild(top);
      body.appendChild(el('div', 'schednum', row.progress || ''));
      line.appendChild(body);
      if (row.slot || row.countdown) {
        const when = el('div', 'schedwhen');
        when.appendChild(el('div', null, row.slot || ''));
        when.appendChild(el('div', 'schedleft', row.countdown || ''));
        line.appendChild(when);
      }
      line.addEventListener('click', function () {
        // The picture and the id travel with the click here too - see
        // cardFor, and the details page that opened on flat black with
        // "no matched title" when they did not.
        tellHost({ action: 'open', kind: 'title', id: row.id || '',
                   title: row.title || '', type: row.type || '',
                   url: row.url || '',
                   poster: row.art || row.cover || '', imdb: row.imdb || '' });
      });
      wrap.appendChild(markProgress(line, row));
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
/* **Saved's own select mode.** The owner, 4 September 2026: "add a
   select button in saved to select multi then delete!". Separate from
   shelfState because the two pages share nothing else - a shelf card is
   an icon with a name, a Saved card is a poster with a status - and one
   object for both meant leaving a page mid-selection carried the picks
   onto the other. Both are cleared by go(). */
let savedState = { selecting: false, picked: new Set() };
let shelfState = { sort: 'Custom Order', selecting: false, picked: new Set() };

function shelfCard(row, shelf) {
  const card = el('div', 'sc' + (row.shape === 'square' ? ' square' : ''));
  // Its id on the node, so Select All can repaint the marks without
  // redrawing the grid - see the tick's handler in shelfInto.
  card.dataset.pid = row.id || '';
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
    const picked = shelfState.picked.has(row.id);
    const mark = el('div', 'spick' + (picked ? ' on' : ''));
    if (picked) card.classList.add('picked');
    card.appendChild(mark);
    /* **The marks change, the page does not.** The owner, 4 September
       2026, about Saved and "any page that has selection": `go()`
       rebuilds the page element and the scroll goes with it, so every
       tick threw him back to the top of the grid. A pick is two classes
       and the bar's three numbers. */
    card.addEventListener('click', function () {
      const on = shelfState.picked.has(row.id);
      if (on) shelfState.picked.delete(row.id);
      else shelfState.picked.add(row.id);
      card.classList.toggle('picked', !on);
      mark.classList.toggle('on', !on);
      if (page && page._shelfSync) page._shelfSync();
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
    /* **Drag to reorder, in any sort - the drop switches to Custom
       Order.** The owner, 4 September 2026: "in the games, apps and webs
       pages allow dragging the cards to sort them, and make the sort
       type auto change to custom order like in the Qt before!!"

       It was removed earlier the same day at his word and is back on
       his word, with the one change he asked for. The old version was
       `draggable` *only* while the sort already said Custom Order -
       games._begin_custom_order's rule, because a drop from a
       re-sorted grid would write the order that happens to be on
       screen. Switching the sort as part of the drop answers that
       properly: the order on screen becomes the order in the file,
       which is what Custom Order means, and it is what the Qt page did.

       The Qt side's `reorder` handler was deleted with the first
       change; it is restored with this one (web_pages._shelf_action). */
    card.draggable = true;
    card.addEventListener('dragstart', function (e) {
      e.dataTransfer.setData('text/plain', row.id);
      e.dataTransfer.effectAllowed = 'move';
      card.classList.add('dragging');
    });
    card.addEventListener('dragend', function () {
      card.classList.remove('dragging');
    });
    card.addEventListener('dragover', function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      card.classList.add('dropinto');
    });
    card.addEventListener('dragleave', function () {
      card.classList.remove('dropinto');
    });
    card.addEventListener('drop', function (e) {
      e.preventDefault();
      card.classList.remove('dropinto');
      const moved = e.dataTransfer.getData('text/plain');
      if (!moved || moved === row.id) return;
      // The order on screen is about to become the file's, so the sort
      // has to say so before the redraw reads it.
      shelfState.sort = 'Custom Order';
      tellHost({ action: 'reorder', shelf: shelf,
                 moved: moved, target: row.id });
    });
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
    const count = el('span', 'selcount',
                     n + ' ' + (n === 1 ? noun[0] : noun[1]));
    bar.appendChild(count);
    const all = el('label', 'selall');
    const tick = el('input');
    tick.type = 'checkbox';
    tick.checked = n > 0 && n === rows.length;
    const sync = function () {
      const size = shelfState.picked.size;
      count.textContent = size + ' ' + (size === 1 ? noun[0] : noun[1]);
      bin.disabled = size === 0;
      tick.checked = size > 0 && size === rows.length;
    };
    parent._shelfSync = sync;
    tick.addEventListener('change', function () {
      shelfState.picked = tick.checked
        ? new Set(rows.map(function (r) { return r.id; })) : new Set();
      parent.querySelectorAll('.sc').forEach(function (card) {
        const on = shelfState.picked.has(card.dataset.pid);
        card.classList.toggle('picked', on);
        const box = card.querySelector('.spick');
        if (box) box.classList.toggle('on', on);
      });
      sync();
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
    row.dataset.dlid = String(job.id || '');
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
    // What downloadsPatch compares against, so a state change that adds
    // or removes a button is noticed without reading the DOM back.
    acts.dataset.acts = [job.can_pause ? 'Pause' : '',
                         job.can_resume ? 'Resume' : '',
                         job.active ? 'Cancel' : ''].filter(Boolean).join(',');
    row.appendChild(acts);
    list.appendChild(row);
  });
  if (!(data.rows || []).length) {
    list.appendChild(el('div', 'empty', 'Nothing downloading.'));
  }
  panel.appendChild(list);
  parent.appendChild(panel);
}

/* Write a fresh answer into the rows already on screen. Returns false
   when the set of jobs has changed, which is the one case that needs the
   page built again. */
function downloadsPatch(data) {
  const list = page.querySelector('.dllist');
  if (!list) return false;
  const rows = Array.prototype.slice.call(list.querySelectorAll('.dlrow'));
  const jobs = data.rows || [];
  if (rows.length !== jobs.length) { go('downloads'); return false; }
  for (let i = 0; i < jobs.length; i += 1) {
    const job = jobs[i], row = rows[i];
    if (row.dataset.dlid !== String(job.id || '')) { go('downloads'); return false; }
    const title = row.querySelector('.dltitle');
    if (title && title.textContent !== (job.title || '')) {
      title.textContent = job.title || '';
    }
    const detail = row.querySelector('.dldetail');
    const words = job.state_text + (job.detail ? '  ·  ' + job.detail : '');
    if (detail && detail.textContent !== words) detail.textContent = words;
    // The bar exists only while a job is running, so its arrival or
    // departure is a change of shape rather than of text.
    const fill = row.querySelector('.dlfill');
    if (!!fill !== !!job.active) { go('downloads'); return false; }
    if (fill) {
      const want = Math.max(0, Math.min(100, job.percent)) + '%';
      if (fill.style.width !== want) fill.style.width = want;
    }
    // Which buttons a state offers is part of that state.
    const acts = row.querySelector('.dlacts');
    const wanted = [job.can_pause ? 'Pause' : '', job.can_resume ? 'Resume' : '',
                    job.active ? 'Cancel' : ''].filter(Boolean).join(',');
    if (acts && acts.dataset.acts !== wanted) { go('downloads'); return false; }
  }
  const folder = page.querySelector('.dlpath');
  if (folder && folder.textContent !== (data.folder || '')) {
    folder.textContent = data.folder || '';
  }
  return true;
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
  askForCover(img, row);
  card.appendChild(img);
  card.appendChild(el('div', 't', row.title || ''));
  card.appendChild(el('div', 'm' + (row.saved ? ' s' : ''), row.meta || ''));
  card.addEventListener('click', function () {
    tellHost({ action: 'open', kind: row.kind || 'title', id: row.id || '',
               title: row.title || '', type: row.type || '',
               url: row.url || '', poster: row.art || row.cover || '',
               imdb: row.imdb || '' });
  });
  markProgress(card, row);
  // Only a catalogue card carries the saved colour - progressInto reads
  // this to know it may touch the class.
  card.dataset.pgrid = '1';
  // What applySort orders by: the source's own position (so "Most
  // Watched" can be put back exactly), the year and the rating. Numbers
  // the server already had - reading them off the meta line instead
  // would be parsing a display string.
  card.dataset.pord = String(gridOrder++);
  if (row.year) card.dataset.pyear = String(row.year);
  if (row.rating) card.dataset.prating = String(row.rating);
  return card;
}

/* The position a card was handed to the page in, counted across the
   whole session: a batch appended by a scroll or by pullGenre has to
   sort *after* what is already there when the order is the source's
   own, and a per-render counter would restart and interleave them. */
let gridOrder = 1;


/* Rewrite every card's number from /api/progress, in place.

   Cheap enough to run on every mark: the answer is one dictionary of
   short strings (one entry per marked title, 28 of them on his data)
   and the walk is over the cards actually in the document. It touches
   only `.m`, `.cnum`, `.histmeta` and `.schednum` - the four places a
   number is drawn - and only when the text it would write differs, so
   a page whose numbers are already right is not re-laid out at all.

   Fired by the window when history.json moves (hostMessage `marks`,
   which is web_pages._check_covered's own 150ms stat). Standalone, with
   no window to tell it, it is fired on a slow timer instead so the page
   can still be compared side by side in a browser. */
/* **Every route says how long it took, and every later append says
   when it landed.** Written 3 September 2026 while chasing the owner's
   "in manhwa and movies pages the cards transition is a bit delayed":
   three different screen metrics were tried and all three were
   dominated by when the network answered rather than by anything the
   page did - a control run of the *same* build put Manhwa's settle at
   2.59s and then 3.58s. A screen measurement cannot separate those; a
   line saying "manhwa drawn in 41ms, batch of 6 at 2956ms" can, and it
   keeps saying it on his machine rather than only on a test rig.

   Two lines per route at most, so the log stays readable: the render,
   and each append with its size and its distance from the render. */
function sayRender(route, ms, rows) {
  tellHost({ action: 'diag', what: 'route drawn', route: route,
             ms: Math.round(ms), rows: rows });
}

function sayBatch(route, ms, rows, from) {
  tellHost({ action: 'diag', what: 'batch appended', route: route,
             ms: Math.round(ms), rows: rows, from: from });
}

let progressAt = -1;

function progressInto() {
  fetch('/api/progress').then(function (r) { return r.json(); })
    .then(function (live) {
      const stamp = (live.stamp || []).join(',');
      if (stamp === progressAt) return;
      progressAt = stamp;
      const savedWatch = new Set(live.saved || []);
      const savedRead = new Set(live.saved_read || []);
      const cards = page.querySelectorAll('[data-ptitle]');
      cards.forEach(function (card) {
        const id = card.dataset.pid || '';
        const title = card.dataset.ptitle || '';
        // **The card's own side decides which table it reads.** A key
        // can name two works - "one piece" is a manga he reads and an
        // anime he does not - so the answer carries the episode form
        // and the chapter form separately (server._progress_now) and
        // the card takes the one its type asks for. Reading the wrong
        // one is how a Discover anime card came to wear "Ch 1191".
        // **The saved colour, off the same answer.** A catalogue card
        // writes its meta line in accent when the title is already in
        // the library (server._grid_row's `saved`), and that is the one
        // mark on the card saying "you have this" - so it moves here
        // too rather than waiting for the page to be rebuilt.
        const reading = READING_KINDS.indexOf(
          (card.dataset.ptype || '').toLowerCase()) >= 0;
        const meta = card.querySelector('.m');
        if (meta && card.dataset.pgrid) {
          // Its own side of the split, for the reason in
          // server._saved_titles: the manga One Piece was putting the
          // accent on the anime's card.
          const has = (reading ? savedRead : savedWatch).has(title);
          if (meta.classList.contains('s') !== has) meta.classList.toggle('s', has);
        }
        const marks = (reading ? live.read : live.marks) || {};
        let now = '';
        if (id && Object.prototype.hasOwnProperty.call(marks, id)) now = marks[id];
        else if (title && Object.prototype.hasOwnProperty.call(marks, title)) now = marks[title];
        else return;                 // nothing marked for this one
        const base = card.dataset.pbase || '';
        const sep = card.dataset.psep || '  ';
        // A status card keeps the number on its own line (.cnum) and
        // its .m is the status - so the two are written separately and
        // never into each other.
        const num = card.querySelector('.cnum, .schednum');
        if (num) {
          if (num.textContent !== now) num.textContent = now;
          return;
        }
        const line = card.querySelector('.histmeta') || card.querySelector('.m');
        if (!line) return;
        const text = [now, base].filter(Boolean).join(sep);
        if (line.textContent !== text) line.textContent = text;
      });
    })
    .catch(function () { /* the numbers on screen stay */ });
}


/* ---- the filter, back on the Watch and Read pages ------------------
   **The owner, 3 September 2026: "re-add the filter button and
   functionality to the watch and reading pages".**

   windows/tracker.py's own shape, in its own words: a button opening a
   choice of *All* plus the ticks that make sense for the page, beside a
   text field that narrows by name. Its ticks were type and status,
   because the Qt page listed the library; these pages list a catalogue,
   where the two facts a card carries are its name and whether it is
   already in the library - so those are the ticks, and Saved is the one
   that reads off the same accent the card already wears.

   The Saved page keeps the statuses, because there it is the library
   being listed and status is what its sections are grouped by.

   Filtering is done here, over the cards that are already drawn, and
   re-applied to every batch the scroll brings in - never as a request.
   A filter that went to the server would re-run a site sweep for a
   narrowing the page can do in a millisecond, and would lose the depth
   already scrolled in. */
let pageFilter = {open: false, text: '', pick: 'all', genres: [], sort: ''};

/* **How a catalogue may be ordered.** The owner, 4 September 2026: "in
   the watch, and read pages, add a list button on the right that change
   the sort, like last released, most watched, highest rate and so on
   and make the sort changes accordingly!"

   The empty key is the source's own order and stays the default: for a
   video medium that *is* "most watched" (Cinemeta's `top` catalog, which
   is what the page heading already calls it), and for a reading medium
   it is the site sweep's own ranking. Sorting happens over the cards in
   the document rather than by asking again, so it costs no request and
   applies to everything scrolled in as well.

   A row with nothing to sort on keeps its place behind the ones that
   have: the reading catalogues carry no year and no rating, so those two
   orders quietly do nothing there rather than shuffling the page into
   an arbitrary shape. */
const SORTS = [
  {key: '', label: 'Most Watched'},
  {key: 'released', label: 'Last Released'},
  {key: 'rating', label: 'Highest Rated'},
  {key: 'title', label: 'Name (A-Z)'},
];

function resetFilter() {
  pageFilter = {open: false, text: '', pick: 'all', genres: [], sort: ''};
}

function applyFilter(host) {
  const text = (pageFilter.text || '').trim().toLowerCase();
  const pick = pageFilter.pick || 'all';
  let shown = 0, total = 0;
  (host || page).querySelectorAll('[data-ptitle]').forEach(function (card) {
    total += 1;
    const name = card.dataset.ptitle || '';
    let ok = !text || name.indexOf(text) >= 0;
    /* **Any of the ticked genres, not all of them.** The Qt filter's
       groups worked this way too (tracker._toggle_filter builds a set
       and a row passes when its value is in it): ticking Romance and
       Comedy asks for either, which is what a reader means by it.
       Ticking none asks for everything. */
    if (ok && pageFilter.genres.length) {
      const mine = (card.dataset.pgenres || '').split('');
      ok = pageFilter.genres.some(function (g) { return mine.indexOf(g) >= 0; });
    }
    if (ok && pick !== 'all') {
      const meta = card.querySelector('.m');
      const saved = !!(meta && meta.classList.contains('s'));
      const status = (card.dataset.pstatus || '').toLowerCase();
      if (pick === 'saved') ok = saved;
      else if (pick === 'unsaved') ok = !saved;
      else ok = status === pick;
    }
    card.hidden = !ok;
    if (ok) shown += 1;
  });
  // Saved's Select All box means "everything showing", so what is
  // showing has just changed under it - see savedSelectInto.
  const settle = (host || page)._savedTick;
  if (settle) settle();
  // A section whose every row is hidden goes with them, or the page is
  // a column of headings over nothing.
  (host || page).querySelectorAll('.row, .schedblock').forEach(function (block) {
    const rows = block.querySelectorAll('[data-ptitle]');
    block.hidden = rows.length > 0 &&
      Array.prototype.every.call(rows, function (c) { return c.hidden; });
  });
  const count = (host || page).querySelector('.fcount');
  if (count) {
    const narrowed = text || pick !== 'all' || pageFilter.genres.length;
    // **And whether more is still on its way.** A ticked genre the page
    // is short of goes and asks for more (pullGenre), which takes
    // seconds - and a count that reads "1 of 60" for eight of them
    // while the answer is in flight is the owner's report of 4
    // September 2026 about Romance on the Anime page.
    const pulling = count.dataset.pulling || '';
    count.textContent = narrowed
      ? shown + ' of ' + total + (pulling ? '  ·  ' + pulling : '')
      : (pulling || '');
  }
}

/* Order the cards already in the grid. Stable, and by the card's own
   dataset - the numbers the server put there (server._grid_row) rather
   than by re-parsing the meta line, which is a display string. */
function applySort(host) {
  const grid = (host || page).querySelector('.grid');
  if (!grid) return;
  const key = pageFilter.sort || '';
  const cards = Array.prototype.slice.call(grid.children);
  if (!cards.length) return;
  if (!key) {
    // Back to the order the source gave, which every card remembers.
    cards.sort(function (a, b) {
      return (+a.dataset.pord || 0) - (+b.dataset.pord || 0);
    });
  } else if (key === 'title') {
    cards.sort(function (a, b) {
      return (a.dataset.ptitle || '').localeCompare(b.dataset.ptitle || '');
    });
  } else {
    const field = key === 'released' ? 'pyear' : 'prating';
    cards.sort(function (a, b) {
      const x = +a.dataset[field] || 0, y = +b.dataset[field] || 0;
      // Rows with no number at all sit behind the ones that have one,
      // in their original order, rather than being flung to the top.
      if (!x && !y) return (+a.dataset.pord || 0) - (+b.dataset.pord || 0);
      if (!x) return 1;
      if (!y) return -1;
      return y - x || (+a.dataset.pord || 0) - (+b.dataset.pord || 0);
    });
  }
  const frag = document.createDocumentFragment();
  cards.forEach(function (c) { frag.appendChild(c); });
  grid.appendChild(frag);
}

/* **Select, and delete what is picked** - the owner, 4 September 2026:
   "add a select button in saved to select multi then delete!".

   The shelves have had this since they were written (shelfInto), and it
   is deliberately the same thing here: Select on the right of the row
   the Filter button is on, a bar under it with the count, Select All and
   the bin, and a tick in the corner of every picked card. What differs
   is only where the rows live - a shelf has one flat grid, Saved is
   grouped by status and medium, so the ids are gathered across sections.

   The bar goes *after* the filter row and before the first section, so
   turning selection on does not move the grid sideways or reflow it. */
function savedSelectInto(parent, data) {
  const bar = parent.querySelector('.filterrow');
  if (!bar) return;
  /* **What Select All means is what is on screen.** The owner, 4
     September 2026: "when I put a filter on Watching in the saved page
     and check the select all box make it only select all appearing on
     the filter (Watching)."

     Read from the DOM at the moment the box is ticked, not from the
     payload: the filter hides cards with `hidden` (applyFilter) and the
     payload knows nothing about it, so ticking the box picked every
     saved title including the ones the filter had just taken off the
     page. The cards are drawn after this runs, which is why it is a
     function rather than a list. */
  const onScreen = function () {
    const out = [];
    parent.querySelectorAll('[data-pid]:not([hidden])').forEach(function (card) {
      if (card.dataset.pid) out.push(card.dataset.pid);
    });
    return out;
  };
  const pick = el('button', 'selbtn' + (savedState.selecting ? ' on' : ''),
                  savedState.selecting ? 'Done' : 'Select');
  pick.title = 'Pick several titles to remove';
  pick.addEventListener('click', function () {
    savedState.selecting = !savedState.selecting;
    savedState.picked.clear();
    go(currentRoute());
  });
  bar.appendChild(pick);
  if (!savedState.selecting) return;

  const strip = el('div', 'selbar');
  const n = savedState.picked.size;
  const count = el('span', 'selcount',
                   n + ' ' + (n === 1 ? 'title' : 'titles'));
  strip.appendChild(count);
  const all = el('label', 'selall');
  const tick = el('input');
  tick.type = 'checkbox';
  /* Ticked only when everything *showing* is picked - the same rule the
     box acts on, so it cannot say "all" about a page it did not fill.
     Read after this turn: the sections are drawn after this bar is, and
     applyFilter sets `hidden` after that again, so asking now would ask
     an empty page. */
  const settle = function () {
    const showing = onScreen();
    const size = savedState.picked.size;
    count.textContent = size + ' ' + (size === 1 ? 'title' : 'titles');
    bin.disabled = size === 0;
    tick.checked = showing.length > 0
      && showing.every(function (id) { return savedState.picked.has(id); });
  };
  parent._savedTick = settle;      // applyFilter calls it when the page changes
  parent._savedSync = settle;      // and every pick, which redraws nothing
  setTimeout(settle, 0);
  tick.addEventListener('change', function () {
    savedState.picked = tick.checked ? new Set(onScreen()) : new Set();
    // Same reason as a single pick: repaint the marks, keep the scroll.
    parent.querySelectorAll('[data-pid]').forEach(function (card) {
      const on = savedState.picked.has(card.dataset.pid);
      card.classList.toggle('picked', on);
      const box = card.querySelector('.spick');
      if (box) box.classList.toggle('on', on);
    });
    settle();
  });
  all.appendChild(tick);
  all.appendChild(el('span', null, 'Select All'));
  strip.appendChild(all);
  const bin = el('button', 'binbtn', '');            // Delete
  bin.title = 'Remove the picked titles from Saved';
  bin.disabled = n === 0;
  bin.addEventListener('click', function () {
    // Qt owns the question and the write - it is several entries out of
    // a file, which is storage's rules, not the page's.
    tellHost({ action: 'saved', do: 'delete', tab: data.tab || 'watch',
               ids: Array.from(savedState.picked) });
    /* **And selection ends here, not when the answer comes back.**
       The host asks its question and then redraws this page; measured on
       the frozen build, the redraw landed while `selecting` was still on
       and drew a bar counting two titles that were already gone. Leaving
       select mode as the request goes is both honest - there is nothing
       picked any more - and the state the page comes back in whether the
       question is answered yes or no. */
    savedState.selecting = false;
    savedState.picked.clear();
  });
  strip.appendChild(bin);
  parent.appendChild(strip);
}

function filterInto(parent, data, picks) {
  const bar = el('div', 'filterrow');
  const button = el('button', 'pill2' + (pageFilter.open ? ' on' : ''),
                    '☷  Filter');
  button.title = 'Filter this page';
  const box = el('div', 'filterrow');
  box.style.display = pageFilter.open ? 'flex' : 'none';
  const field = el('input');
  field.type = 'search';
  field.placeholder = 'Filter by name';
  field.value = pageFilter.text || '';
  field.addEventListener('input', function () {
    pageFilter.text = field.value;
    applyFilter(parent);
  });
  box.appendChild(field);
  picks.forEach(function (p) {
    const pill = el('button', 'pill2' + (p.key === pageFilter.pick ? ' on' : ''),
                    p.label);
    pill.addEventListener('click', function () {
      pageFilter.pick = p.key;
      box.querySelectorAll('.pill2').forEach(function (n) {
        n.classList.toggle('on', n === pill);
      });
      applyFilter(parent);
    });
    box.appendChild(pill);
  });
  box.appendChild(el('span', 'fcount', ''));
  /* **Sort and Clear All live on the always-visible row, and Sort is
     the one on the end.** The owner, 4 September 2026: "make the sort
     list in watch/read pages appear even when I did not press the
     filter button, and exchange its place with the Clear All button!"

     They were inside `box`, which is the panel the Filter button opens -
     so changing the order of a page meant opening a filter first, and
     the sort is not a filter. `bar` is the row the Filter button itself
     sits on and is never hidden. Clear All keeps its place beside the
     button whose ticks it clears; Sort goes to the right-hand end. */
  if (data.kind === 'grid') {
    const sortWrap = el('label', 'sortpick');
    sortWrap.appendChild(el('span', null, 'Sort:'));
    const sel = el('select');
    SORTS.forEach(function (s) {
      const opt = el('option', null, s.label);
      opt.value = s.key;
      if (s.key === (pageFilter.sort || '')) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', function () {
      pageFilter.sort = sel.value;
      applySort(parent);
      applyFilter(parent);
    });
    sortWrap.appendChild(sel);
    parent._sortWrap = sortWrap;
  }
  /* **And a way out of every tick at once.** The owner, 4 September
     2026: "add a button Clear All to clear (uncheck) all filters
     checked." Clears the ticks, the name box and the Saved/Not-saved
     pill together - "all filters checked" is all of them, and leaving
     one of the three behind is the surprise this exists to avoid.

     **Named in full, shown with the ticks, and on the left**, all three
     the same day: "make the Clear All Filters button only appears when
     Filter is pressed like the checkboxes, and change its position to
     be on the left side not the right side!". So it lives in `box` -
     the panel the Filter button opens - at the head of it, while Sort
     stays on the always-visible row, because a sort is not a filter. */
  const clear = el('button', 'pill2 clearall', 'Clear All Filters');
  clear.title = 'Clear every filter on this page';
  clear.addEventListener('click', function () {
    pageFilter.text = '';
    pageFilter.pick = 'all';
    pageFilter.genres = [];
    field.value = '';
    box.querySelectorAll('.pill2').forEach(function (n) {
      n.classList.toggle('on', n.textContent === 'All');
    });
    const gbox = parent._genreBox;
    if (gbox) {
      gbox.querySelectorAll('input').forEach(function (t) { t.checked = false; });
      gbox.querySelectorAll('.gpick').forEach(function (t) {
        t.classList.remove('on');
      });
    }
    applyFilter(parent);
  });
  box.insertBefore(clear, box.firstChild);
  if (parent._sortWrap) { bar.appendChild(parent._sortWrap); parent._sortWrap = null; }
  parent.appendChild(bar);
  parent.appendChild(box);

  /* The genres this page actually holds, as checkboxes. Built from the
     cards rather than from a fixed list, so a page only ever offers a
     tick that can match something - and rebuilt when a scroll batch
     brings a genre the page did not have (see refreshGenres). */
  const genreBox = el('div', 'genrebox');
  genreBox.style.display = pageFilter.open ? 'flex' : 'none';
  parent.appendChild(genreBox);
  parent._genreBox = genreBox;
  parent._filterData = data;
  refreshGenres(parent);

  button.addEventListener('click', function () {
    pageFilter.open = !pageFilter.open;
    button.classList.toggle('on', pageFilter.open);
    box.style.display = pageFilter.open ? 'flex' : 'none';
    genreBox.style.display = pageFilter.open ? 'flex' : 'none';
    if (pageFilter.open) field.focus();
  });
  // First on the row, whatever was appended to it above: the handler
  // needs `box` and `genreBox`, which are built after Clear All and
  // Sort, so the button is made early and put in its place here.
  bar.insertBefore(button, bar.firstChild);
}

function refreshGenres(host) {
  host = host || page;
  const box = host._genreBox;
  if (!box) return;
  const seen = {};
  host.querySelectorAll('[data-pgenres]').forEach(function (card) {
    (card.dataset.pgenres || '').split('').forEach(function (g) {
      if (g) seen[g] = (seen[g] || 0) + 1;
    });
  });
  /* **The medium's own genre list, not only what happens to be
     loaded.** The owner, 4 September 2026: "add to the watch and read
     pages genres in the filter, like add to anime filter: Romance and
     other genres." Built from the rows alone, Anime offered the six its
     thirty cached rows named and Romance was not one of them.
     `genrechoices` is the vocabulary the app browses by
     (server.WATCH_GENRES / READ_GENRES); the rows can still add a tag
     the list does not carry, which is how a reading page keeps Isekai. */
  const offered = ((host._filterData || {}).genrechoices || []);
  offered.forEach(function (g) { if (!(g in seen)) seen[g] = 0; });
  /* **A fixed order, so the ticks stop moving under the pointer.** The
     owner, 4 September 2026: "whenever while I am selecting Romance or
     any in filter and more cards load, the filters checkbox move
     around!!!". This sorted by how many cards carried each genre, and a
     ticked genre goes and fetches more of itself (pullGenre) - so every
     batch that landed re-ranked the row and the box he was aiming at
     was somewhere else. Popularity is not worth a moving target.

     The medium's own vocabulary first, in the order the server lists it
     (server.WATCH_GENRES / READ_GENRES), then anything the rows added
     that the list does not carry, alphabetically. Neither of those
     changes as cards arrive, so the `dataset.names` check below now
     really does mean "nothing new" and the boxes are not rebuilt at
     all - which is also what stops a half-clicked tick being replaced
     mid-click. */
  const extra = Object.keys(seen).filter(function (g) {
    return offered.indexOf(g) < 0;
  }).sort(function (a, b) { return a.localeCompare(b); });
  const names = offered.concat(extra).slice(0, offered.length ? 40 : 24);
  if (box.dataset.names === names.join('|')) return;   // nothing new
  box.dataset.names = names.join('|');
  box.innerHTML = '';
  names.forEach(function (name) {
    const tag = el('label', 'gpick' + (pageFilter.genres.indexOf(name) >= 0 ? ' on' : ''));
    const tick = el('input');
    tick.type = 'checkbox';
    tick.checked = pageFilter.genres.indexOf(name) >= 0;
    tick.addEventListener('change', function () {
      const at = pageFilter.genres.indexOf(name);
      if (tick.checked && at < 0) pageFilter.genres.push(name);
      else if (!tick.checked && at >= 0) pageFilter.genres.splice(at, 1);
      tag.classList.toggle('on', tick.checked);
      applyFilter(host);
      if (tick.checked) pullGenre(host, name);
    });
    tag.appendChild(tick);
    tag.appendChild(el('span', null, name));
    box.appendChild(tag);
  });
}


/* **A ticked genre the page has little of asks for it.** The tick list
   offers the whole vocabulary, so most of it matches nothing on a page
   holding thirty cached rows - and a tick that can only ever answer
   "nothing" is worse than no tick. The genre route is the same source
   the genre page uses, so this is that page's rows merged into this
   one, deduped by title, and the filter re-applied over the result.

   Once per genre per page, and only when the page is genuinely short of
   it: a catalogue scrolled deep already has plenty and does not need a
   request to prove it. */
/* How many rows of a ticked genre are worth showing before the walk
   stops, how many batches it may take to get there, and how long it may
   spend trying. A tick is a press he is watching, so all three are
   deliberately small - the point is that the count grows past "1" while
   he looks at it, not that the catalog is exhausted. Measured 4
   September 2026: one /api/genre page of Romance answers 14 anime out
   of 73 rows in 2.3s, and each further /api/more batch is 0.5-3s. */
const GENRE_WANT = 30;
const GENRE_PULL_BATCHES = 4;
const GENRE_PULL_BUDGET_MS = 14000;

function pullGenre(host, name) {
  const data = host._filterData || {};
  const grid = host.querySelector('.grid');
  if (!grid || !data.genrechoices) return;
  host._pulled = host._pulled || {};
  if (host._pulled[name]) return;

  function matching() {
    let n = 0;
    host.querySelectorAll('[data-pgenres]').forEach(function (c) {
      if ((c.dataset.pgenres || '').split('').indexOf(name) >= 0) n += 1;
    });
    return n;
  }
  if (matching() >= GENRE_WANT) return;
  host._pulled[name] = true;
  const stamp = token;
  /* **Asked of the document, every time, not of a set taken once.** The
     owner, 4 September 2026, with a picture of the Romance filter: Ouran
     High School Host Club, Blue Box, Hi Score Girl, Lovely Complex,
     Orange and Takagi-san each on screen **twice**.

     This walk ran against a snapshot of the titles that were on the page
     when the tick was pressed - and while it runs, the page's own
     scroll top-up (moreOnScroll) and the catalogue's live batch
     (liveBrowse) are appending as well. Neither knew about the other's
     rows, so the same title arrived down two paths and both let it in.
     Reading the grid at the moment of the append is the only answer
     that cannot go stale, and it costs one walk of the cards per batch
     against a request that took seconds. */
  const known = function () { return drawnTitles(host); };
  const medium = 'genre:' + name + ':' + (data.genrereading ? '1' : '0')
                 + ':' + (data.genrekind || 'all');

  /* **Say it is still looking.** The owner, 4 September 2026: "in the
     anime page, when I select Romance in the filter it does show only 1
     anime, while there are more as romance!". Photographed on the
     frozen build that day: two seconds after the tick the count read
     **"1 of 60"** with a single card under it, and ten seconds after it
     read "14 of 73" - so the answer was coming and nothing on screen
     said so. A count that is quietly wrong for eight seconds is the
     thing he reported, not the count it settles on. */
  const count = host.querySelector('.fcount');
  const say = function (text) { if (count) count.dataset.pulling = text; };
  say('looking for more ' + name + '...');
  applyFilter(host);

  function absorb(rows) {
    const seen = known();
    const fresh = (rows || []).filter(function (r) {
      const key = (r.title || '').trim().toLowerCase();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    if (fresh.length) {
      const frag = document.createDocumentFragment();
      fresh.forEach(function (r) { frag.appendChild(gridCard(r)); });
      grid.appendChild(frag);
      refreshGenres(host);
      applySort(host);
    }
    applyFilter(host);
    return fresh.length;
  }

  /* **And keep asking until there are enough of them.** One
     `/api/genre` page answered 14 Romance anime out of 73 rows, because
     Cinemeta files anime as *series with genre=Anime* and the wanted
     genre has to be applied to the rows afterwards
     (rules/integrations.md) - so a page of 50 holds a handful of any one
     genre. The route already hands back its own cursor and `/api/more`
     continues from it, which is what the genre page's own scroll uses;
     this is that walk, run until the tick has something worth showing.

     Bounded three ways, because this is a tick he just pressed: a
     target count, a batch cap, and a wall clock. A dry batch stops it
     for good - the catalog has run out and asking again cannot help. */
  /* **The clock starts when the walk does.** Measured on the frozen
     build, 4 September 2026: the first /api/genre for Romance is 2s
     warm and **10s cold**, and starting the budget at the tick meant a
     cold page had already spent it before the first continuation could
     run - so the count stopped at 14 exactly as it had before. There is
     nothing to protect against here anyway: rows are on screen and the
     count says it is still looking, which is what rule 7 asks for. */
  let until = 0;
  let batches = 0;
  function step(skip) {
    if (!until) until = Date.now() + GENRE_PULL_BUDGET_MS;
    fetch('/api/more?medium=' + encodeURIComponent(medium) +
          '&have=0&skip=' + skip)
      .then(function (r) { return r.json(); })
      .then(function (found) {
        if (stamp !== token || !grid.isConnected) return;
        const added = absorb(found.rows);
        batches += 1;
        const next = found.skip || 0;
        /* **A batch of nothing new is not the end.** Measured 4
           September 2026: consecutive Romance batches overlapped almost
           completely until the server's cursor was fixed, and even now
           a batch can be all duplicates while the next one is not. What
           says "there is no more" is the source answering *no rows at
           all*, or its cursor refusing to advance - both of which stop
           this loop below. */
        if ((found.rows || []).length && matching() < GENRE_WANT
            && batches < GENRE_PULL_BATCHES
            && Date.now() < until && next > skip) {
          say('looking for more ' + name + '...');
          step(next);
          return;
        }
        say('');
        applyFilter(host);
      })
      .catch(function () { say(''); applyFilter(host); });
  }

  fetch('/api/genre?name=' + encodeURIComponent(name) +
        '&reading=' + (data.genrereading ? '1' : '0') +
        '&tab=' + encodeURIComponent(data.genrekind || 'all'))
    .then(function (r) { return r.json(); })
    .then(function (found) {
      if (stamp !== token || !grid.isConnected) return;
      absorb(found.rows);
      if (matching() < GENRE_WANT && (found.skip || 0) > 0) {
        step(found.skip);
      } else {
        say('');
        applyFilter(host);
      }
    })
    .catch(function () { say(''); applyFilter(host); });
}



/* Every title already drawn on this page, read at the moment of the
   append. Three things append to one grid - the scroll top-up, the
   catalogue's live batch, and a ticked genre's own walk - and each used
   to keep a private set built when it started, so a title reaching two
   of them landed twice. That is the doubled Ouran / Blue Box / Hi Score
   Girl / Lovely Complex / Orange / Takagi-san the owner photographed on
   4 September 2026. */
function drawnTitles(host) {
  const out = new Set();
  (host || page).querySelectorAll('[data-ptitle]').forEach(function (c) {
    out.add(c.dataset.ptitle);
  });
  return out;
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

/* **A page opening is not a page being scrolled, and the top-up used to
   land in the middle of it.** The owner, 3 September 2026: "in manhwa
   and movies pages the cards transition is a bit delayed than the other
   watch and read pages, fix it."

   Measured that day on the build he tested, driving the frozen exe from
   outside and scoring a band of screen against where the run ends up
   (scratchpad/repro2.py): the first cards are inked at 0.26-0.31s on
   every one of the six medium rows, and then the picture goes on
   changing - Movies 0.30s, Series 0.26s, Anime 0.30s, Manga 0.30s,
   Manhua 0.57s and **Manhwa 3.56s**.

   The 3.5s is this function. The cached catalogue is 30-41 rows, which
   is shorter than the viewport plus MORE_MARGIN_PX, so `pull()` fired
   the instant the page was drawn - and one `/api/more` batch for a
   reading medium is a whole-sites sweep: measured on his own sites,
   Manhwa's first batch is **13.08s and 60 rows**, then 94 rows every
   2.5-4.8s; Movies' is 30 rows every 0.7-5.8s. So sixty cards were
   appended into a grid the eye was still arriving at, and the batch
   after it 60ms later.

   Nothing about the fetch is wrong - the depth is wanted, and rule 7
   says show what there is and fill the rest in. What was wrong is
   *when*: the fill has to start after the opening has landed, not
   inside it. So the first pull waits for the page to be quiet, and the
   re-arm after a batch is a fifth of a second rather than 60ms. A page
   the user is actually scrolling never sees either delay: `settling()`
   is false the moment the route has been on screen for SETTLE_MS.

   Quiet is measured, not assumed: the route's own age. A transition is
   180-300ms of animation plus the first paint, and a batch that lands
   after it is an append below the fold like any other. */
const SETTLE_MS = 450;
let routeAt = 0;

function settling() {
  return performance.now() - routeAt < SETTLE_MS;
}

// `stamp`, not `token`: the parameter used to be called `token` and so
// shadowed the module-level counter of that name, while the body tested
// `mine !== token` - and `mine` is a const local to go(). That is a
// ReferenceError on the first line of pull(), thrown before a single
// batch was ever asked for, which is why these pages loaded nothing more
// however far they were scrolled.
function moreOnScroll(data, host, grid, stamp) {
  if (!data.browse) return;
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
    if (settling()) { setTimeout(pull, SETTLE_MS); return; }
    busy = true;
    // `have` is what the page is already showing, counted now - the
    // reading branch of _more_browse widens its sweep by it, and a
    // count taken when the page opened goes stale the moment anything
    // else appends (see drawnTitles).
    fetch('/api/more?medium=' + encodeURIComponent(data.browse) +
          '&have=' + drawnTitles(host).size + '&skip=' + skip)
      .then(function (r) { return r.json(); })
      .then(function (batch) {
        if (stamp !== token) return;
        skip = batch.skip || skip;
        const seen = drawnTitles(host);
        const fresh = (batch.rows || []).filter(function (r) {
          const key = (r.title || '').trim().toLowerCase();
          if (!key || seen.has(key)) return false;
          seen.add(key);
          return true;
        });
        // A reading sweep answers before every title has a medium and
        // says how many are still being classified (server._more
        // `pending`); a batch with nothing new is only "dry" once that
        // count is zero, or a cold device's page would stop asking two
        // batches into a minute-long classification.
        if (!fresh.length) { if (!batch.pending) dry += 1; return; }
        dry = 0;
        const frag = document.createDocumentFragment();
        fresh.forEach(function (r) { frag.appendChild(gridCard(r)); });
        grid.appendChild(frag);
        sayBatch(currentRoute(), performance.now() - routeAt, fresh.length,
                 'more');
        refreshGenres(host);
        applySort(host);
        applyFilter(host);
      })
      .catch(function () { dry += 1; })
      .then(function () {
        busy = false;
        // The batch may not have filled the viewport - ask again rather
        // than waiting for a scroll that will not come. A fifth of a
        // second, not 60ms: a reading batch is 94 cards and appending
        // them sixteen times a second is a grid re-laid out under the
        // pointer for as long as the sweep keeps answering (see
        // SETTLE_MS above for the measurement).
        setTimeout(pull, 200);
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
      let fresh = (live.rows || []).filter(function (r) { return !!r.title; });
      if (!fresh.length) return;
      // Held until the opening has landed, for the reason written above
      // SETTLE_MS: Movies' sweep answers in about a second, which is
      // inside the transition, and thirty cards appended there is the
      // grid re-laying out while it is being looked at.
      const apply = function () {
        if (stamp !== token || !grid.isConnected) return;
        // Read here rather than above: this call is held for SETTLE_MS
        // and the scroll top-up can append inside that wait.
        const have = drawnTitles(page);
        fresh = fresh.filter(function (r) { return !have.has(key(r)); });
        if (!fresh.length) return;
        const frag = document.createDocumentFragment();
        fresh.forEach(function (r) { frag.appendChild(gridCard(r)); });
        grid.appendChild(frag);
        sayBatch(currentRoute(), performance.now() - routeAt, fresh.length,
                 'live');
        refreshGenres(page);
        applySort(page);
        applyFilter(page);
        const note = page.querySelector('header p');
        if (note) note.textContent = data.note;
        const empty = page.querySelector('.empty');
        if (empty) empty.remove();
      };
      if (settling()) setTimeout(apply, SETTLE_MS);
      else apply();
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

/* Embedded, the window says when the marks moved - it is already
   stat()ing history.json every 150ms to decide whether this view may
   paint (web_pages._check_covered), so there is nothing to poll.
   Standalone, there is no window, and the page is opened in a browser
   to be compared with the app side by side - so it asks for itself, at
   a rate that is a comparison aid rather than a mechanism. */
if (!EMBED) setInterval(progressInto, 1500);
