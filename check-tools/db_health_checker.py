import sqlite3
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from pathlib import Path
import json
from datetime import datetime


@dataclass
class HealthIssue:
    """Represents a database health issue found during validation."""
    severity: str  # 'error', 'warning', 'info'
    category: str  # 'schema', 'integrity', 'data_quality', etc.
    message: str
    details: Dict[str, Any] = None


class DatabaseHealthChecker:
    """Validates the health of a documentation.db file."""
    
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.issues: List[HealthIssue] = []
        self._existing_tables: set = None
    
    def _get_existing_tables(self) -> set:
        """Get the set of existing tables in the database."""
        if self._existing_tables is None:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                self._existing_tables = {row[0] for row in cursor.fetchall()}
        return self._existing_tables
    
    def _table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the database."""
        return table_name in self._get_existing_tables()
    
    def check_all(self) -> List[HealthIssue]:
        """Run all health checks and return issues found."""
        self.issues = []
        
        # Schema integrity checks
        self._check_table_existence()
        
        # Only run data checks if all required tables exist
        if self._all_required_tables_exist():
            self._check_required_columns()
            self._check_required_fields_not_null()
            self._check_content_not_empty()
            self._check_foreign_key_integrity()
            self._check_uri_consistency_with_hash_handling()  # Use the new hash-handling version
            self._check_unique_constraints()
        
        return self.issues
    
    def export_issues_to_log(self, log_file_path: str = None) -> str:
        """Export detailed issue information to a log file."""
        if log_file_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file_path = f"db_health_log_{timestamp}.json"
        
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "database_path": str(self.db_path),
            "total_issues": len(self.issues),
            "issues_by_category": {},
            "detailed_issues": []
        }
        
        # Group issues by category
        for issue in self.issues:
            category = issue.category
            if category not in log_data["issues_by_category"]:
                log_data["issues_by_category"][category] = []
            log_data["issues_by_category"][category].append({
                "severity": issue.severity,
                "message": issue.message,
                "details": issue.details
            })
            
            # Add detailed information for business logic errors
            if category == "business_logic" and "broken URI references" in issue.message:
                detailed_info = self._get_broken_uri_details()
                log_data["detailed_issues"].append({
                    "category": category,
                    "message": issue.message,
                    "details": issue.details,
                    "broken_uris": detailed_info
                })
            elif category == "referential_integrity" and "invalid tooltipId references" in issue.message:
                detailed_info = self._get_orphaned_tooltip_button_details()
                log_data["detailed_issues"].append({
                    "category": category,
                    "message": issue.message,
                    "details": issue.details,
                    "orphaned_buttons": detailed_info
                })
            else:
                log_data["detailed_issues"].append({
                    "category": category,
                    "message": issue.message,
                    "details": issue.details
                })
        
        # Write to log file
        with open(log_file_path, 'w') as f:
            json.dump(log_data, f, indent=2)
        
        return log_file_path
    
    def _get_broken_uri_details(self) -> List[Dict[str, Any]]:
        """Get detailed information about broken URI references."""
        if not self._table_exists('TooltipButtons') or not self._table_exists('Content'):
            return []
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Get all content paths
            cursor.execute("SELECT path FROM Content")
            content_paths = {row[0] for row in cursor.fetchall()}
            
            cursor.execute("""
                SELECT 
                    tb.tooltipId,
                    tb.buttonNumberId,
                    tb.description,
                    tb.uri,
                    t.summary as tooltip_summary,
                    t.detail as tooltip_detail,
                    tc.category as tooltip_category
                FROM TooltipButtons tb
                LEFT JOIN Tooltips t ON tb.tooltipId = t.id
                LEFT JOIN TooltipCategories tc ON t.categoryId = tc.id
                WHERE tb.uri IS NOT NULL
                ORDER BY tb.uri, tb.tooltipId
            """)
            
            broken_uris = []
            for row in cursor.fetchall():
                tooltip_id, button_number_id, description, uri, tooltip_summary, tooltip_detail, tooltip_category = row
                
                # Strip hash anchor from URI (like a real HTTP server would)
                uri_without_hash = uri.split('#')[0] if '#' in uri else uri
                
                # Strip whitespace (both leading and trailing)
                uri_without_hash = uri_without_hash.strip()
                
                if uri_without_hash not in content_paths:
                    broken_uris.append({
                        'tooltip_id': tooltip_id,
                        'button_number_id': button_number_id,
                        'button_description': description,
                        'broken_uri': uri_without_hash,
                        'tooltip_summary': tooltip_summary,
                        'tooltip_detail': tooltip_detail,
                        'tooltip_category': tooltip_category
                    })
            
            return broken_uris
    
    def _get_orphaned_tooltip_button_details(self) -> List[Dict[str, Any]]:
        """Get detailed information about orphaned tooltip button references."""
        if not self._table_exists('TooltipButtons') or not self._table_exists('Tooltips'):
            return []
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    tb.tooltipId,
                    tb.buttonNumberId,
                    tb.description,
                    tb.uri
                FROM TooltipButtons tb
                WHERE tb.tooltipId NOT IN (SELECT id FROM Tooltips)
                ORDER BY tb.tooltipId
            """)
            
            orphaned_buttons = []
            for row in cursor.fetchall():
                orphaned_buttons.append({
                    "tooltip_id": row[0],
                    "button_number_id": row[1],
                    "button_description": row[2],
                    "uri": row[3]
                })
            
            return orphaned_buttons
    
    def _all_required_tables_exist(self) -> bool:
        """Check if all required tables exist."""
        required_tables = {
            'Content', 'Tooltips', 'TooltipButtons', 'Languages', 
            'ContentTypes', 'TooltipCategories', 'TooltipButtonNumbers', 'LastChange'
        }
        existing_tables = self._get_existing_tables()
        return required_tables.issubset(existing_tables)
    
    def _check_table_existence(self):
        """Check that all required tables exist."""
        required_tables = {
            'Content', 'Tooltips', 'TooltipButtons', 'Languages', 
            'ContentTypes', 'TooltipCategories', 'TooltipButtonNumbers', 'LastChange'
        }
        
        existing_tables = self._get_existing_tables()
        missing_tables = required_tables - existing_tables
        
        for table in missing_tables:
            self.issues.append(HealthIssue(
                severity='error',
                category='schema',
                message=f"Required table '{table}' is missing",
                details={'missing_table': table}
            ))
    
    def _check_required_columns(self):
        """Check that required columns exist in each table."""
        # This is a basic implementation - could be expanded with full schema validation
        pass
    
    def _check_required_fields_not_null(self):
        """Check that NOT NULL columns actually contain data."""
        if not self._table_exists('Tooltips') or not self._table_exists('TooltipButtons'):
            return
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Check Tooltips table
            cursor.execute("""
                SELECT COUNT(*) FROM Tooltips 
                WHERE summary IS NULL OR detail IS NULL
            """)
            null_tooltips = cursor.fetchone()[0]
            if null_tooltips > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='data_completeness',
                    message=f"Found {null_tooltips} tooltips with NULL summary or detail",
                    details={'table': 'Tooltips', 'count': null_tooltips}
                ))
            
            # Check TooltipButtons table
            cursor.execute("""
                SELECT COUNT(*) FROM TooltipButtons 
                WHERE description IS NULL OR uri IS NULL
            """)
            null_buttons = cursor.fetchone()[0]
            if null_buttons > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='data_completeness',
                    message=f"Found {null_buttons} tooltip buttons with NULL description or uri",
                    details={'table': 'TooltipButtons', 'count': null_buttons}
                ))
    
    def _check_content_not_empty(self):
        """Check that Content table has no empty content."""
        if not self._table_exists('Content'):
            return
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM Content 
                WHERE content IS NULL OR LENGTH(content) = 0
            """)
            empty_content = cursor.fetchone()[0]
            if empty_content > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='data_quality',
                    message=f"Found {empty_content} content records with empty or NULL content",
                    details={'table': 'Content', 'count': empty_content}
                ))
    
    def _check_foreign_key_integrity(self):
        """Check foreign key integrity across all tables."""
        if not all(self._table_exists(table) for table in ['Content', 'Languages', 'ContentTypes', 'Tooltips', 'TooltipCategories', 'TooltipButtons', 'TooltipButtonNumbers']):
            return
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Check Content.languageID references
            cursor.execute("""
                SELECT COUNT(*) FROM Content 
                WHERE languageID NOT IN (SELECT id FROM Languages)
            """)
            orphaned_language_refs = cursor.fetchone()[0]
            if orphaned_language_refs > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='referential_integrity',
                    message=f"Found {orphaned_language_refs} content records with invalid languageID references",
                    details={'table': 'Content', 'column': 'languageID', 'count': orphaned_language_refs}
                ))
            
            # Check Content.contentTypeID references
            cursor.execute("""
                SELECT COUNT(*) FROM Content 
                WHERE contentTypeID NOT IN (SELECT id FROM ContentTypes)
            """)
            orphaned_content_type_refs = cursor.fetchone()[0]
            if orphaned_content_type_refs > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='referential_integrity',
                    message=f"Found {orphaned_content_type_refs} content records with invalid contentTypeID references",
                    details={'table': 'Content', 'column': 'contentTypeID', 'count': orphaned_content_type_refs}
                ))
            
            # Check Tooltips.categoryId references
            cursor.execute("""
                SELECT COUNT(*) FROM Tooltips 
                WHERE categoryId NOT IN (SELECT id FROM TooltipCategories)
            """)
            orphaned_category_refs = cursor.fetchone()[0]
            if orphaned_category_refs > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='referential_integrity',
                    message=f"Found {orphaned_category_refs} tooltips with invalid categoryId references",
                    details={'table': 'Tooltips', 'column': 'categoryId', 'count': orphaned_category_refs}
                ))
            
            # Check TooltipButtons.tooltipId references
            cursor.execute("""
                SELECT COUNT(*) FROM TooltipButtons 
                WHERE tooltipId NOT IN (SELECT id FROM Tooltips)
            """)
            orphaned_tooltip_refs = cursor.fetchone()[0]
            if orphaned_tooltip_refs > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='referential_integrity',
                    message=f"Found {orphaned_tooltip_refs} tooltip buttons with invalid tooltipId references",
                    details={'table': 'TooltipButtons', 'column': 'tooltipId', 'count': orphaned_tooltip_refs}
                ))
            
            # Check TooltipButtons.buttonNumberId references
            cursor.execute("""
                SELECT COUNT(*) FROM TooltipButtons 
                WHERE buttonNumberId NOT IN (SELECT id FROM TooltipButtonNumbers)
            """)
            orphaned_button_number_refs = cursor.fetchone()[0]
            if orphaned_button_number_refs > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='referential_integrity',
                    message=f"Found {orphaned_button_number_refs} tooltip buttons with invalid buttonNumberId references",
                    details={'table': 'TooltipButtons', 'column': 'buttonNumberId', 'count': orphaned_button_number_refs}
                ))
    
    def _check_uri_consistency_with_hash_handling(self):
        """Check that TooltipButtons.uri references exist in Content.path, handling hash anchors."""
        if not self._table_exists('TooltipButtons') or not self._table_exists('Content'):
            return
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Get all content paths
            cursor.execute("SELECT path FROM Content")
            content_paths = {row[0] for row in cursor.fetchall()}
            
            # Get all tooltip button URIs
            cursor.execute("""
                SELECT 
                    tb.tooltipId,
                    tb.buttonNumberId,
                    tb.description,
                    tb.uri
                FROM TooltipButtons tb
                WHERE tb.uri IS NOT NULL
            """)
            
            broken_uri_refs = 0
            for row in cursor.fetchall():
                tooltip_id, button_number_id, description, uri = row
                
                # Strip hash anchor from URI (like a real HTTP server would)
                uri_without_hash = uri.split('#')[0] if '#' in uri else uri
                
                # Strip whitespace (both leading and trailing)
                uri_without_hash = uri_without_hash.strip()
                
                if uri_without_hash not in content_paths:
                    broken_uri_refs += 1
            
            if broken_uri_refs > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='business_logic',
                    message=f"Found {broken_uri_refs} tooltip buttons with broken URI references (after removing hash anchors and whitespace)",
                    details={'table': 'TooltipButtons', 'column': 'uri', 'count': broken_uri_refs}
                ))
    
    def _check_unique_constraints(self):
        """Check that unique constraints are working properly."""
        if not self._table_exists('Tooltips'):
            return
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Check Tooltips unique constraint
            cursor.execute("""
                SELECT COUNT(*) FROM (
                    SELECT categoryId, tag, COUNT(*) as cnt 
                    FROM Tooltips 
                    GROUP BY categoryId, tag 
                    HAVING cnt > 1
                )
            """)
            duplicate_tooltips = cursor.fetchone()[0]
            if duplicate_tooltips > 0:
                self.issues.append(HealthIssue(
                    severity='error',
                    category='data_quality',
                    message=f"Found {duplicate_tooltips} duplicate tooltip entries violating unique constraint",
                    details={'table': 'Tooltips', 'constraint': '(categoryId, tag)', 'count': duplicate_tooltips}
                ))
