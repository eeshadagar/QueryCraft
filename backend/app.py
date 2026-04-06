
from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3, os, json, re, tempfile, time
import pandas as pd
import google.generativeai as genai
from dotenv import load_dotenv
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

app = Flask(__name__)
CORS(app)

MODEL = genai.GenerativeModel("gemini-2.5-flash")

ERROR_TAXONOMY = """
SQL Error Taxonomy:
SYNTAX: sql_syntax_error, invalid_alias
SCHEMA_LINK: table_missing, col_missing, ambiguous_col, incorrect_foreign_key
JOIN: join_missing, join_wrong_type, extra_table, incorrect_col
FILTER: where_missing, condition_wrong_col, condition_type_mismatch
AGGREGATION: agg_no_groupby, groupby_missing_col, having_without_groupby, having_incorrect, having_vs_where
VALUE: hardcoded_value, value_format_wrong
SUBQUERY: unused_subquery, subquery_missing, subquery_correlation_error
SET_OPS: union_missing, intersect_missing, except_missing
OTHER: order_by_missing, limit_missing, duplicate_select, unsupported_function, extra_values_selected
"""

db_store = {}

def get_schema_string(db_id):
    return db_store.get(db_id, {}).get("schema")

def execute_sql(db_id, sql):
    if db_id not in db_store:
        return None, "Database not found"
    try:
        conn = sqlite3.connect(db_store[db_id]["path"])
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description] if cursor.description else []
        conn.close()
        return {"columns": cols, "rows": rows}, None
    except Exception as e:
        return None, str(e)

def llm_call(system_prompt, user_prompt):
    try:
        full_prompt = f"""
{system_prompt}

User Question:
{user_prompt}
"""

        response = MODEL.generate_content(full_prompt)

        return response.text.strip()

    except Exception as e:
        print("GEMINI ERROR:", e)
        return f"ERROR: {str(e)}"

def schema_linking_agent(question, schema):
    system = """You are a Schema Linking Agent for Text-to-SQL.
Given a natural language question and a database schema, identify relevant tables/columns/keys.
Respond ONLY as JSON:
{"relevant_tables":[],"relevant_columns":{},"primary_keys":{},"foreign_keys":[],"reasoning":""}"""
    result = llm_call(system, f"Question: {question}\n\nSchema:\n{schema}")
    try:
        cleaned = re.sub(r"```json|```", "", result).strip()
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}")+1]
        return json.loads(cleaned)
    except:
        return {"relevant_tables": [], "relevant_columns": {}, "primary_keys": {}, "foreign_keys": [], "reasoning": result}

def subproblem_agent(question, schema_link):
    system = """You are a Subproblem Decomposition Agent. Decompose the query into SQL clause subproblems.
Respond ONLY as JSON with keys: SELECT, FROM, JOIN, WHERE, GROUP_BY, HAVING, ORDER_BY, LIMIT, subquery_needed, set_operation"""
    result = llm_call(system, f"Question: {question}\n\nLinked Schema:\n{json.dumps(schema_link, indent=2)}")
    try:
        cleaned = re.sub(r"```json|```", "", result).strip()
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}")+1]
        return json.loads(cleaned)
    except:
        return {"SELECT": result}

def query_plan_agent(question, schema_link, subproblems):
    system = """You are a Query Plan Agent using Chain-of-Thought reasoning.
Generate a STEP-BY-STEP plan. DO NOT write SQL yet.
Format: STEP 1: ... STEP 2: ... REASONING: ..."""
    return llm_call(system, f"Question: {question}\n\nLinked Schema:\n{json.dumps(schema_link,indent=2)}\n\nSubproblems:\n{json.dumps(subproblems,indent=2)}")

def sql_agent(question, schema, query_plan):
    system = "You are a SQL generation agent. Generate valid SQLite SQL. Output ONLY raw SQL, no markdown, no semicolons."
    sql = llm_call(system, f"Question: {question}\n\nSchema:\n{schema}\n\nQuery Plan:\n{query_plan}")
    return re.sub(r"```sql|```", "", sql).strip().rstrip(";")

def correction_plan_agent(question, schema, incorrect_sql, error_msg):
    system = f"""You are a Correction Plan Agent using Chain-of-Thought + error taxonomy.
{ERROR_TAXONOMY}
Identify error types and produce a step-by-step CORRECTION PLAN. Do NOT write SQL."""
    return llm_call(system, f"Question: {question}\n\nSchema:\n{schema}\n\nIncorrect SQL:\n{incorrect_sql}\n\nError:\n{error_msg}")

def correction_sql_agent(question, schema, incorrect_sql, correction_plan):
    system = "You are a Correction SQL Agent. Fix the SQL using the correction plan. Output ONLY raw SQL, no markdown, no semicolons."
    sql = llm_call(system, f"Question: {question}\n\nSchema:\n{schema}\n\nIncorrect SQL:\n{incorrect_sql}\n\nCorrection Plan:\n{correction_plan}")
    return re.sub(r"```sql|```", "", sql).strip().rstrip(";")

@app.route("/api/query", methods=["POST"])
def run_pipeline():
    data = request.json
    question = data.get("question", "")
    db_id = data.get("db_id", "")
    max_corrections = data.get("max_corrections", 1)
    if not question or not db_id:
        return jsonify({"error": "question and db_id required"}), 400
    schema = get_schema_string(db_id)
    if not schema:
        return jsonify({"error": f"Database '{db_id}' not found"}), 404

    steps = []
    start_time = time.time()

    steps.append({"agent": "schema_linking", "status": "running"})
    schema_link = schema_linking_agent(question, schema)
    steps[-1].update({"status": "done", "output": schema_link})

    steps.append({"agent": "subproblem", "status": "running"})
    subproblems = subproblem_agent(question, schema_link)
    steps[-1].update({"status": "done", "output": subproblems})

    steps.append({"agent": "query_plan", "status": "running"})
    query_plan = query_plan_agent(question, schema_link, subproblems)
    steps[-1].update({"status": "done", "output": {"plan": query_plan}})

    steps.append({"agent": "sql_generation", "status": "running"})
    sql = sql_agent(question, schema, query_plan)
    steps[-1].update({"status": "done", "output": {"sql": sql}})

    result, error = execute_sql(db_id, sql)
    correction_attempts = []
    for attempt in range(1, max_corrections + 1):
        if not error:
            break
        steps.append({"agent": "correction_plan", "status": "running", "attempt": attempt})
        correction_plan = correction_plan_agent(question, schema, sql, error)
        steps[-1].update({"status": "done", "output": {"plan": correction_plan, "original_error": error}})

        steps.append({"agent": "correction_sql", "status": "running", "attempt": attempt})
        sql = correction_sql_agent(question, schema, sql, correction_plan)
        steps[-1].update({"status": "done", "output": {"sql": sql}})

        correction_attempts.append({"attempt": attempt, "correction_plan": correction_plan, "corrected_sql": sql, "error": error})
        result, error = execute_sql(db_id, sql)

    return jsonify({
        "question": question, "db_id": db_id, "final_sql": sql,
        "result": result, "error": error, "success": error is None,
        "steps": steps, "correction_attempts": correction_attempts,
        "elapsed_seconds": round(time.time() - start_time, 2)
    })

@app.route("/api/databases", methods=["GET"])
def list_databases():
    return jsonify([{"db_id": k, "tables": list(v["tables"].keys()), "table_count": len(v["tables"])} for k, v in db_store.items()])

@app.route("/api/databases/<db_id>/schema", methods=["GET"])
def get_schema(db_id):
    if db_id not in db_store:
        return jsonify({"error": "not found"}), 404
    return jsonify({"db_id": db_id, "schema": db_store[db_id]["schema"], "tables": db_store[db_id]["tables"]})

@app.route("/api/databases/upload", methods=["POST"])
def upload_db():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    filename = file.filename.lower()
    db_id = request.form.get("db_id", filename.split(".")[0])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")

    try:
        conn = sqlite3.connect(tmp.name)
        if filename.endswith(".csv"):
            df = pd.read_csv(file)

            df.columns = [col.strip().replace(" ", "_") for col in df.columns]

            table_name = db_id
            df.to_sql(table_name, conn, index=False, if_exists="replace")

        elif filename.endswith(".db") or filename.endswith(".sqlite"):
            file.save(tmp.name)

        else:
            return jsonify({"error": "Unsupported file type"}), 400

        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables_raw = cursor.fetchall()

        tables = {}
        schema_lines = []

        for (table_name,) in tables_raw:
            cursor.execute(f"PRAGMA table_info({table_name})")
            cols = cursor.fetchall()

            cursor.execute(f"PRAGMA foreign_key_list({table_name})")
            fks = cursor.fetchall()

            col_defs = [
                f"  {c[1]} {c[2]}{' PRIMARY KEY' if c[5] else ''}"
                for c in cols
            ]

            fk_lines = [
                f"  FOREIGN KEY ({f[3]}) REFERENCES {f[2]}({f[4]})"
                for f in fks
            ]

            tables[table_name] = {
                "columns": [
                    {"name": c[1], "type": c[2], "pk": bool(c[5])}
                    for c in cols
                ],
                "foreign_keys": [
                    {"from": f[3], "to_table": f[2], "to_col": f[4]}
                    for f in fks
                ]
            }

            schema_lines.append(
                f"CREATE TABLE {table_name} (\n" +
                ",\n".join(col_defs + fk_lines) +
                "\n);"
            )

        conn.close()

        db_store[db_id] = {
            "schema": "\n\n".join(schema_lines),
            "tables": tables,
            "path": tmp.name
        }

        return jsonify({"db_id": db_id, "tables": list(tables.keys())})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/databases/create", methods=["POST"])
def create_sample_db():
    db_id = "university"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
    CREATE TABLE students (student_id INTEGER PRIMARY KEY, name TEXT, age INTEGER, department_id INTEGER, gpa REAL, FOREIGN KEY (department_id) REFERENCES departments(department_id));
    CREATE TABLE departments (department_id INTEGER PRIMARY KEY, name TEXT, building TEXT, budget REAL);
    CREATE TABLE courses (course_id INTEGER PRIMARY KEY, title TEXT, credits INTEGER, department_id INTEGER, FOREIGN KEY (department_id) REFERENCES departments(department_id));
    CREATE TABLE enrollments (enrollment_id INTEGER PRIMARY KEY, student_id INTEGER, course_id INTEGER, grade TEXT, semester TEXT, FOREIGN KEY (student_id) REFERENCES students(student_id), FOREIGN KEY (course_id) REFERENCES courses(course_id));
    INSERT INTO departments VALUES (1,'Computer Science','Tech Hall',500000),(2,'Mathematics','Science Hall',300000),(3,'Physics','Science Hall',250000);
    INSERT INTO students VALUES (1,'Alice Johnson',22,1,3.9),(2,'Bob Smith',20,1,3.2),(3,'Carol White',21,2,3.7),(4,'Dave Brown',23,3,2.9),(5,'Eve Davis',22,2,3.5);
    INSERT INTO courses VALUES (1,'Intro to Programming',3,1),(2,'Data Structures',3,1),(3,'Calculus I',4,2),(4,'Linear Algebra',3,2),(5,'Quantum Mechanics',4,3);
    INSERT INTO enrollments VALUES (1,1,1,'A','Fall2024'),(2,1,2,'A-','Fall2024'),(3,2,1,'B+','Fall2024'),(4,2,2,'B','Spring2024'),(5,3,3,'A','Fall2024'),(6,3,4,'A-','Spring2024'),(7,4,5,'C+','Fall2024'),(8,5,3,'B+','Fall2024'),(9,5,4,'A','Spring2024');
    """)
    conn.commit(); conn.close()
    db_store[db_id] = {
        "schema": """CREATE TABLE students (\n  student_id INTEGER  PRIMARY KEY,\n  name TEXT,\n  age INTEGER,\n  department_id INTEGER,\n  gpa REAL,\n  FOREIGN KEY (department_id) REFERENCES departments(department_id)\n);\n\nCREATE TABLE departments (\n  department_id INTEGER  PRIMARY KEY,\n  name TEXT,\n  building TEXT,\n  budget REAL\n);\n\nCREATE TABLE courses (\n  course_id INTEGER  PRIMARY KEY,\n  title TEXT,\n  credits INTEGER,\n  department_id INTEGER,\n  FOREIGN KEY (department_id) REFERENCES departments(department_id)\n);\n\nCREATE TABLE enrollments (\n  enrollment_id INTEGER  PRIMARY KEY,\n  student_id INTEGER,\n  course_id INTEGER,\n  grade TEXT,\n  semester TEXT,\n  FOREIGN KEY (student_id) REFERENCES students(student_id),\n  FOREIGN KEY (course_id) REFERENCES courses(course_id)\n);""",
        "tables": {
            "students": {"columns": [{"name":"student_id","type":"INTEGER","pk":True},{"name":"name","type":"TEXT","pk":False},{"name":"age","type":"INTEGER","pk":False},{"name":"department_id","type":"INTEGER","pk":False},{"name":"gpa","type":"REAL","pk":False}],"foreign_keys":[{"from":"department_id","to_table":"departments","to_col":"department_id"}]},
            "departments": {"columns":[{"name":"department_id","type":"INTEGER","pk":True},{"name":"name","type":"TEXT","pk":False},{"name":"building","type":"TEXT","pk":False},{"name":"budget","type":"REAL","pk":False}],"foreign_keys":[]},
            "courses": {"columns":[{"name":"course_id","type":"INTEGER","pk":True},{"name":"title","type":"TEXT","pk":False},{"name":"credits","type":"INTEGER","pk":False},{"name":"department_id","type":"INTEGER","pk":False}],"foreign_keys":[{"from":"department_id","to_table":"departments","to_col":"department_id"}]},
            "enrollments": {"columns":[{"name":"enrollment_id","type":"INTEGER","pk":True},{"name":"student_id","type":"INTEGER","pk":False},{"name":"course_id","type":"INTEGER","pk":False},{"name":"grade","type":"TEXT","pk":False},{"name":"semester","type":"TEXT","pk":False}],"foreign_keys":[{"from":"student_id","to_table":"students","to_col":"student_id"},{"from":"course_id","to_table":"courses","to_col":"course_id"}]}
        },
        "path": tmp.name
    }
    return jsonify({"db_id": db_id, "tables": ["students","departments","courses","enrollments"]})

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "databases": list(db_store.keys())})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
