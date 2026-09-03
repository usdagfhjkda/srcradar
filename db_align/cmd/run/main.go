// Command run is the entry point for the recon.db_align tool.
//
// Usage:
//
//	db_align -n ExampleCo                       # 控股树 only
//	db_align -n ExampleCo -icp -app -wx-app     # + opt-in assets
//	db_align -n ExampleCo -all                  # 控股树 + all asset sections
//	db_align -n ExampleCo --broad               # wide-mode disambiguation
//	db_align -n ExampleCo -type aqc,kc          # multiple data sources
//	db_align -n ExampleCo -invest 100           # only 100%-owned subs
//	db_align -n ExampleCo -no-permute           # don't run keyword variants
//
// All flag values match the conventions of ENScan_GO. The tool is a thin
// orchestrator on top of ENScan: it shells out per query, parses the JSON
// output, and writes to the shared recon.sqlite3 database.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"db_align/internal/crawler"
	"db_align/internal/enscan"
	"db_align/internal/mapper"
	"db_align/internal/resolver"
	"db_align/internal/scope"
	"db_align/internal/store"
)

var (
	flagName      = flag.String("n", "", "业务名 (e.g. ExampleCo)")
	flagDB        = flag.String("db", defaultDB(), "sqlite database path")
	flagBinary    = flag.String("enscan", defaultBinary(), "path to enscan binary")
	flagTypes     = flag.String("type", "aqc,tyc,rb,qimai", "data sources (comma-separated, e.g. aqc,kc); default fan-out spreads load across AQC + TYC(公众号) + RB(小程序) + qimai(APP) to dodge AQC anti-bot")
	flagInvest    = flag.Float64("invest", 51, "持股比例下限 % (0 = no filter)")
	flagBroad     = flag.Bool("broad", false, "宽进消歧 (Top5+同实控人扩展)")
	flagNoPermute = flag.Bool("no-permute", false, "不为每个公司跑关键词变体")
	flagDelay     = flag.Int("delay", 0, "每次 enscan 调用的额外延迟（秒）")
	flagProxy     = flag.String("proxy", enscan.DefaultProxy, "传给 enscan 的 -proxy（默认空 = 不走代理）")
	flagScope     = flag.Bool("scope", false, "从资产中提取主域写入 scopes 表")
	flagMapBack   = flag.Bool("backfill-main-licence", true, "爬取后回填 companies.main_licence")
	flagTimeout   = flag.Int("timeout", 300, "每次 enscan 调用的超时（秒）")
	flagPID       = flag.String("pid", "", "跳过 resolver，直接用该 PID 作为 root（多个用逗号分隔）")
	flagMaxDepth  = flag.Int("max-depth", 0, "树爬最大深度（0=不限，root 算 depth=0）")
	flagTreeRetry = flag.Int("tree-retry", 1, "树爬瞬时错误重试次数")
	flagNoBranch  = flag.Bool("no-branch", false, "跳过分公司（分公司非独立法人，只要子公司时用）")
	flagLogFile   = flag.String("log-file", defaultLogFile(), "日志文件路径（含时间戳）；空字符串关闭文件日志（默认 ./logs/db_align_<时间>.log）")
)

// defaultLogFile returns a timestamped path under ./logs so successive
// runs do not clobber each other. Override via -log-file.
func defaultLogFile() string {
	return filepath.Join("logs", "db_align_"+time.Now().Format("20060102_150405")+".log")
}

func defaultDB() string {
	if v := os.Getenv("RECON_DB"); v != "" {
		return v
	}
	// Walk up from cwd to find the recon root.
	cwd, _ := os.Getwd()
	for p := cwd; p != "/" && p != "."; p = filepath.Dir(p) {
		cand := filepath.Join(p, "db", "recon.sqlite3")
		if _, err := os.Stat(cand); err == nil {
			return cand
		}
	}
	return "./recon.sqlite3"
}

func defaultBinary() string {
	if v := os.Getenv("ENSCAN_BIN"); v != "" {
		return v
	}
	cwd, _ := os.Getwd()
	for p := cwd; p != "/" && p != "."; p = filepath.Dir(p) {
		cand := filepath.Join(p, "ENScan_GO", "ENScan")
		if _, err := os.Stat(cand); err == nil {
			return cand
		}
	}
	return "./ENScan"
}

func main() {
	flag.Parse()

	// Tee all log output to stderr AND a timestamped log file (default on).
	// File open failures fall back to stderr-only with a one-line warning
	// so a read-only filesystem does not abort the run.
	writers := []io.Writer{os.Stderr}
	var logFile *os.File
	if p := *flagLogFile; p != "" {
		if dir := filepath.Dir(p); dir != "" {
			_ = os.MkdirAll(dir, 0o755)
		}
		f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[db_align] warning: cannot open log file %q: %v (continuing with stderr only)\n", p, err)
		} else {
			logFile = f
			writers = append(writers, f)
		}
	}
	lg := log.New(io.MultiWriter(writers...), "[db_align] ", log.LstdFlags|log.Lmsgprefix)
	if logFile != nil {
		defer logFile.Close()
		lg.Printf("log file: %s", logFile.Name())
	}

	if strings.TrimSpace(*flagName) == "" {
		fmt.Fprintln(os.Stderr, "error: -n <业务名> is required")
		flag.Usage()
		os.Exit(2)
	}

	// Opt-in asset sections (off by default).
	assetSections := collectAssetFlags()
	if *flagAll {
		assetSections = enscan.AllAssetSections
	}
	if *flagInvest == 0 {
		*flagInvest = 0 // explicit "no filter" — let the upstream return everything
	}

	// Open store.
	st, err := store.Open(*flagDB)
	if err != nil {
		lg.Fatalf("open store: %v", err)
	}
	defer st.Close()

	bizID, err := st.UpsertBusiness(*flagName)
	if err != nil {
		lg.Fatalf("upsert business: %v", err)
	}
	lg.Printf("business id=%d name=%q", bizID, *flagName)

	// Build runner.
	runner := enscan.NewRunner(*flagBinary)
	runner.Types = splitCSV(*flagTypes)
	runner.Timeout = time.Duration(*flagTimeout) * time.Second
	runner.Proxy = *flagProxy

	// Context with SIGINT cancellation.
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// Step 1: resolve business name → company candidates, or bypass via -pid.
	var seeds []resolver.Candidate
	if strings.TrimSpace(*flagPID) != "" {
		// -pid bypass: skip resolver entirely. Each entry must be "name:pid".
		lg.Printf("step 1/4 resolve: -pid bypass, raw=%q", *flagPID)
		for _, raw := range splitCSV(*flagPID) {
			raw = strings.TrimSpace(raw)
			name, pid, ok := strings.Cut(raw, ":")
			if !ok {
				lg.Fatalf("-pid entry %q must be in 'name:pid' format "+
					"(e.g. ExampleCo子公司有限公司:00000000000000)", raw)
			}
			name, pid = strings.TrimSpace(name), strings.TrimSpace(pid)
			if name == "" || pid == "" {
				lg.Fatalf("-pid entry %q has empty name or pid", raw)
			}
			seeds = append(seeds, resolver.Candidate{
				Name: name, PID: pid, Reason: "pid bypass",
			})
		}
	} else {
		lg.Printf("step 1/4 resolve: name=%q broad=%v", *flagName, *flagBroad)
		res, err := resolver.Resolve(ctx, runner, *flagName, *flagBroad)
		if err != nil {
			lg.Fatalf("resolve: %v", err)
		}
		lg.Printf("  selected: %q (pid=%s, score reason=%q)",
			res.Selected.Name, res.Selected.PID, res.Selected.Reason)
		for i, c := range res.Candidates {
			lg.Printf("  cand[%d]: %q pid=%s score=%d reason=%q",
				i, c.Name, c.PID, c.Score, c.Reason)
		}
		if *flagBroad && len(res.Candidates) == 1 {
			lg.Printf("  note: AQC returned only 1 candidate; -broad had no effect " +
				"(use -pid name:pid to bypass resolver)")
		}

		// Persist the root company.
		rootCo := &store.Company{
			UnitName:   res.Selected.Name,
			NatureName: "企业",
			BusinessID: bizID,
		}
		rootCID, err := st.UpsertCompany(rootCo)
		if err != nil {
			lg.Fatalf("upsert root company: %v", err)
		}
		lg.Printf("  root company id=%d", rootCID)

		seeds = append(seeds, *res.Selected)
		if *flagBroad {
			for _, c := range res.Candidates {
				if c.PID == res.Selected.PID {
					continue
				}
				seeds = append(seeds, c)
			}
		}
	}

	// Step 2: walk the holding tree for every seed.
	cr := crawler.New(runner, st, bizID, crawler.Opts{
		InvestThreshold: *flagInvest,
		AssetSections:   nil, // tree phase does not collect assets
		Broad:           *flagBroad,
		Delay:           time.Duration(*flagDelay) * time.Second,
		Permute:         !*flagNoPermute,
		MaxDepth:        *flagMaxDepth,
		TreeRetries:     *flagTreeRetry,
		NoBranch:        *flagNoBranch,
	}, lg)

	var allNodes []crawler.HoldingNode
	for _, seed := range seeds {
		if seed.PID == "" {
			continue
		}
		lg.Printf("step 2/4 tree: seed=%q pid=%s", seed.Name, seed.PID)
		nodes, err := cr.RunTree(ctx, seed.Name, seed.PID)
		if err != nil {
			lg.Printf("  tree error on seed %q: %v (continuing)", seed.Name, err)
			continue
		}
		allNodes = append(allNodes, nodes...)
	}
	lg.Printf("tree summary: %d unique companies across %d seed(s)", len(allNodes), len(seeds))

	// Step 3: asset reverse-lookup.
	if len(assetSections) > 0 {
		lg.Printf("step 3/4 assets: sections=%v", assetSections)
		cr.Opts.AssetSections = assetSections
		if err := cr.RunAssets(ctx, allNodes); err != nil {
			lg.Printf("  assets error: %v", err)
		}
	} else {
		lg.Printf("step 3/4 assets: skipped (no -section flags, no -all)")
	}

	// Step 4: post-process.
	if *flagMapBack {
		lg.Printf("step 4/4 backfill: main_licence")
		n, err := mapper.BackfillMainLicence(st, lg)
		if err != nil {
			lg.Printf("  backfill error: %v", err)
		}
		lg.Printf("  backfilled %d company rows", n)
	}
	if *flagScope {
		lg.Printf("step 4/4 scope extraction")
		total := 0
		for _, n := range allNodes {
			if n.PID == "" {
				continue
			}
			opts := []enscan.SearchOpt{enscan.WithDelay(time.Duration(*flagDelay) * time.Second)}
			res, err := runner.Search(ctx, n.Name, []string{enscan.SecICP, enscan.SecAPP, enscan.SecWxApp}, opts...)
			if err != nil {
				lg.Printf("  scope fetch error company=%q: %v", n.Name, err)
				continue
			}
			hosts := scope.Extract(res)
			wrote, err := scope.Persist(st, bizID, hosts, lg)
			if err != nil {
				lg.Printf("  scope persist error: %v", err)
			}
			total += wrote
		}
		lg.Printf("  scope: wrote %d new scope rows", total)
	}

	lg.Printf("done")
}
