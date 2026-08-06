// Drives the sidebar templates/nav.peb renders: the mobile off-canvas drawer
// (hamburger button, backdrop, edge swipe-to-open / swipe-to-close) and the
// per-topic collapse/expand toggles (.nav-toggle-group, one per node with
// children). Also auto-expands and highlights whichever nav-link matches the
// current page on load, so a reader landing deep in the tree doesn't see an
// entirely collapsed sidebar with no indication of where they are.
(function () {
  var sidebar = document.getElementById("sidebar");
  if (!sidebar) return;

  var menuButton = document.getElementById("nav-toggle");
  var backdrop = document.getElementById("nav-backdrop");
  var MOBILE_QUERY = "(max-width: 900px)";

  function isMobile() {
    return window.matchMedia && window.matchMedia(MOBILE_QUERY).matches;
  }

  function openSidebar() {
    sidebar.classList.add("sidebar-open");
    if (backdrop) backdrop.classList.add("nav-backdrop--visible");
    if (menuButton) menuButton.setAttribute("aria-expanded", "true");
  }

  function closeSidebar() {
    sidebar.classList.remove("sidebar-open");
    if (backdrop) backdrop.classList.remove("nav-backdrop--visible");
    if (menuButton) menuButton.setAttribute("aria-expanded", "false");
  }

  function toggleSidebar() {
    if (sidebar.classList.contains("sidebar-open")) {
      closeSidebar();
    } else {
      openSidebar();
    }
  }

  if (menuButton) menuButton.addEventListener("click", toggleSidebar);
  if (backdrop) backdrop.addEventListener("click", closeSidebar);

  function toggleItem(item) {
    var toggle = item.querySelector(":scope > .nav-row > .nav-toggle-group");
    var expanded = item.classList.toggle("nav-expanded");
    if (toggle) toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  }

  // Per-topic collapse/expand: every node with children renders a sibling
  // .nav-toggle-group button next to its label/link (see nav.peb) so this
  // never has to guess which <li> a click landed in beyond closest(). A
  // group header with no page of its own (.nav-group-title, not a link) also
  // toggles on click, since clicking it can't navigate anywhere anyway.
  sidebar.addEventListener("click", function (event) {
    var toggle = event.target.closest(".nav-toggle-group");
    if (toggle) {
      toggleItem(toggle.closest(".nav-item"));
      return;
    }
    var groupTitle = event.target.closest(".nav-group-title");
    if (groupTitle) {
      toggleItem(groupTitle.closest(".nav-item"));
      return;
    }
    // Navigating on mobile should close the drawer instead of leaving it
    // covering the page the reader just opened.
    if (event.target.closest(".nav-link") && isMobile()) {
      closeSidebar();
    }
  });

  function expandAncestors(item) {
    while (item) {
      item.classList.add("nav-expanded");
      var toggle = item.querySelector(":scope > .nav-row > .nav-toggle-group");
      if (toggle) toggle.setAttribute("aria-expanded", "true");
      var parentList = item.parentElement;
      item = parentList ? parentList.closest(".nav-item") : null;
    }
  }

  // Scrolls only the sidebar's own internal scrollbox to reveal el, without
  // touching window scroll - the sidebar is position:fixed on mobile (and
  // translated off-canvas until opened), so a plain el.scrollIntoView()
  // would have the browser scroll the whole page trying to "reveal" an
  // element that fixed positioning makes it unable to actually bring into
  // view that way.
  function scrollWithinSidebar(el) {
    var elRect = el.getBoundingClientRect();
    var boxRect = sidebar.getBoundingClientRect();
    sidebar.scrollTop += elRect.top - boxRect.top - sidebar.clientHeight / 2;
  }

  function highlightCurrentPage() {
    var path = window.location.pathname.replace(/^\//, "").replace(/\.html$/, "");
    var current = sidebar.querySelector('.nav-link[data-nav-id="' + path + '"]');
    if (!current) return;
    current.classList.add("nav-link--active");
    current.setAttribute("aria-current", "page");
    current.closest(".nav-item").classList.add("nav-item--current");
    expandAncestors(current.closest(".nav-item"));
    scrollWithinSidebar(current);
  }

  // The static-site pipeline (RenderDocs.java) has page.peb include a
  // pre-rendered nav.html at request/build time, so the sidebar already has
  // its content by the time this script runs. The database-backed server
  // has no such file-based template include (see layout.pebble - templates
  // there are fully self-contained), so its page template instead renders
  // <aside id="sidebar" data-nav-src="..."></aside> empty and expects the
  // client to fetch and inject the nav markup itself. Handle both the same
  // way: run the content-dependent init once the markup actually exists.
  var navSrc = sidebar.getAttribute("data-nav-src");
  if (navSrc) {
    fetch(navSrc)
      .then(function (response) {
        return response.text();
      })
      .then(function (html) {
        sidebar.insertAdjacentHTML("beforeend", html);
        highlightCurrentPage();
      })
      .catch(function (err) {
        console.error("Failed to load navigation from " + navSrc + ":", err);
      });
  } else {
    highlightCurrentPage();
  }

  // Edge swipe-right opens the drawer, swipe-left (anywhere) closes it.
  var touchStartX = null;
  var touchStartY = null;
  var EDGE_ZONE = 24;
  var SWIPE_THRESHOLD = 60;

  document.addEventListener(
    "touchstart",
    function (event) {
      var t = event.touches[0];
      touchStartX = t.clientX;
      touchStartY = t.clientY;
    },
    { passive: true }
  );

  document.addEventListener(
    "touchend",
    function (event) {
      if (touchStartX === null || !isMobile()) {
        touchStartX = null;
        touchStartY = null;
        return;
      }
      var startX = touchStartX;
      var startY = touchStartY;
      touchStartX = null;
      touchStartY = null;

      var t = event.changedTouches[0];
      var dx = t.clientX - startX;
      var dy = t.clientY - startY;
      if (Math.abs(dx) < SWIPE_THRESHOLD || Math.abs(dx) < Math.abs(dy)) return;

      var isOpen = sidebar.classList.contains("sidebar-open");
      if (!isOpen && dx > 0 && startX <= EDGE_ZONE) {
        openSidebar();
      } else if (isOpen && dx < 0) {
        closeSidebar();
      }
    },
    { passive: true }
  );
})();
