# Check Doc DB

A comprehensive database health checker for documentation.db files. This tool validates the integrity and health of SQLite databases containing team documentation.

## Overview

The documentation database contains 3 tiers of documentation:
- **Tier 1 & 2**: Tooltips stored in the "Tooltips" table (summary and detail columns)
- **Tier 3**: Content files stored in the "Content" table, referenced by button URIs in "TooltipButtons"

## Original Database Description

We have a sqlite database which contains all of my team's documentation. There are 3 tiers. The first and second tier are called tooltips, and they are in the "tooltips" table, tier 1 is in the "summary" column, while tier-2 is in the "detail" column. Then there is a normalized "TooltipButtons" table which contains button labels (in text) and button URIs. Those URIs refer to the tier-3 files which are in the "Content" table.

### Database Schema

```sql
sqlite> .schema
CREATE TABLE Content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        languageID INTEGER NOT NULL,
        content BLOB NOT NULL,
        contentTypeID INTEGER NOT NULL,
        FOREIGN KEY (languageID) REFERENCES Languages(id),
        FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id)
    );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE Languages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE
    );
CREATE TABLE ContentTypes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE,
        compression TEXT NOT NULL
    );
CREATE TABLE TooltipCategories (
 id       INTEGER PRIMARY KEY,
 category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 'Tooltips' (
  'id'         INTEGER PRIMARY KEY,
  'categoryId' INTEGER NOT NULL,
  'tag'        TEXT NOT NULL,
  'summary'    TEXT NOT NULL,
  'detail'     TEXT NOT NULL,
  UNIQUE ('categoryId', 'tag'),
  FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id)
);
CREATE TABLE TooltipButtonNumbers (
  'id'   INTEGER UNIQUE -- Manually assigned so buttons show in the order we want.
);
CREATE TABLE TooltipButtons (
  'tooltipId'        INTEGER,
  'buttonNumberId'   INTEGER,
  'description'      TEXT,
  'uri'              TEXT,
  FOREIGN KEY(tooltipId) REFERENCES Tooltips(id),
  FOREIGN KEY(buttonNumberId)  REFERENCES TooltipButtonNumbers(id)
);
CREATE TABLE LastChange (
	now TIMESTAMP,
	who TEXT
);
```

## Features

### Health Checks Performed

1. **Schema Integrity**
   - Validates all required tables exist
   - Checks table structure and constraints

2. **Data Completeness**
   - Ensures NOT NULL columns contain actual data
   - Validates content records are not empty

3. **Referential Integrity**
   - Foreign key relationship validation
   - Orphaned record detection

4. **Business Logic**
   - URI consistency between TooltipButtons and Content
   - Unique constraint validation

5. **Data Quality**
   - Duplicate entry detection
   - Content size validation

### Detailed Error Logging

The tool automatically generates detailed JSON log files containing:

- **Broken URI References**: Complete details of tooltip buttons pointing to non-existent content
  - Tooltip ID and summary
  - Button description and broken URI
  - Tooltip category
  - Tooltip detail

- **Orphaned References**: Details of tooltip buttons referencing non-existent tooltips
  - Tooltip ID
  - Button description and URI
  - Button number ID

- **Summary Statistics**: Categorized issue counts and metadata

## Installation

```bash
# Clone the repository
git clone <repository-url>
cd check-doc-db

# Install dependencies using uv
uv sync
```

## Usage

### Command Line

```bash
# Check the health of documentation.db
uv run python main.py
```

This will:
1. Run all health checks
2. Display a summary of issues found
3. Automatically generate a detailed JSON log file
4. Show what information is available in the log

### Programmatic Usage

```python
from db_health_checker import DatabaseHealthChecker

# Create a health checker instance
checker = DatabaseHealthChecker("path/to/documentation.db")

# Run all health checks
issues = checker.check_all()

# Export detailed issues to log file
log_file = checker.export_issues_to_log("my_issues.json")

# Process the results
for issue in issues:
    print(f"{issue.severity}: {issue.message}")
    print(f"Category: {issue.category}")
    print(f"Details: {issue.details}")
```

### Log File Format

The generated log file contains structured JSON data:

```json
{
  "timestamp": "2025-08-27T18:13:02.978120",
  "database_path": "documentation.db",
  "total_issues": 3,
  "issues_by_category": {
    "business_logic": [...],
    "referential_integrity": [...],
    "data_quality": [...]
  },
  "detailed_issues": [
    {
      "category": "business_logic",
      "message": "Found 1839 tooltip buttons with broken URI references",
      "details": {...},
      "broken_uris": [
        {
          "tooltip_id": 7977,
          "button_number_id": 1,
          "button_description": "View full documentation",
          "broken_uri": "a/android/os/strictmode/CleartextNetworkViolation.html",
          "tooltip_summary": "",
          "tooltip_detail": "No detail found",
          "tooltip_category": "java"
        }
      ]
    }
  ]
}
```

## Testing

The project follows Test-Driven Development (TDD) principles. Run tests with:

```bash
uv run pytest tests/ -v
```

### Test Coverage

- ✅ Schema validation tests
- ✅ Data integrity tests  
- ✅ Foreign key relationship tests
- ✅ Business logic validation tests
- ✅ Edge case handling tests
- ✅ Healthy database validation tests
- ✅ Logging functionality tests

## Current Issues Found

The current documentation.db has the following health issues:

1. **1 empty content record** in the Content table
2. **1,794 orphaned tooltip button references** (TooltipButtons pointing to non-existent tooltips)
3. **1,839 broken URI references** (TooltipButtons pointing to non-existent content)

Detailed information about these issues is automatically logged to JSON files for inspection and analysis.

## Development

### Project Structure

```
check-doc-db/
├── db_health_checker.py    # Main health checker implementation
├── main.py                 # Command line interface
├── tests/                  # Test suite
│   └── test_db_health_checker.py
├── pyproject.toml          # Project configuration
└── README.md              # This file
```

### Adding New Health Checks

1. Add a new private method to `DatabaseHealthChecker`
2. Call it from `check_all()` method
3. Write tests for the new check
4. Follow TDD: Red → Green → Refactor

### Health Issue Structure

```python
@dataclass
class HealthIssue:
    severity: str      # 'error', 'warning', 'info'
    category: str      # 'schema', 'integrity', 'data_quality', etc.
    message: str       # Human-readable description
    details: Dict      # Additional context and data
```

## Contributing

1. Follow TDD principles
2. Write comprehensive tests
3. Ensure all tests pass before submitting
4. Add documentation for new features

## License

[Add your license information here]