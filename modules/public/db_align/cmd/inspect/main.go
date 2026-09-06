package main

// One-off DB inspector: prints mapp_records row count + service_type distribution.
// Usage: ./inspect [DB_PATH]   (default: <repo>/db/recon.sqlite3)

import (
	"database/sql"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	_ "modernc.org/sqlite"
)

func main() {
	defaultDB, err := filepath.Abs(filepath.Join("..", "..", "..", "db", "recon.sqlite3"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "resolve default db:", err)
		os.Exit(1)
	}
	dbPath := flag.String("db", defaultDB, "path to recon.sqlite3")
	flag.Parse()

	db, err := sql.Open("sqlite", *dbPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "open:", err)
		os.Exit(1)
	}
	defer db.Close()

	var n int
	if err := db.QueryRow("SELECT COUNT(*) FROM mapp_records").Scan(&n); err != nil {
		fmt.Fprintln(os.Stderr, "count:", err)
		os.Exit(1)
	}
	fmt.Printf("mapp_records: %d\n", n)
	r, err := db.Query("SELECT service_type, COUNT(*) FROM mapp_records GROUP BY service_type")
	if err != nil {
		fmt.Fprintln(os.Stderr, "group:", err)
		os.Exit(1)
	}
	defer r.Close()
	for r.Next() {
		var st, c int
		if err := r.Scan(&st, &c); err != nil {
			fmt.Fprintln(os.Stderr, "scan:", err)
			continue
		}
		fmt.Printf("  type=%d → %d\n", st, c)
	}
}
