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

          <div className="actions">
            <button type="submit" className="btn primary" disabled={loading}>
              {loading && <span className="spinner" aria-hidden />}
              {loading ? 'Running…' : 'Get recommendation'}
            </button>
          </div>

          {error && <div className="error">{error}</div>}
        </form>

        <div className="panel">
          <h2>Recommendation</h2>
          {!result && !loading && (
            <div className="placeholder">
              <div className="big">♠ ♥</div>
              <p>Submit the form to see the line, equity, and reasoning.</p>
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
