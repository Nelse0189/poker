import { useState } from 'react'
import './App.css'

type AnalyzeResponse = {
  street: string
  equity: number
  win_probability: number
  pot_odds: number
  required_equity: number
  expected_value_call: number
  action: string
  sizing: number | null
  reasoning: string[]
  hand_class: string | null
  outs_to_improve: number | null
  spr: number | null
  villain_range_used: string | null
  source: 'preflop_chart' | 'postflop_mc'
  confidence: 'high' | 'mixed' | 'low'
}

type SolveStatus = {
  id: string
  status: 'pending' | 'running' | 'done' | 'error'
  progress: number
  message: string
  elapsed_s: number
  error: string | null
  result: {
    root_strategy: Record<string, number>
    root_actions: string[]
    iterations_done: number
    villain_combos_used: number
    exploitability_proxy: number
    top_action: string
    top_action_frequency: number
    villain_range_used: string
  } | null
}

const POSITIONS = ['UTG', 'MP', 'CO', 'BTN', 'SB', 'BB'] as const
const FACING_ACTIONS = [
  { id: 'none', label: 'No action — hero opens' },
  { id: 'limp', label: 'Limped to hero' },
  { id: 'open_raise', label: 'Villain open-raised' },
  { id: 'three_bet', label: "Villain 3-bet hero's open" },
  { id: 'four_bet', label: "Villain 4-bet hero's 3-bet" },
  { id: 'check', label: 'Checked to hero (postflop)' },
  { id: 'bet', label: 'Villain bet (postflop)' },
  { id: 'raise', label: 'Villain raised (postflop)' },
] as const

function normalizeCards(raw: string): string {
  return raw.trim().replace(/\s+/g, '')
}

function formatAction(a: string): string {
  return a.replace(/_/g, ' ')
}

function App() {
  const [heroCards, setHeroCards] = useState('AhKs')
  const [board, setBoard] = useState('')
  const [numOpponents, setNumOpponents] = useState(1)
  const [opponentRange, setOpponentRange] = useState('auto')
  const [pot, setPot] = useState(3.5)
  const [toCall, setToCall] = useState(2.5)
  const [heroStack, setHeroStack] = useState(100)
  const [effectiveStack, setEffectiveStack] = useState<number | ''>('')
  const [bigBlind, setBigBlind] = useState(1)
  const [heroPosition, setHeroPosition] = useState<string>('BTN')
  const [aggressorPosition, setAggressorPosition] = useState<string>('CO')
  const [facingAction, setFacingAction] = useState<string>('open_raise')
  const [sampleCount, setSampleCount] = useState(1200)

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AnalyzeResponse | null>(null)

  const [solving, setSolving] = useState(false)
  const [solveStatus, setSolveStatus] = useState<SolveStatus | null>(null)
  const [solveIterations, setSolveIterations] = useState(400)

  async function onAnalyze(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setResult(null)
    setLoading(true)
    try {
      const body: Record<string, unknown> = {
        hero_cards: normalizeCards(heroCards),
        board: normalizeCards(board),
        num_opponents: numOpponents,
        opponent_range: opponentRange.trim() || 'auto',
        pot,
        to_call: toCall,
        hero_stack: heroStack,
        big_blind: bigBlind,
        sample_count: sampleCount,
        facing_action: facingAction,
      }
      if (heroPosition) body.hero_position = heroPosition
      if (aggressorPosition) body.aggressor_position = aggressorPosition
      if (effectiveStack !== '' && !Number.isNaN(effectiveStack)) {
        body.effective_stack = effectiveStack
      }

      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        const detail = (err as { detail?: string }).detail
        throw new Error(
          typeof detail === 'string' ? detail : `HTTP ${res.status}`,
        )
      }
      const data = (await res.json()) as AnalyzeResponse
      setResult(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  async function onSolveGTO() {
    setError(null)
    setSolveStatus(null)
    setSolving(true)
    try {
      const boardClean = normalizeCards(board)
      if (![6, 8, 10].includes(boardClean.length)) {
        throw new Error(
          'GTO solver requires a postflop board (3, 4, or 5 cards).',
        )
      }
      const body: Record<string, unknown> = {
        hero_cards: normalizeCards(heroCards),
        board: boardClean,
        villain_range: opponentRange.trim() || 'auto',
        pot,
        to_call: toCall,
        hero_stack: heroStack,
        villain_stack:
          effectiveStack !== '' && !Number.isNaN(effectiveStack)
            ? effectiveStack
            : heroStack,
        hero_first_to_act: toCall === 0,
        facing_action: facingAction,
        iterations: solveIterations,
        equity_samples: 250,
      }
      if (heroPosition) body.hero_position = heroPosition
      if (aggressorPosition) body.aggressor_position = aggressorPosition

      const startRes = await fetch('/api/solve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!startRes.ok) {
        const err = await startRes.json().catch(() => ({}))
        const detail = (err as { detail?: string }).detail
        throw new Error(
          typeof detail === 'string' ? detail : `HTTP ${startRes.status}`,
        )
      }
      const start = (await startRes.json()) as SolveStatus
      setSolveStatus(start)

      for (let i = 0; i < 300; i++) {
        await new Promise((r) => setTimeout(r, 600))
        const sRes = await fetch(`/api/solve/${start.id}`)
        if (!sRes.ok) throw new Error(`poll HTTP ${sRes.status}`)
        const cur = (await sRes.json()) as SolveStatus
        setSolveStatus(cur)
        if (cur.status === 'done' || cur.status === 'error') break
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Solve failed')
    } finally {
      setSolving(false)
    }
  }

  const actionClass =
    result?.action === 'fold'
      ? 'danger'
      : result?.action === 'check' || result?.action === 'call'
        ? 'ok'
        : 'warn'

  const isPreflopChart = result?.source === 'preflop_chart'

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>Optimal play (NLHE)</h1>
          <p className="subtitle">
            Preflop GTO charts + postflop Monte Carlo equity via{' '}
            <a
              className="ext"
              href="https://github.com/uoftcprg/pokerkit"
              target="_blank"
              rel="noreferrer"
            >
              pokerkit
            </a>{' '}
            — with SPR, fold equity, and implied odds.
          </p>
        </div>
      </header>

      <div className="grid">
        <form className="panel" onSubmit={onAnalyze}>
          <h2>Game state</h2>

          <div className="field">
            <label htmlFor="hero">Hero hole cards</label>
            <input
              id="hero"
              className="mono"
              value={heroCards}
              onChange={(e) => setHeroCards(e.target.value)}
              placeholder="AhKs"
              maxLength={4}
              required
            />
            <p className="hint">Four chars: rank+suit twice (e.g. Ah Ks → AhKs).</p>
          </div>

          <div className="field">
            <label htmlFor="board">Board</label>
            <input
              id="board"
              className="mono"
              value={board}
              onChange={(e) => setBoard(e.target.value)}
              placeholder="Jc3d5c — empty preflop"
            />
            <p className="hint">0, 3, 4, or 5 cards; no spaces.</p>
          </div>

          <div className="row">
            <div className="field">
              <label htmlFor="heroPos">Hero position</label>
              <select
                id="heroPos"
                value={heroPosition}
                onChange={(e) => setHeroPosition(e.target.value)}
              >
                <option value="">— none —</option>
                {POSITIONS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="aggPos">Aggressor position</label>
              <select
                id="aggPos"
                value={aggressorPosition}
                onChange={(e) => setAggressorPosition(e.target.value)}
              >
                <option value="">— none —</option>
                {POSITIONS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="field">
            <label htmlFor="facing">Facing action</label>
            <select
              id="facing"
              value={facingAction}
              onChange={(e) => setFacingAction(e.target.value)}
            >
              {FACING_ACTIONS.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="range">Villain range</label>
            <input
              id="range"
              className="mono"
              value={opponentRange}
              onChange={(e) => setOpponentRange(e.target.value)}
              placeholder="auto, random, or 22+,AJs+,KQs"
            />
            <p className="hint">
              <span className="mono">auto</span> infers from position + action;{' '}
              <span className="mono">random</span> = any two.
            </p>
          </div>

          <div className="row-3">
            <div className="field">
              <label htmlFor="pot">Pot</label>
              <input
                id="pot"
                type="number"
                min={0}
                step={0.5}
                value={pot}
                onChange={(e) => setPot(Number(e.target.value))}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="tocall">To call</label>
              <input
                id="tocall"
                type="number"
                min={0}
                step={0.5}
                value={toCall}
                onChange={(e) => setToCall(Number(e.target.value))}
              />
            </div>
            <div className="field">
              <label htmlFor="bb">Big blind</label>
              <input
                id="bb"
                type="number"
                min={0.01}
                step={0.5}
                value={bigBlind}
                onChange={(e) => setBigBlind(Number(e.target.value))}
              />
            </div>
          </div>

          <div className="row-3">
            <div className="field">
              <label htmlFor="stack">Hero stack</label>
              <input
                id="stack"
                type="number"
                min={1}
                step={1}
                value={heroStack}
                onChange={(e) => setHeroStack(Number(e.target.value))}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="effstack">Effective stack</label>
              <input
                id="effstack"
                type="number"
                min={0}
                step={1}
                value={effectiveStack}
                placeholder="= hero"
                onChange={(e) =>
                  setEffectiveStack(
                    e.target.value === '' ? '' : Number(e.target.value),
                  )
                }
              />
            </div>
            <div className="field">
              <label htmlFor="opps">Opponents</label>
              <input
                id="opps"
                type="number"
                min={1}
                max={8}
                value={numOpponents}
                onChange={(e) => setNumOpponents(Number(e.target.value))}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="samples">MC samples (postflop only)</label>
            <input
              id="samples"
              type="number"
              min={200}
              max={20000}
              step={100}
              value={sampleCount}
              onChange={(e) => setSampleCount(Number(e.target.value))}
            />
            <p className="hint">Adaptive: stops early when the decision is clear.</p>
          </div>

          <div className="field">
            <label htmlFor="iters">GTO solver iterations</label>
            <input
              id="iters"
              type="number"
              min={50}
              max={4000}
              step={50}
              value={solveIterations}
              onChange={(e) => setSolveIterations(Number(e.target.value))}
            />
            <p className="hint">
              More = closer to equilibrium, slower. ~300 is a good start.
            </p>
          </div>

          <div className="actions">
            <button type="submit" className="btn primary" disabled={loading || solving}>
              {loading && <span className="spinner" aria-hidden />}
              {loading ? 'Running…' : 'Quick recommendation'}
            </button>
            <button
              type="button"
              className="btn"
              disabled={loading || solving}
              onClick={onSolveGTO}
              title="Run a real CFR solver on this spot — takes several seconds"
            >
              {solving && <span className="spinner" aria-hidden />}
              {solving ? 'Solving…' : 'Run GTO solve'}
            </button>
          </div>

          {error && <div className="error">{error}</div>}
        </form>

        <div className="panel">
          <h2>Recommendation</h2>

          {(solving || solveStatus) && (
            <div
              style={{
                marginBottom: 18,
                padding: 12,
                border: '1px solid var(--border)',
                borderRadius: 10,
                background: 'var(--bg-2)',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <strong>GTO solver</strong>
                <span className="mono" style={{ color: 'var(--muted)' }}>
                  {solveStatus
                    ? `${solveStatus.status} · ${Math.round(
                        solveStatus.progress * 100,
                      )}% · ${solveStatus.elapsed_s.toFixed(1)}s`
                    : 'starting…'}
                </span>
              </div>
              <div className="equity-bar" style={{ marginTop: 8 }}>
                <div
                  className="fill"
                  style={{
                    width: `${Math.min(100, (solveStatus?.progress ?? 0) * 100)}%`,
                  }}
                />
              </div>
              {solveStatus?.error && (
                <div className="error" style={{ marginTop: 8 }}>
                  {solveStatus.error}
                </div>
              )}
              {solveStatus?.result && (
                <div style={{ marginTop: 10 }}>
                  <div style={{ color: 'var(--muted)', fontSize: 13 }}>
                    Top action:{' '}
                    <span className="mono" style={{ color: 'var(--text)' }}>
                      {formatAction(solveStatus.result.top_action)}
                    </span>{' '}
                    · {Math.round(
                      solveStatus.result.top_action_frequency * 100,
                    )}
                    %
                  </div>
                  <div style={{ marginTop: 8 }}>
                    {solveStatus.result.root_actions.map((a) => {
                      const p = solveStatus.result!.root_strategy[a] ?? 0
                      return (
                        <div
                          key={a}
                          style={{
                            display: 'grid',
                            gridTemplateColumns: '80px 1fr 50px',
                            gap: 8,
                            alignItems: 'center',
                            marginBottom: 4,
                          }}
                        >
                          <span className="mono" style={{ fontSize: 13 }}>
                            {formatAction(a)}
                          </span>
                          <div className="equity-bar" style={{ marginTop: 0 }}>
                            <div
                              className="fill"
                              style={{ width: `${Math.min(100, p * 100)}%` }}
                            />
                          </div>
                          <span
                            className="mono"
                            style={{ fontSize: 12, textAlign: 'right' }}
                          >
                            {(p * 100).toFixed(0)}%
                          </span>
                        </div>
                      )
                    })}
                  </div>
                  <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 8 }}>
                    {solveStatus.result.iterations_done} iters ·{' '}
                    {solveStatus.result.villain_combos_used} villain combos ·
                    range{' '}
                    <span className="mono">
                      {solveStatus.result.villain_range_used.slice(0, 60)}
                      {solveStatus.result.villain_range_used.length > 60
                        ? '…'
                        : ''}
                    </span>
                  </div>
                </div>
              )}
            </div>
          )}

          {!result && !loading && !solving && !solveStatus && (
            <div className="placeholder">
              <div className="big">♠ ♥</div>
              <p>
                Get a quick recommendation, or run the GTO solver for a real
                mixed strategy (takes a few seconds).
              </p>
            </div>
          )}
          {result && (
            <>
              <div className="result-hero">
                <div className={`result-action ${actionClass}`}>
                  {formatAction(result.action)}
                </div>
                <div className="result-sub">
                  {result.street} ·{' '}
                  {result.sizing != null
                    ? `size to ${result.sizing}`
                    : 'no extra chips'}
                  {' · '}
                  <span className="mono">
                    {result.source === 'preflop_chart'
                      ? 'chart'
                      : 'MC + decision'}
                  </span>
                  {result.confidence !== 'high' && (
                    <> · {result.confidence} confidence</>
                  )}
                </div>
              </div>

              {!isPreflopChart && (
                <>
                  <div className="metric-grid">
                    <div className="metric">
                      <div className="label">Equity</div>
                      <div className="value">
                        {(result.equity * 100).toFixed(1)}%
                      </div>
                    </div>
                    <div className="metric">
                      <div className="label">Need</div>
                      <div className="value">
                        {(result.required_equity * 100).toFixed(1)}%
                      </div>
                    </div>
                    <div className="metric">
                      <div className="label">EV (call)</div>
                      <div className="value">
                        {result.expected_value_call >= 0 ? '+' : ''}
                        {result.expected_value_call.toFixed(1)}
                      </div>
                    </div>
                  </div>

                  <div className="equity-bar">
                    <div
                      className="fill"
                      style={{
                        width: `${Math.min(100, result.equity * 100)}%`,
                      }}
                    />
                    <div
                      className="threshold"
                      style={{
                        left: `${result.required_equity * 100}%`,
                      }}
                    />
                  </div>

                  {(result.hand_class || result.spr != null) && (
                    <div className="metric-grid" style={{ marginTop: 12 }}>
                      {result.hand_class && (
                        <div className="metric">
                          <div className="label">Hand</div>
                          <div className="value" style={{ fontSize: 16 }}>
                            {result.hand_class.replace(/_/g, ' ')}
                          </div>
                        </div>
                      )}
                      {result.spr != null && (
                        <div className="metric">
                          <div className="label">SPR</div>
                          <div className="value">{result.spr.toFixed(1)}</div>
                        </div>
                      )}
                      {result.outs_to_improve != null &&
                        result.outs_to_improve > 0 && (
                          <div className="metric">
                            <div className="label">Outs (approx)</div>
                            <div className="value">
                              {result.outs_to_improve}
                            </div>
                          </div>
                        )}
                    </div>
                  )}
                </>
              )}

              <div className="reasoning">
                <ul>
                  {result.reasoning.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ul>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default App
