// Package store wraps the shared recon.sqlite3 database with a small set of
// upsert helpers used by the runner. It only creates the service_type_map
// table and two indexes; the rest of the schema is owned by the recon
// platform and must not be modified here.
package store

import (
	"database/sql"
	_ "embed"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

//go:embed schema.sql
var schemaSQL string

// Store is a thin handle over the shared sqlite database. Safe for concurrent
// use via the underlying database/sql connection pool.
type Store struct {
	DB *sql.DB
}

// Open opens (or creates) the database file and applies the local schema.
// Foreign keys are enabled; WAL mode is on for better concurrent read/write
// from sibling recon tools.
func Open(path string) (*Store, error) {
	dsn := fmt.Sprintf("file:%s?_pragma=foreign_keys(1)&_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)", path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1) // sqlite write serialization; reads still benefit from WAL
	if err := db.Ping(); err != nil {
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	s := &Store{DB: db}
	if err := s.applySchema(); err != nil {
		_ = db.Close()
		return nil, err
	}
	if err := s.applyMigrations(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() error { return s.DB.Close() }

func (s *Store) applySchema() error {
	_, err := s.DB.Exec(schemaSQL)
	return err
}

// applyMigrations runs idempotent ALTER TABLE statements for columns we
// rely on but the upstream recon platform does not declare. Each step
// checks PRAGMA table_info first so re-running on a fresh db is a no-op.
//
//   - companies.group: manual classification label (e.g. "核心"/"E组-弱关联").
//     Seeded from operator-provided TSV via manage/add_business.sh -s;
//     read back by db_align to skip noisy groups before burning ENScan
//     rate limit.
func (s *Store) applyMigrations() error {
	if !s.columnExists("companies", "group") {
		if _, err := s.DB.Exec(`ALTER TABLE companies ADD COLUMN "group" TEXT`); err != nil {
			return fmt.Errorf("add companies.group: %w", err)
		}
	}
	return nil
}

func (s *Store) columnExists(table, column string) bool {
	rows, err := s.DB.Query(`PRAGMA table_info(` + table + `)`)
	if err != nil {
		return false
	}
	defer rows.Close()
	for rows.Next() {
		var cid int64
		var name, ctype string
		var notnull int64
		var dflt, pk sql.NullString
		// PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			continue
		}
		if name == column {
			return true
		}
	}
	return false
}

// Business is the input entity — a free-form name like "ExampleCo".
type Business struct {
	ID   int64
	Name string
}

// UpsertBusiness returns the business id for the given name, inserting it if
// new. Match is case-insensitive on trimmed name.
func (s *Store) UpsertBusiness(name string) (int64, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return 0, errors.New("business name is empty")
	}
	var id int64
	err := s.DB.QueryRow(`SELECT id FROM businesses WHERE business_name = ? COLLATE NOCASE`, name).Scan(&id)
	if err == nil {
		return id, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return 0, fmt.Errorf("query business: %w", err)
	}
	res, err := s.DB.Exec(`INSERT INTO businesses (business_name) VALUES (?)`, name)
	if err != nil {
		return 0, fmt.Errorf("insert business: %w", err)
	}
	return res.LastInsertId()
}

// Company is a legal entity (法人) under one or more businesses.
type Company struct {
	ID          int64
	UnitName    string
	NatureName  string
	MainLicence string
	BusinessID  int64
	Group       string // manual classification label (e.g. "核心"/"A组-悠享"/"E组-弱关联"); empty = unclassified
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

// UpsertCompany inserts or refreshes a company row keyed by (business_id,
// unit_name). main_licence is updated in place if it was previously NULL;
// group follows the same rule — non-empty new value overwrites, empty keeps
// existing. Pass `Group: ""` to never touch the column.
func (s *Store) UpsertCompany(c *Company) (int64, error) {
	if c.UnitName == "" {
		return 0, errors.New("company unit_name is empty")
	}
	if c.BusinessID == 0 {
		return 0, errors.New("company business_id is 0")
	}
	now := time.Now().UTC()

	var id int64
	err := s.DB.QueryRow(
		`SELECT id, COALESCE(main_licence, '') FROM companies WHERE business_id = ? AND unit_name = ?`,
		c.BusinessID, c.UnitName,
	).Scan(&id, new(string))
	if err == nil {
		// Refresh nature_name and fill in main_licence if it was empty.
		// group: same rule as main_licence — only overwrite when caller
		// passes a non-empty value. "" leaves the existing label alone.
		_, _ = s.DB.Exec(
			`UPDATE companies SET nature_name = COALESCE(NULLIF(?, ''), nature_name),
			        main_licence = CASE WHEN main_licence IS NULL OR main_licence = '' THEN NULLIF(?, '') ELSE main_licence END,
			        "group" = CASE WHEN "group" IS NULL OR "group" = '' THEN NULLIF(?, '') ELSE "group" END,
			        updated_at = ?
			 WHERE id = ?`,
			c.NatureName, c.MainLicence, c.Group, now, id,
		)
		return id, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return 0, fmt.Errorf("query company: %w", err)
	}
	res, err := s.DB.Exec(
		`INSERT INTO companies (unit_name, nature_name, main_licence, "group", business_id, created_at, updated_at)
		 VALUES (?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, ?)`,
		c.UnitName, c.NatureName, c.MainLicence, c.Group, c.BusinessID, now, now,
	)
	if err != nil {
		return 0, fmt.Errorf("insert company: %w", err)
	}
	return res.LastInsertId()
}

// MappRecord is a single sub-ICP/app/mini-program/wechat/... entry belonging
// to a company. Keyed by (company_id, service_licence) when licence is
// non-empty, else falls back to (company_id, service_name, service_type).
type MappRecord struct {
	ID                int64
	CompanyID         int64
	SourceDataID      string
	ServiceName       string
	ServiceLicence    string
	ServiceType       int
	ContentTypeName   string
	Domain            string
	RecordUpdatedAt   string
	FetchedAt         time.Time
	RawJSON           string
}

// UpsertMappRecord idempotently writes a mapp_record. It also records the
// service_type into service_type_map with a placeholder name if unseen.
func (s *Store) UpsertMappRecord(r *MappRecord) (int64, error) {
	if r.CompanyID == 0 {
		return 0, errors.New("mapp_record company_id is 0")
	}
	if r.FetchedAt.IsZero() {
		r.FetchedAt = time.Now().UTC()
	}

	// record service_type in the map (INSERT OR IGNORE) so the next insert
	// path can carry a label; never fails the upsert.
	if r.ServiceType != 0 {
		_, _ = s.DB.Exec(
			`INSERT OR IGNORE INTO service_type_map (service_type, type_name) VALUES (?, ?)`,
			r.ServiceType, fmt.Sprintf("type_%d", r.ServiceType),
		)
	}

	// Match strategy:
	//  1. If service_licence is non-empty, match on (company_id, service_licence).
	//  2. Else match on (company_id, service_name, service_type).
	var id int64
	var matchErr error
	matchedByLicence := false
	if r.ServiceLicence != "" {
		matchErr = s.DB.QueryRow(
			`SELECT id FROM mapp_records WHERE company_id = ? AND service_licence = ?`,
			r.CompanyID, r.ServiceLicence,
		).Scan(&id)
		matchedByLicence = matchErr == nil
	}
	if !matchedByLicence {
		matchErr = s.DB.QueryRow(
			`SELECT id FROM mapp_records WHERE company_id = ? AND service_name = ? AND service_type = ?`,
			r.CompanyID, r.ServiceName, r.ServiceType,
		).Scan(&id)
	}

	// The mapp_records schema (owned by the recon platform, declared in
	// ymicp/icp_mapp_query.py) defines service_licence as TEXT NOT NULL
	// UNIQUE. APP / 微信小程序 / 微博 sections don't carry an ICP licence,
	// so without this synthesizer a second empty-licence insert under the
	// same company would fail UNIQUE and be silently dropped by the
	// crawler. The synth value is deterministic per
	// (company_id, service_type, service_name), which is exactly the
	// fallback identity used by the match step above, so re-runs upsert
	// instead of inserting duplicates. Downstream readers should still key
	// on (company_id, service_name, service_type) for these sections.
	licenceForInsert := r.ServiceLicence
	if licenceForInsert == "" {
		licenceForInsert = fmt.Sprintf("synth:%d:%d:%s", r.CompanyID, r.ServiceType, r.ServiceName)
	}

	// matchedByLicence is true when the match key was (company_id, service_licence);
	// in that case the licence is the identity so we must NOT overwrite the
	// service_name — a second observation with a different name is just an
	// alternate label, not a correction.
	if matchErr == nil {
		var nameSet string
		if !matchedByLicence {
			nameSet = `service_name = COALESCE(NULLIF(?, ''), service_name),`
		}
		_, err := s.DB.Exec(
			fmt.Sprintf(`UPDATE mapp_records SET
			        source_data_id = COALESCE(NULLIF(?, ''), source_data_id),
			        %s
			        service_type = CASE WHEN service_type = 0 OR service_type IS NULL THEN ? ELSE service_type END,
			        content_type_name = COALESCE(NULLIF(?, ''), content_type_name),
			        domain = COALESCE(NULLIF(?, ''), domain),
			        record_updated_at = COALESCE(NULLIF(?, ''), record_updated_at),
			        fetched_at = ?,
			        raw_json = ?
			 WHERE id = ?`, nameSet),
			r.SourceDataID,
			r.ServiceName, r.ServiceType, r.ContentTypeName,
			r.Domain, r.RecordUpdatedAt, r.FetchedAt, r.RawJSON, id,
		)
		return id, err
	}
	if !errors.Is(matchErr, sql.ErrNoRows) {
		return 0, fmt.Errorf("query mapp_record: %w", matchErr)
	}
	res, err := s.DB.Exec(
		`INSERT INTO mapp_records
		 (company_id, source_data_id, service_name, service_licence, service_type,
		  content_type_name, domain, record_updated_at, fetched_at, raw_json)
		 VALUES (?, NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), ?, NULLIF(?, ''))`,
		r.CompanyID, r.SourceDataID, r.ServiceName, licenceForInsert, r.ServiceType,
		r.ContentTypeName, r.Domain, r.RecordUpdatedAt, r.FetchedAt, r.RawJSON,
	)
	if err != nil {
		return 0, fmt.Errorf("insert mapp_record: %w", err)
	}
	return res.LastInsertId()
}

// Scope is an authorised asset string (domain or IP) attached to a business.
type Scope struct {
	ID         int64
	BusinessID int64
	ScopeName  string
	Asset      string
	Wildcard   bool
}

// UpsertScope idempotently writes a scope row, keyed by (business_id, asset).
// Existing scope_name and is_wildcard are preserved unless the new value is
// non-empty / true.
func (s *Store) UpsertScope(sc *Scope) (int64, error) {
	if sc.BusinessID == 0 || sc.Asset == "" {
		return 0, errors.New("scope business_id and asset are required")
	}
	if sc.ScopeName == "" {
		sc.ScopeName = "可测资产"
	}
	now := time.Now().UTC()

	res, err := s.DB.Exec(
		`INSERT INTO scopes (business_id, scope_name, asset, is_wildcard, created_at, updated_at, fetched_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?)
		 ON CONFLICT(business_id, asset) DO UPDATE SET
		     scope_name = COALESCE(excluded.scope_name, scope_name),
		     is_wildcard = MAX(scopes.is_wildcard, excluded.is_wildcard),
		     updated_at = excluded.updated_at,
		     fetched_at = excluded.fetched_at`,
		sc.BusinessID, sc.ScopeName, sc.Asset, boolToInt(sc.Wildcard), now, now, now,
	)
	if err != nil {
		return 0, fmt.Errorf("upsert scope: %w", err)
	}
	id, _ := res.LastInsertId()
	// ON CONFLICT UPDATE returns 0 in some sqlite builds; fall back to a lookup.
	if id == 0 {
		err := s.DB.QueryRow(`SELECT id FROM scopes WHERE business_id = ? AND asset = ?`,
			sc.BusinessID, sc.Asset).Scan(&id)
		if err != nil {
			return 0, fmt.Errorf("lookup scope id: %w", err)
		}
	}
	return id, nil
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
