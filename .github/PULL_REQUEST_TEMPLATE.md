## Proposed changes

> English | [中文](PULL_REQUEST_TEMPLATE_zh.md)

<!-- one sentence about what changed + the related issue (Closes #123) -->

## Proof

<!-- how it was tested / which commands were run / screenshots -->

## Checklist

### Where the change lives

- [ ] A new module goes to `modules/public/<name>/` (the default, it is optional)
- [ ] If it has to go to `modules/main/`, the reason is given under "Behaviour changes"

### Driven by the configuration (if there is a new tool call or path)

- [ ] It goes through `config/*.conf` + `load_config_set` (L1 env > L2 conf > L3 default)
- [ ] No absolute path is hardcoded (such as `$HOME/...`)

### The sensitive information

- [ ] No real business domain (example.com / foo.test stand in)
- [ ] No real vendor name / no real subsidiary name
- [ ] No real credential (token / password / cookie / private key)
- [ ] No real email address / no real IP / no real hostname
- [ ] No slice of recon.sqlite3 / no real output of scan_results
- [ ] A change of a Go module (`cdnmatch` / `db_align`) is a commit that the upstream already has

### Tests / documentation

- [ ] The self test of the module was run (bash -n / shellcheck / python -m py_compile / go vet)
- [ ] The default behaviour of `install.sh` changed -> it is written down under "Behaviour changes"
- [ ] A README / CHANGELOG / a comment was added (when the feature is visible to the user)

## Behaviour changes (if there are any)

<!-- before the change against after the change; when it is a break change, write it down in detail -->
