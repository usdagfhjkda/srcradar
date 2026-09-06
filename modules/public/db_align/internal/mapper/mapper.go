// Package mapper backfills companies.main_licence from the mapp_records
// already persisted by the assets pipeline.
//
// In ENScan's icp output, every record carries a `serviceLicence` of the
// form "<main>-<seq>" (e.g. "京ICP备12345678号-3X"). The prefix before the
// final "-<seq>" is the main licence and belongs on the company row, not on
// each sub-record. This package walks the mapp_records table, extracts the
// main licence for every company, and writes it to companies.main_licence
// when that field is currently empty.
package mapper

import (
	"database/sql"
	"fmt"
	"log"
	"regexp"
	"strings"

	"db_align/internal/store"
)

// mainLicenceRe matches a Chinese ICP main licence: <prefix>ICP备<digits><suffix>.
// We do not parse the suffix; the regex only needs to anchor the prefix so
// we can split it off the sub-licence.
var (
	icpPrefixRe = regexp.MustCompile(`^([一-龥]{0,3}ICP[备備]?[号號]?\d+[号號]?)`)
	seqSuffixRe = regexp.MustCompile(`[-－][\dA-Za-z]{1,4}$`)
)

// BackfillMainLicence scans all mapp_records rows with a non-empty
// service_licence, derives the main licence for each company, and writes
// it to companies.main_licence when empty.
//
// Already-populated main_licence values are NOT overwritten.
func BackfillMainLicence(s *store.Store, lg *log.Logger) (int, error) {
	if lg == nil {
		lg = log.Default()
	}
	rows, err := s.DB.Query(`
		SELECT company_id, service_licence
		FROM mapp_records
		WHERE service_licence IS NOT NULL AND service_licence != ''
	`)
	if err != nil {
		return 0, fmt.Errorf("query mapp_records: %w", err)
	}
	defer rows.Close()

	byCompany := map[int64]string{} // company_id → main licence
	for rows.Next() {
		var cid int64
		var lic string
		if err := rows.Scan(&cid, &lic); err != nil {
			return 0, fmt.Errorf("scan: %w", err)
		}
		main := deriveMainLicence(lic)
		if main == "" {
			continue
		}
		// Keep the shortest plausible main licence; longer ones are usually
		// already the main form.
		if cur, ok := byCompany[cid]; !ok || len(main) < len(cur) {
			byCompany[cid] = main
		}
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}

	updated := 0
	for cid, main := range byCompany {
		res, err := s.DB.Exec(
			`UPDATE companies
			 SET main_licence = ?
			 WHERE id = ? AND (main_licence IS NULL OR main_licence = '')`,
			main, cid,
		)
		if err != nil {
			return updated, fmt.Errorf("update company %d: %w", cid, err)
		}
		n, _ := res.RowsAffected()
		updated += int(n)
		if n > 0 {
			lg.Printf("mapper: company id=%d main_licence=%s", cid, main)
		}
	}
	return updated, nil
}

// deriveMainLicence returns the main licence from a service_licence of the
// form "<main>-<seq>". Returns "" if it cannot confidently split.
func deriveMainLicence(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	// Drop a trailing sequence like "-1", "-3X", "-12A". 限定为 - 后跟 1-4
	// 字符，否则会把合法的"-"误切。
	if loc := seqSuffixRe.FindStringIndex(s); loc != nil {
		s = s[:loc[0]]
	}
	// Now s should look like a full ICP main licence; if it still doesn't
	// contain the digits+号 pattern, just return as-is (some sources
	// already give the main form).
	if icpPrefixRe.MatchString(s) || strings.Contains(s, "号") || strings.Contains(s, "號") {
		return s
	}
	return ""
}

// Helper used by store tests to inspect the by-company map.
func _ensureImported() { _ = sql.ErrNoRows }
