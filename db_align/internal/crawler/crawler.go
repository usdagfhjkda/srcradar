// Package crawler implements two pipelines driven by ENScan:
//
//   1. HoldingTree: given a starting legal entity (pid+name), recursively
//      walk its invest/branch/holds graph down to a configurable depth
//      (default unlimited, gated by an in-memory cycle guard).
//
//   2. Assets: for every entity the tree produced, optionally reverse-lookup
//      each opt-in asset section (icp, app, wechat, ...). Each section is
//      independent so the user can opt in/out per field.
//
// All results are written through the store package, not returned to the
// caller — the runner prints progress and exits 0/1.
package crawler

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	"db_align/internal/enscan"
	"db_align/internal/permute"
	"db_align/internal/store"
)

// Opts controls a full crawl run.
type Opts struct {
	InvestThreshold float64 // %; 0 = no filter; default 51
	AssetSections   []string
	Broad           bool
	Delay           time.Duration
	Permute         bool   // run keyword variants per company
	MaxDepth        int    // 0 = unlimited (default); >0 caps tree depth from root
	TreeRetries     int    // # of retries on transient enscan errors during tree walk; default 1
	NoBranch        bool   // skip 分公司 (branch offices); they are not separate legal entities
}

// DefaultOpts returns a strict, holding-tree-only configuration.
func DefaultOpts() Opts {
	return Opts{InvestThreshold: 51, Permute: true}
}

// Crawler owns the runner + store and runs a full pipeline.
type Crawler struct {
	Runner   *enscan.Runner
	Store    *store.Store
	Business int64
	Opts     Opts
	Logger   *log.Logger
}

// New returns a Crawler.
func New(r *enscan.Runner, s *store.Store, bizID int64, o Opts, lg *log.Logger) *Crawler {
	if lg == nil {
		lg = log.Default()
	}
	return &Crawler{Runner: r, Store: s, Business: bizID, Opts: o, Logger: lg}
}

// HoldingNode is one entity visited during tree walk.
type HoldingNode struct {
	CompanyID int64
	Name      string
	PID       string
	Depth     int
	Parent    string // parent company name (empty for root)
	Relation  string // "root" | "invest" | "branch" | "holds"
	Scale     string
}

// RunTree walks the holding graph starting at root and persists every
// visited company to the store. Returns the list of visited nodes
// (deduplicated by PID, depth-first).
func (c *Crawler) RunTree(ctx context.Context, rootName, rootPID string) ([]HoldingNode, error) {
	if rootPID == "" {
		return nil, errors.New("root pid is required")
	}
	seen := map[string]bool{} // pid → visited
	var out []HoldingNode

	var walk func(name, pid string, depth int, parent, relation, scale string) error
	walk = func(name, pid string, depth int, parent, relation, scale string) error {
		if seen[pid] {
			return nil
		}
		seen[pid] = true

		cid, err := c.persistCompany(ctx, name)
		if err != nil {
			return fmt.Errorf("persist %q: %w", name, err)
		}
		node := HoldingNode{
			CompanyID: cid, Name: name, PID: pid, Depth: depth,
			Parent: parent, Relation: relation, Scale: scale,
		}
		out = append(out, node)
		c.Logger.Printf("  tree  depth=%d pid=%s name=%q relation=%s parent=%q",
			depth, pid, name, relation, parent)

		// Depth cap. 0 = unlimited (legacy behavior). A positive value stops
		// descent at depth==MaxDepth (root counted as depth 0).
		if c.Opts.MaxDepth > 0 && depth >= c.Opts.MaxDepth {
			return nil
		}

		// Fetch the next layer. AQC's per-company endpoint doesn't populate
		// the "holds" section for typical companies (the 控股 graph lives in
		// the paid 企业图谱), so we only request invest + branch.
		sections := enscan.HoldingTreeSections
		if c.Opts.NoBranch {
			sections = []string{enscan.SecInvest}
		}
		opts := []enscan.SearchOpt{
			enscan.WithInvest(c.Opts.InvestThreshold),
			enscan.WithBranch(!c.Opts.NoBranch),
		}
		if c.Opts.Delay > 0 {
			opts = append(opts, enscan.WithDelay(c.Opts.Delay))
		}
		retries := c.Opts.TreeRetries
		if retries < 1 {
			retries = 1
		}
		var res *enscan.Result
		for attempt := 0; attempt < retries; attempt++ {
			res, err = c.Runner.Search(ctx, name, sections, opts...)
			if err == nil {
				break
			}
			if attempt < retries-1 {
				c.Logger.Printf("  tree  depth=%d pid=%s holding fetch error (attempt %d/%d): %v (retrying)",
					depth, pid, attempt+1, retries, err)
				time.Sleep(time.Duration(2*(attempt+1)) * time.Second)
			}
		}
		if err != nil {
			// 多数情况下 invest/branch/holds 任一为空时上游仍返回 0 行；只有
			// 网络/账号/被风控时才会 error。这里选择 log + 继续，不阻断整棵树。
			c.Logger.Printf("  tree  depth=%d pid=%s holding fetch error after %d attempt(s): %v (skipped)",
				depth, pid, retries, err)
			return nil
		}
		for _, h := range res.Holdings() {
			if h.PID == "" || h.Name == "" {
				continue
			}
			if err := walk(h.Name, h.PID, depth+1, name, h.Relation, h.Scale); err != nil {
				c.Logger.Printf("  tree  descend error: %v", err)
			}
		}
		return nil
	}

	if err := walk(rootName, rootPID, 0, "", "root", ""); err != nil {
		return out, err
	}
	return out, nil
}

// RunAssets reverse-lookups every opt-in asset section for every company
// returned by RunTree. Results are written into mapp_records; nothing is
// returned.
func (c *Crawler) RunAssets(ctx context.Context, nodes []HoldingNode) error {
	if len(c.Opts.AssetSections) == 0 {
		c.Logger.Printf("assets: no sections requested, skipping")
		return nil
	}
	for _, n := range nodes {
		if n.PID == "" {
			continue
		}
		keywords := []string{n.Name}
		if c.Opts.Permute {
			keywords = permute.Variants(n.Name)
		}
		seen := map[string]bool{} // dedup (section, key) within a single company
		for _, kw := range keywords {
			opts := []enscan.SearchOpt{}
			if c.Opts.Delay > 0 {
				opts = append(opts, enscan.WithDelay(c.Opts.Delay))
			}
			res, err := c.Runner.Search(ctx, kw, c.Opts.AssetSections, opts...)
			if err != nil {
				c.Logger.Printf("  assets pid=%s kw=%q error: %v (skipped)", n.PID, kw, err)
				continue
			}
			if err := c.persistAssets(n, res, seen); err != nil {
				c.Logger.Printf("  assets pid=%s persist error: %v", n.PID, err)
			}
		}
	}
	return nil
}

// persistAssets walks every section in the ENScan result and writes each
// record to mapp_records. seen is shared across keyword variants for a
// single company so the same record is not written twice. Single-record
// upsert errors are logged and skipped (not returned) so a UNIQUE conflict
// or transient hiccup on one record does not silently drop the rest of the
// section.
func (c *Crawler) persistAssets(n HoldingNode, res *enscan.Result, seen map[string]bool) error {
	for _, sec := range c.Opts.AssetSections {
		records := res.Records(sec)
		if len(records) == 0 {
			continue
		}
		for _, r := range records {
			rec, key := buildMappRecord(n, sec, r)
			if key == "" {
				continue
			}
			tag := sec + "::" + key
			if seen[tag] {
				continue
			}
			seen[tag] = true
			if _, err := c.Store.UpsertMappRecord(rec); err != nil {
				c.Logger.Printf("  assets pid=%s sec=%s upsert error name=%q: %v (skipping record)",
					n.PID, sec, rec.ServiceName, err)
				continue
			}
		}
	}
	return nil
}

// buildMappRecord converts one ENScan record into a *store.MappRecord
// suitable for upsert. Returns a non-empty key when the record carries
// at least a name or a licence.
func buildMappRecord(n HoldingNode, section string, r map[string]string) (*store.MappRecord, string) {
	rec := &store.MappRecord{CompanyID: n.CompanyID, FetchedAt: time.Now().UTC()}

	switch section {
	case enscan.SecICP:
		rec.ServiceName = r["website_name"]
		rec.ServiceLicence = r["icp"]
		rec.Domain = r["domain"]
		rec.ContentTypeName = "ICP备案"
		rec.ServiceType = 1 // 1=网站 by convention
		// main_licence 是公司的"主备案号"，存到 companies 而非 mapp_records。
		// 后续可由独立流程从 companies 中再读出来。
	case enscan.SecAPP:
		rec.ServiceName = r["name"]
		rec.ContentTypeName = "APP"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecAPP]
		rec.Domain = r["link"]
	case enscan.SecWxApp:
		rec.ServiceName = r["name"]
		rec.ContentTypeName = "微信小程序"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecWxApp]
	case enscan.SecWechat:
		rec.ServiceName = r["name"]
		rec.ContentTypeName = "微信公众号"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecWechat]
	case enscan.SecWeibo:
		rec.ServiceName = r["name"]
		rec.Domain = r["profile_url"]
		rec.ContentTypeName = "微博"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecWeibo]
	case enscan.SecCopyright:
		rec.ServiceName = r["name"]
		rec.ServiceLicence = r["reg_num"]
		rec.ContentTypeName = "软件著作权"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecCopyright]
	case enscan.SecSupplier:
		// 供应商关联的是另一家公司（pid 在记录里），这里只存名称作为 mapp_record 的
		// service_name 以便不丢信息；关联关系由 companies.pid 索引独立维护。
		rec.ServiceName = r["name"]
		rec.ContentTypeName = "供应商"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecSupplier]
	case enscan.SecJob:
		rec.ServiceName = r["name"]
		rec.ContentTypeName = "招聘"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecJob]
	case enscan.SecPartner:
		rec.ServiceName = r["name"]
		rec.ContentTypeName = "股东"
		rec.ServiceType = enscan.SectionToDBType[enscan.SecPartner]
	default:
		rec.ServiceName = r["name"]
	}
	if rec.ServiceName == "" {
		rec.ServiceName = strings.TrimSpace(r["name"])
	}
	key := rec.ServiceName + "|" + rec.ServiceLicence
	return rec, key
}

// persistCompany writes the company to the store and tries to populate
// its main_licence from the icp field when present.
func (c *Crawler) persistCompany(ctx context.Context, name string) (int64, error) {
	co := &store.Company{UnitName: name, NatureName: "企业", BusinessID: c.Business}
	cid, err := c.Store.UpsertCompany(co)
	if err != nil {
		return 0, err
	}
	// Cheap fill: do not over-call enscan for the licence. The mapper step
	// in the assets pipeline writes the per-record licence; main_licence can
	// be backfilled lazily by reading existing mapp_records rows.
	return cid, nil
}
