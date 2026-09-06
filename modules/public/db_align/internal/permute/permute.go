// Package permute generates a small set of keyword variants from a seed
// business name, so that asset reverse-lookup can be repeated under each
// alias and catch records indexed under brand / 曾用名 / 英文名.
//
// The generator is intentionally simple — it returns 1–6 strings, never
// hundreds, because each variant triggers an ENScan subprocess call.
package permute

import (
	"strings"
	"unicode/utf8"
)

// Variants returns the seed plus a handful of plausible aliases.
//
// The heuristics are deliberately cheap; they are NOT a Chinese name model.
// They cover the common patterns that show up in AQC's name history:
//   - 全称 vs 简称 ("ExampleCo" vs "ExampleCo子公司")
//   - English fallback when a name is Chinese ("XiaoMi" / "xiaomi")
//   - Brand-only tokens when the name has separators ("TestBiz-移动" → "TestBiz", "移动")
func Variants(seed string) []string {
	seed = strings.TrimSpace(seed)
	if seed == "" {
		return nil
	}
	out := []string{seed}
	seen := map[string]bool{seed: true}

	add := func(s string) {
		s = strings.TrimSpace(s)
		if s == "" || seen[s] {
			return
		}
		seen[s] = true
		out = append(out, s)
	}

	// 1) Split on common separators and try each token as a brand.
	for _, sep := range []string{"-", "·", "•", " ", "—", "－", "_"} {
		if strings.Contains(seed, sep) {
			for _, part := range strings.Split(seed, sep) {
				add(part)
			}
		}
	}

	// 2) 数字↔汉字 常见替换 (e.g. "98" ↔ "九八"). Only do 1-digit and
	//    2-digit ASCII digit groups; leave 3+ digit numbers alone.
	add(DigitsToChinese(seed))

	// 3) English fallback: pinyin-style lower-cased ASCII, only when the
	//    seed contains at least one ASCII letter.
	if hasASCIILetter(seed) {
		add(strings.ToLower(seed))
		add(onlyASCIILetters(seed))
	}

	return out
}

// DigitsToChinese replaces 1-2 digit ASCII runs (bounded by non-ASCII-letter
// characters) with their Chinese counterparts: "98" → "九八", "1号店" →
// "一号店". Exported so the resolver can use the same conversion when
// matching user input like "98同城" against AQC's "九八同城信息技术" form.
func DigitsToChinese(s string) string {
	// Map only the standalone 0-9 groups at length 1-2, AND only when the
	// surrounding characters are not ASCII letters (avoids rewriting model
	// identifiers like "x86", "i386", "ARM64").
	subs := map[string]string{
		"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
		"5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
	}
	var b strings.Builder
	runes := []rune(s)
	isASCIILetter := func(c rune) bool { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') }
	isDigit := func(c rune) bool { return c >= '0' && c <= '9' }
	for i := 0; i < len(runes); i++ {
		c := runes[i]
		if isDigit(c) {
			j := i
			for j < len(runes) && isDigit(runes[j]) {
				j++
			}
			runLen := j - i
			prevIsLetter := i > 0 && isASCIILetter(runes[i-1])
			nextIsLetter := j < len(runes) && isASCIILetter(runes[j])
			prevIsDigit := i > 0 && isDigit(runes[i-1])
			nextIsDigit := j < len(runes) && isDigit(runes[j])
			if runLen <= 2 && !prevIsDigit && !nextIsDigit && !prevIsLetter && !nextIsLetter {
				for k := i; k < j; k++ {
					b.WriteString(subs[string(runes[k])])
				}
				i = j - 1
				continue
			}
		}
		b.WriteRune(c)
	}
	return b.String()
}

func hasASCIILetter(s string) bool {
	for _, c := range s {
		if c >= 'A' && c <= 'Z' {
			return true
		}
		if c >= 'a' && c <= 'z' {
			return true
		}
	}
	return false
}

func onlyASCIILetters(s string) string {
	var b strings.Builder
	for _, c := range s {
		if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') {
			b.WriteRune(c)
		}
	}
	return b.String()
}

// RuneCount is a small helper for callers that want to log variant size.
func RuneCount(s string) int { return utf8.RuneCountInString(s) }
