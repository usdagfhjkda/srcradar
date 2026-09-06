package enscan

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"
)

// tmpOutDir creates a unique scratch directory under $TMPDIR (or /tmp).
func tmpOutDir() (string, error) {
	base := os.TempDir()
	dir := filepath.Join(base, fmt.Sprintf("db_align_enscan_%d", time.Now().UnixNano()))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("mkdir tmp: %w", err)
	}
	return dir, nil
}

// latestJSON returns the most recent .json file in dir (single-call temp dir
// usually has only one).
func latestJSON(dir string) (string, error) {
	matches, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil {
		return "", err
	}
	if len(matches) == 0 {
		return "", fmt.Errorf("enscan produced no JSON output in %s", dir)
	}
	sort.Strings(matches)
	return matches[len(matches)-1], nil
}

// parseJSONFile reads and unmarshals an ENScan JSON file.
func parseJSONFile(path string) (*Result, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read enscan output: %w", err)
	}
	// ENScan writes a map[string][]map[string]string (top-level keys are
	// section names like "icp", "app", "invest" ...).
	var sections map[string][]map[string]string
	if err := json.Unmarshal(raw, &sections); err != nil {
		return nil, fmt.Errorf("decode enscan output: %w (head: %.200s)", err, string(raw))
	}
	return &Result{Sections: sections, Raw: raw, Path: path}, nil
}
