# Web Scraper

A Go program to recursively scrape websites and save files locally while maintaining the original directory structure.

## Features

- **Recursive Scraping**: Follows links within the same domain
- **Directory Structure Preservation**: Maintains the original URL path structure locally
- **Parallel Processing**: Optional multi-threaded scraping for improved performance
- **Concurrent Safety**: Thread-safe implementation with mutex protection
- **Duplicate Prevention**: Avoids re-scraping already visited URLs
- **Idempotent Operation**: Skips existing files for efficient resuming
- **Flexible Output**: Configurable output directory
- **Test-Driven Development**: Comprehensive test suite

## Installation

1. Ensure you have Go installed (via flox or other package manager)
2. Install dependencies:
   ```bash
   go mod tidy
   ```

## Usage

### Command Line Interface

```bash
# Basic usage - scrape a website to current directory
./webscraper http://example.com

# Specify output directory
./webscraper -output ./downloaded http://example.com

# Use parallel scraping for faster performance
./webscraper -parallel http://example.com

# Use parallel scraping with custom number of workers
./webscraper -parallel -workers 8 http://example.com

# Resume interrupted scraping (idempotent)
./webscraper -output ./downloaded http://example.com

# Show help
./webscraper -h
```

### Programmatic Usage

```go
scraper := NewScraper("./output")
err := scraper.ScrapeURL("http://example.com")
if err != nil {
    log.Fatal(err)
}
```

## How It Works

1. **URL Processing**: The scraper starts with a given URL and parses it
2. **File Existence Check**: Checks if the target file already exists locally
3. **Content Download**: Downloads the content of each page (skips if already exists)
4. **Local Storage**: Saves files locally maintaining the directory structure:
   - `http://example.com/a/b/c.html` → `./a/b/c.html`
   - `http://example.com/` → `./index.html`
   - `http://example.com/subdir/` → `./subdir/index.html`
5. **Link Extraction**: For HTML files, extracts all `<a href>` links
6. **Recursive Scraping**: Follows internal links within the same domain
7. **Duplicate Prevention**: Tracks visited URLs to avoid infinite loops

## File Structure

```
scrape/
├── main.go          # Command-line interface
├── scraper.go       # Core scraper implementation
├── scraper_test.go  # Comprehensive test suite
├── go.mod           # Go module definition
├── go.sum           # Dependency checksums
└── README.md        # This file
```

## Testing

Run the test suite:

```bash
go test -v
```

The tests include:
- **Integration Tests**: Full scraping workflow with mock HTTP server
- **Unit Tests**: Individual function testing for URL normalization, link extraction, etc.
- **Edge Cases**: Handling of various URL formats and edge cases

## Dependencies

- `github.com/PuerkitoBio/goquery`: HTML parsing and link extraction
- `golang.org/x/net`: Enhanced networking capabilities

## Example Output

When scraping `http://example.com`:

```
Starting to scrape: http://example.com
Output directory: ./downloaded
Saved: ./downloaded/index.html
Saved: ./downloaded/page1.html
Saved: ./downloaded/subdir/page2.html
Scraping completed successfully!
```

**Resuming an interrupted scrape:**

```
Starting to scrape: http://example.com
Output directory: ./downloaded
Skipping existing file: ./downloaded/index.html
Skipping existing file: ./downloaded/page1.html
Saved: ./downloaded/subdir/page2.html
Scraping completed successfully!
```

The scraper emits different messages:
- `"Saved: {path}"` - when a new file is downloaded
- `"Skipping existing file: {path}"` - when a file already exists and is skipped

## Limitations

- Only scrapes HTTP/HTTPS URLs (not FTP, file://, etc.)
- Only follows links within the same domain and scheme
- Skips non-HTML content for link extraction
- No rate limiting (be respectful to servers)
- No authentication support
- File existence check is based on local file system (no content validation)

## Development

This project follows Test-Driven Development (TDD) principles:
1. **Red**: Write failing tests
2. **Green**: Implement code to make tests pass
3. **Refactor**: Improve code while keeping tests green

## License

This project is open source and available under the MIT License. 