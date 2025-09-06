import pytest
import tempfile
import sqlite3
from pathlib import Path
import sys
import json
import os
sys.path.append(str(Path(__file__).parent.parent))

from db_health_checker import DatabaseHealthChecker, HealthIssue


class TestDatabaseHealthChecker:
    """Test suite for DatabaseHealthChecker."""
    
    def test_should_detect_empty_content_records(self):
        """Test that empty content records are detected."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # Should find 1 empty content record (as we discovered earlier)
        empty_content_issues = [i for i in issues if "empty or NULL content" in i.message]
        assert len(empty_content_issues) == 1
        assert empty_content_issues[0].details['count'] == 1
    
    def test_should_detect_orphaned_tooltip_button_references(self):
        """Test that orphaned tooltip button references are detected."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # Should find orphaned tooltipId references (we found 1,794 earlier)
        orphaned_tooltip_issues = [i for i in issues if "invalid tooltipId references" in i.message]
        assert len(orphaned_tooltip_issues) == 1
        assert orphaned_tooltip_issues[0].details['count'] == 1794
    
    def test_should_detect_broken_uri_references(self):
        """Test that broken URI references are detected."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # Should find broken URI references (we found 1,669 after fixing hash handling)
        broken_uri_issues = [i for i in issues if "broken URI references" in i.message]
        assert len(broken_uri_issues) == 1
        assert broken_uri_issues[0].details['count'] == 1669
    
    def test_should_pass_foreign_key_checks_for_valid_references(self):
        """Test that valid foreign key references pass checks."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # These should NOT have issues (we verified they were clean)
        language_ref_issues = [i for i in issues if "invalid languageID references" in i.message]
        content_type_ref_issues = [i for i in issues if "invalid contentTypeID references" in i.message]
        category_ref_issues = [i for i in issues if "invalid categoryId references" in i.message]
        button_number_ref_issues = [i for i in issues if "invalid buttonNumberId references" in i.message]
        
        assert len(language_ref_issues) == 0
        assert len(content_type_ref_issues) == 0
        assert len(category_ref_issues) == 0
        assert len(button_number_ref_issues) == 0
    
    def test_should_pass_null_field_checks(self):
        """Test that NOT NULL fields are properly populated."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # These should NOT have issues (we verified they were clean)
        null_tooltip_issues = [i for i in issues if "NULL summary or detail" in i.message]
        null_button_issues = [i for i in issues if "NULL description or uri" in i.message]
        
        assert len(null_tooltip_issues) == 0
        assert len(null_button_issues) == 0
    
    def test_should_detect_all_required_tables(self):
        """Test that all required tables are present."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # Should not have any missing table issues
        missing_table_issues = [i for i in issues if "Required table" in i.message and "is missing" in i.message]
        assert len(missing_table_issues) == 0
    
    def test_should_categorize_issues_properly(self):
        """Test that issues are properly categorized."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # Check that we have issues in expected categories
        categories = {issue.category for issue in issues}
        expected_categories = {'data_quality', 'referential_integrity', 'business_logic'}
        assert categories.issubset(expected_categories)
        
        # All issues should be errors (not warnings or info)
        severities = {issue.severity for issue in issues}
        assert severities == {'error'}
    
    def test_should_provide_detailed_issue_information(self):
        """Test that issues provide detailed information."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        for issue in issues:
            assert issue.message is not None
            assert issue.category is not None
            assert issue.severity is not None
            assert issue.details is not None
            assert isinstance(issue.details, dict)
    
    def test_should_handle_nonexistent_database(self):
        """Test that the checker handles nonexistent database files gracefully."""
        checker = DatabaseHealthChecker("nonexistent.db")
        issues = checker.check_all()
        
        # SQLite creates empty databases, so we should get missing table issues
        missing_table_issues = [i for i in issues if "Required table" in i.message and "is missing" in i.message]
        assert len(missing_table_issues) > 0
        
        # Should include all missing required tables
        missing_tables = {issue.details['missing_table'] for issue in missing_table_issues}
        expected_missing = {'Content', 'Tooltips', 'TooltipButtons', 'Languages', 'ContentTypes', 'TooltipCategories', 'TooltipButtonNumbers', 'LastChange'}
        assert missing_tables == expected_missing
    
    def test_should_work_with_healthy_database(self):
        """Test that a healthy database passes all checks."""
        # Create a minimal healthy database
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_file:
            db_path = tmp_file.name
        
        try:
            # Create a healthy database structure
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                
                # Create required tables
                cursor.execute("""
                    CREATE TABLE Languages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        value TEXT NOT NULL UNIQUE
                    )
                """)
                cursor.execute("""
                    CREATE TABLE ContentTypes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        value TEXT NOT NULL UNIQUE,
                        compression TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE TooltipCategories (
                        id INTEGER PRIMARY KEY,
                        category TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE Content (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT NOT NULL,
                        languageID INTEGER NOT NULL,
                        content BLOB NOT NULL,
                        contentTypeID INTEGER NOT NULL,
                        FOREIGN KEY (languageID) REFERENCES Languages(id),
                        FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE Tooltips (
                        id INTEGER PRIMARY KEY,
                        categoryId INTEGER NOT NULL,
                        tag TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        UNIQUE (categoryId, tag),
                        FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE TooltipButtonNumbers (
                        id INTEGER UNIQUE
                    )
                """)
                cursor.execute("""
                    CREATE TABLE TooltipButtons (
                        tooltipId INTEGER,
                        buttonNumberId INTEGER,
                        description TEXT,
                        uri TEXT,
                        FOREIGN KEY(tooltipId) REFERENCES Tooltips(id),
                        FOREIGN KEY(buttonNumberId) REFERENCES TooltipButtonNumbers(id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE LastChange (
                        now TIMESTAMP,
                        who TEXT
                    )
                """)
                
                # Insert minimal valid data
                cursor.execute("INSERT INTO Languages (value) VALUES ('en-US')")
                cursor.execute("INSERT INTO ContentTypes (value, compression) VALUES ('text/plain', 'none')")
                cursor.execute("INSERT INTO TooltipCategories (id, category) VALUES (1, 'test')")
                cursor.execute("INSERT INTO TooltipButtonNumbers (id) VALUES (1)")
                cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES ('test.txt', 1, 'test content', 1)")
                cursor.execute("INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (1, 1, 'test', 'summary', 'detail')")
                cursor.execute("INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (1, 1, 'test button', 'test.txt')")
                
                conn.commit()
            
            # Test the healthy database
            checker = DatabaseHealthChecker(db_path)
            issues = checker.check_all()
            
            # Should have no issues
            assert len(issues) == 0
            
        finally:
            # Clean up
            Path(db_path).unlink(missing_ok=True)
    
    def test_should_detect_missing_tables(self):
        """Test that missing tables are detected."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_file:
            db_path = tmp_file.name
        
        try:
            # Create database with missing tables
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE Content (id INTEGER PRIMARY KEY)")
                conn.commit()
            
            checker = DatabaseHealthChecker(db_path)
            issues = checker.check_all()
            
            # Should detect missing tables
            missing_table_issues = [i for i in issues if "Required table" in i.message and "is missing" in i.message]
            assert len(missing_table_issues) > 0
            
            # Should include all missing required tables
            missing_tables = {issue.details['missing_table'] for issue in missing_table_issues}
            expected_missing = {'Tooltips', 'TooltipButtons', 'Languages', 'ContentTypes', 'TooltipCategories', 'TooltipButtonNumbers', 'LastChange'}
            assert missing_tables == expected_missing
            
        finally:
            Path(db_path).unlink(missing_ok=True)
    
    def test_should_export_issues_to_log_file(self):
        """Test that issues can be exported to a log file."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # Export to log file
        log_file = checker.export_issues_to_log()
        
        # Verify log file exists
        assert os.path.exists(log_file)
        
        # Verify log file contains valid JSON
        with open(log_file, 'r') as f:
            log_data = json.load(f)
        
        # Verify log structure
        assert 'timestamp' in log_data
        assert 'database_path' in log_data
        assert 'total_issues' in log_data
        assert 'issues_by_category' in log_data
        assert 'detailed_issues' in log_data
        
        # Verify total issues count
        assert log_data['total_issues'] == len(issues)
        
        # Verify issues by category
        assert 'business_logic' in log_data['issues_by_category']
        assert 'referential_integrity' in log_data['issues_by_category']
        assert 'data_quality' in log_data['issues_by_category']
        
        # Verify detailed issues contain broken URI information
        business_logic_issues = [i for i in log_data['detailed_issues'] if i['category'] == 'business_logic']
        assert len(business_logic_issues) > 0
        
        # Check if broken_uris field exists in business logic issues
        broken_uri_issues = [i for i in business_logic_issues if 'broken_uris' in i]
        assert len(broken_uri_issues) > 0
        
        # Verify broken URIs contain expected fields
        if broken_uri_issues:
            broken_uris = broken_uri_issues[0]['broken_uris']
            assert len(broken_uris) > 0
            assert all(key in broken_uris[0] for key in ['tooltip_id', 'button_description', 'broken_uri'])
        
        # Clean up
        os.remove(log_file)
    
    def test_should_export_issues_to_specified_log_file(self):
        """Test that issues can be exported to a specified log file."""
        checker = DatabaseHealthChecker("documentation.db")
        issues = checker.check_all()
        
        # Export to specific log file
        custom_log_file = "custom_test_log.json"
        log_file = checker.export_issues_to_log(custom_log_file)
        
        # Verify log file exists and has correct name
        assert log_file == custom_log_file
        assert os.path.exists(custom_log_file)
        
        # Clean up
        os.remove(custom_log_file)
