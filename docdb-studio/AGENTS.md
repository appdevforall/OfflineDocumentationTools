
Always use "uv" to manage Python libraries and dependencies.

Follow PEP8 style


Project Overview
A Python-based desktop/web application using the Flet framework for the GUI and SQLite3 for local data management. The goal is to provide a clean interface for CRUD operations on a relational database.

Tech Stack
Language: Python 3.x

GUI Framework: Flet (Flutter-based)

Database: SQLite3 (Standard Library)

Pattern: Model-View-Controller (MVC) or Functional State Management

Architecture Guidelines
1. Flet GUI (The View)
Componentization: Break UI into reusable classes (e.g., class TodoItem(ft.Column):).

Async Support: Use asyncio for database calls to prevent UI freezing.

State Management: Prefer passing state through control properties or a centralized state class rather than global variables.

2. SQLite3 (The Model)
Connection Handling: Use a context manager (with sqlite3.connect(...)) or a dedicated DatabaseManager class to ensure connections are closed.

Schema: Keep SQL schemas in a separate schema.sql file or a dedicated init_db() function. Never change the schema.

Security: Always use parameterized queries (?) to prevent SQL injection.

Coding Standards
Naming: snake_case for variables and functions; PascalCase for Flet component classes.

Typing: Use Python type hints (ft.Page, sqlite3.Cursor, etc.) to help the agent provide better completions.

Error Handling: Wrap DB operations in try-except blocks and display errors to the user via ft.SnackBar.
