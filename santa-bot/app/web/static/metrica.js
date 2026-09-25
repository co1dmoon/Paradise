// Yandex Metrica loader, included on the landing only when METRICA_ID is set.
// A separate file (not inline) so the Content-Security-Policy needs no 'unsafe-inline'.
(function () {
  "use strict";
  var counter = Number(document.currentScript && document.currentScript.getAttribute("data-counter"));
  if (!counter) {
    return;
  }
  window.ym = window.ym || function () {
    (window.ym.a = window.ym.a || []).push(arguments);
  };
  window.ym.l = Date.now();
  var tag = document.createElement("script");
  tag.async = true;
  tag.src = "https://mc.yandex.ru/metrika/tag.js";
  document.head.appendChild(tag);
  window.ym(counter, "init", { clickmap: true, trackLinks: true, accurateTrackBounce: true });
})();
