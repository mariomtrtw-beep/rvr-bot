import React, { useState, useEffect } from 'react'

export default function GameBoard({ gameState, onGuess }) {
  const [currentGuess, setCurrentGuess] = useState('')
  const [isFlipping, setIsFlipping] = useState(false)

  const wordLength = gameState?.wordLength || 5
  const maxGuesses = 6

  // Get keyboard state from guesses
  const getKeyboardState = () => {
    const state = {}
    
    for (const guess of gameState?.guesses || []) {
      for (let i = 0; i < guess.word.length; i++) {
        const letter = guess.word[i]
        const feedback = guess.feedback[i]
        
        // Upgrade state: absent < present < correct
        if (!state[letter] || 
            (state[letter] === 'absent' && feedback !== 'absent') ||
            (state[letter] === 'present' && feedback === 'correct')) {
          state[letter] = feedback
        }
      }
    }
    
    return state
  }

  const keyboardState = getKeyboardState()

  const handleKeyPress = (key) => {
    if (!gameState || gameState.solved) return
    
    if (key === 'ENTER') {
      if (currentGuess.length === wordLength) {
        setIsFlipping(true)
        onGuess(currentGuess)
        setCurrentGuess('')
        setTimeout(() => setIsFlipping(false), 500)
      }
    } else if (key === 'BACKSPACE') {
      setCurrentGuess(currentGuess.slice(0, -1))
    } else if (currentGuess.length < wordLength && /^[A-Z]$/i.test(key)) {
      setCurrentGuess(currentGuess + key.toUpperCase())
    }
  }

  const getTileClass = (guessIndex, letterIndex) => {
    const guess = gameState.guesses[guessIndex]
    if (!guess) return 'tile'
    
    const feedback = guess.feedback[letterIndex]
    return `tile ${feedback} ${isFlipping && guessIndex === gameState.guesses.length - 1 ? 'flipped' : ''}`
  }

  const getTileContent = (guessIndex, letterIndex) => {
    const guess = gameState.guesses[guessIndex]
    if (!guess) return ''
    return guess.word[letterIndex]
  }

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Enter') {
        handleKeyPress('ENTER')
      } else if (e.key === 'Backspace') {
        handleKeyPress('BACKSPACE')
      } else if (/^[a-zA-Z]$/.test(e.key)) {
        handleKeyPress(e.key.toUpperCase())
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [gameState, currentGuess, onGuess])

  return (
    <div className="space-y-4">
      {/* Guess Grid */}
      <div className="grid grid-rows-6 gap-2 mb-8">
        {Array.from({ length: maxGuesses }).map((_, guessIndex) => (
          <div key={guessIndex} className="grid grid-cols-5 gap-2">
            {Array.from({ length: wordLength }).map((_, letterIndex) => (
              <div
                key={letterIndex}
                className={getTileClass(guessIndex, letterIndex)}
              >
                <div className="tile-inner">
                  <div className="tile-front">
                    {currentGuess.length > letterIndex && guessIndex === gameState.guesses.length
                      ? currentGuess[letterIndex]
                      : getTileContent(guessIndex, letterIndex)
                    }
                  </div>
                  <div className="tile-back">
                    {getTileContent(guessIndex, letterIndex)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>

      {/* Game Status */}
      {gameState && (
        <div className="text-center">
          {gameState.solved ? (
            <div className="space-y-2">
              <p className="text-2xl font-bold text-rvr-cyan">
                🎉 Solved in {gameState.guesses.length} guesses!
              </p>
              <button
                onClick={() => {
                  const shareText = generateShareText(gameState)
                  if (navigator.share) {
                    navigator.share({ text: shareText })
                  } else {
                    navigator.clipboard.writeText(shareText)
                    alert('Results copied to clipboard!')
                  }
                }}
                className="bg-rvr-cyan text-rvr-bg px-6 py-2 rounded font-semibold hover:bg-rvr-cyan/80 transition-colors"
              >
                📤 Share Results
              </button>
            </div>
          ) : gameState.guesses.length >= maxGuesses ? (
            <div className="space-y-2">
              <p className="text-2xl font-bold text-red-500">
                ❌ Game Over!
              </p>
              <p className="text-lg text-gray-400">
                The word was: <span className="font-bold text-white">{gameState.word || '???'}</span>
              </p>
            </div>
          ) : (
            <p className="text-lg text-gray-400">
              {maxGuesses - gameState.guesses.length} guesses remaining
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function generateShareText(gameState) {
  const guessCount = gameState.guesses.length
  const date = new Date().toLocaleDateString()
  
  // Generate emoji grid
  const emojiMap = {
    'correct': '🟩',
    'present': '🟨',
    'absent': '⬛'
  }
  
  const emojiGrid = gameState.guesses.map(guess =>
    guess.feedback.map(f => emojiMap[f]).join('')
  ).join('\n')
  
  return `RVR-Wordle ${date}\n${guessCount}/6\n\n${emojiGrid}`
}
