package permute

import (
	"reflect"
	"testing"
)

func TestVariants_ChineseName(t *testing.T) {
	got := Variants("ExampleCo子公司")
	if !contains(got, "ExampleCo子公司") {
		t.Errorf("seed missing: %v", got)
	}
	if !contains(got, "ExampleCo") {
		t.Errorf("expected ExampleCo alias (separator-split token), got %v", got)
	}
}

func TestVariants_EnglishName(t *testing.T) {
	got := Variants("TestBiz")
	if !contains(got, "testbiz") {
		t.Errorf("expected lowercase, got %v", got)
	}
}

func TestVariants_Separated(t *testing.T) {
	got := Variants("TestBiz-移动")
	if !contains(got, "TestBiz") || !contains(got, "移动") {
		t.Errorf("expected tokens split, got %v", got)
	}
}

func TestVariants_DigitsToChinese_3DigitsLeftAlone(t *testing.T) {
	// 3+ digit runs shouldn't be converted (noise, ambiguous).
	if got := Variants("10086"); contains(got, "一零零八六") {
		t.Errorf("3-digit run should not be converted, got %v", got)
	}
}

func TestDigitsToChinese(t *testing.T) {
	cases := []struct{ in, want string }{
		{"98", "九八"},
		{"1号店", "一号店"},
		{"1234", "1234"}, // 3+ digits, not converted
		{"x86", "x86"},   // not standalone
	}
	for _, c := range cases {
		if got := DigitsToChinese(c.in); got != c.want {
			t.Errorf("DigitsToChinese(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func contains(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

func TestVariants_Dedup(t *testing.T) {
	// Chinese-only seed that doesn't trigger the digits-to-Chinese,
	// English-fallback, or separator-split branches.
	got := Variants("占位测试名")
	if !reflect.DeepEqual(got, []string{"占位测试名"}) {
		t.Errorf("seed-only should dedup to single element, got %v", got)
	}
}
