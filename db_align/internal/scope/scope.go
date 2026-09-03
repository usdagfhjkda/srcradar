// Package scope extracts likely main-domain candidates from the various
// ENScan record sections and writes them into the scopes table.
//
// The heuristic is: a "scope" is a registrable domain (eTLD+1) that the
// business is plausibly responsible for. Sources of evidence:
//
//   - icp: the "homeSite" / "domain" column of an ICP record;
//   - wx_app: the qrcode URL or the read_num link (rarely useful);
//   - app: the "link" / "market" URL, which usually points to a store but
//     sometimes contains a developer website.
//
// We do NOT do eTLD+1 reduction (no public suffix list dependency); we just
// return the host part of any URL and dedup within a single company.
package scope

import (
	"fmt"
	"log"
	"net/url"
	"strings"

	"db_align/internal/enscan"
	"db_align/internal/store"
)

// Extract walks every record in the ENScan Result and returns the set of
// distinct host strings that look like scopes (have at least one dot, no
// scheme, no path).
func Extract(res *enscan.Result) []string {
	seen := map[string]bool{}
	var out []string

	add := func(s string) {
		s = normaliseHost(s)
		if s == "" || seen[s] {
			return
		}
		seen[s] = true
		out = append(out, s)
	}

	for sec, list := range res.Sections {
		for _, rec := range list {
			switch sec {
			case enscan.SecICP:
				add(rec["homeSite"])
				add(rec["domain"])
				add(rec["website"])
			case enscan.SecAPP:
				add(hostFromURL(rec["link"]))
				add(hostFromURL(rec["market"]))
			case enscan.SecWxApp:
				// qrcode is a wxpath:// URL — ignore.
				_ = rec["qrcode"]
			case enscan.SecWechat:
				_ = rec["qrcode"]
			}
		}
	}
	return out
}

// Persist writes the scopes to the store, attached to the given business id.
// Hosts are inserted as is_wildcard=0 by default; ICP records with a "*" or
// known wildcard forms are flagged.
func Persist(s *store.Store, businessID int64, hosts []string, lg *log.Logger) (int, error) {
	if lg == nil {
		lg = log.Default()
	}
	n := 0
	for _, h := range hosts {
		if h == "" {
			continue
		}
		wildcard := strings.HasPrefix(h, "*.")
		if wildcard {
			h = strings.TrimPrefix(h, "*.")
		}
		sc := &store.Scope{BusinessID: businessID, Asset: h, Wildcard: wildcard}
		if _, err := s.UpsertScope(sc); err != nil {
			return n, fmt.Errorf("upsert scope %q: %w", h, err)
		}
		n++
		lg.Printf("scope: business=%d asset=%q wildcard=%v", businessID, h, wildcard)
	}
	return n, nil
}

// hostFromURL returns the host of a URL string, or "" if parsing fails.
func hostFromURL(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	if !strings.Contains(s, "://") {
		// Maybe a bare host.
		return normaliseHost(s)
	}
	u, err := url.Parse(s)
	if err != nil {
		return ""
	}
	return normaliseHost(u.Host)
}

// normaliseHost strips scheme/path/port and lowercases. Returns "" if the
// resulting host does not look like a domain (no dot, or contains spaces).
func normaliseHost(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	if s == "" {
		return ""
	}
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
	}
	if i := strings.IndexAny(s, "/?#:"); i >= 0 {
		s = s[:i]
	}
	// Drop a leading "*." (handled by Persist; keep raw here).
	s = strings.TrimPrefix(s, "*.")
	if !strings.Contains(s, ".") {
		return ""
	}
	if strings.ContainsAny(s, " \t\n") {
		return ""
	}
	return s
}
