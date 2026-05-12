// Game logic helpers
const words = require('./words');

// Check if game is complete
function isGameComplete(gameState) {
  if (gameState.solved) return true;
  if (gameState.guesses.length >= 6) return true;
  return false;
}

// Get game status message
function getGameStatus(gameState) {
  if (gameState.solved) {
    const guessCount = gameState.guesses.length;
    if (guessCount === 1) return '🍀 Lucky guess!';
    if (guessCount === 6) return '💀 Clutch or kick!';
    return `✅ Solved in ${guessCount} guesses`;
  }
  if (gameState.guesses.length >= 6) {
    return '❌ Game over';
  }
  return `${6 - gameState.guesses.length} guesses remaining`;
}

// Calculate score (for potential future use)
function calculateScore(guesses, timeMs) {
  // Base score: 1000 for solving
  let score = 1000;
  
  // Deduct for guesses used (max 600 deduction for 6 guesses)
  score -= (guesses - 1) * 100;
  
  // Bonus for speed (if solved in under 60 seconds)
  if (timeMs < 60000) {
    score += Math.floor((60000 - timeMs) / 100);
  }
  
  return Math.max(100, score);
}

// Get shareable result grid
function getShareGrid(guesses, targetLength) {
  const emojiMap = {
    'correct': '🟩',
    'present': '🟨',
    'absent': '⬛'
  };
  
  return guesses.map(guess => 
    guess.feedback.map(f => emojiMap[f]).join('')
  ).join('\n');
}

// Get keyboard state from guesses
function getKeyboardState(guesses, targetWord) {
  const state = {};
  
  for (const guess of guesses) {
    for (let i = 0; i < guess.word.length; i++) {
      const letter = guess.word[i];
      const feedback = guess.feedback[i];
      
      // Upgrade state: absent < present < correct
      if (!state[letter] || 
          (state[letter] === 'absent' && feedback !== 'absent') ||
          (state[letter] === 'present' && feedback === 'correct')) {
        state[letter] = feedback;
      }
    }
  }
  
  return state;
}

module.exports = {
  isGameComplete,
  getGameStatus,
  calculateScore,
  getShareGrid,
  getKeyboardState
};
