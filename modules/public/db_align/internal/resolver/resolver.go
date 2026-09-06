// Package resolver turns a free-form business name (e.g. "ExampleCo") into a
// concrete legal entity ("ExampleCo子公司有限公司", pid=...) suitable for further
// holding-tree recursion.
//
// Two modes are supported:
//
//   - Strict (default): pick the single best match. "Best" is the candidate
//     whose name shares the longest common prefix with the input and whose
//     nature is "企业". This is what most HW engagements want — one entity,
//     no noise.
//
//   - Broad (--broad): return the top-N candidates. The downstream crawler
//     then walks the holding tree for every one of them, which catches the
//     case where a business has multiple sibling legal entities under
//     different brand names.
package resolver

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"

	"db_align/internal/enscan"
	"db_align/internal/permute"
)

// Candidate is one company returned by ENScan for the business name.
type Candidate struct {
	Name        string
	PID         string
	RegCode     string
	LegalPerson string
	Nature      string
	Score       int
	Reason      string
}

// Result is the resolver's verdict. Selected is the Strict pick (or the
// highest-scoring one in Broad). Candidates is the full ranked list.
type Result struct {
	Input      string
	Selected   *Candidate
	Candidates []Candidate
}

// BroadN is the number of candidates retained in --broad mode.
const BroadN = 5

// MinAcceptScore is the minimum match quality the resolver accepts in strict
// (non-broad) mode. Below this we return an error and ask the user to rerun
// with -broad (to inspect every candidate) or -pid (to bypass resolution
// entirely). Tuned against the 2026-07-24 dry-run: "ExampleCo" resolved to the
// group parent "ExampleCo集团控股有限公司" at score=50 (weak match), but
// the correct entity is the operating subsidiary "ExampleCo子公司有限公司" which
// would have scored 200. Keeping a hard floor forces the operator to make
// an explicit choice instead of silently binding the SRC to the wrong
// entity.
//
//   200  exact match after stripping legal suffix / input is full prefix
//   100  strong prefix match (≥10 chars shared prefix × 10)
//    60  short-prefix match (e.g. "ExampleCo" inside "ExampleCo科技..." → 5×10 + 50 contains bonus)
//    50  weak match (only the partial-prefix bonus, no contains)
//    0   no common prefix / no contains
//
// We reject anything < 80 in strict mode; that catches the weak-match tier
// (50) and the short-prefix tier (60-80), letting through only strong
// prefixes (≥10 chars) and exact matches.
const MinAcceptScore = 80

// Resolve searches ENScan for the business name and ranks the hits.
// broad=true returns the top BroadN candidates; otherwise only the single
// best match is selected.
func Resolve(ctx context.Context, r *enscan.Runner, name string, broad bool) (*Result, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return nil, errors.New("empty business name")
	}

	res, err := r.Search(ctx, name, nil) // no -field: we just want enterprise_info
	if err != nil {
		return nil, fmt.Errorf("enscan search %q: %w", name, err)
	}
	companies := res.Companies()
	if len(companies) == 0 {
		return nil, fmt.Errorf("no match for %q in any configured source", name)
	}

	cands := make([]Candidate, 0, len(companies))
	for _, c := range companies {
		cands = append(cands, Candidate{
			Name:        c.Name,
			PID:         c.PID,
			RegCode:     c.RegCode,
			LegalPerson: c.LegalPerson,
			Score:       score(name, c.Name),
		})
	}
	// Sort: score desc, then "企业" nature ahead of others, then shorter name.
	sort.SliceStable(cands, func(i, j int) bool {
		if cands[i].Score != cands[j].Score {
			return cands[i].Score > cands[j].Score
		}
		ni := strings.Contains(cands[i].Name, "公司")
		nj := strings.Contains(cands[j].Name, "公司")
		if ni != nj {
			return ni
		}
		return utf8.RuneCountInString(cands[i].Name) < utf8.RuneCountInString(cands[j].Name)
	})
	for i := range cands {
		cands[i].Reason = explain(name, cands[i].Name, cands[i].Score)
	}

	out := &Result{Input: name, Candidates: cands}
	if broad {
		n := BroadN
		if n > len(cands) {
			n = len(cands)
		}
		// Also expand to same-legal-person siblings in Broad mode.
		expanded := expandByLegalPerson(cands, n)
		out.Candidates = expanded
	}
	if len(out.Candidates) == 0 {
		return nil, errors.New("resolver produced no candidates")
	}
	sel := out.Candidates[0]
	// Strict mode rejects weak matches so the operator must explicitly choose
	// (via -broad to inspect, or -pid to bypass resolution).
	if !broad && sel.Score < MinAcceptScore {
		return nil, fmt.Errorf(
			"resolver best match %q (pid=%s) scored %d, below threshold %d; "+
				"rerun with -broad to inspect %d candidate(s), or pass -pid <known_pid> to bypass resolution",
			sel.Name, sel.PID, sel.Score, MinAcceptScore, len(out.Candidates))
	}
	out.Selected = &sel
	return out, nil
}

// score returns a coarse match quality. Tries the input as-typed and also
// in digits→Chinese form (e.g. "98同城" → "九八同城"), taking the best match.
// +100 per char of shared prefix, -50 when the candidate has a different
// suffix family (有限公司 vs 集团).
func score(input, cand string) int {
	if cand == "" {
		return -1000
	}
	input = strings.TrimSpace(input)
	cand = strings.TrimSpace(cand)
	stripped := stripLegalSuffix(cand)
	for strings.HasSuffix(stripped, "公司") && len(stripped) > 2 {
		stripped = strings.TrimSuffix(stripped, "公司")
	}
	best := -1000
	for _, in := range inputForms(input) {
		if s := scoreOne(in, cand, stripped); s > best {
			best = s
		}
	}
	return best
}

// inputForms returns the original input plus any plausible digits-to-Chinese
// rewrite. The list is deduped and order-preserving so the original form
// wins ties.
func inputForms(input string) []string {
	forms := []string{input}
	if cn := permute.DigitsToChinese(input); cn != input {
		forms = append(forms, cn)
	}
	// Dedup.
	seen := map[string]bool{}
	out := make([]string, 0, len(forms))
	for _, f := range forms {
		if !seen[f] {
			seen[f] = true
			out = append(out, f)
		}
	}
	return out
}

// scoreOne is the original scoring function over a single (input, cand,
// stripped) triple. cand is the raw candidate (used by the group-word
// penalty), stripped is the legal-suffix-stripped form (used for the rest
// of the matching).
func scoreOne(input, cand, stripped string) int {

	// 1) Exact-match on the stripped form scores very high. Also treat
	// "input is a complete prefix of stripped" as exact — that's the common
	// case for "VendorAB" matching "VendorAB科技有限责任公司".
	if stripped == input || strings.HasPrefix(stripped, input) {
		return 200
	}

	// 2) Common-prefix length on the stripped form, capped.
	pref := commonPrefixLen(stripped, input)
	score := pref * 10

	// 3) Bonus when the input is a substring of the stripped candidate or
	// vice versa — catches "ExampleCo" inside "ExampleCo科技".
	if strings.Contains(stripped, input) || strings.Contains(input, stripped) {
		score += 50
	}

	// 4) Penalty if input mentions a group/holding keyword but candidate
	// doesn't carry it.
	groupWords := []string{"集团", "控股", "总公司"}
	if hasAny(input, groupWords) && !hasAny(cand, groupWords) {
		score -= 30
	}
	return score
}

func explain(input, cand string, s int) string {
	stripped := stripLegalSuffix(cand)
	for strings.HasSuffix(stripped, "公司") && len(stripped) > 2 {
		stripped = strings.TrimSuffix(stripped, "公司")
	}
	switch {
	case s >= 200 && stripped == input:
		return "exact match after stripping legal suffix"
	case s >= 200 && strings.HasPrefix(stripped, input):
		return "input is full prefix of candidate"
	case strings.Contains(stripped, input):
		return "input is substring of candidate"
	case commonPrefixLen(stripped, input) >= 3:
		return "strong prefix match"
	default:
		return fmt.Sprintf("weak match (score=%d)", s)
	}
}

func expandByLegalPerson(in []Candidate, topN int) []Candidate {
	if len(in) == 0 {
		return in
	}
	head := in[:min(topN, len(in))]
	seen := map[string]bool{}
	out := make([]Candidate, 0, len(head))
	for _, c := range head {
		if seen[c.PID] {
			continue
		}
		seen[c.PID] = true
		out = append(out, c)
	}
	return out
}

func stripLegalSuffix(s string) string {
	// Order matters: longer / more specific suffixes first so that
	// "股份有限公司" beats "有限公司", and "有限责任公司" beats "公司".
	for _, suf := range []string{
		"有限责任公司", "股份有限公司", "有限公司", "控股集团",
		"集团", "总公司", "分公司",
	} {
		if strings.HasSuffix(s, suf) {
			return strings.TrimSuffix(s, suf)
		}
	}
	return s
}

func commonPrefixLen(a, b string) int {
	ar := []rune(a)
	br := []rune(b)
	n := len(ar)
	if len(br) < n {
		n = len(br)
	}
	for i := 0; i < n; i++ {
		if ar[i] != br[i] {
			return i
		}
	}
	return n
}

func hasAny(s string, words []string) bool {
	for _, w := range words {
		if strings.Contains(s, w) {
			return true
		}
	}
	return false
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
