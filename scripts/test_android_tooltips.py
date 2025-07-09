import unittest
from bs4 import BeautifulSoup
from android_tooltips import analyze_classes, get_detail, normalize_button_uri
import os
import tempfile

class TestAndroidTooltips(unittest.TestCase):
    def test_analyze_classes(self):
        # Create a temporary directory and file for testing
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample HTML content that matches Android classes.html structure
            html_content = """
            <h2 id="letter_A" data-text="A" tabindex="-1">A</h2>
            <table>
                <tr data-version-added="1">
                    <td class="jd-linkcol"><a href="/reference/android/widget/AbsListView">AbsListView</a></td>
                    <td class="jd-descrcol" width="100%">
                      Base class that can be used to implement virtualized lists of items.&nbsp;
                    </td>
                </tr>
                <tr data-version-added="1">
                    <td class="jd-linkcol"><a href="/reference/android/widget/AbsListView.LayoutParams">AbsListView.LayoutParams</a></td>
                    <td class="jd-descrcol" width="100%">
                      AbsListView extends LayoutParams to provide a place to hold the view type.&nbsp;
                    </td>
                </tr>
            </table>
            """
            
            # Write the content to a temporary file
            classes_path = os.path.join(temp_dir, 'classes.html')
            with open(classes_path, 'w') as f:
                f.write(html_content)
            
            # Create individual HTML files for each class
            for class_name in ['AbsListView', 'AbsListView.LayoutParams']:
                class_html = f"""
                <html>
                <head><title>{class_name} Class</title></head>
                <body>
                    <div class="jd-details-descr">
                        <p>This is the detailed description for {class_name}.</p>
                    </div>
                </body>
                </html>
                """
                
                # Create the directory structure
                class_dir = os.path.join(temp_dir, 'reference', 'android', 'widget')
                os.makedirs(class_dir, exist_ok=True)
                
                # Write the class HTML file
                class_path = os.path.join(class_dir, f'{class_name}.html')
                with open(class_path, 'w') as f:
                    f.write(class_html)
            
            # Call the function
            result = analyze_classes(temp_dir)
            
            # Verify the results
            self.assertEqual(len(result), 2)
            self.assertEqual(
                result['AbsListView'][0],  # description
                'Base class that can be used to implement virtualized lists of items.'
            )
            self.assertEqual(
                result['AbsListView'][1],  # url
                '/reference/android/widget/AbsListView'
            )
            self.assertEqual(
                result['AbsListView.LayoutParams'][0],  # description
                'AbsListView extends LayoutParams to provide a place to hold the view type.'
            )
            self.assertEqual(
                result['AbsListView.LayoutParams'][1],  # url
                '/reference/android/widget/AbsListView.LayoutParams'
            )
            
            # Test database insertion
            from android_tooltips import traverse
            db_path = os.path.join(temp_dir, 'test.db')
            
            # Process the classes and insert into database
            traverse(temp_dir, db_path, debug_mode=False)
            
            # Query the database to verify records were inserted
            import sqlite3
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Count total records
            cursor.execute("SELECT COUNT(*) FROM ide_tooltip_table")
            total_count = cursor.fetchone()[0]
            
            # Count records for each specific class
            cursor.execute("SELECT COUNT(*) FROM ide_tooltip_table WHERE tooltipTag = 'AbsListView'")
            abslistview_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM ide_tooltip_table WHERE tooltipTag = 'AbsListView.LayoutParams'")
            layoutparams_count = cursor.fetchone()[0]
            
            # Get sample record to verify content
            cursor.execute("SELECT tooltipSummary FROM ide_tooltip_table WHERE tooltipTag = 'AbsListView'")
            result = cursor.fetchone()
            abslistview_summary = result[0] if result else None
            
            conn.close()
            
            # Assert the counts match what we expect
            self.assertEqual(total_count, 2, f"Expected 2 records, found {total_count}")
            self.assertEqual(abslistview_count, 1, f"Expected 1 AbsListView record, found {abslistview_count}")
            self.assertEqual(layoutparams_count, 1, f"Expected 1 LayoutParams record, found {layoutparams_count}")
            
            # Verify the summary content
            self.assertIsNotNone(abslistview_summary)
            self.assertIn('Base class that can be used to implement virtualized lists of items', abslistview_summary)

    def test_get_detail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample HTML content for Android class detail
            html_content = """
            <div class="jd-details api apilevel-1">
                <h4 class="jd-details-title">
                    <span class="normal">public abstract class</span>
                    <span class="sympad">AbsListView</span>
                    <span class="normal">extends</span>
                    <a href="/reference/android/widget/AdapterView">AdapterView</a>&lt;<a href="/reference/android/widget/ListAdapter">ListAdapter</a>&gt;
                </h4>
                <div class="api-level">
                    <div class="jd-details-descr">
                        <p>Base class that can be used to implement virtualized lists of items. A list does not have a spatial definition here. For instance, subclasses of this class can display the content in a grid, in a carousel, as stack, etc.</p>
                        <p>This class provides a framework for displaying a large data set. The data can come from various sources such as a database or an array. Subclasses of <code>AbsListView</code> are responsible for managing the data, displaying it, and handling user interaction.</p>
                    </div>
                </div>
            </div>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'AbsListView.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Call the function
            result = get_detail(file_path)
            
            # Verify the results
            self.assertIn('Base class that can be used to implement virtualized lists of items', result)
            self.assertIn('This class provides a framework for displaying a large data set', result)
            self.assertIn('Subclasses of <code>AbsListView</code> are responsible for managing the data', result)

    def test_normalize_button_uri(self):
        # Test case 1: Simple relative path
        file_path = "SourceDocs/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/AbsListView.html"
        button_uri = "/reference/android/widget/AdapterView"
        expected = "http://localhost:6174/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/AdapterView.html"
        self.assertEqual(normalize_button_uri(file_path, button_uri), expected)

        # Test case 2: Already absolute path
        file_path = "SourceDocs/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/AbsListView.html"
        button_uri = "http://localhost:6174/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/AdapterView.html"
        expected = "http://localhost:6174/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/AdapterView.html"
        self.assertEqual(normalize_button_uri(file_path, button_uri), expected)

        # Test case 3: Package reference
        file_path = "SourceDocs/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/AbsListView.html"
        button_uri = "/reference/android/widget/package-summary"
        expected = "http://localhost:6174/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/package-summary.html"
        self.assertEqual(normalize_button_uri(file_path, button_uri), expected)

    def test_analyze_classes_with_empty_description(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample HTML content with empty description
            html_content = """
            <table>
                <tr data-version-added="1">
                    <td class="jd-linkcol"><a href="/reference/android/widget/TestClass">TestClass</a></td>
                    <td class="jd-descrcol" width="100%">
                      &nbsp;
                    </td>
                </tr>
            </table>
            """
            
            # Write the content to a temporary file
            classes_path = os.path.join(temp_dir, 'classes.html')
            with open(classes_path, 'w') as f:
                f.write(html_content)
            
            # Call the function
            result = analyze_classes(temp_dir)
            
            # Verify the results - should handle empty descriptions gracefully
            self.assertEqual(len(result), 1)
            self.assertEqual(result['TestClass'][0], '')  # description
            self.assertEqual(result['TestClass'][1], '/reference/android/widget/TestClass')  # url

    def test_analyze_classes_with_multiple_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample HTML content with multiple tables
            html_content = """
            <h2 id="letter_A" data-text="A" tabindex="-1">A</h2>
            <table>
                <tr data-version-added="1">
                    <td class="jd-linkcol"><a href="/reference/android/widget/AbsListView">AbsListView</a></td>
                    <td class="jd-descrcol" width="100%">
                      Base class for virtualized lists.&nbsp;
                    </td>
                </tr>
            </table>
            
            <h2 id="letter_B" data-text="B" tabindex="-1">B</h2>
            <table>
                <tr data-version-added="1">
                    <td class="jd-linkcol"><a href="/reference/android/widget/Button">Button</a></td>
                    <td class="jd-descrcol" width="100%">
                      A push-button widget.&nbsp;
                    </td>
                </tr>
            </table>
            """
            
            # Write the content to a temporary file
            classes_path = os.path.join(temp_dir, 'classes.html')
            with open(classes_path, 'w') as f:
                f.write(html_content)
            
            # Call the function
            result = analyze_classes(temp_dir)
            
            # Verify the results - should process all tables
            self.assertEqual(len(result), 2)
            self.assertEqual(result['AbsListView'][0], 'Base class for virtualized lists.')  # description
            self.assertEqual(result['AbsListView'][1], '/reference/android/widget/AbsListView')  # url
            self.assertEqual(result['Button'][0], 'A push-button widget.')  # description
            self.assertEqual(result['Button'][1], '/reference/android/widget/Button')  # url

    def test_debug_mode_limiting(self):
        """Test that debug mode correctly limits processing to first 10 items"""
        from android_tooltips import traverse
        import tempfile
        import sqlite3
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create sample HTML content with more than 10 items
            html_content = """
            <table>
            """
            
            # Add 15 items to the table
            for i in range(15):
                html_content += f"""
                <tr data-version-added="1">
                    <td class="jd-linkcol"><a href="/reference/android/widget/TestClass{i}">TestClass{i}</a></td>
                    <td class="jd-descrcol" width="100%">
                      Test class {i}.&nbsp;
                    </td>
                </tr>
                """
            
            html_content += """
            </table>
            """
            
            # Write the content to a temporary file
            classes_path = os.path.join(temp_dir, 'classes.html')
            with open(classes_path, 'w') as f:
                f.write(html_content)
            
            # Create individual HTML files for each class
            for i in range(15):
                class_html = f"""
                <html>
                <head><title>TestClass{i} Class</title></head>
                <body>
                    <div class="jd-details-descr">
                        <p>This is the detailed description for TestClass{i}.</p>
                    </div>
                </body>
                </html>
                """
                
                # Create the directory structure
                class_dir = os.path.join(temp_dir, 'reference', 'android', 'widget')
                os.makedirs(class_dir, exist_ok=True)
                
                # Write the class HTML file
                class_path = os.path.join(class_dir, f'TestClass{i}.html')
                with open(class_path, 'w') as f:
                    f.write(class_html)
            
            # Create a temporary database file
            db_path = os.path.join(temp_dir, 'test.db')
            
            # Test debug mode (should process only first 10)
            traverse(temp_dir, db_path, debug_mode=True)
            
            # Check database contents
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ide_tooltip_table")
            count = cursor.fetchone()[0]
            conn.close()
            
            # Should have exactly 10 items in debug mode
            self.assertEqual(count, 10)
            
            # Test normal mode (should process all 15)
            db_path2 = os.path.join(temp_dir, 'test2.db')
            traverse(temp_dir, db_path2, debug_mode=False)
            
            # Check database contents
            conn = sqlite3.connect(db_path2)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ide_tooltip_table")
            count2 = cursor.fetchone()[0]
            conn.close()
            
            # Should have all 15 items in normal mode
            self.assertEqual(count2, 15)

if __name__ == '__main__':
    unittest.main()
