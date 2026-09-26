{#
  Generic Dataverse staging (impl 02 U02-115; design 02 §4.1.2 config-defined sources,
  §4.1): one table `stg.dataverse_<entity>` per entity of `extra_entities["dataverse"]`,
  same shape as `140_stg_files.sql` (U02-112): the lake's own columns, no casting. Raw
  reads go through `m.latest` only; an absent entity gives a table with the metadata
  aliases and no data columns.
#}
{% import "_macros.jinja" as m %}
{% set V = 'VARCHAR' %}
{#- every lake column except the metadata columns and `_payload` (already among them). -#}
{% set META = ('_record_id', '_source', '_entity', '_source_key', '_source_updated_at', '_fetched_at', '_deleted', '_payload') %}
{% for entity in extra_entities.get('dataverse', ()) %}
{% set ns = namespace(cols=[]) %}
{% for column in (lake.get('dataverse', entity).columns | sort) %}
{% if column not in META %}
{% set ns.cols = ns.cols + [(column, V)] %}
{% endif %}
{% endfor %}
CREATE OR REPLACE TABLE stg.{{ ('dataverse_' ~ entity) | ident }} AS
SELECT
    l._record_id AS record_id, l._source_key AS source_key, l._source_updated_at AS source_updated_at
{%- for column, _sqltype in ns.cols %},
    l.{{ column | ident }}
{%- endfor %}

FROM {{ m.latest('dataverse', entity, ns.cols) }} AS l;
{% endfor %}
