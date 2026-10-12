// Accessibility options in the right-hand end of the menu bar (nav).
// So far: a text-size slider and a "Force plain font" checkbox. Settings are
// remembered in this browser only (localStorage) and never sent anywhere.
// Included on every page that has the menu bar; runs before first paint so
// there's no flash of the small text.
(function () {
  var KEY = 'eventscout-a11y';
  var MIN = 100, MAX = 200, STEP = 10;
  var state = { size: 100, plain: false };

  try {
    var saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (saved.size >= MIN && saved.size <= MAX) state.size = saved.size;
    state.plain = !!saved.plain;
  } catch (e) { /* private mode etc.: fall back to the defaults */ }

  var root = document.documentElement;
  function apply() {
    // Every font size on the site is in rem, so scaling the root scales the lot.
    root.style.fontSize = state.size === 100 ? '' : state.size + '%';
    root.classList.toggle('a11y-plain-font', state.plain);
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { }
  }
  apply();

  var css = document.createElement('style');
  css.textContent =
    'html.a11y-plain-font body, html.a11y-plain-font body * { font-family: Verdana, Geneva, sans-serif !important; letter-spacing: normal !important; }' +
    '.a11y-btn { position: absolute; right: 0.6rem; top: 50%; transform: translateY(-50%); background: none; border: 0; padding: 2px 4px; cursor: pointer; opacity: 0.7; line-height: 0; }' +
    '.a11y-btn:hover, .a11y-btn[aria-expanded="true"] { opacity: 1; }' +
    '.a11y-btn:focus-visible { outline: 2px solid #fff; outline-offset: 2px; opacity: 1; }' +
    '.a11y-btn img { height: 1.35rem; width: auto; display: block; }' +
    '.a11y-panel { position: absolute; right: 0.6rem; top: 100%; z-index: 1100; background: #fff; color: #222; border: 1px solid rgba(0,0,0,0.25); border-radius: 4px; box-shadow: 0 4px 14px rgba(0,0,0,0.25); padding: 0.8rem 1rem; width: 14rem; font-size: 0.9rem; }' +
    '.a11y-panel[hidden] { display: none; }' +
    '.a11y-panel label { display: block; }' +
    '.a11y-panel input[type=range] { width: 100%; margin: 0.4rem 0 0.7rem; }' +
    '.a11y-panel .a11y-check { display: flex; gap: 0.5rem; align-items: center; }' +
    '@media (max-width: 700px) { nav ul { padding-right: 2.4rem; } }' +
    'html.embed .a11y-btn, html.embed .a11y-panel { display: none; }';
  document.head.appendChild(css);

  function build() {
    var nav = document.querySelector('nav');
    if (!nav) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'a11y-btn';
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-controls', 'a11y-panel');
    btn.setAttribute('aria-label', 'Text size options');
    btn.title = 'Text size options';
    btn.innerHTML = '<img src="accessibility_larger_text.png" alt="" />';

    var panel = document.createElement('div');
    panel.className = 'a11y-panel';
    panel.id = 'a11y-panel';
    panel.hidden = true;
    panel.innerHTML =
      '<label for="a11y-size">Text size: <span id="a11y-size-val"></span></label>' +
      '<input type="range" id="a11y-size" min="' + MIN + '" max="' + MAX + '" step="' + STEP + '" />' +
      '<label class="a11y-check"><input type="checkbox" id="a11y-plain" /> Force plain font</label>';

    nav.appendChild(btn);
    nav.appendChild(panel);

    var size = panel.querySelector('#a11y-size');
    var val = panel.querySelector('#a11y-size-val');
    var plain = panel.querySelector('#a11y-plain');
    size.value = state.size;
    plain.checked = state.plain;
    val.textContent = state.size + '%';

    size.addEventListener('input', function () {
      state.size = parseInt(size.value, 10);
      val.textContent = state.size + '%';
      apply(); save();
    });
    plain.addEventListener('change', function () {
      state.plain = plain.checked;
      apply(); save();
    });

    function setOpen(open) {
      panel.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    btn.addEventListener('click', function () { setOpen(panel.hidden); });
    document.addEventListener('click', function (e) {
      if (!panel.hidden && !panel.contains(e.target) && !btn.contains(e.target)) setOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) { setOpen(false); btn.focus(); }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
