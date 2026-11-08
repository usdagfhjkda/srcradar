# the notify module of the push (an optional plugin)

> English | [中文](README_zh.md)

> **The position of the module**: the plugin of the integration layer, **it is completely optional**. Once the diff stage of `daily` ran,
> it encrypts the `summary.md` of the day with **OpenPGP/GPG (RFC 4880) RSA-3072**
> and it pushes it to the mailbox of the operator through the **HTTP API of Resend (`POST https://api.resend.com/emails`)**.
> The receiver decrypts it automatically and reads it offline on the phone, with [FairEmail](https://email.faircode.eu/) +
> [OpenKeychain](https://www.openkeychain.org/).

## The navigation

- **This document**: §the boundary of the module / §the chain of the push / §the installation / §the configuration / §the setup of the receiver / §the content of the push / §the safety / §the operation / §the description of the files
- **The upstream it depends on**: the `summary.md` that the diff stage of `daily` produces (this module hooks into it, it does not run the diff again)
- **The dependencies of the binaries**: `gpg` (apt: gnupg) + `curl` (for the call of the HTTP API of Resend; every modern distribution of Linux comes with them)
- **The relation with db/daily/pdtm**: there is no hard dependency — `notify` goes through the file `summary.md`, and it does not touch the DB

## I. The boundary of the module

| The item | The scope | The license |
|---|---|---|
| `notify_send.sh` / `install.sh` / `lib/notify_config.py` | the integration layer that srcradar wrote | Apache-2.0 |
| `gpg` (GnuPG) | the package of the OS (apt: gnupg), the OpenPGP of the RFC 4880 | GPL-3.0 |
| `curl` | the package of the OS / busybox | the license of curl (of the MIT style) |
| the HTTP API of Resend | the delivery of the transactional mail of a third party | the ToS of Resend.com |
| FairEmail / OpenKeychain | the receiver (the phone) | FairEmail: personal / OpenKeychain: Apache-2.0 |

**What srcradar does not maintain**: GnuPG / curl / the API of Resend / the app of the receiver.
This module does only five things: **the configuration + the scheduling + the wrapping of the OpenPGP + the desensitization of the body + the push of Resend**.

## II. The chain of the push

```
modules/main/daily/daily_monitor.sh
    │
    │  at the end of the diff stage (when added+reactivated+changed > 0) || true
    ▼
modules/public/notify/notify_send.sh
    │
    ├─ it reads config/notify.conf        (RESEND_API_KEY / FROM_ADDR / TO_ADDR /
    │                                SUBJECT / GPG_RECIPIENT, the .gitignore hides it)
    ├─ gpg -r <recipient> -e summary.md  → summary.gpg
    ├─ it writes summary.body.md (only a summary, no detail of the assets)
    ├─ build_resend_payload() writes ~/.cache/notify/payload.json:
    │      {
    │        "from": "<FROM_ADDR>",
    │        "to":   ["<TO_ADDR>"],
    │        "subject": "<SUBJECT> <BUSINESS> <DATE> daily diff",
    │        "text": "<the content of body.md>",
    │        "attachments": [{"filename": "summary.gpg",
    │                         "content": "<base64(summary.gpg)>"}]
    │      }
    │      (the attachments[].content has to be a base64 string, the API of Resend enforces it)
    └─ push_resend():
           curl -fsS -X POST https://api.resend.com/emails \
             -H "Authorization: Bearer $RESEND_API_KEY" \
             -H "Content-Type: application/json" \
             --data-binary "@$PAYLOAD_JSON"
            │
            ▼
        the HTTP API of Resend  →  the mailbox of the receiver (the phone decrypts it with FairEmail + OpenKeychain)
```

**The condition of the trigger**: `daily_monitor.sh` only calls this script when `added + reactivated + changed > 0`,
so that every run of the cron does not trigger a mail without a change.

**The fallback of a failure**: `daily_monitor.sh` wraps the call of this script with `|| true`,
and a failed push does not affect the transaction that the diff stage already finished (`change_type=0` is committed).

## III. The installation

```bash
# 1. install the dependencies of the OS
sudo apt-get install -y gnupg curl        # curl comes with most of the distributions

# 2. write the example (srcradar install.sh already wrote it in modules/public/notify/config/)
bash modules/public/notify/install.sh --check

# 3. the operator fills the real values (they do not go into git!)
cp modules/public/notify/config/notify.conf.example \
   modules/public/notify/config/notify.conf
chmod 600 modules/public/notify/config/notify.conf
$EDITOR modules/public/notify/config/notify.conf
```

> **The key point**: `config/notify.conf` is already inside the `.gitignore` (the wildcard `config/*` + the explicit `config/notify.conf`),
> and the real credentials / the real addresses never reach a commit.

## IV. The configuration (config/notify.conf)

```ini
# the semantics of the fields: see the comment at the top of the example
RESEND_API_KEY=REPLACE_WITH_RESEND_API_KEY
FROM_ADDR=noreply@your-domain.example
TO_ADDR=REPLACE_WITH_RECIPIENT_EMAIL
SUBJECT=[srcradar] daily diff
GPG_RECIPIENT=REPLACE_WITH_RECIPIENT_EMAIL
```

The description of the fields:

| The field | What it means | Where it comes from |
|---|---|---|
| `RESEND_API_KEY` | the API key of Resend (it starts with `re_`, it is ≥ 20 characters, it is bound to the sending domain) | https://resend.com/api-keys |
| `FROM_ADDR` | the address of the sender, it has to be under a verified sending domain | it is added and verified in the back office of Resend |
| `TO_ADDR` | the mailbox of the receiver | the operator, the same as `GPG_RECIPIENT` |
| `SUBJECT` | the prefix of the subject | custom |
| `GPG_RECIPIENT` | the placeholder of the mailbox that `gpg -r` uses | usually = `TO_ADDR` |

Any field that is empty / that starts with `REPLACE_WITH_` → `notify_send.sh` refuses to send and it prints the missing items.
`install.sh --check` gives a soft hint on the format of the `RESEND_API_KEY` (it does not start with `re_` or it is too short).

## V. The setup of the receiver

### A. The generation / the export of the pair of keys of GPG (a single action of the operator)

The receiver (FairEmail + OpenKeychain) decrypts with the **private key of the phone**,
so the pair of keys is generated **on the device of the receiver**, and **the side of the push does not store any private key**:

```bash
# on the *phone of the receiver* or on the *computer of the receiver*:
gpg --batch --quick-gen-key "Operator <operator@example.com>" rsa3072 default 0
gpg --list-keys --keyid-format LONG            # record the fingerprint
gpg --armor --export OPERATOR_FINGERPRINT > operator.pub.asc
```

> A primary key of RSA-3072 + an encryption subkey of RSA-3072 (the standard of the OpenPGP), that is the combination that `gpg --quick-gen-key` generates by default.

### B. The distribution of the public key (the .asc goes to the side of the push)

- copy `operator.pub.asc` to the side of the push of srcradar and import it into the local keyring of GPG:
  ```bash
  gpg --import operator.pub.asc
  gpg --list-keys                              # operator@example.com has to show up
  ```
  > **Note**: the field `GPG_RECIPIENT` of `notify_send.sh` is a real mailbox, but it only shows up
  > inside the `config/notify.conf` that the operator filled by hand, and it **does not go into the repository** (`.gitignore`).

### C. The verification of the sending domain (Resend + the DNS of Cloudflare)

The mail that Resend pushes needs the sending domain to pass the verification of the SPF / DKIM / DMARC. The operator has to:

1. add the sending domain in the back office of Resend (https://resend.com/domains) (such as `your-domain.example`),
   and the back office gives a set of values of the DNS records (SPF / DKIM / DMARC).
2. add the records of the back office in the matching zone of the DNS of the console of Cloudflare (this module does not call the API of Cloudflare; the DNS is maintained by hand by the operator):
   - the record `TXT`: `<domain>` → `v=spf1 include:_spf.resend.com ~all` (SPF)
   - the record `CNAME`: `resend._domainkey.<domain>` → the value of the DKIM that Resend gives (DKIM)
   - the record `TXT`: `_dmarc.<domain>` → `v=DMARC1; p=...` (DMARC, it is recommended to start with `p=none`)
   - the record `MX`: `<domain>` → the MX that Resend gives (it is optional, it is only needed to receive through Resend)
   - the records of the return path / of the tracking of the bounces (they come from Resend, they are usually `TXT`)
3. wait in the back office of Resend until the state of the verification of the DNS goes from "Pending" to "Verified".
4. the `FROM_ADDR` has to use any address under the sending domain that passed the verification (such as `noreply@your-domain.example`).

> This module only describes **the fields of the DNS records that have to be added on Cloudflare** (SPF / DKIM / DMARC / MX),
> it **does not provide an automatic operation of the API of Cloudflare** — the real operations are left to the manual of the operator.

### D. Android: FairEmail + OpenKeychain

1. install [FairEmail](https://email.faircode.eu/) (the personal and the paid version both work)
2. install [OpenKeychain](https://www.openkeychain.org/) (Apache-2.0, it is on F-Droid / on Play)
3. in OpenKeychain: My Keys → Import from file → pick `operator.pub.asc` *or* generate a new pair of keys directly on the phone with `gpg --quick-gen-key`
4. set OpenKeychain as the encryption provider of FairEmail:
   FairEmail → Settings → Encryption → OpenKeychain → pick the key of the operator
5. add the account of the mailbox to FairEmail (IMAP + SMTP), and test a mail that it sends to itself to verify the push

Once the push of srcradar arrives:
- the subject: `<SUBJECT> <BUSINESS> <DATE> daily diff`
- the attachment: `summary.gpg`
- a click on the attachment → FairEmail calls OpenKeychain → it decrypts with the private key of the phone → the whole summary.md is visible

## VI. The content of the push (only a summary, no detail of the assets)

The body of the mail (markdown, plain text):

```
[SUBJECT] BUSINESS_NAME_PLACEHOLDER DATE_PLACEHOLDER daily diff

+ ADDED_PLACEHOLDER added
~ CHANGED_PLACEHOLDER changed
! REACTIVATED_PLACEHOLDER reactivated
deactivated: DEACTIVATED_PLACEHOLDER

Dashboard: http://127.0.0.1:8765/BUSINESS_NAME_PLACEHOLDER/diff (it requires an SSH tunnel)
Encrypted attachment: summary.gpg (OpenPGP, decrypt it with OpenKeychain)
```

The attachment `summary.gpg` = `gpg -r <recipient> -e summary.md`,
it holds the **whole** summary.md (the rows of the original csv, the hosts, the status codes and the like) for an offline reading.
The API of Resend requires that the `attachments[].content` is a base64 string, and this script encodes it with
`base64 -w 0 summary.gpg` and embeds it in the payload.json.

> **The desensitization**: the `BUSINESS_NAME_PLACEHOLDER` / the `DATE_PLACEHOLDER` / the four numeric placeholders
> are filled by `notify_send.sh` when it writes the body, following the arguments of the caller; the real name of the business / the subdomains / the IPs /
> the status codes / the levels of the risk **never reach the body**, and the whole data only lives in the encrypted attachment.

## VII. The safety

- **The isolation of the credentials**: `config/notify.conf` goes through `chmod 600`, and it is inside the `.gitignore` (`config/*`);
  the `payload.json` is written with `chmod 0600`, and the directory with `chmod 0700`
- **A narrow surface of the authentication**: Resend authenticates with a single API key, and it does not need the two credentials of an SMTP user + an app password;
  the OAuth2 of Outlook / the App Registration of Azure / the refresh_token are abandoned
- **The private key never leaves the receiver**: the side of the push only imports the public key, it never imports the private key
- **The compliance with the standard of the OpenPGP**: the RFC 4880, a primary of RSA-3072 + an encryption subkey of RSA-3072, with `gpg --batch` + `--trust-model always`
- **The fallback of a failure**: `daily_monitor.sh` wraps it with `|| true`; a failure of any stage of `notify_send.sh` does not throw an uncaught exception to the stdout
- **No cookie / token is persisted**: it does not write `~/.bashrc` / `~/.netrc` / `~/.config/httpie*`
- **The leftovers of the payload**: `~/.cache/notify/payload.json` holds the attachment in base64, and its content is equivalent to the encrypted attachment
  + the body in plain text; the permissions are `0600`, the directory is `0700`, and only the current user can read it

## VIII. The operation

| The symptom | The possible cause | What to do |
|---|---|---|
| `notify_config.py parse 失败` | the format of a field of `notify.conf` is wrong (an empty `=` / a Chinese key and the like) | check it by hand with `bash install.sh --check`; refer to the `example` |
| `notify.conf 字段未填真值: RESEND_API_KEY` | it is still `REPLACE_WITH_*` | generate an API key that starts with `re_` in the back office of Resend; fill the real value |
| the soft hint of `install.sh` "RESEND_API_KEY 不以 re_ 开头" | the format is wrong | the API key of Resend has to start with `re_` |
| the soft hint of `install.sh` "长度 < 20 字符" | the API key was truncated | generate it again; `re_` plus a random suffix is usually 30+ characters |
| `gpg 加密失败` | the public key of the receiver was not imported on the side of the push | `gpg --import operator.pub.asc` on the side of the push + verify it with `gpg --list-keys` |
| `Resend HTTP 401` | the API key expired / it is mistyped | rotate it in the back office of Resend and fill the `RESEND_API_KEY` again |
| `Resend HTTP 403` | the API key is not bound to a sending domain / the domain is not verified | in the back office of Resend, Domains → add the DNS of the sending domain (SPF/DKIM/DMARC) → wait for "Verified" |
| `Resend HTTP 422` | the format of a field of the payload is wrong (the usual ones: the `to` has to be a list, and the `attachments[].content` has to be a base64 string) | check the `~/.cache/notify/payload.json`; the base64 encoding must not hold a line break |
| `Resend HTTP 429` | the rate limit of Resend was triggered (2 req/s by default) | back off and retry; lower the frequency of the cron; a retry-with-backoff is recommended in the production |
| `curl exit non-zero` (a failure of the network layer) | the side of the push has no access to the internet / the resolution of the DNS fails / the TLS fails | `curl -v https://api.resend.com/emails` to test the connectivity; check the whitelist of the outgoing traffic |
| `body 占位符没替换` | the caller (`daily_monitor.sh`) is not hooked up yet | see the §the points of integration; `daily_monitor.sh` injects the numbers after the diff |
| `FairEmail 显示附件但不解` | OpenKeychain is not installed / FairEmail does not point to OpenKeychain | FairEmail → Settings → Encryption → pick OpenKeychain → pick the key |

## IX. The points of integration

The end of the diff stage of `modules/main/daily/daily_monitor.sh` gains:

```bash
# optional: the push of notify (only when added+reactivated+changed > 0)
NOTIFY_SEND="$RECON_ROOT/public/notify/notify_send.sh"
if [ -x "$NOTIFY_SEND" ] && [ -f "$REPORT_DIR/summary.md" ]; then
    bash "$NOTIFY_SEND" "$REPORT_DIR/summary.md" "..." "$RUN_ID" "$RUN_START_AT" \
        2>>"$LOG_FILE" || true
fi
```

> The push is not forced; the module `notify/` does not exist / the `notify_send.sh` is not executable / the `notify.conf` is not filled → everything is skipped silently.

## X. The description of the files

| The file | The description |
|---|---|
| `install.sh` | it writes the example + it checks gpg / curl / python3 / notify_config.py |
| `notify_send.sh` | the main script of the push (the encryption of gpg + the desensitization of the body + the curl POST of the HTTP API of Resend) |
| `lib/notify_config.py` | the reader of config/notify.conf (the same model as load_config.py) |
| `config/notify.conf.example` | the template of the placeholders, every sensitive field = `REPLACE_WITH_*` (it goes into the repository) |
| `config/notify.conf` | the real values that the operator fills, `chmod 600`, the `.gitignore` hides it (it does not go into the repository) |
| `~/.cache/notify/payload.json` | the payload of Resend in JSON that is generated before the push, `chmod 0600` (it does not go into git, it is a local cache) |
| `README.md` | this document |