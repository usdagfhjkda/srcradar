package scope

import "testing"

func TestNormaliseHost(t *testing.T) {
	cases := []struct{ in, want string }{
		{"example.com", "example.com"},
		{"  EXAMPLE.com  ", "example.com"},
		{"https://example.com/path", "example.com"},
		{"example.com:8080", "example.com"},
		{"example.com/path", "example.com"},
		{"localhost", ""},
		{"", ""},
		{"a b.com", ""},
		{"*.example.com", "example.com"},
	}
	for _, c := range cases {
		if got := normaliseHost(c.in); got != c.want {
			t.Errorf("normaliseHost(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestHostFromURL(t *testing.T) {
	cases := []struct{ in, want string }{
		{"https://example.com/p?q=1", "example.com"},
		{"http://www.example.com", "www.example.com"},
		{"example.com", "example.com"},
		{"not a url at all", ""},
		{"://broken", ""},
	}
	for _, c := range cases {
		if got := hostFromURL(c.in); got != c.want {
			t.Errorf("hostFromURL(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
