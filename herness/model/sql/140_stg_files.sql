{#
  Generic files staging (impl 02 U02-112; design 02 §4.1.2 config-defined sources, §5.10):
  one table `stg.files_<entity>` per entity of `extra_entities["files"]`, carrying the
  lake's own columns with no casting (canonical feeds from these tables are open item
  OI-05; no `core` table reads them in this version). Raw reads go through `m.latest`
  only; an absent entity gives a table with the metadata aliases and no data columns.
#}
{% import "_macros.jinja" as m %}
{% set V = 'VARCHAR' %}
{#- every lake column except the metadata columns and `_payload` (already among them). -#}
{% set META = ('_record_id', '_source', '_entity', '_source_key', '_source_updated_at', '_fetched_at', '_deleted', '_payload') %}
{% for entity in extra_entities.get('files', ()) %}
{% set ns = namespace(cols=[]) %}
{% for column in (lake.get('files', entity).columns | sort) %}
{% if column not in META %}
{% set ns.cols = ns.cols + [(column, V)] %}
{% endif %}
{% endfor %}
CREATE OR REPLACE TABLE stg.{{ ('files_' ~ entity) | ident }} AS
SELECT
    l._record_id AS record_id, l._source_key AS source_key, l._source_updated_at AS source_updated_at
{%- for column, _sqltype in ns.cols %},
    l.{{ column | ident }}
{%- endfor %}

FROM {{ m.latest('files', entity, ns.cols) }} AS l;
{% endfor %}
