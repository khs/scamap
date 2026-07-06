/* header-bg.js — lazy, responsive header banner (shared by all pages).
 *
 * The banner art is heavy (the desktop PNG is ~1.7 MB), so instead of loading it
 * from CSS on the critical path we apply it here from a `defer` script: it runs
 * after the HTML is parsed (never blocking first paint), identifies mobile vs
 * desktop with matchMedia, preloads the right file OFF the critical path, and only
 * swaps it in once decoded. Mobile gets a ~37 KB downscaled JPEG; desktop gets the
 * full-res PNG. Until (or unless) the art arrives the header keeps its solid blue
 * background colour, so the parchment title is always legible.
 */
(function () {
  var header = document.querySelector('header');
  if (!header) return;
  var DESKTOP = 'newheaderbg_eventscout_blue.png';
  var MOBILE  = 'newheaderbg_eventscout_blue_mobile.jpg';
  var mq = window.matchMedia('(max-width: 700px)');
  var applied = '';
  function apply() {
    var src = mq.matches ? MOBILE : DESKTOP;
    if (src === applied) return;               // already showing the right one
    var img = new Image();
    img.onload = function () {
      header.style.backgroundImage = 'url("' + src + '")';
      applied = src;
    };
    img.src = src;                             // decode off the render path, then swap
  }
  apply();
  // Re-pick if the viewport crosses the breakpoint (rotate / resize).
  if (mq.addEventListener) mq.addEventListener('change', apply);
  else if (mq.addListener) mq.addListener(apply);   // older Safari
})();
