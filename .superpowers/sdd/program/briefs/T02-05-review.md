# T02-05 review (relayed; reviewer's heredoc write failed with a bash quoting parse error)
Verdict Approved, 0 Critical / 0 Important / 5 Minor. All units/tests ✅; gates green (migrate.py 100% cov, BT02-07 9.5 ms).
Minor 1 migrate.py:96-98 digit-leading but malformed names (012_Add-Col.sql, 12_x.sql, 012_x.SQL) silently skipped; closed reason set has none; at minimum log a warning; spec-owner ruling needed.
Minor 2 migrate.py:40 \d{3} matches Unicode digits; use [0-9]{3} or re.ASCII.
Minor 3 migrate.py:43-46 denylist narrower than §3.6 allowlist (REINDEX/ANALYZE pass); multi-statement line surfaces as apply_failed anyway.
Minor 4 herness/store/migrations has no __init__.py (namespace package: multiple sys.path dirs merge); spec-owner decision.
Minor 5 migrate() reads applied before discovery, pending_migrations() discovers first (different error precedence); out-of-order log emitted before in-txn re-check.
Deviations 1-8 all accepted.
