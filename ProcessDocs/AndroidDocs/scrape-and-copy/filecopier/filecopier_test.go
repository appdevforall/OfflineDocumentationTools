package filecopier

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFileCopier_CopyDirectory(t *testing.T) {
	// Create temporary directories for testing
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create test directory structure in source
	testFiles := map[string]string{
		"file1.txt":                    "content1",
		"subdir/file2.txt":             "content2",
		"subdir/nested/file3.txt":      "content3",
		"subdir/nested/deep/file4.txt": "content4",
		"another/file5.html":           "<html>content5</html>",
		"empty.txt":                    "",
	}

	// Create the test files
	for path, content := range testFiles {
		fullPath := filepath.Join(sourceDir, path)
		dir := filepath.Dir(fullPath)
		if err := os.MkdirAll(dir, 0755); err != nil {
			t.Fatalf("Failed to create directory %s: %v", dir, err)
		}
		if err := os.WriteFile(fullPath, []byte(content), 0644); err != nil {
			t.Fatalf("Failed to create test file %s: %v", fullPath, err)
		}
	}

	// Create an empty directory
	emptyDir := filepath.Join(sourceDir, "empty_dir")
	if err := os.MkdirAll(emptyDir, 0755); err != nil {
		t.Fatalf("Failed to create empty directory: %v", err)
	}

	// Copy the directory
	copier := NewFileCopier()
	err := copier.CopyDirectory(sourceDir, destDir)
	if err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	// Verify all files were copied correctly
	for path, expectedContent := range testFiles {
		sourcePath := filepath.Join(sourceDir, path)
		destPath := filepath.Join(destDir, expectedDestName(path))

		// Check that destination file exists
		if _, err := os.Stat(destPath); os.IsNotExist(err) {
			t.Errorf("Destination file does not exist: %s", destPath)
			continue
		}

		// Check that content matches
		destContent, err := os.ReadFile(destPath)
		if err != nil {
			t.Errorf("Failed to read destination file %s: %v", destPath, err)
			continue
		}

		if string(destContent) != expectedContent {
			t.Errorf("Content mismatch for %s: expected '%s', got '%s'", path, expectedContent, string(destContent))
		}

		// Check that source file still exists (copy, not move)
		if _, err := os.Stat(sourcePath); os.IsNotExist(err) {
			t.Errorf("Source file was removed: %s", sourcePath)
		}
	}

	// Verify empty directory was created
	destEmptyDir := filepath.Join(destDir, "empty_dir")
	if _, err := os.Stat(destEmptyDir); os.IsNotExist(err) {
		t.Errorf("Empty directory was not copied: %s", destEmptyDir)
	}
}

func TestFileCopier_CopyDirectoryWithPermissions(t *testing.T) {
	// Create temporary directories for testing
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a test file with specific permissions
	testFile := filepath.Join(sourceDir, "executable.sh")
	content := "#!/bin/bash\necho 'hello'"
	if err := os.WriteFile(testFile, []byte(content), 0755); err != nil {
		t.Fatalf("Failed to create test file: %v", err)
	}

	// Copy the directory
	copier := NewFileCopier()
	err := copier.CopyDirectory(sourceDir, destDir)
	if err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	// Verify the file was copied with correct permissions
	destFile := filepath.Join(destDir, expectedDestName("executable.sh"))
	info, err := os.Stat(destFile)
	if err != nil {
		t.Errorf("Failed to stat destination file: %v", err)
	} else {
		mode := info.Mode()
		if mode&0111 == 0 {
			t.Errorf("Executable permissions not preserved: %s", mode)
		}
	}
}

func TestFileCopier_CopyDirectorySourceNotExists(t *testing.T) {
	destDir := t.TempDir()
	copier := NewFileCopier()
	
	err := copier.CopyDirectory("/nonexistent/path", destDir)
	if err == nil {
		t.Error("Expected error when source directory does not exist")
	}
}

func TestFileCopier_CopyDirectoryDestExists(t *testing.T) {
	// Create temporary directories for testing
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a file in the destination that might conflict
	existingFile := filepath.Join(destDir, expectedDestName("file1.txt"))
	if err := os.WriteFile(existingFile, []byte("existing content"), 0644); err != nil {
		t.Fatalf("Failed to create existing file: %v", err)
	}

	// Create a file in source with the same name
	sourceFile := filepath.Join(sourceDir, "file1.txt")
	if err := os.WriteFile(sourceFile, []byte("new content"), 0644); err != nil {
		t.Fatalf("Failed to create source file: %v", err)
	}

	// Copy the directory (should overwrite existing file)
	copier := NewFileCopier()
	err := copier.CopyDirectory(sourceDir, destDir)
	if err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	// Verify the file was overwritten
	content, err := os.ReadFile(existingFile)
	if err != nil {
		t.Errorf("Failed to read overwritten file: %v", err)
	} else if string(content) != "new content" {
		t.Errorf("File was not overwritten: expected 'new content', got '%s'", string(content))
	}
}

func TestFileCopier_CopyDirectoryLargeFile(t *testing.T) {
	// Create temporary directories for testing
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a large file (1MB)
	largeFile := filepath.Join(sourceDir, "large.bin")
	largeContent := make([]byte, 1024*1024) // 1MB
	for i := range largeContent {
		largeContent[i] = byte(i % 256)
	}
	
	if err := os.WriteFile(largeFile, largeContent, 0644); err != nil {
		t.Fatalf("Failed to create large file: %v", err)
	}

	// Copy the directory
	copier := NewFileCopier()
	err := copier.CopyDirectory(sourceDir, destDir)
	if err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	// Verify the large file was copied correctly
	destLargeFile := filepath.Join(destDir, expectedDestName("large.bin"))
	destContent, err := os.ReadFile(destLargeFile)
	if err != nil {
		t.Errorf("Failed to read copied large file: %v", err)
	} else if len(destContent) != len(largeContent) {
		t.Errorf("Large file size mismatch: expected %d, got %d", len(largeContent), len(destContent))
	}

	// Verify content matches
	for i, b := range destContent {
		if b != largeContent[i] {
			t.Errorf("Large file content mismatch at byte %d: expected %d, got %d", i, largeContent[i], b)
			break
		}
	}
} 

// Rename the test function to reflect <article> extraction
func TestFileCopier_ExtractArticleTag(t *testing.T) {
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	input := `before
<h1>Test Title</h1>
<article>
  <p>Keep this!</p>
</article>
after`
	expected := `<html><head><title>Test Title</title></head><body><article>
  <p>Keep this!</p>
</article></body></html>`

	sourceFile := filepath.Join(sourceDir, "article.html")
	if err := os.WriteFile(sourceFile, []byte(input), 0644); err != nil {
		t.Fatalf("Failed to create source file: %v", err)
	}

	copier := NewFileCopier()
	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	destFile := filepath.Join(destDir, "article.html")
	output, err := os.ReadFile(destFile)
	if err != nil {
		t.Fatalf("Failed to read destination file: %v", err)
	}

	if string(output) != expected {
		t.Errorf("Extracted content mismatch.\nExpected:\n%s\nGot:\n%s", expected, string(output))
	}

	plainInput := "no article tag here"
	plainSource := filepath.Join(sourceDir, "plain.txt")
	if err := os.WriteFile(plainSource, []byte(plainInput), 0644); err != nil {
		t.Fatalf("Failed to create plain source file: %v", err)
	}

	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}
	plainDest := filepath.Join(destDir, expectedDestName("plain.txt"))
	plainOutput, err := os.ReadFile(plainDest)
	if err != nil {
		t.Fatalf("Failed to read plain destination file: %v", err)
	}
	if string(plainOutput) != plainInput {
		t.Errorf("Plain file content mismatch. Expected '%s', got '%s'", plainInput, string(plainOutput))
	}
} 

func TestFileCopier_AddHtmlExtension(t *testing.T) {
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a file without .html extension
	sourceFile := filepath.Join(sourceDir, "test")
	content := "some content"
	if err := os.WriteFile(sourceFile, []byte(content), 0644); err != nil {
		t.Fatalf("Failed to create source file: %v", err)
	}

	copier := NewFileCopier()
	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	// Check that destination file has .html extension
	destFile := filepath.Join(destDir, expectedDestName("test"))
	if _, err := os.Stat(destFile); os.IsNotExist(err) {
		t.Errorf("Destination file with .html extension does not exist: %s", destFile)
	}

	// Check that content was copied correctly
	destContent, err := os.ReadFile(destFile)
	if err != nil {
		t.Errorf("Failed to read destination file: %v", err)
	} else if string(destContent) != content {
		t.Errorf("Content mismatch: expected '%s', got '%s'", content, string(destContent))
	}

	// Test that files with .html extension are not modified
	htmlSourceFile := filepath.Join(sourceDir, "test.html")
	htmlContent := "html content"
	if err := os.WriteFile(htmlSourceFile, []byte(htmlContent), 0644); err != nil {
		t.Fatalf("Failed to create HTML source file: %v", err)
	}

	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	// Check that HTML file was copied without adding another .html
	htmlDestFile := filepath.Join(destDir, expectedDestName("test.html"))
	htmlDestContent, err := os.ReadFile(htmlDestFile)
	if err != nil {
		t.Errorf("Failed to read HTML destination file: %v", err)
	} else if string(htmlDestContent) != htmlContent {
		t.Errorf("HTML content mismatch: expected '%s', got '%s'", htmlContent, string(htmlDestContent))
	}
} 

func TestFileCopier_IndexHtmlHandling(t *testing.T) {
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Test 1: index.html in package-summary directory should be skipped
	packageSummaryDir := filepath.Join(sourceDir, "some", "package-summary")
	if err := os.MkdirAll(packageSummaryDir, 0755); err != nil {
		t.Fatalf("Failed to create package-summary directory: %v", err)
	}
	packageSummaryIndex := filepath.Join(packageSummaryDir, "index.html")
	if err := os.WriteFile(packageSummaryIndex, []byte("should be skipped"), 0644); err != nil {
		t.Fatalf("Failed to create package-summary index.html: %v", err)
	}

	// Test 2: regular index.html should be renamed
	regularDir := filepath.Join(sourceDir, "android_test", "reference", "androidx", "constraintlayout", "motion", "utils", "ViewOscillator")
	if err := os.MkdirAll(regularDir, 0755); err != nil {
		t.Fatalf("Failed to create regular directory: %v", err)
	}
	regularIndex := filepath.Join(regularDir, "index.html")
	if err := os.WriteFile(regularIndex, []byte("should be renamed"), 0644); err != nil {
		t.Fatalf("Failed to create regular index.html: %v", err)
	}

	copier := NewFileCopier()
	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	// Verify package-summary index.html was skipped
	packageSummaryDest := filepath.Join(destDir, "some", "package-summary", "index.html")
	if _, err := os.Stat(packageSummaryDest); err == nil {
		t.Errorf("Package-summary index.html should have been skipped: %s", packageSummaryDest)
	}

	// Verify regular index.html was renamed
	expectedRenamedPath := filepath.Join(destDir, "android_test", "reference", "androidx", "constraintlayout", "motion", "utils", "ViewOscillator.html")
	if _, err := os.Stat(expectedRenamedPath); os.IsNotExist(err) {
		t.Errorf("Regular index.html should have been renamed to: %s", expectedRenamedPath)
	}

	// Verify content was copied correctly
	content, err := os.ReadFile(expectedRenamedPath)
	if err != nil {
		t.Errorf("Failed to read renamed file: %v", err)
	} else if string(content) != "should be renamed" {
		t.Errorf("Content mismatch: expected 'should be renamed', got '%s'", string(content))
	}
}

func TestFileCopier_IndexHtmlNamingCollision(t *testing.T) {
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a directory structure that would cause a naming collision
	dir1 := filepath.Join(sourceDir, "dir1")
	dir2 := filepath.Join(sourceDir, "dir2")
	if err := os.MkdirAll(dir1, 0755); err != nil {
		t.Fatalf("Failed to create dir1: %v", err)
	}
	if err := os.MkdirAll(dir2, 0755); err != nil {
		t.Fatalf("Failed to create dir2: %v", err)
	}

	// Create index.html in dir1
	index1 := filepath.Join(dir1, "index.html")
	if err := os.WriteFile(index1, []byte("content1"), 0644); err != nil {
		t.Fatalf("Failed to create index1: %v", err)
	}

	// Create a file that would conflict with dir1.html
	conflictFile := filepath.Join(destDir, "dir1.html")
	if err := os.WriteFile(conflictFile, []byte("existing content"), 0644); err != nil {
		t.Fatalf("Failed to create conflict file: %v", err)
	}

	copier := NewFileCopier()
	err := copier.CopyDirectory(sourceDir, destDir)
	if err == nil {
		t.Error("Expected error due to naming collision, but got none")
	} else if !strings.Contains(err.Error(), "naming collision") {
		t.Errorf("Expected naming collision error, got: %v", err)
	}
}

func TestFileCopier_HtmlBoilerplate(t *testing.T) {
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a file with article and h1 tags (h1 inside article)
	input := `some content before
<article>
  <h1 id="drophelper" data-text="DropHelper" tabindex="-1">DropHelper</h1>
  <p>Article content here</p>
  <h2>Subheading</h2>
</article>
some content after`

	expected := `<html><head><title>DropHelper</title></head><body><article>
  <h1 id="drophelper" data-text="DropHelper" tabindex="-1">DropHelper</h1>
  <p>Article content here</p>
  <h2>Subheading</h2>
</article></body></html>`

	sourceFile := filepath.Join(sourceDir, "test.html")
	if err := os.WriteFile(sourceFile, []byte(input), 0644); err != nil {
		t.Fatalf("Failed to create source file: %v", err)
	}

	copier := NewFileCopier()
	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	destFile := filepath.Join(destDir, "test.html")
	output, err := os.ReadFile(destFile)
	if err != nil {
		t.Fatalf("Failed to read destination file: %v", err)
	}

	if string(output) != expected {
		t.Errorf("HTML boilerplate mismatch.\nExpected:\n%s\nGot:\n%s", expected, string(output))
	}
}

func TestFileCopier_HtmlBoilerplateNoH1(t *testing.T) {
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a file with article but no h1 tag
	input := `some content before
<article>
  <p>Article content here</p>
</article>
some content after`

	expected := `<html><head><title>Untitled</title></head><body><article>
  <p>Article content here</p>
</article></body></html>`

	sourceFile := filepath.Join(sourceDir, "test.html")
	if err := os.WriteFile(sourceFile, []byte(input), 0644); err != nil {
		t.Fatalf("Failed to create source file: %v", err)
	}

	copier := NewFileCopier()
	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	destFile := filepath.Join(destDir, "test.html")
	output, err := os.ReadFile(destFile)
	if err != nil {
		t.Fatalf("Failed to read destination file: %v", err)
	}

	if string(output) != expected {
		t.Errorf("HTML boilerplate mismatch (no h1).\nExpected:\n%s\nGot:\n%s", expected, string(output))
	}
}

func TestFileCopier_HtmlBoilerplateComplexH1(t *testing.T) {
	sourceDir := t.TempDir()
	destDir := t.TempDir()

	// Create a file with complex h1 tag containing nested elements (h1 inside article)
	input := `some content before
<article>
  <h1 id="complex" class="title"><span class="icon">📚</span>Complex <strong>Title</strong> with <em>Formatting</em></h1>
  <p>Article content here</p>
</article>
some content after`

	expected := `<html><head><title>📚Complex Title with Formatting</title></head><body><article>
  <h1 id="complex" class="title"><span class="icon">📚</span>Complex <strong>Title</strong> with <em>Formatting</em></h1>
  <p>Article content here</p>
</article></body></html>`

	sourceFile := filepath.Join(sourceDir, "test.html")
	if err := os.WriteFile(sourceFile, []byte(input), 0644); err != nil {
		t.Fatalf("Failed to create source file: %v", err)
	}

	copier := NewFileCopier()
	if err := copier.CopyDirectory(sourceDir, destDir); err != nil {
		t.Fatalf("CopyDirectory failed: %v", err)
	}

	destFile := filepath.Join(destDir, "test.html")
	output, err := os.ReadFile(destFile)
	if err != nil {
		t.Fatalf("Failed to read destination file: %v", err)
	}

	if string(output) != expected {
		t.Errorf("HTML boilerplate mismatch (complex h1).\nExpected:\n%s\nGot:\n%s", expected, string(output))
	}
}

// Helper to get expected destination filename
func expectedDestName(name string) string {
	if filepath.Ext(name) == ".html" {
		return name
	}
	return name + ".html"
} 