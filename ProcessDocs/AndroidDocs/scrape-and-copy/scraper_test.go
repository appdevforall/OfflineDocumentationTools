package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestScraper_ScrapeURL(t *testing.T) {
	// Create a test server
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Write([]byte(`
				<html>
					<head><title>Test Page</title></head>
					<body>
						<a href="/page1.html">Page 1</a>
						<a href="/subdir/page2.html">Page 2</a>
						<a href="http://external.com">External</a>
					</body>
				</html>
			`))
		case "/page1.html":
			w.Write([]byte(`<html><body><h1>Page 1 Content</h1></body></html>`))
		case "/subdir/page2.html":
			w.Write([]byte(`<html><body><h1>Page 2 Content</h1></body></html>`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	// Create temporary directory for test
	testDir := t.TempDir()

	scraper := NewScraper(testDir)
	err := scraper.ScrapeURL(server.URL)
	if err != nil {
		t.Fatalf("Failed to scrape URL: %v", err)
	}

	// Check if files were created
	expectedFiles := []string{
		"index.html",
		"page1.html",
		filepath.Join("subdir", "page2.html"),
	}

	for _, file := range expectedFiles {
		filePath := filepath.Join(testDir, file)
		if _, err := os.Stat(filePath); os.IsNotExist(err) {
			t.Errorf("Expected file %s was not created", filePath)
		}
	}
}

func TestScraper_ExtractLinks(t *testing.T) {
	html := `
		<html>
			<body>
				<a href="/page1.html">Page 1</a>
				<a href="/subdir/page2.html">Page 2</a>
				<a href="http://external.com">External</a>
				<a href="mailto:test@example.com">Email</a>
				<a href="#anchor">Anchor</a>
			</body>
		</html>
	`

	scraper := NewScraper("")
	links := scraper.extractLinks(html, "http://example.com")

	expectedLinks := []string{
		"http://example.com/page1.html",
		"http://example.com/subdir/page2.html",
	}

	if len(links) != len(expectedLinks) {
		t.Errorf("Expected %d links, got %d", len(expectedLinks), len(links))
	}

	for _, expected := range expectedLinks {
		found := false
		for _, link := range links {
			if link == expected {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("Expected link %s not found", expected)
		}
	}
}

func TestScraper_NormalizeURL(t *testing.T) {
	scraper := NewScraper("")
	
	tests := []struct {
		baseURL string
		href    string
		expected string
	}{
		{"http://example.com", "/page.html", "http://example.com/page.html"},
		{"http://example.com", "page.html", "http://example.com/page.html"},
		{"http://example.com", "http://example.com/page.html", "http://example.com/page.html"},
		{"http://example.com", "http://external.com/page.html", "http://external.com/page.html"},
		{"http://example.com", "#anchor", ""},
		{"http://example.com", "mailto:test@example.com", ""},
	}

	for _, test := range tests {
		result := scraper.normalizeURL(test.baseURL, test.href)
		if result != test.expected {
			t.Errorf("normalizeURL(%s, %s) = %s, expected %s", 
				test.baseURL, test.href, result, test.expected)
		}
	}
}

func TestScraper_ShouldScrape(t *testing.T) {
	scraper := NewScraper("")
	
	tests := []struct {
		url      string
		baseURL  string
		expected bool
	}{
		{"http://example.com/page.html", "http://example.com", true},
		{"http://example.com/subdir/page.html", "http://example.com", true},
		{"http://external.com/page.html", "http://example.com", false},
		{"https://example.com/page.html", "http://example.com", false},
		{"http://example.com", "http://example.com", true},
		// New cases for exclusion
		{"http://example.com/java/page.html", "http://example.com", false},
		{"http://example.com/kotlin/page.html", "http://example.com", false},
		{"http://example.com/dalvik/page.html", "http://example.com", false},
		{"http://example.com/other/java/page.html", "http://example.com", false},
		{"http://example.com/other/kotlin/page.html", "http://example.com", false},
		{"http://example.com/other/dalvik/page.html", "http://example.com", false},
	}

	for _, test := range tests {
		result := scraper.shouldScrape(test.url, test.baseURL)
		if result != test.expected {
			t.Errorf("shouldScrape(%s, %s) = %t, expected %t", 
				test.url, test.baseURL, result, test.expected)
		}
	}
}

func TestScraper_GetLocalPath(t *testing.T) {
	scraper := NewScraper("/tmp/test")
	
	tests := []struct {
		url      string
		baseURL  string
		expected string
	}{
		{"http://example.com/page.html", "http://example.com", "/tmp/test/page.html"},
		{"http://example.com/", "http://example.com", "/tmp/test/index.html"},
		{"http://example.com/subdir/page.html", "http://example.com", "/tmp/test/subdir/page.html"},
		{"http://example.com/subdir/", "http://example.com", "/tmp/test/subdir/index.html"},
	}

	for _, test := range tests {
		result := scraper.getLocalPath(test.url, test.baseURL)
		if result != test.expected {
			t.Errorf("getLocalPath(%s, %s) = %s, expected %s", 
				test.url, test.baseURL, result, test.expected)
		}
	}
} 

func TestScraper_ParallelScraping(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Write([]byte(`
				<html>
					<body>
						<a href="/a.html">A</a>
						<a href="/b.html">B</a>
						<a href="/c.html">C</a>
					</body>
				</html>
			`))
		case "/a.html":
			w.Write([]byte(`<html><body>A</body></html>`))
		case "/b.html":
			w.Write([]byte(`<html><body>B</body></html>`))
		case "/c.html":
			w.Write([]byte(`<html><body>C</body></html>`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	testDir := t.TempDir()
	scraper := NewScraper(testDir)

	// We'll call a new method ScrapeURLParallel for this test
	err := scraper.ScrapeURLParallel(server.URL, 4) // 4 workers
	if err != nil {
		t.Fatalf("Failed to scrape in parallel: %v", err)
	}

	expectedFiles := []string{
		"index.html",
		"a.html",
		"b.html",
		"c.html",
	}

	for _, file := range expectedFiles {
		filePath := filepath.Join(testDir, file)
		if _, err := os.Stat(filePath); os.IsNotExist(err) {
			t.Errorf("Expected file %s was not created", filePath)
		}
	}
} 

func TestScraper_Idempotent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Write([]byte(`
				<html>
					<body>
						<a href="/page1.html">Page 1</a>
						<a href="/page2.html">Page 2</a>
					</body>
				</html>
			`))
		case "/page1.html":
			w.Write([]byte(`<html><body>Page 1 Content</body></html>`))
		case "/page2.html":
			w.Write([]byte(`<html><body>Page 2 Content</body></html>`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	testDir := t.TempDir()
	scraper := NewScraper(testDir)

	// First run - should download all files
	err := scraper.ScrapeURL(server.URL)
	if err != nil {
		t.Fatalf("Failed to scrape URL: %v", err)
	}

	// Verify files were created
	expectedFiles := []string{"index.html", "page1.html", "page2.html"}
	for _, file := range expectedFiles {
		filePath := filepath.Join(testDir, file)
		if _, err := os.Stat(filePath); os.IsNotExist(err) {
			t.Errorf("Expected file %s was not created", filePath)
		}
	}

	// Second run - should skip existing files
	// We'll modify the server to return different content to verify it's not re-downloading
	server2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Write([]byte(`
				<html>
					<body>
						<a href="/page1.html">Page 1</a>
						<a href="/page2.html">Page 2</a>
					</body>
				</html>
			`))
		case "/page1.html":
			w.Write([]byte(`<html><body>DIFFERENT Page 1 Content</body></html>`))
		case "/page2.html":
			w.Write([]byte(`<html><body>DIFFERENT Page 2 Content</body></html>`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server2.Close()

	// Create a new scraper with the same output directory
	scraper2 := NewScraper(testDir)
	
	// This should skip existing files and not download the "DIFFERENT" content
	err = scraper2.ScrapeURL(server2.URL)
	if err != nil {
		t.Fatalf("Failed to scrape URL on second run: %v", err)
	}

	// Verify the original content is still there (not overwritten)
	content, err := os.ReadFile(filepath.Join(testDir, "page1.html"))
	if err != nil {
		t.Fatalf("Failed to read page1.html: %v", err)
	}
	if strings.Contains(string(content), "DIFFERENT") {
		t.Error("File was re-downloaded when it should have been skipped")
	}
	if !strings.Contains(string(content), "Page 1 Content") {
		t.Error("Original content was lost")
	}
} 

func TestCircularLinkDetection(t *testing.T) {
	// Create a temporary directory for testing
	tempDir := t.TempDir()
	scraper := NewScraper(tempDir)
	
	// Set small limits for testing
	scraper.maxVisited = 100
	scraper.maxDepth = 5
	
	// Mock HTTP server that serves pages with circular links
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/page-a":
			w.Header().Set("Content-Type", "text/html")
			w.Write([]byte(`
				<html>
					<body>
						<a href="/page-b">Link to Page B</a>
					</body>
				</html>
			`))
		case "/page-b":
			w.Header().Set("Content-Type", "text/html")
			w.Write([]byte(`
				<html>
					<body>
						<a href="/page-c">Link to Page C</a>
					</body>
				</html>
			`))
		case "/page-c":
			w.Header().Set("Content-Type", "text/html")
			w.Write([]byte(`
				<html>
					<body>
						<a href="/page-a">Link back to Page A</a>
					</body>
				</html>
			`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	
	// Start scraping from page-a
	err := scraper.ScrapeURL(server.URL + "/page-a")
	if err != nil {
		t.Fatalf("Scraping failed: %v", err)
	}
	
	// Verify that each page was visited exactly once
	expectedVisits := []string{
		server.URL + "/page-a",
		server.URL + "/page-b", 
		server.URL + "/page-c",
	}
	
	// With Bloom filter, we can't get exact count, but we can test that expected URLs are marked as visited
	// and that we didn't get stuck in an infinite loop (which would cause the test to timeout)
	
	// Verify all expected pages were visited
	for _, expectedURL := range expectedVisits {
		scraper.mutex.RLock()
		visited := scraper.visited.Test([]byte(expectedURL))
		scraper.mutex.RUnlock()
		if !visited {
			t.Errorf("Expected page %s to be visited", expectedURL)
		}
	}
	
	// Check that the Bloom filter has reasonable capacity
	scraper.mutex.RLock()
	capacity := scraper.visited.Cap()
	scraper.mutex.RUnlock()
	
	if capacity < 1000 {
		t.Errorf("Bloom filter capacity too low: %d", capacity)
	}
} 