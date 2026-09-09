"""Tests for the Android reference HTML -> JSON extractor.

The fixtures are cut-down versions of real pages from the scrape, kept faithful in the places that
matter: doclava's unclosed summary tables, its `data-version-*` attributes and split signature
blocks; Dackka's `api-item` wrappers, its header block and its single-cell parameter declarations.
A fixture that tidied those up would test a page shape the scrape does not contain.
"""

import json
from pathlib import Path

import pytest

import android_docs_to_json as extractor


# --------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------

# Note the missing </table> before the constants table, and again before the detail sections:
# doclava never closes a summary table, and every HTML parser therefore nests everything that
# follows inside the first one. The extractor has to cope, so the fixture keeps the defect.
DOCLAVA_CLASS = """
<html><head><title>Widget</title></head><body><article class="devsite-article">
<style>.ignored { color: red }</style>
<div class="devsite-article-meta nocontent"><ul class="devsite-breadcrumb-list"><li>crumbs</li></ul></div>
<div id="api-info-block"><div class="api-level">Added in <a href="/guide/x">API level 4</a></div>
<div class="sum-details-links">Summary: <a href="#pubmethods">Methods</a></div></div>
<div id="jd-content" data-version-added="4">
<!-- ======== START OF CLASS DATA ======== -->
<h1 class="api-title" id="widget" data-text="Widget">Widget</h1>
<hr style="margin: 0">
<div class="nocontent"><a href="/reference/kotlin/android/demo/Widget">Kotlin</a></div>
<p>
<code class="api-signature" translate="no" dir="ltr">public class Widget</code>
<code class="api-signature" translate="no" dir="ltr">extends <a href="/reference/android/demo/Base">Base</a></code>
<code class="api-signature" translate="no" dir="ltr">
      implements
        <a href="/reference/android/demo/Widget.Listener">Widget.Listener</a>,
        <a href="/reference/java/lang/Cloneable">Cloneable</a>
</code>
</p>
<table class="jd-inheritance-table">
<tr><td colspan="2" class="jd-inheritance-class-cell"><a href="/reference/java/lang/Object">java.lang.Object</a></td></tr>
<tr><td class="jd-inheritance-space">&nbsp;&nbsp;&nbsp;&#x21b3;</td>
<td colspan="1" class="jd-inheritance-class-cell">android.demo.Widget</td></tr>
</table>
<table class="jd-sumtable jd-sumtable-subclasses"><tr><td><div class="expandable">
<span class="expand-control">Known direct subclasses</span>
<div id="subclasses-direct" class="showalways"><a href="/reference/android/demo/FancyWidget">FancyWidget</a></div>
</div></td></tr></table>
<p>A widget that does something. Second sentence is not the brief.
See the <a href="/guide/topics/widgets">guide</a>.</p>
<h2 class="api-section" id="summary" data-text="Summary">Summary</h2>
<table id="nestedclasses" class="responsive">
<tr><th colspan="2"><h3 id="nested-classes" data-text="Nested classes">Nested classes</h3></th></tr>
<tr data-version-added="9"><td class="jd-typecol"><code translate="no" dir="ltr">interface</code></td>
<td class="jd-descrcol" width="100%">
<code translate="no" dir="ltr"><a href="/reference/android/demo/Widget.Listener">Widget.Listener</a></code>
<p>Watches a widget.&nbsp;</p></td></tr>
<tr><td colspan="2"><div class="expandable jd-inherited-apis">
<span class="expand-control">From class <code><a href="/reference/android/demo/Base">android.demo.Base</a></code></span>
<table class="responsive"><tr><td><code>interface</code></td>
<td width="100%"><code><a href="/reference/android/demo/Base.Handle">Base.Handle</a></code><p>Inherited nested type.</p></td>
</tr></table></div></td></tr>
<table id="constants" class="responsive constants">
<tr><th colspan="2"><h3 id="constants" data-text="Constants">Constants</h3></th></tr>
<tr data-version-added="4"><td><code translate="no" dir="ltr">int</code></td>
<td width="100%"><code translate="no" dir="ltr"><a href="/reference/android/demo/Widget#MODE_FAST">MODE_FAST</a></code>
<p>Go quickly.&nbsp;</p></td></tr>
<table id="inhmethods" class="responsive methods inhtable">
<tr><th><h3 id="inherited-methods" data-text="Inherited methods">Inherited methods</h3></th></tr>
<tr><td colspan="2"><div class="expandable jd-inherited-apis">
<span class="expand-control">From class <code><a href="/reference/android/demo/Base">android.demo.Base</a></code></span>
<table class="responsive"><tr data-version-added="1"><td><code>void</code></td>
<td width="100%"><code><a href="/reference/android/demo/Base#reset()">reset</a>()</code><p>Puts it back.</p></td>
</tr></table></div></td></tr>
<!-- ========= ENUM CONSTANTS DETAIL ======== -->
<h2 class="api-section" id="constants_1" data-text="Constants">Constants</h2>
<div data-version-added="4">
<h3 class="api-name" id="MODE_FAST" data-text="MODE_FAST">MODE_FAST</h3>
<div class="api-level">Added in <a href="/guide/x">API level 4</a></div>
<div></div><devsite-code><pre class="api-signature no-pretty-print" translate="no" dir="ltr">
public static final int MODE_FAST</pre></devsite-code>
<p>Go quickly.</p></p>
<div><p><b>See also:</b></p>
<ul class="nolist"><li><code><a href="/reference/android/demo/Widget#setMode(int)">setMode(int)</a></code></li></ul></div>
<p>Constant Value:
    1
    (0x00000001)
</div>
<!-- ========= METHOD DETAIL ======== -->
<h2 class="api-section" id="public-methods_1" data-text="Public methods">Public methods</h2>
<div data-version-added="4" data-version-deprecated="11">
<h3 class="api-name" id="setMode(int)" data-text="setMode">setMode</h3>
<div class="api-level"><div>Added in <a href="/guide/x">API level 4</a>
<br>Deprecated in <a href="/guide/x">API level 11</a></div></div>
<div></div><devsite-code><pre class="api-signature no-pretty-print" translate="no" dir="ltr">
public <a href="/reference/android/demo/Base">Base</a> setMode (int mode)</pre></devsite-code>
<p><p class="caution"><strong>This method was deprecated in API level 11.</strong><br/>
Use <code><a href="/reference/android/demo/Widget#setSpeed(int)">setSpeed(int)</a></code> instead.</p>
<p>Sets the mode.</p></p>
<table class="responsive"><tr><th colspan=2>Parameters</th></tr>
<tr><td><code translate="no" dir="ltr">mode</code></td>
<td width="100%"><code translate="no" dir="ltr">int</code>: One of the mode constants.</p></td></tr></table>
<table class="responsive"><tr><th colspan=2>Returns</th></tr>
<tr><td><code translate="no" dir="ltr"><a href="/reference/android/demo/Base">Base</a></code></td>
<td width="100%">This widget, for chaining.</p></td></tr></table>
<table class="responsive"><tr><th colspan=2>Throws</th></tr>
<tr><td><code translate="no" dir="ltr"><a href="/reference/java/lang/IllegalStateException">IllegalStateException</a></code></td>
<td width="100%">if it is too late.</td></tr></table>
</div>
<!-- ========= END OF CLASS DATA ========= -->
</div></article>
<devsite-hats-survey class="nocontent"></devsite-hats-survey>
</body></html>
"""

DACKKA_CLASS = """
<html><head><title>Gadget</title></head><body><article class="devsite-article">
<style>.ignored { color: red }</style>
<div class="devsite-article-body">
<div itemscope itemtype="http://developers.google.com/ReferenceObject">
<meta itemprop="name" content="Gadget"><meta itemprop="language" content="JAVA">
</div>
<div id="header-block">
<div><h1 id="gadget" data-text="Gadget">Gadget</h1></div>
<div id="metadata-info-block">
<div id="maven-coordinates">Artifact: <a href="/jetpack/androidx/releases/demo">androidx.demo:demo</a></div>
<div id="source-link"><a href="https://cs.android.com/x" class="external">View Source</a></div>
<div id="version-metadata"><div id="added-in">Added in <a href="/jetpack/androidx/releases/demo#1.2.0">1.2.0</a></div></div>
</div></div>
<hr style="margin: 0">
<div class="nocontent"><a href="/reference/kotlin/androidx/demo/Gadget">Kotlin</a></div>
<p><div></div><devsite-code><pre translate="no" dir="ltr">public final class <a href="/reference/androidx/demo/Gadget">Gadget</a> implements <a href="https://developer.android.com/reference/java/io/Closeable.html">Closeable</a></pre></devsite-code></p>
<div class="devsite-table-wrapper"><table class="jd-inheritance-table"><tbody>
<tr><td colspan="2"><a href="https://developer.android.com/reference/java/lang/Object.html">java.lang.Object</a></td></tr>
<tr><td class="jd-inheritance-space">&nbsp;&nbsp;&nbsp;&#x21b3;</td>
<td colspan="1"><a href="/reference/androidx/demo/Gadget">androidx.demo.Gadget</a></td></tr>
</tbody></table></div>
<hr>
<p>A gadget. It has two sentences.</p>
<h2 id="summary" data-text="Summary">Summary</h2>
<div class="devsite-table-wrapper"><table class="responsive">
<colgroup><col width="40%"><col></colgroup>
<thead><tr><th colspan="100%"><h3 id="public-methods" data-text="Public methods">Public methods</h3></th></tr></thead>
<tbody class="list">
<tr><td><code translate="no" dir="ltr">void</code></td>
<td><div><code translate="no" dir="ltr"><a href="/reference/androidx/demo/Gadget#attach(androidx.demo.Part)">attach</a>(@<a href="/reference/androidx/annotation/NonNull">NonNull</a> <a href="/reference/androidx/demo/Part">Part</a>&nbsp;part)</code></div>
<p>Attaches a part.</p></td></tr>
</tbody></table></div>
<div class="devsite-table-wrapper"><table class="responsive" id="inhmethods">
<thead><tr><th colspan="100%"><h3 id="inherited-methods" data-text="Inherited methods">Inherited methods</h3></th></tr></thead>
<tbody class="list"><tr><td><devsite-expandable>
<span class="expand-control">From <a href="https://developer.android.com/reference/java/io/Closeable.html">java.io.Closeable</a></span>
<div class="devsite-table-wrapper"><table class="responsive"><tbody class="list">
<tr><td><code translate="no" dir="ltr">void</code></td>
<td><div><code translate="no" dir="ltr"><a href="https://developer.android.com/reference/java/io/Closeable.html#close()">close</a>()</code></div></td></tr>
</tbody></table></div></devsite-expandable></td></tr></tbody></table></div>
<div class="list">
<h2 id="public-methods_1" data-text="Public methods">Public methods</h2>
<div class="api-item"><a name="attach(androidx.demo.Part)"></a><a name="attach"></a>
<div class="api-name-block"><div><h3 id="attach(androidx.demo.Part)" data-text="attach">attach</h3></div>
<div id="metadata-info-block"><div id="version-metadata">
<div id="added-in">Added in <a href="/jetpack/androidx/releases/demo#1.3.0">1.3.0</a></div></div></div></div>
<div></div><devsite-code><pre class="api-signature no-pretty-print" translate="no" dir="ltr">public&nbsp;void&nbsp;<a href="/reference/androidx/demo/Gadget#attach(androidx.demo.Part)">attach</a>(@<a href="/reference/androidx/annotation/NonNull">NonNull</a> <a href="/reference/androidx/demo/Part">Part</a>&nbsp;part)</pre></devsite-code>
<p>Attaches a part.</p>
<div class="devsite-table-wrapper"><table class="responsive">
<thead><tr><th colspan="100%">Parameters</th></tr></thead>
<tbody class="list"><tr>
<td><code translate="no" dir="ltr">@<a href="/reference/androidx/annotation/NonNull">NonNull</a> <a href="/reference/androidx/demo/Part">Part</a>&nbsp;part</code></td>
<td><p>the part to attach</p></td></tr>
<tr><td><code translate="no" dir="ltr">boolean&nbsp;force</code></td>
<td><code translate="no" dir="ltr"><a href="/reference/androidx/demo/Part">Part</a></code> is replaced when true</td></tr>
</tbody></table></div>
<div class="devsite-table-wrapper"><table class="responsive">
<thead><tr><th colspan="100%">See also</th></tr></thead>
<tbody class="list"><tr>
<td><code translate="no" dir="ltr"><a href="/reference/androidx/demo/Gadget#detach()">detach</a></code></td>
<td></td></tr></tbody></table></div>
</div></div>
<devsite-hats-survey class="nocontent"></devsite-hats-survey>
</div></article></body></html>
"""

LISTING_PAGE = """
<html><head><title>Class Index</title></head><body><article>
<div class="devsite-article-body">
<div id="header-block"><div><h1 id="class-index" data-text="Class Index">Class Index</h1></div></div>
<h2 id="letter_G" data-text="G">G</h2>
<div class="devsite-table-wrapper"><table class="responsive"><tbody class="list">
<tr><td><code translate="no" dir="ltr"><a href="/reference/androidx/demo/Gadget">Gadget</a></code></td>
<td><p>A gadget.</p></td></tr>
</tbody></table></div>
</div></article></body></html>
"""

PACKAGE_PAGE = """
<html><head><title>android.demo</title></head><body><article>
<div id="jd-content" data-version-added="4">
<h1 id="android.demo" data-text="android.demo">android.demo</h1>
<hr style="margin: 0">
<div class="nocontent">Kotlin | Java</div>
Widgets and gadgets.
<h2 id="classes" data-text="Classes">Classes</h2>
<table class="jd-sumtable-expando">
<tr data-version-added="4"><td class="jd-linkcol"><a href="/reference/android/demo/Widget">Widget</a></td>
<td class="jd-descrcol" width="100%">A widget.&nbsp;</td></tr>
</table>
<div class="data-reference-resources-wrapper"><ul><li>duplicate list</li></ul></div>
</div></article></body></html>
"""

SUPPORT_LIBRARY_CLASS = """
<html><body><article><div class="devsite-article-body">
<div id="header-block">
<div><h1 id="mediabrowsercompat" data-text="MediaBrowserCompat">MediaBrowserCompat</h1></div>
<div id="metadata-info-block">
<div id="maven-coordinates">Artifact: <a href="/jetpack/androidx/releases/media">androidx.media:media</a></div>
<div id="version-metadata"><div id="added-in">Added in <a href="/jetpack/androidx/releases/media#1.1.0">1.1.0</a></div></div>
</div></div>
<p><div></div><devsite-code><pre translate="no" dir="ltr">public final class <a href="/reference/android/support/v4/media/MediaBrowserCompat">MediaBrowserCompat</a></pre></devsite-code></p>
<p>A browser for media content.</p>
</div></article></body></html>
"""

GENERIC_CLASS = """
<html><body><article><div id="jd-content" data-version-added="21">
<h1 class="api-title" id="rational">Rational</h1>
<p>
<code class="api-signature">public final class Rational</code>
<code class="api-signature">extends <a href="/reference/java/lang/Number">Number</a></code>
<code class="api-signature">
      implements
        <a href="/reference/java/lang/Comparable">Comparable</a>&lt;<a href="/reference/android/demo/Rational">Rational</a>&gt;,
        <a href="/reference/android/demo/Widget.Listener">Widget.Listener</a>
</code>
</p>
<p>A rational number.</p>
</div></article></body></html>
"""

KNOWN_PAGES = {
    "android/demo/Widget", "android/demo/Widget.Listener", "android/demo/Base",
    "android/demo/Base.Handle", "android/demo/FancyWidget", "android/demo/package-summary",
    "androidx/demo/Gadget", "androidx/demo/Part", "androidx/annotation/NonNull",
    "androidx/classes", "android/demo/Rational", "android/support/v4/media/MediaBrowserCompat",
}


@pytest.fixture
def linker():
    return extractor.Linker(set(KNOWN_PAGES))


def parse(markup, relative, linker, tmp_path):
    source = tmp_path / Path(relative).name
    source.write_text(markup, encoding="utf-8")
    return extractor.convert(source, relative, linker)


@pytest.fixture
def doclava(linker, tmp_path):
    return parse(DOCLAVA_CLASS, "android/demo/Widget.html", linker, tmp_path)


@pytest.fixture
def dackka(linker, tmp_path):
    return parse(DACKKA_CLASS, "androidx/demo/Gadget.html", linker, tmp_path)


def section(page, key, section_id):
    matches = [s for s in page.get(key, []) if s["id"] == section_id]
    assert matches, f"no {key} section {section_id!r} in {[s['id'] for s in page.get(key, [])]}"
    return matches[0]


def member(page, section_id, name):
    matches = [m for m in section(page, "details", section_id)["members"] if m["name"] == name]
    assert matches, f"no member {name!r} in {section_id}"
    return matches[0]


# --------------------------------------------------------------------------------------------
# Link rewriting
# --------------------------------------------------------------------------------------------

class TestLinker:
    def test_page_in_the_scrape_becomes_a_relative_json_link(self, linker):
        assert linker.resolve("/reference/android/demo/Base",
                              "android/demo/Widget.html") == "Base.json"

    def test_relative_link_climbs_out_of_the_package_directory(self, linker):
        assert linker.resolve("/reference/androidx/demo/Gadget",
                              "android/demo/Widget.html") == "../../androidx/demo/Gadget.json"

    def test_fragment_is_carried_through_untouched(self, linker):
        # doclava's own anchors are percent-encoded; the links in the scrape point at exactly
        # those strings, so re-encoding or decoding either side would break the pair.
        url = "/reference/android/demo/Widget#setMode(int,%20android.demo.Base)"
        assert linker.resolve(url, "android/demo/Widget.html") == \
            "Widget.json#setMode(int,%20android.demo.Base)"

    def test_page_outside_the_scrape_becomes_an_absolute_url(self, linker):
        assert linker.resolve("/reference/java/lang/Object", "android/demo/Widget.html") == \
            "https://developer.android.com/reference/java/lang/Object"

    def test_absolute_form_of_a_scraped_page_is_recognised(self, linker):
        # Dackka links to the framework with an absolute URL and an .html suffix; doclava links
        # to the same page with a site-relative path and none. Both name one file.
        assert linker.resolve("https://developer.android.com/reference/android/demo/Base.html",
                              "android/demo/Widget.html") == "Base.json"

    def test_non_reference_site_path_becomes_absolute(self, linker):
        assert linker.resolve("/guide/topics/manifest", "android/demo/Widget.html") == \
            "https://developer.android.com/guide/topics/manifest"

    def test_external_url_is_left_alone(self, linker):
        assert linker.resolve("https://cs.android.com/x", "a/b.html") == "https://cs.android.com/x"

    def test_other_url_schemes_are_left_alone(self, linker):
        # The scrape contains a few hand-written ones. Resolving them as paths would turn them
        # into links into this tree.
        assert linker.resolve("ftp:/dkuug.dk/i18n.txt", "a/b.html") == "ftp:/dkuug.dk/i18n.txt"
        assert linker.resolve("mailto:x@example.com", "a/b.html") == "mailto:x@example.com"

    def test_same_page_anchor_is_left_alone(self, linker):
        assert linker.resolve("#summary", "a/b.html") == "#summary"

    def test_protocol_relative_url_gets_a_scheme(self, linker):
        assert linker.resolve("//example.com/x", "a/b.html") == "https://example.com/x"


# --------------------------------------------------------------------------------------------
# Path-derived identity
# --------------------------------------------------------------------------------------------

class TestIdentity:
    def test_package_and_name_come_from_the_path(self):
        assert extractor.page_identity("android/app/Activity.html") == {
            "library": "android", "packageName": "android.app", "name": "Activity",
            "qualifiedName": "android.app.Activity"}

    def test_a_nested_class_keeps_its_dotted_name(self):
        # The path is what distinguishes `android.app.ActionBar.LayoutParams` from a top-level
        # class called `ActionBar.LayoutParams`; the heading on the page does not.
        assert extractor.page_identity("android/app/ActionBar.LayoutParams.html")["name"] == \
            "ActionBar.LayoutParams"

    @pytest.mark.parametrize("signature,expected", [
        ("public class Widget extends Base", "class"),
        ("public interface Listener", "interface"),
        ("public final enum Mode", "enum"),
        ("public @interface Nullable", "annotation"),
        ("@MustBeDocumented public annotation AnimRes", "annotation"),
        ("public object Registry", "object"),
        ("", None),
    ])
    def test_kind_is_read_from_the_signature(self, signature, expected):
        assert extractor.class_kind(signature) == expected


# --------------------------------------------------------------------------------------------
# doclava pages
# --------------------------------------------------------------------------------------------

class TestDoclavaClass:
    def test_page_identity(self, doclava):
        assert doclava["page"] == "android-class"
        assert doclava["flavor"] == "doclava"
        assert doclava["versionScheme"] == "api-level"
        assert doclava["qualifiedName"] == "android.demo.Widget"
        assert doclava["kind"] == "class"

    def test_signature_is_joined_from_the_separate_blocks(self, doclava):
        assert doclava["signature"] == \
            "public class Widget extends Base implements Widget.Listener, Cloneable"
        assert 'href="Base.json"' in doclava["signatureHtml"]

    def test_version_comes_from_the_content_attribute(self, doclava):
        assert doclava["addedIn"] == "4"

    def test_inheritance_chain_ends_with_this_class_unlinked(self, doclava):
        chain = doclava["inheritance"]
        assert [entry["label"] for entry in chain] == ["java.lang.Object", "android.demo.Widget"]
        assert "url" not in chain[-1]

    def test_known_subclasses(self, doclava):
        assert doclava["knownDirectSubclasses"] == [
            {"label": "FancyWidget", "url": "FancyWidget.json"}]

    def test_description_is_the_prose_and_nothing_else(self, doclava):
        description = doclava["description"]
        assert "A widget that does something." in description
        # The chrome around it, and the summary and detail sections, are all accounted for
        # elsewhere; none of it may leak into the prose.
        for leaked in ("crumbs", "Kotlin", "MODE_FAST", "Summary", "START OF CLASS DATA",
                       "hats-survey", "sum-details-links"):
            assert leaked not in description

    def test_brief_is_the_first_sentence(self, doclava):
        assert doclava["brief"] == "A widget that does something."

    def test_summary_sections_are_in_reference_order(self, doclava):
        assert [s["id"] for s in doclava["summary"]] == ["nested-classes", "constants"]

    def test_summary_survives_the_unclosed_table(self, doclava):
        # Everything after `<table id="nestedclasses">` parses as a child of it, because doclava
        # never closes it. Each section must still see only its own rows.
        assert len(section(doclava, "summary", "nested-classes")["rows"]) == 1
        assert len(section(doclava, "summary", "constants")["rows"]) == 1

    def test_summary_row_fields(self, doclava):
        row = section(doclava, "summary", "constants")["rows"][0]
        assert row["name"] == "MODE_FAST"
        assert row["anchor"] == "MODE_FAST"
        assert row["type"] == "int"
        assert row["addedIn"] == "4"
        assert 'href="Widget.json#MODE_FAST"' in row["member"]
        assert "Go quickly." in row["description"]

    def test_a_section_carries_both_declared_and_inherited_members(self, doclava):
        # doclava's nested-classes table holds the class's own nested types and the ones it
        # inherits, in one table.
        nested = section(doclava, "summary", "nested-classes")
        assert nested["rows"][0]["name"] == "Widget.Listener"
        assert [group["from"]["label"] for group in nested["groups"]] == ["android.demo.Base"]
        assert nested["groups"][0]["rows"][0]["name"] == "Base.Handle"

    def test_purely_inherited_sections_are_separate(self, doclava):
        inherited = section(doclava, "inherited", "inherited-methods")
        assert inherited["groups"][0]["from"] == {"label": "android.demo.Base",
                                                  "url": "Base.json"}
        assert inherited["groups"][0]["rows"][0]["name"] == "reset"

    def test_constant_detail(self, doclava):
        constant = member(doclava, "constants", "MODE_FAST")
        assert constant["anchor"] == "MODE_FAST"
        assert constant["signature"] == "public static final int MODE_FAST"
        assert constant["constantValue"] == "1 (0x00000001)"
        assert constant["description"] == "<p>Go quickly.</p>"
        assert constant["seeAlso"] == [
            {"label": "setMode(int)", "url": "Widget.json#setMode(int)"}]
        # The "See also:" heading and the list itself are extracted, not left in the prose.
        assert "See also" not in constant["description"]

    def test_method_detail(self, doclava):
        method = member(doclava, "public-methods", "setMode")
        assert method["anchor"] == "setMode(int)"
        assert method["signature"] == "public Base setMode (int mode)"
        assert method["parameters"] == [{"name": "mode", "type": "int",
                                        "declaration": "int mode",
                                        "description": "One of the mode constants."}]
        assert method["returns"] == [{"type": '<a href="Base.json">Base</a>',
                                      "description": "This widget, for chaining."}]
        assert method["throws"][0]["description"] == "if it is too late."
        assert "IllegalStateException" in method["throws"][0]["type"]
        assert method["description"] == "<p>Sets the mode.</p>"

    def test_deprecation(self, doclava):
        method = member(doclava, "public-methods", "setMode")
        assert method["deprecated"] is True
        assert method["deprecatedIn"] == "11"
        assert method["deprecationLabel"] == "This method was deprecated in API level 11."
        assert 'href="Widget.json#setSpeed(int)"' in method["deprecationNote"]
        # The deprecation notice is its own field, so it must not also be part of the prose.
        assert "deprecated" not in method["description"]


# --------------------------------------------------------------------------------------------
# Dackka pages
# --------------------------------------------------------------------------------------------

class TestDackkaClass:
    def test_page_identity(self, dackka):
        assert dackka["flavor"] == "dackka"
        assert dackka["versionScheme"] == "library-version"
        assert dackka["qualifiedName"] == "androidx.demo.Gadget"
        assert dackka["kind"] == "class"

    def test_release_metadata(self, dackka):
        assert dackka["addedIn"] == "1.2.0"
        assert dackka["artifact"] == {
            "label": "androidx.demo:demo",
            "url": "https://developer.android.com/jetpack/androidx/releases/demo",
            "linkClass": "external-link"}
        assert dackka["sourceUrl"] == "https://cs.android.com/x"

    def test_signature(self, dackka):
        assert dackka["signature"] == "public final class Gadget implements Closeable"

    def test_inheritance_resolves_the_absolute_framework_link(self, dackka):
        assert dackka["inheritance"][0]["url"] == \
            "https://developer.android.com/reference/java/lang/Object"

    def test_description(self, dackka):
        assert dackka["description"] == "<p>A gadget. It has two sentences.</p>"
        assert dackka["brief"] == "A gadget."

    def test_summary_row(self, dackka):
        row = section(dackka, "summary", "public-methods")["rows"][0]
        assert row["name"] == "attach"
        assert row["anchor"] == "attach(androidx.demo.Part)"
        assert row["type"] == "void"
        # Dackka wraps the member in a spacer <div> inside the <code>; both go, because the
        # template puts the fragment in a <code> of its own.
        assert row["member"].startswith('<a href="Gadget.json#attach(')

    def test_inherited_methods(self, dackka):
        group = section(dackka, "inherited", "inherited-methods")["groups"][0]
        assert group["from"]["label"] == "java.io.Closeable"
        assert group["rows"][0]["name"] == "close"

    def test_member_detail(self, dackka):
        method = member(dackka, "public-methods", "attach")
        assert method["addedIn"] == "1.3.0"
        assert method["signature"] == "public void attach(@NonNull Part part)"
        assert method["description"] == "<p>Attaches a part.</p>"
        assert method["seeAlso"] == [{"label": "detach", "url": "Gadget.json#detach()"}]

    def test_parameter_declaration_is_kept_whole(self, dackka):
        # Dackka writes the annotations, the type and the name as one declaration in one cell.
        # There is no faithful way to split that, so `declaration` is what a template prints.
        parameter = member(dackka, "public-methods", "attach")["parameters"][0]
        assert parameter["name"] == "part"
        assert parameter["declaration"] == \
            '@<a href="../annotation/NonNull.json">NonNull</a> <a href="Part.json">Part</a> part'
        assert parameter["description"] == "<p>the part to attach</p>"

    def test_legacy_anchors_do_not_leak_into_the_prose(self, dackka):
        # Dackka precedes each item with empty <a name="..."> anchors for its older link forms.
        assert "<a" not in member(dackka, "public-methods", "attach")["description"]


# --------------------------------------------------------------------------------------------
# Listing pages
# --------------------------------------------------------------------------------------------

class TestListingPages:
    def test_class_index(self, linker, tmp_path):
        page = parse(LISTING_PAGE, "androidx/classes.html", linker, tmp_path)
        assert page["page"] == "android-index"
        assert page["title"] == "Class Index"
        assert [group["id"] for group in page["groups"]] == ["letter_G"]
        row = page["groups"][0]["rows"][0]
        assert row["name"] == "Gadget"
        assert 'href="demo/Gadget.json"' in row["member"]

    def test_scraped_package_summary(self, linker, tmp_path):
        page = parse(PACKAGE_PAGE, "android/demo/package-summary.html", linker, tmp_path)
        assert page["page"] == "android-package"
        assert page["title"] == "android.demo"
        assert "Widgets and gadgets." in page["description"]
        # The site repeats the whole listing in a hidden block for its own search index.
        assert "duplicate list" not in page["description"]
        row = page["groups"][0]["rows"][0]
        assert row["name"] == "Widget"
        assert row["description"] == "A widget."


# --------------------------------------------------------------------------------------------
# Generated navigation
# --------------------------------------------------------------------------------------------

class TestGeneratedPages:
    def entries(self):
        return [
            {"name": "Widget", "kind": "class", "brief": "A widget.", "addedIn": "4",
             "deprecatedIn": "21", "file": "Widget.json"},
            {"name": "Listener", "kind": "interface", "brief": "Watches.", "file":
             "Listener.json"},
        ]

    def test_package_page_groups_by_kind(self):
        page = extractor.package_page("android.demo", self.entries(),
                                      "android/demo/package-summary.json")
        assert page["page"] == "android-package"
        assert page["generated"] is True
        assert [group["id"] for group in page["groups"]] == ["interfaces", "classes"]
        row = page["groups"][1]["rows"][0]
        assert row["name"] == "Widget"
        assert row["member"] == '<a href="Widget.json">Widget</a>'
        assert row["addedIn"] == "4"
        assert row["deprecatedIn"] == "21"
        assert page["versionScheme"] == "api-level"

    def test_overview_lists_every_package_once(self):
        records = [
            {"library": "android", "packageName": "android.demo", "page": "android-class"},
            {"library": "android", "packageName": "android.demo", "page": "android-class"},
            {"library": "androidx", "packageName": "androidx.demo", "page": "android-class"},
        ]
        page = extractor.overview_page(records, ["androidx/classes.json"])
        assert [group["id"] for group in page["groups"]] == ["android", "androidx", "indexes"]
        android = page["groups"][0]["rows"]
        assert len(android) == 1
        assert android[0]["description"] == "2 types"
        assert android[0]["member"] == \
            '<a href="android/demo/package-summary.json">android.demo</a>' 
        assert page["groups"][1]["rows"][0]["description"] == "1 type"


# --------------------------------------------------------------------------------------------
# Fragment handling
# --------------------------------------------------------------------------------------------

class TestFragments:
    @pytest.mark.parametrize("html,expected", [
        ("<p>One sentence. And another.</p>", "One sentence."),
        ("<p>No terminator</p>", "No terminator"),
        ("<p>With <code>markup</code> inside. Rest.</p>", "With markup inside."),
        # A brief is text: the entities the HTML escaped are resolved back.
        ("<p>Holds a <code>List&lt;String&gt;</code>. Rest.</p>", "Holds a List<String>."),
        (None, None),
        ("", None),
    ])
    def test_first_sentence(self, html, expected):
        assert extractor.first_sentence(html) == expected

    def test_text_treats_a_line_break_as_whitespace(self, linker):
        # Dackka separates a signature's annotations with <br> and no text, which read together
        # would run them into one token.
        from bs4 import BeautifulSoup
        node = BeautifulSoup("<pre>@One<br>@Two<br>public class X</pre>", "lxml").pre
        assert extractor.text_of(node) == "@One @Two public class X"

    def test_output_is_json_serialisable(self, doclava, dackka):
        for page in (doclava, dackka):
            assert json.loads(json.dumps(page)) == page


# --------------------------------------------------------------------------------------------
# Deprecation at the class level
# --------------------------------------------------------------------------------------------

DEPRECATED_CLASS = """
<html><body><article><div id="jd-content" data-version-added="1" data-version-deprecated="21">
<h1 class="api-title" id="oldwidget">OldWidget</h1>
<p><code class="api-signature">public class OldWidget</code></p>
<p class="caution"><strong>This class was deprecated in API level 21.</strong><br/>
Use <code><a href="/reference/android/demo/Widget">Widget</a></code> instead.</p>
<p>Does the old thing.</p>
</div></article></body></html>
"""

CAUTIONING_CLASS = """
<html><body><article><div id="jd-content" data-version-added="1">
<h1 class="api-title" id="sharpwidget">SharpWidget</h1>
<p><code class="api-signature">public class SharpWidget</code></p>
<p>Does a sharp thing.</p>
<p class="caution"><strong>Careful:</strong> this can lose data.</p>
</div></article></body></html>
"""


class TestClassDeprecation:
    def test_notice_becomes_its_own_field(self, linker, tmp_path):
        page = parse(DEPRECATED_CLASS, "android/demo/OldWidget.html", linker, tmp_path)
        assert page["deprecated"] is True
        assert page["deprecatedIn"] == "21"
        assert page["deprecationLabel"] == "This class was deprecated in API level 21."
        assert 'href="Widget.json"' in page["deprecationNote"]
        # It used to be read as the class's first sentence, which is what the brief is for.
        assert page["brief"] == "Does the old thing."
        assert "deprecated" not in page["description"]

    def test_a_caution_that_is_not_about_deprecation_stays_in_the_prose(self, linker, tmp_path):
        # Devsite uses `.caution` for any warning, so the wording has to say "deprecated" before
        # the block is treated as one.
        page = parse(CAUTIONING_CLASS, "android/demo/SharpWidget.html", linker, tmp_path)
        assert "deprecated" not in page
        assert "can lose data" in page["description"]


# --------------------------------------------------------------------------------------------
# Documentation that is not part of the schema
# --------------------------------------------------------------------------------------------

# A class page whose prose -- at class level and inside a member -- contains tables and a warning
# of its own. None of it is a Parameters/Returns/Throws/See-also table, so all of it has to
# survive untouched, which is what the site's own pages look like.
PROSE_CLASS = """
<html><body><article><div id="jd-content" data-version-added="1">
<h1 class="api-title" id="ruler">Ruler</h1>
<p><code class="api-signature">public class Ruler</code></p>
<p>Measures things.</p>
<table class="responsive">
<tr><th>Rule</th><th>Example</th></tr>
<tr><td>HOSTNAME</td><td>example.com</td></tr>
</table>
<p class="caution"><strong>Careful:</strong> the deprecated overload is not the same.</p>
<h2 class="api-section" id="summary">Summary</h2>
<table id="pubmethods" class="responsive methods">
<tr><th colspan="2"><h3 id="public-methods" data-text="Public methods">Public methods</h3></th></tr>
<tr><td><code>void</code></td>
<td width="100%"><code><a href="/reference/android/demo/Ruler#measure(int)">measure</a>(int units)</code>
<p>Measures.&nbsp;</p></td></tr>
<h2 class="api-section" id="public-methods_1" data-text="Public methods">Public methods</h2>
<div data-version-added="1">
<h3 class="api-name" id="measure(int)" data-text="measure">measure</h3>
<div class="api-level">Added in <a href="/guide/x">API level 1</a></div>
<div></div><devsite-code><pre class="api-signature">public void measure (int units)</pre></devsite-code>
<p>Measures in the given units.</p>
<table class="responsive">
<tr><th>Unit</th><th>Meaning</th></tr>
<tr><td>0</td><td>millimetres</td></tr>
</table>
<table class="responsive"><tr><th colspan=2>Parameters</th></tr>
<tr><td><code>units</code></td><td width="100%"><code>int</code>: which unit.</p></td></tr></table>
<table class="responsive"><tr><th colspan=2>Parameters</th></tr>
<tr><td><code>extra</code></td><td width="100%"><code>int</code>: a second table the source split out.</p></td></tr></table>
</div>
</div></article></body></html>
"""


class TestProseIsLeftAlone:
    @pytest.fixture
    def prose(self, linker, tmp_path):
        return parse(PROSE_CLASS, "android/demo/Ruler.html", linker, tmp_path)

    def test_a_prose_table_keeps_its_header_row(self, prose):
        # The header row was being removed before the code decided whether it was even reading a
        # table it understood, which left the reader a table of unlabelled columns.
        assert "<th>Rule</th>" in prose["description"]
        assert "<th>Example</th>" in prose["description"]

    def test_a_prose_table_inside_a_member_keeps_its_header_row(self, prose):
        description = member(prose, "public-methods", "measure")["description"]
        assert "<th>Unit</th>" in description
        assert "<th>Meaning</th>" in description

    def test_a_second_table_under_one_heading_is_kept(self, prose):
        # Assigning instead of extending dropped the first table's rows silently.
        names = [p["name"] for p in member(prose, "public-methods", "measure")["parameters"]]
        assert names == ["units", "extra"]

    def test_a_warning_that_merely_mentions_deprecation_is_not_a_deprecation_notice(self, prose):
        # "the deprecated overload is not the same" is the author's warning about this class, not
        # a notice that the class is deprecated. Hoisting it would both mislabel the page and tear
        # the warning out of the paragraph it belongs to.
        assert "deprecated" not in prose
        assert "deprecationNote" not in prose
        assert "the deprecated overload" in prose["description"]


# --------------------------------------------------------------------------------------------
# Interfaces, anchors, and the identity of a listing page
# --------------------------------------------------------------------------------------------

class TestImplementedInterfaces:
    def test_doclava_interfaces_come_off_the_signature(self, doclava):
        assert doclava["implements"] == [
            {"label": "Widget.Listener", "url": "Widget.Listener.json"},
            {"label": "Cloneable",
             "url": "https://developer.android.com/reference/java/lang/Cloneable",
             "linkClass": "external-link"}]

    def test_dackka_interfaces_come_off_the_signature(self, dackka):
        assert dackka["implements"] == [
            {"label": "Closeable",
             "url": "https://developer.android.com/reference/java/io/Closeable",
             "linkClass": "external-link"}]

    def test_the_superclass_is_not_in_the_list(self, doclava):
        # Only links after the `implements` keyword; `extends Base` precedes it.
        assert "Base" not in [entry["label"] for entry in doclava["implements"]]


class TestAnchors:
    def test_a_row_on_this_page_keeps_its_anchor(self, doclava):
        assert section(doclava, "summary", "constants")["rows"][0]["anchor"] == "MODE_FAST"

    def test_an_inherited_row_has_no_anchor(self, doclava):
        # It links to the member on the class that declares it, and that fragment is an id on
        # *that* page -- so there is no anchor for it here.
        row = section(doclava, "inherited", "inherited-methods")["groups"][0]["rows"][0]
        assert row["name"] == "reset"
        assert "anchor" not in row


class TestListingIdentity:
    def test_a_package_page_is_named_after_its_package(self, linker, tmp_path):
        page = parse(PACKAGE_PAGE, "android/demo/package-summary.html", linker, tmp_path)
        assert page["packageName"] == "android.demo"
        assert page["name"] == "android.demo"
        assert page["qualifiedName"] == "android.demo"
        assert page["versionScheme"] == "api-level"

    def test_an_index_page_belongs_to_no_package(self, linker, tmp_path):
        # Its file name names nothing: "androidx.classes" is not a package, and anything counting
        # packages would count this page as a type in one.
        page = parse(LISTING_PAGE, "androidx/classes.html", linker, tmp_path)
        assert "packageName" not in page
        assert "qualifiedName" not in page
        assert page["library"] == "androidx"
        assert page["versionScheme"] == "library-version"

    def test_a_heading_with_no_table_does_not_claim_the_next_headings_table(self, linker, tmp_path):
        markup = """
        <html><body><article><div id="jd-content">
        <h1 id="android.demo">android.demo</h1>
        <h2 id="note">Note</h2>
        <p>No table under this heading.</p>
        <h2 id="classes">Classes</h2>
        <table class="jd-sumtable-expando"><tr>
        <td class="jd-linkcol"><a href="/reference/android/demo/Widget">Widget</a></td>
        <td class="jd-descrcol" width="100%">A widget.&nbsp;</td></tr></table>
        </div></article></body></html>
        """
        page = parse(markup, "android/demo/package-summary.html", linker, tmp_path)
        assert [group["id"] for group in page["groups"]] == ["classes"]
        assert page["groups"][0]["rows"][0]["name"] == "Widget"


class TestOverviewCounts:
    def test_only_types_are_counted(self):
        # A package page and an index page sit in a package without being types in it.
        records = [
            {"library": "android", "packageName": "android.demo", "page": "android-class"},
            {"library": "android", "packageName": "android.demo", "page": "android-package"},
            {"library": "android", "packageName": "android.demo", "page": "android-index"},
        ]
        rows = extractor.overview_page(records, [])["groups"][0]["rows"]
        assert rows[0]["description"] == "1 type"


class TestJsonPath:
    @pytest.mark.parametrize("relative,expected", [
        ("android/app/Activity.html", "android/app/Activity.json"),
        # `replace` would rewrite both, and the two trees would stop mirroring each other.
        ("android/x.html/Activity.html", "android/x.html/Activity.json"),
        ("index", "index.json"),
    ])
    def test_only_the_extension_changes(self, relative, expected):
        assert extractor.json_path(relative) == expected


# --------------------------------------------------------------------------------------------
# Which of the two version schemes a page is on
# --------------------------------------------------------------------------------------------

class TestVersionScheme:
    @pytest.mark.parametrize("flavor,library,expected", [
        ("doclava", "android", "api-level"),
        ("dackka", "androidx", "library-version"),
        # The support library: Dackka-written Jetpack documentation that sits under android/, so
        # the path says one thing and the tool that wrote it says another. The tool wins.
        ("dackka", "android", "library-version"),
        # A page with no recognisable flavor falls back to the path.
        ("unknown", "android", "api-level"),
        ("unknown", "androidx", "library-version"),
    ])
    def test_the_tool_that_wrote_the_page_decides(self, flavor, library, expected):
        assert extractor.version_scheme(flavor, library) == expected

    def test_a_dackka_page_under_android_is_versioned_by_release(self, linker, tmp_path):
        page = parse(SUPPORT_LIBRARY_CLASS,
                     "android/support/v4/media/MediaBrowserCompat.html", linker, tmp_path)
        assert page["library"] == "android"
        assert page["flavor"] == "dackka"
        assert page["addedIn"] == "1.1.0"
        # "Added in API level 1.1.0" names an API level that does not exist.
        assert page["versionScheme"] == "library-version"

    def test_a_generated_package_page_takes_the_scheme_of_its_types(self):
        entries = [{"name": "MediaBrowserCompat", "kind": "class", "file": "X.json",
                    "versionScheme": "library-version"}]
        page = extractor.package_page("android.support.v4.media", entries,
                                      "android/support/v4/media/package-summary.json")
        assert page["library"] == "android"
        assert page["versionScheme"] == "library-version"

    def test_a_generated_package_page_falls_back_to_the_path(self):
        entries = [{"name": "Widget", "kind": "class", "file": "Widget.json"}]
        page = extractor.package_page("android.demo", entries,
                                      "android/demo/package-summary.json")
        assert page["versionScheme"] == "api-level"


class TestGenericInterfaces:
    @pytest.fixture
    def generic(self, linker, tmp_path):
        return parse(GENERIC_CLASS, "android/demo/Rational.html", linker, tmp_path)

    def test_a_type_argument_is_not_an_implemented_interface(self, generic):
        # `implements Comparable<Rational>` links both names; only the one outside the brackets
        # is an interface. Taking both had the class implementing itself.
        assert generic["implements"] == [
            {"label": "Comparable",
             "url": "https://developer.android.com/reference/java/lang/Comparable",
             "linkClass": "external-link"},
            {"label": "Widget.Listener", "url": "Widget.Listener.json"}]

    def test_the_signature_still_records_the_type_argument(self, generic):
        # Dropping it from `implements` must not drop it from the signature.
        assert generic["signature"] == \
            "public final class Rational extends Number implements Comparable<Rational>, Widget.Listener"
        assert 'href="Rational.json"' in generic["signatureHtml"]

    def test_a_repeated_type_argument_is_not_listed_twice(self, linker, tmp_path):
        markup = GENERIC_CLASS.replace(
            '<a href="/reference/android/demo/Widget.Listener">Widget.Listener</a>',
            '<a href="/reference/java/lang/Iterable">Iterable</a>'
            '&lt;<a href="/reference/android/demo/Rational">Rational</a>&gt;')
        page = parse(markup, "android/demo/Rational.html", linker, tmp_path)
        assert [entry["label"] for entry in page["implements"]] == ["Comparable", "Iterable"]


class TestDeprecationWording:
    def test_a_notice_behind_other_bold_text_is_still_found(self, linker, tmp_path):
        # Matching only the <strong> missed this shape; matching only the body would miss the
        # ordinary one. Both are tried.
        markup = """
        <html><body><article><div id="jd-content" data-version-added="1">
        <h1 class="api-title" id="oldwidget">OldWidget</h1>
        <p><code class="api-signature">public class OldWidget</code></p>
        <p class="caution"><strong>Note:</strong> This class was deprecated in API level 21.
        Use <code><a href="/reference/android/demo/Widget">Widget</a></code>.</p>
        <p>Does the old thing.</p>
        </div></article></body></html>
        """
        page = parse(markup, "android/demo/OldWidget.html", linker, tmp_path)
        assert page["deprecated"] is True
        assert page["brief"] == "Does the old thing."
        assert "deprecated" not in page["description"]


class TestDocument:
    def test_a_document_arrives_with_the_chrome_already_gone(self, linker, tmp_path):
        # The fragment helpers no longer strip anything themselves, so this is the invariant they
        # rest on: a Document cannot be built without it.
        source = tmp_path / "Widget.html"
        source.write_text(DOCLAVA_CLASS, encoding="utf-8")
        doc = extractor.Document.read(source, "android/demo/Widget.html", linker)
        assert doc.flavor == "doclava"
        assert doc.relative == "android/demo/Widget.html"
        assert doc.own_file == "Widget.json"
        assert doc.root.select(".nocontent") == []
        assert doc.root.find_all("devsite-hats-survey") == []
        assert doc.root.find_all("style") == []

    def test_a_page_with_no_recognisable_content_is_no_document(self, linker, tmp_path):
        source = tmp_path / "empty.html"
        source.write_text("<html></html>", encoding="utf-8")
        assert extractor.Document.read(source, "android/demo/empty.html", linker) is None

    def test_link_resolves_against_the_page_it_belongs_to(self, linker, tmp_path):
        source = tmp_path / "Widget.html"
        source.write_text(DOCLAVA_CLASS, encoding="utf-8")
        doc = extractor.Document.read(source, "android/demo/Widget.html", linker)
        assert doc.link("/reference/android/demo/Base") == "Base.json"


class TestCaseFoldedPaths:
    def test_a_link_finds_a_page_stored_under_a_different_case(self):
        # `android.os.strictmode` is a package and `android.os.StrictMode` a class; the scrape is
        # read off a filesystem that cannot give both a directory, so the package's pages sit
        # under `StrictMode/`. The links still spell it lowercase.
        linker = extractor.Linker({"android/os/StrictMode/Violation"})
        assert linker.resolve("/reference/android/os/strictmode/Violation",
                              "android/os/StrictMode.html") == "StrictMode/Violation.json"

    def test_an_exact_match_still_wins(self):
        linker = extractor.Linker({"android/demo/Widget", "android/demo/widget"})
        assert linker.resolve("/reference/android/demo/widget",
                              "android/demo/Other.html") == "widget.json"

    def test_a_page_the_scrape_really_lacks_is_still_external(self):
        linker = extractor.Linker({"android/demo/Widget"})
        assert linker.resolve("/reference/java/lang/Object", "android/demo/Widget.html") == \
            "https://developer.android.com/reference/java/lang/Object"


# --------------------------------------------------------------------------------------------
# Telling the reader which links leave the app, and which lead nowhere
# --------------------------------------------------------------------------------------------

class TestLinkClass:
    @pytest.mark.parametrize("resolved,expected", [
        # Into this tree: the only thing the linker produces for a page it found.
        ("Base.json", None),
        ("../view/View.json#foo(int)", None),
        ("#summary", None),
        # Off-site, and so needing the network.
        ("https://developer.android.com/reference/java/lang/Object", "external-link"),
        ("https://cs.android.com/x", "external-link"),
        ("mailto:x@example.com", "external-link"),
        ("ftp:/dkuug.dk/i18n.txt", "external-link"),
        # Neither: a URL the linker could not place, from a malformed href in the source.
        ('"/reference/android/content/Intent', "broken-link"),
        ("URL", "broken-link"),
        (None, None),
    ])
    def test_a_resolved_url_says_what_kind_of_link_it_is(self, resolved, expected):
        assert extractor.link_class(resolved) == expected

    def test_an_off_site_link_in_prose_is_marked(self, doclava):
        # java.lang.Cloneable is not in this scrape, so the link leaves the app.
        implemented = {entry["label"]: entry for entry in doclava["implements"]}
        assert implemented["Cloneable"]["linkClass"] == "external-link"

    def test_a_link_into_the_tree_is_not_marked(self, doclava):
        implemented = {entry["label"]: entry for entry in doclava["implements"]}
        assert "linkClass" not in implemented["Widget.Listener"]

    def test_an_anchor_inside_a_documentation_fragment_gets_the_class(self, doclava):
        # The prose links to a guide page, which the scrape does not contain.
        assert 'class="external-link"' in doclava["description"]
        assert "developer.android.com" in doclava["description"]

    def test_a_class_the_source_already_set_is_kept(self, linker, tmp_path):
        markup = """
        <html><body><article><div id="jd-content">
        <h1 class="api-title" id="widget">Widget</h1>
        <p><code class="api-signature">public class Widget</code></p>
        <p>See <a class="api-reference" href="https://example.com/x">elsewhere</a>.</p>
        </div></article></body></html>
        """
        page = parse(markup, "android/demo/Widget.html", linker, tmp_path)
        assert 'class="api-reference external-link"' in page["description"]
