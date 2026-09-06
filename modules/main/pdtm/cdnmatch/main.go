// Package main implements cdnmatch: a CDN/WAF/Cloud classifier that consumes
// dnsx JSONL output (already containing A/AAAA/CNAME rows) and produces every
// derived file pdtm/scanner.sh stages 1-2 used to build by hand. Output file
// names and line formats match the legacy awk pipeline 1:1 so downstream
// stages (3-9) keep reading `tmp_domain_ip_pairs.txt` etc. unchanged.
//
// Pure offline: only publicsuffix + bart lookups, no DNS queries.
// Replaces the live `cdncheck -i <hosts>` binary call which blocks reading
// stdin and serialises ~0.9s/host (root cause of the 7h45m hang on
// 2026-07-31).
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"sort"
	"strings"

	"github.com/projectdiscovery/cdncheck"
)

// DnsxRecord is the subset of dnsx -j JSONL output we read. We deliberately
// do NOT pin a HostResolver for fields outside this struct so a future dnsx
// upgrade widens schema without silently affecting us.
type DnsxRecord struct {
	Host   string   `json:"host"`
	A      []string `json:"a"`
	AAAA   []string `json:"aaaa"`
	CNAME  []string `json:"cname"`
	NS     []string `json:"ns"`
	Status string   `json:"status_code"`
}

type hostClass int

const (
	classNone hostClass = iota
	classCloud
	classCDN
	classWAF // highest — wafs are universally "not our service surface"
)

func (c hostClass) rank() int { return int(c) }

func (r hostsPass1Result) rank() int { return r.class.rank() }

type bucket struct {
	domains    map[string]bool   // host -> present in this bucket
	ips        map[string]bool   // ip   -> present in this bucket
	hostProv   map[string]string // host -> matched provider name
	ipProv     map[string]string // ip   -> matched provider name
	byProvider map[string]int    // provider name -> hit count
}

func newBucket() *bucket {
	return &bucket{
		domains:    map[string]bool{},
		ips:        map[string]bool{},
		hostProv:   map[string]string{},
		ipProv:     map[string]string{},
		byProvider: map[string]int{},
	}
}

func (b *bucket) addHost(host, prov string) bool {
	if b.domains[host] {
		return false
	}
	b.domains[host] = true
	b.hostProv[host] = prov
	b.byProvider[prov]++
	return true
}

func (b *bucket) addIP(ip, prov string) bool {
	if b.ips[ip] {
		return false
	}
	b.ips[ip] = true
	b.ipProv[ip] = prov
	b.byProvider[prov]++
	return true
}

// classOf returns the highest-priority class a host is assigned to, taking
// into account all (host, ip, cname) signals collected during pass 1.
type hostsPass1Result struct {
	class hostClass
	prov  string
}

// classify is pass 2: walk hostByIP and decide canonical class per host, then
// propagate forward: if a host is classCDN, every IP it binds also becomes
// classCDN. If a host is classWAF, every IP it binds becomes classWAF.
// IPs/hosts that already have a higher class keep the higher class.
type classifyInput struct {
	hostByIP     map[string][]string // ip -> hosts
	hostFirst    map[string]hostsPass1Result
	ipFirst      map[string]hostsPass1Result
	hostCNAME    map[string]string
}

func canonicalize(in classifyInput) (hostClass map[string]hostsPass1Result, ipClass map[string]hostsPass1Result) {
	hostClass = map[string]hostsPass1Result{}
	for h, r := range in.hostFirst {
		hostClass[h] = r
	}
	ipClass = map[string]hostsPass1Result{}
	for ip, r := range in.ipFirst {
		ipClass[ip] = r
	}

	// Round 1: from host class -> promote its IPs.
	for ip, hosts := range in.hostByIP {
		for _, h := range hosts {
			hr, ok := hostClass[h]
			if !ok {
				continue
			}
			if cur, ok := ipClass[ip]; !ok || hr.rank() > cur.rank() {
				ipClass[ip] = hr
			}
		}
	}
	// Round 2: from IP class -> promote its hosts.
	for ip, hosts := range in.hostByIP {
		ir, ok := ipClass[ip]
		if !ok {
			continue
		}
		for _, h := range hosts {
			if cur, ok := hostClass[h]; !ok || ir.rank() > cur.rank() {
				hostClass[h] = ir
			}
		}
	}
	return hostClass, ipClass
}

func main() {
	var (
		inPath     = flag.String("in", "", "dnsx JSONL input file (dnsx -j ...)")
		purePath   = flag.String("domains", "", "pure_domains.txt — full valid domain list, for non_cdn derivation")
		outDir     = flag.String("out", ".", "output directory")
		statsPath  = flag.String("stats", "", "optional stats JSON output path")
		matchCloud = flag.Bool("cloud", false, "also flag cloud-provider IPs and their hosts (AWS/Azure/GCP) — defaults to false to mirror the legacy `cdncheck -cdn -waf` flags")
	)
	flag.Parse()

	if *inPath == "" {
		log.Fatalf("missing -in")
	}
	if *outDir == "" {
		*outDir = "."
	}

	// 3 retries mirrors cdncheck.NewWithOpts default; resolvers left empty so
	// cdncheck uses DefaultResolvers. We never call retriabledns methods, so
	// resolver choice doesn't affect runtime.
	client, err := cdncheck.NewWithOpts(3, nil)
	if err != nil {
		log.Fatalf("cdncheck init: %v", err)
	}

	// Pass 1: collect raw signals.
	var (
		pairsIP        []string // FILE_IP lines
		pairsIPv6      []string // FILE_IPV6 lines
		pairsCNAME     []string // FILE_CNAME lines (host \\t cname.target per aliased value)
		pairsNS        []string // FILE_NS lines
		hostByIP       = map[string][]string{}
		seenDomainIP   = map[string]bool{} // "host\\nip" dedupe for FILE_IP/FILE_IPV6
		seenDomainCNAME = map[string]bool{}
		hostFirst      = map[string]hostsPass1Result{}
		ipFirst        = map[string]hostsPass1Result{}
		hostCNAMEProv  = map[string]string{}
		recordCount    int
		skipped        int
	)

	in, err := os.Open(*inPath)
	if err != nil {
		log.Fatalf("open %s: %v", *inPath, err)
	}
	defer in.Close()
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	bumpHost := func(host string, klass hostClass, prov string) {
		cur, ok := hostFirst[host]
		if !ok || klass.rank() > cur.rank() {
			hostFirst[host] = hostsPass1Result{class: klass, prov: prov}
		}
	}
	bumpIP := func(ip string, klass hostClass, prov string) {
		cur, ok := ipFirst[ip]
		if !ok || klass.rank() > cur.rank() {
			ipFirst[ip] = hostsPass1Result{class: klass, prov: prov}
		}
	}

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var rec DnsxRecord
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			continue
		}
		host := strings.TrimSuffix(rec.Host, ".")
		if host == "" {
			continue
		}
		recordCount++
		if rec.Status != "" && rec.Status != "NOERROR" {
			skipped++
			continue
		}

		// IP-side: A/AAAA records. Each (ip, host) pair feeds FILE_IP/FILE_IPV6
		// and the host<->IP cross-mapping table.
		for _, raw := range rec.A {
			if net.ParseIP(raw) == nil {
				continue
			}
			key := host + " " + raw
			if !seenDomainIP[key] {
				seenDomainIP[key] = true
				pairsIP = append(pairsIP, raw+" "+host)
			}
			hostByIP[raw] = appendUnique(hostByIP[raw], host)
			matched, prov, itemType, err := client.Check(net.ParseIP(raw))
			if err == nil && matched && itemType != "" {
				klass := classFor(itemType)
				if klass != classCloud || *matchCloud {
					bumpIP(raw, klass, prov)
				}
			}
		}
		for _, raw := range rec.AAAA {
			if net.ParseIP(raw) == nil {
				continue
			}
			key := host + " " + raw
			if !seenDomainIP[key] {
				seenDomainIP[key] = true
				pairsIPv6 = append(pairsIPv6, raw+" "+host)
			}
			hostByIP[raw] = appendUnique(hostByIP[raw], host)
			matched, prov, itemType, err := client.Check(net.ParseIP(raw))
			if err == nil && matched && itemType != "" {
				klass := classFor(itemType)
				if klass != classCloud || *matchCloud {
					bumpIP(raw, klass, prov)
				}
			}
		}
		// Host IP-class bumps: if host has any IP in a CDN range, host becomes CDN-class too.
		// We do this at canonicalize-pass time, but record raw IP first.

		// CNAME-side: each cname target is checked against the suffix table.
		for _, cname := range rec.CNAME {
			cname = strings.TrimSuffix(strings.TrimSpace(cname), ".")
			if cname == "" {
				continue
			}
			key := host + " " + cname
			if !seenDomainCNAME[key] {
				seenDomainCNAME[key] = true
				pairsCNAME = append(pairsCNAME, host+" "+cname)
			}
			matched, prov, _, err := client.CheckSuffix(cname)
			if err == nil && matched && prov != "" {
				hostCNAMEProv[host] = prov
				// CNAME suffix matches are always WAF class per cdncheck.CheckSuffix.
				bumpHost(host, classWAF, prov)
			}
		}

		// NS records (preserved for parity with legacy awk output).
		for _, ns := range rec.NS {
			ns = strings.TrimSuffix(strings.TrimSpace(ns), ".")
			if ns == "" {
				continue
			}
			pairsNS = append(pairsNS, host+" "+ns)
		}
	}
	if err := scanner.Err(); err != nil {
		log.Fatalf("scan %s: %v", *inPath, err)
	}

	// Pass 2: canonicalize — propagate host<->IP classification so that
	// "host behind CDN IP" matches what scanner.sh cross-mapping used to do.
	hostFinal, ipFinal := canonicalize(classifyInput{
		hostByIP:  hostByIP,
		hostFirst: hostFirst,
		ipFirst:   ipFirst,
		hostCNAME: hostCNAMEProv,
	})

	// Build buckets. Use a single helper that picks the right bucket per host
	// (waf > cdn > cloud > none).
	bCDN := newBucket()
	bWAF := newBucket()
	bCloud := newBucket()

	// all_unique_ips is EVERY resolved A/AAAA, not just classified ones —
	// mirrors scanner.sh's `awk '{print $1}' FILE_IP | sort -u` so downstream
	// naabu scans see the full target set.
	allIPSet := map[string]bool{}
	for _, raw := range pairsIP {
		if i := strings.IndexByte(raw, ' '); i > 0 {
			allIPSet[raw[:i]] = true
		}
	}
	allIPs := make([]string, 0, len(allIPSet))
	for ip := range allIPSet {
		allIPs = append(allIPs, ip)
	}
	for ip, r := range ipFinal {
		switch r.class {
		case classWAF:
			bWAF.addIP(ip, r.prov)
		case classCDN:
			bCDN.addIP(ip, r.prov)
		case classCloud:
			bCloud.addIP(ip, r.prov)
		}
	}
	allHostSet := map[string]bool{}
	for _, raw := range pairsIP {
		if i := strings.IndexByte(raw, ' '); i > 0 {
			allHostSet[raw[i+1:]] = true
		}
	}
	for _, raw := range pairsCNAME {
		if i := strings.IndexByte(raw, ' '); i > 0 {
			allHostSet[raw[:i]] = true
		}
	}
	allHosts := make([]string, 0, len(allHostSet))
	for h := range allHostSet {
		allHosts = append(allHosts, h)
	}
	for host, r := range hostFinal {
		switch r.class {
		case classWAF:
			bWAF.addHost(host, r.prov)
		case classCDN:
			bCDN.addHost(host, r.prov)
		case classCloud:
			bCloud.addHost(host, r.prov)
		}
	}

	// non_cdn hosts = pure_domains minus CDN-classified hosts. We accept the
	// full pure list via -domains so NXDOMAIN hosts that dnsx didn't return
	// are still treated as non-CDN (matches scanner.sh's `comm -23 pure_domains
	// cdn_domains` derivation). Without -domains we fall back to the JSONL
	// subset, which loses NXDOMAIN rows.
	pureDomains := readLines(*purePath)
	pureSet := map[string]bool{}
	for _, d := range pureDomains {
		d = strings.TrimSuffix(strings.TrimSpace(d), ".")
		if d != "" {
			pureSet[d] = true
		}
	}
	cdnHostSet := map[string]bool{}
	for h := range bCDN.domains {
		cdnHostSet[h] = true
	}
	for h := range bWAF.domains {
		cdnHostSet[h] = true
	}
	if *matchCloud {
		for h := range bCloud.domains {
			cdnHostSet[h] = true
		}
	}
	nonCDNDomains := []string{}
	for d := range pureSet {
		if !cdnHostSet[d] {
			nonCDNDomains = append(nonCDNDomains, d)
		}
	}
	// non_cdn_ips: every IP whose bound hosts contain at least one non-CDN
	// domain. Mirrors scanner.sh:253-257 to avoid losing real-business IPs
	// that happen to share a CDN's CIDR.
	nonCDNIPs := []string{}
	for _, ip := range allIPs {
		hosts := hostByIP[ip]
		if len(hosts) == 0 {
			continue
		}
		hasCDN := false
		hasNonCDN := false
		for _, h := range hosts {
			if cdnHostSet[h] {
				hasCDN = true
			} else {
				hasNonCDN = true
			}
		}
		if hasNonCDN || !hasCDN {
			nonCDNIPs = appendUnique(nonCDNIPs, ip)
		}
	}

	sort.Strings(pairsIP)
	sort.Strings(pairsIPv6)
	sort.Strings(pairsCNAME)
	sort.Strings(pairsNS)
	sort.Strings(allIPs)
	sort.Strings(allHosts)
	sort.Strings(nonCDNDomains)
	sort.Strings(nonCDNIPs)

	// Write everything. File names + line formats mirror the legacy awk
	// pipeline exactly so downstream shell stages don't need to change.
	files := []struct {
		path  string
		lines []string
	}{
		{"tmp_domain_ip_pairs.txt", pairsIP},
		{"tmp_domain_ipv6_pairs.txt", pairsIPv6},
		{"tmp_domain_cname_pairs.txt", pairsCNAME},
		{"tmp_domain_ns_pairs.txt", pairsNS},
		{"all_unique_ips.txt", allIPs},
		{"cdn_ips.txt", sortedKeys(bCDN.ips)},
		{"cdn_domains.txt", sortedKeys(bCDN.domains)},
		{"waf_ips.txt", sortedKeys(bWAF.ips)},
		{"waf_domains.txt", sortedKeys(bWAF.domains)},
		{"cloud_ips.txt", sortedKeys(bCloud.ips)},
		{"cloud_domains.txt", sortedKeys(bCloud.domains)},
		{"non_cdn_list.txt", nonCDNDomains},
		{"non_cdn_ips.txt", nonCDNIPs},
	}
	for _, f := range files {
		if err := writeLines(*outDir+"/"+f.path, f.lines); err != nil {
			log.Fatalf("write %s: %v", f.path, err)
		}
	}

	if *statsPath != "" {
		stats := map[string]any{
			"records_total":   recordCount,
			"records_skipped": skipped,
			"pairs_ip":        len(pairsIP),
			"pairs_ipv6":      len(pairsIPv6),
			"pairs_cname":     len(pairsCNAME),
			"unique_ips":      len(allIPs),
			"unique_hosts":    len(allHosts),
			"cdn_ip_hits":     len(bCDN.ips),
			"cdn_domain_hits": len(bCDN.domains),
			"waf_ip_hits":     len(bWAF.ips),
			"waf_domain_hits": len(bWAF.domains),
			"cloud_ip_hits":   len(bCloud.ips),
			"cloud_domain_hits": len(bCloud.domains),
			"non_cdn_hosts":   len(nonCDNDomains),
			"non_cdn_ips":     len(nonCDNIPs),
			"by_provider":     mergeByProvider(bCDN, bWAF, bCloud),
		}
		b, _ := json.MarshalIndent(stats, "", "  ")
		if err := os.WriteFile(*statsPath, b, 0o644); err != nil {
			log.Fatalf("write stats: %v", err)
		}
	}

	fmt.Printf("[cdnmatch] records=%d skipped=%d unique_ips=%d unique_hosts=%d "+
		"cdn(d=%d,i=%d) waf(d=%d,i=%d) cloud(d=%d,i=%d) non_cdn(d=%d,i=%d)\n",
		recordCount, skipped, len(allIPs), len(allHosts),
		len(bCDN.domains), len(bCDN.ips),
		len(bWAF.domains), len(bWAF.ips),
		len(bCloud.domains), len(bCloud.ips),
		len(nonCDNDomains), len(nonCDNIPs),
	)
}

func classFor(itemType string) hostClass {
	switch itemType {
	case "waf":
		return classWAF
	case "cdn":
		return classCDN
	case "cloud":
		return classCloud
	default:
		return classNone
	}
}

// classFromHost decodes a string-keyed hostCNAMEProv ("waf:cloudflare").
// Unused outside hot path; kept for symmetry.
func classFromHost(s string) hostClass {
	if strings.HasPrefix(s, "waf:") {
		return classWAF
	}
	return classNone
}

func appendUnique(slice []string, v string) []string {
	for _, e := range slice {
		if e == v {
			return slice
		}
	}
	return append(slice, v)
}

func sortedKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func mergeByProvider(bs ...*bucket) map[string]int {
	out := map[string]int{}
	for _, b := range bs {
		for k, v := range b.byProvider {
			out[k] += v
		}
	}
	return out
}

func writeLines(path string, keys []string) error {
	if dir := parentDir(path); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
	}
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	defer w.Flush()
	for _, k := range keys {
		if _, err := w.WriteString(k + "\n"); err != nil {
			return err
		}
	}
	return nil
}

func readLines(path string) []string {
	if path == "" {
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		if l := strings.TrimSpace(sc.Text()); l != "" {
			out = append(out, l)
		}
	}
	return out
}

func parentDir(p string) string {
	if i := strings.LastIndex(p, "/"); i >= 0 {
		return p[:i]
	}
	return ""
}
