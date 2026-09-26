-- DuckDB scalar macros used by staging (impl 02 U02-108; design 02 §4.2).
-- Stored in schema main of the build file (not TEMP) so a resumed stage sees them.

-- Naive formats are read as UTC with timezone('UTC', ...), independent of the session zone.
CREATE OR REPLACE MACRO main.ts_utc(x) AS
    CASE WHEN x IS NULL THEN NULL ELSE coalesce(
        timezone('UTC', try_strptime(CAST(x AS VARCHAR), '%Y-%m-%d %H:%M:%S')),
        timezone('UTC', try_strptime(CAST(x AS VARCHAR), '%Y-%m-%d %H:%M:%S.%f')),
        timezone('UTC', try_strptime(CAST(x AS VARCHAR), '%Y-%m-%dT%H:%M:%S')),
        timezone('UTC', try_strptime(CAST(x AS VARCHAR), '%Y-%m-%dT%H:%M:%S.%f')),
        CAST(try_strptime(CAST(x AS VARCHAR), '%Y-%m-%dT%H:%M:%S%z') AS TIMESTAMPTZ),
        CAST(try_strptime(CAST(x AS VARCHAR), '%Y-%m-%dT%H:%M:%S.%f%z') AS TIMESTAMPTZ),
        timezone('UTC', try_strptime(CAST(x AS VARCHAR), '%Y-%m-%dT%H:%M:%SZ')),
        timezone('UTC', try_strptime(CAST(x AS VARCHAR), '%Y-%m-%dT%H:%M:%S.%fZ'))
    ) END;

CREATE OR REPLACE MACRO main.to_date(x) AS
    CAST(try_strptime(CAST(x AS VARCHAR), '%Y-%m-%d') AS DATE);

CREATE OR REPLACE MACRO main.to_bool(x) AS
    CASE
        WHEN lower(CAST(x AS VARCHAR)) IN ('true', '1', 'yes', 'y', 't') THEN true
        WHEN lower(CAST(x AS VARCHAR)) IN ('false', '0', 'no', 'n', 'f') THEN false
    END;

CREATE OR REPLACE MACRO main.lead_int(x, lo, hi) AS
    CASE
        WHEN TRY_CAST(regexp_extract(CAST(x AS VARCHAR), '^([0-9]+)(\s*-.*)?$', 1) AS INTEGER)
            BETWEEN lo AND hi
        THEN TRY_CAST(regexp_extract(CAST(x AS VARCHAR), '^([0-9]+)(\s*-.*)?$', 1) AS INTEGER)
    END;

CREATE OR REPLACE MACRO main.to_double(x) AS
    CASE WHEN isfinite(TRY_CAST(x AS DOUBLE)) THEN TRY_CAST(x AS DOUBLE) END;

CREATE OR REPLACE MACRO main.to_int(x) AS
    TRY_CAST(x AS INTEGER);

-- ServiceNow glide durations: plain seconds, or a `1970-01-01 HH:MM:SS` timestamp.
CREATE OR REPLACE MACRO main.sn_duration_s(x) AS
    CASE
        WHEN regexp_full_match(CAST(x AS VARCHAR), '[0-9]+') THEN TRY_CAST(x AS BIGINT)
        ELSE CAST(epoch(main.ts_utc(x)) AS BIGINT)
    END;

-- CASE (not AND) guards the JSON readers: its branches only see the rows that pass.
CREATE OR REPLACE MACRO main.jstr(x, path) AS
    CASE WHEN json_valid(x) THEN json_extract_string(x, path) END;

CREATE OR REPLACE MACRO main.json_names(x) AS
    CASE WHEN json_valid(x) THEN json_extract_string(x, '$[*].name') END;

CREATE OR REPLACE MACRO main.json_str_list(x) AS
    CASE WHEN json_valid(x) THEN json_extract_string(x, '$[*]') END;

-- Atlassian document format: every "text" value, JSON-unescaped, joined by one space;
-- anything else (Data Center wiki text) is returned unchanged.
CREATE OR REPLACE MACRO main.jira_text(x) AS
    CASE
        WHEN json_valid(x) THEN CASE
            WHEN json_extract_string(x, '$.type') = 'doc' THEN array_to_string(
                list_transform(
                    regexp_extract_all(x, '"text"\s*:\s*"((?:[^"\\]|\\.)*)"', 1),
                    lambda s: json_extract_string('"' || s || '"', '$')
                ),
                ' '
            )
            ELSE x
        END
        ELSE x
    END;

CREATE OR REPLACE MACRO main.team_value(x) AS
    CASE
        WHEN json_valid(x) THEN CASE json_type(x)
            WHEN 'OBJECT' THEN coalesce(
                json_extract_string(x, '$.name'),
                json_extract_string(x, '$.title'),
                json_extract_string(x, '$.value')
            )
            WHEN 'VARCHAR' THEN json_extract_string(x, '$')
            ELSE x
        END
        ELSE x
    END;
