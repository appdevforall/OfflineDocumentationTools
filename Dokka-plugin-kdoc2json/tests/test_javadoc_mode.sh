#!/usr/bin/env bash
# Exercises the "javadoc-mode" config option against examples/example-java-library.
#
# Javadoc mode's contract is that the output *mirrors the javadoc tool's own api/ tree* -- both
# where files land and what each page contains -- so these assertions are written against real
# javadoc behaviour: package directories rather than Dokka's `com.example.shapes/-rectangle/`
# layout, `<init>(double,double)` member anchors, an inheritance closure, inherited-member groups,
# and the global index files (allclasses-index, deprecated-list, constant-values, index-files,
# element-list).
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$TESTS_DIR/lib.sh"

publish_plugin

JD_ON='{"logLevel":"debug","javadoc-mode":true,"prettyPrint":true}'
JD_OFF='{"logLevel":"debug","prettyPrint":true}'

echo "==> javadoc-mode disabled: output keeps Dokka's own layout"
run_dokka_java "$JD_OFF"
assert_file_exists "$JAVA_OUTPUT_DIR/com.example.shapes/-rectangle/index.json" \
    "default mode still writes Dokka-shaped pages"
assert_file_not_exists "$JAVA_OUTPUT_DIR/com/example/shapes/Rectangle.json" \
    "default mode writes no javadoc-shaped pages"
assert_file_not_exists "$JAVA_OUTPUT_DIR/allclasses-index.json" \
    "default mode writes no javadoc index files"

echo
echo "==> javadoc-mode enabled: layout mirrors javadoc's api/ tree"
run_dokka_java "$JD_ON"

# Package-as-directory layout, and a nested type kept as Outer.Nested in the enclosing package.
assert_file_exists "$JAVA_OUTPUT_DIR/com/example/shapes/Rectangle.json" \
    "class page lands at <package/as/path>/<ClassName>.json"
assert_file_exists "$JAVA_OUTPUT_DIR/com/example/shapes/Rectangle.Builder.json" \
    "nested type keeps its dotted name, as javadoc does"
assert_file_exists "$JAVA_OUTPUT_DIR/com/example/shapes/package-summary.json" \
    "package page lands at package-summary.json"
assert_file_exists "$JAVA_OUTPUT_DIR/com/example/shapes/spi/package-summary.json" \
    "a second package gets its own package-summary.json"
assert_file_not_exists "$JAVA_OUTPUT_DIR/com.example.shapes/-rectangle/index.json" \
    "Dokka-shaped pages are not written in javadoc mode"

# The global index files javadoc emits at the root of api/.
for f in index.json allclasses-index.json allpackages-index.json deprecated-list.json \
         constant-values.json element-list index-files/index-1.json; do
    assert_file_exists "$JAVA_OUTPUT_DIR/$f" "javadoc index file $f is written"
done

# No HTML is ever produced -- rendering is the downstream template engine's job.
html_count=$(find "$JAVA_OUTPUT_DIR" -name '*.html' 2>/dev/null | wc -l | tr -d ' ')
assert_eq "$html_count" "0" "javadoc mode writes no HTML files"

echo
echo "==> class page content mirrors a javadoc class page"
RECT="$JAVA_OUTPUT_DIR/com/example/shapes/Rectangle.json"
assert_json "$RECT" "d['qualifiedName']" "com.example.shapes.Rectangle" "qualified name"
assert_json "$RECT" "d['signature']" "public class Rectangle extends AbstractShape<Double>" \
    "type signature reads as javadoc prints it"
assert_json "$RECT" "d['superclass']['qualifiedName']" "com.example.shapes.AbstractShape" "superclass"
assert_json "$RECT" "[t['qualifiedName'] for t in d['inheritance']]" \
    "['com.example.shapes.AbstractShape', 'com.example.shapes.Rectangle']" \
    "inheritance tree runs ancestor-first, ending at this type"
assert_json "$RECT" "[t['qualifiedName'] for t in d['allImplementedInterfaces']]" \
    "['com.example.shapes.Shape']" \
    "All Implemented Interfaces closes over the superclass chain"
assert_json "$RECT" "[t['qualifiedName'] for t in d['directKnownSubclasses']]" \
    "['com.example.shapes.Square']" "Direct Known Subclasses"
assert_json "$RECT" "[n['qualifiedName'] for n in d['nestedTypes']]" \
    "['com.example.shapes.Rectangle.Builder']" "nested type summary"

# javadoc has used <init>(...) for constructor anchors since JDK 18, with erased parameter types.
assert_json "$RECT" "sorted(c['anchor'] for c in d['constructors'])" \
    "['<init>()', '<init>(double,double)']" "constructor anchors use javadoc's <init>(...) form"

# A private field with a public getter is a *method* in javadoc, not a field. Dokka merges the
# pair into a Kotlin-style property, so this is the regression guard for unfolding it back.
assert_json "$RECT" "sorted(m['name'] for m in d['methods'])" \
    "['area', 'getHeight', 'getWidth', 'perimeter']" \
    "Java accessors stay methods rather than becoming synthetic properties"
assert_json "$RECT" "sorted(f['name'] for f in d['fields'])" \
    "['EMPTY_LABEL', 'SIDE_COUNT']" "only real fields are listed as fields"

# Overrides / Specified by, derived from erased signatures.
assert_json "$RECT" "[s['declaringType']['qualifiedName'] for m in d['methods'] if m['name']=='area' for s in m['specifiedBy']]" \
    "['com.example.shapes.Shape']" "Specified by points at the declaring interface"
# javadoc groups inherited members per declaring type, interfaces included -- Rectangle inherits
# scaled() from the Shape interface as well as the AbstractShape methods.
assert_json "$RECT" "sorted(g['declaringType']['qualifiedName'] for g in d['inheritedMethods'])" \
    "['com.example.shapes.AbstractShape', 'com.example.shapes.Shape']" \
    "inherited methods are grouped by declaring type, classes and interfaces alike"

# Deprecation carries javadoc's since/forRemoval, not just the comment.
assert_json "$RECT" "[ (m['deprecated']['forRemoval'], m['deprecated']['since']) for m in d['methods'] if m['name']=='perimeter' ]" \
    "[(True, '2.0')]" "@Deprecated(since, forRemoval) is captured"

echo
echo "==> interface, enum, annotation and exception pages"
SHAPE="$JAVA_OUTPUT_DIR/com/example/shapes/Shape.json"
assert_json "$SHAPE" "d['kind']" "interface" "interface kind"
assert_json "$SHAPE" "[m['signature'] for m in d['methods'] if m['name']=='scaled']" \
    "['public default Shape<U> scaled(double factor) throws IllegalArgumentException']" \
    "a non-abstract interface method is recovered as 'default'"
assert_json "$SHAPE" "d['typeParameters'][0]['description'] is not None" "True" \
    "@param <U> is attached to the type parameter"
assert_json "$SHAPE" "d['since']" "['1.0']" "@since is plain text, not a wrapped paragraph"
assert_json "$SHAPE" "d['authors']" "['Docs Pipeline']" "@author is captured"
assert_json "$SHAPE" "d['isFunctionalInterface']" "False" \
    "an interface with two abstract methods is not functional"

FACTORY="$JAVA_OUTPUT_DIR/com/example/shapes/spi/ShapeFactory.json"
assert_json "$FACTORY" "d['isFunctionalInterface']" "True" \
    "an interface with one abstract method is functional"
assert_json "$FACTORY" "[e['type']['qualifiedName'] for e in d['methods'][0]['exceptions']]" \
    "['java.text.ParseException']" "@throws is captured with its resolved type"

CORNER="$JAVA_OUTPUT_DIR/com/example/shapes/Corner.json"
assert_json "$CORNER" "d['kind']" "enum" "enum kind"
assert_json "$CORNER" "[e['name'] for e in d['enumConstants']]" \
    "['TOP_LEFT', 'TOP_RIGHT', 'BOTTOM_LEFT', 'BOTTOM_RIGHT']" \
    "enum constants keep declaration order"

MEASURED="$JAVA_OUTPUT_DIR/com/example/shapes/Measured.json"
assert_json "$MEASURED" "d['kind']" "annotation" "annotation kind"
assert_json "$MEASURED" "d['signature']" "public @interface Measured" "annotation signature"
assert_json "$MEASURED" "sorted(e['name'] for e in d['annotationElements'])" \
    "['tolerance', 'verifiedBy']" "annotation elements are listed"

EXC="$JAVA_OUTPUT_DIR/com/example/shapes/ShapeException.json"
assert_json "$EXC" "d['kind']" "exception" \
    "a Throwable subtype is tabled as an exception, as javadoc does"

echo
echo "==> package, module and global index pages"
PKG="$JAVA_OUTPUT_DIR/com/example/shapes/package-summary.json"
assert_json "$PKG" "[t['name'] for t in d['interfaces']]" "['Shape']" "package interface table"
assert_json "$PKG" "[t['name'] for t in d['exceptions']]" "['ShapeException']" "package exception table"
assert_json "$PKG" "[t['name'] for t in d['annotationTypes']]" "['Measured']" "package annotation table"
# Shape, AbstractShape, Rectangle, Rectangle.Builder, Square, Corner, Measured, ShapeException.
assert_json "$PKG" "len(d['allTypes'])" "8" "allTypes lists every type in the package"

# module-summary.json is built from module-info.java, which Dokka's model does not carry at all --
# these assertions are the guard on that separate parsing path.
MOD="$JAVA_OUTPUT_DIR/module-summary.json"
assert_json "$MOD" "d['name']" "com.example.shapes" \
    "module name comes from module-info.java, not from Dokka's module name"
assert_json "$MOD" "d['since']" "['1.0']" "the module's @since is captured"
assert_json "$MOD" "[(r['module'], r['isTransitive'], r['isStatic']) for r in d['requires']]" \
    "[('java.logging', True, False), ('java.sql', False, True)]" \
    "requires keeps its transitive/static modifiers"
assert_json "$MOD" "[(e['packageName'], e['to']) for e in d['exports']]" \
    "[('com.example.shapes', []), ('com.example.shapes.spi', [])]" "exports directives"
assert_json "$MOD" "[u['qualifiedName'] for u in d['uses']]" \
    "['com.example.shapes.spi.ShapeFactory']" "uses resolves to the documented service type"
assert_json "$MOD" "'<code>module-info.java</code>' in d['description']" "True" \
    "javadoc inline tags in the module comment are rendered"
assert_json "$MOD" "sorted(p['name'] for p in d['packages'])" \
    "['com.example.shapes', 'com.example.shapes.spi']" "module lists its documented packages"

ALL="$JAVA_OUTPUT_DIR/allclasses-index.json"
# The eight in com.example.shapes plus ShapeFactory in com.example.shapes.spi.
assert_json "$ALL" "len(d['types'])" "9" "allclasses-index covers every documented type"
assert_json "$ALL" "[t['url'] for t in d['types'] if t['name']=='Shape']" \
    "['com/example/shapes/Shape.json']" "index links are relative to the index page"

DEP="$JAVA_OUTPUT_DIR/deprecated-list.json"
assert_json "$DEP" "[e['element'] for e in d['sections']['methods']]" \
    "['com.example.shapes.Rectangle.perimeter()']" "deprecated methods are listed"
# Regression guard: the comment is a rendered fragment and must be re-rendered relative to the
# page it lands on, not lifted verbatim off the class page (where the href is just
# "Rectangle.json#getWidth()"). Asserted on the parsed value, since prettyPrint escapes the
# quotes in the raw file.
assert_json "$DEP" "'href=\"com/example/shapes/Rectangle.json#getWidth()\"' in d['sections']['methods'][0]['comment']" \
    "True" "links inside a deprecation comment resolve from the index page"

CONST="$JAVA_OUTPUT_DIR/constant-values.json"
assert_json "$CONST" "sorted(f['value'] for t in d['packages']['com.example.shapes'] for f in t['fields'])" \
    "['\"empty\"', '4', '64']" "constant values are unwrapped to their literals"

assert_contains "$JAVA_OUTPUT_DIR/element-list" "com.example.shapes.spi" \
    "element-list names every documented package"

IDX="$JAVA_OUTPUT_DIR/index-files/index-1.json"
assert_json "$IDX" "d['entries'][0]['url'].startswith('../')" "True" \
    "index-files entries link back out of their own directory"
assert_json "$IDX" "len(d['letters']) > 1" "True" "index pages carry the full letter list"

echo
echo "==> javadoc mode honours the shared output options"
run_dokka_java '{"logLevel":"debug","javadoc-mode":true,"omitNulls":true,"omitFields":["firstSentence"]}'
assert_not_contains "$JAVA_OUTPUT_DIR/com/example/shapes/Rectangle.json" '"firstSentence"' \
    "omitFields strips keys from javadoc-mode pages"
assert_not_contains "$JAVA_OUTPUT_DIR/com/example/shapes/Rectangle.json" '"seeAlso":[]' \
    "omitNulls strips empty values from javadoc-mode pages"
assert_eq "$(line_count "$JAVA_OUTPUT_DIR/com/example/shapes/Rectangle.json")" "1" \
    "prettyPrint off yields compact single-line JSON"

summarize_and_exit
