package main

import (
	"flag"
	"fmt"
	"log"
	"os"
)

func main() {
	var outputDir string
	var numWorkers int
	var useParallel bool
	var maxVisited int
	var maxDepth int
	
	flag.StringVar(&outputDir, "output", ".", "Output directory for scraped files")
	flag.IntVar(&numWorkers, "workers", 4, "Number of worker goroutines for parallel scraping")
	flag.BoolVar(&useParallel, "parallel", false, "Use parallel scraping with multiple workers")
	flag.IntVar(&maxVisited, "max-urls", 10000, "Maximum number of URLs to remember (memory limit)")
	flag.IntVar(&maxDepth, "max-depth", 10, "Maximum recursion depth")
	flag.Parse()

	if flag.NArg() < 1 {
		fmt.Println("Usage: webscraper [options] <URL>")
		fmt.Println("Options:")
		flag.PrintDefaults()
		os.Exit(1)
	}

	startURL := flag.Arg(0)
	
	// Create output directory if it doesn't exist
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		log.Fatalf("Failed to create output directory: %v", err)
	}

	scraper := NewScraper(outputDir)
	// Override default limits with command-line options
	scraper.maxVisited = maxVisited
	scraper.maxDepth = maxDepth
	
	fmt.Printf("Starting to scrape: %s\n", startURL)
	fmt.Printf("Output directory: %s\n", outputDir)
	fmt.Printf("Memory limits: Max URLs=%d, Max Depth=%d\n", maxVisited, maxDepth)
	
	var err error
	if useParallel {
		fmt.Printf("Using parallel scraping with %d workers\n", numWorkers)
		err = scraper.ScrapeURLParallel(startURL, numWorkers)
	} else {
		fmt.Println("Using sequential scraping")
		err = scraper.ScrapeURL(startURL)
	}
	
	if err != nil {
		log.Fatalf("Scraping failed: %v", err)
	}
	
	fmt.Println("Scraping completed successfully!")
} 