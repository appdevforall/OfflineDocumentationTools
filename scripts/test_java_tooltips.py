import unittest
from bs4 import BeautifulSoup
from java_tooltips import analyze_classes, get_detail, normalize_button_uri
import os
import tempfile

class TestJavaTooltips(unittest.TestCase):
    def test_analyze_classes(self):
        # Create a temporary directory and file for testing
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample HTML content
            html_content = """
            <div id="all-classes-table.tabpanel" role="tabpanel">
                <div class="summary-table two-column-summary">
                    <div class="table-header col-first">Class</div>
                    <div class="table-header col-last">Description</div>
                    <div class="col-first even-row-color all-classes-table">
                        <a href="java.desktop/java/awt/desktop/AboutEvent.html">AboutEvent</a>
                    </div>
                    <div class="col-last even-row-color all-classes-table">
                        <div class="block">Event sent when the application is asked to open its about window.</div>
                    </div>
                    <div class="col-first odd-row-color all-classes-table">
                        <a href="jdk.net/jdk/net/Sockets.html">Sockets</a>
                    </div>
                    <div class="col-last odd-row-color all-classes-table">
                        Deprecated.
                        <div class="deprecation-comment">Java SE 9 added standard methods to set/get socket options, and retrieve the per-Socket supported options effectively rendering this API redundant.</div>
                    </div>
                </div>
            </div>
            """
            
            # Write the content to a temporary file
            index_path = os.path.join(temp_dir, 'allclasses-index.html')
            with open(index_path, 'w') as f:
                f.write(html_content)
            
            # Call the function
            result = analyze_classes(temp_dir)
            
            # Verify the results
            self.assertEqual(len(result), 2)
            self.assertEqual(
                result['java.desktop/java/awt/desktop/AboutEvent.html'],
                'Event sent when the application is asked to open its about window.'
            )
            self.assertEqual(
                result['jdk.net/jdk/net/Sockets.html'],
                'Deprecated. Java SE 9 added standard methods to set/get socket options, and retrieve the per-Socket supported options effectively rendering this API redundant.'
            )

    def test_get_detail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample HTML content
            html_content = """
            <section class="class-description" id="class-description">
                <hr>
                <div class="type-signature">
                    <span class="modifiers">public final class </span>
                    <span class="element-name type-name-label">JarSigner</span>
                    <span class="extends-implements">extends <a href="../../../../java.base/java/lang/Object.html">Object</a></span>
                </div>
                <div class="block">An immutable utility class to sign a jar file.
                <p>
                A caller creates a <code>JarSigner.Builder</code> object, (optionally) sets
                some parameters, and calls <a href="JarSigner.Builder.html#build()"><code>build</code></a> to create
                a <code>JarSigner</code> object.</div>
                <dl class="notes">
                    <dt>Since:</dt>
                    <dd>9</dd>
                </dl>
            </section>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'JarSigner.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Call the function
            result = get_detail(file_path)
            
            # Verify the results
            self.assertIn('An immutable utility class to sign a jar file', result)
            self.assertIn('A caller creates a JarSigner.Builder object', result)
            self.assertIn('Since: 9', result)
            # Verify type signature is excluded
            self.assertNotIn('public final class JarSigner', result)

    def test_normalize_button_uri(self):
        # Test case 1: Simple relative path
        file_path = "SourceDocs/JavaDocs/html/api/java.management.rmi/javax/management/remote/rmi/RMIIIOPServerImpl.html"
        button_uri = "../../../../module-summary.html"
        expected = "http://localhost:6174/JavaDoc/html/api/java.management.rmi/module-summary.html"
        self.assertEqual(normalize_button_uri(file_path, button_uri), expected)

        # Test case 2: No parent directory references
        file_path = "SourceDocs/JavaDocs/html/api/java.base/java/lang/Object.html"
        button_uri = "module-summary.html"
        expected = "http://localhost:6174/JavaDoc/html/api/java.base/module-summary.html"
        self.assertEqual(normalize_button_uri(file_path, button_uri), expected)

        # Test case 3: Multiple parent directory references
        file_path = "SourceDocs/JavaDocs/html/api/java.base/java/lang/reflect/AnnotatedElement.html"
        button_uri = "../../../../../../module-summary.html"
        expected = "http://localhost:6174/JavaDoc/html/api/java.base/module-summary.html"
        self.assertEqual(normalize_button_uri(file_path, button_uri), expected)

if __name__ == '__main__':
    unittest.main() 