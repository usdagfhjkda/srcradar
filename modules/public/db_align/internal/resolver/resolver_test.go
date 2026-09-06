package resolver

import "testing"

func TestStripLegalSuffix(t *testing.T) {
	cases := []struct{ in, want string }{
		{"ExampleCo子公司有限公司", "ExampleCo子公司"},
		{"TestBiz科技有限责任公司", "TestBiz科技"},
		{"LargeBiz集团股份有限公司", "LargeBiz集团"},
		{"DemoCorp", "DemoCorp"},
		{"北京TestBiz移动软件有限公司", "北京TestBiz移动软件"},
	}
	for _, c := range cases {
		if got := stripLegalSuffix(c.in); got != c.want {
			t.Errorf("stripLegalSuffix(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestScore_ExactAfterStrip(t *testing.T) {
	got := score("TestBiz", "TestBiz科技有限公司")
	if got < 200 {
		t.Errorf("exact-after-strip should score >=200, got %d", got)
	}
}

func TestScore_Prefix(t *testing.T) {
	// "VendorAB科技" is the full prefix of "VendorAB科技有限责任公司" after
	// stripping the legal suffix. The candidate has only one legal suffix;
	// the while-loop in score() does not strip further because the stripped
	// form "VendorAB科技" does not end in "公司".
	a := score("VendorAB科技", "VendorAB科技有限责任公司")
	b := score("VendorAB科技", "VendorAB科技股份有限公司")
	if a != 200 || b != 200 {
		t.Errorf("both should score 200 (full prefix), got a=%d b=%d", a, b)
	}
}

func TestScore_PrefixPartial(t *testing.T) {
	// "TestBiz科技" is the full prefix of "TestBiz科技" but "TestBiz科技" is NOT the full
	// prefix of "TestBiz" — that direction is a non-match.
	got := score("TestBiz科技", "TestBiz")
	if got >= 200 {
		t.Errorf("reverse prefix should not be 200, got %d", got)
	}
}

func TestScore_GroupPenalty(t *testing.T) {
	// input mentions 集团 but candidate doesn't → penalty
	pen := score("DemoCorp集团", "DemoCorp有限公司")
	noPen := score("DemoCorp", "DemoCorp有限公司")
	if pen >= noPen {
		t.Errorf("group-mention input should score lower: pen=%d noPen=%d", pen, noPen)
	}
}

func TestScore_DigitsToChinese(t *testing.T) {
	// "98同城" should match "九八同城信息技术有限公司" via the digits→Chinese
	// rewrite of the input. Without the rewrite, "98同城" shares no prefix
	// with "九八同城信息技术" so the score would be low.
	direct := score("九八同城", "九八同城信息技术有限公司")
	mixed := score("98同城", "九八同城信息技术有限公司")
	// The mixed form should reach the same strong-match tier as the direct form
	// because the resolver tries both spellings of the input.
	if mixed < 200 {
		t.Errorf("'98同城' against '九八同城信息技术有限公司' should score >=200 (digit rewrite), got %d", mixed)
	}
	if direct != 200 {
		t.Errorf("'九八同城' against '九八同城信息技术有限公司' should score 200 (full prefix), got %d", direct)
	}
}

func TestCommonPrefixLen(t *testing.T) {
	cases := []struct{ a, b string; want int }{
		{"abc科技", "abc通讯", 3},
		{"abc", "abd", 2},
		{"abc", "abc", 3},
		{"abc", "xyz", 0},
		{"", "abc", 0},
	}
	for _, c := range cases {
		if got := commonPrefixLen(c.a, c.b); got != c.want {
			t.Errorf("commonPrefixLen(%q,%q) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

func TestExpandByLegalPerson_Dedup(t *testing.T) {
	in := []Candidate{
		{PID: "1", Name: "A"},
		{PID: "1", Name: "A dup"},
		{PID: "2", Name: "B"},
	}
	out := expandByLegalPerson(in, 3)
	if len(out) != 2 {
		t.Errorf("expected dedup to 2, got %d", len(out))
	}
}
