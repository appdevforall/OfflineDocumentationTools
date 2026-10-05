// Makes the <div class="tabs">...</div> blocks page.peb renders (for
// md_to_json.py's "tabs" block type) switchable, including linking every
// tabs block that shares the same data-group on a page - e.g. picking
// "Groovy" in one Kotlin/Groovy/Maven build-script tabs block switches every
// other build-script tabs block on the page - and remembering the choice
// across pages via localStorage, mirroring Writerside's data-sync-tabs.
(function () {
  var STORAGE_PREFIX = "docs-tab-group:";

  function activate(container, groupKey) {
    var buttons = container.querySelectorAll(".tab-button");
    var panels = container.querySelectorAll(".tab-panel");
    var hasMatch = false;
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i].dataset.groupKey === groupKey) {
        hasMatch = true;
        break;
      }
    }
    if (!hasMatch) return;

    buttons.forEach(function (btn) {
      var active = btn.dataset.groupKey === groupKey;
      btn.classList.toggle("tab-button--active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    panels.forEach(function (panel) {
      var active = panel.dataset.groupKey === groupKey;
      panel.classList.toggle("tab-panel--active", active);
      panel.hidden = !active;
    });
  }

  function switchGroup(group, groupKey) {
    document.querySelectorAll('.tabs[data-group="' + group + '"]').forEach(function (container) {
      activate(container, groupKey);
    });
    try {
      localStorage.setItem(STORAGE_PREFIX + group, groupKey);
    } catch (e) {
      // localStorage unavailable (private browsing, etc.) - selection just won't persist.
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest(".tab-button");
    if (!button) return;
    var container = button.closest(".tabs");
    if (!container) return;
    var groupKey = button.dataset.groupKey;
    var group = container.dataset.group;
    if (group) {
      switchGroup(group, groupKey);
    } else {
      activate(container, groupKey);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".tabs[data-group]").forEach(function (container) {
      var group = container.dataset.group;
      var saved = null;
      try {
        saved = localStorage.getItem(STORAGE_PREFIX + group);
      } catch (e) {
        // ignore
      }
      if (saved) activate(container, saved);
    });
  });
})();
