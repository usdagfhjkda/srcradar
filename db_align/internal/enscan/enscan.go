// Package enscan wraps the local ENScan_GO CLI as a subprocess and parses
// its JSON output. The runner shells out to ENScan for each step (search,
// holding-tree, asset reverse-lookup) so that the upstream tool's anti-bot
// behaviour, cookie rotation and source-switching stay exactly as designed.
package enscan

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// Field section keys in ENScan's JSON output. Matches common/output.go
// ENSMapLN in the upstream repo.
const (
	SecEnterpriseInfo = "enterprise_info"
	SecICP            = "icp"
	SecAPP            = "app"
	SecWechat         = "wechat"
	SecWxApp          = "wx_app"
	SecWeibo          = "weibo"
	SecSupplier       = "supplier"
	SecJob            = "job"
	SecInvest         = "invest"
	SecBranch         = "branch"
	SecHolds          = "holds"
	SecCopyright      = "copyright"
	SecPartner        = "partner"
)

// AllAssetSections lists every opt-in asset section the runner supports.
// Anything NOT in this list (invest/branch) is part of the holding tree
// and always queried.
var AllAssetSections = []string{
	SecICP, SecAPP, SecWechat, SecWxApp, SecWeibo,
	SecSupplier, SecJob, SecCopyright, SecPartner,
}

// HoldingTreeSections are always queried (the default mode of this tool).
// SecHolds is intentionally excluded: AQC's per-company endpoint returns
// `holds` as null for typical companies (the "企业图谱" / 控股 graph is a
// separate paid feature). Querying it only burns an HTTP round trip.
var HoldingTreeSections = []string{SecInvest, SecBranch}

// FieldOf returns the -field argument value for a section.
func FieldOf(sec string) string { return sec }

// SectionToDBType maps an ENScan output section to a mapp_records.service_type
// hint. The actual service_type integer comes from AQC's raw response; this
// is only used as a default when the upstream value is missing.
var SectionToDBType = map[string]int{
	SecAPP:       6, // observed convention in upstream data
	SecWxApp:     7, // observed: all 9 known records are 7 = 微信小程序
	SecWechat:    4, // placeholder until confirmed against raw AQC
	SecWeibo:     5, // placeholder
	SecCopyright: 8, // placeholder
	SecSupplier:  9, // placeholder
	SecJob:       10, // placeholder
	SecPartner:   11, // placeholder
}

// Result is the parsed JSON ENScan writes to disk in -json mode.
// It is a flat map keyed by section; each value is a list of records.
type Result struct {
	Sections map[string][]map[string]string `json:"-"`
	Raw      []byte                         `json:"-"`
	Path     string                         `json:"-"`
}

// Company is a thin projection over the enterprise_info section used by the
// resolver. PID is the AQC internal id (== companies.id in upstream).
type Company struct {
	Name        string
	LegalPerson string
	Status      string
	RegCode     string
	PID         string
	Nature      string
}

// Companies extracts the enterprise_info records (there is usually exactly
// one per search).
func (r *Result) Companies() []Company {
	var out []Company
	for _, m := range r.Sections[SecEnterpriseInfo] {
		out = append(out, Company{
			Name:        m["name"],
			LegalPerson: m["legal_person"],
			Status:      m["status"],
			RegCode:     m["reg_code"],
			PID:         m["pid"],
		})
	}
	return out
}

// HoldingCompany is a record from invest/branch/holds. Name and PID are
// sufficient for further recursion.
type HoldingCompany struct {
	Name     string
	PID      string
	Scale    string // 投资比例 (e.g. "100%")
	Level    string // 持股层级 (holds only)
	Relation string // 分支/控股/投资
}

// Holdings flattens invest+branch+holds into a single list, tagging each
// entry with the source section.
func (r *Result) Holdings() []HoldingCompany {
	tag := func(sec string) string {
		switch sec {
		case SecInvest:
			return "invest"
		case SecBranch:
			return "branch"
		case SecHolds:
			return "holds"
		}
		return sec
	}
	var out []HoldingCompany
	for _, sec := range HoldingTreeSections {
		for _, m := range r.Sections[sec] {
			out = append(out, HoldingCompany{
				Name:     m["name"],
				PID:      m["pid"],
				Scale:    m["scale"],
				Level:    m["level"],
				Relation: tag(sec),
			})
		}
	}
	return out
}

// Records returns the records of a given section as a generic slice.
func (r *Result) Records(section string) []map[string]string {
	return r.Sections[section]
}

// DefaultProxy is the default -proxy value passed to every ENScan call.
// Empty string means "do not pass -proxy; let enscan connect directly".
// Override per-run via the CLI -proxy flag, or per-call via WithProxy.
const DefaultProxy = ""

// Runner invokes the ENScan binary.
type Runner struct {
	Binary  string        // path to enscan binary, e.g. ./ENScan_GO/ENScan
	Types   []string      // data sources, default ["aqc","tyc","rb","qimai"]
	Delay   time.Duration // sleep before/after each call
	Timeout time.Duration // per-call timeout (default 5 min)
	Proxy   string        // default -proxy value passed to every call (empty = no proxy)
}

// NewRunner returns a Runner with sensible defaults.
func NewRunner(binary string) *Runner {
	return &Runner{
		Binary:  binary,
		Types:   []string{"aqc", "tyc", "rb", "qimai"},
		Delay:   0,
		Timeout: 5 * time.Minute,
		Proxy:   DefaultProxy,
	}
}

// Search runs `enscan -n <name> [-type a,kc,...] [-field ...] [-invest N] [-hold] [-branch]` and
// parses the resulting JSON file written to OutDir.
//
// The upstream binary always writes a JSON file when -json is passed, named
// "<keyword>-<date>-<unix>.json". We use a unique tmp dir per call and
// return the merged Result.
func (r *Runner) Search(ctx context.Context, name string, fields []string, opts ...SearchOpt) (*Result, error) {
	o := searchOpts{invest: 0, hold: false, branch: false, deep: 0}
	for _, fn := range opts {
		fn(&o)
	}
	if o.delay > 0 {
		time.Sleep(o.delay)
	}
	if r.Delay > 0 {
		time.Sleep(r.Delay)
	}

	tmpDir, err := tmpOutDir()
	if err != nil {
		return nil, err
	}

	args := []string{
		"-n", name,
		"-type", strings.Join(r.Types, ","),
		"-json",
		"-out-dir", tmpDir,
		"-out-type", "json",
		"-is-show=false",
	}
	if len(fields) > 0 {
		args = append(args, "-field", strings.Join(fields, ","))
	}
	if o.invest > 0 {
		args = append(args, "-invest", fmt.Sprintf("%d", int(o.invest)))
	}
	if o.hold {
		args = append(args, "-hold")
	}
	if o.branch {
		args = append(args, "-branch")
	}
	if o.deep > 0 {
		args = append(args, "-deep", fmt.Sprintf("%d", o.deep))
	}
	// Per-call proxy wins; otherwise inherit the runner-level default.
	if o.proxy == "" {
		o.proxy = r.Proxy
	}
	if o.proxy != "" {
		args = append(args, "-proxy", o.proxy)
	}

	cmd := exec.CommandContext(ctx, r.Binary, args...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("enscan failed: %w (stderr: %s)", err, stderr.String())
	}
	if r.Delay > 0 {
		time.Sleep(r.Delay)
	}
	if o.delay > 0 {
		time.Sleep(o.delay)
	}

	// Find the most recent .json file in tmpDir.
	path, err := latestJSON(tmpDir)
	if err != nil {
		return nil, err
	}
	res, err := parseJSONFile(path)
	if err != nil {
		return nil, err
	}
	return res, nil
}

// SearchOpt configures a single Search call.
type SearchOpt func(*searchOpts)

type searchOpts struct {
	invest float64
	hold   bool
	branch bool
	deep   int
	delay  time.Duration
	proxy  string
}

// WithInvest sets the -invest ratio filter (e.g. 51 for ≥51%).
func WithInvest(pct float64) SearchOpt { return func(o *searchOpts) { o.invest = pct } }

// WithHold enables -hold (include holding companies).
func WithHold(b bool) SearchOpt { return func(o *searchOpts) { o.hold = b } }

// WithBranch enables -branch (include branch offices).
func WithBranch(b bool) SearchOpt { return func(o *searchOpts) { o.branch = b } }

// WithDeep sets the recursion depth.
func WithDeep(d int) SearchOpt { return func(o *searchOpts) { o.deep = d } }

// WithDelay adds a per-call delay (cumulative with Runner.Delay).
func WithDelay(d time.Duration) SearchOpt { return func(o *searchOpts) { o.delay = d } }

// WithProxy routes the call through a proxy string (passed as -proxy).
func WithProxy(p string) SearchOpt { return func(o *searchOpts) { o.proxy = p } }
