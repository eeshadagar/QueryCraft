import { useState, useEffect, useRef } from "react";
import "./App.css";

const API = "http://localhost:5000/api";

const AGENT_META = {
  schema_linking: { label: "Schema Linking", icon: "🔗", color: "#6EE7B7" },
  subproblem: { label: "Subproblem Decomposition", icon: "🧩", color: "#93C5FD" },
  query_plan: { label: "Query Plan (CoT)", icon: "🗺️", color: "#FCD34D" },
  sql_generation: { label: "SQL Generation", icon: "⚡", color: "#F9A8D4" },
  correction_plan: { label: "Correction Plan", icon: "🔍", color: "#FCA5A5" },
  correction_sql: { label: "Correction SQL", icon: "🔧", color: "#FDBA74" },
};

function SchemaPanel({ tables }) {
  const [expanded, setExpanded] = useState({});
  if (!tables) return null;
  return (
    <div className="schema-panel">
      <h3 className="panel-title">Database Schema</h3>
      {Object.entries(tables).map(([table, info]) => (
        <div key={table} className="table-card">
          <button
            className="table-header"
            onClick={() => setExpanded((e) => ({ ...e, [table]: !e[table] }))}
          >
            <span className="table-name">{table}</span>
            <span className="col-count">{info.columns.length} cols</span>
            <span>{expanded[table] ? "▲" : "▼"}</span>
          </button>
          {expanded[table] && (
            <div className="table-cols">
              {info.columns.map((col) => (
                <div key={col.name} className="col-row">
                  <span className={`col-name ${col.pk ? "pk" : ""}`}>
                    {col.pk ? "🔑 " : "   "}
                    {col.name}
                  </span>
                  <span className="col-type">{col.type}</span>
                </div>
              ))}
              {info.foreign_keys && info.foreign_keys.length > 0 && (
                <div className="fk-section">
                  {info.foreign_keys.map((fk, i) => (
                    <div key={i} className="fk-row">
                      {fk.from} to {fk.to_table}.{fk.to_col}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function AgentStep({ step, idx }) {
  const meta = AGENT_META[step.agent] || { label: step.agent, icon: "🤖", color: "#ccc" };
  const [open, setOpen] = useState(false);
  const isCorrection = step.agent.startsWith("correction");

  return (
    <div className={`agent-step ${isCorrection ? "correction" : ""}`}>
      <div className="step-header" onClick={() => setOpen((o) => !o)}>
        <div className="step-left">
          <span className="step-num">{idx + 1}</span>
          <span className="step-icon">{meta.icon}</span>
          <div>
            <div className="step-label">{meta.label}</div>
            {step.attempt && <div className="step-attempt">Attempt #{step.attempt}</div>}
          </div>
        </div>
        <div className="step-right">
          <span className={`step-badge ${step.status}`}>{step.status}</span>
          <span className="step-chevron">{open ? "▲" : "▼"}</span>
        </div>
      </div>

      {open && step.output && (
        <div className="step-output">
          {step.agent === "schema_linking" && (
            <div>
              <div className="output-label">Relevant Tables:</div>
              <div className="tag-row">
                {(step.output.relevant_tables || []).map((t) => (
                  <span key={t} className="tag">{t}</span>
                ))}
              </div>
              <div className="output-label mt">Reasoning:</div>
              <p className="reasoning-text">{step.output.reasoning}</p>
            </div>
          )}
          {step.agent === "subproblem" && (
            <div className="subproblem-grid">
              {Object.entries(step.output)
                .filter(([k, v]) => v && v !== "null" && k !== "subquery_needed" && k !== "set_operation")
                .map(([clause, val]) => (
                  <div key={clause} className="clause-card">
                    <div className="clause-name">{clause}</div>
                    <div className="clause-val">{typeof val === "string" ? val : JSON.stringify(val)}</div>
                  </div>
                ))}
            </div>
          )}
          {(step.agent === "query_plan" || step.agent === "correction_plan") && (
            <pre className="plan-text">{step.output.plan || step.output.correction_plan}</pre>
          )}
          {(step.agent === "sql_generation" || step.agent === "correction_sql") && (
            <pre className="sql-block">{step.output.sql}</pre>
          )}
          {step.output.original_error && (
            <div className="error-box">Error: {step.output.original_error}</div>
          )}
        </div>
      )}
    </div>
  );
}

function ResultTable({ result }) {
  if (!result) return null;
  const { columns, rows } = result;
  if (!rows || rows.length === 0) return <div className="no-results">Query returned 0 rows.</div>;
  return (
    <div className="result-wrapper">
      <div className="result-meta">{rows.length} row{rows.length !== 1 ? "s" : ""} returned</div>
      <div className="table-scroll">
        <table className="result-table">
          <thead>
            <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{cell === null ? <span className="null-val">NULL</span> : String(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const SAMPLES = [
  "What is the average GPA of students in the Computer Science department?",
  "List all students enrolled in more than one course with their course count.",
  "Which department has the highest budget?",
  "Find names of students who got an A grade in any course.",
  "How many courses does each department offer? Order by count descending.",
];

export default function App() {
  const [databases, setDatabases] = useState([]);
  const [selectedDb, setSelectedDb] = useState("");
  const [dbDetail, setDbDetail] = useState(null);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [activeTab, setActiveTab] = useState("pipeline");
  const [loadingDb, setLoadingDb] = useState(false);
  const fileRef = useRef();
  const [cooldown, setCooldown] = useState(0);

  useEffect(() => { loadDatabases(); }, []);

  async function loadDatabases() {
    try {
      const r = await fetch(`${API}/databases`);
      const data = await r.json();
      setDatabases(data);
      if (data.length > 0) {
        setSelectedDb(data[0].db_id);
        loadSchema(data[0].db_id);
      }
    } catch (e) {
      console.error("Could not reach backend:", e);
    }
  }

  async function loadSchema(db_id) {
    const r = await fetch(`${API}/databases/${db_id}/schema`);
    setDbDetail(await r.json());
  }

  async function createSampleDb() {
    setLoadingDb(true);
    await fetch(`${API}/databases/create`, { method: "POST" });
    await loadDatabases();
    setSelectedDb("university");
    await loadSchema("university");
    setLoadingDb(false);
  }

  async function uploadDb(e) {
    const file = e.target.files[0];
    if (!file) return;
    setLoadingDb(true);
    const form = new FormData();
    form.append("file", file);
    form.append("db_id", file.name.replace(/\.[^.]+$/, ""));
    const r = await fetch(`${API}/databases/upload`, { method: "POST", body: form });
    const data = await r.json();
    if (!data.error) {
      await loadDatabases();
      setSelectedDb(data.db_id);
      await loadSchema(data.db_id);
    }
    setLoadingDb(false);
  }

  async function runQuery() {
  if (!question.trim() || !selectedDb || cooldown > 0) return;

  setLoading(true);
  setCooldown(60); 

  const interval = setInterval(() => {
    setCooldown((c) => {
      if (c <= 1) {
        clearInterval(interval);
        return 0;
      }
      return c - 1;
    });
  }, 1000);

  try {
    const r = await fetch(`${API}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, db_id: selectedDb, max_corrections: 1 }),
    });

    setResult(await r.json());
  } catch (e) {
    setResult({ error: e.message });
  }

  setLoading(false);
  }
  return (
    <div className="app">
      <header className="app-header">
        <div className="header-content">
          <div className="logo">
            <span className="logo-sql">QueryCraft</span>
          </div>
          <div className="header-sub">Multi-Agent Text-to-SQL</div>
          <div className="paper-badge">Guided Error Correction</div>
        </div>
      </header>

      <div className="main-layout">
        <aside className="sidebar">
          <div className="sidebar-section">
            <h2 className="section-title">Databases</h2>
            {databases.length === 0 ? (
              <div className="empty-db">
                <p>No databases yet.</p>
                <button className="btn-primary" onClick={createSampleDb} disabled={loadingDb}>
                  {loadingDb ? "Creating..." : "Load University Demo"}
                </button>
              </div>
            ) : (
              <>
                <div className="db-list">
                  {databases.map((db) => (
                    <button
                      key={db.db_id}
                      className={`db-item ${selectedDb === db.db_id ? "active" : ""}`}
                      onClick={() => { setSelectedDb(db.db_id); loadSchema(db.db_id); }}
                    >
                      <span className="db-icon">DB</span>
                      <div>
                        <div className="db-name">{db.db_id}</div>
                        <div className="db-meta">{db.table_count} tables</div>
                      </div>
                    </button>
                  ))}
                </div>
                <div className="db-actions">
                  <button className="btn-secondary" onClick={createSampleDb} disabled={loadingDb}>+ Demo</button>
                  <button className="btn-secondary" onClick={() => fileRef.current.click()} disabled={loadingDb}>Upload</button>
                  <input ref={fileRef} type="file" accept=".db,.sqlite,.csv" onChange={uploadDb} style={{ display: "none" }} />
                </div>
              </>
            )}
          </div>
          {dbDetail && <SchemaPanel tables={dbDetail.tables} />}
        </aside>

        <main className="content">
          <div className="query-card">
            <div className="query-label">Natural Language Question</div>
            <textarea
              className="query-input"
              placeholder="Ask anything about your database in plain English..."
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              rows={3}
            />
            <div className="query-footer">
              <div className="sample-questions">
                {SAMPLES.map((q, i) => (
                  <button key={i} className="sample-btn" onClick={() => setQuestion(q)}>{q}</button>
                ))}
              </div>
                <button 
                className="run-btn" 
                onClick={runQuery} 
                disabled={loading || cooldown > 0 || !selectedDb || !question.trim()}
                >
                {loading 
                    ? "Running..." 
                    : cooldown > 0 
                    ? `Wait ${cooldown}s` 
                    : "Run"}
                </button>
            </div>
          </div>

          {loading && (
            <div className="loading-pipeline">
              <div className="pipeline-animation">
                {Object.values(AGENT_META).map((m, i) => (
                  <div key={i} className="pipe-node" style={{ animationDelay: `${i * 0.3}s` }}>
                    <span>{m.icon}</span>
                    <span className="pipe-label">{m.label}</span>
                  </div>
                ))}
              </div>
              <p className="loading-text">Agents are reasoning through your question...</p>
            </div>
          )}

          {result && (
            <div className="results-section">
              <div className={`status-bar ${result.success ? "success" : "failed"}`}>
                <span>{result.success ? "Success" : "Failed"}</span>
                <span className="status-time">{result.elapsed_seconds}s</span>
                {result.correction_attempts && result.correction_attempts.length > 0 && (
                  <span className="correction-count">{result.correction_attempts.length} correction(s)</span>
                )}
              </div>

              <div className="tabs">
                <button className={`tab ${activeTab === "pipeline" ? "active" : ""}`} onClick={() => setActiveTab("pipeline")}>Pipeline</button>
                <button className={`tab ${activeTab === "sql" ? "active" : ""}`} onClick={() => setActiveTab("sql")}>SQL</button>
                <button className={`tab ${activeTab === "results" ? "active" : ""}`} onClick={() => setActiveTab("results")}>Results</button>
              </div>

              {activeTab === "pipeline" && (
                <div className="pipeline-steps">
                  {result.steps && result.steps.map((s, i) => <AgentStep key={i} step={s} idx={i} />)}
                </div>
              )}
              {activeTab === "sql" && (
                <div className="sql-view">
                  <div className="sql-label">Final SQL</div>
                  <pre className="sql-final">{result.final_sql}</pre>
                  {result.error && <div className="error-box">{result.error}</div>}
                </div>
              )}
              {activeTab === "results" && (
                <div className="result-view">
                  <ResultTable result={result.result} />
                </div>
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}