package main

import (
	"flag"

	"db_align/internal/enscan"
)

// Opt-in asset flags. Each corresponds to one -field section in ENScan's
// output. All default to false so the default mode is "holding tree only".
var (
	flagICP       = flag.Bool("icp", false, "收集 ICP 备案")
	flagAPP       = flag.Bool("app", false, "收集 APP")
	flagWechat    = flag.Bool("wechat", false, "收集 微信公众号")
	flagWxApp     = flag.Bool("wx-app", false, "收集 微信小程序")
	flagWeibo     = flag.Bool("weibo", false, "收集 微博")
	flagSupplier  = flag.Bool("supplier", false, "收集 供应商")
	flagJob       = flag.Bool("job", false, "收集 招聘信息")
	flagCopyright = flag.Bool("copyright", false, "收集 软件著作权")
	flagPartner   = flag.Bool("partner", false, "收集 股东信息")
	flagAll       = flag.Bool("all", false, "启用所有资产字段（icp/app/wechat/wx-app/weibo/supplier/job/copyright/partner）")
)

// collectAssetFlags returns the list of ENScan section keys corresponding
// to the user-supplied -flag values.
func collectAssetFlags() []string {
	// Order matters: write to mapp_records in a stable sequence so logs read
	// top-down.  Use a tiny table to keep the boilerplate tiny.
	table := []struct {
		on      bool
		section string
	}{
		{*flagICP, enscan.SecICP},
		{*flagAPP, enscan.SecAPP},
		{*flagWxApp, enscan.SecWxApp},
		{*flagWechat, enscan.SecWechat},
		{*flagWeibo, enscan.SecWeibo},
		{*flagCopyright, enscan.SecCopyright},
		{*flagSupplier, enscan.SecSupplier},
		{*flagJob, enscan.SecJob},
		{*flagPartner, enscan.SecPartner},
	}
	var out []string
	for _, t := range table {
		if t.on {
			out = append(out, t.section)
		}
	}
	return out
}

func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == ',' {
			seg := s[start:i]
			if seg != "" {
				out = append(out, seg)
			}
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
