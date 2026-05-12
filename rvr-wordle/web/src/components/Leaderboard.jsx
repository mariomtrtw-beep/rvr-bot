import React, { useState, useEffect } from 'react'

export default function Leaderboard({ mode, data, onRefresh }) {
  const [isRefreshing, setIsRefreshing] = useState(false)

  const handleRefresh = async () => {
    setIsRefreshing(true)
    await onRefresh()
    setTimeout(() => setIsRefreshing(false), 500)
  }

  const getModeIcon = () => {
    switch (mode) {
      case 'classic': return '🎯'
      case 'race': return '🏁'
      case 'battle': return '⚔️'
      case 'timeattack': return '⏱️'
      default: return '🏆'
    }
  }

  const formatTime = (ms) => {
    const seconds = Math.floor(ms / 1000)
    const minutes = Math.floor(seconds / 60)
    const remainingSeconds = seconds % 60
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`
  }

  const renderEntry = (entry, index) => {
    const isTimeAttack = mode === 'timeattack'
    
    return (
      <div key={entry.userId || index} className="flex items-center justify-between p-3 bg-rvr-card/30 rounded-lg border border-rvr-cyan/20">
        <div className="flex items-center space-x-3">
          <span className="text-lg font-bold text-rvr-cyan">#{index + 1}</span>
          <span className="font-semibold">{entry.username}</span>
        </div>
        
        <div className="text-right">
          {isTimeAttack ? (
            <div>
              <div className="text-rvr-cyan font-bold">{formatTime(entry.timeMs)}</div>
              <div className="text-xs text-gray-400">{entry.guesses} guesses</div>
            </div>
          ) : (
            <div className="flex items-center space-x-2">
              <span className="text-rvr-cyan font-bold">{entry.guesses}</span>
              <span className="text-gray-400">guesses</span>
              {entry.time && (
                <>
                  <span className="text-gray-400">•</span>
                  <span className="text-rvr-cyan">{formatTime(entry.time)}</span>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center space-x-2">
          <span className="text-xl font-bold">{getModeIcon()}</span>
          <span className="text-lg capitalize">{mode} Leaderboard</span>
        </div>
        
        <button
          onClick={handleRefresh}
          disabled={isRefreshing}
          className="bg-rvr-cyan text-rvr-bg px-4 py-2 rounded font-semibold disabled:opacity-50 transition-opacity"
        >
          {isRefreshing ? '🔄' : '🔄'} Refresh
        </button>
      </div>

      {/* Leaderboard */}
      <div className="space-y-2">
        {data.length === 0 ? (
          <div className="text-center py-8 text-gray-400">
            <p>No entries yet</p>
            <p className="text-sm mt-2">Be the first to solve today's word!</p>
          </div>
        ) : (
          data.map((entry, index) => renderEntry(entry, index))
        )}
      </div>

      {/* Mode-specific info */}
      <div className="mt-6 p-4 bg-rvr-card/30 rounded-lg border border-rvr-cyan/20">
        <h4 className="font-semibold text-rvr-cyan mb-2">Mode Info</h4>
        <div className="text-sm text-gray-300 space-y-1">
          {mode === 'classic' && (
            <>
              <p>• Solve today's word in 6 guesses</p>
              <p>• Fewer guesses = better score</p>
            </>
          )}
          {mode === 'race' && (
            <>
              <p>• Real-time competition</p>
              <p>• First to solve wins bonus points</p>
              <p>• Watch others solve in real-time</p>
            </>
          )}
          {mode === 'battle' && (
            <>
              <p>• Earn 🛢️ oil cans by solving</p>
              <p>• Use oil to sabotage opponents</p>
              <p>• Block letters or skip turns</p>
            </>
          )}
          {mode === 'timeattack' && (
            <>
              <p>• Solve as fast as possible</p>
              <p>• Time-based scoring</p>
              <p>• Global speed leaderboard</p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
