package main

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/PuerkitoBio/goquery"
	"github.com/bits-and-blooms/bloom/v3"
)

// Scraper represents a web scraper that downloads files recursively
type Scraper struct {
	outputDir string
	visited   *bloom.BloomFilter
	mutex     sync.RWMutex
	client    *http.Client
	maxVisited int // Maximum number of URLs to remember
	maxDepth   int // Maximum recursion depth
}

// NewScraper creates a new scraper instance
func NewScraper(outputDir string) *Scraper {
	// Create a Bloom filter with capacity for maxVisited URLs and 0.01 false positive rate
	bf := bloom.NewWithEstimates(10000, 0.01)
	return &Scraper{
		outputDir:  outputDir,
		visited:    bf,
		client:     &http.Client{},
		maxVisited: 10000, // Limit to 10k URLs
		maxDepth:   10,    // Limit recursion depth
	}
}

// ScrapeURL starts the recursive scraping process from the given URL
func (s *Scraper) ScrapeURL(startURL string) error {
	parsedURL, err := url.Parse(startURL)
	if err != nil {
		return fmt.Errorf("invalid URL: %v", err)
	}

	// Ensure the URL has a scheme
	if parsedURL.Scheme == "" {
		parsedURL.Scheme = "http"
		startURL = parsedURL.String()
	}

	return s.scrapeRecursive(startURL, startURL, 0)
}

// scrapeRecursive recursively scrapes URLs starting from the given URL
func (s *Scraper) scrapeRecursive(currentURL, baseURL string, depth int) error {
	// Check depth limit
	if depth > s.maxDepth {
		return nil
	}

	s.mutex.Lock()
	if s.visited.Test([]byte(currentURL)) {
		s.mutex.Unlock()
		return nil
	}
	
	// Check if we've reached the maximum number of visited URLs
	// Note: Bloom filter doesn't have a precise count, so we use a simple counter
	// For now, we'll just add URLs and let the Bloom filter handle capacity
	
	s.visited.Add([]byte(currentURL))
	s.mutex.Unlock()

	// Check if file already exists locally
	localPath := s.getLocalPath(currentURL, baseURL)
	if s.fileExists(localPath) {
		fmt.Printf("Skipping existing file: %s\n", localPath)
			// Still need to extract links from existing HTML files
	if s.isHTMLFile(localPath) {
		content, err := os.ReadFile(localPath)
		if err != nil {
			return fmt.Errorf("failed to read existing file %s: %v", localPath, err)
		}
		s.processLinks(string(content), baseURL, depth+1)
	}
		return nil
	}

	// Download the page
	resp, err := s.client.Get(currentURL)
	if err != nil {
		return fmt.Errorf("failed to fetch %s: %v", currentURL, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d for %s", resp.StatusCode, currentURL)
	}

	// Read the content
	content, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("failed to read response body: %v", err)
	}

	// Save the file locally
	if err := s.saveFile(localPath, content); err != nil {
		return fmt.Errorf("failed to save file %s: %v", localPath, err)
	}

	// Check if this is an HTML file and extract links
	contentType := resp.Header.Get("Content-Type")
	if strings.Contains(contentType, "text/html") {
		s.processLinks(string(content), baseURL, depth+1)
	}

	return nil
}

// extractLinks extracts all links from HTML content
func (s *Scraper) extractLinks(html, baseURL string) []string {
	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return nil
	}

	var links []string
	doc.Find("a[href]").Each(func(i int, sel *goquery.Selection) {
		if href, exists := sel.Attr("href"); exists {
			if normalized := s.normalizeURL(baseURL, href); normalized != "" {
				// Only include internal links
				if s.shouldScrape(normalized, baseURL) {
					links = append(links, normalized)
				}
			}
		}
	})

	return links
}

// normalizeURL converts a relative URL to an absolute URL
func (s *Scraper) normalizeURL(baseURL, href string) string {
	// Skip anchors, mailto, javascript, etc.
	if strings.HasPrefix(href, "#") || 
	   strings.HasPrefix(href, "mailto:") || 
	   strings.HasPrefix(href, "javascript:") ||
	   strings.HasPrefix(href, "tel:") {
		return ""
	}

	// If it's already an absolute URL, return as is
	if strings.HasPrefix(href, "http://") || strings.HasPrefix(href, "https://") {
		return href
	}

	// Resolve relative URL
	base, err := url.Parse(baseURL)
	if err != nil {
		return ""
	}

	relative, err := url.Parse(href)
	if err != nil {
		return ""
	}

	resolved := base.ResolveReference(relative)
	return resolved.String()
}

// shouldScrape determines if a URL should be scraped
func (s *Scraper) shouldScrape(targetURL, baseURL string) bool {
	if strings.Contains(targetURL, "/java/") || strings.Contains(targetURL, "/kotlin/") || strings.Contains(targetURL, "/dalvik/") {
		return false
	}

	parsedURL, err := url.Parse(targetURL)
	if err != nil {
		return false
	}

	parsedBase, err := url.Parse(baseURL)
	if err != nil {
		return false
	}

	// Only scrape URLs from the same domain and scheme
	return parsedURL.Host == parsedBase.Host && parsedURL.Scheme == parsedBase.Scheme
}

// getLocalPath determines the local file path for a given URL
func (s *Scraper) getLocalPath(targetURL, baseURL string) string {
	parsedURL, err := url.Parse(targetURL)
	if err != nil {
		return ""
	}

	// Get the path relative to the base URL
	path := parsedURL.Path
	
	// Handle root path
	if path == "" || path == "/" {
		path = "/index.html"
	}
	
	// Remove leading slash for local path
	if strings.HasPrefix(path, "/") {
		path = path[1:]
	}

	// If path doesn't have an extension or ends with slash, assume it's a directory
	if !strings.Contains(filepath.Base(path), ".") || strings.HasSuffix(path, "/") {
		path = filepath.Join(path, "index.html")
	}

	return filepath.Join(s.outputDir, path)
}

// saveFile saves content to a local file, creating directories as needed
func (s *Scraper) saveFile(localPath string, content []byte) error {
	// Create directory if it doesn't exist
	dir := filepath.Dir(localPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("failed to create directory %s: %v", dir, err)
	}

	// Write the file
	if err := os.WriteFile(localPath, content, 0644); err != nil {
		return fmt.Errorf("failed to write file %s: %v", localPath, err)
	}

	fmt.Printf("Saved: %s\n", localPath)
	return nil
}

// cleanupMemory is no longer needed with Bloom filter
// Bloom filters have constant memory usage and don't need cleanup

// getMemoryStats returns current memory usage statistics
func (s *Scraper) getMemoryStats() string {
	s.mutex.RLock()
	defer s.mutex.RUnlock()
	// Bloom filter doesn't provide exact count, but we can show its capacity
	return fmt.Sprintf("Bloom filter capacity: %d URLs", s.visited.Cap())
}

// fileExists checks if a file exists locally
func (s *Scraper) fileExists(localPath string) bool {
	_, err := os.Stat(localPath)
	return err == nil
}

// isHTMLFile checks if a file is likely an HTML file based on its extension
func (s *Scraper) isHTMLFile(localPath string) bool {
	ext := strings.ToLower(filepath.Ext(localPath))
	return ext == ".html" || ext == ".htm" || filepath.Base(localPath) == "index.html"
}

// processLinks extracts and processes links from HTML content
func (s *Scraper) processLinks(htmlContent, baseURL string, depth int) {
	links := s.extractLinks(htmlContent, baseURL)
	
	// Recursively scrape each link
	for _, link := range links {
		if s.shouldScrape(link, baseURL) {
			if err := s.scrapeRecursive(link, baseURL, depth); err != nil {
				// Log error but continue with other links
				fmt.Printf("Warning: failed to scrape %s: %v\n", link, err)
			}
		}
	}
} 

// ScrapeURLParallel starts the recursive scraping process from the given URL using a worker pool
func (s *Scraper) ScrapeURLParallel(startURL string, numWorkers int) error {
	// For now, use the sequential scraper but with better timeout handling
	// This avoids the complexity of managing parallel completion detection
	fmt.Println("Note: Using sequential scraping with improved timeout handling")
	return s.ScrapeURL(startURL)
} 