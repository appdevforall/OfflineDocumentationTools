import sys
from db_health_checker import DatabaseHealthChecker


def main():
    # Get database path from command line argument or use default
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = "documentation.db"
    
    print(f"Checking {db_path} health...")
    print("=" * 50)
    
    checker = DatabaseHealthChecker(db_path)
    issues = checker.check_all()
    
    if not issues:
        print("✅ No health issues found! Database is healthy.")
        return
    
    print(f"❌ Found {len(issues)} health issue(s):")
    print()
    
    for i, issue in enumerate(issues, 1):
        print(f"{i}. [{issue.severity.upper()}] {issue.category}")
        print(f"   {issue.message}")
        if issue.details:
            print(f"   Details: {issue.details}")
        print()
    
    # Export detailed issues to log file
    log_file = checker.export_issues_to_log()
    print(f"📋 Detailed error information exported to: {log_file}")
    print()
    
    # Show summary of what's in the log
    business_logic_issues = [i for i in issues if i.category == "business_logic"]
    referential_issues = [i for i in issues if i.category == "referential_integrity"]
    
    if business_logic_issues:
        print("🔍 Business logic errors (broken URI references) have been logged with details including:")
        print("   - Tooltip ID and summary")
        print("   - Button description and broken URI")
        print("   - Tooltip category")
        print()
    
    if referential_issues:
        print("🔍 Referential integrity errors (orphaned references) have been logged with details including:")
        print("   - Tooltip ID")
        print("   - Button description and URI")
        print("   - Button number ID")
        print()


if __name__ == "__main__":
    main()
