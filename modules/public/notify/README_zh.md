# notify 推送模块 (可选插件)

> [English](README.md) | 中文

> **模块定位**:集成层 plugin,**完全可选**。在 `daily` diff 阶段跑完后,
> 把当天的 `summary.md` 用 **OpenPGP/GPG (RFC 4880) RSA-3072** 加密后
> 通过 **Resend HTTP API (`POST https://api.resend.com/emails`)** 推送到
> 操作员的邮箱。收件端用 [FairEmail](https://email.faircode.eu/) +
> [OpenKeychain](https://www.openkeychain.org/) 在手机上自动解密、离线阅读。

## 导航

- **本文档**:§模块边界 / §推送链路 / §安装 / §配置 / §收件端 setup / §推送内容 / §安全 / §运维 / §文件说明
- **依赖上游**:`daily` diff 阶段生成的 `summary.md`(由本模块 hook,不重跑 diff)
- **依赖二进制**:`gpg` (apt: gnupg) + `curl` (Resend HTTP API 调用;所有现代 Linux 发行版自带)
- **与 db/daily/pdtm 关系**:无硬依赖 — `notify` 走 `summary.md` 文件,不动 DB

## 一、模块边界

| 项 | 范围 | License |
|---|---|---|
| `notify_send.sh` / `install.sh` / `lib/notify_config.py` | srcradar 原创集成层 | Apache-2.0 |
| `gpg` (GnuPG) | OS 包(apt: gnupg),RFC 4880 OpenPGP | GPL-3.0 |
| `curl` | OS 包 / busybox | curl license (MIT-style) |
| Resend HTTP API | 第三方事务邮件投递 | Resend.com ToS |
| FairEmail / OpenKeychain | 收件端(手机) | FairEmail: 个人 / OpenKeychain: Apache-2.0 |

**srcradar 不维护**:GnuPG / curl / Resend API / 收件 App。
本模块只做 **配置 + 调度 + OpenPGP 包装 + body 脱敏 + Resend 推送** 五件事。

## 二、推送链路

```
modules/main/daily/daily_monitor.sh
    │
    │  diff 阶段末尾(added+reactivated+changed > 0 时) || true
    ▼
modules/public/notify/notify_send.sh
    │
    ├─ 读 config/notify.conf        (RESEND_API_KEY / FROM_ADDR / TO_ADDR /
    │                                SUBJECT / GPG_RECIPIENT,.gitignore 屏蔽)
    ├─ gpg -r <recipient> -e summary.md  → summary.gpg
    ├─ 写 summary.body.md (只摘要,无资产细节)
    ├─ build_resend_payload() 写 ~/.cache/notify/payload.json:
    │      {
    │        "from": "<FROM_ADDR>",
    │        "to":   ["<TO_ADDR>"],
    │        "subject": "<SUBJECT> <BUSINESS> <DATE> daily diff",
    │        "text": "<body.md 内容>",
    │        "attachments": [{"filename": "summary.gpg",
    │                         "content": "<base64(summary.gpg)>"}]
    │      }
    │      (attachments[].content 必须是 base64 字符串,Resend API 强制)
    └─ push_resend():
           curl -fsS -X POST https://api.resend.com/emails \
             -H "Authorization: Bearer $RESEND_API_KEY" \
             -H "Content-Type: application/json" \
             --data-binary "@$PAYLOAD_JSON"
            │
            ▼
        Resend HTTP API  → 收件人邮箱(手机 FairEmail + OpenKeychain 自动解)
```

**触发条件**:`daily_monitor.sh` 只在 `added + reactivated + changed > 0` 时调用本脚本,
避免每次 cron 跑都触发一封无变更邮件。

**失败兜底**:`daily_monitor.sh` 用 `|| true` 包住本脚本调用,
推送失败不影响 diff 阶段已完成的事务(`change_type=0` 已 commit)。

## 三、安装

```bash
# 1. 装 OS 依赖
sudo apt-get install -y gnupg curl        # curl 大多发行版自带

# 2. 写 example(默认已被 srcradar install.sh 写入 modules/public/notify/config/)
bash modules/public/notify/install.sh --check

# 3. operator 自填真值(不进 git!)
cp modules/public/notify/config/notify.conf.example \
   modules/public/notify/config/notify.conf
chmod 600 modules/public/notify/config/notify.conf
$EDITOR modules/public/notify/config/notify.conf
```

> **关键**:`config/notify.conf` 已经在 `.gitignore` 内(`config/*` 通配 + `config/notify.conf` 显式),
> 真凭据 / 真实邮箱地址不会进任何 commit。

## 四、配置(config/notify.conf)

```ini
# 字段语义:见 example 顶部注释
RESEND_API_KEY=REPLACE_WITH_RESEND_API_KEY
FROM_ADDR=noreply@your-domain.example
TO_ADDR=REPLACE_WITH_RECIPIENT_EMAIL
SUBJECT=[srcradar] daily diff
GPG_RECIPIENT=REPLACE_WITH_RECIPIENT_EMAIL
```

字段说明:

| 字段 | 含义 | 来源 |
|---|---|---|
| `RESEND_API_KEY` | Resend API key(`re_` 开头,≥ 20 字符,绑发件域) | https://resend.com/api-keys |
| `FROM_ADDR` | 发件人地址,必须在已验证的发件域下 | Resend 后台添加并验证 |
| `TO_ADDR` | 收件人邮箱 | operator,与 `GPG_RECIPIENT` 一致 |
| `SUBJECT` | 主题前缀 | 自定义 |
| `GPG_RECIPIENT` | 给 `gpg -r` 用的邮箱占位 | 通常 = `TO_ADDR` |

任何字段留空 / 以 `REPLACE_WITH_` 开头 → `notify_send.sh` 拒绝发送并打印缺失项。
`install.sh --check` 会对 `RESEND_API_KEY` 格式做软提示(不以 `re_` 开头或长度过短)。

## 五、收件端 setup

### A. 生成 / 导出 GPG 密钥对(operator 一次性操作)

收件端 (FairEmail + OpenKeychain) 用 **手机上的私钥** 解密,
所以密钥对在**收件端设备**上生成,**推送端不存任何私钥**:

```bash
# 在 *收件手机* 或 *收件电脑* 上:
gpg --batch --quick-gen-key "Operator <operator@example.com>" rsa3072 default 0
gpg --list-keys --keyid-format LONG            # 记录指纹
gpg --armor --export OPERATOR_FINGERPRINT > operator.pub.asc
```

> RSA-3072 主密钥 + RSA-3072 加密子密钥(OpenPGP 标准),`gpg --quick-gen-key` 默认生成的就是这个组合。

### B. 公钥分发(把 .asc 推到推送端)

- 把 `operator.pub.asc` 拷到 srcradar 推送端,导入到本地 GPG keyring:
  ```bash
  gpg --import operator.pub.asc
  gpg --list-keys                              # 应能看到 operator@example.com
  ```
  > **注意**:`notify_send.sh` 的 `GPG_RECIPIENT` 字段是真邮箱地址,但只在 operator
  > 自填的 `config/notify.conf` 内出现,**不入仓**(`.gitignore`)。

### C. 发件域验证(Resend + Cloudflare DNS)

Resend 推送邮件需要发件域通过 SPF / DKIM / DMARC 验证。操作员需要:

1. 在 Resend 后台 (https://resend.com/domains) 添加发件域(如 `your-domain.example`),
   后台会给出一组 DNS 记录值(SPF / DKIM / DMARC)。
2. 在 Cloudflare 控制台的对应 DNS 区(本模块不调 Cloudflare API;DNS 由 operator
   手动维护)添加后台给出的记录:
   - `TXT` 记录:`<domain>` → `v=spf1 include:_spf.resend.com ~all`(SPF)
   - `CNAME` 记录:`resend._domainkey.<domain>` → Resend 给的 DKIM 值(DKIM)
   - `TXT` 记录:`_dmarc.<domain>` → `v=DMARC1; p=...`(DMARC,推荐 `p=none` 起步)
   - `MX` 记录:`<domain>` → Resend 给的 MX(可选,只有走 Resend 收件才需要)
   - 验证返回路径 / bounce tracking 记录(由 Resend 给出,通常是 `TXT`)
3. 在 Resend 后台等 DNS 验证状态从 "Pending" 变 "Verified"。
4. `FROM_ADDR` 必须使用验证通过的发件域下任意地址(如 `noreply@your-domain.example`)。

> 本模块只描述 **需要在 Cloudflare 上加的 DNS 记录字段**(SPF / DKIM / DMARC / MX),
> **不提供 Cloudflare API 自动操作** — 真值操作留给 operator 手册。

### D. Android: FairEmail + OpenKeychain

1. 装 [FairEmail](https://email.faircode.eu/) (个人/付费版都可)
2. 装 [OpenKeychain](https://www.openkeychain.org/) (Apache-2.0,F-Droid / Play 都有)
3. 在 OpenKeychain 里:My Keys → Import from file → 选 `operator.pub.asc` *或* 直接在手机上 `gpg --quick-gen-key` 生成新密钥对
4. 把 OpenKeychain 设成 FairEmail 的加密 provider:
   FairEmail → Settings → Encryption → OpenKeychain → 选 operator 密钥
5. 把收件邮箱账号加到 FairEmail(IMAP + SMTP),测试一封自发自收验证 push

收到 srcradar 推送后:
- 标题:`<SUBJECT> <BUSINESS> <DATE> daily diff`
- 附件:`summary.gpg`
- 点击附件 → FairEmail 唤起 OpenKeychain → 用手机私钥解 → 看到完整 summary.md

## 六、推送内容(只摘要,无资产细节)

邮件正文(markdown,纯文本):

```
[SUBJECT] BUSINESS_NAME_PLACEHOLDER DATE_PLACEHOLDER daily diff

+ ADDED_PLACEHOLDER added
~ CHANGED_PLACEHOLDER changed
! REACTIVATED_PLACEHOLDER reactivated
deactivated: DEACTIVATED_PLACEHOLDER

Dashboard: http://127.0.0.1:8765/BUSINESS_NAME_PLACEHOLDER/diff (需 SSH 隧道)
Encrypted attachment: summary.gpg (OpenPGP,需 OpenKeychain 解密)
```

附件 `summary.gpg` = `gpg -r <recipient> -e summary.md`,
**完整** summary.md(原 csv 行、host、状态码等)离线阅读用。
Resend API 要求 `attachments[].content` 必须是 base64 字符串,本脚本用
`base64 -w 0 summary.gpg` 编码后嵌入 payload.json。

> **脱敏**:`BUSINESS_NAME_PLACEHOLDER` / `DATE_PLACEHOLDER` / 四个数字占位
> 由 `notify_send.sh` 在写 body 时根据 caller 传参填充;真业务名 / 子域 / IP /
> 状态码 / 风险等级**一律不进 body**,完整数据只在加密附件里。

## 七、安全

- **凭据隔离**:`config/notify.conf` 走 `chmod 600`,在 `.gitignore`(`config/*`)
  内;`payload.json` 写入时 `chmod 0600`,目录 `chmod 0700`
- **鉴权面窄**:Resend 是单 API key 鉴权,不需要 SMTP user + app password 双凭据;
  Outlook OAuth2 / Azure App Registration / refresh_token 已弃用
- **私钥不出收件端**:推送端只 import 公钥,从不 import 私钥
- **OpenPGP 标准合规**:RFC 4880,RSA-3072 主 + RSA-3072 加密子,`gpg --batch` + `--trust-model always`
- **失败兜底**:`daily_monitor.sh` 用 `|| true` 包住;`notify_send.sh` 任何阶段失败不抛未捕获异常到 stdout
- **不持久化 cookie / token**:不写 `~/.bashrc` / `~/.netrc` / `~/.config/httpie*`
- **payload 残留**:`~/.cache/notify/payload.json` 含 base64 附件,内容等同加密附件
  + body 明文;权限 `0600`,目录 `0700`,只当前用户可读

## 八、运维

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `notify_config.py parse 失败` | `notify.conf` 字段格式错(空 `=` / 中文 key 等) | 用 `bash install.sh --check` 自检;参考 `example` |
| `notify.conf 字段未填真值: RESEND_API_KEY` | 还是 `REPLACE_WITH_*` | 在 Resend 后台生成 `re_` 开头的 API key;填真值 |
| `install.sh` 软提示 "RESEND_API_KEY 不以 re_ 开头" | 格式错 | Resend API key 必须 `re_` 开头 |
| `install.sh` 软提示 "长度 < 20 字符" | API key 被截断 | 重新生成;`re_` 加上 random suffix 通常 30+ 字符 |
| `gpg 加密失败` | 收件端公钥未导入推送端 | 在推送端 `gpg --import operator.pub.asc` + `gpg --list-keys` 验证 |
| `Resend HTTP 401` | API key 失效 / 拼错 | 在 Resend 后台 rotate,重填 `RESEND_API_KEY` |
| `Resend HTTP 403` | API key 没绑发件域 / 域未验证 | 在 Resend 后台 Domains → 给发件域加 DNS(SPF/DKIM/DMARC)→ 等 "Verified" |
| `Resend HTTP 422` | payload 字段格式错(常见:`to` 必须是 list,`attachments[].content` 必须是 base64 字符串) | 检查 `~/.cache/notify/payload.json`;base64 编码不能含换行 |
| `Resend HTTP 429` | 触发 Resend 限速(2 req/s 默认) | 退避重试;调低 cron 频率;生产建议加 retry-with-backoff |
| `curl exit non-zero`(网络层失败) | 推送端无外网 / DNS 解析失败 / TLS 失败 | `curl -v https://api.resend.com/emails` 测连通;检查出网白名单 |
| `body 占位符没替换` | caller (`daily_monitor.sh`) 还没接入 | 见 §集成点;`daily_monitor.sh` 在 diff 后注入数字 |
| `FairEmail 显示附件但不解` | OpenKeychain 没装 / FairEmail 没指 OpenKeychain | FairEmail → Settings → Encryption → 选 OpenKeychain → 选密钥 |

## 九、集成点

`modules/main/daily/daily_monitor.sh` diff 阶段末尾追加:

```bash
# optional: notify 推送(仅 added+reactivated+changed > 0 时)
NOTIFY_SEND="$RECON_ROOT/public/notify/notify_send.sh"
if [ -x "$NOTIFY_SEND" ] && [ -f "$REPORT_DIR/summary.md" ]; then
    bash "$NOTIFY_SEND" "$REPORT_DIR/summary.md" "..." "$RUN_ID" "$RUN_START_AT" \
        2>>"$LOG_FILE" || true
fi
```

> 不强制 push;`notify/` 模块不存在 / `notify_send.sh` 不可执行 / `notify.conf` 没填 → 全部静默跳过。

## 十、文件说明

| 文件 | 说明 |
|---|---|
| `install.sh` | 写 example + 自检 gpg / curl / python3 / notify_config.py |
| `notify_send.sh` | 主推送脚本(gpg 加密 + body 脱敏 + Resend HTTP API curl POST) |
| `lib/notify_config.py` | config/notify.conf 读取器(走 load_config.py 同款模式) |
| `config/notify.conf.example` | 占位模板,所有敏感字段 = `REPLACE_WITH_*`(入仓) |
| `config/notify.conf` | operator 自填真值,`chmod 600`,`.gitignore` 屏蔽(不入仓) |
| `~/.cache/notify/payload.json` | 推送前生成的 Resend JSON payload,`chmod 0600`(不入 git,本地缓存) |
| `README.md` | 本文档 |