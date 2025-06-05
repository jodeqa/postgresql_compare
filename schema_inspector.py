# app.py (or schema_inspector.py)

import psycopg2
from psycopg2.extras import RealDictCursor

def get_pg_connection(host, port, database, user, password):
    """
    Return a new psycopg2 connection using RealDictCursor for easy dict results.
    """
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password
    )
    return conn


def fetch_tables_and_columns(conn):
    """
    Returns a dict of:
      {
         'table_name1': {
             'columns': {
                 'col1': {'data_type': 'integer', 'is_nullable': 'NO', 'column_default': 'nextval(...)', ...},
                 'col2': {...},
                 ...
             }
         },
         'table_name2': { ... },
         ...
      }
    """

    sql = """
    SELECT 
        table_schema,
        table_name,
        column_name,
        data_type,
        is_nullable,
        column_default,
        character_maximum_length,
        numeric_precision,
        numeric_scale
    FROM 
        information_schema.columns
    WHERE 
        table_schema NOT IN ('pg_catalog','information_schema')
    ORDER BY table_schema, table_name, ordinal_position;
    """
    # We’ll only include tables in the “public” (or non system) schemas.
    result = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            schema = row['table_schema']
            table = row['table_name']
            full_table_name = f"{schema}.{table}"
            if full_table_name not in result:
                result[full_table_name] = {'columns': {}}
            result[full_table_name]['columns'][row['column_name']] = {
                'data_type': row['data_type'],
                'is_nullable': row['is_nullable'],
                'column_default': row['column_default'],
                'character_maximum_length': row['character_maximum_length'],
                'numeric_precision': row['numeric_precision'],
                'numeric_scale': row['numeric_scale']
            }
    return result


def fetch_indexes(conn):
    """
    Returns a dict:
      {
         'schema.table_name': {
             'idx_name1': {
                 'columns': ['col1', 'col2', ...],
                 'is_unique': True/False,
                 'index_type': 'btree'/'gin'/'gist'/...,
                 'expression': <if functional index, else None>
             },
             'idx_name2': { ... },
             ...
         },
         ...
      }
    """
    sql = """
    SELECT
        ns.nspname AS table_schema,
        t.relname AS table_name,
        i.relname AS index_name,
        ix.indisunique AS is_unique,
        ix.indisprimary AS is_primary,
        am.amname AS index_type,
        pg_get_indexdef(ix.indexrelid) AS index_def
    FROM
        pg_class t
        JOIN pg_namespace ns ON (t.relnamespace = ns.oid)
        JOIN pg_index ix ON (t.oid = ix.indrelid)
        JOIN pg_class i ON (ix.indexrelid = i.oid)
        JOIN pg_am am ON (i.relam = am.oid)
    WHERE
        ns.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY
        ns.nspname, t.relname, i.relname;
    """
    result = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            fq_table = f"{row['table_schema']}.{row['table_name']}"
            if fq_table not in result:
                result[fq_table] = {}
            # We can parse index_def to extract column list vs expressions.
            # For simplicity, store the entire index_def text and is_unique/is_primary.
            result[fq_table][row['index_name']] = {
                'is_unique': row['is_unique'],
                'is_primary': row['is_primary'],
                'index_type': row['index_type'],
                'index_def': row['index_def']
            }
    return result


def fetch_foreign_keys(conn):
    """
    Returns a dict:
      {
        'schema.table_name': {
            'fk_constraint_name1': {
                'columns': ['col1','col2', ...],
                'referenced_table': 'schema2.table2',
                'referenced_columns': ['refcol1','refcol2', ...]
            },
            ...
        },
        ...
      }
    """
    sql = """
    SELECT
        tc.constraint_name,
        tc.table_schema,
        tc.table_name,
        kcu.column_name,
        ccu.table_schema AS foreign_table_schema,
        ccu.table_name AS foreign_table_name,
        ccu.column_name AS foreign_column_name
    FROM
        information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
          AND ccu.table_schema = tc.table_schema
    WHERE
        tc.constraint_type = 'FOREIGN KEY'
        AND tc.table_schema NOT IN ('information_schema', 'pg_catalog')
    ORDER BY
        tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position;
    """
    result = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        # We need to group by constraint_name
        for row in cur.fetchall():
            src = f"{row['table_schema']}.{row['table_name']}"
            fk_name = row['constraint_name']
            if src not in result:
                result[src] = {}
            if fk_name not in result[src]:
                result[src][fk_name] = {
                    'columns': [],
                    'referenced_table': f"{row['foreign_table_schema']}.{row['foreign_table_name']}",
                    'referenced_columns': []
                }
            result[src][fk_name]['columns'].append(row['column_name'])
            result[src][fk_name]['referenced_columns'].append(row['foreign_column_name'])
    return result


def fetch_enum_types(conn):
    """
    Returns a dict:
      {
         'schema.enum_type_name': ['label1', 'label2', ...],
         ...
      }
    """
    sql = """
    SELECT
        n.nspname AS enum_schema,
        t.typname AS enum_name,
        e.enumlabel AS enum_label,
        e.enumsortorder
    FROM
        pg_type t
        JOIN pg_enum e ON t.oid = e.enumtypid
        JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
    WHERE
        n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY
        n.nspname, t.typname, e.enumsortorder;
    """
    result = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            fq_name = f"{row['enum_schema']}.{row['enum_name']}"
            if fq_name not in result:
                result[fq_name] = []
            result[fq_name].append(row['enum_label'])
    return result


# app.py

def inspect_database(params):
    """
    Given a dict like:
       {
         'host': 'db1.example.com',
         'port': 5432,
         'database': 'mydb',
         'user': '...',
         'password': '...'
       }
    Returns a dict:
       {
         'tables': {...},         # from fetch_tables_and_columns
         'indexes': {...},        # from fetch_indexes
         'fks': {...},            # from fetch_foreign_keys
         'enums': {...}           # from fetch_enum_types
       }
    """
    conn = get_pg_connection(
        params['host'], params['port'],
        params['database'], params['user'], params['password']
    )
    try:
        tables = fetch_tables_and_columns(conn)
        indexes = fetch_indexes(conn)
        fks = fetch_foreign_keys(conn)
        enums = fetch_enum_types(conn)
        return {
            'tables': tables,
            'indexes': indexes,
            'fks': fks,
            'enums': enums
        }
    finally:
        conn.close()


def compare_schemas(schema1, schema2):
    """
    Takes two schema dicts (as returned by inspect_database) and returns a "diff" dict:
      {
         'tables_only_in_db1': [ 'schema.tableA', ... ],
         'tables_only_in_db2': [ ... ],
         'tables_in_both': {
            'schema.tableX': {
                'columns_only_in_db1': [colA, colB, ...],
                'columns_only_in_db2': [...],
                'columns_in_both': {
                    'col1': {
                        'db1': { data_type: ..., is_nullable: ..., ... },
                        'db2': { ... },
                        'diff': True/False
                    },
                    ...
                },
                'indexes_only_in_db1': [ idx1, idx2, ... ],
                'indexes_only_in_db2': [...],
                'indexes_in_both': {
                    'idx_common': {
                        'db1': { index_def: '...', is_unique: ..., ... },
                        'db2': { ... },
                        'diff': True/False
                    },
                    ...
                },
                'fks_only_in_db1': [ fk_name1, ... ],
                'fks_only_in_db2': [...],
                'fks_in_both': {
                    'fk_common': {
                        'db1': { columns: [...], referenced_table: '...', referenced_columns: [...] },
                        'db2': { ... },
                        'diff': True/False
                    },
                    ...
                }
            },
            ...
         },
         'enums_only_in_db1': [ 'schema.enum1', ... ],
         'enums_only_in_db2': [ ... ],
         'enums_in_both': {
             'schema.enumX': {
                'db1': [ 'label1', 'label2', ... ],
                'db2': [ ... ],
                'diff': True/False
             },
             ...
         }
      }
    """

    diff = {
        'tables_only_in_db1': [],
        'tables_only_in_db2': [],
        'tables_in_both': {},
        'enums_only_in_db1': [],
        'enums_only_in_db2': [],
        'enums_in_both': {}
    }

    # 1. Compare Tables
    tables1 = set(schema1['tables'].keys())
    tables2 = set(schema2['tables'].keys())
    diff['tables_only_in_db1'] = sorted(list(tables1 - tables2))
    diff['tables_only_in_db2'] = sorted(list(tables2 - tables1))
    common_tables = sorted(list(tables1 & tables2))

    for tbl in common_tables:
        block = {}
        # --- Columns ---
        cols1 = set(schema1['tables'][tbl]['columns'].keys())
        cols2 = set(schema2['tables'][tbl]['columns'].keys())
        block['columns_only_in_db1'] = sorted(list(cols1 - cols2))
        block['columns_only_in_db2'] = sorted(list(cols2 - cols1))

        # Columns in both → check if metadata matches
        columns_in_both = {}
        for col in sorted(list(cols1 & cols2)):
            meta1 = schema1['tables'][tbl]['columns'][col]
            meta2 = schema2['tables'][tbl]['columns'][col]
            # Compare data_type, is_nullable, column_default, lengths/precision
            is_same = (
                meta1['data_type'] == meta2['data_type'] and
                meta1['is_nullable'] == meta2['is_nullable'] and
                (meta1['column_default'] or '').strip() == (meta2['column_default'] or '').strip() and
                meta1['character_maximum_length'] == meta2['character_maximum_length'] and
                meta1['numeric_precision'] == meta2['numeric_precision'] and
                meta1['numeric_scale'] == meta2['numeric_scale']
            )
            columns_in_both[col] = {
                'db1': meta1,
                'db2': meta2,
                'diff': not is_same
            }
        block['columns_in_both'] = columns_in_both

        # --- Indexes ---
        idxs1 = set(schema1['indexes'].get(tbl, {}).keys())
        idxs2 = set(schema2['indexes'].get(tbl, {}).keys())
        block['indexes_only_in_db1'] = sorted(list(idxs1 - idxs2))
        block['indexes_only_in_db2'] = sorted(list(idxs2 - idxs1))

        idxs_in_both = {}
        for idx in sorted(list(idxs1 & idxs2)):
            meta1 = schema1['indexes'][tbl][idx]
            meta2 = schema2['indexes'][tbl][idx]
            # Compare index_def or is_unique/is_primary/index_type
            same = (
                meta1['index_def'].strip() == meta2['index_def'].strip() and
                meta1['is_unique'] == meta2['is_unique'] and
                meta1['is_primary'] == meta2['is_primary']
            )
            idxs_in_both[idx] = {
                'db1': meta1,
                'db2': meta2,
                'diff': not same
            }
        block['indexes_in_both'] = idxs_in_both

        # --- Foreign Keys ---
        fks1 = set(schema1['fks'].get(tbl, {}).keys())
        fks2 = set(schema2['fks'].get(tbl, {}).keys())
        block['fks_only_in_db1'] = sorted(list(fks1 - fks2))
        block['fks_only_in_db2'] = sorted(list(fks2 - fks1))

        fks_in_both = {}
        for fk in sorted(list(fks1 & fks2)):
            meta1 = schema1['fks'][tbl][fk]
            meta2 = schema2['fks'][tbl][fk]
            same = (
                meta1['referenced_table'] == meta2['referenced_table'] and
                meta1['columns'] == meta2['columns'] and
                meta1['referenced_columns'] == meta2['referenced_columns']
            )
            fks_in_both[fk] = {
                'db1': meta1,
                'db2': meta2,
                'diff': not same
            }
        block['fks_in_both'] = fks_in_both

        diff['tables_in_both'][tbl] = block

    # 2. Compare ENUMs
    enums1 = set(schema1['enums'].keys())
    enums2 = set(schema2['enums'].keys())
    diff['enums_only_in_db1'] = sorted(list(enums1 - enums2))
    diff['enums_only_in_db2'] = sorted(list(enums2 - enums1))

    for enum in sorted(list(enums1 & enums2)):
        labels1 = schema1['enums'][enum]
        labels2 = schema2['enums'][enum]
        same = labels1 == labels2
        diff['enums_in_both'][enum] = {
            'db1': labels1,
            'db2': labels2,
            'diff': not same
        }

    return diff
