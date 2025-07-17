package filecopier

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
)

// FileCopier represents a file copying utility
type FileCopier struct {
	// No options needed; always extract <article>
	fileCount int64 // Atomic counter for progress tracking
}

// NewFileCopier creates a new file copier instance
func NewFileCopier() *FileCopier {
	return &FileCopier{}
}

// CopyDirectory recursively copies all files from sourceDir to destDir
// while preserving the directory structure
func (fc *FileCopier) CopyDirectory(sourceDir, destDir string) error {
	// Check if source directory exists
	sourceInfo, err := os.Stat(sourceDir)
	if err != nil {
		return fmt.Errorf("source directory does not exist: %v", err)
	}
	if !sourceInfo.IsDir() {
		return fmt.Errorf("source path is not a directory: %s", sourceDir)
	}

	// Create destination directory if it doesn't exist
	if err := os.MkdirAll(destDir, sourceInfo.Mode()); err != nil {
		return fmt.Errorf("failed to create destination directory: %v", err)
	}

	// Walk through the source directory
	return filepath.Walk(sourceDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}

		// Calculate the relative path from source directory
		relPath, err := filepath.Rel(sourceDir, path)
		if err != nil {
			return fmt.Errorf("failed to calculate relative path: %v", err)
		}

		// Skip the root directory itself
		if relPath == "." {
			return nil
		}

		// Calculate the destination path
		destPath := filepath.Join(destDir, relPath)

		// Handle different file types
		if info.IsDir() {
			// Create directory in destination
			if err := os.MkdirAll(destPath, info.Mode()); err != nil {
				return fmt.Errorf("failed to create directory %s: %v", destPath, err)
			}
		} else if info.Mode()&os.ModeSymlink != 0 {
			// Handle symlinks
			if err := fc.copySymlink(path, destPath); err != nil {
				return fmt.Errorf("failed to copy symlink %s: %v", path, err)
			}
		} else {
			// Copy regular file, always extracting <article>
			if err := fc.copyFile(path, destPath, info.Mode()); err != nil {
				return fmt.Errorf("failed to copy file %s: %v", path, err)
			}
		}

		return nil
	})
}

// copyFile copies a single file from source to destination, always extracting <article>
func (fc *FileCopier) copyFile(source, dest string, mode os.FileMode) error {
	// Handle index.html files specially
	if filepath.Base(source) == "index.html" {
		// Check if it's in a package-summary directory
		parentDir := filepath.Base(filepath.Dir(source))
		if parentDir == "package-summary" {
			// Skip this file
			return nil
		}
		
		// Rename: remove index.html and add .html to parent directory
		parentPath := filepath.Dir(dest)
		newDest := parentPath + ".html"
		
		// Check for naming collision
		if _, err := os.Stat(newDest); err == nil {
			return fmt.Errorf("naming collision: destination file already exists: %s", newDest)
		}
		
		dest = newDest
	} else {
		// Add .html suffix if not present
		if !strings.HasSuffix(dest, ".html") {
			dest = dest + ".html"
		}
	}

	// Read the entire source file
	content, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	
	// Extract <article>...</article> (including tags)
	article := extractActivityTag(string(content))
	if article != "" {
		// Extract title from the first <h1> tag
		title := extractTitle(string(content))
		
		// Wrap in HTML boilerplate
		htmlContent := fmt.Sprintf("<html><head><title>%s</title></head><body>%s</body></html>", title, article)
		err = os.WriteFile(dest, []byte(htmlContent), mode)
	} else {
		// If not found, copy as-is
		err = os.WriteFile(dest, content, mode)
	}

	// Update progress counter and display
	if err == nil {
		count := atomic.AddInt64(&fc.fileCount, 1)
		fmt.Printf("\rFiles processed: %d", count)
	}

	return err
}

// GetFileCount returns the total number of files processed
func (fc *FileCopier) GetFileCount() int64 {
	return atomic.LoadInt64(&fc.fileCount)
}

// extractActivityTag returns the first <article ...>...</article> (including tags), or "" if not found
func extractActivityTag(s string) string {
	re := regexp.MustCompile(`(?s)<article[^>]*>.*?</article>`) // (?s) = dot matches newline
	match := re.FindString(s)
	return match
}

// extractTitle returns the text content of the first <h1> tag, or "Untitled" if not found
func extractTitle(s string) string {
	re := regexp.MustCompile(`<h1[^>]*>(.*?)</h1>`)
	match := re.FindStringSubmatch(s)
	if len(match) > 1 {
		// Remove any HTML tags from the title content
		titleRe := regexp.MustCompile(`<[^>]*>`)
		cleanTitle := titleRe.ReplaceAllString(match[1], "")
		return strings.TrimSpace(cleanTitle)
	}
	return "Untitled"
}

// copySymlink copies a symlink from source to destination
func (fc *FileCopier) copySymlink(source, dest string) error {
	// Read the symlink target
	target, err := os.Readlink(source)
	if err != nil {
		return err
	}

	// Create the symlink in destination
	return os.Symlink(target, dest)
} 