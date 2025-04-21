#!/usr/bin/env python3
"""
This script reads a JSON file describing a table‐of‐contents
and writes three files:
 • an HTML file (“toc.html”) that contains a <div> (with a fixed width)
   that holds a multi–level list (<ul><li>…) of TOC entries;
 • a CSS file (“toc_style.css”) with layout rules and colors for the three entry types;
 • a JavaScript file (“toc_script.js”) that makes headings clickable and toggles
   expansion (including rotating an embedded SVG triangle icon).

Usage:
  python build_toc.py input.json toc.html toc_style.css toc_script.js

You can customize the following parameters:
  - TOC_WIDTH: the fixed width of the TOC container (in pixels)
  - ICON_COLOR: the fill color used for the SVG “expanded icon”
  - ICON_WIDTH: the width (in pixels) of the icon (height is equal to width)
"""

import sys, json, os
from bs4 import BeautifulSoup, Tag

# ----- PARAMETERS (customize as desired) -----
TOC_WIDTH = 300         # in pixels, for the container <div>
ICON_COLOR = "#007700"  # developer-specified color for the expanded icon (greenish)
ICON_WIDTH = 16         # in pixels (square)

# ----- HELPER FUNCTIONS -----

def is_external(url):
    """Return True if the URL string starts with http:// or https://"""
    if url.startswith("http://") or url.startswith("https://"):
        return True
    return False

def build_flat_list(entities, top_level_ids):
    """
    Given a dictionary of all TOC entities (each keyed by id) and a list of top-level IDs,
    return a flat list (in display order) of entries.

    Our strategy is to recursively process each top–level entry and then sort children
    by the integer value in the "tabIndex" field.
    """
    flat_list = []

    # Build a mapping from parent id to list of child ids.
    children_map = {}
    for eid, entry in entities.items():
        parent = entry.get("parentId")
        if parent:
            children_map.setdefault(parent, []).append(entry)

    # sort children lists by "tabIndex"
    for parent in children_map:
        children_map[parent].sort(key=lambda x: x.get("tabIndex", 0))

    def add_entry(entry):
        flat_list.append(entry)
        # If this entry is a heading (has a "pages" list or if it exists as parent in children_map),
        # then add its children (if any) in order.
        # (Even if it has a "url", an entry with children is used as a heading.)
        eid = entry["id"]
        if eid in children_map:
            for child in children_map[eid]:
                add_entry(child)

    # Process top-level IDs in the given order.
    for tid in top_level_ids:
        if tid in entities:
            add_entry(entities[tid])

    return flat_list

def entry_type(entry):
    """Return the string: 'heading' if the entry has children (or a pages list),
       'page' if it is a relative URL page,
       'external' if its URL is absolute.
    """
    # if an entry has a "pages" list or if it is found to be a parent (we assume cached during building the final flat list)
    if entry.get("pages") is not None:
        return "heading"
    if "url" in entry:
        if is_external(entry["url"]):
            return "external"
        return "page"
    # Fallback: if no url and no pages, treat it as heading.
    return "heading"

def build_svg_icon(soup):
    """
    Returns a new BeautifulSoup Tag that holds the SVG code.
    We build a simple filled equilateral triangle fitting in a square
    of ICON_WIDTH x ICON_WIDTH. (The triangle points down by default.)
    """
    svg = soup.new_tag("svg",
                       xmlns="http://www.w3.org/2000/svg",
                       width=str(ICON_WIDTH), height=str(ICON_WIDTH),
                       viewBox="0 0 100 100",
                       class_="expand-icon")
    # Create a polygon that is an equilateral triangle. Coordinates chosen so that
    # the triangle fills most of a 100x100 box.
    polygon = soup.new_tag("polygon",
                           points="20,30 80,30 50,70",
                           fill=ICON_COLOR)
    svg.append(polygon)
    return svg

# ----- MAIN PROCESSING FUNCTION -----

def main():
    if len(sys.argv) != 5:
        sys.exit("Usage: python build_toc.py input.json toc.html toc_style.css toc_script.js")

    json_file = sys.argv[1]
    html_out = sys.argv[2]
    css_out = sys.argv[3]
    js_out = sys.argv[4]

    # Read JSON file.
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # We expect the JSON to have "entities" and "topLevelIds".
    entities = data.get("entities", {})
    # In our example JSON the actual entries are stored under entities["pages"]
    # so check for that.
    if "pages" in entities:
        entities = entities["pages"]
    top_level_ids = data.get("topLevelIds", [])

    # Build a flat list of entries ordered appropriately.
    flat_entries = build_flat_list(entities, top_level_ids)

    # Create the HTML document using BeautifulSoup.
    soup = BeautifulSoup("<!DOCTYPE html><html></html>", "html.parser")
    head = soup.new_tag("head")

    # Link the CSS stylesheet.
    link_tag = soup.new_tag("link", rel="stylesheet", href=os.path.basename(css_out))
    head.append(link_tag)

    # Add a meta charset and a title.
    meta_tag = soup.new_tag("meta", charset="UTF-8")
    head.append(meta_tag)
    title_tag = soup.new_tag("title")
    title_tag.string = "Table of Contents"
    head.append(title_tag)
    soup.html.append(head)

    body = soup.new_tag("body")

    # Create a container div with a fixed width.
    container = soup.new_tag("div", **{"class": "toc-container"})
    container["style"] = f"width: {TOC_WIDTH}px; border:1px solid #ccc; padding:5px; font-family: sans-serif;"

    # Create the <ul> that will hold the TOC.
    ul = soup.new_tag("ul", **{"id": "toc-list", "class": "toc-list"})
    # Remove default list-style.
    ul["style"] = "list-style:none; margin:0; padding:0;"

    # For each entry in the flat list, generate an <li>.
    # Only top-level entries (level 0) will be initially visible;
    # all others get a CSS class "hidden".
    for entry in flat_entries:
        li = soup.new_tag("li", **{"data-id": entry["id"], "data-level": str(entry.get("level",0))})
        # Set an inline style for left padding. Let each level add, say, 20px.
        level = entry.get("level", 0)
        icon_total_width = ICON_WIDTH + 4  # leave a few pixels margin for the icon if needed
        li["style"] = f"padding-left: {level * icon_total_width + 4}px; margin: 4px 0; cursor: pointer;"
        # Hide any non top-level entry initially.
        if level != 0:
            li["class"] = "hidden"

        etype = entry_type(entry)
        li["class"] = li.get("class", "") + " " + etype

        # For heading entries, we want to add an expanded icon (the svg), next to the text.
        if etype == "heading":
            icon_tag = build_svg_icon(soup)
            # Wrap the icon in a span (so that we can later rotate it via JS)
            span_icon = soup.new_tag("span", **{"class": "icon-wrapper"})
            span_icon.append(icon_tag)
            li.append(span_icon)

            # Also add a span for the title.
            span_title = soup.new_tag("span", **{"class": "entry-title"})
            span_title.string = entry.get("title", "")
            li.append(span_title)
        else:
            # For pages or external, we wrap the title text as <a>
            if "url" in entry:
                a_tag = soup.new_tag("a", href=entry["url"], **{"class": "entry-title"})
                a_tag.string = entry.get("title", "")
                li.append(a_tag)
            else:
                span_title = soup.new_tag("span", **{"class": "entry-title"})
                span_title.string = entry.get("title", "")
                li.append(span_title)

        # Also store the parent id if available (for JS toggling).
        if "parentId" in entry:
            li["data-parent"] = entry["parentId"]
        else:
            li["data-parent"] = ""

        ul.append(li)

    container.append(ul)
    body.append(container)

    # Add a <script> tag to include our JS file.
    script_tag = soup.new_tag("script", src=os.path.basename(js_out))
    body.append(script_tag)

    soup.html.append(body)

    # Write out the HTML file.
    with open(html_out, "w", encoding="utf-8") as f:
        f.write(str(soup.prettify()))

    # Write out the CSS file.
    css_content = f"""
/* toc_style.css */

.toc-container {{
    /* fixed width specified by build script */
    margin: 10px;
}}

li.page a {{
    color: blue;
    text-decoration: none;
}}

li.external a {{
    color: red;
    text-decoration: none;
}}

li.heading .entry-title {{
    color: purple;
}}

.hidden {{
    display: none;
}}

.icon-wrapper {{
    display: inline-block;
    vertical-align: middle;
    width: {ICON_WIDTH}px;
    height: {ICON_WIDTH}px;
    margin-right: 4px;
    transition: transform 0.2s ease;
}}

li.heading.expanded .icon-wrapper {{
    transform: rotate(90deg);
}}

/* Optionally, add hover cursors for pages */
li.page a:hover, li.external a:hover {{
    text-decoration: underline;
}}
"""
    with open(css_out, "w", encoding="utf-8") as f:
        f.write(css_content.strip())

    # Write out the JavaScript file.
    js_content = r"""
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
"""
    with open(js_out, "w", encoding="utf-8") as f:
        f.write(js_content.strip())

    print(f"Generated {html_out}, {css_out} and {js_out} successfully.")

if __name__ == '__main__':
    main()