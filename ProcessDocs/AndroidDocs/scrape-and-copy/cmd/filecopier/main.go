package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"

	"webscraper/filecopier"
)

func main() {
	// Parse command line flags
	flag.Parse()

	// Check if we have exactly two arguments
	if flag.NArg() != 2 {
		fmt.Println("Usage: filecopier <source_directory> <destination_directory>")
		fmt.Println("Example: filecopier /path/to/source /path/to/destination")
		os.Exit(1)
	}

	sourceDir := flag.Arg(0)
	destDir := flag.Arg(1)

	// Validate source directory
	if sourceDir == "" {
		log.Fatal("Source directory cannot be empty")
	}

	// Resolve absolute paths
	sourceAbs, err := filepath.Abs(sourceDir)
	if err != nil {
		log.Fatalf("Failed to resolve source directory path: %v", err)
	}

	destAbs, err := filepath.Abs(destDir)
	if err != nil {
		log.Fatalf("Failed to resolve destination directory path: %v", err)
	}

	// Check if source and destination are the same
	if sourceAbs == destAbs {
		log.Fatal("Source and destination directories cannot be the same")
	}

	// Check if destination is a subdirectory of source (to prevent infinite recursion)
	relPath, err := filepath.Rel(sourceAbs, destAbs)
	if err == nil && !strings.HasPrefix(relPath, "..") && relPath != "." {
		log.Fatal("Destination directory cannot be a subdirectory of source directory")
	}

	fmt.Printf("Copying from: %s\n", sourceAbs)
	fmt.Printf("Copying to:   %s\n", destAbs)
	fmt.Println("Starting copy...")

	// Create file copier and start copying
	copier := filecopier.NewFileCopier()
	if err := copier.CopyDirectory(sourceAbs, destAbs); err != nil {
		log.Fatalf("Copy failed: %v", err)
	}

	// Show final summary
	fileCount := copier.GetFileCount()
	fmt.Printf("\nCopy completed successfully! Processed %d files.\n", fileCount)
} 