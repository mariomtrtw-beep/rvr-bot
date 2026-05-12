import React from 'react'

export default function Keyboard({ guesses, onGuess }) {
  // Calculate keyboard state from guesses
  const getKeyboardState = () => {
    const state = {}
    
    for (const guess of guesses || []) {
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

  const rows = [
    ['Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P'],
    ['A', 'S', 'D', 'F', 'G', 'H', 'J', 'K', 'L'],
    ['ENTER', 'Z', 'X', 'C', 'V', 'B', 'N', 'M', 'BACKSPACE']
  ]

  const handleKeyClick = (key) => {
    onGuess(key)
  }

  const getKeyClass = (key) => {
    let baseClass = 'key-btn'
    
    if (key === 'ENTER' || key === 'BACKSPACE') {
      baseClass += ' text-xs px-2 py-3'
    } else {
      baseClass += ' text-sm px-3 py-4'
    }
    
    const state = keyboardState[key]
    if (state === 'correct') {
      baseClass += ' correct'
    } else if (state === 'present') {
      baseClass += ' present'
    } else if (state === 'absent') {
      baseClass += ' absent'
    }
    
    return baseClass
  }

  const getKeyContent = (key) => {
    if (key === 'ENTER') return 'ENTER'
    if (key === 'BACKSPACE') return '←'
    return key
  }

  return (
    <div className="flex flex-col space-y-2 p-4 bg-rvr-card/30 rounded-lg">
      {rows.map((row, rowIndex) => (
        <div key={rowIndex} className="flex justify-center space-x-1">
          {row.map((key, keyIndex) => (
            <button
              key={keyIndex}
              onClick={() => handleKeyClick(key)}
              className={getKeyClass(key)}
              disabled={keyboardState[key] === 'correct'}
            >
              {getKeyContent(key)}
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}
