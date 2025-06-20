import os
import json

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from sshtunnel import SSHTunnelForwarder

app = Flask(__name__)
app.secret_key = r'5o|\/|3\^/#ere83y@nd┌ѵ┐Ø'  # for flashing errors, etc.

# Path to the JSON file where we store configurations.
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'configs.json')


def load_configs():
    """
    Load all saved configurations from CONFIG_FILE.
    Returns a dict of the form:
      {
        "config_name1": { "db1": {...}, "db2": {...} },
        "config_name2": { … },
        …
      }
    If file does not exist or is empty, returns {}.
    """
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        # If file is malformed or empty, start fresh
        return {}


def save_configs(all_configs):
    """
    Overwrite CONFIG_FILE with the full 'all_configs' dict.
    """
    with open(CONFIG_FILE, 'w') as f:
        json.dump(all_configs, f, indent=2)


def get_pg_connection_direct(host, port, database, user, password):
    """
    Return a psycopg2 connection to Postgres (direct TCP).
    """
    return psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password
    )


def get_pg_connection_ssh_tunnel(
    ssh_host, ssh_port, ssh_user, ssh_password, ssh_pkey,
    remote_db_host, remote_db_port, db_name, db_user, db_password
):
    """
    Open an SSH tunnel from localhost:<local_port> → remote_db_host:remote_db_port
    using the given SSH credentials. Return (conn, tunnel).
    """
    tunnel_kwargs = {
        'ssh_address_or_host': (ssh_host, int(ssh_port)),
        'remote_bind_address': (remote_db_host, int(remote_db_port)),
        'ssh_username': ssh_user
    }

    # Prioritize private key if provided, otherwise use password
    if ssh_pkey:
        tunnel_kwargs['ssh_pkey'] = os.path.expanduser(ssh_pkey)
    elif ssh_password:
        tunnel_kwargs['ssh_password'] = ssh_password
    else:
        raise ValueError("SSH Tunnel requires either ssh_password or ssh_pkey")

    tunnel = SSHTunnelForwarder(**tunnel_kwargs)
    tunnel.start()

    local_port = tunnel.local_bind_port
    conn = psycopg2.connect(
        host='127.0.0.1',
        port=local_port,
        dbname=db_name,
        user=db_user,
        password=db_password
    )
    return conn, tunnel


def fetch_tables_and_columns(conn):
    """
    Returns a dict mapping "schema.table" → { 'columns': { column_name: metadata_dict } }.
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

    result = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            schema = row['table_schema']
            tbl = row['table_name']
            full_name = f"{schema}.{tbl}"
            if full_name not in result:
                result[full_name] = {'columns': {}}
            result[full_name]['columns'][row['column_name']] = {
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
    Returns a dict mapping "schema.table" → { index_name: {index metadata} }.
    """
    sql = """
    SELECT
        ns.nspname AS table_schema,
        t.relname AS table_name,
        i.relname AS index_name,
        ix.indisunique AS is_unique,
        ix.indisprimary AS is_primary,
        am.amname AS index_type,
        pg_get_indexdef(ix.indexrelid) AS index_def,
        ARRAY(
          SELECT pg_get_indexdef(ix.indexrelid, k + 1, TRUE)
          FROM generate_subscripts(ix.indkey, 1) AS k
          ORDER BY k
        ) AS columns
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
            fq = f"{row['table_schema']}.{row['table_name']}"
            if fq not in result:
                result[fq] = {}
            result[fq][row['index_name']] = {
                'is_unique': row['is_unique'],
                'is_primary': row['is_primary'],
                'index_type': row['index_type'],
                'index_def': row['index_def'],
                'columns': row['columns']  # <-- new!
            }
    return result


def fetch_foreign_keys(conn):
    """
    Returns a dict mapping "schema.table" → { fk_constraint_name: {columns, referenced_table, referenced_columns} }.
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
        for row in cur.fetchall():
            src = f"{row['table_schema']}.{row['table_name']}"
            fk_name = row['constraint_name']
            if src not in result:
                result[src] = {}
            if fk_name not in result[src]:
                result[src][fk_name] = {
                    'constraint_name': fk_name,  # <-- ADD THIS LINE
                    'columns': [],
                    'referenced_table': f"{row['foreign_table_schema']}.{row['foreign_table_name']}",
                    'referenced_columns': []
                }
            result[src][fk_name]['columns'].append(row['column_name'])
            result[src][fk_name]['referenced_columns'].append(row['foreign_column_name'])
    return result


def fetch_enum_types(conn):
    """
    Returns a dict mapping "schema.enum_type" → [ list of labels in order ].
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
            full = f"{row['enum_schema']}.{row['enum_name']}"
            if full not in result:
                result[full] = []
            result[full].append(row['enum_label'])
    return result


def inspect_database(db_params):
    """
    Given a dict with keys:
      - conn_method: "direct" or "ssh"
      - host, port, database, user, password   (Postgres creds)
      - ssh_host, ssh_port, ssh_user, ssh_pass, ssh_pkey  (SSH creds if conn_method=="ssh")
    Returns a dict { 'tables': ..., 'indexes': ..., 'fks': ..., 'enums': ... }.
    """
    conn = None
    tunnel = None
    try:
        if db_params['conn_method'] == 'ssh':
            # Open SSH tunnel
            conn, tunnel = get_pg_connection_ssh_tunnel(
                ssh_host=db_params['ssh_host'],
                ssh_port=db_params['ssh_port'],
                ssh_user=db_params['ssh_user'],
                ssh_password=db_params['ssh_pass'],
                ssh_pkey=db_params['ssh_pkey'],
                remote_db_host=db_params['host'],
                remote_db_port=db_params['port'],
                db_name=db_params['database'],
                db_user=db_params['user'],
                db_password=db_params['password']
            )
        else:
            # Direct
            conn = get_pg_connection_direct(
                host=db_params['host'],
                port=db_params['port'],
                database=db_params['database'],
                user=db_params['user'],
                password=db_params['password']
            )

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
        if conn:
            conn.close()
        if tunnel:
            tunnel.stop()


def compare_schemas(schema1, schema2):
    """
    Build a diff dict comparing schema1 vs. schema2 (exactly as before).
    """
    diff = {
        'tables_only_in_db1': [],
        'tables_only_in_db2': [],
        'tables_in_both': {},
        'enums_only_in_db1': [],
        'enums_only_in_db2': [],
        'enums_in_both': {}
    }

    # 1) Tables
    t1 = set(schema1['tables'].keys())
    t2 = set(schema2['tables'].keys())
    diff['tables_only_in_db1'] = sorted(list(t1 - t2))
    diff['tables_only_in_db2'] = sorted(list(t2 - t1))
    common_tables = sorted(list(t1 & t2))

    for tbl in common_tables:
        block = {}
        # ---- Columns ----
        c1 = set(schema1['tables'][tbl]['columns'].keys())
        c2 = set(schema2['tables'][tbl]['columns'].keys())

        # missing in each
        block['columns_only_in_db1'] = [
            col for col in schema1['tables'][tbl]['columns']
            if col in (c1 - c2)
        ]
        block['columns_only_in_db2'] = [
            col for col in schema2['tables'][tbl]['columns']
            if col in (c2 - c1)
        ]

        # common columns, in original (ordinal) order
        common = c1 & c2
        cboth = {}
        for col in schema1['tables'][tbl]['columns'].keys():
            if col in common:
                m1 = schema1['tables'][tbl]['columns'][col]
                m2 = schema2['tables'][tbl]['columns'][col]
                same = (
                        m1['data_type'] == m2['data_type'] and
                        m1['is_nullable'] == m2['is_nullable'] and
                        (m1['column_default'] or '').strip() == (m2['column_default'] or '').strip() and
                        m1['character_maximum_length'] == m2['character_maximum_length'] and
                        m1['numeric_precision'] == m2['numeric_precision'] and
                        m1['numeric_scale'] == m2['numeric_scale']
                )
                cboth[col] = {
                    'db1': m1,
                    'db2': m2,
                    'diff': not same
                }
        block['columns_in_both'] = cboth

        # ---- Indexes ----
        i1 = set(schema1['indexes'].get(tbl, {}).keys())
        i2 = set(schema2['indexes'].get(tbl, {}).keys())
        block['indexes_only_in_db1'] = sorted(list(i1 - i2))
        block['indexes_only_in_db2'] = sorted(list(i2 - i1))

        iboth = {}
        for idx in sorted(list(i1 & i2)):
            im1 = schema1['indexes'][tbl][idx]
            im2 = schema2['indexes'][tbl][idx]
            same = (
                im1['index_def'].strip() == im2['index_def'].strip() and
                im1['is_unique'] == im2['is_unique'] and
                im1['is_primary'] == im2['is_primary']
            )
            iboth[idx] = {
                'db1': im1,
                'db2': im2,
                'diff': not same
            }
        block['indexes_in_both'] = iboth

        # ---- Foreign Keys ----
        f1 = set(schema1['fks'].get(tbl, {}).keys())
        f2 = set(schema2['fks'].get(tbl, {}).keys())
        block['fks_only_in_db1'] = sorted(list(f1 - f2))
        block['fks_only_in_db2'] = sorted(list(f2 - f1))

        fboth = {}
        for fk in sorted(list(f1 & f2)):
            fm1 = schema1['fks'][tbl][fk]
            fm2 = schema2['fks'][tbl][fk]
            same = (
                fm1['referenced_table'] == fm2['referenced_table'] and
                fm1['columns'] == fm2['columns'] and
                fm1['referenced_columns'] == fm2['referenced_columns']
            )
            fboth[fk] = {
                'db1': fm1,
                'db2': fm2,
                'diff': not same
            }
        block['fks_in_both'] = fboth

        diff['tables_in_both'][tbl] = block

    # 2) ENUMs
    e1 = set(schema1['enums'].keys())
    e2 = set(schema2['enums'].keys())
    diff['enums_only_in_db1'] = sorted(list(e1 - e2))
    diff['enums_only_in_db2'] = sorted(list(e2 - e1))

    for enum in sorted(list(e1 & e2)):
        lbl1 = schema1['enums'][enum]
        lbl2 = schema2['enums'][enum]
        same = (lbl1 == lbl2)
        diff['enums_in_both'][enum] = {
            'db1': lbl1,
            'db2': lbl2,
            'diff': not same
        }

    return diff


@app.route('/', methods=['GET', 'POST'])
def index():
    """
    1) On GET: load saved configs (for dropdown), render blank form or pre-filled if ?config_name=...
    2) On POST:
       - If action == 'save', save the current form into configs.json under the provided config_name.
       - If action == 'load', load values from the selected config and re-render form.
       - If action == 'compare', attempt to connect both DBs and show compare.html.
    """

    all_configs = load_configs()
    config_names = sorted(all_configs.keys())

    # Default “empty” form values:
    defaults = {
        # DB1 defaults
        'db1_conn_method': 'direct',
        'db1_host': '',
        'db1_port': 5432,
        'db1_name': '',
        'db1_user': '',
        'db1_pass': '',
        'db1_ssh_host': '',
        'db1_ssh_port': 22,
        'db1_ssh_user': '',
        'db1_ssh_pass': '',
        'db1_ssh_pkey': '',
        # DB2 defaults
        'db2_conn_method': 'direct',
        'db2_host': '',
        'db2_port': 5432,
        'db2_name': '',
        'db2_user': '',
        'db2_pass': '',
        'db2_ssh_host': '',
        'db2_ssh_port': 22,
        'db2_ssh_user': '',
        'db2_ssh_pass': '',
        'db2_ssh_pkey': '',
        # “UI” defaults
        'selected_config': '',
        'config_name': ''
    }

    if request.method == 'POST':
        action = request.form.get('action')

        # 1) SAVE CONFIGURATION
        if action == 'save':
            config_name = request.form.get('config_name', '').strip()
            if not config_name:
                flash("You must enter a configuration name to save.", "danger")
                # Re-populate defaults from form so user doesn’t lose what they typed:
                for key in defaults:
                    defaults[key] = request.form.get(key, defaults[key])
                return render_template('index.html',
                                       config_names=config_names,
                                       defaults=defaults)

            # Gather all DB1/DB2 fields from the form:
            db1 = {
                'conn_method': request.form.get('db1_conn_method'),
                'host': request.form.get('db1_host', '').strip(),
                'port': int(request.form.get('db1_port', '5432') or 5432),
                'database': request.form.get('db1_name', '').strip(),
                'user': request.form.get('db1_user', '').strip(),
                'password': request.form.get('db1_pass', ''),
                'ssh_host': request.form.get('db1_ssh_host', '').strip(),
                'ssh_port': int(request.form.get('db1_ssh_port', '22') or 22),
                'ssh_user': request.form.get('db1_ssh_user', '').strip(),
                'ssh_pass': request.form.get('db1_ssh_pass', ''),
                'ssh_pkey': request.form.get('db1_ssh_pkey', '').strip()
            }
            db2 = {
                'conn_method': request.form.get('db2_conn_method'),
                'host': request.form.get('db2_host', '').strip(),
                'port': int(request.form.get('db2_port', '5432') or 5432),
                'database': request.form.get('db2_name', '').strip(),
                'user': request.form.get('db2_user', '').strip(),
                'password': request.form.get('db2_pass', ''),
                'ssh_host': request.form.get('db2_ssh_host', '').strip(),
                'ssh_port': int(request.form.get('db2_ssh_port', '22') or 22),
                'ssh_user': request.form.get('db2_ssh_user', '').strip(),
                'ssh_pass': request.form.get('db2_ssh_pass', ''),
                'ssh_pkey': request.form.get('db2_ssh_pkey', '').strip()
            }

            # Overwrite or create a new entry:
            all_configs[config_name] = {'db1': db1, 'db2': db2}
            try:
                save_configs(all_configs)
                flash(f"Configuration '{config_name}' saved successfully.", "success")
            except Exception as e:
                flash(f"Error saving configuration: {e}", "danger")

            # Update the dropdown list (in case it’s a new name)
            config_names = sorted(all_configs.keys())

            # Re-populate form fields with exactly what the user just saved:
            for k, v in db1.items():
                defaults['db1_' + k] = v
            for k, v in db2.items():
                defaults['db2_' + k] = v
            defaults['selected_config'] = config_name
            defaults['config_name'] = config_name

            return render_template('index.html',
                                   config_names=config_names,
                                   defaults=defaults)

        # 2) LOAD CONFIGURATION
        elif action == 'load':
            to_load = request.form.get('selected_config', '').strip()
            if not to_load or to_load not in all_configs:
                flash("Please select a valid configuration to load.", "danger")
                # Re-render with whatever was sent (so user doesn’t lose typed data)
                for key in defaults:
                    defaults[key] = request.form.get(key, defaults[key])
                return render_template('index.html',
                                       config_names=config_names,
                                       defaults=defaults)

            # Grab saved values and overwrite defaults:
            cfg = all_configs[to_load]
            db1_saved = cfg['db1']
            db2_saved = cfg['db2']

            defaults['db1_conn_method'] = db1_saved.get('conn_method', 'direct')
            defaults['db1_host'] = db1_saved.get('host', '')
            defaults['db1_port'] = db1_saved.get('port', 5432)
            defaults['db1_name'] = db1_saved.get('database', '')
            defaults['db1_user'] = db1_saved.get('user', '')
            defaults['db1_pass'] = db1_saved.get('password', '')
            defaults['db1_ssh_host'] = db1_saved.get('ssh_host', '')
            defaults['db1_ssh_port'] = db1_saved.get('ssh_port', 22)
            defaults['db1_ssh_user'] = db1_saved.get('ssh_user', '')
            defaults['db1_ssh_pass'] = db1_saved.get('ssh_pass', '')
            defaults['db1_ssh_pkey'] = db1_saved.get('ssh_pkey', '')

            defaults['db2_conn_method'] = db2_saved.get('conn_method', 'direct')
            defaults['db2_host'] = db2_saved.get('host', '')
            defaults['db2_port'] = db2_saved.get('port', 5432)
            defaults['db2_name'] = db2_saved.get('database', '')
            defaults['db2_user'] = db2_saved.get('user', '')
            defaults['db2_pass'] = db2_saved.get('password', '')
            defaults['db2_ssh_host'] = db2_saved.get('ssh_host', '')
            defaults['db2_ssh_port'] = db2_saved.get('ssh_port', 22)
            defaults['db2_ssh_user'] = db2_saved.get('ssh_user', '')
            defaults['db2_ssh_pass'] = db2_saved.get('ssh_pass', '')
            defaults['db2_ssh_pkey'] = db2_saved.get('ssh_pkey', '')

            defaults['selected_config'] = to_load
            defaults['config_name'] = to_load

            return render_template('index.html',
                                   config_names=config_names,
                                   defaults=defaults)

        # 3) COMPARE SCHEMAS
        elif action == 'compare':
            # Build db1 & db2 dicts exactly as before:
            db1 = {
                'conn_method': request.form.get('db1_conn_method'),
                'host': request.form.get('db1_host', '').strip(),
                'port': int(request.form.get('db1_port', '5432') or 5432),
                'database': request.form.get('db1_name', '').strip(),
                'user': request.form.get('db1_user', '').strip(),
                'password': request.form.get('db1_pass', ''),
                'ssh_host': request.form.get('db1_ssh_host', '').strip(),
                'ssh_port': int(request.form.get('db1_ssh_port', '22') or 22),
                'ssh_user': request.form.get('db1_ssh_user', '').strip(),
                'ssh_pass': request.form.get('db1_ssh_pass', ''),
                'ssh_pkey': request.form.get('db1_ssh_pkey', '').strip()
            }
            db2 = {
                'conn_method': request.form.get('db2_conn_method'),
                'host': request.form.get('db2_host', '').strip(),
                'port': int(request.form.get('db2_port', '5432') or 5432),
                'database': request.form.get('db2_name', '').strip(),
                'user': request.form.get('db2_user', '').strip(),
                'password': request.form.get('db2_pass', ''),
                'ssh_host': request.form.get('db2_ssh_host', '').strip(),
                'ssh_port': int(request.form.get('db2_ssh_port', '22') or 22),
                'ssh_user': request.form.get('db2_ssh_user', '').strip(),
                'ssh_pass': request.form.get('db2_ssh_pass', ''),
                'ssh_pkey': request.form.get('db2_ssh_pkey', '').strip()
            }

            # Attempt to inspect both DBs
            try:
                schema1 = inspect_database(db1)
                if not schema1['tables']:  # Check if tables dict is empty
                    raise Exception("No tables returned from DB1! Connection succeeded but no schema data.")
            except Exception as e:
                for key in defaults:
                    defaults[key] = request.form.get(key, defaults[key])
                flash(f"Error connecting to Database 1: {e}", "danger")
                print(f"Database 1 inspection failed: {e}")
                return render_template('index.html',
                                       config_names=config_names,
                                       defaults=defaults)
            try:
                schema2 = inspect_database(db2)
            except Exception as e:
                flash(f"Error connecting to Database 2: {e}", "danger")
                for key in defaults:
                    defaults[key] = request.form.get(key, defaults[key])
                return render_template('index.html',
                                       config_names=config_names,
                                       defaults=defaults)

            diff = compare_schemas(schema1, schema2)
            return render_template('compare.html',
                                   db1_info=db1,
                                   db2_info=db2,
                                   diff=diff,
                                   db1_schema=schema1,
                                   db2_schema=schema2)

        else:
            # Unknown action, just re-render form
            flash("Unknown action.", "warning")
            for key in defaults:
                defaults[key] = request.form.get(key, defaults[key])
            return render_template('index.html',
                                   config_names=config_names,
                                   defaults=defaults)

    # ----- If GET -----
    else:
        # See if user provided ?config_name=... in the URL
        selected = request.args.get('config_name', '').strip()
        if selected and selected in all_configs:
            cfg = all_configs[selected]
            db1_saved = cfg['db1']
            db2_saved = cfg['db2']

            defaults['db1_conn_method'] = db1_saved.get('conn_method', 'direct')
            defaults['db1_host'] = db1_saved.get('host', '')
            defaults['db1_port'] = db1_saved.get('port', 5432)
            defaults['db1_name'] = db1_saved.get('database', '')
            defaults['db1_user'] = db1_saved.get('user', '')
            defaults['db1_pass'] = db1_saved.get('password', '')
            defaults['db1_ssh_host'] = db1_saved.get('ssh_host', '')
            defaults['db1_ssh_port'] = db1_saved.get('ssh_port', 22)
            defaults['db1_ssh_user'] = db1_saved.get('ssh_user', '')
            defaults['db1_ssh_pass'] = db1_saved.get('ssh_pass', '')
            defaults['db1_ssh_pkey'] = db1_saved.get('ssh_pkey', '')

            defaults['db2_conn_method'] = db2_saved.get('conn_method', 'direct')
            defaults['db2_host'] = db2_saved.get('host', '')
            defaults['db2_port'] = db2_saved.get('port', 5432)
            defaults['db2_name'] = db2_saved.get('database', '')
            defaults['db2_user'] = db2_saved.get('user', '')
            defaults['db2_pass'] = db2_saved.get('password', '')
            defaults['db2_ssh_host'] = db2_saved.get('ssh_host', '')
            defaults['db2_ssh_port'] = db2_saved.get('ssh_port', 22)
            defaults['db2_ssh_user'] = db2_saved.get('ssh_user', '')
            defaults['db2_ssh_pass'] = db2_saved.get('ssh_pass', '')
            defaults['db2_ssh_pkey'] = db2_saved.get('ssh_pkey', '')

            defaults['selected_config'] = selected
            defaults['config_name'] = selected

        # On a plain GET, all other fields remain blank/ default.

        return render_template('index.html',
                               config_names=config_names,
                               defaults=defaults)


def pg_ident(schema, name):
    return f'{schema}."{name}"'


def generate_sql_for_direction(diff, from_db, to_db, db1_info=None, db2_info=None, db1_schema=None, db2_schema=None):
    stmts = []
    from_schema = db1_schema if from_db == "db1" else db2_schema
    to_schema = db2_schema if from_db == "db1" else db1_schema

    # Create missing tables in 'to_db'
    for tbl in diff.get(f"tables_only_in_{from_db}", []):
        table_info = get_table_info(from_schema, tbl)
        stmts.append(get_create_table_sql(tbl, table_info))

    # Add missing columns to existing tables in 'to_db'
    for tbl, info in diff.get("tables_in_both", {}).items():
        for col in info.get(f"columns_only_in_{from_db}", []):
            col_info = get_column_info(from_schema, tbl, col)
            stmts.append(get_add_column_sql(tbl, col, col_info))

    # Add missing indexes
    for tbl, info in diff.get("tables_in_both", {}).items():
        for idx in info.get(f"indexes_only_in_{from_db}", []):
            idx_info = get_index_info(from_schema, tbl, idx)
            stmts.append(get_create_index_sql(idx, idx_info, tbl))

    # Add missing foreign keys
    for tbl, info in diff.get("tables_in_both", {}).items():
        for fk in info.get(f"fks_only_in_{from_db}", []):
            fk_info = get_fk_info(from_schema, tbl, fk)
            stmts.append(get_add_fk_sql(tbl, fk_info))

    # Create missing enums
    for enum in diff.get(f"enums_only_in_{from_db}", []):
        enum_values = get_enum_values(from_schema, enum)
        stmts.append(get_create_enum_sql(enum, enum_values))

    # Add missing enum values
    for enum, einfo in diff.get("enums_in_both", {}).items():
        vals_from = set(einfo[from_db])
        vals_to = set(einfo[to_db])
        missing = vals_from - vals_to
        for v in missing:
            stmts.append(get_add_enum_value_sql(enum, v))

    return "\n".join(stmts) if stmts else "-- No changes needed"


@app.route('/generate_sql_diff', methods=['POST'])
def generate_sql_diff():
    """
    Generate SQL statements to sync schemas in the selected direction.
    Expects JSON: { "direction": "db1_to_db2", "diff": {...} }
    Returns: { "sql": "..." }
    """
    data = request.json
    direction = data.get('direction')
    diff = data.get('diff')

    db1_info = data.get('db1_info')
    db2_info = data.get('db2_info')
    db1_schema = data.get('db1_schema')
    db2_schema = data.get('db2_schema')

    # Here you would call your function to generate SQL based on direction and diff
    if direction == 'db1_to_db2':
        sql = generate_sql_for_direction(
            diff, from_db='db1', to_db='db2',
            db1_info=db1_info, db2_info=db2_info,
            db1_schema=db1_schema, db2_schema=db2_schema
        )
    elif direction == 'db2_to_db1':
        sql = generate_sql_for_direction(
            diff, from_db='db2', to_db='db1',
            db1_info=db1_info, db2_info=db2_info,
            db1_schema=db1_schema, db2_schema=db2_schema
        )
    else:
        return jsonify({'sql': '-- Invalid direction'}), 400

    return jsonify({"sql": sql})


def get_create_table_sql(table_name, table_info):
    """
    table_info: dict with keys 'columns', 'primary_key', 'foreign_keys'
    """
    if '.' in table_name:
        schema, tbl = table_name.split('.', 1)
        table_ident = f'{schema}."{tbl}"'
    else:
        table_ident = f'"{table_name}"'
    lines = []
    for col_name, col in table_info['columns'].items():
        # Compose the column DDL
        coltype = col["data_type"]
        if coltype in ("integer", "bigint", "smallint"):
            typstr = coltype
        elif col.get("numeric_precision") and col.get("numeric_scale") is not None:
            typstr = f'numeric({col["numeric_precision"]},{col["numeric_scale"]})'
        elif col.get("character_maximum_length"):
            typstr = f'{coltype}({col["character_maximum_length"]})'
        else:
            typstr = coltype
        line = f'"{col_name}" {typstr}'
        if col["is_nullable"] == "NO":
            line += " NOT NULL"
        if col.get("column_default"):
            line += f' DEFAULT {col["column_default"]}'
        lines.append(line)
    # Primary key
    if table_info.get("primary_key"):
        pk = ', '.join(f'"{k}"' for k in table_info["primary_key"])
        lines.append(f'PRIMARY KEY ({pk})')
    # Foreign keys (optional)
    for fk in table_info.get("foreign_keys", []):
        col_list = ', '.join(f'"{c}"' for c in fk["columns"])
        ref_col_list = ', '.join(f'"{c}"' for c in fk["referenced_columns"])
        lines.append(
            f'FOREIGN KEY ({col_list}) REFERENCES {fk["referenced_table"]} ({ref_col_list})'
        )
    columns_sql = ',\n  '.join(lines)
    return f'CREATE TABLE {table_ident} (\n  {columns_sql}\n);'


def get_add_column_sql(table_name, col_name, col):
    if '.' in table_name:
        schema, tbl = table_name.split('.', 1)
        table_ident = f'{schema}."{tbl}"'
    else:
        table_ident = f'"{table_name}"'
    coltype = col["data_type"]
    if coltype in ("integer", "bigint", "smallint"):
        typstr = coltype
    elif col.get("numeric_precision") and col.get("numeric_scale") is not None:
        typstr = f'numeric({col["numeric_precision"]},{col["numeric_scale"]})'
    elif col.get("character_maximum_length"):
        typstr = f'{coltype}({col["character_maximum_length"]})'
    else:
        typstr = coltype
    line = f'ALTER TABLE {table_ident} ADD COLUMN "{col_name}" {typstr}'
    if col["is_nullable"] == "NO":
        line += " NOT NULL"
    if col.get("column_default"):
        line += f' DEFAULT {col["column_default"]}'
    return line + ";"


def get_create_index_sql(index_name, index_info, table_name):
    if '.' in table_name:
        schema, tbl = table_name.split('.', 1)
        table_ident = f'{schema}."{tbl}"'
    else:
        table_ident = f'"{table_name}"'
    unique = "UNIQUE " if index_info.get("is_unique") else ""
    cols = ', '.join(f'"{c}"' for c in index_info["columns"])
    return f'CREATE {unique}INDEX "{index_name}" ON {table_ident} ({cols});'


def get_add_fk_sql(table_name, fk):
    if '.' in table_name:
        schema, tbl = table_name.split('.', 1)
        table_ident = f'{schema}."{tbl}"'
    else:
        table_ident = f'"{table_name}"'
    col_list = ', '.join(f'"{c}"' for c in fk["columns"])
    ref_col_list = ', '.join(f'"{c}"' for c in fk["referenced_columns"])
    return (
        f'ALTER TABLE {table_ident} ADD CONSTRAINT "{fk["constraint_name"]}" '
        f'FOREIGN KEY ({col_list}) REFERENCES {fk["referenced_table"]} ({ref_col_list});'
    )


def get_create_enum_sql(enum_name, enum_values):
    if '.' in enum_name:
        schema, name = enum_name.split('.', 1)
        enum_ident = f'{schema}."{name}"'
    else:
        enum_ident = f'"{enum_name}"'
    vals = ', '.join(f"'{v}'" for v in enum_values)
    return f'CREATE TYPE {enum_ident} AS ENUM ({vals});'


def get_table_info(schema, table_name):
    """Return the full table info for table_name from given schema dict."""
    return schema['tables'].get(table_name, {})

def get_column_info(schema, table_name, col_name):
    """Return the column definition for col_name in table_name from schema dict."""
    table = schema['tables'].get(table_name, {})
    return table.get('columns', {}).get(col_name, {})


def get_index_info(schema, table_name, index_name):
    """Return the index definition for index_name in table_name from schema dict."""
    return schema['indexes'].get(table_name, {}).get(index_name, {})


def get_fk_info(schema, table_name, fk_name):
    """Return the FK definition for fk_name in table_name from schema dict."""
    return schema['fks'].get(table_name, {}).get(fk_name, {})


def get_enum_values(schema, enum_name):
    """Return the values for the enum type from schema dict."""
    return schema['enums'].get(enum_name, [])


def get_add_enum_value_sql(enum_name, value):
    return f"ALTER TYPE \"{enum_name}\" ADD VALUE IF NOT EXISTS '{value}';"


if __name__ == '__main__':
    app.run(debug=True)
