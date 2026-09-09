## Proposed changes

<!-- 一句话概括改了什么 + 关联 issue (Closes #123) -->

## Proof

<!-- 怎么测的 / 跑过哪些命令 / 截图 -->

## Checklist

### 改动定位

- [ ] 新增模块放在 `modules/public/<name>/`(默认,可选装)
- [ ] 若必须放 `modules/main/`,下面"行为变化"说明原因

### 配置驱动(若有新增工具调用 / 路径)

- [ ] 走 `config/*.conf` + `load_config_set`(L1 env > L2 conf > L3 默认)
- [ ] 不硬编码绝对路径(如 `$HOME/...`)

### 敏感信息自查

- [ ] 无真实业务域名(用 example.com / foo.test 占位)
- [ ] 无真实厂商名 / 真实子公司名
- [ ] 无真实凭据(token / password / cookie / 私钥)
- [ ] 无真实邮箱 / 真实 IP / 真实 hostname
- [ ] 无 recon.sqlite3 切片 / scan_results 真实输出
- [ ] Go 模块改动(`cdnmatch` / `db_align`)是上游已有 commit

### 测试 / 文档

- [ ] 跑过模块自测(bash -n / shellcheck / python -m py_compile / go vet)
- [ ] 改了 `install.sh` 默认行为 → 在"行为变化"段说明
- [ ] 加了 README / CHANGELOG / 注释(若功能对用户可见)

## 行为变化(若有)

<!-- 改动前 vs 改动后;若是 break change,详细写 -->