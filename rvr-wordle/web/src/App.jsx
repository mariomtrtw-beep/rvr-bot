import React, { useState, useEffect } from 'react'
import { Routes, Route, useNavigate, useSearchParams } from 'react-router-dom'
import io from 'socket.io-client'
import ModeSelector from './components/ModeSelector.jsx'
import GameBoard from './components/GameBoard.jsx'
import Keyboard from './components/Keyboard.jsx'
import Leaderboard from './components/Leaderboard.jsx'
import AuthCallback from './components/AuthCallback.jsx'

function App() {
  const [socket, setSocket] = useState(null)
  const [user, setUser] = useState(null)
  const [currentMode, setCurrentMode] = useState(null)
  const [gameState, setGameState] = useState(null)
  const [leaderboard, setLeaderboard] = useState([])
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  useEffect(() => {
    // Check for auth callback token
    const token = searchParams.get('token')
    if (token) {
      localStorage.setItem('wordle-token', token)
      navigate('/', { replace: true })
      return
    }

    // Check for existing token
    const savedToken = localStorage.getItem('wordle-token')
    if (!savedToken) {
      // Redirect to Discord OAuth
      window.location.href = `${import.meta.env.VITE_API_URL}/auth/discord`
      return
    }

    // Connect to Socket.io
    const newSocket = io(import.meta.env.VITE_API_URL || 'http://localhost:3001')
    setSocket(newSocket)

    // Authenticate
    newSocket.emit('authenticate', savedToken)

    newSocket.on('authenticated', (data) => {
      setUser(data)
    })

    newSocket.on('auth_error', () => {
      localStorage.removeItem('wordle-token')
      window.location.href = `${import.meta.env.VITE_API_URL}/auth/discord`
    })

    newSocket.on('mode_joined', (data) => {
      setCurrentMode(data.mode)
      setGameState({
        guesses: [],
        solved: false,
        wordLength: data.wordLength
      })
    })

    newSocket.on('guess_result', (data) => {
      setGameState(prev => ({
        ...prev,
        guesses: [...prev.guesses, data],
        solved: false
      }))
    })

    newSocket.on('game_won', (data) => {
      setGameState(prev => ({
        ...prev,
        solved: true,
        guesses: [...prev.guesses, data]
      }))
      
      // Show share dialog
      setTimeout(() => {
        const shareText = `RVR-Wordle ${new Date().toLocaleDateString()}\n${data.guesses}/6\n\n${getShareGrid([...gameState.guesses, data])}`
        if (navigator.share) {
          navigator.share({ text: shareText })
        } else {
          navigator.clipboard.writeText(shareText)
          alert('Results copied to clipboard!')
        }
      }, 1000)
    })

    newSocket.on('game_lost', (data) => {
      setGameState(prev => ({
        ...prev,
        solved: false,
        word: data.word
      }))
    })

    newSocket.on('leaderboard', (data) => {
      setLeaderboard(data)
    })

    newSocket.on('player_joined', (data) => {
      if (currentMode === 'race') {
        // Update race state
        console.log(`${data.username} joined the race`)
      }
    })

    newSocket.on('player_solved', (data) => {
      if (currentMode === 'race') {
        console.log(`${data.username} solved in ${data.guesses} guesses!`)
      }
    })

    return () => {
      newSocket.disconnect()
    }
  }, [])

  function getShareGrid(guesses, finalGuess) {
    const emojiMap = {
      'correct': '🟩',
      'present': '🟨',
      'absent': '⬛'
    }
    
    return guesses.map(guess => 
      guess.feedback.map(f => emojiMap[f]).join('')
    ).join('\n')
  }

  function handleModeSelect(mode) {
    if (!socket) return
    socket.emit('join_mode', mode)
  }

  function handleGuess(guess) {
    if (!socket || !gameState || gameState.solved) return
    socket.emit('submit_guess', guess)
  }

  function handleGetLeaderboard(mode) {
    if (!socket) return
    socket.emit('get_leaderboard', mode)
  }

  if (!user) {
    return <div className="flex items-center justify-center min-h-screen">
      <div className="text-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-rvr-cyan mx-auto mb-4"></div>
        <p className="text-rvr-cyan">Connecting to RVR-Wordle...</p>
      </div>
    </div>
  }

  if (!currentMode) {
    return <ModeSelector user={user} onModeSelect={handleModeSelect} />
  }

  return (
    <div className="min-h-screen bg-rvr-bg text-white">
      {/* Header */}
      <header className="bg-rvr-card/50 border-b border-rvr-cyan/30 p-4">
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <div className="flex items-center space-x-4">
            <span className="text-2xl font-bold text-rvr-cyan">🏎️ RVR-Wordle</span>
            <span className="text-gray-400">|</span>
            <span className="text-lg capitalize">{currentMode}</span>
          </div>
          <div className="flex items-center space-x-4">
            <img 
              src={`https://cdn.discordapp.com/avatars/${user.uid}/${user.avatar}.png`} 
              alt={user.username}
              className="w-8 h-8 rounded-full"
            />
            <span className="text-gray-300">{user.username}</span>
          </div>
        </div>
      </header>

      {/* Main Game Area */}
      <main className="max-w-6xl mx-auto p-4">
        <Routes>
          <Route path="/" element={
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
              {/* Game Board */}
              <div className="lg:col-span-2">
                <GameBoard 
                  gameState={gameState}
                  onGuess={handleGuess}
                />
                <Keyboard 
                  guesses={gameState?.guesses || []}
                  onGuess={handleGuess}
                />
              </div>

              {/* Sidebar */}
              <div className="space-y-6">
                {/* Mode Info */}
                <div className="bg-rvr-card/50 rounded-lg p-4 border border-rvr-cyan/30">
                  <h3 className="text-rvr-cyan font-bold mb-2">Mode: {currentMode}</h3>
                  <p className="text-gray-400 text-sm">
                    {currentMode === 'classic' && 'Solve today\'s word in 6 guesses'}
                    {currentMode === 'race' && 'Race against others to solve first'}
                    {currentMode === 'battle' && 'Solve to earn oil cans and sabotage opponents'}
                    {currentMode === 'timeattack' && 'Solve as fast as possible'}
                  </p>
                </div>

                {/* Leaderboard */}
                <Leaderboard 
                  mode={currentMode}
                  data={leaderboard}
                  onRefresh={() => handleGetLeaderboard(currentMode)}
                />

                {/* Stats */}
                <div className="bg-rvr-card/50 rounded-lg p-4 border border-rvr-cyan/30">
                  <h3 className="text-rvr-cyan font-bold mb-2">Your Stats</h3>
                  <div className="space-y-1 text-sm">
                    <div>Classic: {user.stats.classic.wins}/{user.stats.classic.played} wins</div>
                    <div>Race: {user.stats.race.wins}/{user.stats.race.played} wins</div>
                    <div>Battle: {user.stats.battle.wins}/{user.stats.battle.played} wins</div>
                    <div>Time Attack: {user.stats.timeattack.played} games</div>
                  </div>
                </div>
              </div>
            </div>
          } />

          <Route path="/auth/callback" element={<AuthCallback />} />
        </Routes>
      </main>
    </div>
  )
}

export default App
