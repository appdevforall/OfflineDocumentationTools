// toc_script.js

document.addEventListener("DOMContentLoaded", function(){
    // Find all li elements that are headings.
    var tocList = document.getElementById("toc-list");
    var entries = tocList.getElementsByTagName("li");

    // helper to convert HTMLCollection to array.
    var entriesArray = Array.prototype.slice.call(entries);

    // When a heading is clicked, toggle its expanded state.
    entriesArray.forEach(function(entry) {
        if (entry.classList.contains("heading")) {
            entry.addEventListener("click", function(e) {
                // Prevent click on inner <a> if any.
                if (e.target.tagName.toLowerCase() === "a") return;
                // Toggle expanded state.
                if (entry.classList.contains("expanded")) {
                    collapse(entry);
                } else {
                    expand(entry);
                }
                e.stopPropagation();
            });
        }
    });

    // Expand: show direct children (and leave them collapsed by default).
    function expand(entry) {
        entry.classList.add("expanded");
        var parentId = entry.getAttribute("data-id");
        var entryLevel = parseInt(entry.getAttribute("data-level"));
        // Show all li elements whose data-parent equals parentId.
        entriesArray.forEach(function(child) {
            if(child.getAttribute("data-parent") === parentId){
                child.classList.remove("hidden");
            }
        });
    }

    // Collapse: hide not only direct children but all descendants.
    function collapse(entry) {
        entry.classList.remove("expanded");
        var parentId = entry.getAttribute("data-id");
        var entryLevel = parseInt(entry.getAttribute("data-level"));
        entriesArray.forEach(function(child) {
            // If this child's ancestor (immediate parent chain) contains the current entry, hide it.
            // For simplicity, check if the child's data-parent equals the current entry id.
            if(child.getAttribute("data-parent") === parentId){
                hideRecursively(child);
            }
        });
    }

    function hideRecursively(entry) {
        entry.classList.add("hidden");
        // If an entry is a heading and expanded, remove expanded and hide its children.
        if(entry.classList.contains("heading") && entry.classList.contains("expanded")){
            entry.classList.remove("expanded");
        }
        var entryId = entry.getAttribute("data-id");
        entriesArray.forEach(function(child) {
            if(child.getAttribute("data-parent") === entryId){
                hideRecursively(child);
            }
        });
    }
});